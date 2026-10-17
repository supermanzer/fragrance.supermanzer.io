"""
DB-backed attempt cap and replace gap, three-phase receipt confirm, key-mode
anomaly handling, the account-email notice copy, the negative cache of the
lazy fill and the config PATCH that must not undo a confirmed address.
"""

from datetime import timedelta
from unittest.mock import patch

import stripe
from django.contrib.auth.models import User
from django.core import mail
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from fragrance import recipient
from fragrance.models import (
    FragranceConfig,
    PendingEmailChange,
    RecipientChangeAttempt,
    Subscription,
)
from fragrance.serializers import FragranceConfigSerializer
from fragrance.tasks import send_recipient_changed_notice
from fragrance.tests.recipient.common import PASSWORD, RecipientTestCase
from fragrance.tests.support.stripe_fakes import _customer
from fragrance.views import FailOpenScopedRateThrottle
from rest_framework.test import APIClient


def _stripe_missing() -> stripe.InvalidRequestError:
    return stripe.InvalidRequestError(
        "No such customer: 'cus_alice'", param="id", code="resource_missing", http_status=404
    )


class DbAttemptCapTests(RecipientTestCase):
    """The cap must hold with the DRF throttle (Redis) out of the picture."""

    def setUp(self) -> None:
        super().setUp()
        throttle = patch.object(FailOpenScopedRateThrottle, "allow_request", return_value=True)
        throttle.start()
        self.addCleanup(throttle.stop)
        self.change_url = reverse("recipient-change")
        self.resend_url = reverse("recipient-resend")
        self.receipt_url = reverse("receipt-email")
        self.receipt_resend_url = reverse("receipt-email-resend")

    def change(self, *, password: str = PASSWORD, email: str = "new@example.com", client=None):
        return (client or self.api).post(
            self.change_url, {"email": email, "current_password": password}, format="json"
        )

    def test_sixth_wrong_password_is_429_before_the_password_is_checked(self) -> None:
        for _ in range(5):
            self.assertEqual(self.change(password="nope").json()["code"], "invalid_password")

        with patch.object(recipient, "password_accepted") as check:
            response = self.change(password=PASSWORD)

        self.assertEqual(response.status_code, 429)
        body = response.json()
        self.assertEqual(body["code"], "rate_limited")
        self.assertTrue(1 <= body["retry_after"] <= 3600)
        self.assertTrue(response.headers.get("Retry-After"))
        check.assert_not_called()
        self.assertIsNone(self.pending(purpose="delivery"))
        self.assertEqual(RecipientChangeAttempt.objects.filter(user=self.user).count(), 5)

    def test_refused_requests_are_not_recorded_so_retrying_never_extends_the_lockout(self) -> None:
        for _ in range(5):
            self.change(password="nope")
        for _ in range(3):
            self.assertEqual(self.change(password="nope").status_code, 429)

        self.assertEqual(RecipientChangeAttempt.objects.filter(user=self.user).count(), 5)

    def test_slot_frees_when_the_oldest_attempt_ages_out(self) -> None:
        for _ in range(5):
            self.change(password="nope")
        RecipientChangeAttempt.objects.filter(user=self.user).update(
            created_at=timezone.now() - timedelta(minutes=59)
        )
        refused = self.change(password="nope")
        self.assertEqual(refused.status_code, 429)
        self.assertLessEqual(refused.json()["retry_after"], 61)

        RecipientChangeAttempt.objects.filter(user=self.user).update(
            created_at=timezone.now() - timedelta(minutes=61)
        )

        self.assertEqual(self.change(password="nope").status_code, 400)
        self.assertEqual(RecipientChangeAttempt.objects.filter(user=self.user).count(), 1)

    def test_cap_is_per_user_and_per_purpose(self) -> None:
        for _ in range(5):
            self.change(password="nope")
        other = self.make_user(username="carol")
        client = APIClient()
        client.force_authenticate(user=other)

        self.assertEqual(self.change(client=client).status_code, 202)
        receipt = self.api.post(
            self.receipt_url, {"email": "bill@example.com", "current_password": PASSWORD}, format="json"
        )
        self.assertEqual(receipt.status_code, 202)

    def test_receipt_change_is_capped_with_the_same_contract(self) -> None:
        for _ in range(5):
            response = self.api.post(
                self.receipt_url, {"email": "bill@example.com", "current_password": "nope"}, format="json"
            )
            self.assertEqual(response.json()["code"], "invalid_password")

        response = self.api.post(
            self.receipt_url, {"email": "bill@example.com", "current_password": PASSWORD}, format="json"
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "rate_limited")
        self.assertIsNone(self.pending(purpose="receipt"))

    def test_resend_is_capped_in_the_db_and_a_gap_refusal_does_not_spend_it(self) -> None:
        self.change()
        too_soon = self.api.post(self.resend_url)
        self.assertEqual(too_soon.json()["code"], "resend_too_soon")
        self.assertEqual(RecipientChangeAttempt.objects.filter(action="resend").count(), 0)

        for _ in range(5):
            self.clear_gap(purpose="delivery")
            self.assertEqual(self.api.post(self.resend_url).status_code, 202)
        self.clear_gap(purpose="delivery")

        response = self.api.post(self.resend_url)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "rate_limited")

    def test_expired_rows_are_pruned_on_write(self) -> None:
        other = self.make_user(username="carol")
        RecipientChangeAttempt.objects.create(
            user=other, purpose="delivery", action="change", created_at=timezone.now() - timedelta(hours=2)
        )

        self.change()

        self.assertFalse(RecipientChangeAttempt.objects.filter(user=other).exists())

    def test_invalid_input_counts_like_any_other_attempt(self) -> None:
        for _ in range(5):
            self.assertEqual(self.change(email="nope").json()["code"], "invalid_email")

        self.assertEqual(self.change(email="nope").status_code, 429)


