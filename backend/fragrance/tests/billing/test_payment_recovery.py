from datetime import timedelta
from typing import Any
from unittest.mock import patch

import stripe
from celery.exceptions import Retry
from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from fragrance.billing import (
    PAYMENT_RECOVERY_RETRYING_WINDOW,
    payment_recovery_payload,
    recover_past_due_invoice,
    subscription_status_payload,
)
from fragrance.models import StripeEvent, Subscription
from fragrance.tasks import STRIPE_HANDLED_EVENT_TYPES, process_stripe_event
from fragrance.tests.support.bases import StripeClientTestCase
from fragrance.tests.support.stripe_fakes import (
    _KEY,
    _STRIPE_TEST_SETTINGS,
    _list_object,
    _sub_list,
)

_CUSTOMER = "cus_recover_1"
_SUBSCRIPTION = "sub_recover_1"
_METHOD = "pm_new_card"


def _invoice(*, invoice_id: str = "in_open_1", amount_remaining: int = 400) -> stripe.Invoice:
    return stripe.Invoice.construct_from(
        {
            "id": invoice_id,
            "object": "invoice",
            "customer": _CUSTOMER,
            "status": "open",
            "amount_remaining": amount_remaining,
        },
        _KEY,
    )


def _paid(*, invoice_id: str = "in_open_1") -> stripe.Invoice:
    return stripe.Invoice.construct_from(
        {"id": invoice_id, "object": "invoice", "status": "paid", "amount_remaining": 0}, _KEY
    )


def _stripe_subscription(*, status: str) -> stripe.Subscription:
    return stripe.Subscription.construct_from(
        {"id": _SUBSCRIPTION, "object": "subscription", "status": status}, _KEY
    )


def _card_error() -> stripe.CardError:
    return stripe.CardError("Your card was declined.", param=None, code="card_declined")


def _attached(*, customer: Any = _CUSTOMER, method_id: str = _METHOD) -> dict:
    return {"id": method_id, "object": "payment_method", "customer": customer}


