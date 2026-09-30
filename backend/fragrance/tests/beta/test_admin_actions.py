import re
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from fragrance.models import BetaSignup, Plan, Subscription
from fragrance.services import verify_beta_invite_token
from fragrance.tasks import send_beta_invite_email


class BetaSignupAdminApproveTests(TestCase):
    """
    Covers the approve_signups admin action: guard-on-pending, the
    duplicate-User-email refusal (the guard against reopening
    PasswordResetRequestView's MultipleObjectsReturned bug), username
    collision handling, the Plan-may-not-exist-yet fallback, and that the
    invite email is queued via transaction.on_commit rather than sent inline.
    """

    def setUp(self) -> None:
        self.superuser = User.objects.create_superuser(
            username="hal", email="hal@example.com", password="hal-9000"
        )
        self.client.force_login(self.superuser)
        self.plan = Plan.objects.create(
            name="Beta Monthly",
            slug="beta-monthly",
            stripe_price_id="price_default_monthly",
            amount_cents=400,
            billing_interval="month",
        )

    def _changelist_url(self) -> str:
        return reverse("admin:fragrance_betasignup_changelist")

    def _approve(self, pks: list[int]) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                self._changelist_url(),
                {"action": "approve_signups", "_selected_action": [str(pk) for pk in pks]},
            )

    def test_approve_creates_active_user_with_unusable_password(self) -> None:
        signup = BetaSignup.objects.create(email="new@example.com", normalized_email="new@example.com")
        with patch.object(send_beta_invite_email, "delay"):
            self._approve([signup.pk])

        signup.refresh_from_db()
        self.assertEqual(signup.status, "approved")
        self.assertIsNotNone(signup.approved_at)
        self.assertEqual(signup.approved_by, self.superuser)
        user = signup.invited_user
        self.assertIsNotNone(user)
        self.assertTrue(user.is_active)
        self.assertFalse(user.has_usable_password())

    def test_approve_creates_subscription_with_null_plan_and_none_status(self) -> None:
        """Approval no longer assigns a plan, even when an active Plan exists: the first grant sets it."""
        signup = BetaSignup.objects.create(email="sub@example.com", normalized_email="sub@example.com")
        with patch.object(send_beta_invite_email, "delay"):
            self._approve([signup.pk])

        signup.refresh_from_db()
        sub = Subscription.objects.get(user=signup.invited_user)
        self.assertEqual(sub.status, "none")
        self.assertIsNone(sub.plan)
        self.assertTrue(Plan.objects.filter(pk=self.plan.pk, is_active=True).exists())

    def test_approve_creates_the_fragrance_config_and_periodic_task(self) -> None:
        from django_celery_beat.models import PeriodicTask
        from fragrance.models import FragranceConfig
        from fragrance.services import default_schedule_for

        signup = BetaSignup.objects.create(email="cfg@example.com", normalized_email="cfg@example.com")
        with patch.object(send_beta_invite_email, "delay"):
            self._approve([signup.pk])

        signup.refresh_from_db()
        user = signup.invited_user
        config = FragranceConfig.objects.get(user=user)
        self.assertEqual(config.recipient_email, "cfg@example.com")
        self.assertEqual(
            (config.run_day_of_month, config.run_hour), default_schedule_for(user_id=user.id)
        )
        task = PeriodicTask.objects.get(name=f"fragrance-run-{user.id}")
        self.assertEqual(task.crontab.day_of_month, str(config.run_day_of_month))
        self.assertTrue(task.enabled)

    def test_a_skipped_approval_creates_no_config(self) -> None:
        from fragrance.models import FragranceConfig

        signup = BetaSignup.objects.create(
            email="skipcfg@example.com", normalized_email="skipcfg@example.com", status="rejected"
        )
        with patch.object(send_beta_invite_email, "delay"):
            self._approve([signup.pk])

        self.assertEqual(FragranceConfig.objects.count(), 0)

    def test_a_config_failure_rolls_the_whole_approval_back(self) -> None:
        from fragrance.models import FragranceConfig

        signup = BetaSignup.objects.create(email="atomic@example.com", normalized_email="atomic@example.com")
        with patch.object(send_beta_invite_email, "delay"), patch(
            "fragrance.admin.get_or_create_fragrance_config", side_effect=RuntimeError("boom")
        ):
            self._approve([signup.pk])

        signup.refresh_from_db()
        self.assertEqual(signup.status, "pending")
        self.assertFalse(User.objects.filter(email="atomic@example.com").exists())
        self.assertEqual(FragranceConfig.objects.count(), 0)

    def test_approve_with_no_plan_seeded_creates_subscription_with_null_plan(self) -> None:
        Plan.objects.all().delete()
        signup = BetaSignup.objects.create(
            email="noplan@example.com", normalized_email="noplan@example.com"
        )
        with patch.object(send_beta_invite_email, "delay"):
            self._approve([signup.pk])

        signup.refresh_from_db()
        sub = Subscription.objects.get(user=signup.invited_user)
        self.assertIsNone(sub.plan)
        self.assertEqual(sub.status, "none")

    def test_approve_queues_email_rather_than_sending_inline(self) -> None:
        signup = BetaSignup.objects.create(
            email="queued@example.com", normalized_email="queued@example.com"
        )
        with patch.object(send_beta_invite_email, "delay") as mock_delay:
            self._approve([signup.pk])

        signup.refresh_from_db()
        mock_delay.assert_called_once_with(signup_id=signup.id)
        self.assertEqual(len(mail.outbox), 0)

    def test_approve_refuses_when_a_user_with_that_email_already_exists(self) -> None:
        User.objects.create_user(
            username="existing", email="dupe@example.com", password="pw12345!"
        )
        signup = BetaSignup.objects.create(email="dupe@example.com", normalized_email="dupe@example.com")
        with patch.object(send_beta_invite_email, "delay") as mock_delay:
            self._approve([signup.pk])

        signup.refresh_from_db()
        self.assertEqual(signup.status, "pending")
        self.assertIsNone(signup.invited_user)
        mock_delay.assert_not_called()
        self.assertEqual(User.objects.filter(email__iexact="dupe@example.com").count(), 1)

    def test_approve_skips_non_pending_signups(self) -> None:
        signup = BetaSignup.objects.create(
            email="already@example.com", normalized_email="already@example.com", status="rejected"
        )
        with patch.object(send_beta_invite_email, "delay") as mock_delay:
            self._approve([signup.pk])

        signup.refresh_from_db()
        self.assertEqual(signup.status, "rejected")
        self.assertIsNone(signup.invited_user)
        mock_delay.assert_not_called()

    def test_approve_username_collision_produces_distinct_usernames(self) -> None:
        User.objects.create_user(username="ryan", email="ryan@gmail.com", password="pw12345!")
        signup = BetaSignup.objects.create(email="ryan@example.com", normalized_email="ryan@example.com")
        with patch.object(send_beta_invite_email, "delay"):
            self._approve([signup.pk])

        signup.refresh_from_db()
        self.assertEqual(signup.invited_user.username, "ryan_2")

    def test_approve_continues_past_a_skipped_row_in_the_same_batch(self) -> None:
        good = BetaSignup.objects.create(email="good@example.com", normalized_email="good@example.com")
        bad = BetaSignup.objects.create(
            email="bad@example.com", normalized_email="bad@example.com", status="approved"
        )
        with patch.object(send_beta_invite_email, "delay") as mock_delay:
            self._approve([good.pk, bad.pk])

        good.refresh_from_db()
        bad.refresh_from_db()
        self.assertEqual(good.status, "approved")
        self.assertIsNotNone(good.invited_user)
        self.assertEqual(bad.status, "approved")  # unchanged, was already approved
        mock_delay.assert_called_once_with(signup_id=good.id)


