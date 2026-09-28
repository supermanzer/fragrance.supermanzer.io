from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import anthropic
import httpx
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from fragrance.ai_providers.base import StructuredOutputError
from fragrance.ai_providers.verification import (
    CheckResult,
    PersistOutcome,
    persist_verification_results,
    run_verification_checks,
)
from fragrance.models import AIModelConfig
from fragrance.tests.support.llm_fakes import (
    _VALID_PROFILE_RESPONSE,
    _VALID_REPLACEMENT_RESPONSE,
    _ScriptedProvider,
    _ScriptedProviderWithErrors,
    _valid_candidates_response,
    _valid_email_content_response,
)


class VerifyAIModelCommandTests(TestCase):
    def setUp(self) -> None:
        # Migration 0006 seeds a default active row into every fresh test
        # database — these tests assert on the exact set of AIModelConfig
        # rows present (zero, one, or a specific pair/model_id) and must
        # start from an empty table.
        AIModelConfig.objects.all().delete()

    def _build_command_path(self) -> str:
        return "fragrance.management.commands.verify_ai_model.registry.build_provider"

    @override_settings(ANTHROPIC_API_KEY="")
    def test_missing_credential_raises_clear_command_error_not_a_traceback(
        self,
    ) -> None:
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "verify_ai_model", "anthropic", "claude-haiku-4-5-20251001"
            )
        self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))

    def test_unknown_family_rejected_by_argument_parsing(self) -> None:
        with self.assertRaises(CommandError):
            call_command("verify_ai_model", "not-a-real-family", "some-model")

    @override_settings(ANTHROPIC_API_KEY="sk-test")
    def test_full_pass_marks_matching_config_verified_with_yes_flag(self) -> None:
        AIModelConfig.objects.create(
            family="anthropic", model_id="claude-haiku-4-5-20251001", is_active=True
        )
        scripted = _ScriptedProvider(
            responses_by_tool={
                "create_profile": _VALID_PROFILE_RESPONSE,
                "select_candidates": _valid_candidates_response(),
                "select_replacement": _VALID_REPLACEMENT_RESPONSE,
                "generate_email_content": _valid_email_content_response(),
            }
        )
        out = StringIO()
        with patch(self._build_command_path(), return_value=scripted):
            call_command(
                "verify_ai_model",
                "anthropic",
                "claude-haiku-4-5-20251001",
                "--yes",
                stdout=out,
            )

        self.assertIn("4/4 schema checks passed", out.getvalue())
        config = AIModelConfig.objects.get(
            family="anthropic", model_id="claude-haiku-4-5-20251001"
        )
        self.assertIsNotNone(config.verified_at)

    @override_settings(ANTHROPIC_API_KEY="sk-test")
    def test_no_matching_config_row_is_reported_and_not_an_error(self) -> None:
        scripted = _ScriptedProvider(
            responses_by_tool={
                "create_profile": _VALID_PROFILE_RESPONSE,
                "select_candidates": _valid_candidates_response(),
                "select_replacement": _VALID_REPLACEMENT_RESPONSE,
                "generate_email_content": _valid_email_content_response(),
            }
        )
        out = StringIO()
        with patch(self._build_command_path(), return_value=scripted):
            call_command(
                "verify_ai_model",
                "anthropic",
                "claude-haiku-4-5-20251001",
                "--yes",
                stdout=out,
            )
        self.assertIn("No matching AIModelConfig row", out.getvalue())
        self.assertEqual(AIModelConfig.objects.count(), 0)

    @override_settings(ANTHROPIC_API_KEY="sk-test")
    def test_schema_violation_reported_as_failure_and_exits_nonzero(self) -> None:
        # 4 candidates violates CANDIDATES_SCHEMA's minItems/maxItems: 5 —
        # exactly the gap Task 4's app-level check exists for, here caught by
        # the harness's schema validator instead.
        scripted = _ScriptedProvider(
            responses_by_tool={
                "create_profile": _VALID_PROFILE_RESPONSE,
                "select_candidates": _valid_candidates_response(count=4),
                "select_replacement": _VALID_REPLACEMENT_RESPONSE,
                "generate_email_content": _valid_email_content_response(),
            }
        )
        out = StringIO()
        with patch(self._build_command_path(), return_value=scripted):
            with self.assertRaises(SystemExit):
                call_command(
                    "verify_ai_model",
                    "anthropic",
                    "claude-haiku-4-5-20251001",
                    stdout=out,
                )
        self.assertIn("FAIL", out.getvalue())
        self.assertIn("3/4 schema checks passed", out.getvalue())

    @override_settings(ANTHROPIC_API_KEY="sk-test")
    def test_full_pass_persists_enum_and_strict_schema_flags_alongside_verified_at(
        self,
    ) -> None:
        AIModelConfig.objects.create(
            family="anthropic", model_id="claude-haiku-4-5-20251001", is_active=True
        )
        scripted = _ScriptedProvider(
            responses_by_tool={
                "create_profile": _VALID_PROFILE_RESPONSE,
                "select_candidates": _valid_candidates_response(),
                "select_replacement": _VALID_REPLACEMENT_RESPONSE,
                "generate_email_content": _valid_email_content_response(),
            }
        )
        out = StringIO()
        with patch(self._build_command_path(), return_value=scripted):
            call_command(
                "verify_ai_model",
                "anthropic",
                "claude-haiku-4-5-20251001",
                "--yes",
                stdout=out,
            )

        config = AIModelConfig.objects.get(
            family="anthropic", model_id="claude-haiku-4-5-20251001"
        )
        self.assertIsNotNone(config.verified_at)
        # email-content-with-enum is part of a full 4/4 pass here, so both
        # capability flags land True.
        self.assertTrue(config.supports_enum_enforcement)
        self.assertTrue(config.supports_strict_schema)

    @override_settings(ANTHROPIC_API_KEY="sk-test")
    def test_partial_pass_resets_capability_flags_but_leaves_verified_at_untouched(
        self,
    ) -> None:
        previously_verified_at = timezone.now() - timedelta(days=3)
        AIModelConfig.objects.create(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            is_active=True,
            supports_enum_enforcement=True,
            supports_strict_schema=True,
            verified_at=previously_verified_at,
        )
        # 4 candidates violates CANDIDATES_SCHEMA's minItems/maxItems: 5 —
        # a partial (3/4) pass that must not leave the stale True flags from
        # a previous verification standing.
        scripted = _ScriptedProvider(
            responses_by_tool={
                "create_profile": _VALID_PROFILE_RESPONSE,
                "select_candidates": _valid_candidates_response(count=4),
                "select_replacement": _VALID_REPLACEMENT_RESPONSE,
                "generate_email_content": _valid_email_content_response(),
            }
        )
        out = StringIO()
        with patch(self._build_command_path(), return_value=scripted):
            with self.assertRaises(SystemExit):
                call_command(
                    "verify_ai_model",
                    "anthropic",
                    "claude-haiku-4-5-20251001",
                    stdout=out,
                )

        config = AIModelConfig.objects.get(
            family="anthropic", model_id="claude-haiku-4-5-20251001"
        )
        self.assertFalse(config.supports_enum_enforcement)
        self.assertFalse(config.supports_strict_schema)
        # verified_at is untouched by a partial pass — neither cleared nor
        # re-stamped.
        self.assertEqual(config.verified_at, previously_verified_at)

    @override_settings(ANTHROPIC_API_KEY="sk-test")
    def test_inconclusive_check_leaves_trust_fields_untouched_and_exits_nonzero(
        self,
    ) -> None:
        # A connectivity failure on one check must not be scored the same
        # as a genuine schema failure: it carries zero information about
        # this model's actual capability, so the pre-existing (real) trust
        # state must survive untouched.
        previously_verified_at = timezone.now() - timedelta(days=3)
        AIModelConfig.objects.create(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            is_active=True,
            supports_enum_enforcement=True,
            supports_strict_schema=True,
            verified_at=previously_verified_at,
        )
        connection_error = anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        scripted = _ScriptedProviderWithErrors(
            responses_by_tool={
                "create_profile": _VALID_PROFILE_RESPONSE,
                "select_candidates": connection_error,
                "select_replacement": _VALID_REPLACEMENT_RESPONSE,
                "generate_email_content": _valid_email_content_response(),
            }
        )
        out = StringIO()
        with patch(self._build_command_path(), return_value=scripted):
            with self.assertRaises(SystemExit):
                call_command(
                    "verify_ai_model",
                    "anthropic",
                    "claude-haiku-4-5-20251001",
                    stdout=out,
                )

        self.assertIn("Could not complete verification", out.getvalue())
        config = AIModelConfig.objects.get(
            family="anthropic", model_id="claude-haiku-4-5-20251001"
        )
        self.assertTrue(config.supports_enum_enforcement)
        self.assertTrue(config.supports_strict_schema)
        self.assertEqual(config.verified_at, previously_verified_at)

    @override_settings(ANTHROPIC_API_KEY="sk-test")
    def test_provider_rejected_request_is_a_definitive_failure_not_inconclusive(
        self,
    ) -> None:
        # An SDK BadRequestError (e.g. Anthropic 400ing a strict tool_choice
        # the model doesn't support) IS the signal the enum-enforcement
        # check exists to catch — it must reset stale flags like any other
        # genuine failure, not be treated as "we never found out".
        previously_verified_at = timezone.now() - timedelta(days=3)
        AIModelConfig.objects.create(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            is_active=True,
            supports_enum_enforcement=True,
            supports_strict_schema=True,
            verified_at=previously_verified_at,
        )
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(400, request=request, json={"error": {"message": "bad"}})
        bad_request_error = anthropic.BadRequestError(
            "strict tool mode not supported", response=response, body=None
        )
        scripted = _ScriptedProviderWithErrors(
            responses_by_tool={
                "create_profile": _VALID_PROFILE_RESPONSE,
                "select_candidates": _valid_candidates_response(),
                "select_replacement": _VALID_REPLACEMENT_RESPONSE,
                "generate_email_content": bad_request_error,
            }
        )
        out = StringIO()
        with patch(self._build_command_path(), return_value=scripted):
            with self.assertRaises(SystemExit):
                call_command(
                    "verify_ai_model",
                    "anthropic",
                    "claude-haiku-4-5-20251001",
                    stdout=out,
                )

        self.assertNotIn("Could not complete verification", out.getvalue())
        self.assertIn("3/4 schema checks passed", out.getvalue())
        config = AIModelConfig.objects.get(
            family="anthropic", model_id="claude-haiku-4-5-20251001"
        )
        self.assertFalse(config.supports_enum_enforcement)
        self.assertFalse(config.supports_strict_schema)
        self.assertEqual(config.verified_at, previously_verified_at)

    @override_settings(ANTHROPIC_API_KEY="sk-test")
    def test_bad_request_error_on_non_enum_check_leaves_trust_fields_untouched(
        self,
    ) -> None:
        # A BadRequestError on "profile" never asked for strict/enum mode,
        # so — unlike the email-content-with-enum case above — it must be
        # treated as inconclusive end to end: same "Could not complete
        # verification" messaging and untouched trust fields as a plain
        # connectivity failure. This is the exact shape of this project's
        # earlier "anthropic-workspace-id is required" production incident,
        # which 400ed on every call regardless of which check was running.
        previously_verified_at = timezone.now() - timedelta(days=3)
        AIModelConfig.objects.create(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            is_active=True,
            supports_enum_enforcement=True,
            supports_strict_schema=True,
            verified_at=previously_verified_at,
        )
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(400, request=request, json={"error": {"message": "bad"}})
        bad_request_error = anthropic.BadRequestError(
            "anthropic-workspace-id is required", response=response, body=None
        )
        scripted = _ScriptedProviderWithErrors(
            responses_by_tool={
                "create_profile": bad_request_error,
                "select_candidates": _valid_candidates_response(),
                "select_replacement": _VALID_REPLACEMENT_RESPONSE,
                "generate_email_content": _valid_email_content_response(),
            }
        )
        out = StringIO()
        with patch(self._build_command_path(), return_value=scripted):
            with self.assertRaises(SystemExit):
                call_command(
                    "verify_ai_model",
                    "anthropic",
                    "claude-haiku-4-5-20251001",
                    stdout=out,
                )

        self.assertIn("Could not complete verification", out.getvalue())
        config = AIModelConfig.objects.get(
            family="anthropic", model_id="claude-haiku-4-5-20251001"
        )
        self.assertTrue(config.supports_enum_enforcement)
        self.assertTrue(config.supports_strict_schema)
        self.assertEqual(config.verified_at, previously_verified_at)

    @override_settings(ANTHROPIC_API_KEY="sk-test")
    def test_bad_request_error_on_all_four_checks_is_inconclusive_not_partial_fail(
        self,
    ) -> None:
        # This is this project's actual earlier production incident: a
        # misconfigured "anthropic-workspace-id is required" 400 fired on
        # every single call, regardless of which check was running. Before
        # this fix, all four checks were scored as definitive failures
        # (0/4 pass -> partial_fail), which reset both capability flags to
        # False on real trust state built from a genuine prior verification.
        # After this fix, three of the four checks (profile, candidates,
        # replacement) are correctly recognized as carrying no capability
        # signal and marked inconclusive, so persist_verification_results'
        # `any(result.inconclusive for result in results)` guard fires and
        # nothing is touched.
        previously_verified_at = timezone.now() - timedelta(days=3)
        AIModelConfig.objects.create(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            is_active=True,
            supports_enum_enforcement=True,
            supports_strict_schema=True,
            verified_at=previously_verified_at,
        )
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(400, request=request, json={"error": {"message": "bad"}})
        bad_request_error = anthropic.BadRequestError(
            "anthropic-workspace-id is required", response=response, body=None
        )
        scripted = _ScriptedProviderWithErrors(
            responses_by_tool={
                "create_profile": bad_request_error,
                "select_candidates": bad_request_error,
                "select_replacement": bad_request_error,
                "generate_email_content": bad_request_error,
            }
        )
        out = StringIO()
        with patch(self._build_command_path(), return_value=scripted):
            with self.assertRaises(SystemExit):
                call_command(
                    "verify_ai_model",
                    "anthropic",
                    "claude-haiku-4-5-20251001",
                    stdout=out,
                )

        self.assertIn("Could not complete verification", out.getvalue())
        config = AIModelConfig.objects.get(
            family="anthropic", model_id="claude-haiku-4-5-20251001"
        )
        self.assertTrue(config.supports_enum_enforcement)
        self.assertTrue(config.supports_strict_schema)
        self.assertEqual(config.verified_at, previously_verified_at)