class ReplaceGapTests(RecipientTestCase):
    def test_start_change_refuses_a_replace_inside_the_gap(self) -> None:
        recipient.start_change(user=self.user, purpose="delivery", address="one@example.com")
        nonce = self.pending(purpose="delivery").nonce

        with self.assertRaises(recipient.ResendTooSoonError) as caught:
            recipient.start_change(user=self.user, purpose="delivery", address="two@example.com")

        self.assertTrue(1 <= caught.exception.retry_after <= 60)
        pending = self.pending(purpose="delivery")
        self.assertEqual((pending.address, pending.nonce), ("one@example.com", nonce))

    def test_gap_is_per_purpose_and_per_user(self) -> None:
        recipient.start_change(user=self.user, purpose="delivery", address="one@example.com")
        other = self.make_user(username="carol")

        recipient.start_change(user=self.user, purpose="receipt", address="r@example.com")
        recipient.start_change(user=other, purpose="delivery", address="c@example.com")

    def test_a_failed_queue_lifts_the_gap(self) -> None:
        pending = recipient.start_change(user=self.user, purpose="delivery", address="one@example.com")
        PendingEmailChange.objects.filter(pk=pending.pk).update(last_sent_at=None)

        recipient.start_change(user=self.user, purpose="delivery", address="two@example.com")

        self.assertEqual(self.pending(purpose="delivery").address, "two@example.com")

    def test_endpoint_answers_429_resend_too_soon_with_retry_after(self) -> None:
        self.api.post(
            reverse("recipient-change"), {"email": "one@example.com", "current_password": PASSWORD}, format="json"
        )

        response = self.api.post(
            reverse("recipient-change"), {"email": "two@example.com", "current_password": PASSWORD}, format="json"
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "resend_too_soon")
        self.assertTrue(1 <= response.json()["retry_after"] <= 60)
        self.assertEqual(self.pending(purpose="delivery").address, "one@example.com")

    def test_receipt_endpoint_answers_429_resend_too_soon(self) -> None:
        body = {"email": "one@example.com", "current_password": PASSWORD}
        self.api.post(reverse("receipt-email"), body, format="json")

        response = self.api.post(reverse("receipt-email"), {**body, "email": "two@example.com"}, format="json")

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "resend_too_soon")


