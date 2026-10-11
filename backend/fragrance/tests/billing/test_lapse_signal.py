"""
Row 13: Subscription.lapse_reason, recorded from customer.subscription.updated
/ .deleted by billing.record_subscription_lapse_signal and surfaced as the
`lapse_reason` of the subscription payload. Display-only: none of these tests
may see access or status change.
"""

from datetime import timedelta
from unittest.mock import patch

import stripe
from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone
from fragrance.billing import (
    lapse_reason_payload,
    record_subscription_lapse_signal,
    subscription_status_payload,
)
from fragrance.models import StripeEvent, Subscription
from fragrance.tasks import STRIPE_HANDLED_EVENT_TYPES, process_stripe_event
from fragrance.tests.support.bases import StripeClientTestCase
from fragrance.tests.support.stripe_fakes import _KEY, _STRIPE_TEST_SETTINGS

_CUSTOMER = "cus_lapse_1"
_SUBSCRIPTION = "sub_lapse_1"


def _stripe_sub(
    *,
    status: str,
    sub_id: str = _SUBSCRIPTION,
    cancel_at_period_end: bool = False,
    cancel_reason: str | None = None,
) -> stripe.Subscription:
    data: dict = {
        "id": sub_id,
        "object": "subscription",
        "status": status,
        "customer": _CUSTOMER,
        "cancel_at_period_end": cancel_at_period_end,
        "cancel_at": None,
    }
    if cancel_reason is not None:
        data["cancellation_details"] = {"reason": cancel_reason}
    return stripe.Subscription.construct_from(data, _KEY)


def _event_object(*, sub_id: str = _SUBSCRIPTION) -> dict:
    return {"id": sub_id, "object": "subscription", "customer": _CUSTOMER}


