"""GET/POST /billing/receipt-email/, .../resend/, DELETE .../pending/ and the lazy Stripe cache."""

from unittest.mock import patch

import stripe
from django.core.cache import cache
from django.urls import reverse
from fragrance import recipient
from fragrance.models import PendingEmailChange, Subscription
from fragrance.tests.recipient.common import PASSWORD, RecipientTestCase
from fragrance.tests.support.stripe_fakes import _customer
from rest_framework.test import APIClient


class _ReceiptBase(RecipientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("receipt-email")
        self.resend_url = reverse("receipt-email-resend")
        self.pending_url = reverse("receipt-email-pending")

    def request_change(self, email: object = "billing@example.com", *, password: object = PASSWORD, client=None):
        body: dict = {"email": email}
        if password is not None:
            body["current_password"] = password
        return (client or self.api).post(self.url, body, format="json")

    def set_cache(self, value: str) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(receipt_email=value)


class ReceiptReadTests(_ReceiptBase):
    def test_resting_state(self) -> None:
        response = self.api.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "has_customer": True,
                "receipt_email": "alice@example.com",
                "pending_receipt_email": None,
                "pending_state": "none",
                "pending_expires_at": None,
                "resend_available_at": None,
            },
        )
        self.stripe.v1.customers.retrieve.assert_not_called()

    def test_unauthenticated_is_401(self) -> None:
        for call in (
            lambda c: c.get(self.url),
            lambda c: c.post(self.url, {}, format="json"),
            lambda c: c.post(self.resend_url),
            lambda c: c.delete(self.pending_url),
        ):
            self.assertEqual(call(APIClient()).status_code, 401)

    def test_no_customer_is_200_with_everything_empty(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(stripe_customer_id=None, receipt_email="")

        body = self.api.get(self.url).json()

        self.assertEqual(body["has_customer"], False)
        self.assertIsNone(body["receipt_email"])
        self.assertEqual(body["pending_state"], "none")
        self.stripe.v1.customers.retrieve.assert_not_called()

    def test_blank_cache_is_filled_once_from_stripe(self) -> None:
        self.set_cache("")
        self.stripe.v1.customers.retrieve.return_value = _customer(email="Stripe@Example.com")

        first = self.api.get(self.url).json()
        second = self.api.get(self.url).json()

        self.assertEqual(first["receipt_email"], "Stripe@Example.com")
        self.assertEqual(second["receipt_email"], "Stripe@Example.com")
        self.stripe.v1.customers.retrieve.assert_called_once()
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.receipt_email, "Stripe@Example.com")

    def test_stripe_failure_on_the_lazy_read_is_200_with_null(self) -> None:
        self.set_cache("")
        self.stripe.v1.customers.retrieve.side_effect = stripe.APIConnectionError("down")

        with self.assertLogs("fragrance.recipient", level="WARNING"):
            response = self.api.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["receipt_email"])
        self.assertTrue(response.json()["has_customer"])

    def test_deleted_customer_is_unknown_and_not_cached(self) -> None:
        self.set_cache("")
        deleted = stripe.Customer.construct_from(
            {"id": "cus_alice", "object": "customer", "deleted": True}, "sk_test_dummy"
        )
        self.stripe.v1.customers.retrieve.return_value = deleted

        self.assertIsNone(self.api.get(self.url).json()["receipt_email"])

    def test_lazy_fill_never_overwrites_a_value_that_landed_meanwhile(self) -> None:
        self.set_cache("")

        def _retrieve(*args: object, **kwargs: object):
            self.set_cache("confirmed@example.com")
            return _customer(email="stale@example.com")

        self.stripe.v1.customers.retrieve.side_effect = _retrieve

        body = self.api.get(self.url).json()

        self.assertEqual(body["receipt_email"], "confirmed@example.com")

    def test_lazy_fill_switch_off_never_calls_stripe(self) -> None:
        self.set_cache("")
        with patch.object(recipient, "RECEIPT_EMAIL_LAZY_FILL", False):
            body = self.api.get(self.url).json()

        self.assertIsNone(body["receipt_email"])
        self.stripe.v1.customers.retrieve.assert_not_called()

    def test_exempt_account_is_409_exempt(self) -> None:
        staff = self.make_user(username="ops", staff=True)
        client = APIClient()
        client.force_authenticate(user=staff)

        for response in (
            client.get(self.url),
            self.request_change(client=client),
            client.post(self.resend_url),
        ):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["code"], "exempt")

    def test_revoked_account_is_409_account_locked_with_the_support_sentence(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(status="revoked")

        for response in (self.api.get(self.url), self.request_change(), self.api.post(self.resend_url)):
            self.assertEqual(response.status_code, 409)
            body = response.json()
            self.assertEqual(body["code"], "account_locked")
            self.assertIn("help@example.test", body["detail"])
        self.assertIsNone(self.pending(purpose="receipt"))

    def test_reads_do_not_spend_the_change_budget(self) -> None:
        for _ in range(8):
            self.assertEqual(self.api.get(self.url).status_code, 200)

        self.assertEqual(self.request_change().status_code, 202)

    def test_pending_block_is_scoped_to_the_caller(self) -> None:
        other = self.make_user(username="carol")
        recipient.start_change(user=other, purpose="receipt", address="secret@example.com")

        body = self.api.get(self.url).json()

        self.assertIsNone(body["pending_receipt_email"])


class ReceiptRequestTests(_ReceiptBase):
    def test_valid_request_is_202_in_the_get_shape_and_nothing_changes_yet(self) -> None:
        response = self.request_change("Billing@Example.com")

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["receipt_email"], "alice@example.com")
        self.assertEqual(body["pending_receipt_email"], "billing@example.com")
        self.assertEqual(body["pending_state"], "pending")
        self.assertTrue(body["has_customer"])
        pending = self.pending(purpose="receipt")
        assert pending is not None
        self.mock_confirm_delay.assert_called_once_with(
            user_id=self.user.pk, purpose="receipt", nonce=pending.nonce
        )
        self.stripe.v1.customers.update.assert_not_called()
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.receipt_email, "alice@example.com")

    def test_no_customer_is_409_no_stripe_customer(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(stripe_customer_id=None)

        for response in (self.request_change(), self.api.post(self.resend_url)):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["code"], "no_stripe_customer")
        self.assertIsNone(self.pending(purpose="receipt"))

    def test_password_is_checked_before_anything_is_stored(self) -> None:
        for password in ("wrong-password", None):
            cache.clear()
            response = self.request_change(password=password)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["code"], "invalid_password")
        self.assertIsNone(self.pending(purpose="receipt"))
        self.mock_confirm_delay.assert_not_called()

    def test_invalid_email(self) -> None:
        response = self.request_change("not-an-address")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_email")

    def test_same_as_current_compares_with_the_receipt_address_not_the_account_email(self) -> None:
        self.set_cache("old-billing@example.com")

        same = self.request_change("OLD-BILLING@example.com")
        account = self.request_change("alice@example.com")

        self.assertEqual(same.status_code, 400)
        self.assertEqual(same.json()["code"], "same_as_current")
        self.assertEqual(account.status_code, 202)

    def test_blank_cache_is_filled_before_the_comparison(self) -> None:
        self.set_cache("")
        self.stripe.v1.customers.retrieve.return_value = _customer(email="billing@example.com")

        response = self.request_change("billing@example.com")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "same_as_current")

    def test_lapsed_user_is_not_gated(self) -> None:
        self.lapse()

        self.assertEqual(self.request_change().status_code, 202)
        self.assertEqual(self.api.get(self.url).status_code, 200)

    def test_sixth_change_is_429_rate_limited(self) -> None:
        for i in range(5):
            self.clear_gap(purpose="receipt")
            self.assertEqual(self.request_change(f"b{i}@example.com").status_code, 202)
        self.clear_gap(purpose="receipt")

        response = self.request_change("b6@example.com")

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "rate_limited")
        self.assertIsInstance(response.json()["retry_after"], int)

    def test_wrong_passwords_spend_the_budget(self) -> None:
        for _ in range(5):
            self.assertEqual(self.request_change(password="nope").status_code, 400)

        self.assertEqual(self.request_change().status_code, 429)

    def test_delivery_and_receipt_changes_are_independent(self) -> None:
        self.request_change("billing@example.com")
        recipient.start_change(user=self.user, purpose="delivery", address="picks@example.com")

        self.assertEqual(PendingEmailChange.objects.filter(user=self.user).count(), 2)
        self.assertEqual(self.pending(purpose="receipt").address, "billing@example.com")
        self.assertEqual(self.pending(purpose="delivery").address, "picks@example.com")

    def test_a_delivery_token_cannot_confirm_the_receipt_change(self) -> None:
        self.request_change("same@example.com")
        recipient.start_change(user=self.user, purpose="delivery", address="same@example.com")
        delivery_token = self.token_for(purpose="delivery")

        result = recipient.confirm_change(token=delivery_token)

        self.assertEqual(result.purpose, "delivery")
        self.stripe.v1.customers.update.assert_not_called()
        self.assertIsNotNone(self.pending(purpose="receipt"))


