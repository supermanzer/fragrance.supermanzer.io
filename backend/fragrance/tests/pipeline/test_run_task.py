from unittest.mock import MagicMock, patch

from celery.exceptions import Retry
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from fragrance.ai_providers import registry
from fragrance.models import (
    AIModelConfig,
    BetaSignup,
    FragranceConfig,
    Recommendation,
    RecommendationRun,
)
from fragrance.tasks import (
    monthly_fragrance_run,
    render_and_send_email,
    send_beta_invite_email,
    send_recommendation_email,
)
from fragrance.tests.support.factories import active_subscription_for
from fragrance.tests.support.llm_fakes import _echo_query_search, _ScriptedProvider


class MonthlyFragranceRunTests(TestCase):
    def setUp(self) -> None:
        # Migration 0006 seeds a default active row into every fresh test
        # database — these tests set up their own explicit AIModelConfig
        # state per test (including "none active at all") and must start
        # from an empty table.
        AIModelConfig.objects.all().delete()
        self.user = User.objects.create_user(username="dave", password="hal-9000")
        active_subscription_for(self.user)
        FragranceConfig.objects.create(
            user=self.user, recipient_email="dave@example.com"
        )

    def test_success_path_resolves_provider_once_and_snapshots_it_on_the_run(
        self,
    ) -> None:
        AIModelConfig.objects.create(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            is_active=True,
            supports_enum_enforcement=True,
        )
        candidates = [
            {"name": f"Candidate {i}", "house": f"House {i}"} for i in range(5)
        ]
        scripted = _ScriptedProvider(
            responses_by_tool={
                "create_profile": {
                    "loved_notes": "oud",
                    "liked_notes": "citrus",
                    "disliked_notes": "aquatic",
                    "owns_list": [],
                    "search_angle_1": "niche oud",
                    "search_angle_2": "amber 2026",
                },
                "select_candidates": {"candidates": candidates},
                "generate_email_content": {
                    "intro": "Welcome back, Dave.",
                    "rationales": [
                        {"name": c["name"], "rationale": f"Suits {c['name']}."}
                        for c in candidates
                    ],
                },
            }
        )

        with (
            patch(
                "fragrance.tasks.registry.build_provider", return_value=scripted
            ),
            patch(
                "fragrance.search.searxng_search", side_effect=_echo_query_search
            ),
            patch("fragrance.tasks.send_recommendation_email.delay") as mock_delay,
        ):
            monthly_fragrance_run(user_id=self.user.id)

        run = RecommendationRun.objects.get(user=self.user)
        self.assertEqual(run.status, "done")
        self.assertEqual(run.provider_family, "anthropic")
        self.assertEqual(run.provider_model_id, "claude-haiku-4-5-20251001")
        self.assertEqual(run.intro, "Welcome back, Dave.")
        self.assertEqual(
            Recommendation.objects.filter(run=run, status="confirmed").count(), 5
        )
        mock_delay.assert_called_once_with(user_id=self.user.id, run_id=run.id)
        # Exactly one provider resolution per run — not re-resolved per call.
        tool_names_called = [c["tool_name"] for c in scripted.calls]
        self.assertEqual(
            tool_names_called,
            ["create_profile", "select_candidates", "generate_email_content"],
        )

    def test_failure_path_no_active_ai_model_config_fails_run_with_clear_message(
        self,
    ) -> None:
        # No AIModelConfig row exists at all — the provider-resolution failure
        # this task introduces must be a clear, typed error surfaced onto the
        # run, not a downstream AttributeError from a None provider.
        monthly_fragrance_run.push_request(retries=monthly_fragrance_run.max_retries)
        try:
            with self.assertRaises(registry.NoActiveAIModelConfigError):
                monthly_fragrance_run(user_id=self.user.id)
        finally:
            monthly_fragrance_run.pop_request()

        run = RecommendationRun.objects.get(user=self.user)
        self.assertEqual(run.status, "failed")
        self.assertIn("No active AIModelConfig", run.error_message)

    def test_config_error_fails_on_first_attempt_not_after_retry_backoff(
        self,
    ) -> None:
        # Same failure as above, but with retries=0 (the very first attempt,
        # not the exhausted-retries branch) — proves NoActiveAIModelConfigError
        # is treated as non-retryable, not just "eventually fails after the
        # existing 300s x 2 backoff." A bare Exception here would instead hit
        # self.retry(...), which raises Retry (a control-flow exception, not
        # a real failure) and leaves run.status as 'running'.
        with self.assertRaises(registry.NoActiveAIModelConfigError):
            monthly_fragrance_run(user_id=self.user.id)

        run = RecommendationRun.objects.get(user=self.user)
        self.assertEqual(run.status, "failed")
        self.assertIn("No active AIModelConfig", run.error_message)

    def test_sdk_api_error_message_is_sanitized_on_run_error_message(self) -> None:
        AIModelConfig.objects.create(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            is_active=True,
            supports_enum_enforcement=True,
        )

        class _ExplodingProvider:
            def generate_structured(self, **kwargs: object) -> dict:
                import anthropic

                raise anthropic.APIConnectionError(
                    message="Incorrect API key provided: sk-***abcXYZ",
                    request=MagicMock(),
                )

        monthly_fragrance_run.push_request(retries=monthly_fragrance_run.max_retries)
        try:
            with self.assertRaises(Exception):
                with patch(
                    "fragrance.tasks.registry.build_provider",
                    return_value=_ExplodingProvider(),
                ):
                    monthly_fragrance_run(user_id=self.user.id)
        finally:
            monthly_fragrance_run.pop_request()

        run = RecommendationRun.objects.get(user=self.user)
        self.assertEqual(run.status, "failed")
        self.assertNotIn("sk-", run.error_message)
        self.assertNotIn("abcXYZ", run.error_message)
        self.assertIn("configured AI provider", run.error_message)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    FRONTEND_URL="http://test.local",
)
class RenderAndSendEmailTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", password="hal-9000")
        self.config = FragranceConfig.objects.create(
            user=self.user, recipient_email="dave@example.com"
        )
        self.run = RecommendationRun.objects.create(user=self.user, status="done", intro="Welcome back.")
        self.confirmed_pick = Recommendation.objects.create(
            user=self.user, run=self.run, name="Aventus", house="Creed",
            status="confirmed", rationale="Smoky and bold.",
        )
        Recommendation.objects.create(
            user=self.user, run=self.run, name="Rejected Pick", house="Nowhere",
            status="replaced", rationale="Failed verification.",
        )

    def test_email_contains_three_confirm_urls_for_confirmed_pick_only(self) -> None:
        render_and_send_email(user_id=self.user.id, run_id=self.run.id)

        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertEqual(body.count("http://test.local/recommendations/confirm?t="), 3)
        self.assertNotIn("Rejected Pick", body)

        self.run.refresh_from_db()
        self.assertEqual(self.run.email_status, "sent")

    def test_configured_recipient_wins_over_the_account_email(self) -> None:
        User.objects.filter(pk=self.user.pk).update(email="account@example.com")

        render_and_send_email(user_id=self.user.id, run_id=self.run.id)

        self.assertEqual(mail.outbox[0].to, ["dave@example.com"])

    def test_blank_recipient_falls_back_to_the_account_email_with_a_warning(self) -> None:
        User.objects.filter(pk=self.user.pk).update(email="account@example.com")
        FragranceConfig.objects.filter(pk=self.config.pk).update(recipient_email="")

        with self.assertLogs("fragrance.tasks", level="WARNING") as logs:
            render_and_send_email(user_id=self.user.id, run_id=self.run.id)

        self.assertEqual(mail.outbox[0].to, ["account@example.com"])
        self.assertTrue(any("blank recipient_email" in line for line in logs.output))
        self.config.refresh_from_db()
        self.assertEqual(self.config.recipient_email, "")

    def test_missing_config_is_created_from_the_account_email_and_the_email_still_sends(self) -> None:
        from django_celery_beat.models import PeriodicTask

        User.objects.filter(pk=self.user.pk).update(email="account@example.com")
        self.config.delete()
        PeriodicTask.objects.filter(name=f"fragrance-run-{self.user.id}").delete()

        with self.assertLogs("fragrance.tasks", level="WARNING") as logs:
            render_and_send_email(user_id=self.user.id, run_id=self.run.id)

        self.assertEqual(mail.outbox[0].to, ["account@example.com"])
        self.assertTrue(any("no FragranceConfig" in line for line in logs.output))
        created = FragranceConfig.objects.get(user=self.user)
        self.assertEqual(created.recipient_email, "account@example.com")
        self.assertTrue(PeriodicTask.objects.filter(name=f"fragrance-run-{self.user.id}").exists())

    def test_no_address_anywhere_raises_instead_of_sending_to_a_blank_recipient(self) -> None:
        FragranceConfig.objects.filter(pk=self.config.pk).update(recipient_email="")

        with self.assertRaises(ValueError), self.assertLogs("fragrance.tasks", level="WARNING"):
            render_and_send_email(user_id=self.user.id, run_id=self.run.id)

        self.assertEqual(mail.outbox, [])


