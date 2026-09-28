from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from fragrance.ai_providers.verification import CheckResult
from fragrance.models import AIModelConfig
from rest_framework import status


class AIModelConfigAdminReadonlyFieldsTests(TestCase):
    """
    Regression test for .claude/plans/2026-08-30-admin-hardening-plan.md,
    Finding 3: verified_at/supports_strict_schema/supports_enum_enforcement
    must only ever be set by the verify_ai_model conformance harness, never
    hand-edited through the admin change form.
    """

    def setUp(self) -> None:
        # Migration 0006 seeds a default anthropic:claude-haiku-4-5-20251001
        # row into every fresh test database, which collides with this
        # test's own row on the (family, model_id) unique constraint.
        AIModelConfig.objects.all().delete()
        self.superuser = User.objects.create_superuser(
            username="hal", email="hal@example.com", password="hal-9000"
        )
        self.client.force_login(self.superuser)
        self.config = AIModelConfig.objects.create(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            is_active=False,
            supports_strict_schema=False,
            supports_enum_enforcement=False,
        )

    def _change_url(self) -> str:
        return reverse("admin:fragrance_aimodelconfig_change", args=[self.config.pk])

    def test_readonly_fields_are_not_rendered_as_editable_form_inputs(self) -> None:
        response = self.client.get(self._change_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        form_fields = response.context["adminform"].form.fields
        for field_name in (
            "verified_at",
            "supports_strict_schema",
            "supports_enum_enforcement",
        ):
            self.assertNotIn(field_name, form_fields)
        # is_active and model_id remain the intended operator controls.
        self.assertIn("is_active", form_fields)
        self.assertIn("model_id", form_fields)

    def test_post_changes_editable_fields_but_ignores_readonly_trust_fields(
        self,
    ) -> None:
        payload = {
            "family": "openai",
            "model_id": "gpt-4.1-mini",
            "max_tokens_default": 2048,
            "is_active": "on",
            "notes": "hand-edited via admin",
            # An attacker/careless admin attempting to inject these anyway —
            # since they're readonly, Django's ModelForm never binds them.
            "supports_strict_schema": "on",
            "supports_enum_enforcement": "on",
            "_save": "Save",
        }
        response = self.client.post(self._change_url(), payload)
        # A 302 (redirect to changelist) proves the POST actually succeeded,
        # not that it silently failed form validation and returned 200 with
        # errors while touching nothing.
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)

        self.config.refresh_from_db()
        self.assertEqual(self.config.family, "openai")
        self.assertEqual(self.config.model_id, "gpt-4.1-mini")
        self.assertEqual(self.config.max_tokens_default, 2048)
        self.assertTrue(self.config.is_active)
        self.assertEqual(self.config.notes, "hand-edited via admin")
        # The three trust fields must remain exactly as they were.
        self.assertFalse(self.config.supports_strict_schema)
        self.assertFalse(self.config.supports_enum_enforcement)
        self.assertIsNone(self.config.verified_at)


