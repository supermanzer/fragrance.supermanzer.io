"""
Reply-To on picks and failure notices (replies reach SUPPORT_EMAIL); credential-bearing mail
(password reset, beta invite) and operator mail to ADMIN_EMAIL carry none.
"""

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from fragrance.models import BetaSignup, FragranceConfig, Recommendation, RecommendationRun
from fragrance.tasks import (
    render_and_send_email,
    send_beta_invite_email,
    send_operator_failure_digest,
    send_operator_run_exhausted_alert,
    send_password_reset_email,
    send_run_failure_notice,
)
from django.utils import timezone

SETTINGS = dict(
    SUPPORT_EMAIL="support@example.test",
    ADMIN_EMAIL="ops@example.test",
    DEFAULT_FROM_EMAIL="picks@example.test",
    FRONTEND_URL="https://app.example.test",
)
FEEDBACK_LINE = "Was any pick a miss? Reply to this email and tell me."


@override_settings(**SETTINGS)
class ReplyToTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", email="dave@example.test", password="hal-9000")
        FragranceConfig.objects.create(user=self.user, recipient_email="dave@example.test")
        self.run = RecommendationRun.objects.create(user=self.user, status="done", intro="Welcome back.")
        Recommendation.objects.create(
            user=self.user, run=self.run, name="Aventus", house="Creed", status="confirmed", rationale="Smoky."
        )

    def test_picks_email_replies_go_to_support_and_carry_the_feedback_line(self) -> None:
        render_and_send_email(user_id=self.user.id, run_id=self.run.id)

        self.assertEqual(mail.outbox[0].reply_to, ["support@example.test"])
        self.assertEqual(mail.outbox[0].body.count(FEEDBACK_LINE), 1)

    def test_run_failure_notice_replies_go_to_support(self) -> None:
        RecommendationRun.objects.filter(pk=self.run.pk).update(status="failed", last_failed_at=timezone.now())

        send_run_failure_notice(run_id=self.run.id, kind="retrying")

        self.assertEqual(mail.outbox[0].reply_to, ["support@example.test"])

    def test_password_reset_mail_carries_no_reply_to(self) -> None:
        send_password_reset_email(user_id=self.user.id)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].reply_to, [])

    def test_beta_invite_carries_no_reply_to(self) -> None:
        signup = BetaSignup.objects.create(
            email="dave@example.test", normalized_email="dave@example.test", invited_user=self.user
        )

        send_beta_invite_email(signup_id=signup.id)

        self.assertEqual(mail.outbox[0].to, ["dave@example.test"])
        self.assertEqual(mail.outbox[0].reply_to, [])

    def test_operator_mail_to_admin_has_no_reply_to(self) -> None:
        RecommendationRun.objects.filter(pk=self.run.pk).update(
            status="failed", retry_state="exhausted", last_failed_at=timezone.now()
        )

        send_operator_run_exhausted_alert(run_id=self.run.id)
        RecommendationRun.objects.filter(pk=self.run.pk).update(operator_notified_at=None)
        send_operator_failure_digest()

        self.assertEqual(len(mail.outbox), 2)
        for message in mail.outbox:
            self.assertEqual(message.to, ["ops@example.test"])
            self.assertEqual(message.reply_to, [])

    @override_settings(SUPPORT_EMAIL="")
    def test_blank_support_email_yields_no_reply_to(self) -> None:
        render_and_send_email(user_id=self.user.id, run_id=self.run.id)
        RecommendationRun.objects.filter(pk=self.run.pk).update(status="failed", last_failed_at=timezone.now())
        send_run_failure_notice(run_id=self.run.id, kind="retrying")

        self.assertEqual(len(mail.outbox), 2)
        for message in mail.outbox:
            self.assertEqual(message.reply_to, [])