# ---------------------------------------------------------------------------
# Email-task retry regression (InvoiceNotSettledError NameError)
# ---------------------------------------------------------------------------


class EmailTaskRetryTests(TestCase):
    def _run(self, *, task, retries: int, **kwargs: object) -> None:
        task.push_request(retries=retries)
        try:
            task(**kwargs)
        finally:
            task.pop_request()

    def test_beta_invite_smtp_failure_schedules_a_300s_retry(self) -> None:
        invited = User.objects.create_user(username="invitee", email="i@example.com", password="pw12345!")
        signup = BetaSignup.objects.create(
            email="i@example.com", normalized_email="i@example.com", invited_user=invited
        )
        with patch("fragrance.tasks.EmailMessage.send", side_effect=OSError("smtp down")):
            with patch.object(send_beta_invite_email, "retry", side_effect=Retry()) as mock_retry:
                with self.assertRaises(Retry):
                    self._run(task=send_beta_invite_email, retries=0, signup_id=signup.id)
        self.assertEqual(mock_retry.call_args.kwargs["countdown"], 300)
        self.assertIsInstance(mock_retry.call_args.kwargs["exc"], OSError)

    def test_beta_invite_reraises_the_original_error_once_retries_are_exhausted(self) -> None:
        invited = User.objects.create_user(username="invitee2", email="i2@example.com", password="pw12345!")
        signup = BetaSignup.objects.create(
            email="i2@example.com", normalized_email="i2@example.com", invited_user=invited
        )
        with patch("fragrance.tasks.EmailMessage.send", side_effect=OSError("smtp down")):
            with patch.object(send_beta_invite_email, "retry") as mock_retry:
                with self.assertRaises(OSError):
                    self._run(
                        task=send_beta_invite_email,
                        retries=send_beta_invite_email.max_retries,
                        signup_id=signup.id,
                    )
        mock_retry.assert_not_called()

    def test_recommendation_email_failure_schedules_a_300s_retry_and_leaves_email_status_alone(self) -> None:
        user = User.objects.create_user(username="recip", email="r@example.com", password="pw12345!")
        run = RecommendationRun.objects.create(user=user, status="done", email_status="pending")
        with patch("fragrance.tasks.render_and_send_email", side_effect=OSError("smtp down")):
            with patch.object(send_recommendation_email, "retry", side_effect=Retry()) as mock_retry:
                with self.assertRaises(Retry):
                    self._run(task=send_recommendation_email, retries=0, user_id=user.id, run_id=run.id)
        self.assertEqual(mock_retry.call_args.kwargs["countdown"], 300)
        run.refresh_from_db()
        self.assertEqual(run.email_status, "pending")
        self.assertIsNone(run.sent_at)

    def test_recommendation_email_marks_the_run_failed_once_retries_are_exhausted(self) -> None:
        user = User.objects.create_user(username="recip2", email="r2@example.com", password="pw12345!")
        run = RecommendationRun.objects.create(user=user, status="done", email_status="pending")
        with patch("fragrance.tasks.render_and_send_email", side_effect=OSError("smtp down")):
            with patch.object(send_recommendation_email, "retry") as mock_retry:
                with self.assertRaises(OSError):
                    self._run(
                        task=send_recommendation_email,
                        retries=send_recommendation_email.max_retries,
                        user_id=user.id,
                        run_id=run.id,
                    )
        mock_retry.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.email_status, "failed")