class AIModelConfigAdminSaveModelTrustResetTests(TestCase):
    """
    Regression coverage for 2026-09-05-ai-model-config-admin-verify-now.md,
    Finding 4 / 2026-09-05-ai-model-verification-admin-review.md Action Item
    3: a plain Save (not "Run conformance check now") that changes family or
    model_id must reset the three trust fields, since a verified_at/
    capability-flag combination only ever attests to the *previous*
    (family, model_id) pair. Unlike test_post_changes_editable_fields_but_
    ignores_readonly_trust_fields above (which starts from an all-False/None
    row and so can't distinguish "reset happened" from "was already False"),
    this class starts from a genuinely verified, active row.
    """

    def setUp(self) -> None:
        AIModelConfig.objects.all().delete()
        self.superuser = User.objects.create_superuser(
            username="hal", email="hal@example.com", password="hal-9000"
        )
        self.client.force_login(self.superuser)
        self.previously_verified_at = timezone.now() - timedelta(days=3)
        self.config = AIModelConfig.objects.create(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            is_active=True,
            supports_strict_schema=True,
            supports_enum_enforcement=True,
            verified_at=self.previously_verified_at,
        )

    def _change_url(self) -> str:
        return reverse("admin:fragrance_aimodelconfig_change", args=[self.config.pk])

    def _base_payload(self) -> dict:
        return {
            "family": self.config.family,
            "model_id": self.config.model_id,
            "max_tokens_default": self.config.max_tokens_default,
            "is_active": "on",
            "notes": self.config.notes,
            "_save": "Save",
        }

    def test_model_id_change_resets_trust_fields(self) -> None:
        payload = self._base_payload()
        payload["model_id"] = "claude-opus-5-unverified"
        response = self.client.post(self._change_url(), payload)
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)

        self.config.refresh_from_db()
        self.assertEqual(self.config.model_id, "claude-opus-5-unverified")
        self.assertIsNone(self.config.verified_at)
        self.assertFalse(self.config.supports_enum_enforcement)
        self.assertFalse(self.config.supports_strict_schema)

    def test_family_change_resets_trust_fields(self) -> None:
        payload = self._base_payload()
        payload["family"] = "openai"
        payload["model_id"] = "gpt-4.1-mini"
        response = self.client.post(self._change_url(), payload)
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)

        self.config.refresh_from_db()
        self.assertEqual(self.config.family, "openai")
        self.assertIsNone(self.config.verified_at)
        self.assertFalse(self.config.supports_enum_enforcement)
        self.assertFalse(self.config.supports_strict_schema)

    def test_unrelated_field_change_leaves_trust_fields_untouched(self) -> None:
        payload = self._base_payload()
        payload["notes"] = "operator note, no model change"
        response = self.client.post(self._change_url(), payload)
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)

        self.config.refresh_from_db()
        self.assertEqual(self.config.notes, "operator note, no model change")
        self.assertEqual(self.config.verified_at, self.previously_verified_at)
        self.assertTrue(self.config.supports_enum_enforcement)
        self.assertTrue(self.config.supports_strict_schema)

    def test_max_tokens_default_only_change_leaves_trust_fields_untouched(self) -> None:
        payload = self._base_payload()
        payload["max_tokens_default"] = 2048
        response = self.client.post(self._change_url(), payload)
        self.assertEqual(response.status_code, status.HTTP_302_FOUND)

        self.config.refresh_from_db()
        self.assertEqual(self.config.max_tokens_default, 2048)
        self.assertEqual(self.config.verified_at, self.previously_verified_at)
        self.assertTrue(self.config.supports_enum_enforcement)
        self.assertTrue(self.config.supports_strict_schema)


