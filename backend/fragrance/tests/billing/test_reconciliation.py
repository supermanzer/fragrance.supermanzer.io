from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import stripe
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone
from fragrance.models import (
    BetaSignup,
    Plan,
    ProcessedPayment,
    StripeEvent,
    Subscription,
)
from fragrance.tests.support.bases import StripeClientTestCase
from fragrance.tests.support.stripe_fakes import (
    _STRIPE_TEST_SETTINGS,
    _list_object,
    _sdk_invoice,
    _sdk_line,
)


@override_settings(**_STRIPE_TEST_SETTINGS)
class ReconcileSubscriptionsTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        from fragrance.billing import reconcile_subscriptions

        self.reconcile = reconcile_subscriptions
        self.plan = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, billing_interval="month",
        )
        self.period_end = int(timezone.now().timestamp()) + 30 * 86400

    def _make_user(self, *, name: str, customer_id: str | None, status: str = "checkout_started",
                   expires_in: timedelta | None = None) -> Subscription:
        user = User.objects.create_user(username=name, email=f"{name}@example.com")
        BetaSignup.objects.create(
            email=f"{name}@example.com", normalized_email=f"{name}@example.com",
            status="converted", invited_user=user,
        )
        return Subscription.objects.create(
            user=user, stripe_customer_id=customer_id, status=status,
            access_expires_at=timezone.now() + expires_in if expires_in is not None else None,
        )

    def _invoice(self, *, invoice_id: str, customer_id: str, **kwargs) -> stripe.Invoice:
        return _sdk_invoice(
            invoice_id=invoice_id, customer_id=customer_id, subscription_id=f"sub_{invoice_id}", amount_paid=400,
            lines=[_sdk_line(price_id="price_mo", period_end=self.period_end)], **kwargs,
        )

    def _listing(self, *invoices: stripe.Invoice) -> stripe.ListObject:
        return _list_object(data=list(invoices))

    def test_lost_renewal_and_paid_never_loaded_are_granted_and_rerun_is_noop(self) -> None:
        renewal = self._make_user(name="renew", customer_id="cus_renew", status="active", expires_in=timedelta(days=-3))
        loaded = self._make_user(name="missed", customer_id="cus_missed")
        self.stripe.v1.invoices.list.return_value = self._listing(
            self._invoice(invoice_id="in_renew", customer_id="cus_renew"),
            self._invoice(invoice_id="in_missed", customer_id="cus_missed"),
        )

        first = self.reconcile()
        renewal.refresh_from_db()
        loaded.refresh_from_db()
        expiry_after_first = loaded.access_expires_at
        second = self.reconcile()
        loaded.refresh_from_db()

        self.assertEqual(first["recorded"], 2)
        self.assertEqual(renewal.status, "active")
        self.assertGreater(renewal.access_expires_at, timezone.now())
        self.assertEqual(loaded.status, "active")
        self.assertEqual(second["recorded"], 0)
        self.assertEqual(second["already_applied"], 2)
        self.assertEqual(loaded.access_expires_at, expiry_after_first)
        self.assertEqual(ProcessedPayment.objects.count(), 2)

    def test_active_subscriber_long_past_expiry_is_still_reconciled(self) -> None:
        sub = self._make_user(name="ancient", customer_id="cus_old", status="active", expires_in=-timedelta(days=40))
        self.stripe.v1.invoices.list.return_value = self._listing(self._invoice(invoice_id="in_old", customer_id="cus_old"))
        self.assertEqual(self.reconcile()["recorded"], 1)
        sub.refresh_from_db()
        self.assertGreater(sub.access_expires_at, timezone.now())

    def test_processed_invoice_counts_as_already_applied(self) -> None:
        sub = self._make_user(name="dup", customer_id="cus_dup", status="active", expires_in=timedelta(days=1))
        ProcessedPayment.objects.create(
            stripe_invoice_id="in_dup", subscription=sub, amount_paid_cents=400,
            period_end=timezone.now() + timedelta(days=1),
        )
        before = sub.access_expires_at
        self.stripe.v1.invoices.list.return_value = self._listing(self._invoice(invoice_id="in_dup", customer_id="cus_dup"))
        summary = self.reconcile()
        sub.refresh_from_db()
        self.assertEqual(summary["already_applied"], 1)
        self.assertEqual(summary["recorded"], 0)
        self.assertEqual(sub.access_expires_at, before)

    def test_unpaid_invoice_grants_nothing(self) -> None:
        sub = self._make_user(name="unpaid", customer_id="cus_unpaid")
        self.stripe.v1.invoices.list.return_value = self._listing(
            self._invoice(invoice_id="in_open", customer_id="cus_unpaid", status="open", amount_remaining=400)
        )
        summary = self.reconcile()
        sub.refresh_from_db()
        self.assertEqual(summary["recorded"], 0)
        self.assertIsNone(sub.access_expires_at)
        self.assertEqual(ProcessedPayment.objects.count(), 0)

    def test_unapproved_user_grants_nothing(self) -> None:
        sub = self._make_user(name="pending", customer_id="cus_pending")
        BetaSignup.objects.filter(invited_user=sub.user).update(status="pending")
        self.stripe.v1.invoices.list.return_value = self._listing(self._invoice(invoice_id="in_pend", customer_id="cus_pending"))
        summary = self.reconcile()
        sub.refresh_from_db()
        self.assertEqual(summary["recorded"], 0)
        self.assertEqual(sub.status, "checkout_started")

    def test_invoice_list_is_one_account_wide_call(self) -> None:
        self._make_user(name="params", customer_id="cus_params")
        self.stripe.v1.invoices.list.return_value = self._listing()
        self.reconcile()
        self.stripe.v1.invoices.list.assert_called_once()
        params = self.stripe.v1.invoices.list.call_args.kwargs["params"]
        self.assertNotIn("customer", params)
        self.assertEqual(params["status"], "paid")
        self.assertEqual(params["limit"], 100)
        now = int(timezone.now().timestamp())
        self.assertAlmostEqual(params["created"]["gte"], now - 7 * 86400, delta=5)

    def test_lookback_is_configurable(self) -> None:
        self.stripe.v1.invoices.list.return_value = self._listing()
        self.reconcile(lookback=timedelta(days=30))
        now = int(timezone.now().timestamp())
        params = self.stripe.v1.invoices.list.call_args.kwargs["params"]
        self.assertAlmostEqual(params["created"]["gte"], now - 30 * 86400, delta=5)

    def test_revoked_customers_paid_invoice_is_never_granted(self) -> None:
        sub = self._make_user(name="revoked", customer_id="cus_rev", status="revoked", expires_in=timedelta(days=1))
        before = sub.access_expires_at
        self.stripe.v1.invoices.list.return_value = self._listing(self._invoice(invoice_id="in_rev", customer_id="cus_rev"))
        with self.assertLogs("fragrance.billing", level="CRITICAL"):
            summary = self.reconcile()
        sub.refresh_from_db()
        # Recorded (and paged) so it is never reprocessed, but nothing is granted.
        self.assertEqual(summary["recorded"], 1)
        self.assertEqual(summary["invoices_matched"], 1)
        self.assertEqual(sub.status, "revoked")
        self.assertEqual(sub.access_expires_at, before)
        self.assertIsNone(sub.plan)
        self.assertEqual(ProcessedPayment.objects.count(), 1)

    def test_invoice_for_unknown_customer_is_ignored(self) -> None:
        self.stripe.v1.invoices.list.return_value = self._listing(self._invoice(invoice_id="in_x", customer_id="cus_stranger"))
        summary = self.reconcile()
        self.assertEqual(summary["invoices_seen"], 1)
        self.assertEqual(summary["invoices_matched"], 0)
        self.assertEqual(summary["recorded"], 0)
        self.assertEqual(ProcessedPayment.objects.count(), 0)

    def test_abandoned_checkout_costs_no_per_customer_call(self) -> None:
        for i in range(25):
            self._make_user(name=f"abandoned{i}", customer_id=f"cus_ab{i}")
        self.stripe.v1.invoices.list.return_value = self._listing()
        self.reconcile()
        self.assertEqual(self.stripe.v1.invoices.list.call_count, 1)

    def test_one_error_does_not_stop_the_batch(self) -> None:
        self._make_user(name="first", customer_id="cus_first")
        self._make_user(name="second", customer_id="cus_second")
        self.stripe.v1.invoices.list.return_value = self._listing(
            self._invoice(invoice_id="in_first", customer_id="cus_first"),
            self._invoice(invoice_id="in_second", customer_id="cus_second"),
        )
        with patch("fragrance.billing._grant_period_for_invoice") as mock_grant:
            mock_grant.side_effect = [RuntimeError("boom"), True]
            summary = self.reconcile()
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["recorded"], 1)

    def test_listing_failure_is_contained_and_events_still_requeued(self) -> None:
        self.stripe.v1.invoices.list.side_effect = stripe.APIConnectionError("boom")
        with self.assertLogs("fragrance.billing", level="ERROR"):
            summary = self.reconcile()
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["recorded"], 0)

    def test_errors_with_no_grants_and_pending_invoices_log_at_error(self) -> None:
        self._make_user(name="stuck", customer_id="cus_stuck")
        self.stripe.v1.invoices.list.return_value = self._listing(self._invoice(invoice_id="in_stuck", customer_id="cus_stuck"))
        with patch("fragrance.billing._grant_period_for_invoice", side_effect=RuntimeError("boom")):
            with self.assertLogs("fragrance.billing", level="ERROR") as logs:
                self.reconcile()
        self.assertTrue(any("NO payments recorded" in line for line in logs.output))

    def test_deadline_stops_the_loop(self) -> None:
        for name in ("a", "b", "c"):
            self._make_user(name=name, customer_id=f"cus_{name}")
        self.stripe.v1.invoices.list.return_value = self._listing(
            *(self._invoice(invoice_id=f"in_{n}", customer_id=f"cus_{n}") for n in ("a", "b", "c"))
        )
        with self.assertLogs("fragrance.billing", level="ERROR") as logs:
            summary = self.reconcile(time_budget_seconds=0)
        self.assertTrue(summary["deadline_exceeded"])
        self.assertEqual(summary["recorded"], 0)
        self.assertTrue(any("budget exhausted" in line for line in logs.output))

    @patch("fragrance.billing.RECONCILE_MAX_INVOICES", 2)
    def test_pagination_sanity_cap_warns(self) -> None:
        self.stripe.v1.invoices.list.return_value = self._listing(
            *(self._invoice(invoice_id=f"in_{i}", customer_id="cus_none") for i in range(5))
        )
        with self.assertLogs("fragrance.billing", level="WARNING") as logs:
            summary = self.reconcile()
        self.assertTrue(summary["capped"])
        self.assertEqual(summary["invoices_seen"], 2)
        self.assertTrue(any("sanity cap" in line for line in logs.output))

    @override_settings(STRIPE_SECRET_KEY="")
    def test_unset_key_returns_cleanly(self) -> None:
        self._make_user(name="nokey", customer_id="cus_nokey")
        with self.assertLogs("fragrance.billing", level="WARNING"):
            summary = self.reconcile()
        self.assertTrue(summary["skipped"])
        self.stripe.v1.invoices.list.assert_not_called()

    def test_dry_run_writes_nothing(self) -> None:
        sub = self._make_user(name="dry", customer_id="cus_dry")
        self.stripe.v1.invoices.list.return_value = self._listing(self._invoice(invoice_id="in_dry", customer_id="cus_dry"))
        with patch("fragrance.tasks.process_stripe_event.delay") as mock_delay:
            StripeEvent.objects.create(
                stripe_event_id="evt_dry", event_type="payment_intent.succeeded",
                payload={"data": {"object": {"id": "pi_1"}}},
            )
            StripeEvent.objects.filter(stripe_event_id="evt_dry").update(
                received_at=timezone.now() - timedelta(hours=1)
            )
            summary = self.reconcile(dry_run=True)
            mock_delay.assert_not_called()
        sub.refresh_from_db()
        self.assertEqual(summary["would_grant"], 1)
        self.assertEqual(summary["events_requeued"], 1)
        self.assertEqual(sub.status, "checkout_started")
        self.assertIsNone(sub.access_expires_at)
        self.assertEqual(ProcessedPayment.objects.count(), 0)

    def test_management_command_dry_run_and_days(self) -> None:
        self._make_user(name="cmd", customer_id="cus_cmd")
        self.stripe.v1.invoices.list.return_value = self._listing(self._invoice(invoice_id="in_cmd", customer_id="cus_cmd"))
        out = StringIO()
        call_command("reconcile_stripe", "--dry-run", "--days", "30", stdout=out)
        self.assertIn("would_grant: 1", out.getvalue())
        self.assertEqual(ProcessedPayment.objects.count(), 0)
        now = int(timezone.now().timestamp())
        params = self.stripe.v1.invoices.list.call_args.kwargs["params"]
        self.assertAlmostEqual(params["created"]["gte"], now - 30 * 86400, delta=5)

    def test_management_command_rejects_non_positive_days(self) -> None:
        with self.assertRaises(CommandError):
            call_command("reconcile_stripe", "--days", "0")

    def test_reconcile_task_has_time_limits(self) -> None:
        from fragrance.tasks import reconcile_stripe_subscriptions

        self.assertEqual(reconcile_stripe_subscriptions.soft_time_limit, 240)
        self.assertEqual(reconcile_stripe_subscriptions.time_limit, 300)


