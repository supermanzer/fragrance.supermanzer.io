"""
Time limits on the single worker slot: process_stripe_event is bounded, and
reconcile_subscriptions lets SoftTimeLimitExceeded propagate instead of
counting it as a per-invoice error.
"""

from unittest.mock import patch

import stripe
from celery.exceptions import Retry, SoftTimeLimitExceeded
from django.test import override_settings
from fragrance import billing
from fragrance.models import StripeEvent
from fragrance.tasks import process_stripe_event, reconcile_stripe_subscriptions
from fragrance.tests.support.bases import StripeClientTestCase
from fragrance.tests.support.stripe_fakes import _STRIPE_TEST_SETTINGS, _list_object


class TaskLimitTests(StripeClientTestCase):
    def test_process_stripe_event_is_bounded_below_the_reconcile_ceiling(self) -> None:
        self.assertEqual(process_stripe_event.soft_time_limit, 300)
        self.assertEqual(process_stripe_event.time_limit, 330)
        self.assertLess(process_stripe_event.soft_time_limit, process_stripe_event.time_limit)

    def test_soft_limit_in_process_stripe_event_records_failed_and_takes_the_retry_ladder(self) -> None:
        StripeEvent.objects.create(
            stripe_event_id="evt_slow",
            event_type="payment_intent.succeeded",
            payload={"data": {"object": {"id": "pi_slow"}}},
        )
        process_stripe_event.push_request(retries=0)
        try:
            with patch("fragrance.billing.fulfill_payment_intent", side_effect=SoftTimeLimitExceeded()):
                with patch.object(process_stripe_event, "retry", side_effect=Retry()) as retry:
                    with self.assertRaises(Retry):
                        process_stripe_event(stripe_event_id="evt_slow")
        finally:
            process_stripe_event.pop_request()

        self.assertEqual(retry.call_args.kwargs["countdown"], 300)
        row = StripeEvent.objects.get(stripe_event_id="evt_slow")
        self.assertEqual(row.status, "failed")


@override_settings(**_STRIPE_TEST_SETTINGS)
class ReconcileSoftLimitTests(StripeClientTestCase):
    def test_soft_limit_while_listing_invoices_propagates(self) -> None:
        self.stripe.v1.invoices.list.side_effect = SoftTimeLimitExceeded()
        with self.assertRaises(SoftTimeLimitExceeded):
            billing.reconcile_subscriptions()

    def test_soft_limit_while_handling_an_invoice_propagates_and_is_not_an_invoice_error(self) -> None:
        from django.contrib.auth.models import User
        from fragrance.models import Subscription
        from fragrance.tests.support.stripe_fakes import _sdk_invoice

        user = User.objects.create_user(username="slow", email="slow@example.test", password="pw12345!")
        Subscription.objects.create(user=user, stripe_customer_id="cus_slow")
        invoice = _sdk_invoice(invoice_id="in_slow", customer_id="cus_slow", subscription_id="sub_slow", amount_paid=400, lines=[])
        self.stripe.v1.invoices.list.return_value = _list_object(data=[invoice])
        with patch("fragrance.billing._grant_period_for_invoice", side_effect=SoftTimeLimitExceeded()):
            with self.assertLogs("fragrance.billing", level="ERROR") as logs:
                with self.assertRaises(SoftTimeLimitExceeded):
                    billing.reconcile_subscriptions()

        self.assertTrue(any("soft time limit" in line for line in logs.output))
        self.assertFalse(any("invoice in_slow" in line and "failed" in line for line in logs.output))

    def test_an_ordinary_invoice_error_is_still_counted_and_the_sweep_continues(self) -> None:
        from django.contrib.auth.models import User
        from fragrance.models import Subscription
        from fragrance.tests.support.stripe_fakes import _sdk_invoice

        user = User.objects.create_user(username="boom", email="boom@example.test", password="pw12345!")
        Subscription.objects.create(user=user, stripe_customer_id="cus_boom")
        invoice = _sdk_invoice(invoice_id="in_boom", customer_id="cus_boom", subscription_id="sub_boom", amount_paid=400, lines=[])
        self.stripe.v1.invoices.list.return_value = _list_object(data=[invoice])
        with patch("fragrance.billing._grant_period_for_invoice", side_effect=stripe.APIConnectionError("down")):
            with self.assertLogs("fragrance.billing", level="ERROR"):
                summary = billing.reconcile_subscriptions()

        self.assertEqual(summary["errors"], 1)