class RunVerificationChecksClassificationTests(TestCase):
    """
    Unit-level coverage of run_verification_checks' three-way exception
    classification, independent of either caller (CLI/admin).
    """

    def test_generic_sdk_exception_is_marked_inconclusive(self) -> None:
        connection_error = anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        provider = _ScriptedProviderWithErrors(
            responses_by_tool={
                "create_profile": connection_error,
                "select_candidates": _valid_candidates_response(),
                "select_replacement": _VALID_REPLACEMENT_RESPONSE,
                "generate_email_content": _valid_email_content_response(),
            }
        )
        results = run_verification_checks(provider=provider)
        profile_result = next(r for r in results if r.label == "profile")
        self.assertFalse(profile_result.passed)
        self.assertTrue(profile_result.inconclusive)

    def test_bad_request_error_on_non_enum_check_is_marked_inconclusive(self) -> None:
        # BadRequestError on "profile" never asked for strict/enum mode (only
        # ENUM_CHECK_LABEL does), so a 400 here carries zero signal about
        # schema capability — exactly the misconfiguration class this
        # project's earlier "anthropic-workspace-id is required" incident
        # belonged to. It must be routed to inconclusive=True, not scored as
        # a definitive capability failure.
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(400, request=request, json={"error": {"message": "bad"}})
        bad_request_error = anthropic.BadRequestError(
            "bad request", response=response, body=None
        )
        provider = _ScriptedProviderWithErrors(
            responses_by_tool={
                "create_profile": bad_request_error,
                "select_candidates": _valid_candidates_response(),
                "select_replacement": _VALID_REPLACEMENT_RESPONSE,
                "generate_email_content": _valid_email_content_response(),
            }
        )
        results = run_verification_checks(provider=provider)
        profile_result = next(r for r in results if r.label == "profile")
        self.assertFalse(profile_result.passed)
        self.assertTrue(profile_result.inconclusive)

    def test_bad_request_error_on_enum_check_is_a_definitive_failure_not_inconclusive(
        self,
    ) -> None:
        # BadRequestError specifically on email-content-with-enum (the one
        # check that requests strict/enum-constrained tool mode) IS the
        # signal this harness exists to catch — it must remain a definitive
        # failure, not inconclusive.
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(400, request=request, json={"error": {"message": "bad"}})
        bad_request_error = anthropic.BadRequestError(
            "strict tool mode not supported", response=response, body=None
        )
        provider = _ScriptedProviderWithErrors(
            responses_by_tool={
                "create_profile": _VALID_PROFILE_RESPONSE,
                "select_candidates": _valid_candidates_response(),
                "select_replacement": _VALID_REPLACEMENT_RESPONSE,
                "generate_email_content": bad_request_error,
            }
        )
        results = run_verification_checks(provider=provider)
        email_result = next(r for r in results if r.label == "email-content-with-enum")
        self.assertFalse(email_result.passed)
        self.assertFalse(email_result.inconclusive)

    def test_structured_output_error_is_a_definitive_failure_not_inconclusive(
        self,
    ) -> None:
        provider = _ScriptedProviderWithErrors(
            responses_by_tool={
                "create_profile": StructuredOutputError("malformed"),
                "select_candidates": _valid_candidates_response(),
                "select_replacement": _VALID_REPLACEMENT_RESPONSE,
                "generate_email_content": _valid_email_content_response(),
            }
        )
        results = run_verification_checks(provider=provider)
        profile_result = next(r for r in results if r.label == "profile")
        self.assertFalse(profile_result.passed)
        self.assertFalse(profile_result.inconclusive)

    def test_schema_violation_is_a_definitive_failure_not_inconclusive(self) -> None:
        provider = _ScriptedProviderWithErrors(
            responses_by_tool={
                "create_profile": _VALID_PROFILE_RESPONSE,
                "select_candidates": _valid_candidates_response(count=4),
                "select_replacement": _VALID_REPLACEMENT_RESPONSE,
                "generate_email_content": _valid_email_content_response(),
            }
        )
        results = run_verification_checks(provider=provider)
        candidates_result = next(r for r in results if r.label == "candidates")
        self.assertFalse(candidates_result.passed)
        self.assertFalse(candidates_result.inconclusive)


