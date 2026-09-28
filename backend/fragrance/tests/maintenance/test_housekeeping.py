from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from fragrance.models import RecommendationRun, StripeEvent
from fragrance.services import (
    QuotaExceeded,
    RunInProgress,
    quota_snapshot,
    start_recommendation_run,
)
from fragrance.tasks import monthly_fragrance_run
from fragrance.tests.support.factories import active_subscription_for
from rest_framework_simplejwt.tokens import RefreshToken


class NightlyHousekeepingTests(TestCase):
    def test_old_payloads_are_emptied_rows_kept_recent_untouched(self) -> None:
        from fragrance.tasks import prune_stripe_event_payloads

        payload = {"data": {"object": {"id": "pi_x"}}}
        for evt_id in ("evt_old", "evt_recent"):
            StripeEvent.objects.create(
                stripe_event_id=evt_id, event_type="payment_intent.succeeded",
                status="processed", payload=payload,
            )
        StripeEvent.objects.filter(stripe_event_id="evt_old").update(
            received_at=timezone.now() - timedelta(days=91)
        )

        self.assertEqual(prune_stripe_event_payloads(), 1)

        old = StripeEvent.objects.get(stripe_event_id="evt_old")
        self.assertEqual(old.payload, {})
        self.assertEqual(old.event_type, "payment_intent.succeeded")
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_recent").payload, payload)
        self.assertEqual(prune_stripe_event_payloads(), 0)

    def test_replay_guard_survives_pruning(self) -> None:
        from django.db import IntegrityError, transaction

        from fragrance.tasks import prune_stripe_event_payloads

        StripeEvent.objects.create(stripe_event_id="evt_guard", event_type="charge.dispute.created", payload={"a": 1})
        StripeEvent.objects.filter(stripe_event_id="evt_guard").update(
            received_at=timezone.now() - timedelta(days=120)
        )
        prune_stripe_event_payloads()
        with self.assertRaises(IntegrityError), transaction.atomic():
            StripeEvent.objects.create(stripe_event_id="evt_guard", event_type="charge.dispute.created")
        self.assertEqual(StripeEvent.objects.filter(stripe_event_id="evt_guard").count(), 1)

    def test_flush_expired_tokens_deletes_expired_outstanding_only(self) -> None:
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

        from fragrance.tasks import flush_expired_jwt_tokens

        user = User.objects.create_user(username="tok", password="pw12345!")
        expired = RefreshToken.for_user(user)
        expired_jti = expired["jti"]
        OutstandingToken.objects.filter(jti=expired_jti).update(expires_at=timezone.now() - timedelta(days=1))
        live_jti = RefreshToken.for_user(user)["jti"]

        flush_expired_jwt_tokens()

        self.assertFalse(OutstandingToken.objects.filter(jti=expired_jti).exists())
        self.assertTrue(OutstandingToken.objects.filter(jti=live_jti).exists())


class StaleInFlightRunTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="stale", password="pw12345!")
        active_subscription_for(self.user)

    def _stranded(self, *, hours_ago: float, run_status: str = "running") -> RecommendationRun:
        run = RecommendationRun.objects.create(user=self.user, status=run_status)
        RecommendationRun.objects.filter(pk=run.pk).update(
            triggered_at=timezone.now() - timedelta(hours=hours_ago)
        )
        return run

    def test_run_older_than_bound_no_longer_blocks_trigger(self) -> None:
        self._stranded(hours_ago=3)
        # The stale row is neither in flight nor (once reaped) quota-consuming;
        # un-reaped it still counts toward the quota, so use a staff user for
        # the pure in-flight check below and the reaper test for the quota one.
        staff = User.objects.create_user(username="stale-staff", password="pw12345!", is_staff=True)
        RecommendationRun.objects.create(user=staff, status="running")
        RecommendationRun.objects.filter(user=staff).update(
            triggered_at=timezone.now() - timedelta(hours=3)
        )
        self.assertEqual(start_recommendation_run(user=staff, source="test").status, "pending")

    def test_run_younger_than_bound_still_blocks(self) -> None:
        staff = User.objects.create_user(username="fresh-staff", password="pw12345!", is_staff=True)
        RecommendationRun.objects.create(user=staff, status="running")
        RecommendationRun.objects.filter(user=staff).update(
            triggered_at=timezone.now() - timedelta(hours=1)
        )
        with self.assertRaises(RunInProgress):
            start_recommendation_run(user=staff, source="test")

    def test_reaper_fails_only_stale_rows(self) -> None:
        from fragrance.tasks import STALE_RUN_ERROR_MESSAGE, reap_stale_runs

        stale_running = self._stranded(hours_ago=3, run_status="running")
        stale_pending = self._stranded(hours_ago=5, run_status="pending")
        fresh = self._stranded(hours_ago=1, run_status="running")
        old_done = self._stranded(hours_ago=9, run_status="done")

        self.assertEqual(reap_stale_runs(), 2)

        for run in (stale_running, stale_pending):
            run.refresh_from_db()
            self.assertEqual(run.status, "failed")
            self.assertEqual(run.error_message, STALE_RUN_ERROR_MESSAGE)
        fresh.refresh_from_db()
        old_done.refresh_from_db()
        self.assertEqual(fresh.status, "running")
        self.assertEqual(old_done.status, "done")
        self.assertEqual(reap_stale_runs(), 0)

    def test_reaped_run_does_not_count_toward_quota(self) -> None:
        from fragrance.tasks import reap_stale_runs

        self._stranded(hours_ago=3, run_status="running")
        with self.assertRaises(QuotaExceeded):
            start_recommendation_run(user=self.user, source="test")
        reap_stale_runs()
        self.assertEqual(quota_snapshot(user=self.user, subscription=self.user.subscription)["runs_used"], 0)
        self.assertEqual(start_recommendation_run(user=self.user, source="test").status, "pending")

    def test_snapshot_can_trigger_agrees_with_chokepoint(self) -> None:
        staff = User.objects.create_user(username="agree-staff", password="pw12345!", is_staff=True)
        for hours_ago, expected in ((1, False), (3, True)):
            RecommendationRun.objects.filter(user=staff).delete()
            RecommendationRun.objects.create(user=staff, status="running")
            RecommendationRun.objects.filter(user=staff).update(
                triggered_at=timezone.now() - timedelta(hours=hours_ago)
            )
            snapshot = quota_snapshot(user=staff, subscription=None)
            self.assertEqual(snapshot["can_trigger"], expected)
            try:
                start_recommendation_run(user=staff, source="test")
                started = True
            except RunInProgress:
                started = False
            self.assertEqual(started, expected)

    def test_reaper_is_registered_in_beat_schedule(self) -> None:
        from config.celery import app

        entry = app.conf.beat_schedule["system-reap-stale-runs"]
        self.assertEqual(entry["task"], "fragrance.tasks.reap_stale_runs")

    def test_monthly_run_has_time_limits(self) -> None:
        self.assertEqual(monthly_fragrance_run.soft_time_limit, 1200)
        self.assertEqual(monthly_fragrance_run.time_limit, 1500)

    def test_soft_time_limit_fails_the_run_without_retry(self) -> None:
        from celery.exceptions import SoftTimeLimitExceeded

        run = RecommendationRun.objects.create(user=self.user, status="pending")
        with patch(
            "fragrance.tasks.registry.get_active_config", side_effect=SoftTimeLimitExceeded()
        ), patch.object(monthly_fragrance_run, "retry") as retry:
            monthly_fragrance_run(user_id=self.user.id, run_id=run.id)
        retry.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertTrue(run.error_message)