class AccountEmailNoticeTests(RecipientTestCase):
    def confirm(self, *, address: str) -> None:
        recipient.start_change(user=self.user, purpose="delivery", address=address)
        result = recipient.confirm_change(token=self.token_for(purpose="delivery"))
        self.assertEqual(result.outcome, "confirmed")

    def test_account_mailbox_is_told_too_when_the_previous_delivery_address_was_elsewhere(self) -> None:
        FragranceConfig.objects.filter(pk=self.config.pk).update(recipient_email="elsewhere@example.com")

        self.confirm(address="new@example.com")

        calls = [call.kwargs for call in self.mock_notice_delay.call_args_list]
        self.assertEqual(
            calls,
            [
                {
                    "user_id": self.user.pk,
                    "purpose": "delivery",
                    "recipient": "elsewhere@example.com",
                    "new_address": "new@example.com",
                },
                {
                    "user_id": self.user.pk,
                    "purpose": "delivery",
                    "recipient": "alice@example.com",
                    "new_address": "new@example.com",
                    "account_copy": True,
                },
            ],
        )

    def test_one_notice_when_previous_is_the_account_email(self) -> None:
        self.confirm(address="new@example.com")

        self.assertEqual(self.mock_notice_delay.call_count, 1)

    def test_no_account_copy_when_the_new_address_is_the_account_email(self) -> None:
        FragranceConfig.objects.filter(pk=self.config.pk).update(recipient_email="elsewhere@example.com")

        self.confirm(address="alice@example.com")

        self.assertEqual(
            [call.kwargs["recipient"] for call in self.mock_notice_delay.call_args_list],
            ["elsewhere@example.com"],
        )

    def test_account_copy_wording_is_not_instead_of_this_address_and_is_escaped(self) -> None:
        mail.outbox.clear()
        send_recipient_changed_notice.apply(
            kwargs={
                "user_id": self.user.pk,
                "purpose": "delivery",
                "recipient": "alice@example.com",
                "new_address": "a&b<x>@example.com",
                "account_copy": True,
            }
        )

        message = mail.outbox[0]
        self.assertEqual(message.to, ["alice@example.com"])
        self.assertNotIn("instead of this address", message.body)
        self.assertIn("for this account now go to", message.body)
        html = message.alternatives[0].content
        self.assertNotIn("a&b<x>", html)
        self.assertIn("a&amp;b&lt;x&gt;@example.com", html)

    def test_receipt_changes_do_not_copy_the_account_mailbox(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(receipt_email="old-billing@example.com")
        self.stripe.v1.customers.update.return_value = _customer(email="billing@example.com")
        recipient.start_change(user=self.user, purpose="receipt", address="billing@example.com")

        recipient.confirm_change(token=self.token_for(purpose="receipt"))

        self.assertEqual(
            [call.kwargs["recipient"] for call in self.mock_notice_delay.call_args_list],
            ["old-billing@example.com"],
        )


class ThreePhaseReceiptConfirmTests(RecipientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.stripe.v1.customers.update.return_value = _customer(email="billing@example.com")

    def start(self, address: str = "billing@example.com") -> str:
        recipient.start_change(user=self.user, purpose="receipt", address=address)
        return self.token_for(purpose="receipt")

    def test_no_transaction_is_open_around_the_stripe_call(self) -> None:
        token = self.start()
        baseline = len(connection.savepoint_ids)
        seen: list[int] = []

        def _update(*args: object, **kwargs: object):
            seen.append(len(connection.savepoint_ids))
            return _customer(email="billing@example.com")

        self.stripe.v1.customers.update.side_effect = _update

        self.assertEqual(recipient.confirm_change(token=token).outcome, "confirmed")

        self.assertEqual(seen, [baseline])

    def test_row_replaced_during_the_stripe_call_still_updates_the_cache_and_keeps_the_new_row(self) -> None:
        token = self.start("billing@example.com")

        def _replace(*args: object, **kwargs: object):
            self.clear_gap(purpose="receipt")
            recipient.start_change(user=self.user, purpose="receipt", address="other@example.com")
            return _customer(email="billing@example.com")

        self.stripe.v1.customers.update.side_effect = _replace

        result = recipient.confirm_change(token=token)

        self.assertEqual(result.outcome, "confirmed")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.receipt_email, "billing@example.com")
        self.assertEqual(self.pending(purpose="receipt").address, "other@example.com")
        self.assertEqual(self.mock_notice_delay.call_count, 1)

    def test_row_cancelled_during_the_stripe_call_still_updates_the_cache(self) -> None:
        token = self.start()

        def _cancel(*args: object, **kwargs: object):
            recipient.cancel(user=self.user, purpose="receipt")
            return _customer(email="billing@example.com")

        self.stripe.v1.customers.update.side_effect = _cancel

        self.assertEqual(recipient.confirm_change(token=token).outcome, "confirmed")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.receipt_email, "billing@example.com")

    def test_a_concurrent_duplicate_confirm_sends_one_notice(self) -> None:
        token = self.start()
        results: list[str] = []
        entered: list[bool] = []

        def _duplicate(*args: object, **kwargs: object):
            if not entered:
                entered.append(True)
                results.append(recipient.confirm_change(token=token).outcome)
            return _customer(email="billing@example.com")

        self.stripe.v1.customers.update.side_effect = _duplicate

        results.append(recipient.confirm_change(token=token).outcome)

        self.assertEqual(results.count("confirmed"), 1)
        self.assertEqual(self.mock_notice_delay.call_count, 1)

    def test_a_failed_attempt_rotates_the_idempotency_key(self) -> None:
        token = self.start()
        nonce = self.pending(purpose="receipt").nonce
        self.stripe.v1.customers.update.side_effect = [
            stripe.APIError("boom", http_status=500),
            stripe.APIConnectionError("down"),
            _customer(email="billing@example.com"),
        ]

        with self.assertLogs("fragrance.recipient", level="ERROR"):
            self.assertEqual(recipient.confirm_change(token=token).outcome, "stripe_unavailable")
            self.assertEqual(recipient.confirm_change(token=token).outcome, "stripe_unavailable")
        self.assertEqual(recipient.confirm_change(token=token).outcome, "confirmed")

        keys = [c.kwargs["options"]["idempotency_key"] for c in self.stripe.v1.customers.update.call_args_list]
        self.assertEqual(keys, [f"receipt-email-{nonce}-{i}" for i in range(3)])

    def test_a_new_request_restarts_the_attempt_counter(self) -> None:
        token = self.start("one@example.com")
        self.stripe.v1.customers.update.side_effect = stripe.APIError("boom", http_status=500)
        with self.assertLogs("fragrance.recipient", level="ERROR"):
            recipient.confirm_change(token=token)
        self.assertEqual(self.pending(purpose="receipt").confirm_attempts, 1)

        self.clear_gap(purpose="receipt")
        recipient.start_change(user=self.user, purpose="receipt", address="two@example.com")

        self.assertEqual(self.pending(purpose="receipt").confirm_attempts, 0)

    def test_inactive_account_gets_invalid_and_stripe_is_not_called(self) -> None:
        token = self.start()
        User.objects.filter(pk=self.user.pk).update(is_active=False)

        self.assertEqual(recipient.confirm_change(token=token).outcome, "invalid")
        self.stripe.v1.customers.update.assert_not_called()
        self.assertIsNotNone(self.pending(purpose="receipt"))

    def test_inactive_account_cannot_confirm_a_delivery_change_either(self) -> None:
        recipient.start_change(user=self.user, purpose="delivery", address="new@example.com")
        token = self.token_for(purpose="delivery")
        User.objects.filter(pk=self.user.pk).update(is_active=False)

        self.assertEqual(recipient.confirm_change(token=token).outcome, "invalid")
        self.config.refresh_from_db()
        self.assertEqual(self.config.recipient_email, "alice@example.com")