class BetaSignupAdminRejectTests(TestCase):
    def setUp(self) -> None:
        self.superuser = User.objects.create_superuser(
            username="hal", email="hal@example.com", password="hal-9000"
        )
        self.client.force_login(self.superuser)

    def _reject(self, pks: list[int]) -> None:
        self.client.post(
            reverse("admin:fragrance_betasignup_changelist"),
            {"action": "reject_signups", "_selected_action": [str(pk) for pk in pks]},
        )

    def test_reject_pending_signup(self) -> None:
        signup = BetaSignup.objects.create(email="r@example.com", normalized_email="r@example.com")
        self._reject([signup.pk])
        signup.refresh_from_db()
        self.assertEqual(signup.status, "rejected")

    def test_reject_skips_non_pending_signup(self) -> None:
        signup = BetaSignup.objects.create(
            email="a@example.com", normalized_email="a@example.com", status="approved"
        )
        self._reject([signup.pk])
        signup.refresh_from_db()
        self.assertEqual(signup.status, "approved")


class BetaSignupAdminResendInviteTests(TestCase):
    """
    Covers the resend_invite admin action: recovery for a signup whose
    invite email never arrived (spam filter, bounce, typo) -- previously a
    dead end short of a Django shell once approve_signups moved the row past
    'pending'.
    """

    def setUp(self) -> None:
        self.superuser = User.objects.create_superuser(
            username="hal", email="hal@example.com", password="hal-9000"
        )
        self.client.force_login(self.superuser)

    def _resend(self, pks: list[int]) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("admin:fragrance_betasignup_changelist"),
                {"action": "resend_invite", "_selected_action": [str(pk) for pk in pks]},
            )

    def test_resend_queues_email_for_approved_signup(self) -> None:
        user = User.objects.create_user(username="resend_me", email="resend@example.com")
        signup = BetaSignup.objects.create(
            email="resend@example.com", normalized_email="resend@example.com",
            status="approved", invited_user=user,
        )
        with patch.object(send_beta_invite_email, "delay") as mock_delay:
            self._resend([signup.pk])

        mock_delay.assert_called_once_with(signup_id=signup.id)
        self.assertEqual(len(mail.outbox), 0)

    def test_resend_skips_pending_signup(self) -> None:
        signup = BetaSignup.objects.create(
            email="still_pending@example.com", normalized_email="still_pending@example.com",
        )
        with patch.object(send_beta_invite_email, "delay") as mock_delay:
            self._resend([signup.pk])
        mock_delay.assert_not_called()

    def test_resend_skips_converted_signup(self) -> None:
        user = User.objects.create_user(username="already_in", email="already_in@example.com")
        signup = BetaSignup.objects.create(
            email="already_in@example.com", normalized_email="already_in@example.com",
            status="converted", invited_user=user,
        )
        with patch.object(send_beta_invite_email, "delay") as mock_delay:
            self._resend([signup.pk])
        mock_delay.assert_not_called()

    def test_resend_skips_rejected_signup(self) -> None:
        signup = BetaSignup.objects.create(
            email="nope@example.com", normalized_email="nope@example.com", status="rejected",
        )
        with patch.object(send_beta_invite_email, "delay") as mock_delay:
            self._resend([signup.pk])
        mock_delay.assert_not_called()

    def test_resend_actually_sends_a_valid_accept_link(self) -> None:
        # Unmocked end-to-end run (no captureOnCommitCallbacks needed since
        # this calls the task body directly, matching how
        # BetaInviteEmailTaskTests already exercises send_beta_invite_email):
        # confirms resend_invite's queued call produces a real, verifiable
        # token, not just that .delay() was called.
        user = User.objects.create_user(username="fresh_token", email="fresh_token@example.com")
        signup = BetaSignup.objects.create(
            email="fresh_token@example.com", normalized_email="fresh_token@example.com",
            status="approved", invited_user=user,
        )
        send_beta_invite_email(signup_id=signup.id)

        self.assertEqual(len(mail.outbox), 1)
        match = re.search(r"token=([^\s]+)", mail.outbox[0].body)
        self.assertIsNotNone(match)
        payload = verify_beta_invite_token(token=match.group(1))
        self.assertEqual(payload, {"sid": signup.id, "uid": user.id})