@override_settings(**_STRIPE_TEST_SETTINGS)
class LapseSignalHandlerTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="lapser", email="lapser@example.com")
        self.subscription = Subscription.objects.create(
            user=self.user,
            stripe_customer_id=_CUSTOMER,
            stripe_subscription_id=_SUBSCRIPTION,
            status="active",
            access_expires_at=timezone.now() - timedelta(days=3),
        )

    def _record(self, *, current: stripe.Subscription, sub_id: str = _SUBSCRIPTION) -> str:
        self.stripe.v1.subscriptions.retrieve.return_value = current
        return record_subscription_lapse_signal(stripe_subscription=_event_object(sub_id=sub_id))

    def _reload(self) -> Subscription:
        self.subscription.refresh_from_db()
        return self.subscription

    def test_event_is_ignored_when_the_retrieved_subscription_belongs_to_another_customer(self) -> None:
        foreign = stripe.Subscription.construct_from(
            {
                "id": _SUBSCRIPTION,
                "object": "subscription",
                "status": "past_due",
                "customer": "cus_someone_else",
                "cancel_at_period_end": False,
                "cancel_at": None,
            },
            _KEY,
        )
        with self.assertLogs("fragrance.billing", level="WARNING") as logs:
            outcome = self._record(current=foreign)
        after = self._reload()
        self.assertEqual(outcome, "ignored")
        self.assertEqual(after.lapse_reason, "")
        self.assertIsNone(after.lapse_signal_at)
        self.assertNotIn("lapser@example.com", " ".join(logs.output))

    def test_expanded_customer_object_matching_the_row_is_accepted(self) -> None:
        expanded = stripe.Subscription.construct_from(
            {
                "id": _SUBSCRIPTION,
                "object": "subscription",
                "status": "past_due",
                "customer": {"id": _CUSTOMER, "object": "customer"},
                "cancel_at_period_end": False,
                "cancel_at": None,
            },
            _KEY,
        )
        self.assertEqual(self._record(current=expanded), "payment_failed")

    def test_past_due_is_recorded_as_payment_failed_and_never_touches_access(self) -> None:
        before = self._reload()
        outcome = self._record(current=_stripe_sub(status="past_due"))
        after = self._reload()
        self.assertEqual(outcome, "payment_failed")
        self.assertEqual(after.lapse_reason, "payment_failed")
        self.assertIsNotNone(after.lapse_signal_at)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.access_expires_at, before.access_expires_at)
        self.assertEqual(after.last_paid_at, before.last_paid_at)

    def test_unpaid_is_payment_failed(self) -> None:
        self._record(current=_stripe_sub(status="unpaid"))
        self.assertEqual(self._reload().lapse_reason, "payment_failed")

    def test_user_cancellation_is_cancelled(self) -> None:
        self._record(current=_stripe_sub(status="canceled", cancel_reason="cancellation_requested"))
        self.assertEqual(self._reload().lapse_reason, "cancelled")

    def test_scheduled_cancellation_while_still_active_is_cancelled(self) -> None:
        self._record(current=_stripe_sub(status="active", cancel_at_period_end=True))
        self.assertEqual(self._reload().lapse_reason, "cancelled")

    def test_cancel_after_retries_exhausted_stays_payment_failed(self) -> None:
        self._record(current=_stripe_sub(status="canceled", cancel_reason="payment_failed"))
        self.assertEqual(self._reload().lapse_reason, "payment_failed")

    def test_dispute_cancellation_records_nothing(self) -> None:
        outcome = self._record(current=_stripe_sub(status="canceled", cancel_reason="payment_disputed"))
        self.assertEqual(outcome, "ignored")
        self.assertEqual(self._reload().lapse_reason, "")

    def test_active_again_clears_the_signal(self) -> None:
        self._record(current=_stripe_sub(status="past_due"))
        outcome = self._record(current=_stripe_sub(status="active"))
        self.assertEqual(outcome, "cleared")
        self.assertEqual(self._reload().lapse_reason, "")

    def test_incomplete_is_ignored(self) -> None:
        outcome = self._record(current=_stripe_sub(status="incomplete"))
        self.assertEqual(outcome, "ignored")
        self.assertIsNone(self._reload().lapse_signal_at)

    def test_state_is_re_read_from_stripe_so_a_stale_event_cannot_regress_it(self) -> None:
        # The delivered event says past_due, but Stripe now says active.
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_sub(status="active")
        outcome = record_subscription_lapse_signal(
            stripe_subscription={**_event_object(), "status": "past_due"}
        )
        self.assertEqual(outcome, "cleared")
        self.assertEqual(self._reload().lapse_reason, "")

    def test_older_observation_does_not_overwrite_a_newer_one(self) -> None:
        newer = timezone.now() + timedelta(hours=1)
        Subscription.objects.filter(pk=self.subscription.pk).update(
            lapse_reason="cancelled", lapse_signal_at=newer
        )
        outcome = self._record(current=_stripe_sub(status="past_due"))
        self.assertEqual(outcome, "stale_event")
        self.assertEqual(self._reload().lapse_reason, "cancelled")

    def test_event_for_a_different_stripe_subscription_is_ignored(self) -> None:
        outcome = self._record(current=_stripe_sub(status="canceled", sub_id="sub_old"), sub_id="sub_old")
        self.assertEqual(outcome, "stale_subscription")
        self.stripe.v1.subscriptions.retrieve.assert_not_called()
        self.assertEqual(self._reload().lapse_reason, "")

    def test_unknown_customer_is_a_noop(self) -> None:
        outcome = record_subscription_lapse_signal(
            stripe_subscription={"id": "sub_x", "customer": "cus_nobody"}
        )
        self.assertEqual(outcome, "unknown_customer")
        self.stripe.v1.subscriptions.retrieve.assert_not_called()

    def test_revoked_account_is_left_untouched(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(status="revoked")
        outcome = self._record(current=_stripe_sub(status="past_due"))
        self.assertEqual(outcome, "not_eligible")
        after = self._reload()
        self.assertEqual(after.status, "revoked")
        self.assertEqual(after.lapse_reason, "")

    def test_staff_account_is_not_eligible(self) -> None:
        User.objects.filter(pk=self.user.pk).update(is_staff=True)
        outcome = self._record(current=_stripe_sub(status="past_due"))
        self.assertEqual(outcome, "not_eligible")

    def test_stripe_failure_propagates_so_the_task_retries(self) -> None:
        self.stripe.v1.subscriptions.retrieve.side_effect = stripe.APIConnectionError("down")
        with self.assertRaises(stripe.APIConnectionError):
            record_subscription_lapse_signal(stripe_subscription=_event_object())

    def test_uses_the_background_client_only(self) -> None:
        self._record(current=_stripe_sub(status="past_due"))
        self.mock_get_stripe_client.assert_called()
        self.mock_get_interactive_stripe_client.assert_not_called()


@override_settings(**_STRIPE_TEST_SETTINGS)
class LapseSignalTaskWiringTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="wired", email="wired@example.com")
        self.subscription = Subscription.objects.create(
            user=self.user,
            stripe_customer_id=_CUSTOMER,
            stripe_subscription_id=_SUBSCRIPTION,
            status="active",
            access_expires_at=timezone.now() - timedelta(days=1),
        )

    def _store(self, *, event_id: str, event_type: str) -> None:
        StripeEvent.objects.create(
            stripe_event_id=event_id,
            event_type=event_type,
            stripe_created=timezone.now(),
            payload={"id": event_id, "type": event_type, "data": {"object": _event_object()}},
        )

    def test_both_event_types_are_handled(self) -> None:
        self.assertIn("customer.subscription.updated", STRIPE_HANDLED_EVENT_TYPES)
        self.assertIn("customer.subscription.deleted", STRIPE_HANDLED_EVENT_TYPES)

    def test_updated_event_records_the_signal_and_replay_is_inert(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_sub(status="past_due")
        self._store(event_id="evt_sub_upd", event_type="customer.subscription.updated")
        process_stripe_event(stripe_event_id="evt_sub_upd")
        process_stripe_event(stripe_event_id="evt_sub_upd")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.lapse_reason, "payment_failed")
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_sub_upd").status, "processed")

    def test_deleted_event_records_cancelled(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_sub(
            status="canceled", cancel_reason="cancellation_requested"
        )
        self._store(event_id="evt_sub_del", event_type="customer.subscription.deleted")
        process_stripe_event(stripe_event_id="evt_sub_del")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.lapse_reason, "cancelled")

    def test_stripe_failure_marks_the_event_failed_and_retries(self) -> None:
        from celery.exceptions import Retry

        self.stripe.v1.subscriptions.retrieve.side_effect = stripe.APIConnectionError("down")
        self._store(event_id="evt_sub_fail", event_type="customer.subscription.updated")
        with patch.object(process_stripe_event, "retry", side_effect=Retry()) as mock_retry:
            with self.assertLogs("fragrance.tasks", level="ERROR"), self.assertRaises(Retry):
                process_stripe_event(stripe_event_id="evt_sub_fail")
        mock_retry.assert_called_once()
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_sub_fail").status, "failed")


class LapseReasonPayloadTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="payload", email="payload@example.com")
        self.now = timezone.now()
        self.subscription = Subscription.objects.create(
            user=self.user,
            stripe_customer_id="cus_payload",
            status="active",
            access_expires_at=self.now - timedelta(days=2),
            last_paid_at=self.now - timedelta(days=32),
            lapse_reason="payment_failed",
            lapse_signal_at=self.now - timedelta(days=1),
        )

    def _payload(self) -> str | None:
        self.subscription.refresh_from_db()
        return subscription_status_payload(subscription=self.subscription)["lapse_reason"]

    def test_lapsed_account_reports_the_stored_reason(self) -> None:
        self.assertEqual(self._payload(), "payment_failed")

    def test_cancelled_reason_is_reported(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(lapse_reason="cancelled")
        self.assertEqual(self._payload(), "cancelled")

    def test_no_signal_is_null(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(lapse_reason="", lapse_signal_at=None)
        self.assertIsNone(self._payload())

    def test_hidden_while_access_is_active(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(
            access_expires_at=self.now + timedelta(days=5)
        )
        self.assertIsNone(self._payload())

    def test_hidden_once_a_payment_was_recorded_after_the_signal(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(last_paid_at=self.now)
        self.assertIsNone(self._payload())

    def test_hidden_for_revoked_accounts(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(status="revoked")
        self.assertIsNone(self._payload())

    def test_hidden_for_never_subscribed_rows(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(
            status="checkout_started", access_expires_at=None
        )
        self.assertIsNone(self._payload())

    def test_hidden_for_exempt_users(self) -> None:
        User.objects.filter(pk=self.user.pk).update(is_staff=True)
        self.subscription.refresh_from_db()
        self.subscription.user.refresh_from_db()
        self.assertIsNone(lapse_reason_payload(subscription=self.subscription))

    def test_endpoint_exposes_the_reason_for_the_owner_only(self) -> None:
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=self.user)
        self.assertEqual(
            client.get("/api/v1/billing/subscription/").json()["lapse_reason"], "payment_failed"
        )
        other = User.objects.create_user(username="other_payload", email="other_payload@example.com")
        other_client = APIClient()
        other_client.force_authenticate(user=other)
        self.assertIsNone(other_client.get("/api/v1/billing/subscription/").json()["lapse_reason"])