@override_settings(**_STRIPE_TEST_SETTINGS)
class RecoverPastDueInvoiceTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="recover", email="recover@example.com")
        self.subscription = Subscription.objects.create(
            user=self.user,
            stripe_customer_id=_CUSTOMER,
            stripe_subscription_id=_SUBSCRIPTION,
            status="active",
            access_expires_at=timezone.now() - timedelta(days=1),
            last_paid_at=timezone.now() - timedelta(days=31),
        )
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(status="past_due")
        self.stripe.v1.invoices.list.return_value = _list_object(data=[_invoice()])
        self.stripe.v1.invoices.pay.return_value = _paid()

    def _reload(self) -> Subscription:
        self.subscription.refresh_from_db()
        return self.subscription

    def test_past_due_invoice_is_paid_with_the_new_method_explicitly(self) -> None:
        outcome = recover_past_due_invoice(payment_method=_attached())

        self.assertEqual(outcome, "paid")
        self.stripe.v1.invoices.pay.assert_called_once_with(
            invoice="in_open_1",
            params={"payment_method": _METHOD},
            options={"idempotency_key": f"recover-in_open_1-{_METHOD}"},
        )
        self.stripe.v1.invoices.list.assert_called_once_with(
            params={
                "customer": _CUSTOMER,
                "subscription": _SUBSCRIPTION,
                "status": "open",
                "limit": 10,
            }
        )
        row = self._reload()
        self.assertEqual(row.payment_recovery_state, "retrying")
        self.assertIsNotNone(row.payment_recovery_attempted_at)

    def test_customer_object_form_is_normalized_to_its_id(self) -> None:
        outcome = recover_past_due_invoice(payment_method=_attached(customer={"id": _CUSTOMER}))
        self.assertEqual(outcome, "paid")

    def test_uses_the_background_client_not_the_interactive_one(self) -> None:
        recover_past_due_invoice(payment_method=_attached())
        self.mock_get_stripe_client.assert_called()
        self.mock_get_interactive_stripe_client.assert_not_called()

    def test_first_checkout_incomplete_subscription_is_a_noop(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(status="incomplete")
        self.assertEqual(recover_past_due_invoice(payment_method=_attached()), "not_past_due")
        self.stripe.v1.invoices.pay.assert_not_called()
        self.stripe.v1.invoices.list.assert_not_called()
        self.assertEqual(self._reload().payment_recovery_state, "")

    def test_active_and_canceled_subscriptions_are_noops(self) -> None:
        for status in ("active", "canceled", "trialing"):
            with self.subTest(status=status):
                self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(status=status)
                self.assertEqual(recover_past_due_invoice(payment_method=_attached()), "not_past_due")
        self.stripe.v1.invoices.pay.assert_not_called()

    def test_no_open_invoice_is_a_noop(self) -> None:
        self.stripe.v1.invoices.list.return_value = _list_object(data=[])
        self.assertEqual(recover_past_due_invoice(payment_method=_attached()), "no_open_invoice")
        self.stripe.v1.invoices.pay.assert_not_called()
        self.assertEqual(self._reload().payment_recovery_state, "")

    def test_zero_amount_invoice_is_skipped(self) -> None:
        self.stripe.v1.invoices.list.return_value = _list_object(data=[_invoice(amount_remaining=0)])
        self.assertEqual(recover_past_due_invoice(payment_method=_attached()), "no_open_invoice")
        self.stripe.v1.invoices.pay.assert_not_called()

    def test_unknown_customer_and_missing_ids_are_noops(self) -> None:
        self.assertEqual(recover_past_due_invoice(payment_method=_attached(customer="cus_nobody")), "unknown_customer")
        self.assertEqual(recover_past_due_invoice(payment_method=_attached(customer=None)), "ignored")
        self.assertEqual(recover_past_due_invoice(payment_method={"customer": _CUSTOMER}), "ignored")
        self.stripe.v1.subscriptions.retrieve.assert_not_called()

    def test_revoked_account_is_never_charged(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(status="revoked")
        self.assertEqual(recover_past_due_invoice(payment_method=_attached()), "not_eligible")
        self.stripe.v1.invoices.pay.assert_not_called()
        self.assertEqual(self._reload().payment_recovery_state, "")

    def test_exempt_accounts_are_noops(self) -> None:
        for flag in ("is_staff", "is_superuser"):
            with self.subTest(flag=flag):
                User.objects.filter(pk=self.user.pk).update(**{flag: True})
                self.assertEqual(recover_past_due_invoice(payment_method=_attached()), "not_eligible")
                User.objects.filter(pk=self.user.pk).update(**{flag: False})
        self.stripe.v1.invoices.pay.assert_not_called()

    def test_subscription_without_a_stripe_subscription_id_is_a_noop(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(stripe_subscription_id=None)
        self.assertEqual(recover_past_due_invoice(payment_method=_attached()), "no_subscription")
        self.stripe.v1.subscriptions.retrieve.assert_not_called()

    def test_declined_card_is_recorded_as_failed_and_returns_normally(self) -> None:
        self.stripe.v1.invoices.pay.side_effect = _card_error()
        with self.assertLogs("fragrance.billing", level="WARNING"):
            outcome = recover_past_due_invoice(payment_method=_attached())
        self.assertEqual(outcome, "declined")
        row = self._reload()
        self.assertEqual(row.payment_recovery_state, "failed")
        self.assertIsNotNone(row.payment_recovery_attempted_at)

    def test_invoice_already_paid_by_stripes_own_retry_counts_as_success(self) -> None:
        self.stripe.v1.invoices.pay.side_effect = stripe.InvalidRequestError("Invoice is already paid", None)
        self.stripe.v1.invoices.retrieve.return_value = _paid()
        self.assertEqual(recover_past_due_invoice(payment_method=_attached()), "paid")
        self.assertEqual(self._reload().payment_recovery_state, "retrying")

    def test_undecidable_rejection_clears_the_recovery_state(self) -> None:
        self.stripe.v1.invoices.pay.side_effect = stripe.InvalidRequestError("payment method detached", None)
        self.stripe.v1.invoices.retrieve.return_value = _invoice()
        with self.assertLogs("fragrance.billing", level="WARNING"):
            outcome = recover_past_due_invoice(payment_method=_attached())
        self.assertEqual(outcome, "rejected")
        self.assertEqual(self._reload().payment_recovery_state, "")

    def test_connection_error_propagates_for_the_task_to_retry(self) -> None:
        self.stripe.v1.invoices.pay.side_effect = stripe.APIConnectionError("down")
        with self.assertRaises(stripe.APIConnectionError):
            recover_past_due_invoice(payment_method=_attached())

    def test_multiple_open_invoices_are_paid_oldest_first(self) -> None:
        self.stripe.v1.invoices.list.return_value = _list_object(
            data=[_invoice(invoice_id="in_new"), _invoice(invoice_id="in_old")]
        )
        recover_past_due_invoice(payment_method=_attached())
        paid = [call.kwargs["invoice"] for call in self.stripe.v1.invoices.pay.call_args_list]
        self.assertEqual(paid, ["in_old", "in_new"])

    def test_replay_after_payment_finds_nothing_to_do(self) -> None:
        recover_past_due_invoice(payment_method=_attached())
        self.stripe.v1.invoices.list.return_value = _list_object(data=[])
        self.stripe.v1.invoices.pay.reset_mock()
        self.assertEqual(recover_past_due_invoice(payment_method=_attached()), "no_open_invoice")
        self.stripe.v1.invoices.pay.assert_not_called()
        self.assertEqual(self._reload().payment_recovery_state, "retrying")


@override_settings(**_STRIPE_TEST_SETTINGS)
class PaymentMethodAttachedTaskTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        user = User.objects.create_user(username="taskrecover", email="taskrecover@example.com")
        self.subscription = Subscription.objects.create(
            user=user,
            stripe_customer_id=_CUSTOMER,
            stripe_subscription_id=_SUBSCRIPTION,
            status="active",
            access_expires_at=timezone.now() - timedelta(days=1),
        )
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(status="past_due")
        self.stripe.v1.invoices.list.return_value = _list_object(data=[_invoice()])
        self.stripe.v1.invoices.pay.return_value = _paid()
        StripeEvent.objects.create(
            stripe_event_id="evt_attached_1",
            event_type="payment_method.attached",
            stripe_created=timezone.now(),
            payload={
                "id": "evt_attached_1",
                "type": "payment_method.attached",
                "data": {"object": _attached()},
            },
        )

    def test_event_type_is_in_the_handled_set(self) -> None:
        self.assertIn("payment_method.attached", STRIPE_HANDLED_EVENT_TYPES)
        self.assertNotIn("payment_method.created", STRIPE_HANDLED_EVENT_TYPES)

    def test_task_pays_and_marks_the_event_processed(self) -> None:
        process_stripe_event(stripe_event_id="evt_attached_1")
        self.stripe.v1.invoices.pay.assert_called_once()
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_attached_1").status, "processed")

    def test_declined_card_marks_the_event_processed_not_failed(self) -> None:
        self.stripe.v1.invoices.pay.side_effect = _card_error()
        with self.assertLogs("fragrance.billing", level="WARNING"):
            process_stripe_event(stripe_event_id="evt_attached_1")
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_attached_1").status, "processed")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.payment_recovery_state, "failed")

    def test_connection_error_marks_failed_and_retries(self) -> None:
        self.stripe.v1.invoices.pay.side_effect = stripe.APIConnectionError("down")
        with patch.object(process_stripe_event, "retry", side_effect=Retry()) as mock_retry:
            with self.assertRaises(Retry):
                process_stripe_event(stripe_event_id="evt_attached_1")
        mock_retry.assert_called_once()
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_attached_1").status, "failed")

    def test_first_checkout_attach_is_a_noop_but_processed(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(status="incomplete")
        process_stripe_event(stripe_event_id="evt_attached_1")
        self.stripe.v1.invoices.pay.assert_not_called()
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_attached_1").status, "processed")


class PaymentRecoveryPayloadTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="payload", email="payload@example.com")
        self.now = timezone.now()
        self.subscription = Subscription.objects.create(
            user=self.user,
            stripe_customer_id="cus_payload",
            status="active",
            access_expires_at=self.now - timedelta(days=1),
            last_paid_at=self.now - timedelta(days=31),
            payment_recovery_state="retrying",
            payment_recovery_attempted_at=self.now - timedelta(minutes=2),
        )

    def _payload(self) -> dict | None:
        self.subscription.refresh_from_db()
        return payment_recovery_payload(subscription=self.subscription)

    def test_retrying_and_failed_are_reported_with_attempted_at(self) -> None:
        self.assertEqual(
            self._payload(),
            {"state": "retrying", "attempted_at": self.subscription.payment_recovery_attempted_at},
        )
        Subscription.objects.filter(pk=self.subscription.pk).update(payment_recovery_state="failed")
        payload = self._payload()
        assert payload is not None
        self.assertEqual(payload["state"], "failed")

    def test_null_when_nothing_was_attempted(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(
            payment_recovery_state="", payment_recovery_attempted_at=None
        )
        self.assertIsNone(self._payload())

    def test_null_once_access_is_restored(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(
            access_expires_at=self.now + timedelta(days=30)
        )
        self.assertIsNone(self._payload())

    def test_null_when_a_payment_was_recorded_after_the_attempt(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(
            payment_recovery_state="failed", last_paid_at=self.now
        )
        self.assertIsNone(self._payload())

    def test_null_for_revoked_and_exempt_accounts(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(status="revoked")
        self.assertIsNone(self._payload())
        Subscription.objects.filter(pk=self.subscription.pk).update(status="active")
        User.objects.filter(pk=self.user.pk).update(is_staff=True)
        self.assertIsNone(self._payload())

    def test_stale_retrying_is_dropped_but_a_stale_failure_is_kept(self) -> None:
        stale = self.now - PAYMENT_RECOVERY_RETRYING_WINDOW - timedelta(minutes=1)
        Subscription.objects.filter(pk=self.subscription.pk).update(payment_recovery_attempted_at=stale)
        self.assertIsNone(self._payload())
        Subscription.objects.filter(pk=self.subscription.pk).update(payment_recovery_state="failed")
        payload = self._payload()
        assert payload is not None
        self.assertEqual(payload["state"], "failed")

    def test_subscription_payload_carries_both_new_keys(self) -> None:
        self.subscription.refresh_from_db()
        body = subscription_status_payload(subscription=self.subscription)
        self.assertIsNone(body["lapse_reason"])
        self.assertEqual(body["payment_recovery"]["state"], "retrying")

    def test_endpoint_is_scoped_to_the_callers_own_subscription(self) -> None:
        other = User.objects.create_user(username="other", email="other@example.com")
        api = APIClient()
        api.force_authenticate(user=other)
        response = api.get("/api/v1/billing/subscription/")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["payment_recovery"])
        api.force_authenticate(user=self.user)
        response = api.get("/api/v1/billing/subscription/")
        self.assertEqual(response.json()["payment_recovery"]["state"], "retrying")
        self.assertIsNone(response.json()["lapse_reason"])