class ReceiptResendAndCancelTests(_ReceiptBase):
    def test_resend_without_pending_is_409(self) -> None:
        response = self.api.post(self.resend_url)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "no_pending")

    def test_resend_gap_then_success_keeps_the_nonce(self) -> None:
        self.request_change()
        nonce = self.pending(purpose="receipt").nonce

        too_soon = self.api.post(self.resend_url)
        self.age_pending(purpose="receipt", hours=1)
        again = self.api.post(self.resend_url)

        self.assertEqual(too_soon.status_code, 429)
        self.assertEqual(too_soon.json()["code"], "resend_too_soon")
        self.assertEqual(again.status_code, 202)
        self.assertEqual(self.pending(purpose="receipt").nonce, nonce)

    def test_hourly_resend_cap(self) -> None:
        self.request_change()
        for _ in range(5):
            self.age_pending(purpose="receipt", hours=1)
            self.assertEqual(self.api.post(self.resend_url).status_code, 202)
        self.age_pending(purpose="receipt", hours=1)

        response = self.api.post(self.resend_url)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "rate_limited")

    def test_cancel_is_idempotent_and_not_gated_even_when_locked_or_exempt(self) -> None:
        self.request_change()
        Subscription.objects.filter(pk=self.subscription.pk).update(status="revoked")

        self.assertEqual(self.api.delete(self.pending_url).status_code, 204)
        self.assertEqual(self.api.delete(self.pending_url).status_code, 204)
        self.assertIsNone(self.pending(purpose="receipt"))

    def test_cancel_does_not_touch_another_users_change(self) -> None:
        other = self.make_user(username="carol")
        recipient.start_change(user=other, purpose="receipt", address="theirs@example.com")

        self.api.delete(self.pending_url)

        self.assertIsNotNone(self.pending(purpose="receipt", user=other))


class CustomerCreationTests(RecipientTestCase):
    def test_creating_the_customer_records_the_account_email_as_the_receipt_address(self) -> None:
        from fragrance import billing

        sub = Subscription.objects.get(pk=self.subscription.pk)
        Subscription.objects.filter(pk=sub.pk).update(stripe_customer_id=None, receipt_email="")
        sub.refresh_from_db()
        self.stripe.v1.customers.create.return_value = _customer(email=self.user.email)

        billing._get_or_create_customer(user=self.user, subscription=sub, client=self.stripe)

        sub.refresh_from_db()
        self.assertEqual(sub.stripe_customer_id, "cus_alice")
        self.assertEqual(sub.receipt_email, "alice@example.com")
