"""POST /auth/confirm-recipient/: token handling, check order and both purposes' side effects."""

from datetime import timedelta
from unittest.mock import patch

import stripe
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core import signing
from django.urls import reverse
from django.utils import timezone
from fragrance import recipient
from fragrance.models import FragranceConfig, PendingEmailChange, Subscription
from fragrance.tasks import _delivery_address
from fragrance.tests.recipient.common import RecipientTestCase
from fragrance.tests.support.stripe_fakes import _customer
from rest_framework.test import APIClient


class _ConfirmBase(RecipientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("confirm_recipient")
        self.anon = APIClient()

    def confirm(self, token: object, *, client=None):
        return (client or self.anon).post(self.url, {"token": token}, format="json")

    def start(self, purpose: str, address: str = "new@example.com") -> str:
        recipient.start_change(user=self.user, purpose=purpose, address=address)
        return self.token_for(purpose=purpose)


class DeliveryConfirmTests(_ConfirmBase):
    def test_confirms_signed_out_and_sets_the_verified_address(self) -> None:
        token = self.start("delivery")

        response = self.confirm(token)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "confirmed", "purpose": "delivery", "recipient_email": "new@example.com"},
        )
        self.config.refresh_from_db()
        self.assertEqual(self.config.recipient_email, "new@example.com")
        self.assertEqual(_delivery_address(user_id=self.user.pk), "new@example.com")
        self.assertIsNone(self.pending(purpose="delivery"))

    def test_sends_the_notice_to_the_previous_address_only(self) -> None:
        token = self.start("delivery")

        self.confirm(token)

        self.mock_notice_delay.assert_called_once_with(
            user_id=self.user.pk,
            purpose="delivery",
            recipient="alice@example.com",
            new_address="new@example.com",
        )

    def test_blank_previous_recipient_notifies_the_account_email(self) -> None:
        FragranceConfig.objects.filter(pk=self.config.pk).update(recipient_email="")
        token = self.start("delivery")

        self.confirm(token)

        self.assertEqual(self.mock_notice_delay.call_args.kwargs["recipient"], "alice@example.com")

    def test_second_open_is_already_confirmed_not_an_error(self) -> None:
        token = self.start("delivery")
        self.confirm(token)
        self.mock_notice_delay.reset_mock()

        response = self.confirm(token)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "already_confirmed")
        self.assertEqual(response.json()["recipient_email"], "new@example.com")
        self.mock_notice_delay.assert_not_called()

    def test_already_confirmed_wins_over_age(self) -> None:
        token = self.start("delivery")
        self.confirm(token)

        with patch("fragrance.recipient.timezone.now", return_value=timezone.now() + timedelta(days=30)):
            response = self.confirm(token)

        self.assertEqual(response.json()["status"], "already_confirmed")

    def test_signed_in_other_user_still_confirms_the_token_owners_change(self) -> None:
        token = self.start("delivery")
        other = self.make_user(username="carol")
        client = APIClient()
        client.force_authenticate(user=other)

        response = self.confirm(token, client=client)

        self.assertEqual(response.json()["status"], "confirmed")
        self.config.refresh_from_db()
        self.assertEqual(self.config.recipient_email, "new@example.com")
        other_config = FragranceConfig.objects.get(user=other)
        self.assertEqual(other_config.recipient_email, "carol@example.com")

    def test_superseded_after_replace_and_after_cancel(self) -> None:
        first = self.start("delivery", "one@example.com")
        self.clear_gap(purpose="delivery")
        recipient.start_change(user=self.user, purpose="delivery", address="two@example.com")
        response = self.confirm(first)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "superseded")
        self.assertEqual(response.json()["purpose"], "delivery")

        second = self.token_for(purpose="delivery")
        recipient.cancel(user=self.user, purpose="delivery")
        self.assertEqual(self.confirm(second).json()["code"], "superseded")
        self.config.refresh_from_db()
        self.assertEqual(self.config.recipient_email, "alice@example.com")

    def test_expired_by_token_age(self) -> None:
        token = self.start("delivery")

        with patch(
            "fragrance.recipient.timezone.now",
            return_value=timezone.now() + timedelta(hours=24, minutes=1),
        ):
            response = self.confirm(token)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "expired")
        self.assertEqual(response.json()["purpose"], "delivery")
        self.assertIsNotNone(self.pending(purpose="delivery"))

    def test_expired_by_row_expiry(self) -> None:
        token = self.start("delivery")
        self.age_pending(purpose="delivery")

        self.assertEqual(self.confirm(token).json()["code"], "expired")

    def test_resend_revives_an_expired_row_with_the_same_link_family(self) -> None:
        token = self.start("delivery")
        self.age_pending(purpose="delivery")
        recipient.resend(user=self.user, purpose="delivery")

        # A fresh signature from the same nonce works; the original token is still young enough here.
        fresh = self.token_for(purpose="delivery")
        self.assertEqual(self.confirm(fresh).json()["status"], "confirmed")
        self.assertEqual(self.confirm(token).json()["status"], "already_confirmed")

    def test_check_order_superseded_beats_expired(self) -> None:
        old = self.start("delivery", "one@example.com")
        self.clear_gap(purpose="delivery")
        recipient.start_change(user=self.user, purpose="delivery", address="two@example.com")

        with patch(
            "fragrance.recipient.timezone.now", return_value=timezone.now() + timedelta(days=3)
        ):
            response = self.confirm(old)

        self.assertEqual(response.json()["code"], "superseded")

    def test_invalid_tokens(self) -> None:
        good = self.start("delivery")
        other_salt = signing.dumps({"uid": self.user.pk}, salt="email-revert")
        wrong_purpose = signing.dumps(
            {"uid": self.user.pk, "purpose": "receipt", "addr": "x@example.com", "nonce": "n", "iat": 1},
            salt="recipient-confirm-delivery",
        )
        unknown_user = signing.dumps(
            {"uid": 999999, "purpose": "delivery", "addr": "x@example.com", "nonce": "n", "iat": 1},
            salt="recipient-confirm-delivery",
        )
        for token in (None, "", 5, ["x"], "garbage", good + "x", good[:-3], other_salt, wrong_purpose, unknown_user, "a" * 5000):
            with self.subTest(token=str(token)[:20]):
                cache.clear()
                response = self.confirm(token)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "invalid")
                self.assertNotIn("purpose", response.json())
        self.assertEqual(self.pending(purpose="delivery").address, "new@example.com")

    def test_missing_body_is_invalid(self) -> None:
        response = self.anon.post(self.url, "not json", content_type="application/json")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid")

    def test_get_is_405(self) -> None:
        self.assertEqual(self.anon.get(self.url).status_code, 405)

    def test_unknown_user_and_bad_signature_are_indistinguishable(self) -> None:
        unknown_user = signing.dumps(
            {"uid": 999999, "purpose": "delivery", "addr": "x@example.com", "nonce": "n", "iat": 1},
            salt="recipient-confirm-delivery",
        )

        a = self.confirm(unknown_user)
        b = self.confirm("tampered")

        self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))

    def test_anonymous_endpoint_is_ip_throttled(self) -> None:
        statuses = [self.confirm("garbage").status_code for _ in range(11)]

        self.assertEqual(statuses[:10], [400] * 10)
        self.assertEqual(statuses[10], 429)

    def test_never_touches_the_login_email_or_password(self) -> None:
        before = User.objects.get(pk=self.user.pk)
        self.confirm(self.start("delivery"))
        after = User.objects.get(pk=self.user.pk)
        self.assertEqual((after.email, after.password), (before.email, before.password))