class KeyModeAnomalyConfirmTests(RecipientTestCase):
    def setUp(self) -> None:
        super().setUp()
        recipient.start_change(user=self.user, purpose="receipt", address="billing@example.com")
        self.token = self.token_for(purpose="receipt")
        self.stripe.v1.customers.update.side_effect = _stripe_missing()

    def test_unknown_customer_is_critical_keeps_the_row_and_asks_to_retry(self) -> None:
        self.stripe.v1.customers.retrieve.side_effect = _stripe_missing()

        with self.assertLogs("fragrance.billing", level="CRITICAL") as logs:
            result = recipient.confirm_change(token=self.token)

        self.assertEqual(result.outcome, "stripe_unavailable")
        self.assertIsNotNone(self.pending(purpose="receipt"))
        message = "\n".join(logs.output)
        self.assertIn("key mismatch", message)
        self.assertIn("cus_alice", message)
        self.assertNotIn("billing@example.com", message)
        self.assertNotIn("alice@example.com", message)

    def test_deleted_customer_known_to_the_account_clears_the_row(self) -> None:
        self.stripe.v1.customers.retrieve.return_value = stripe.Customer.construct_from(
            {"id": "cus_alice", "object": "customer", "deleted": True}, "sk_test_dummy"
        )

        with self.assertLogs("fragrance.recipient", level="ERROR"):
            result = recipient.confirm_change(token=self.token)

        self.assertEqual(result.outcome, "superseded")
        self.assertIsNone(self.pending(purpose="receipt"))

    def test_probe_failure_keeps_the_row(self) -> None:
        self.stripe.v1.customers.retrieve.side_effect = stripe.APIConnectionError("down")

        with self.assertLogs("fragrance.recipient", level="ERROR"):
            result = recipient.confirm_change(token=self.token)

        self.assertEqual(result.outcome, "stripe_unavailable")
        self.assertIsNotNone(self.pending(purpose="receipt"))


