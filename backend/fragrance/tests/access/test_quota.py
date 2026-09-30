import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from fragrance.models import (
    AIModelConfig,
    FragranceConfig,
    RecommendationRun,
    Subscription,
)
from fragrance.services import (
    BEAT_QUOTA_TOLERANCE,
    QuotaExceeded,
    RunInProgress,
    quota_snapshot,
    start_recommendation_run,
)
from fragrance.tasks import monthly_fragrance_run
from fragrance.tests.support.factories import (
    _backdate_runs,
    _make_plan,
    active_subscription_for,
)


class StartRecommendationRunTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="runner", password="pw12345!"
        )
        active_subscription_for(self.user)

    def _finish_latest(self, *, run_status: str) -> RecommendationRun:
        run = RecommendationRun.objects.filter(user=self.user).latest(
            "triggered_at"
        )
        run.status = run_status
        run.save(update_fields=["status"])
        return run

    def test_creates_pending_run(self) -> None:
        run = start_recommendation_run(user=self.user, source="test")
        self.assertEqual(run.status, "pending")
        self.assertEqual(run.user, self.user)

    def test_missing_subscription_row_is_created_at_the_chokepoint(
        self,
    ) -> None:
        staff = User.objects.create_user(
            username="ops", password="pw12345!", is_staff=True
        )
        self.assertFalse(Subscription.objects.filter(user=staff).exists())
        start_recommendation_run(user=staff, source="test")
        self.assertTrue(Subscription.objects.filter(user=staff).exists())

    def test_no_subscription_raises_subscription_required(self) -> None:
        from fragrance.permissions import SubscriptionRequired

        nobody = User.objects.create_user(
            username="nobody", password="pw12345!"
        )
        with self.assertRaises(SubscriptionRequired):
            start_recommendation_run(user=nobody, source="test")
        self.assertFalse(RecommendationRun.objects.filter(user=nobody).exists())

    def test_in_flight_run_blocks(self) -> None:
        start_recommendation_run(user=self.user, source="test")
        with self.assertRaises(RunInProgress):
            start_recommendation_run(user=self.user, source="test")

    def test_second_run_in_window_raises_with_next_eligible_at(self) -> None:
        first = start_recommendation_run(user=self.user, source="test")
        self._finish_latest(run_status="done")
        with self.assertRaises(QuotaExceeded) as ctx:
            start_recommendation_run(user=self.user, source="test")
        first.refresh_from_db()
        self.assertEqual(
            ctx.exception.next_eligible_at,
            first.triggered_at + timedelta(days=30),
        )

    def test_run_allowed_again_after_window(self) -> None:
        start_recommendation_run(user=self.user, source="test")
        self._finish_latest(run_status="done")
        _backdate_runs(user=self.user, days_ago=31)
        self.assertEqual(
            start_recommendation_run(user=self.user, source="test").status,
            "pending",
        )

    def test_one_failed_run_earns_one_retry_two_do_not(self) -> None:
        start_recommendation_run(user=self.user, source="test")
        self._finish_latest(run_status="failed")
        start_recommendation_run(
            user=self.user, source="test"
        )  # forgiven retry
        self._finish_latest(run_status="failed")
        with self.assertRaises(QuotaExceeded):
            start_recommendation_run(user=self.user, source="test")

    def test_successful_retry_after_failure_then_blocked(self) -> None:
        start_recommendation_run(user=self.user, source="test")
        self._finish_latest(run_status="failed")
        start_recommendation_run(user=self.user, source="test")
        self._finish_latest(run_status="done")
        with self.assertRaises(QuotaExceeded):
            start_recommendation_run(user=self.user, source="test")

    def test_annual_plan_gets_one_run_per_30_days_not_twelve(self) -> None:
        annual = _make_plan(slug="annual-q", interval="year")
        active_subscription_for(self.user, plan=annual, days=365)
        start_recommendation_run(user=self.user, source="test")
        self._finish_latest(run_status="done")
        with self.assertRaises(QuotaExceeded):
            start_recommendation_run(user=self.user, source="test")
        _backdate_runs(user=self.user, days_ago=31)
        start_recommendation_run(user=self.user, source="test")

    def test_larger_allowance_next_eligible_uses_correct_run(self) -> None:
        plan = _make_plan(slug="three-q", interval="month")
        plan.max_runs_per_quota_period = 3
        plan.save()
        active_subscription_for(self.user, plan=plan)
        stamps = []
        for days_ago in (20, 10, 1):
            run = RecommendationRun.objects.create(
                user=self.user, status="done"
            )
            RecommendationRun.objects.filter(pk=run.pk).update(
                triggered_at=timezone.now() - timedelta(days=days_ago)
            )
            run.refresh_from_db()
            stamps.append(run.triggered_at)
        with self.assertRaises(QuotaExceeded) as ctx:
            start_recommendation_run(user=self.user, source="test")
        # Only the oldest must leave the window to get back under 3.
        self.assertEqual(
            ctx.exception.next_eligible_at, stamps[0] + timedelta(days=30)
        )

    def test_staff_bypass_quota_but_not_in_flight_guard(self) -> None:
        staff = User.objects.create_user(
            username="ops", password="pw12345!", is_staff=True
        )
        start_recommendation_run(user=staff, source="test")
        RecommendationRun.objects.filter(user=staff).update(status="done")
        start_recommendation_run(user=staff, source="test")  # quota bypassed
        with self.assertRaises(RunInProgress):
            start_recommendation_run(user=staff, source="test")

    def test_other_users_runs_do_not_count(self) -> None:
        other = User.objects.create_user(username="other", password="pw12345!")
        active_subscription_for(other)
        start_recommendation_run(user=other, source="test")
        self.assertEqual(
            start_recommendation_run(user=self.user, source="test").status,
            "pending",
        )


