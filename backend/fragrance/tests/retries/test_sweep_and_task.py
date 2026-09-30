"""The beat sweep that dispatches due retries, and monthly_fragrance_run's part in the chain."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from celery.exceptions import Retry
from django.contrib.auth.models import User
from django.utils import timezone
from fragrance.ai_providers import registry
from fragrance.models import AIModelConfig, FragranceConfig, RecommendationRun, Subscription
from fragrance.services import MAX_RUN_RETRIES
from fragrance.tasks import monthly_fragrance_run, retry_failed_runs
from fragrance.tests.retries.helpers import FrozenClockTestCase, FROZEN_NOW, make_run, make_scheduled_run
from fragrance.tests.support.factories import active_subscription_for
from kombu.exceptions import OperationalError


class RetryFailedRunsTests(FrozenClockTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="sweep", password="pw12345!")
        active_subscription_for(self.user)

    def _sweep(self):
        with (
            patch("fragrance.tasks.timezone.now", return_value=FROZEN_NOW),
            patch("fragrance.tasks.monthly_fragrance_run.delay") as delay,
            patch("fragrance.tasks.send_run_failure_notice.delay") as notice,
            patch("fragrance.tasks.send_operator_run_exhausted_alert.delay") as alert,
        ):
            delay.return_value = SimpleNamespace(id="task-retry")
            dispatched = retry_failed_runs()
        return dispatched, delay, notice, alert

    def test_a_due_retry_is_dispatched_to_the_same_run_with_no_eta_or_countdown(self) -> None:
        run = make_scheduled_run(user=self.user)

        dispatched, delay, _, _ = self._sweep()

        self.assertEqual(dispatched, 1)
        delay.assert_called_once_with(user_id=self.user.id, run_id=run.id)
        run.refresh_from_db()
        self.assertEqual((run.status, run.attempt, run.celery_task_id), ("pending", 2, "task-retry"))

    def test_only_the_database_decides_what_is_due_so_a_fresh_process_finds_it(self) -> None:
        due = make_scheduled_run(user=self.user)
        other = User.objects.create_user(username="sweep2", password="pw12345!")
        active_subscription_for(other)
        later = make_scheduled_run(user=other, due_in=timedelta(hours=5))

        dispatched, delay, _, _ = self._sweep()

        self.assertEqual(dispatched, 1)
        delay.assert_called_once_with(user_id=self.user.id, run_id=due.id)
        later.refresh_from_db()
        self.assertEqual(later.status, "failed")

    def test_running_the_sweep_twice_dispatches_once(self) -> None:
        make_scheduled_run(user=self.user)

        first, _, _, _ = self._sweep()
        second, delay, _, _ = self._sweep()

        self.assertEqual((first, second), (1, 0))
        delay.assert_not_called()

    def test_a_lapsed_account_is_stopped_not_dispatched_and_not_emailed(self) -> None:
        run = make_scheduled_run(user=self.user)
        Subscription.objects.filter(user=self.user).update(access_expires_at=timezone.now() - timedelta(days=1))

        dispatched, delay, notice, alert = self._sweep()

        self.assertEqual(dispatched, 0)
        delay.assert_not_called()
        notice.assert_not_called()
        alert.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.retry_state, "stopped")

    def test_a_chain_that_ran_out_of_period_is_exhausted_and_notified(self) -> None:
        run = make_scheduled_run(user=self.user, started_days_ago=31)

        dispatched, delay, notice, alert = self._sweep()

        self.assertEqual(dispatched, 0)
        delay.assert_not_called()
        notice.assert_called_once_with(run_id=run.id, kind="exhausted")
        alert.assert_called_once_with(run_id=run.id)

    def test_a_broker_outage_releases_the_claim_without_spending_the_attempt(self) -> None:
        run = make_scheduled_run(user=self.user, attempt=2)
        with (
            patch("fragrance.tasks.timezone.now", return_value=FROZEN_NOW),
            patch("fragrance.tasks.monthly_fragrance_run.delay", side_effect=OperationalError("down")),
        ):
            self.assertEqual(retry_failed_runs(), 0)

        run.refresh_from_db()
        self.assertEqual((run.status, run.retry_state, run.attempt), ("failed", "scheduled", 2))
        self.assertEqual(run.next_retry_at, FROZEN_NOW + timedelta(minutes=15))


class MonthlyRunChainTests(FrozenClockTestCase):
    def setUp(self) -> None:
        super().setUp()
        AIModelConfig.objects.all().delete()
        self.user = User.objects.create_user(username="taskchain", password="pw12345!")
        active_subscription_for(self.user)
        FragranceConfig.objects.create(user=self.user, recipient_email="dave@example.com")

    def _run_failing(self, *, run: RecommendationRun):
        with (
            patch("fragrance.tasks.send_run_failure_notice.delay") as notice,
            patch("fragrance.tasks.send_operator_run_exhausted_alert.delay") as alert,
        ):
            with self.assertRaises(registry.NoActiveAIModelConfigError):
                monthly_fragrance_run(user_id=self.user.id, run_id=run.id)
        return notice, alert

    def test_a_failed_first_attempt_schedules_a_retry_and_queues_the_retrying_notice(self) -> None:
        run = make_run(user=self.user, status="pending")

        notice, alert = self._run_failing(run=run)

        run.refresh_from_db()
        self.assertEqual((run.status, run.retry_state, run.attempt), ("failed", "scheduled", 1))
        self.assertIsNotNone(run.next_retry_at)
        self.assertIn("No active AIModelConfig", run.error_message)
        notice.assert_called_once_with(run_id=run.id, kind="retrying")
        alert.assert_not_called()

    def test_a_failed_final_attempt_exhausts_the_chain_and_alerts_the_operator(self) -> None:
        run = make_run(user=self.user, status="pending", attempt=MAX_RUN_RETRIES + 1)

        notice, alert = self._run_failing(run=run)

        run.refresh_from_db()
        self.assertEqual(run.retry_state, "exhausted")
        notice.assert_called_once_with(run_id=run.id, kind="exhausted")
        alert.assert_called_once_with(run_id=run.id)

    def test_a_soft_time_limit_failure_also_enters_the_chain(self) -> None:
        from celery.exceptions import SoftTimeLimitExceeded

        run = make_run(user=self.user, status="pending")
        with (
            patch("fragrance.tasks.registry.get_active_config", side_effect=SoftTimeLimitExceeded()),
            patch("fragrance.tasks.send_run_failure_notice.delay"),
        ):
            monthly_fragrance_run(user_id=self.user.id, run_id=run.id)

        run.refresh_from_db()
        self.assertEqual((run.status, run.retry_state), ("failed", "scheduled"))

    def test_the_internal_celery_retry_does_not_touch_the_chain(self) -> None:
        run = make_run(user=self.user, status="pending")
        with (
            patch("fragrance.tasks.registry.get_active_config", side_effect=RuntimeError("transient")),
            patch.object(monthly_fragrance_run, "retry", side_effect=Retry()),
            patch("fragrance.tasks.send_run_failure_notice.delay") as notice,
        ):
            with self.assertRaises(Retry):
                monthly_fragrance_run(user_id=self.user.id, run_id=run.id)

        run.refresh_from_db()
        self.assertEqual((run.status, run.retry_state), ("running", ""))
        notice.assert_not_called()

    def test_a_late_task_for_a_run_that_left_flight_changes_nothing(self) -> None:
        run = make_scheduled_run(user=self.user)

        monthly_fragrance_run(user_id=self.user.id, run_id=run.id)

        run.refresh_from_db()
        self.assertEqual((run.status, run.retry_state, run.error_message), ("failed", "scheduled", "boom"))
