"""Customer failure emails (once per chain per kind), the operator alert, and the 4-hourly digest."""

from datetime import timedelta
from unittest.mock import patch

from celery.exceptions import Retry
from django.contrib.auth.models import User
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from fragrance.models import FragranceConfig, RecommendationRun
from fragrance.tasks import (
    fail_run_and_notify,
    send_operator_failure_digest,
    send_operator_run_exhausted_alert,
    send_run_failure_notice,
)
from fragrance.tests.retries.helpers import FrozenClockTestCase, make_run
from fragrance.tests.support.factories import active_subscription_for

SETTINGS = dict(
    FRONTEND_URL="https://app.example.test",
    SUPPORT_EMAIL="help@example.test",
    ADMIN_EMAIL="ops@example.test",
    DEFAULT_FROM_EMAIL="noreply@example.test",
)


@override_settings(**SETTINGS)
class RunFailureNoticeTests(FrozenClockTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="notice", email="acct@example.test", password="pw12345!")
        active_subscription_for(self.user)
        FragranceConfig.objects.create(user=self.user, recipient_email="deliver@example.test")
        self.run = make_run(
            user=self.user, status="failed", retry_state="scheduled", attempt=3,
            error_message="SECRET-INTERNAL-ERROR", last_failed_at=timezone.now(),
            next_retry_at=timezone.now() + timedelta(days=1),
        )

    def _refresh(self) -> RecommendationRun:
        self.run.refresh_from_db()
        return self.run

    def test_the_retrying_email_goes_to_the_configured_recipient_with_text_and_html(self) -> None:
        send_run_failure_notice(run_id=self.run.id, kind="retrying")

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["deliver@example.test"])
        self.assertEqual(message.subject, "Your fragrance picks hit a snag. We're trying again.")
        self.assertIn("https://app.example.test/runs", message.body)
        self.assertIn("help@example.test", message.body)
        self.assertEqual(message.alternatives[0][1], "text/html")
        self.assertIn("https://app.example.test/runs", message.alternatives[0][0])
        self.assertIsNotNone(self._refresh().retry_notice_sent_at)

    def test_it_is_sent_once_however_many_times_the_task_runs(self) -> None:
        for _ in range(3):
            send_run_failure_notice(run_id=self.run.id, kind="retrying")

        self.assertEqual(len(mail.outbox), 1)

    def test_later_failures_of_the_same_chain_send_no_further_retrying_email(self) -> None:
        with patch("fragrance.tasks.send_operator_run_exhausted_alert.delay"):
            for _ in range(2):
                RecommendationRun.objects.filter(pk=self.run.pk).update(status="running", retry_state="")
                with patch("fragrance.tasks.send_run_failure_notice.delay", side_effect=lambda **kw: send_run_failure_notice(**kw)):
                    fail_run_and_notify(run_id=self.run.id, error_message="again")

        self.assertEqual(len(mail.outbox), 1)

    def test_neither_email_leaks_attempt_counts_or_error_text(self) -> None:
        RecommendationRun.objects.filter(pk=self.run.pk).update(retry_state="exhausted", attempt=5)
        send_run_failure_notice(run_id=self.run.id, kind="retrying")
        send_run_failure_notice(run_id=self.run.id, kind="exhausted")

        self.assertEqual(len(mail.outbox), 2)
        for message in mail.outbox:
            for text in (message.body, message.alternatives[0][0]):
                self.assertNotIn("SECRET-INTERNAL-ERROR", text)
                self.assertNotIn("attempt", text.lower())
                self.assertNotIn("3 of", text)
                self.assertNotIn("5 of", text)

    def test_the_exhausted_email_is_sent_only_for_an_exhausted_chain(self) -> None:
        send_run_failure_notice(run_id=self.run.id, kind="exhausted")
        self.assertEqual(len(mail.outbox), 0)

        RecommendationRun.objects.filter(pk=self.run.pk).update(retry_state="exhausted")
        send_run_failure_notice(run_id=self.run.id, kind="exhausted")

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "We couldn't create this month's fragrance picks")
        self.assertIn("help@example.test", mail.outbox[0].body)
        self.assertIsNotNone(self._refresh().exhausted_notice_sent_at)

    def test_a_recovered_run_gets_no_late_retrying_email(self) -> None:
        RecommendationRun.objects.filter(pk=self.run.pk).update(status="done", retry_state="")

        send_run_failure_notice(run_id=self.run.id, kind="retrying")

        self.assertEqual(len(mail.outbox), 0)

    def test_a_blank_recipient_falls_back_to_the_account_email(self) -> None:
        FragranceConfig.objects.filter(user=self.user).update(recipient_email="")

        send_run_failure_notice(run_id=self.run.id, kind="retrying")

        self.assertEqual(mail.outbox[0].to, ["acct@example.test"])

    def test_a_failed_send_releases_the_claim_and_schedules_a_retry(self) -> None:
        with (
            patch("django.core.mail.EmailMultiAlternatives.send", side_effect=RuntimeError("smtp down")),
            patch.object(send_run_failure_notice, "retry", side_effect=Retry()) as mock_retry,
        ):
            with self.assertRaises(Retry):
                send_run_failure_notice(run_id=self.run.id, kind="retrying")

        self.assertEqual(mock_retry.call_args.kwargs["countdown"], 300)
        self.assertIsNone(self._refresh().retry_notice_sent_at)

    def test_first_failure_queues_retrying_and_exhaustion_queues_exhausted_plus_the_alert(self) -> None:
        first = make_run(user=self.user, status="running")
        with (
            patch("fragrance.tasks.send_run_failure_notice.delay") as notice,
            patch("fragrance.tasks.send_operator_run_exhausted_alert.delay") as alert,
        ):
            fail_run_and_notify(run_id=first.id, error_message="x")
            notice.assert_called_once_with(run_id=first.id, kind="retrying")
            alert.assert_not_called()

            last = make_run(user=self.user, status="running", attempt=5)
            fail_run_and_notify(run_id=last.id, error_message="x")
            notice.assert_called_with(run_id=last.id, kind="exhausted")
            alert.assert_called_once_with(run_id=last.id)

    def test_a_stopped_chain_sends_no_customer_email(self) -> None:
        from fragrance.models import Subscription

        Subscription.objects.filter(user=self.user).update(status="revoked")
        run = make_run(user=self.user, status="running")
        with (
            patch("fragrance.tasks.send_run_failure_notice.delay") as notice,
            patch("fragrance.tasks.send_operator_run_exhausted_alert.delay") as alert,
        ):
            fail_run_and_notify(run_id=run.id, error_message="x")

        notice.assert_not_called()
        alert.assert_not_called()

    def test_a_broker_failure_while_queueing_notices_does_not_raise(self) -> None:
        run = make_run(user=self.user, status="running")
        with patch("fragrance.tasks.send_run_failure_notice.delay", side_effect=RuntimeError("broker")):
            fail_run_and_notify(run_id=run.id, error_message="x")

        run.refresh_from_db()
        self.assertEqual(run.retry_state, "scheduled")


