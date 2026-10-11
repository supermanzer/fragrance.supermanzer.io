"""
A run that finished but whose send_recommendation_email publish was lost is
requeued by reap_stale_runs, and the send is claimed so that a requeue, a
duplicate delivery or a Celery retry can never mail the user twice.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from fragrance.models import FragranceConfig, Recommendation, RecommendationRun
from fragrance.models import Subscription
from fragrance.services import (
    PENDING_EMAIL_REQUEUE_AGE,
    PENDING_EMAIL_REQUEUE_BATCH,
    PENDING_EMAIL_REQUEUE_MAX_AGE,
    STRANDED_CLAIM_AGE,
)
from kombu.exceptions import OperationalError
from fragrance.tasks import reap_stale_runs, send_recommendation_email
from fragrance.tests.support.factories import active_subscription_for
from rest_framework.test import APIClient

SETTINGS = dict(
    SUPPORT_EMAIL="support@example.test",
    DEFAULT_FROM_EMAIL="picks@example.test",
    FRONTEND_URL="https://app.example.test",
)


@override_settings(**SETTINGS)
class _Base(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", email="dave@example.test", password="hal-9000")
        FragranceConfig.objects.create(user=self.user, recipient_email="dave@example.test")
        active_subscription_for(self.user)

    def _run(self, *, minutes_ago: float = 120, **fields: object) -> RecommendationRun:
        values: dict = {"status": "done", "email_status": "pending", "intro": "Welcome back."}
        values.update(fields)
        run = RecommendationRun.objects.create(user=self.user, **values)
        RecommendationRun.objects.filter(pk=run.pk).update(
            triggered_at=timezone.now() - timedelta(minutes=minutes_ago)
        )
        Recommendation.objects.create(
            user=self.user, run=run, name="Aventus", house="Creed", status="confirmed", rationale="Smoky."
        )
        run.refresh_from_db()
        return run


class ReaperRequeueTests(_Base):
    def test_old_done_pending_unclaimed_run_is_requeued(self) -> None:
        run = self._run()
        with patch.object(send_recommendation_email, "delay") as delay:
            reap_stale_runs()

        delay.assert_called_once_with(user_id=self.user.id, run_id=run.id)

    def test_only_the_stuck_shape_is_requeued(self) -> None:
        fresh_minutes = PENDING_EMAIL_REQUEUE_AGE.total_seconds() / 60 - 5
        self._run(minutes_ago=fresh_minutes)
        self._run(email_status="sent", sent_at=timezone.now())
        self._run(email_status="failed")
        self._run(email_status=None)
        self._run(sent_at=timezone.now())  # claimed by a task that is sending right now
        self._run(status="failed")
        with patch.object(send_recommendation_email, "delay") as delay:
            reap_stale_runs()

        delay.assert_not_called()

    def test_publish_failure_does_not_abort_the_sweep(self) -> None:
        first = self._run()
        second = self._run()
        with patch.object(send_recommendation_email, "delay", side_effect=[OSError("broker down"), None]) as delay:
            with self.assertLogs("fragrance.tasks", level="ERROR"):
                reap_stale_runs()

        self.assertEqual(delay.call_count, 2)
        requeued = {call.kwargs["run_id"] for call in delay.call_args_list}
        self.assertEqual(requeued, {first.id, second.id})

    def test_requeued_run_is_mailed_exactly_once_even_if_the_original_is_also_delivered(self) -> None:
        run = self._run()
        with patch.object(send_recommendation_email, "delay") as delay:
            reap_stale_runs()
        kwargs = delay.call_args.kwargs

        send_recommendation_email(**kwargs)  # the requeued copy
        send_recommendation_email(**kwargs)  # the original, delivered late

        self.assertEqual(len(mail.outbox), 1)
        run.refresh_from_db()
        self.assertEqual(run.email_status, "sent")
        self.assertIsNotNone(run.sent_at)
        with patch.object(send_recommendation_email, "delay") as delay_again:
            reap_stale_runs()
        delay_again.assert_not_called()


class ClaimTests(_Base):
    def test_claimed_run_is_not_sent_again(self) -> None:
        run = self._run(sent_at=timezone.now())

        send_recommendation_email(user_id=self.user.id, run_id=run.id)

        self.assertEqual(len(mail.outbox), 0)

    def test_a_mismatched_user_id_sends_nothing_and_leaves_the_row_alone(self) -> None:
        other = User.objects.create_user(username="frank", email="frank@example.test", password="pw12345!")
        run = self._run()

        send_recommendation_email(user_id=other.id, run_id=run.id)

        self.assertEqual(len(mail.outbox), 0)
        run.refresh_from_db()
        self.assertEqual(run.email_status, "pending")
        self.assertIsNone(run.sent_at)

    def test_non_pending_runs_are_not_sent(self) -> None:
        for email_status in ("sent", "failed", None):
            with self.subTest(email_status=email_status):
                run = self._run(email_status=email_status)
                send_recommendation_email(user_id=self.user.id, run_id=run.id)
        self.assertEqual(len(mail.outbox), 0)

    def test_failed_send_releases_the_claim_so_the_retry_can_send(self) -> None:
        run = self._run()
        with patch("fragrance.tasks.EmailMessage.send", side_effect=OSError("smtp down")):
            with patch.object(send_recommendation_email, "retry", side_effect=RuntimeError("retry")):
                with self.assertRaises(RuntimeError):
                    send_recommendation_email(user_id=self.user.id, run_id=run.id)
        run.refresh_from_db()
        self.assertIsNone(run.sent_at)
        self.assertEqual(run.email_status, "pending")

        send_recommendation_email(user_id=self.user.id, run_id=run.id)

        self.assertEqual(len(mail.outbox), 1)
        run.refresh_from_db()
        self.assertEqual(run.email_status, "sent")

    def test_bookkeeping_failure_after_a_successful_send_does_not_cause_a_second_send(self) -> None:
        run = self._run()
        original_save = RecommendationRun.save

        def failing_save(instance: RecommendationRun, *args: object, **kwargs: object) -> None:
            if kwargs.get("update_fields") == ["sent_at", "email_status"]:
                raise RuntimeError("database went away")
            original_save(instance, *args, **kwargs)  # type: ignore[arg-type]

        with patch.object(RecommendationRun, "save", failing_save):
            with self.assertLogs("fragrance.tasks", level="ERROR"):
                send_recommendation_email(user_id=self.user.id, run_id=run.id)

        send_recommendation_email(user_id=self.user.id, run_id=run.id)
        self.assertEqual(len(mail.outbox), 1)
        run.refresh_from_db()
        self.assertIsNotNone(run.sent_at)

    def test_exhausted_retries_mark_failed_and_leave_the_claim_released(self) -> None:
        run = self._run()
        with patch("fragrance.tasks.EmailMessage.send", side_effect=OSError("smtp down")):
            task = send_recommendation_email
            task.push_request(retries=task.max_retries)
            try:
                with self.assertRaises(OSError):
                    task(user_id=self.user.id, run_id=run.id)
            finally:
                task.pop_request()
        run.refresh_from_db()
        self.assertEqual(run.email_status, "failed")
        self.assertIsNone(run.sent_at)


class ResendEmailViewTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_resend_of_a_failed_run_clears_any_stale_claim_and_queues(self) -> None:
        run = self._run(email_status="failed", sent_at=timezone.now())
        with patch.object(send_recommendation_email, "delay") as delay:
            response = self.client.post(f"/api/v1/runs/{run.id}/resend_email/")

        self.assertEqual(response.status_code, 202)
        delay.assert_called_once_with(user_id=self.user.id, run_id=run.id)
        run.refresh_from_db()
        self.assertEqual(run.email_status, "pending")
        self.assertIsNone(run.sent_at)

    def test_resend_is_refused_while_pending_or_sent(self) -> None:
        for email_status in ("pending", "sent"):
            run = self._run(email_status=email_status)
            response = self.client.post(f"/api/v1/runs/{run.id}/resend_email/")
            self.assertEqual(response.status_code, 400)

    def test_another_users_run_is_404(self) -> None:
        other = User.objects.create_user(username="frank", email="frank@example.test", password="pw12345!")
        active_subscription_for(other)
        run = self._run(email_status="failed")
        other_client = APIClient()
        other_client.force_authenticate(user=other)

        response = other_client.post(f"/api/v1/runs/{run.id}/resend_email/")

        self.assertEqual(response.status_code, 404)


def _minutes(delta: timedelta) -> float:
    return delta.total_seconds() / 60


class StrandedClaimTests(_Base):
    def test_old_claim_that_never_finalised_becomes_a_resendable_failure(self) -> None:
        run = self._run(sent_at=timezone.now() - STRANDED_CLAIM_AGE - timedelta(minutes=1))
        with patch.object(send_recommendation_email, "delay") as delay:
            with self.assertLogs("fragrance.tasks", level="ERROR") as logs:
                reap_stale_runs()

        delay.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.email_status, "failed")
        self.assertIn("Resend", run.error_message)
        self.assertIn(str(run.id), logs.output[0])
        self.assertNotIn("dave@example.test", logs.output[0])

    def test_a_fresh_claim_and_other_shapes_are_left_alone(self) -> None:
        fresh = self._run(sent_at=timezone.now() - timedelta(minutes=10))
        sent = self._run(email_status="sent", sent_at=timezone.now() - timedelta(days=2))
        failed_run = self._run(status="failed", sent_at=timezone.now() - timedelta(days=2))
        reap_stale_runs()

        for run, expected in ((fresh, "pending"), (sent, "sent"), (failed_run, "pending")):
            run.refresh_from_db()
            self.assertEqual(run.email_status, expected)

    def test_a_row_that_finalised_between_select_and_update_is_not_overwritten(self) -> None:
        run = self._run(sent_at=timezone.now() - timedelta(hours=3))
        RecommendationRun.objects.filter(pk=run.pk).update(email_status="sent")
        reap_stale_runs()

        run.refresh_from_db()
        self.assertEqual(run.email_status, "sent")
        self.assertEqual(run.error_message, "")

    def test_resend_works_on_the_failed_stranded_row(self) -> None:
        run = self._run(sent_at=timezone.now() - timedelta(hours=3))
        with self.assertLogs("fragrance.tasks", level="ERROR"):
            reap_stale_runs()
        client = APIClient()
        client.force_authenticate(user=self.user)
        with patch.object(send_recommendation_email, "delay") as delay:
            response = client.post(f"/api/v1/runs/{run.id}/resend_email/")

        self.assertEqual(response.status_code, 202)
        delay.assert_called_once()


class BoundedRequeueTests(_Base):
    def test_rows_older_than_the_ceiling_are_failed_not_mailed(self) -> None:
        old = self._run(minutes_ago=_minutes(PENDING_EMAIL_REQUEUE_MAX_AGE) + 60)
        with patch.object(send_recommendation_email, "delay") as delay:
            with self.assertLogs("fragrance.tasks", level="WARNING") as logs:
                reap_stale_runs()

        delay.assert_not_called()
        old.refresh_from_db()
        self.assertEqual(old.email_status, "failed")
        self.assertIsNone(old.sent_at)
        self.assertTrue(any("1 unsent email" in line for line in logs.output))

    def test_a_run_inside_the_window_is_still_requeued(self) -> None:
        run = self._run(minutes_ago=_minutes(PENDING_EMAIL_REQUEUE_MAX_AGE) - 60)
        with patch.object(send_recommendation_email, "delay") as delay:
            reap_stale_runs()

        delay.assert_called_once_with(user_id=self.user.id, run_id=run.id)

    def test_one_tick_queues_at_most_the_batch_in_id_order(self) -> None:
        runs = [self._run() for _ in range(PENDING_EMAIL_REQUEUE_BATCH + 3)]
        with patch.object(send_recommendation_email, "delay") as delay:
            reap_stale_runs()

        queued = [call.kwargs["run_id"] for call in delay.call_args_list]
        self.assertEqual(queued, [run.id for run in runs[:PENDING_EMAIL_REQUEUE_BATCH]])

    def test_a_lapsed_user_is_skipped_without_consuming_the_batch(self) -> None:
        lapsed = User.objects.create_user(username="frank", email="frank@example.test", password="pw12345!")
        Subscription.objects.create(user=lapsed, status="expired", access_expires_at=timezone.now() - timedelta(days=3))
        for _ in range(PENDING_EMAIL_REQUEUE_BATCH):
            RecommendationRun.objects.create(user=lapsed, status="done", email_status="pending")
        mine = self._run()
        RecommendationRun.objects.filter(user=lapsed).update(triggered_at=timezone.now() - timedelta(hours=3))
        with patch.object(send_recommendation_email, "delay") as delay:
            reap_stale_runs()

        delay.assert_called_once_with(user_id=self.user.id, run_id=mine.id)
        self.assertEqual(
            RecommendationRun.objects.filter(user=lapsed, email_status="pending").count(),
            PENDING_EMAIL_REQUEUE_BATCH,
        )

    def test_revoked_and_inactive_users_are_skipped(self) -> None:
        Subscription.objects.filter(user=self.user).update(status="revoked")
        revoked = self._run()
        Subscription.objects.filter(user=self.user).update(status="active")
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        inactive = self._run()
        with patch.object(send_recommendation_email, "delay") as delay:
            reap_stale_runs()

        delay.assert_not_called()
        for run in (revoked, inactive):
            run.refresh_from_db()
            self.assertEqual(run.email_status, "pending")

    def test_broker_outage_stops_the_tick_after_one_error_log(self) -> None:
        for _ in range(3):
            self._run()
        with patch.object(send_recommendation_email, "delay", side_effect=OperationalError("down")) as delay:
            with self.assertLogs("fragrance.tasks", level="ERROR") as logs:
                reap_stale_runs()

        self.assertEqual(delay.call_count, 1)
        self.assertEqual(len(logs.output), 1)

    def test_soft_time_limit_is_not_swallowed(self) -> None:
        from celery.exceptions import SoftTimeLimitExceeded

        self._run()
        with patch.object(send_recommendation_email, "delay", side_effect=SoftTimeLimitExceeded()):
            with self.assertRaises(SoftTimeLimitExceeded):
                reap_stale_runs()


class FinalFailureAndBookkeepingTests(_Base):
    def test_final_failure_does_not_flip_a_row_that_was_sent_meanwhile(self) -> None:
        run = self._run()

        def send_then_mark_sent(**kwargs: object) -> None:
            RecommendationRun.objects.filter(pk=run.pk).update(email_status="sent")
            raise OSError("smtp down")

        with patch("fragrance.tasks.render_and_send_email", side_effect=send_then_mark_sent):
            task = send_recommendation_email
            task.push_request(retries=task.max_retries)
            try:
                with self.assertRaises(OSError):
                    task(user_id=self.user.id, run_id=run.id)
            finally:
                task.pop_request()

        run.refresh_from_db()
        self.assertEqual(run.email_status, "sent")

    def test_bookkeeping_is_retried_once_on_a_fresh_connection(self) -> None:
        run = self._run()
        original_save = RecommendationRun.save
        attempts = {"count": 0}

        def flaky_save(instance: RecommendationRun, *args: object, **kwargs: object) -> None:
            if kwargs.get("update_fields") == ["sent_at", "email_status"]:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise RuntimeError("connection dropped")
            original_save(instance, *args, **kwargs)  # type: ignore[arg-type]

        with patch.object(RecommendationRun, "save", flaky_save):
            with patch("fragrance.tasks.connection") as fake_connection:
                fake_connection.in_atomic_block = False
                with patch("fragrance.tasks.close_old_connections") as close:
                    send_recommendation_email(user_id=self.user.id, run_id=run.id)

        close.assert_called_once()
        self.assertEqual(attempts["count"], 2)
        run.refresh_from_db()
        self.assertEqual(run.email_status, "sent")
        self.assertEqual(len(mail.outbox), 1)
