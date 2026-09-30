"""The retry chain's state machine (services), the in-flight clock, and the gates that read them."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.utils import timezone
from django.urls import reverse
from fragrance.models import RecommendationRun, Subscription
from fragrance.services import (
    MAX_RUN_RETRIES,
    RUN_RETRY_INTERVAL,
    QuotaExceeded,
    claim_run_retry,
    in_flight_runs,
    quota_snapshot,
    record_run_failure,
    start_recommendation_run,
    unclaim_run_retry,
)
from fragrance.tasks import reap_stale_runs
from fragrance.tests.retries.helpers import FrozenClockTestCase, FROZEN_NOW, make_run, make_scheduled_run
from fragrance.tests.support.factories import active_subscription_for
from rest_framework.test import APIClient


class RecordRunFailureTests(FrozenClockTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="chain", password="pw12345!")
        active_subscription_for(self.user)

    def test_first_failure_schedules_a_retry_24_hours_out_on_the_same_row(self) -> None:
        run = make_run(user=self.user, status="running", started_days_ago=0)

        outcome = record_run_failure(run_id=run.id, error_message="boom", now=FROZEN_NOW)

        run.refresh_from_db()
        self.assertEqual(outcome, "scheduled")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.retry_state, "scheduled")
        self.assertEqual(run.next_retry_at, FROZEN_NOW + RUN_RETRY_INTERVAL)
        self.assertEqual(run.last_failed_at, FROZEN_NOW)
        self.assertEqual(run.error_message, "boom")
        self.assertEqual(RecommendationRun.objects.filter(user=self.user).count(), 1)

    def test_the_fifth_failure_exhausts_the_chain(self) -> None:
        run = make_run(user=self.user, status="running", attempt=MAX_RUN_RETRIES + 1)

        outcome = record_run_failure(run_id=run.id, error_message="boom", now=FROZEN_NOW)

        run.refresh_from_db()
        self.assertEqual(outcome, "exhausted")
        self.assertEqual(run.retry_state, "exhausted")
        self.assertIsNone(run.next_retry_at)

    def test_the_fourth_failure_still_gets_its_last_retry(self) -> None:
        run = make_run(user=self.user, status="running", attempt=MAX_RUN_RETRIES)

        self.assertEqual(record_run_failure(run_id=run.id, error_message="x", now=FROZEN_NOW), "scheduled")

    def test_a_retry_that_would_fall_outside_the_30_day_period_exhausts_instead(self) -> None:
        run = make_run(user=self.user, status="running", started_days_ago=29.5, attempt=2)

        outcome = record_run_failure(run_id=run.id, error_message="x", now=FROZEN_NOW)

        self.assertEqual(outcome, "exhausted")

    def test_the_period_comes_from_the_plan_not_a_hard_coded_30(self) -> None:
        plan = self.user.subscription.plan
        self.assertIsNone(plan)
        from fragrance.tests.support.factories import _make_plan

        short = _make_plan(slug="short", interval="month")
        short.quota_period_days = 2
        short.save()
        active_subscription_for(self.user, plan=short)
        run = make_run(user=self.user, status="running", started_days_ago=1.5)

        self.assertEqual(record_run_failure(run_id=run.id, error_message="x", now=FROZEN_NOW), "exhausted")

    def test_a_lapsed_account_stops_the_chain(self) -> None:
        Subscription.objects.filter(user=self.user).update(access_expires_at=timezone.now() - timedelta(days=1))
        run = make_run(user=self.user, status="running")

        outcome = record_run_failure(run_id=run.id, error_message="x", now=FROZEN_NOW)

        run.refresh_from_db()
        self.assertEqual(outcome, "stopped")
        self.assertEqual(run.retry_state, "stopped")
        self.assertIsNone(run.next_retry_at)

    def test_a_revoked_account_stops_the_chain(self) -> None:
        Subscription.objects.filter(user=self.user).update(status="revoked")
        run = make_run(user=self.user, status="running")

        self.assertEqual(record_run_failure(run_id=run.id, error_message="x", now=FROZEN_NOW), "stopped")

    def test_staff_without_a_subscription_keep_their_retries(self) -> None:
        staff = User.objects.create_user(username="chain_staff", password="pw12345!", is_staff=True)
        run = make_run(user=staff, status="running")

        self.assertEqual(record_run_failure(run_id=run.id, error_message="x", now=FROZEN_NOW), "scheduled")

    def test_a_run_no_longer_in_flight_is_ignored(self) -> None:
        for status in ("done", "failed"):
            with self.subTest(status=status):
                run = make_run(user=self.user, status=status, error_message="orig")
                self.assertEqual(record_run_failure(run_id=run.id, error_message="new", now=FROZEN_NOW), "ignored")
                run.refresh_from_db()
                self.assertEqual(run.error_message, "orig")
                self.assertEqual(run.retry_state, "")

    def test_a_missing_run_is_ignored(self) -> None:
        self.assertEqual(record_run_failure(run_id=999999, error_message="x", now=FROZEN_NOW), "ignored")

    def test_recording_the_same_failure_twice_does_not_advance_the_chain(self) -> None:
        run = make_run(user=self.user, status="running")
        record_run_failure(run_id=run.id, error_message="x", now=FROZEN_NOW)

        self.assertEqual(record_run_failure(run_id=run.id, error_message="x", now=FROZEN_NOW), "ignored")


class ClaimRunRetryTests(FrozenClockTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="claim", password="pw12345!")
        active_subscription_for(self.user)

    def test_a_due_retry_becomes_a_pending_next_attempt_on_the_same_row(self) -> None:
        run = make_scheduled_run(user=self.user, attempt=1)

        outcome = claim_run_retry(run_id=run.id, now=FROZEN_NOW)

        run.refresh_from_db()
        self.assertEqual(outcome, "dispatch")
        self.assertEqual(run.status, "pending")
        self.assertEqual(run.attempt, 2)
        self.assertEqual(run.attempt_started_at, FROZEN_NOW)
        self.assertEqual(run.retry_state, "")
        self.assertIsNone(run.next_retry_at)
        self.assertEqual(RecommendationRun.objects.filter(user=self.user).count(), 1)

    def test_a_retry_that_is_not_due_yet_is_skipped(self) -> None:
        run = make_scheduled_run(user=self.user, due_in=timedelta(hours=3))

        self.assertEqual(claim_run_retry(run_id=run.id, now=FROZEN_NOW), "skip")
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")

    def test_two_sweeps_claim_a_retry_once(self) -> None:
        run = make_scheduled_run(user=self.user)

        first = claim_run_retry(run_id=run.id, now=FROZEN_NOW)
        second = claim_run_retry(run_id=run.id, now=FROZEN_NOW)

        self.assertEqual((first, second), ("dispatch", "skip"))
        run.refresh_from_db()
        self.assertEqual(run.attempt, 2)

    def test_access_lost_since_the_failure_stops_the_chain_at_claim_time(self) -> None:
        run = make_scheduled_run(user=self.user)
        Subscription.objects.filter(user=self.user).update(access_expires_at=timezone.now() - timedelta(days=1))

        outcome = claim_run_retry(run_id=run.id, now=FROZEN_NOW)

        run.refresh_from_db()
        self.assertEqual(outcome, "stopped")
        self.assertEqual((run.status, run.retry_state, run.attempt), ("failed", "stopped", 1))

    def test_a_chain_past_its_period_is_exhausted_not_dispatched(self) -> None:
        run = make_scheduled_run(user=self.user, started_days_ago=31)

        outcome = claim_run_retry(run_id=run.id, now=FROZEN_NOW)

        run.refresh_from_db()
        self.assertEqual(outcome, "exhausted")
        self.assertEqual((run.status, run.retry_state), ("failed", "exhausted"))

    def test_unclaim_restores_the_attempt_and_reschedules(self) -> None:
        run = make_scheduled_run(user=self.user, attempt=2)
        claim_run_retry(run_id=run.id, now=FROZEN_NOW)

        unclaim_run_retry(run_id=run.id, retry_at=FROZEN_NOW + timedelta(minutes=15))

        run.refresh_from_db()
        self.assertEqual((run.status, run.retry_state, run.attempt), ("failed", "scheduled", 2))
        self.assertEqual(run.next_retry_at, FROZEN_NOW + timedelta(minutes=15))


class InFlightClockTests(FrozenClockTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="clock", password="pw12345!")
        active_subscription_for(self.user)

    def test_a_retry_running_days_into_the_chain_is_in_flight_and_is_not_reaped(self) -> None:
        run = make_run(user=self.user, status="running", started_days_ago=3, attempt=3)
        RecommendationRun.objects.filter(pk=run.pk).update(attempt_started_at=FROZEN_NOW)

        self.assertTrue(in_flight_runs(user=self.user, now=FROZEN_NOW).exists())
        with patch("fragrance.services.timezone.now", return_value=FROZEN_NOW):
            reaped = reap_stale_runs()
        self.assertEqual(reaped, 0)
        run.refresh_from_db()
        self.assertEqual(run.status, "running")

    def test_a_legacy_row_without_attempt_started_at_falls_back_to_triggered_at(self) -> None:
        make_run(user=self.user, status="running", started_days_ago=3)

        self.assertFalse(in_flight_runs(user=self.user, now=FROZEN_NOW).exists())

    def test_a_stale_attempt_is_reaped_into_the_retry_chain(self) -> None:
        run = make_run(user=self.user, status="running", started_days_ago=3, attempt=2)
        RecommendationRun.objects.filter(pk=run.pk).update(attempt_started_at=FROZEN_NOW - timedelta(hours=3))

        with (
            patch("django.utils.timezone.now", return_value=FROZEN_NOW),
            patch("fragrance.tasks.send_run_failure_notice.delay"),
        ):
            self.assertEqual(reap_stale_runs(), 1)

        run.refresh_from_db()
        self.assertEqual((run.status, run.retry_state), ("failed", "scheduled"))

    def test_start_recommendation_run_stamps_the_attempt_clock(self) -> None:
        run = start_recommendation_run(user=self.user, source="api")

        self.assertIsNotNone(run.attempt_started_at)
        self.assertEqual(run.attempt, 1)

    def test_a_failed_run_with_a_scheduled_retry_is_in_flight(self) -> None:
        make_scheduled_run(user=self.user)

        self.assertTrue(in_flight_runs(user=self.user, now=FROZEN_NOW).exists())

    def test_exhausted_stopped_and_legacy_failed_runs_are_not_in_flight(self) -> None:
        for retry_state in ("exhausted", "stopped", ""):
            make_run(user=self.user, status="failed", retry_state=retry_state)

        self.assertFalse(in_flight_runs(user=self.user, now=FROZEN_NOW).exists())

    def test_another_users_scheduled_retry_is_not_in_flight_for_me(self) -> None:
        other = User.objects.create_user(username="clock_other", password="pw12345!")
        make_scheduled_run(user=other)

        self.assertFalse(in_flight_runs(user=self.user, now=FROZEN_NOW).exists())


class ChainGateTests(FrozenClockTestCase):
    """Batch 1's constraint: a scheduled retry is in flight; an ended chain means 'no done run yet'."""

    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="gate", password="pw12345!")
        active_subscription_for(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _trigger(self):
        with patch("fragrance.tasks.monthly_fragrance_run.delay") as delay:
            delay.return_value = SimpleNamespace(id="task-gate")
            return self.client.post(reverse("run-trigger")), delay

    def _first_run_available(self) -> bool:
        return self.client.get(reverse("billing-subscription")).json()["quota"]["first_run_available"]

    def test_a_scheduled_retry_blocks_the_first_run_button_and_the_trigger(self) -> None:
        make_run(user=self.user, status="failed", retry_state="scheduled",
                 next_retry_at=FROZEN_NOW + timedelta(days=30), last_failed_at=FROZEN_NOW)

        self.assertFalse(self._first_run_available())
        response, delay = self._trigger()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "run_in_progress")
        delay.assert_not_called()

    def test_an_exhausted_chain_brings_the_first_run_button_back(self) -> None:
        make_run(user=self.user, status="failed", retry_state="exhausted", last_failed_at=FROZEN_NOW)

        self.assertTrue(self._first_run_available())
        response, delay = self._trigger()
        self.assertEqual(response.status_code, 202)
        delay.assert_called_once()

    def test_a_stopped_chain_brings_the_first_run_button_back_once_access_is_active(self) -> None:
        make_run(user=self.user, status="failed", retry_state="stopped", last_failed_at=FROZEN_NOW)

        self.assertTrue(self._first_run_available())

    def test_a_failed_chain_does_not_consume_the_periods_run(self) -> None:
        make_run(user=self.user, status="failed", retry_state="exhausted", last_failed_at=FROZEN_NOW)

        snapshot = quota_snapshot(user=self.user, subscription=self.user.subscription)

        self.assertEqual(snapshot["runs_used"], 0)
        self.assertTrue(snapshot["can_trigger"])

    def test_the_dispatch_cap_counts_chains_one_exhausted_chain_earns_a_fresh_one_two_do_not(self) -> None:
        make_run(user=self.user, status="failed", retry_state="exhausted", started_days_ago=2)
        start_recommendation_run(user=self.user, source="api")
        RecommendationRun.objects.filter(user=self.user, status="pending").update(
            status="failed", retry_state="exhausted"
        )
        with self.assertRaises(QuotaExceeded):
            start_recommendation_run(user=self.user, source="api")

    def test_beat_is_refused_while_a_chain_is_mid_retry(self) -> None:
        make_scheduled_run(user=self.user)
        from fragrance.services import RunInProgress

        with self.assertRaises(RunInProgress):
            start_recommendation_run(user=self.user, source="beat")
