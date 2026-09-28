from datetime import timedelta
from unittest.mock import patch

import stripe
from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone
from fragrance.billing import revoke_for_dispute
from fragrance.models import StripeEvent, Subscription
from fragrance.tasks import process_stripe_event
from fragrance.tests.support.bases import StripeClientTestCase
from fragrance.tests.support.stripe_fakes import (
    _KEY,
    _STRIPE_TEST_SETTINGS,
    _sdk_payment_intent,
    _sub_list,
)


def _empty_subscription_list() -> stripe.ListObject:
    return stripe.ListObject.construct_from(
        {"object": "list", "data": [], "has_more": False}, _KEY
    )


@override_settings(**_STRIPE_TEST_SETTINGS)
class RevokeForDisputeTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="disputeduser", email="disputeduser@example.com")
        self.subscription = Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_dispute_1", status="active",
            access_expires_at=timezone.now() + timedelta(days=20),
        )

    def test_resolvable_dispute_revokes_immediately(self) -> None:
        self.stripe.v1.subscriptions.list.return_value = _empty_subscription_list()
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_dispute", status="succeeded", customer="cus_dispute_1"
        )
        before = timezone.now()

        revoke_for_dispute(dispute={"payment_intent": "pi_dispute"})

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")
        self.assertGreaterEqual(self.subscription.access_expires_at, before)

    def test_missing_payment_intent_revokes_nothing(self) -> None:
        revoke_for_dispute(dispute={})
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "active")
        self.assertIsNotNone(self.subscription.access_expires_at)

    def test_unknown_customer_revokes_nothing(self) -> None:
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_x", status="succeeded", customer="cus_unknown"
        )
        revoke_for_dispute(dispute={"payment_intent": "pi_x"})
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "active")

    def test_unresolvable_charge_revokes_nothing(self) -> None:
        self.stripe.v1.payment_intents.retrieve.side_effect = stripe.InvalidRequestError(
            "no such payment_intent", None
        )
        revoke_for_dispute(dispute={"payment_intent": "pi_missing"})
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "active")

    def test_expanded_payment_intent_object_is_normalized_to_its_id(self) -> None:
        """
        Dispute.payment_intent is Optional[ExpandableField["PaymentIntent"]]
        -- if the event ever arrives with that field expanded, it's a nested
        object, not a bare id string. PaymentIntent.retrieve must still be
        called with the id, not the dict.
        """
        self.stripe.v1.subscriptions.list.return_value = _empty_subscription_list()
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_dispute", status="succeeded", customer="cus_dispute_1"
        )

        revoke_for_dispute(dispute={"payment_intent": {"id": "pi_dispute", "object": "payment_intent"}})

        self.stripe.v1.payment_intents.retrieve.assert_called_once_with("pi_dispute")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")