@override_settings(**SETTINGS)
class OperatorAlertTests(FrozenClockTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="opsuser", email="o@example.test", password="pw12345!")
        self.run = make_run(
            user=self.user, status="failed", retry_state="exhausted", attempt=5,
            error_message="provider 500", last_failed_at=timezone.now(),
        )

    def test_the_alert_goes_to_admin_email_once_with_the_error(self) -> None:
        send_operator_run_exhausted_alert(run_id=self.run.id)
        send_operator_run_exhausted_alert(run_id=self.run.id)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ops@example.test"])
        self.assertIn("provider 500", mail.outbox[0].body)
        self.assertIn(str(self.run.id), mail.outbox[0].subject)
        self.run.refresh_from_db()
        self.assertIsNotNone(self.run.operator_notified_at)

    @override_settings(ADMIN_EMAIL="")
    def test_a_blank_admin_email_skips_without_error(self) -> None:
        send_operator_run_exhausted_alert(run_id=self.run.id)

        self.assertEqual(len(mail.outbox), 0)

    def test_a_failed_send_retries_and_is_not_marked_notified(self) -> None:
        with (
            patch("fragrance.tasks.send_mail", side_effect=RuntimeError("smtp")),
            patch.object(send_operator_run_exhausted_alert, "retry", side_effect=Retry()),
        ):
            with self.assertRaises(Retry):
                send_operator_run_exhausted_alert(run_id=self.run.id)

        self.run.refresh_from_db()
        self.assertIsNone(self.run.operator_notified_at)


@override_settings(**SETTINGS)
class OperatorDigestTests(FrozenClockTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="digest", email="d@example.test", password="pw12345!")

    def _failed(self, *, error: str = "boom", **fields: object) -> RecommendationRun:
        return make_run(
            user=self.user, status="failed", retry_state="scheduled", error_message=error,
            last_failed_at=timezone.now() - timedelta(minutes=1), **fields,
        )

    def test_it_sends_nothing_when_there_are_no_new_failures(self) -> None:
        self.assertEqual(send_operator_failure_digest(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_one_email_lists_every_new_failure_then_none_until_another_happens(self) -> None:
        first = self._failed(error="err-one")
        second = self._failed(error="err-two")

        self.assertEqual(send_operator_failure_digest(), 2)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ops@example.test"])
        for expected in (f"Run {first.id}", f"Run {second.id}", "err-one", "err-two"):
            self.assertIn(expected, mail.outbox[0].body)

        self.assertEqual(send_operator_failure_digest(), 0)
        self.assertEqual(len(mail.outbox), 1)

        RecommendationRun.objects.filter(pk=first.pk).update(last_failed_at=timezone.now() + timedelta(seconds=1))
        with patch("fragrance.tasks.timezone.now", return_value=timezone.now() + timedelta(hours=1)):
            self.assertEqual(send_operator_failure_digest(), 1)
        self.assertEqual(len(mail.outbox), 2)

    def test_an_exhausted_run_already_alerted_is_not_repeated_in_the_digest(self) -> None:
        run = self._failed()
        RecommendationRun.objects.filter(pk=run.pk).update(retry_state="exhausted")
        send_operator_run_exhausted_alert(run_id=run.id)
        mail.outbox.clear()

        self.assertEqual(send_operator_failure_digest(), 0)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(ADMIN_EMAIL="")
    def test_a_blank_admin_email_sends_nothing_and_keeps_the_failures_unreported(self) -> None:
        run = self._failed()

        self.assertEqual(send_operator_failure_digest(), 0)

        run.refresh_from_db()
        self.assertIsNone(run.operator_notified_at)

    def test_the_digest_and_sweep_are_in_the_beat_schedule(self) -> None:
        from config.celery import app

        schedule = app.conf.beat_schedule
        self.assertEqual(schedule["system-retry-failed-runs"]["task"], "fragrance.tasks.retry_failed_runs")
        self.assertEqual(
            schedule["system-operator-failure-digest"]["task"], "fragrance.tasks.send_operator_failure_digest"
        )
