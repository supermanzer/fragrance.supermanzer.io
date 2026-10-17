"""Context carried by the CRITICAL pages and by the swallowed-error log lines (ids only, never secrets)."""

import logging
from datetime import timedelta
from unittest.mock import patch

import stripe
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from fragrance.billing import _grant_period_for_invoice, revoke_for_dispute
from fragrance.models import BetaSignup, Plan, Subscription
from fragrance.tests.support.bases import StripeClientTestCase
from fragrance.tests.support.stripe_fakes import (
    _STRIPE_TEST_SETTINGS,
    _list_object,
    _sdk_invoice,
    _sdk_line,
    _sdk_payment_intent,
)
from rest_framework.test import APIClient


@override_settings(**_STRIPE_TEST_SETTINGS)
class DisputeCriticalContextTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="disputeduser", email="disputeduser@example.com")
        self.subscription = Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_dispute_1", status="active",
            access_expires_at=timezone.now() + timedelta(days=20),
        )
        self.stripe.v1.checkout.sessions.list.return_value = _list_object(data=[])
        self.stripe.v1.subscriptions.list.return_value = _list_object(data=[])
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_dispute", status="succeeded", customer="cus_dispute_1"
        )

    def test_revocation_page_names_user_dispute_payment_intent_amount_and_currency(self) -> None:
        dispute = {"id": "dp_42", "payment_intent": "pi_dispute", "amount": 2500, "currency": "usd"}

        with self.assertLogs("fragrance.billing", level="CRITICAL") as logs:
            revoke_for_dispute(dispute=dispute)

        record = logs.records[0]
        message = record.getMessage()
        self.assertIn(f"user {self.user.pk} ", message)
        for expected in ("dp_42", "pi_dispute", "amount=2500", "currency=usd", "cus_dispute_1"):
            self.assertIn(expected, message)
        self.assertEqual(record.alert_key, "dp_42")  # type: ignore[attr-defined]
        self.assertNotIn("disputeduser", message)

    def test_unresolvable_payment_intent_is_logged_with_dispute_id_and_traceback(self) -> None:
        self.stripe.v1.payment_intents.retrieve.side_effect = stripe.InvalidRequestError(
            "no such payment_intent: leaked-detail", None
        )

        with self.assertLogs("fragrance.billing", level="ERROR") as logs:
            revoke_for_dispute(dispute={"id": "dp_9", "payment_intent": "pi_missing"})

        record = logs.records[0]
        self.assertIn("dp_9", record.getMessage())
        self.assertIsNotNone(record.exc_info)
        self.assertNotIn("leaked-detail", logs.output[0])


@override_settings(**_STRIPE_TEST_SETTINGS)
class InvoiceCriticalContextTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.plan = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, billing_interval="month",
        )
        self.user = User.objects.create_user(username="locked", email="locked@example.com")
        BetaSignup.objects.create(
            email="locked@example.com", normalized_email="locked@example.com",
            status="converted", invited_user=self.user,
        )
        self.subscription = Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_locked", status="revoked"
        )

    def _invoice(self, *, invoice_id: str, period_end: int | None) -> stripe.Invoice:
        invoice = _sdk_invoice(
            invoice_id=invoice_id, customer_id="cus_locked", subscription_id="sub_locked",
            amount_paid=400, lines=[_sdk_line(price_id="price_mo", period_end=period_end)],
        )
        invoice["currency"] = "usd"
        return invoice

    def test_locked_account_page_carries_user_payment_intent_amount_currency_and_key(self) -> None:
        invoice = self._invoice(invoice_id="in_locked", period_end=int(timezone.now().timestamp()) + 86400)

        with self.assertLogs("fragrance.billing", level="CRITICAL") as logs:
            _grant_period_for_invoice(invoice=invoice, payment_intent_id="pi_locked")

        record = logs.records[0]
        message = record.getMessage()
        self.assertIn("MONEY RECEIVED ON A LOCKED ACCOUNT", message)
        for expected in ("in_locked", "400 USD", "pi_locked", f"user {self.user.pk},", "cus_locked"):
            self.assertIn(expected, message)
        self.assertEqual(record.alert_key, "in_locked")  # type: ignore[attr-defined]
        self.assertNotIn("locked@example.com", message)

    def test_no_period_page_carries_the_same_context(self) -> None:
        self.subscription.status = "active"
        self.subscription.save(update_fields=["status"])
        invoice = self._invoice(invoice_id="in_noperiod", period_end=None)

        with self.assertLogs("fragrance.billing", level="CRITICAL") as logs:
            _grant_period_for_invoice(invoice=invoice, payment_intent_id="pi_noperiod")

        record = logs.records[0]
        message = record.getMessage()
        self.assertIn("PAID INVOICE WITH NO PERIOD", message)
        for expected in ("in_noperiod", "400 USD", "pi_noperiod", f"user {self.user.pk},", "beta-monthly"):
            self.assertIn(expected, message)
        self.assertEqual(record.alert_key, "in_noperiod")  # type: ignore[attr-defined]

    def test_invoice_without_currency_or_payment_intent_still_pages(self) -> None:
        invoice = _sdk_invoice(
            invoice_id="in_bare", customer_id="cus_locked", subscription_id="sub_locked",
            amount_paid=400, lines=[_sdk_line(price_id="price_mo", period_end=int(timezone.now().timestamp()) + 86400)],
        )

        with self.assertLogs("fragrance.billing", level="CRITICAL") as logs:
            _grant_period_for_invoice(invoice=invoice)

        self.assertIn("400 ? minor units", logs.records[0].getMessage())
        self.assertIn("payment_intent ?", logs.records[0].getMessage())


class PasswordChangeBlacklistTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="dave", email="dave@example.com", password="old-Pass-12345")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.payload = {
            "current_password": "old-Pass-12345",
            "new_password": "new-Pass-98765-xyz",
            "new_password_confirm": "new-Pass-98765-xyz",
            "refresh": "opaque-refresh-token-value",
        }

    def test_blacklist_failure_is_logged_with_user_id_but_never_the_token_or_password(self) -> None:
        with patch(
            "fragrance.views.billing.blacklist_all_refresh_tokens", side_effect=RuntimeError("db is on fire")
        ):
            with self.assertLogs("fragrance.views", level="ERROR") as logs:
                response = self.client.post(reverse("password_change"), self.payload, format="json")

        self.assertEqual(response.status_code, 503)
        self.assertIn(f"user {self.user.pk}", logs.output[0])
        joined = "\n".join(logs.output)
        self.assertNotIn("opaque-refresh-token-value", joined)
        self.assertNotIn("new-Pass-98765-xyz", joined)
        self.assertEqual(logging.getLevelName(logs.records[0].levelno), "ERROR")