@override_settings(**_STRIPE_TEST_SETTINGS)
class ReconcileStuckEventsTests(StripeClientTestCase):
    def _event(self, *, evt_id: str, status: str, age: timedelta, payload: dict | None) -> None:
        StripeEvent.objects.create(
            stripe_event_id=evt_id, event_type="payment_intent.succeeded", status=status,
            payload=payload if payload is not None else {},
        )
        StripeEvent.objects.filter(stripe_event_id=evt_id).update(received_at=timezone.now() - age)

    @patch("fragrance.tasks.process_stripe_event.delay")
    def test_only_stuck_events_in_window_are_requeued(self, mock_delay) -> None:
        from fragrance.billing import reconcile_subscriptions

        payload = {"data": {"object": {"id": "pi_x"}}}
        self._event(evt_id="evt_recv", status="received", age=timedelta(hours=1), payload=payload)
        self._event(evt_id="evt_fail", status="failed", age=timedelta(hours=5), payload=payload)
        self._event(evt_id="evt_fresh", status="received", age=timedelta(minutes=2), payload=payload)
        self._event(evt_id="evt_old", status="failed", age=timedelta(days=5), payload=payload)
        self._event(evt_id="evt_done", status="processed", age=timedelta(hours=2), payload=payload)
        self._event(evt_id="evt_nopayload", status="failed", age=timedelta(hours=2), payload=None)

        # Invoice.list is deliberately left unconfigured: the invoice-listing failure
        # (now a guarded-fake APIConnectionError, standing in for whatever real error
        # would occur) is contained by reconcile_subscriptions' own try/except, and
        # _requeue_stuck_events runs regardless -- this test's whole point is that the
        # requeue step is independent of the invoice-listing outcome. Opts out of the
        # end-of-test "unconfigured call" tripwire for exactly that reason.
        self.stripe.allow_unconfigured_stripe_calls = True
        with self.assertLogs("fragrance.billing", level="WARNING"):
            summary = reconcile_subscriptions()

        queued = sorted(call.kwargs["stripe_event_id"] for call in mock_delay.call_args_list)
        self.assertEqual(queued, ["evt_fail", "evt_recv"])
        self.assertEqual(summary["events_requeued"], 2)
        self.assertEqual(summary["events_skipped_no_payload"], 1)

    @patch("fragrance.billing.reconcile_subscriptions")
    def test_celery_task_delegates(self, mock_reconcile) -> None:
        from fragrance.tasks import reconcile_stripe_subscriptions

        mock_reconcile.return_value = {"recorded": 0}
        reconcile_stripe_subscriptions()
        mock_reconcile.assert_called_once_with(dry_run=False)