class StartRecommendationRunConcurrencyTests(TransactionTestCase):
    def test_two_concurrent_triggers_produce_exactly_one_run(self) -> None:
        user = User.objects.create_user(username="racer", password="pw12345!")
        active_subscription_for(user)
        outcomes: list[str] = []
        outcomes_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def _trigger() -> None:
            from django.db import connection

            try:
                barrier.wait()
                start_recommendation_run(user=user, source="test")
                result = "ok"
            except (QuotaExceeded, RunInProgress) as exc:
                result = type(exc).__name__
            finally:
                connection.close()
            with outcomes_lock:
                outcomes.append(result)

        threads = [threading.Thread(target=_trigger) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(RecommendationRun.objects.filter(user=user).count(), 1)
        self.assertEqual(sorted(outcomes), ["RunInProgress", "ok"])

    def test_two_concurrent_triggers_with_no_subscription_row_still_one_run(
        self,
    ) -> None:
        staff = User.objects.create_user(
            username="racer_ops", password="pw12345!", is_staff=True
        )
        barrier = threading.Barrier(2)

        def _trigger() -> None:
            from django.db import connection

            try:
                barrier.wait()
                start_recommendation_run(user=staff, source="test")
            except RunInProgress:
                pass
            finally:
                connection.close()

        threads = [threading.Thread(target=_trigger) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(
            RecommendationRun.objects.filter(user=staff).count(), 1
        )
        self.assertEqual(Subscription.objects.filter(user=staff).count(), 1)


class TriggerViewQuotaTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="trig", password="pw12345!"
        )
        active_subscription_for(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.url = reverse("run-trigger")

    def _post(self):
        with patch("fragrance.tasks.monthly_fragrance_run.delay") as delay:
            delay.return_value = SimpleNamespace(id="task-1")
            response = self.client.post(self.url)
        return response, delay

    def test_success_queues_the_created_run(self) -> None:
        response, delay = self._post()
        self.assertEqual(response.status_code, 202)
        run = RecommendationRun.objects.get(user=self.user)
        self.assertEqual(response.json(), {"run_id": run.id})
        delay.assert_called_once_with(user_id=self.user.id, run_id=run.id)
        self.assertEqual(run.celery_task_id, "task-1")

    def test_a_quota_block_without_a_done_run_is_429_quota_exceeded(self) -> None:
        # Two failed dispatches use up max_runs + failed_run_grace without a
        # "done" run, so the first-run gate lets the request reach the quota.
        self._post()
        RecommendationRun.objects.filter(user=self.user).update(status="failed")
        self._post()
        RecommendationRun.objects.filter(user=self.user).update(status="failed")
        response, delay = self._post()
        self.assertEqual(response.status_code, 429)
        body = response.json()
        self.assertEqual(body["code"], "quota_exceeded")
        self.assertIn("detail", body)
        first = RecommendationRun.objects.filter(user=self.user).earliest("triggered_at")
        expected = (
            (first.triggered_at + timedelta(days=30))
            .isoformat()
            .replace("+00:00", "Z")
        )
        self.assertEqual(body["next_eligible_at"], expected)
        delay.assert_not_called()

    def test_trigger_while_running_is_429_run_in_progress(self) -> None:
        self._post()
        response, _ = self._post()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(
            response.json(),
            {
                "code": "run_in_progress",
                "detail": "A run is already in progress.",
            },
        )

    def test_broker_outage_fails_the_run_instead_of_stranding_it_pending(
        self,
    ) -> None:
        from kombu.exceptions import OperationalError

        with patch(
            "fragrance.tasks.monthly_fragrance_run.delay",
            side_effect=OperationalError("down"),
        ):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            RecommendationRun.objects.get(user=self.user).status, "failed"
        )
        # A failed run neither burns the allowance nor blocks a retry.
        response, _ = self._post()
        self.assertEqual(response.status_code, 202)


class BeatPathQuotaTests(TestCase):
    def setUp(self) -> None:
        AIModelConfig.objects.all().delete()
        self.user = User.objects.create_user(
            username="beat", password="pw12345!"
        )
        FragranceConfig.objects.create(
            user=self.user, recipient_email="beat@example.com"
        )

    def test_lapsed_user_is_skipped_quietly(self) -> None:
        with self.assertLogs("fragrance.tasks", level="INFO") as logs:
            monthly_fragrance_run(user_id=self.user.id)
        self.assertFalse(
            RecommendationRun.objects.filter(user=self.user).exists()
        )
        self.assertTrue(any("skipped" in line for line in logs.output))

    def test_exhausted_quota_is_skipped_quietly(self) -> None:
        active_subscription_for(self.user)
        RecommendationRun.objects.create(user=self.user, status="done")
        monthly_fragrance_run(user_id=self.user.id)
        self.assertEqual(
            RecommendationRun.objects.filter(user=self.user).count(), 1
        )

    def test_run_in_progress_is_skipped_quietly(self) -> None:
        active_subscription_for(self.user)
        RecommendationRun.objects.create(user=self.user, status="running")
        monthly_fragrance_run(user_id=self.user.id)
        self.assertEqual(
            RecommendationRun.objects.filter(user=self.user).count(), 1
        )

    def test_deleted_user_is_skipped_quietly(self) -> None:
        monthly_fragrance_run(user_id=999999)

    def test_beat_path_goes_through_chokepoint_and_marks_running(self) -> None:
        active_subscription_for(self.user)
        with patch(
            "fragrance.tasks.registry.get_active_config",
            side_effect=RuntimeError("stop"),
        ):
            monthly_fragrance_run.push_request(
                retries=monthly_fragrance_run.max_retries
            )
            try:
                with self.assertRaises(RuntimeError):
                    monthly_fragrance_run(user_id=self.user.id)
            finally:
                monthly_fragrance_run.pop_request()
        run = RecommendationRun.objects.get(user=self.user)
        self.assertEqual(run.status, "failed")

    def test_view_path_does_not_call_chokepoint_again(self) -> None:
        active_subscription_for(self.user)
        run = start_recommendation_run(user=self.user, source="test")
        with (
            patch("fragrance.tasks.start_recommendation_run") as chokepoint,
            patch(
                "fragrance.tasks.registry.get_active_config",
                side_effect=RuntimeError("stop"),
            ),
        ):
            monthly_fragrance_run.push_request(
                retries=monthly_fragrance_run.max_retries
            )
            try:
                with self.assertRaises(RuntimeError):
                    monthly_fragrance_run(user_id=self.user.id, run_id=run.id)
            finally:
                monthly_fragrance_run.pop_request()
        chokepoint.assert_not_called()
        self.assertEqual(
            RecommendationRun.objects.filter(user=self.user).count(), 1
        )

    def test_28_day_transition_beat_run_is_not_silently_skipped(self) -> None:
        """
        Finding 1: a calendar-monthly beat schedule and a strict 30-day
        rolling quota window disagree on a 28-day month (Feb -> Mar). Without
        the beat tolerance this run never starts and monthly_fragrance_run
        just logs "skipped" -- reproduced here by asserting the pipeline is
        actually reached (it fails deep inside, on the mocked config lookup,
        proving the quota chokepoint let it through).
        """
        active_subscription_for(user=self.user)
        prior = RecommendationRun.objects.create(user=self.user, status="done")
        RecommendationRun.objects.filter(pk=prior.pk).update(
            triggered_at=timezone.now() - timedelta(days=28)
        )
        with patch(
            "fragrance.tasks.registry.get_active_config",
            side_effect=RuntimeError("stop"),
        ):
            monthly_fragrance_run.push_request(
                retries=monthly_fragrance_run.max_retries
            )
            try:
                with self.assertRaises(RuntimeError):
                    monthly_fragrance_run(user_id=self.user.id)
            finally:
                monthly_fragrance_run.pop_request()
        self.assertEqual(
            RecommendationRun.objects.filter(user=self.user).count(), 2
        )

    def test_retry_reuses_the_beat_created_run(self) -> None:
        active_subscription_for(self.user)
        with (
            patch(
                "fragrance.tasks.registry.get_active_config",
                side_effect=RuntimeError("boom"),
            ),
            patch.object(
                monthly_fragrance_run,
                "retry",
                side_effect=RuntimeError("retried"),
            ) as mock_retry,
        ):
            with self.assertRaises(RuntimeError):
                monthly_fragrance_run(user_id=self.user.id)
        run = RecommendationRun.objects.get(user=self.user)
        self.assertEqual(
            mock_retry.call_args.kwargs["kwargs"],
            {"user_id": self.user.id, "run_id": run.id},
        )


class BeatQuotaToleranceTests(TestCase):
    """
    Finding 1: services._evaluate_quota shrinks the rolling window by
    BEAT_QUOTA_TOLERANCE (4 days) for source="beat" only, so a calendar-
    monthly scheduled run is never refused just because the previous run
    hasn't quite aged out of a strict 30-day window in a 28-, 29- or 30-day
    month (or on sub-second timing at the 30-day boundary). Manual/API
    triggers, and the frontend-facing quota_snapshot, keep the strict window.
    """

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="tol", password="pw12345!"
        )
        active_subscription_for(user=self.user)

    def _run_and_backdate(
        self, *, days_ago: float | None = None, source: str = "beat"
    ) -> RecommendationRun:
        run = start_recommendation_run(user=self.user, source=source)
        run.status = "done"
        run.save(update_fields=["status"])
        if days_ago is not None:
            RecommendationRun.objects.filter(pk=run.pk).update(
                triggered_at=timezone.now() - timedelta(days=days_ago)
            )
        run.refresh_from_db()
        return run

    def test_beat_trigger_28_days_later_succeeds(self) -> None:
        self._run_and_backdate(days_ago=28)
        run = start_recommendation_run(user=self.user, source="beat")
        self.assertEqual(run.status, "pending")

    def test_beat_trigger_29_days_later_succeeds(self) -> None:
        self._run_and_backdate(days_ago=29)
        run = start_recommendation_run(user=self.user, source="beat")
        self.assertEqual(run.status, "pending")

    def test_beat_trigger_30_days_later_succeeds(self) -> None:
        self._run_and_backdate(days_ago=30)
        run = start_recommendation_run(user=self.user, source="beat")
        self.assertEqual(run.status, "pending")

    def test_beat_trigger_31_days_later_succeeds(self) -> None:
        self._run_and_backdate(days_ago=31)
        run = start_recommendation_run(user=self.user, source="beat")
        self.assertEqual(run.status, "pending")

    def test_beat_trigger_sub_second_after_30_days_still_succeeds(self) -> None:
        """
        The reported coin-flip: a previous row created a fraction of a
        second later in its hour than the current attempt lands just under
        (or over) a strict 30-day cutoff. BEAT_QUOTA_TOLERANCE (4 days) is
        far larger than any queue/scheduling jitter, so this must always
        succeed regardless of which side of "exactly 30 days" it lands on.
        """
        run = self._run_and_backdate(days_ago=None)
        RecommendationRun.objects.filter(pk=run.pk).update(
            triggered_at=timezone.now()
            - timedelta(days=30)
            + timedelta(milliseconds=200)
        )
        result = start_recommendation_run(user=self.user, source="beat")
        self.assertEqual(result.status, "pending")

    def test_run_just_inside_the_beat_tolerance_boundary_still_blocks(
        self,
    ) -> None:
        """Pins the exact boundary: quota_period_days (30) - BEAT_QUOTA_TOLERANCE (4) = 26 days."""
        effective_window = timedelta(days=30) - BEAT_QUOTA_TOLERANCE
        run = self._run_and_backdate(days_ago=None)
        RecommendationRun.objects.filter(pk=run.pk).update(
            triggered_at=timezone.now() - effective_window + timedelta(hours=1)
        )
        with self.assertRaises(QuotaExceeded):
            start_recommendation_run(user=self.user, source="beat")

    def test_run_just_outside_the_beat_tolerance_boundary_succeeds(
        self,
    ) -> None:
        effective_window = timedelta(days=30) - BEAT_QUOTA_TOLERANCE
        run = self._run_and_backdate(days_ago=None)
        RecommendationRun.objects.filter(pk=run.pk).update(
            triggered_at=timezone.now() - effective_window - timedelta(hours=1)
        )
        result = start_recommendation_run(user=self.user, source="beat")
        self.assertEqual(result.status, "pending")

    def test_beat_trigger_well_inside_shortened_window_is_still_blocked(
        self,
    ) -> None:
        """Tolerance shifts the boundary; it does not remove the quota."""
        self._run_and_backdate(days_ago=10)
        with self.assertRaises(QuotaExceeded):
            start_recommendation_run(user=self.user, source="beat")

    def test_manual_trigger_28_days_later_is_still_refused(self) -> None:
        self._run_and_backdate(days_ago=28, source="api")
        with self.assertRaises(QuotaExceeded):
            start_recommendation_run(user=self.user, source="api")

    def test_manual_trigger_31_days_later_still_succeeds(self) -> None:
        self._run_and_backdate(days_ago=31, source="api")
        run = start_recommendation_run(user=self.user, source="api")
        self.assertEqual(run.status, "pending")

    def test_quota_snapshot_stays_strict_at_28_days(self) -> None:
        """
        quota_snapshot deliberately does not know about the beat tolerance
        (see its docstring): it answers "would a manual trigger work right
        now", which must match start_recommendation_run(source="api").
        """
        self._run_and_backdate(days_ago=28)
        snapshot = quota_snapshot(
            user=self.user, subscription=self.user.subscription
        )
        self.assertFalse(snapshot["can_trigger"])

    def test_beat_trigger_still_respects_failed_run_grace(self) -> None:
        """
        The tolerance shortens the window; it must not also loosen the
        failed-run grace (max_runs + grace dispatched runs still blocks,
        even well inside the shortened window).
        """
        first = start_recommendation_run(user=self.user, source="beat")
        first.status = "failed"
        first.save(update_fields=["status"])
        RecommendationRun.objects.filter(pk=first.pk).update(
            triggered_at=timezone.now() - timedelta(days=5)
        )
        second = start_recommendation_run(
            user=self.user, source="beat"
        )  # forgiven retry
        second.status = "failed"
        second.save(update_fields=["status"])
        RecommendationRun.objects.filter(pk=second.pk).update(
            triggered_at=timezone.now() - timedelta(days=5)
        )
        with self.assertRaises(QuotaExceeded):
            start_recommendation_run(user=self.user, source="beat")

    def test_annual_plan_beat_tolerance_still_reads_quota_period_days(
        self,
    ) -> None:
        """The beat tolerance is subtracted from quota_period_days, never billing_interval."""
        annual = _make_plan(slug="annual-tol", interval="year")
        active_subscription_for(user=self.user, plan=annual, days=365)
        self._run_and_backdate(days_ago=28)
        run = start_recommendation_run(user=self.user, source="beat")
        self.assertEqual(run.status, "pending")


class QuotaSnapshotPayloadTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="snap", password="pw12345!"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _quota(self) -> dict:
        return self.client.get(reverse("billing-subscription")).json()["quota"]

    def test_lapsed_user_has_zero_allowed(self) -> None:
        self.assertEqual(
            self._quota(),
            {
                "runs_allowed": 0,
                "runs_used": 0,
                "next_eligible_at": None,
                "can_trigger": False,
                "first_run_available": False,
                "next_run_at": None,
            },
        )

    def test_planless_active_subscription_uses_defaults(self) -> None:
        active_subscription_for(self.user)
        self.assertEqual(
            self._quota(),
            {
                "runs_allowed": 1,
                "runs_used": 0,
                "next_eligible_at": None,
                "can_trigger": True,
                "first_run_available": True,
                "next_run_at": None,
            },
        )

    def test_used_run_reports_next_eligible_at(self) -> None:
        active_subscription_for(self.user)
        run = RecommendationRun.objects.create(user=self.user, status="done")
        quota = self._quota()
        self.assertEqual(quota["runs_used"], 1)
        self.assertEqual(
            quota["next_eligible_at"],
            (run.triggered_at + timedelta(days=30))
            .isoformat()
            .replace("+00:00", "Z"),
        )

    def test_annual_plan_reports_one_run_allowed(self) -> None:
        annual = _make_plan(slug="annual-s", interval="year")
        active_subscription_for(self.user, plan=annual, days=365)
        self.assertEqual(self._quota()["runs_allowed"], 1)

    def test_staff_uses_defaults(self) -> None:
        self.user.is_staff = True
        self.user.save()
        body = self.client.get(reverse("billing-subscription")).json()
        self.assertTrue(body["exempt"])
        self.assertTrue(body["has_active_access"])
        self.assertIsNone(body["quota"]["runs_allowed"])
        self.assertIsNone(body["quota"]["next_eligible_at"])
        self.assertEqual(body["status"], "none")

    def test_non_exempt_user_reports_exempt_false(self) -> None:
        self.assertFalse(
            self.client.get(reverse("billing-subscription")).json()["exempt"]
        )


class CanTriggerQuotaPayloadTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="ct", password="pw12345!")
        active_subscription_for(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _quota(self) -> dict:
        return self.client.get(reverse("billing-subscription")).json()["quota"]

    def test_available_run(self) -> None:
        q = self._quota()
        self.assertTrue(q["can_trigger"])
        self.assertIsNone(q["next_eligible_at"])

    def test_lapsed_user(self) -> None:
        Subscription.objects.filter(user=self.user).update(status="none")
        q = self._quota()
        self.assertEqual((q["runs_allowed"], q["can_trigger"]), (0, False))
        self.assertIsNone(q["next_eligible_at"])

    def test_exhausted_quota(self) -> None:
        run = RecommendationRun.objects.create(user=self.user, status="done")
        q = self._quota()
        self.assertFalse(q["can_trigger"])
        self.assertIsNotNone(q["next_eligible_at"])

    def test_failure_grace_block_reports_next_eligible_at(self) -> None:
        for _ in range(2):
            RecommendationRun.objects.create(user=self.user, status="failed")
        oldest = (
            RecommendationRun.objects.filter(user=self.user)
            .order_by("triggered_at")
            .first()
        )
        q = self._quota()
        self.assertEqual(q["runs_used"], 0)
        self.assertFalse(q["can_trigger"])
        self.assertEqual(
            q["next_eligible_at"],
            (oldest.triggered_at + timedelta(days=30))
            .isoformat()
            .replace("+00:00", "Z"),
        )

    def test_in_flight_run_is_not_triggerable(self) -> None:
        RecommendationRun.objects.create(user=self.user, status="running")
        self.assertFalse(self._quota()["can_trigger"])

    def test_snapshot_writes_nothing(self) -> None:
        other = User.objects.create_user(username="ct2", password="pw12345!")
        client = APIClient()
        client.force_authenticate(user=other)
        client.get(reverse("billing-subscription"))
        self.assertFalse(RecommendationRun.objects.filter(user=other).exists())

    def test_can_trigger_agrees_with_chokepoint(self) -> None:
        for statuses in (
            [],
            ["done"],
            ["failed"],
            ["failed", "failed"],
            ["running"],
        ):
            RecommendationRun.objects.filter(user=self.user).delete()
            for st in statuses:
                RecommendationRun.objects.create(user=self.user, status=st)
            can = self._quota()["can_trigger"]
            try:
                start_recommendation_run(user=self.user, source="test")
                started = True
            except (QuotaExceeded, RunInProgress):
                started = False
            self.assertEqual(can, started, statuses)