class LazyFillNegativeCacheTests(RecipientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("receipt-email")
        Subscription.objects.filter(pk=self.subscription.pk).update(receipt_email="")

    def test_blank_customer_is_not_asked_again_inside_the_interval(self) -> None:
        self.stripe.v1.customers.retrieve.return_value = _customer(email="")

        for _ in range(4):
            self.assertEqual(self.api.get(self.url).status_code, 200)

        self.stripe.v1.customers.retrieve.assert_called_once()

    def test_failures_are_negative_cached_too_and_expire(self) -> None:
        self.stripe.v1.customers.retrieve.side_effect = stripe.APIConnectionError("down")

        with self.assertLogs("fragrance.recipient", level="WARNING"):
            self.api.get(self.url)
            self.api.get(self.url)
        self.assertEqual(self.stripe.v1.customers.retrieve.call_count, 1)

        Subscription.objects.filter(pk=self.subscription.pk).update(
            receipt_email_checked_at=timezone.now() - recipient.RECEIPT_EMAIL_RECHECK_INTERVAL - timedelta(seconds=5)
        )
        self.stripe.v1.customers.retrieve.side_effect = None
        self.stripe.v1.customers.retrieve.return_value = _customer(email="back@example.com")

        body = self.api.get(self.url).json()

        self.assertEqual(self.stripe.v1.customers.retrieve.call_count, 2)
        self.assertEqual(body["receipt_email"], "back@example.com")


class ConfigPatchKeepsConfirmedAddressTests(RecipientTestCase):
    def test_stale_instance_save_does_not_restore_the_old_address(self) -> None:
        stale = FragranceConfig.objects.get(pk=self.config.pk)
        FragranceConfig.objects.filter(pk=self.config.pk).update(recipient_email="confirmed@example.com")

        serializer = FragranceConfigSerializer(stale, data={"run_hour": 4}, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        saved = serializer.save()

        self.config.refresh_from_db()
        self.assertEqual(self.config.recipient_email, "confirmed@example.com")
        self.assertEqual(self.config.run_hour, 4)
        self.assertEqual(saved.recipient_email, "confirmed@example.com")

    def test_patch_schedule_still_syncs_the_periodic_task(self) -> None:
        from django_celery_beat.models import PeriodicTask

        response = self.api.patch(reverse("fragrance-config"), {"run_hour": 6}, format="json")

        self.assertEqual(response.status_code, 200)
        tasks = PeriodicTask.objects.filter(name__contains=str(self.user.pk))
        self.assertEqual([task.crontab.hour for task in tasks], ["6"])