class PersistVerificationResultsTests(TestCase):
    """
    Unit-level coverage of persist_verification_results' three possible
    outcomes, independent of either caller (CLI/admin).
    """

    def setUp(self) -> None:
        AIModelConfig.objects.all().delete()

    def _full_pass_results(self) -> list[CheckResult]:
        return [
            CheckResult(label="profile", passed=True, detail="ok"),
            CheckResult(label="candidates", passed=True, detail="ok"),
            CheckResult(label="replacement", passed=True, detail="ok"),
            CheckResult(label="email-content-with-enum", passed=True, detail="ok"),
        ]

    def test_no_matching_row_returns_none_config_and_that_status(self) -> None:
        outcome = persist_verification_results(
            family="anthropic", model_id="does-not-exist", results=self._full_pass_results()
        )
        self.assertIsInstance(outcome, PersistOutcome)
        self.assertEqual(outcome.status, "no_matching_row")
        self.assertIsNone(outcome.config)

    def test_inconclusive_result_touches_nothing_even_on_the_active_row(self) -> None:
        previously_verified_at = timezone.now() - timedelta(days=3)
        AIModelConfig.objects.create(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            is_active=True,
            supports_enum_enforcement=True,
            supports_strict_schema=True,
            verified_at=previously_verified_at,
        )
        results = [
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
        outcome = persist_verification_results(
            family="anthropic", model_id="claude-haiku-4-5-20251001", results=results
        )
        self.assertEqual(outcome.status, "inconclusive")
        outcome.config.refresh_from_db()
        self.assertTrue(outcome.config.supports_enum_enforcement)
        self.assertTrue(outcome.config.supports_strict_schema)
        self.assertEqual(outcome.config.verified_at, previously_verified_at)

    def test_full_pass_returns_that_status_and_stamps_the_row(self) -> None:
        AIModelConfig.objects.create(
            family="anthropic", model_id="claude-haiku-4-5-20251001", is_active=True
        )
        outcome = persist_verification_results(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            results=self._full_pass_results(),
        )
        self.assertEqual(outcome.status, "full_pass")
        self.assertIsNotNone(outcome.config.verified_at)
        self.assertTrue(outcome.config.supports_enum_enforcement)
        self.assertTrue(outcome.config.supports_strict_schema)

    def test_partial_fail_returns_that_status_and_resets_flags(self) -> None:
        AIModelConfig.objects.create(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            is_active=True,
            supports_enum_enforcement=True,
            supports_strict_schema=True,
        )
        results = [
            CheckResult(label="profile", passed=True, detail="ok"),
            CheckResult(label="candidates", passed=False, detail="schema violation"),
            CheckResult(label="replacement", passed=True, detail="ok"),
            CheckResult(label="email-content-with-enum", passed=True, detail="ok"),
        ]
        outcome = persist_verification_results(
            family="anthropic", model_id="claude-haiku-4-5-20251001", results=results
        )
        self.assertEqual(outcome.status, "partial_fail")
        self.assertFalse(outcome.config.supports_enum_enforcement)
        self.assertFalse(outcome.config.supports_strict_schema)