class AIModelConfigAdminVerifyNowViewTests(TestCase):
    """
    Covers the "Run conformance check now" button added to the AIModelConfig
    admin change page: it must require staff auth (the standard admin login
    gate covers this — no bespoke permission check was added), and it must
    delegate to the exact same shared verification functions used by
    manage.py verify_ai_model, persisting results the same way.
    """

    def setUp(self) -> None:
        AIModelConfig.objects.all().delete()
        self.staff_user = User.objects.create_superuser(
            username="hal", email="hal@example.com", password="hal-9000"
        )
        self.config = AIModelConfig.objects.create(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            is_active=False,
            supports_strict_schema=False,
            supports_enum_enforcement=False,
        )

    def _change_url(self) -> str:
        return reverse("admin:fragrance_aimodelconfig_change", args=[self.config.pk])

    def _post_verify_now(self) -> object:
        payload = {
            "family": self.config.family,
            "model_id": self.config.model_id,
            "max_tokens_default": self.config.max_tokens_default,
            "notes": self.config.notes,
            "_verify_now": "Run conformance check now",
        }
        return self.client.post(self._change_url(), payload, follow=True)

    def test_button_rendered_on_change_form_for_staff(self) -> None:
        self.client.force_login(self.staff_user)
        response = self.client.get(self._change_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertContains(response, 'name="_verify_now"')
        # The override replaces the submit_buttons_bottom block entirely —
        # this guards against a mistake there silently dropping the stock
        # Save button alongside adding the new one.
        self.assertContains(response, 'name="_save"')

    def test_unauthenticated_post_is_redirected_to_login_and_changes_nothing(
        self,
    ) -> None:
        with patch(
            "fragrance.admin.run_verification_checks"
        ) as mock_run_checks:
            response = self._post_verify_now()

        mock_run_checks.assert_not_called()
        # Anonymous users get bounced to the admin login page, never reach
        # response_change.
        self.assertIn("login", response.redirect_chain[-1][0])
        self.config.refresh_from_db()
        self.assertIsNone(self.config.verified_at)
        self.assertFalse(self.config.supports_enum_enforcement)
        self.assertFalse(self.config.supports_strict_schema)

    def test_authenticated_non_staff_post_is_redirected_to_login_and_changes_nothing(
        self,
    ) -> None:
        non_staff_user = User.objects.create_user(
            username="dave", password="hal-9000", is_staff=False
        )
        self.client.force_login(non_staff_user)
        with patch(
            "fragrance.admin.run_verification_checks"
        ) as mock_run_checks:
            response = self._post_verify_now()

        mock_run_checks.assert_not_called()
        # A logged-in but non-staff user is a distinct path from anonymous:
        # admin's staff_member_required gate rejects the session and
        # redirects to login rather than ever reaching response_change.
        self.assertIn("login", response.redirect_chain[-1][0])
        self.config.refresh_from_db()
        self.assertIsNone(self.config.verified_at)
        self.assertFalse(self.config.supports_enum_enforcement)
        self.assertFalse(self.config.supports_strict_schema)

    def test_full_pass_persists_flags_and_reports_success(self) -> None:
        self.client.force_login(self.staff_user)
        full_pass_results = [
            CheckResult(label="profile", passed=True, detail="ok"),
            CheckResult(label="candidates", passed=True, detail="ok"),
            CheckResult(label="replacement", passed=True, detail="ok"),
            CheckResult(label="email-content-with-enum", passed=True, detail="ok"),
        ]
        with (
            patch("fragrance.admin.registry.build_provider", return_value=object()),
            patch(
                "fragrance.admin.run_verification_checks",
                return_value=full_pass_results,
            ) as mock_run_checks,
        ):
            response = self._post_verify_now()

        mock_run_checks.assert_called_once()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        messages_text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("4/4", messages_text)

        self.config.refresh_from_db()
        self.assertIsNotNone(self.config.verified_at)
        self.assertTrue(self.config.supports_enum_enforcement)
        self.assertTrue(self.config.supports_strict_schema)

    def test_partial_pass_resets_flags_and_reports_warning(self) -> None:
        self.client.force_login(self.staff_user)
        previously_verified_at = timezone.now() - timedelta(days=3)
        self.config.supports_enum_enforcement = True
        self.config.supports_strict_schema = True
        self.config.verified_at = previously_verified_at
        self.config.save(
            update_fields=[
                "supports_enum_enforcement",
                "supports_strict_schema",
                "verified_at",
            ]
        )

        partial_pass_results = [
            CheckResult(label="profile", passed=True, detail="ok"),
            CheckResult(label="candidates", passed=False, detail="schema violation"),
            CheckResult(label="replacement", passed=True, detail="ok"),
            CheckResult(label="email-content-with-enum", passed=True, detail="ok"),
        ]
        with (
            patch("fragrance.admin.registry.build_provider", return_value=object()),
            patch(
                "fragrance.admin.run_verification_checks",
                return_value=partial_pass_results,
            ) as mock_run_checks,
        ):
            response = self._post_verify_now()

        mock_run_checks.assert_called_once()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        messages_text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("3/4", messages_text)

        self.config.refresh_from_db()
        # verified_at is untouched by a partial pass — neither cleared nor
        # re-stamped.
        self.assertEqual(self.config.verified_at, previously_verified_at)
        self.assertFalse(self.config.supports_enum_enforcement)
        self.assertFalse(self.config.supports_strict_schema)

    def test_inconclusive_check_reports_distinct_message_and_changes_nothing(
        self,
    ) -> None:
        # A connectivity/auth/rate-limit failure must not be able to
        # silently corrupt an already-verified active row's trust flags —
        # this is the exact production failure mode the pipeline (tasks.py's
        # registry.get_active_config(enum_required=True)) would otherwise
        # hard-fail on for an unrelated reason.
        self.client.force_login(self.staff_user)
        previously_verified_at = timezone.now() - timedelta(days=3)
        self.config.supports_enum_enforcement = True
        self.config.supports_strict_schema = True
        self.config.verified_at = previously_verified_at
        self.config.save(
            update_fields=[
                "supports_enum_enforcement",
                "supports_strict_schema",
                "verified_at",
            ]
        )

        inconclusive_results = [
            CheckResult(label="profile", passed=True, detail="ok"),
            CheckResult(
                label="candidates",
                passed=False,
                inconclusive=True,
                detail="unexpected error: ...",
            ),
            CheckResult(label="replacement", passed=True, detail="ok"),
            CheckResult(label="email-content-with-enum", passed=True, detail="ok"),
        ]
        with (
            patch("fragrance.admin.registry.build_provider", return_value=object()),
            patch(
                "fragrance.admin.run_verification_checks",
                return_value=inconclusive_results,
            ) as mock_run_checks,
        ):
            response = self._post_verify_now()

        mock_run_checks.assert_called_once()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        messages_text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("Could not complete conformance check", messages_text)
        self.assertIn("candidates", messages_text)
        # Neither the pass/fail phrasing of a partial pass, nor the
        # verified/success phrasing of a full pass, should appear.
        self.assertNotIn("Marked verified", messages_text)
        self.assertNotIn("Capability flags reset to False", messages_text)

        self.config.refresh_from_db()
        self.assertEqual(self.config.verified_at, previously_verified_at)
        self.assertTrue(self.config.supports_enum_enforcement)
        self.assertTrue(self.config.supports_strict_schema)