@override_settings(**_STRIPE_TEST_SETTINGS)
class DisputeCancelsStripeSubscriptionTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="dispcancel", email="dispcancel@example.com")
        self.subscription = Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_dc_1", status="active",
            access_expires_at=timezone.now() + timedelta(days=20),
        )
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_dc", status="succeeded", customer="cus_dc_1"
        )

    def test_cancels_every_live_subscription_immediately_without_proration(self) -> None:
        self.stripe.v1.subscriptions.list.return_value = _sub_list(statuses={
            "sub_a": "active", "sub_b": "past_due", "sub_c": "unpaid", "sub_d": "trialing",
            "sub_e": "incomplete", "sub_f": "canceled", "sub_g": "incomplete_expired", "sub_h": "paused",
        })
        self.stripe.v1.subscriptions.cancel.return_value = None
        with self.assertLogs("fragrance.billing", level="CRITICAL") as logs:
            revoke_for_dispute(dispute={"payment_intent": "pi_dc"})
        cancelled = [call.args[0] for call in self.stripe.v1.subscriptions.cancel.call_args_list]
        self.assertEqual(cancelled, ["sub_a", "sub_b", "sub_c", "sub_d", "sub_e"])
        for call in self.stripe.v1.subscriptions.cancel.call_args_list:
            self.assertEqual(call.kwargs, {"params": {"invoice_now": False, "prorate": False}})
        self.assertEqual(
            self.stripe.v1.subscriptions.list.call_args.kwargs["params"]["customer"], "cus_dc_1"
        )
        self.assertIn("sub_a", logs.output[0])
        self.assertNotIn("sk_test", logs.output[0])
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")

    def test_no_live_subscriptions_is_fine_and_still_revokes(self) -> None:
        self.stripe.v1.subscriptions.list.return_value = _sub_list(statuses={"sub_old": "canceled"})
        revoke_for_dispute(dispute={"payment_intent": "pi_dc"})
        self.stripe.v1.subscriptions.cancel.assert_not_called()
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")

    def test_already_canceled_race_counts_as_done(self) -> None:
        self.stripe.v1.subscriptions.list.return_value = _sub_list(statuses={"sub_a": "active"})
        self.stripe.v1.subscriptions.cancel.side_effect = stripe.InvalidRequestError("already canceled", None)
        self.stripe.v1.subscriptions.retrieve.return_value = stripe.Subscription.construct_from(
            {"id": "sub_a", "object": "subscription", "status": "canceled"}, _KEY
        )
        revoke_for_dispute(dispute={"payment_intent": "pi_dc"})
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")

    def test_cancel_failure_raises_after_attempting_all_and_keeps_local_revoke(self) -> None:
        self.stripe.v1.subscriptions.list.return_value = _sub_list(
            statuses={"sub_a": "active", "sub_b": "active"}
        )
        self.stripe.v1.subscriptions.cancel.side_effect = [
            stripe.APIConnectionError("down"),
            stripe.Subscription.construct_from({"id": "sub_b", "object": "subscription", "status": "active"}, _KEY),
        ]
        with self.assertRaises(stripe.APIConnectionError):
            revoke_for_dispute(dispute={"payment_intent": "pi_dc"})
        self.assertEqual(self.stripe.v1.subscriptions.cancel.call_count, 2)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")

    def test_task_retries_on_cancel_failure_then_replay_is_inert(self) -> None:
        self.stripe.v1.subscriptions.list.return_value = _sub_list(statuses={"sub_a": "active"})
        self.stripe.v1.subscriptions.cancel.side_effect = stripe.APIConnectionError("down")
        StripeEvent.objects.create(
            stripe_event_id="evt_dc", event_type="charge.dispute.created",
            payload={"data": {"object": {"payment_intent": "pi_dc"}}},
        )
        with patch.object(process_stripe_event, "retry", side_effect=RuntimeError("retried")) as mock_retry:
            result = process_stripe_event.apply(kwargs={"stripe_event_id": "evt_dc"})
        self.assertTrue(result.failed())
        mock_retry.assert_called_once()
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_dc").status, "failed")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")
        first_expiry = self.subscription.access_expires_at

        # Retry: Stripe recovers; the sub is now canceled, so nothing left to cancel.
        self.stripe.v1.subscriptions.cancel.reset_mock(side_effect=True)
        self.stripe.v1.subscriptions.list.return_value = _sub_list(statuses={"sub_a": "canceled"})
        process_stripe_event(stripe_event_id="evt_dc")
        self.stripe.v1.subscriptions.cancel.assert_not_called()
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_dc").status, "processed")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.access_expires_at, first_expiry)

    def test_unresolved_customer_cancels_nothing(self) -> None:
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_x", status="succeeded", customer="cus_unknown"
        )
        revoke_for_dispute(dispute={"payment_intent": "pi_x"})
        self.stripe.v1.subscriptions.list.assert_not_called()
        self.stripe.v1.subscriptions.cancel.assert_not_called()
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "active")

    def test_transient_payment_intent_failure_raises_and_cancels_nothing(self) -> None:
        self.stripe.v1.payment_intents.retrieve.side_effect = stripe.APIConnectionError("down")
        with self.assertRaises(stripe.APIConnectionError):
            revoke_for_dispute(dispute={"payment_intent": "pi_dc"})
        self.stripe.v1.subscriptions.cancel.assert_not_called()
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "active")