class ReceiptConfirmTests(_ConfirmBase):
    def setUp(self) -> None:
        super().setUp()
        self.stripe.v1.customers.update.return_value = _customer(email="billing@example.com")

    def test_confirms_updates_stripe_with_the_idempotency_key_then_the_cache(self) -> None:
        token = self.start("receipt", "billing@example.com")
        nonce = self.pending(purpose="receipt").nonce

        response = self.confirm(token)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "confirmed", "purpose": "receipt", "receipt_email": "billing@example.com"},
        )
        self.stripe.v1.customers.update.assert_called_once_with(
            "cus_alice",
            params={"email": "billing@example.com"},
            options={"idempotency_key": f"receipt-email-{nonce}-0"},
        )
        self.mock_get_interactive_stripe_client.assert_called()
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.receipt_email, "billing@example.com")
        self.assertIsNone(self.pending(purpose="receipt"))
        self.mock_notice_delay.assert_called_once_with(
            user_id=self.user.pk,
            purpose="receipt",
            recipient="alice@example.com",
            new_address="billing@example.com",
        )

    def test_never_touches_user_email_password_or_delivery_address(self) -> None:
        before = User.objects.get(pk=self.user.pk)
        self.confirm(self.start("receipt", "billing@example.com"))
        after = User.objects.get(pk=self.user.pk)
        self.assertEqual((after.email, after.password), (before.email, before.password))
        self.config.refresh_from_db()
        self.assertEqual(self.config.recipient_email, "alice@example.com")

    def test_no_notice_when_the_previous_address_is_unknown(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(receipt_email="")
        token = self.start("receipt", "billing@example.com")

        response = self.confirm(token)

        self.assertEqual(response.json()["status"], "confirmed")
        self.mock_notice_delay.assert_not_called()

    def test_stripe_outage_is_502_and_keeps_the_row_and_nonce(self) -> None:
        token = self.start("receipt", "billing@example.com")
        nonce = self.pending(purpose="receipt").nonce
        self.stripe.v1.customers.update.side_effect = stripe.APIConnectionError("down")

        with self.assertLogs("fragrance.recipient", level="ERROR"):
            response = self.confirm(token)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["code"], "stripe_unavailable")
        self.assertEqual(response.json()["purpose"], "receipt")
        self.assertEqual(self.pending(purpose="receipt").nonce, nonce)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.receipt_email, "alice@example.com")
        self.mock_notice_delay.assert_not_called()

        self.stripe.v1.customers.update.side_effect = None
        retry = self.confirm(token)
        self.assertEqual(retry.json()["status"], "confirmed")
        keys = [c.kwargs["options"]["idempotency_key"] for c in self.stripe.v1.customers.update.call_args_list]
        # Stripe replays a failed first attempt for a reused key, so the retry gets its own.
        self.assertEqual(keys, [f"receipt-email-{nonce}-0", f"receipt-email-{nonce}-1"])

    def test_stripe_transient_4xx_classes_keep_the_row(self) -> None:
        token = self.start("receipt", "billing@example.com")
        for error in (
            stripe.RateLimitError("slow down", http_status=429),
            stripe.AuthenticationError("bad key", http_status=401),
            stripe.PermissionError("no", http_status=403),
        ):
            with self.subTest(error=type(error).__name__):
                self.stripe.v1.customers.update.side_effect = error
                with self.assertLogs("fragrance.recipient", level="ERROR"):
                    response = self.confirm(token)
                self.assertEqual(response.status_code, 502)
                self.assertIsNotNone(self.pending(purpose="receipt"))

    def test_stripe_rejection_clears_the_row_and_reports_superseded(self) -> None:
        token = self.start("receipt", "billing@example.com")
        self.stripe.v1.customers.update.side_effect = stripe.InvalidRequestError(
            "Invalid email", param="email", http_status=400
        )

        with self.assertLogs("fragrance.recipient", level="ERROR") as logs:
            response = self.confirm(token)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "superseded")
        self.assertEqual(response.json()["purpose"], "receipt")
        self.assertIsNone(self.pending(purpose="receipt"))
        self.assertNotIn("billing@example.com", "\n".join(logs.output))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.receipt_email, "alice@example.com")

    def test_missing_customer_is_superseded(self) -> None:
        token = self.start("receipt", "billing@example.com")
        Subscription.objects.filter(pk=self.subscription.pk).update(stripe_customer_id=None)

        response = self.confirm(token)

        self.assertEqual(response.json()["code"], "superseded")
        self.assertIsNone(self.pending(purpose="receipt"))
        self.stripe.v1.customers.update.assert_not_called()

    def test_already_confirmed_compares_with_the_receipt_cache(self) -> None:
        token = self.start("receipt", "billing@example.com")
        self.confirm(token)
        self.stripe.v1.customers.update.reset_mock()

        response = self.confirm(token)

        self.assertEqual(response.json()["status"], "already_confirmed")
        self.assertEqual(response.json()["receipt_email"], "billing@example.com")
        self.stripe.v1.customers.update.assert_not_called()

    def test_locked_or_exempt_users_still_resolve_the_link(self) -> None:
        token = self.start("receipt", "billing@example.com")
        Subscription.objects.filter(pk=self.subscription.pk).update(status="revoked")
        User.objects.filter(pk=self.user.pk).update(is_staff=True)

        self.assertEqual(self.confirm(token).json()["status"], "confirmed")

    def test_expired_receipt_link_reports_receipt_purpose(self) -> None:
        token = self.start("receipt", "billing@example.com")
        self.age_pending(purpose="receipt")

        response = self.confirm(token)

        self.assertEqual(response.json(), {
            "code": "expired", "detail": response.json()["detail"], "purpose": "receipt",
        })
        self.stripe.v1.customers.update.assert_not_called()
