"""A scheduled retry the sweep never picks up must stop blocking the user and end through the normal chain rules."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from fragrance.models import RecommendationRun, Subscription
from fragrance.services import (
    RETRY_OVERDUE_GRACE,
    claim_run_retry,
    expire_overdue_retry,
    in_flight_runs,
    quota_snapshot,
    start_recommendation_run,
)
from fragrance.tasks import reap_stale_runs
from fragrance.tests.retries.helpers import FrozenClockTestCase, FROZEN_NOW, make_scheduled_run
from fragrance.tests.support.factories import active_subscription_for
from rest_framework.test import APIClient

JUST_WITHIN = -RETRY_OVERDUE_GRACE + timedelta(minutes=1)
JUST_OVERDUE = -RETRY_OVERDUE_GRACE - timedelta(minutes=1)


class OverdueRetryTestCase(FrozenClockTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="overdue", password="pw12345!")
        active_subscription_for(self.user)


class InFlightBoundTests(OverdueRetryTestCase):
    def test_a_retry_overdue_within_the_grace_still_blocks(self) -> None:
        make_scheduled_run(user=self.user, due_in=JUST_WITHIN)

        self.assertTrue(in_flight_runs(user=self.user, now=FROZEN_NOW).exists())

    def test_a_retry_overdue_past_the_grace_no_longer_blocks(self) -> None:
        make_scheduled_run(user=self.user, due_in=JUST_OVERDUE)

        self.assertFalse(in_flight_runs(user=self.user, now=FROZEN_NOW).exists())

    def test_the_first_run_button_and_the_trigger_come_back_together(self) -> None:
        make_scheduled_run(user=self.user, due_in=JUST_WITHIN)
        client = APIClient()
        client.force_authenticate(user=self.user)
        snapshot = quota_snapshot(user=self.user, subscription=Subscription.objects.get(user=self.user))
        self.assertFalse(snapshot["first_run_available"])
        self.assertEqual(client.post(reverse("run-trigger")).status_code, 429)

        RecommendationRun.objects.update(next_retry_at=FROZEN_NOW + JUST_OVERDUE)

        snapshot = quota_snapshot(user=self.user, subscription=Subscription.objects.get(user=self.user))
        self.assertTrue(snapshot["first_run_available"])
        self.assertTrue(snapshot["can_trigger"])
        with patch("fragrance.tasks.monthly_fragrance_run.delay") as delay:
            delay.return_value.id = "t"
            self.assertEqual(client.post(reverse("run-trigger")).status_code, 202)

    def test_start_recommendation_run_is_not_refused_by_an_overdue_retry(self) -> None:
        make_scheduled_run(user=self.user, due_in=JUST_OVERDUE, started_days_ago=0)

        self.assertEqual(start_recommendation_run(user=self.user, source="api").status, "pending")


class ExpireOverdueRetryTests(OverdueRetryTestCase):
    def test_an_overdue_retry_is_exhausted(self) -> None:
        run = make_scheduled_run(user=self.user, due_in=JUST_OVERDUE)

        self.assertEqual(expire_overdue_retry(run_id=run.id, now=FROZEN_NOW), "exhausted")

        run.refresh_from_db()
        self.assertEqual((run.status, run.retry_state, run.next_retry_at), ("failed", "exhausted", None))

    def test_a_lapsed_account_is_stopped_instead(self) -> None:
        Subscription.objects.filter(user=self.user).update(status="none")
        run = make_scheduled_run(user=self.user, due_in=JUST_OVERDUE)

        self.assertEqual(expire_overdue_retry(run_id=run.id, now=FROZEN_NOW), "stopped")

        run.refresh_from_db()
        self.assertEqual(run.retry_state, "stopped")

    def test_a_retry_within_the_grace_or_not_scheduled_is_left_alone(self) -> None:
        within = make_scheduled_run(user=self.user, due_in=JUST_WITHIN)
        self.assertEqual(expire_overdue_retry(run_id=within.id, now=FROZEN_NOW), "skip")
        within.refresh_from_db()
        self.assertEqual(within.retry_state, "scheduled")
        self.assertEqual(expire_overdue_retry(run_id=0, now=FROZEN_NOW), "skip")

    def test_claim_run_retry_exhausts_an_overdue_retry_instead_of_dispatching_it(self) -> None:
        run = make_scheduled_run(user=self.user, due_in=JUST_OVERDUE)

        self.assertEqual(claim_run_retry(run_id=run.id, now=FROZEN_NOW), "exhausted")

        run.refresh_from_db()
        self.assertEqual((run.status, run.retry_state, run.attempt), ("failed", "exhausted", 1))

    def test_claim_run_retry_still_dispatches_a_retry_within_the_grace(self) -> None:
        run = make_scheduled_run(user=self.user, due_in=JUST_WITHIN)

        self.assertEqual(claim_run_retry(run_id=run.id, now=FROZEN_NOW), "dispatch")


class ReaperOverdueTests(OverdueRetryTestCase):
    def _reap(self):
        with (
            patch("fragrance.tasks.timezone.now", return_value=FROZEN_NOW),
            patch("fragrance.tasks.send_run_failure_notice.delay") as notice,
            patch("fragrance.tasks.send_operator_run_exhausted_alert.delay") as alert,
        ):
            reaped = reap_stale_runs()
        return reaped, notice, alert

    def test_the_reaper_exhausts_an_overdue_chain_and_sends_the_exhausted_notices(self) -> None:
        run = make_scheduled_run(user=self.user, due_in=JUST_OVERDUE)

        reaped, notice, alert = self._reap()

        self.assertEqual(reaped, 1)
        run.refresh_from_db()
        self.assertEqual(run.retry_state, "exhausted")
        notice.assert_called_once_with(run_id=run.id, kind="exhausted")
        alert.assert_called_once_with(run_id=run.id)

    def test_a_stopped_overdue_chain_sends_nothing(self) -> None:
        Subscription.objects.filter(user=self.user).update(status="none")
        run = make_scheduled_run(user=self.user, due_in=JUST_OVERDUE)

        reaped, notice, alert = self._reap()

        self.assertEqual(reaped, 1)
        run.refresh_from_db()
        self.assertEqual(run.retry_state, "stopped")
        notice.assert_not_called()
        alert.assert_not_called()

    def test_the_reaper_leaves_a_retry_within_the_grace_and_other_users_alone(self) -> None:
        within = make_scheduled_run(user=self.user, due_in=JUST_WITHIN)

        reaped, notice, _ = self._reap()

        self.assertEqual(reaped, 0)
        within.refresh_from_db()
        self.assertEqual(within.retry_state, "scheduled")
        notice.assert_not_called()
