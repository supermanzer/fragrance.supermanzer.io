import importlib
import json
import re
import threading
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import openai
import redis
import stripe
from django.apps import apps as django_apps
from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache, caches
from django.core.cache.backends.locmem import LocMemCache
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import (
    Client,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.urls import resolve, reverse
from django.urls.exceptions import Resolver404
from django.utils import timezone
from django.views.debug import SafeExceptionReporterFilter
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from config.debug import AdminHardeningExceptionReporterFilter
from config.settings import _secure_proxy_ssl_header, _validated_admin_url

from .ai_providers import registry
from .ai_providers.anthropic_provider import AnthropicProvider
from .ai_providers.base import PROVIDER_TIMEOUT_SECONDS, StructuredLLMProvider, StructuredOutputError
from .ai_providers.openai_provider import OpenAIProvider, _to_strict_schema
from .ai_providers.qwen_provider import DASHSCOPE_BASE_URL_DEFAULT, QwenProvider
from .ai_providers.verification import (
    CheckResult,
    PersistOutcome,
    persist_verification_results,
    run_verification_checks,
)
from .llm import (
    CandidateCountError,
    build_email_content_tool_schema,
    generate_email_content,
    select_candidates,
    select_replacement,
)
from .models import (
    ActiveAIModelConfigConflictError,
    AIModelConfig,
    BetaSignup,
    Fragrance,
    FragranceConfig,
    Plan,
    PreferenceProfile,
    ProcessedPayment,
    Recommendation,
    RecommendationRun,
    StripeEvent,
    Subscription,
)
from .services import (
    BETA_INVITE_MAX_AGE,
    QuotaExceeded,
    RunInProgress,
    default_schedule_for,
    start_recommendation_run,
    derive_unique_username,
    generate_beta_invite_token,
    generate_recommendation_token,
    normalize_email,
    quota_snapshot,
    verify_beta_invite_token,
)
from .billing import (
    ComplimentaryGrantError,
    grant_complimentary_access,
    SessionNotFoundError,
    SessionOwnershipError,
    fulfill_checkout_session,
    fulfill_payment_intent,
    revoke_for_dispute,
)
from .tasks import (
    monthly_fragrance_run,
    process_stripe_event,
    render_and_send_email,
    send_beta_invite_email,
    send_password_reset_email,
)
from .views import BetaSignupThrottle, FailOpenScopedRateThrottle

def active_subscription_for(
    user: User, *, plan: Plan | None = None, days: int = 30
) -> Subscription:
    """Give a test user an active, unexpired subscription (the reusable factory)."""
    subscription, _ = Subscription.objects.get_or_create(user=user)
    subscription.plan = plan
    subscription.status = "active"
    subscription.access_expires_at = timezone.now() + timedelta(days=days)
    subscription.save()
    return subscription


# Migration module filenames start with digits, which aren't valid dotted-
# import identifiers — importlib is how Django's own migration loader
# imports them too.
_seed_default_anthropic_config_migration = importlib.import_module(
    "fragrance.migrations.0006_seed_default_anthropic_config"
)


class AIModelConfigTests(TestCase):
    def setUp(self) -> None:
        # Migration 0006 seeds a default anthropic:claude-haiku-4-5-20251001
        # row into every fresh test database — these tests need a genuinely
        # empty table to assert their own preconditions, not the migration's.
        AIModelConfig.objects.all().delete()

    def test_activating_a_new_row_deactivates_the_previously_active_one(self) -> None:
        first = AIModelConfig.objects.create(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            is_active=True,
            supports_enum_enforcement=True,
        )
        second = AIModelConfig.objects.create(
            family="openai",
            model_id="gpt-4.1-mini",
            is_active=True,
            supports_enum_enforcement=True,
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)
        self.assertEqual(AIModelConfig.objects.filter(is_active=True).count(), 1)

    def test_saving_inactive_row_does_not_disturb_active_row(self) -> None:
        active = AIModelConfig.objects.create(
            family="anthropic", model_id="claude-haiku-4-5-20251001", is_active=True
        )
        inactive = AIModelConfig.objects.create(
            family="qwen", model_id="qwen-max", is_active=False
        )
        inactive.notes = "updated"
        inactive.save(update_fields=["notes"])

        active.refresh_from_db()
        self.assertTrue(active.is_active)
        self.assertEqual(AIModelConfig.objects.filter(is_active=True).count(), 1)


class AIModelConfigConcurrencyTests(TransactionTestCase):
    """
    Regression test for the exclusivity race traced in .claude/reports/
    2026-08-30-ai-provider-abstraction-review.md, finding 5: two transactions
    activating two different, already-existing rows near-simultaneously used
    to both end up committed with is_active=True, because a blocked
    transaction's cleanup UPDATE only re-checks rows it already matched at
    the start of its own statement — never rows that only become active
    after the other transaction commits. The partial unique index added in
    migration 0005, combined with save() now deactivating other rows before
    activating self, closes this: at most one row can ever be active, and
    the losing transaction gets a clear ActiveAIModelConfigConflictError
    instead of silently succeeding.

    Requires TransactionTestCase (real commits across threads, each with its
    own DB connection) — a TestCase's single wrapped, uncommitted transaction
    can't reproduce cross-transaction lock contention, which is the actual
    mechanism this bug (and its fix) depends on.
    """

    def test_two_existing_rows_activated_concurrently_leaves_exactly_one_active(
        self,
    ) -> None:
        row_a = AIModelConfig.objects.create(family="anthropic", model_id="model-a")
        row_b = AIModelConfig.objects.create(family="openai", model_id="model-b")
        AIModelConfig.objects.create(
            family="qwen", model_id="model-c", is_active=True
        )

        # None = save() succeeded; otherwise the exception class name.
        # Depending on how much the two transactions actually overlap (real
        # thread/DB-connection timing, not fully controllable from Python),
        # either both saves can succeed — if they end up fully serialized,
        # the second one's deactivate-step legitimately sees the first's
        # already-committed row and turns it off itself, no conflict — or
        # one can hit ActiveAIModelConfigConflictError. Both are correct
        # outcomes of the fix; what must never happen, in either case, is
        # ending up with two committed active rows or any *other* exception
        # type leaking out unsanitized.
        outcomes: list[tuple[str, str | None]] = []
        outcomes_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def _activate(pk: int, label: str) -> None:
            from django.db import connection

            try:
                barrier.wait()
                row = AIModelConfig.objects.get(pk=pk)
                row.is_active = True
                try:
                    row.save(update_fields=["is_active"])
                    outcome = None
                except Exception as exc:  # noqa: BLE001 - test wants the type
                    outcome = type(exc).__name__
                with outcomes_lock:
                    outcomes.append((label, outcome))
            finally:
                # Each thread opens its own DB connection; only the main
                # thread's connection is auto-closed after a test.
                connection.close()

        threads = [
            threading.Thread(target=_activate, args=(row_a.pk, "a")),
            threading.Thread(target=_activate, args=(row_b.pk, "b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(outcomes), 2)
        for _, outcome in outcomes:
            self.assertIn(outcome, (None, "ActiveAIModelConfigConflictError"))
        # The invariant that actually matters: never two committed active
        # rows, regardless of which interleaving occurred above.
        self.assertEqual(AIModelConfig.objects.filter(is_active=True).count(), 1)


class SeedDefaultAnthropicConfigMigrationTests(TestCase):
    """
    Exercises migration 0006's RunPython functions directly (calling them
    with the live app registry, not a full MigrationExecutor state replay)
    rather than actually migrating the test DB backward and forward. This
    project has no migration-testing package installed, and juggling real
    schema-history state via MigrationExecutor inside a TransactionTestCase
    (which flushes django_migrations along with every other table on
    teardown) is a known source of test-suite-ordering fragility for a
    migration that has no schema operations to begin with — 0006 is
    RunPython-only, so its actual behavior *is* "call this function against
    a real AIModelConfig table," which is what these tests do directly.
    """

    def setUp(self) -> None:
        # The test DB already carries this same migration's own seeded row
        # (applied once, at test-DB creation time, like every other
        # migration) — clear it so "fresh table" here means what it says,
        # not "whatever this migration already did during DB setup."
        AIModelConfig.objects.all().delete()

    def test_fresh_table_seeds_exactly_the_expected_row(self) -> None:
        self.assertEqual(AIModelConfig.objects.count(), 0)

        _seed_default_anthropic_config_migration.seed_default_anthropic_config(
            django_apps, None
        )

        rows = list(AIModelConfig.objects.all())
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.family, "anthropic")
        self.assertEqual(row.model_id, "claude-haiku-4-5-20251001")
        self.assertTrue(row.is_active)
        self.assertTrue(row.supports_strict_schema)
        self.assertTrue(row.supports_enum_enforcement)
        self.assertIsNone(row.verified_at)
        self.assertIn(
            _seed_default_anthropic_config_migration.SEED_NOTE_MARKER, row.notes
        )

    def test_already_populated_table_is_a_genuine_no_op(self) -> None:
        # Mirrors dev: a row already exists (here, standing in for the one
        # Task 5's manual verify_ai_model run created) before this migration
        # ever runs.
        existing = AIModelConfig.objects.create(
            family="openai", model_id="gpt-4.1-mini", is_active=True
        )

        _seed_default_anthropic_config_migration.seed_default_anthropic_config(
            django_apps, None
        )

        self.assertEqual(AIModelConfig.objects.count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.family, "openai")
        self.assertTrue(existing.is_active)

    def test_reverse_deletes_only_the_row_it_seeded(self) -> None:
        _seed_default_anthropic_config_migration.seed_default_anthropic_config(
            django_apps, None
        )

        _seed_default_anthropic_config_migration.remove_seeded_default_anthropic_config(
            django_apps, None
        )

        self.assertEqual(AIModelConfig.objects.count(), 0)

    def test_reverse_does_not_delete_a_row_an_admin_has_since_edited(self) -> None:
        _seed_default_anthropic_config_migration.seed_default_anthropic_config(
            django_apps, None
        )
        seeded = AIModelConfig.objects.get()
        seeded.notes = "an admin edited this row after it was seeded"
        seeded.save(update_fields=["notes"])

        _seed_default_anthropic_config_migration.remove_seeded_default_anthropic_config(
            django_apps, None
        )

        self.assertEqual(AIModelConfig.objects.count(), 1)


class _FakeProvider:
    """
    Minimal StructuredLLMProvider stand-in for pipeline tests: returns a fixed
    response and records every call's kwargs so tests can assert on the exact
    schema/prompt sent, without depending on any real SDK client.
    """

    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[dict] = []

    def generate_structured(
        self,
        *,
        prompt: str,
        tool_name: str,
        description: str,
        schema: dict,
        max_tokens: int,
    ) -> dict:
        self.calls.append(
            {
                "prompt": prompt,
                "tool_name": tool_name,
                "description": description,
                "schema": schema,
                "max_tokens": max_tokens,
            }
        )
        return self.response


class BuildEmailContentToolSchemaTests(TestCase):
    def test_enum_contains_exact_confirmed_names(self) -> None:
        schema = build_email_content_tool_schema(
            confirmed_names=["Aventus", "Bleu de Chanel"]
        )
        name_schema = schema["properties"]["rationales"]["items"]["properties"][
            "name"
        ]
        self.assertEqual(name_schema["enum"], ["Aventus", "Bleu de Chanel"])
        # Canonical schema is the inner JSON Schema only — no vendor envelope
        # ("input_schema"/"strict") leaks in; each adapter assembles its own.
        self.assertNotIn("input_schema", schema)
        self.assertNotIn("strict", schema)

    def test_no_enum_key_when_no_confirmed_names(self) -> None:
        schema = build_email_content_tool_schema(confirmed_names=[])
        name_schema = schema["properties"]["rationales"]["items"]["properties"][
            "name"
        ]
        self.assertNotIn("enum", name_schema)


def _fake_anthropic_response(input_data: dict) -> SimpleNamespace:
    tool_use_block = SimpleNamespace(type="tool_use", input=input_data)
    return SimpleNamespace(content=[tool_use_block], stop_reason="tool_use")


def _fake_openai_response(data: dict, refusal: str | None = None) -> SimpleNamespace:
    message = SimpleNamespace(
        content=None if refusal else json.dumps(data), refusal=refusal
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class ProviderProtocolConformanceTests(TestCase):
    def test_all_adapters_satisfy_structured_llm_provider(self) -> None:
        anthropic_provider = AnthropicProvider(model_id="claude-x", api_key="k")
        openai_provider = OpenAIProvider(model_id="gpt-x", api_key="k")
        qwen_provider = QwenProvider(model_id="qwen-x", api_key="k")
        for provider in (anthropic_provider, openai_provider, qwen_provider):
            self.assertIsInstance(provider, StructuredLLMProvider)


class AnthropicProviderTests(TestCase):
    def test_generate_structured_forces_tool_choice_and_parses_result(self) -> None:
        provider = AnthropicProvider(model_id="claude-haiku-4-5-20251001", api_key="k")
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _fake_anthropic_response(
            {"foo": "bar"}
        )
        provider._client = mock_client

        result = provider.generate_structured(
            prompt="do the thing",
            tool_name="do_thing",
            description="Does the thing.",
            schema={"type": "object", "properties": {"foo": {"type": "string"}}},
            max_tokens=256,
        )

        self.assertEqual(result, {"foo": "bar"})
        call_kwargs = mock_client.messages.create.call_args.kwargs
        self.assertEqual(
            call_kwargs["tool_choice"], {"type": "tool", "name": "do_thing"}
        )
        self.assertEqual(call_kwargs["tools"][0]["name"], "do_thing")
        self.assertEqual(
            call_kwargs["tools"][0]["input_schema"]["properties"]["foo"]["type"],
            "string",
        )
        self.assertEqual(call_kwargs["max_tokens"], 256)

    def test_missing_tool_use_block_raises_structured_output_error(self) -> None:
        provider = AnthropicProvider(model_id="claude-haiku-4-5-20251001", api_key="k")
        mock_client = MagicMock()
        mock_client.messages.create.return_value = SimpleNamespace(
            content=[], stop_reason="end_turn"
        )
        provider._client = mock_client

        with self.assertRaises(StructuredOutputError):
            provider.generate_structured(
                prompt="p", tool_name="t", description="d", schema={}, max_tokens=10
            )

    def test_get_client_passes_explicit_timeout_to_sdk_constructor(self) -> None:
        # Gunicorn's --timeout 120 (docker-compose.yaml) is the only backstop
        # if the SDK's own 600s default is left in place; this must be
        # comfortably shorter so a hung call fails fast into a reported
        # CheckResult instead of a killed worker.
        provider = AnthropicProvider(model_id="claude-haiku-4-5-20251001", api_key="k")
        with patch("fragrance.ai_providers.anthropic_provider.anthropic.Anthropic") as mock_ctor:
            provider._get_client()
        self.assertEqual(mock_ctor.call_args.kwargs["timeout"], PROVIDER_TIMEOUT_SECONDS)
        self.assertLess(PROVIDER_TIMEOUT_SECONDS, 120.0)


class OpenAIProviderTests(TestCase):
    def test_to_strict_schema_adds_additional_properties_and_full_required(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "candidates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "house": {"type": "string"},
                        },
                        "required": ["name", "house"],
                    },
                    "minItems": 5,
                    "maxItems": 5,
                }
            },
            "required": ["candidates"],
        }
        strict = _to_strict_schema(schema=schema)

        self.assertEqual(strict["additionalProperties"], False)
        self.assertEqual(strict["required"], ["candidates"])
        items_schema = strict["properties"]["candidates"]["items"]
        self.assertEqual(items_schema["additionalProperties"], False)
        self.assertNotIn("minItems", strict["properties"]["candidates"])
        self.assertNotIn("maxItems", strict["properties"]["candidates"])
        # original untouched — _to_strict_schema must deep-copy, not mutate.
        self.assertIn("minItems", schema["properties"]["candidates"])

    def test_generate_structured_sends_strict_json_schema_and_parses_result(self) -> None:
        provider = OpenAIProvider(model_id="gpt-4.1-mini", api_key="k")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_openai_response(
            {"foo": "bar"}
        )
        provider._client = mock_client

        result = provider.generate_structured(
            prompt="do the thing",
            tool_name="do_thing",
            description="Does the thing.",
            schema={"type": "object", "properties": {"foo": {"type": "string"}}},
            max_tokens=256,
        )

        self.assertEqual(result, {"foo": "bar"})
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        response_format = call_kwargs["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        self.assertEqual(response_format["json_schema"]["name"], "do_thing")
        self.assertEqual(
            response_format["json_schema"]["schema"]["additionalProperties"], False
        )

    def test_generate_structured_sends_max_completion_tokens_not_max_tokens(
        self,
    ) -> None:
        # max_tokens is deprecated and rejected outright by OpenAI's o-series
        # reasoning models; max_completion_tokens is accepted by every OpenAI
        # chat model, so it must be the default and max_tokens must never be
        # sent (see .claude/reports/2026-08-30-ai-provider-abstraction-review.md,
        # finding A).
        provider = OpenAIProvider(model_id="o4-mini", api_key="k")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_openai_response(
            {"foo": "bar"}
        )
        provider._client = mock_client

        provider.generate_structured(
            prompt="do the thing",
            tool_name="do_thing",
            description="Does the thing.",
            schema={"type": "object", "properties": {"foo": {"type": "string"}}},
            max_tokens=256,
        )

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertEqual(call_kwargs["max_completion_tokens"], 256)
        self.assertNotIn("max_tokens", call_kwargs)

    def test_token_param_name_is_overridable_per_instance(self) -> None:
        # Constructor-level override for a family/gateway that turns out not
        # to accept max_completion_tokens (e.g. an unverified DashScope
        # model) — QwenProvider composes over this without needing a code
        # change here.
        provider = OpenAIProvider(
            model_id="qwen-max", api_key="k", token_param_name="max_tokens"
        )
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_openai_response(
            {"foo": "bar"}
        )
        provider._client = mock_client

        provider.generate_structured(
            prompt="p", tool_name="t", description="d", schema={}, max_tokens=10
        )

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertEqual(call_kwargs["max_tokens"], 10)
        self.assertNotIn("max_completion_tokens", call_kwargs)

    def test_refusal_raises_structured_output_error(self) -> None:
        provider = OpenAIProvider(model_id="gpt-4.1-mini", api_key="k")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_openai_response(
            {}, refusal="I can't help with that."
        )
        provider._client = mock_client

        with self.assertRaises(StructuredOutputError):
            provider.generate_structured(
                prompt="p", tool_name="t", description="d", schema={}, max_tokens=10
            )

    def test_invalid_json_raises_structured_output_error(self) -> None:
        provider = OpenAIProvider(model_id="gpt-4.1-mini", api_key="k")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="not json", refusal=None)
                )
            ]
        )
        provider._client = mock_client

        with self.assertRaises(StructuredOutputError):
            provider.generate_structured(
                prompt="p", tool_name="t", description="d", schema={}, max_tokens=10
            )

    def test_get_client_passes_explicit_timeout_to_sdk_constructor(self) -> None:
        provider = OpenAIProvider(model_id="gpt-4.1-mini", api_key="k")
        with patch("fragrance.ai_providers.openai_provider.openai.OpenAI") as mock_ctor:
            provider._get_client()
        self.assertEqual(mock_ctor.call_args.kwargs["timeout"], PROVIDER_TIMEOUT_SECONDS)
        self.assertLess(PROVIDER_TIMEOUT_SECONDS, 120.0)


class QwenProviderTests(TestCase):
    def test_defaults_to_international_dashscope_base_url(self) -> None:
        provider = QwenProvider(model_id="qwen-max", api_key="k")
        self.assertEqual(provider._delegate._base_url, DASHSCOPE_BASE_URL_DEFAULT)

    @override_settings(DASHSCOPE_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1")
    def test_respects_settings_override(self) -> None:
        provider = QwenProvider(model_id="qwen-max", api_key="k")
        self.assertEqual(
            provider._delegate._base_url,
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

    def test_delegates_generate_structured_to_openai_provider(self) -> None:
        provider = QwenProvider(model_id="qwen-max", api_key="k")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _fake_openai_response(
            {"foo": "bar"}
        )
        provider._delegate._client = mock_client

        result = provider.generate_structured(
            prompt="p", tool_name="t", description="d", schema={}, max_tokens=10
        )
        self.assertEqual(result, {"foo": "bar"})

    def test_defaults_to_max_completion_tokens_like_openai(self) -> None:
        # Mocked, not a live DashScope call — per Task 7 item 1, a mocked
        # test is sufficient to close this; live `verify_ai_model qwen
        # <model_id>` verification is a follow-up once a real key exists.
        provider = QwenProvider(model_id="qwen-max", api_key="k")
        self.assertEqual(provider._delegate._token_param_name, "max_completion_tokens")

    def test_token_param_name_override_reaches_the_delegate(self) -> None:
        provider = QwenProvider(
            model_id="qwen-max", api_key="k", token_param_name="max_tokens"
        )
        self.assertEqual(provider._delegate._token_param_name, "max_tokens")

    def test_delegate_client_gets_the_explicit_timeout_too(self) -> None:
        # QwenProvider has no client construction of its own — it composes
        # over OpenAIProvider, so the shared timeout only reaches DashScope
        # calls if the delegate's own _get_client() applies it.
        provider = QwenProvider(model_id="qwen-max", api_key="k")
        with patch("fragrance.ai_providers.openai_provider.openai.OpenAI") as mock_ctor:
            provider._delegate._get_client()
        self.assertEqual(mock_ctor.call_args.kwargs["timeout"], PROVIDER_TIMEOUT_SECONDS)


class RegistryTests(TestCase):
    def setUp(self) -> None:
        # Migration 0006 seeds a default active row into every fresh test
        # database — these tests set up their own explicit active/inactive
        # AIModelConfig state and must start from an empty table.
        AIModelConfig.objects.all().delete()

    def test_no_active_config_raises_clear_error(self) -> None:
        with self.assertRaises(registry.NoActiveAIModelConfigError):
            registry.get_active_provider()

    @override_settings(ANTHROPIC_API_KEY="")
    def test_missing_credential_raises_clear_error(self) -> None:
        AIModelConfig.objects.create(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            is_active=True,
            supports_enum_enforcement=True,
        )
        with self.assertRaises(registry.MissingCredentialError):
            registry.get_active_provider()

    @override_settings(ANTHROPIC_API_KEY="sk-test")
    def test_enum_required_rejects_model_without_enum_support(self) -> None:
        AIModelConfig.objects.create(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            is_active=True,
            supports_enum_enforcement=False,
        )
        with self.assertRaises(registry.EnumEnforcementUnsupportedError):
            registry.get_active_provider(enum_required=True)

    @override_settings(ANTHROPIC_API_KEY="sk-test")
    def test_enum_required_accepts_model_with_enum_support(self) -> None:
        AIModelConfig.objects.create(
            family="anthropic",
            model_id="claude-haiku-4-5-20251001",
            is_active=True,
            supports_enum_enforcement=True,
        )
        provider = registry.get_active_provider(enum_required=True)
        self.assertIsInstance(provider, AnthropicProvider)

    @override_settings(ANTHROPIC_API_KEY="sk-test")
    def test_get_active_config_returns_the_active_row(self) -> None:
        active = AIModelConfig.objects.create(
            family="anthropic", model_id="claude-haiku-4-5-20251001", is_active=True
        )
        AIModelConfig.objects.create(
            family="openai", model_id="gpt-4.1-mini", is_active=False
        )
        self.assertEqual(registry.get_active_config().id, active.id)


class GenerateEmailContentTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="dave", password="hal-9000"
        )
        self.profile = PreferenceProfile.objects.create(
            user=self.user,
            loved_notes="oud, amber",
            liked_notes="citrus",
            disliked_notes="aquatic",
            owns_list=[],
            search_angle_1="niche oud fragrances",
            search_angle_2="amber fragrances 2026",
        )
        self.run = RecommendationRun.objects.create(
            user=self.user, profile=self.profile, status="running"
        )

    def _make_confirmed(self, name: str, house: str = "House") -> Recommendation:
        return Recommendation.objects.create(
            user=self.user,
            run=self.run,
            name=name,
            house=house,
            status="confirmed",
            rationale="",
        )

    def test_exact_match_rationale_assignment(self) -> None:
        rec1 = self._make_confirmed(name="Aventus")
        rec2 = self._make_confirmed(name="Bleu de Chanel")
        verified_picks = [
            {"name": "Aventus", "house": "House", "status": "confirmed"},
            {"name": "Bleu de Chanel", "house": "House", "status": "confirmed"},
        ]
        fake_result = {
            "intro": "Welcome back.",
            "rationales": [
                {"name": "Aventus", "rationale": "Smoky and bold."},
                {"name": "Bleu de Chanel", "rationale": "Clean and versatile."},
            ],
        }

        fake_provider = _FakeProvider(response=fake_result)
        generate_email_content(
            run_id=self.run.id, verified_picks=verified_picks, provider=fake_provider
        )

        rec1.refresh_from_db()
        rec2.refresh_from_db()
        self.assertEqual(rec1.rationale, "Smoky and bold.")
        self.assertEqual(rec2.rationale, "Clean and versatile.")

        self.run.refresh_from_db()
        self.assertEqual(self.run.intro, "Welcome back.")

        # The enum sent to the provider must be the exact confirmed names, in creation order.
        self.assertEqual(len(fake_provider.calls), 1)
        sent_schema = fake_provider.calls[0]["schema"]
        sent_enum = sent_schema["properties"]["rationales"]["items"]["properties"][
            "name"
        ]["enum"]
        self.assertEqual(sent_enum, ["Aventus", "Bleu de Chanel"])

    def test_missing_rationale_logs_warning_and_does_not_raise(self) -> None:
        rec1 = self._make_confirmed(name="Aventus")
        verified_picks = [{"name": "Aventus", "house": "House", "status": "confirmed"}]
        # LLM returns a paraphrased name that doesn't match the enum-constrained rec name.
        fake_result = {
            "intro": "Welcome back.",
            "rationales": [
                {"name": "Aventus Eau de Parfum", "rationale": "Smoky and bold."}
            ],
        }

        fake_provider = _FakeProvider(response=fake_result)
        with self.assertLogs("fragrance.llm", level="WARNING") as logs:
            generate_email_content(
                run_id=self.run.id,
                verified_picks=verified_picks,
                provider=fake_provider,
            )

        rec1.refresh_from_db()
        self.assertEqual(rec1.rationale, "")
        self.assertTrue(
            any("Aventus" in message for message in logs.output)
        )


class CandidateCountValidationTests(TestCase):
    """
    App-level guard for a pre-existing gap: no provider family reliably
    enforces the schema's minItems/maxItems at generation time (confirmed
    unsupported in OpenAI's strict-mode subset for our purposes; unconfirmed
    for Qwen; never enforced for Anthropic even pre-refactor).
    """

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", password="hal-9000")
        self.profile = PreferenceProfile.objects.create(
            user=self.user,
            loved_notes="oud",
            liked_notes="citrus",
            disliked_notes="aquatic",
            owns_list=[],
            search_angle_1="x",
            search_angle_2="y",
        )

    def test_four_candidates_rejected(self) -> None:
        provider = _FakeProvider(
            response={"candidates": [{"name": "A", "house": "H"}] * 4}
        )
        with self.assertRaises(CandidateCountError):
            select_candidates(
                user_id=self.user.id,
                profile_id=self.profile.id,
                search_results="",
                provider=provider,
            )

    def test_six_candidates_rejected(self) -> None:
        provider = _FakeProvider(
            response={"candidates": [{"name": "A", "house": "H"}] * 6}
        )
        with self.assertRaises(CandidateCountError):
            select_candidates(
                user_id=self.user.id,
                profile_id=self.profile.id,
                search_results="",
                provider=provider,
            )

    def test_exactly_five_candidates_accepted(self) -> None:
        provider = _FakeProvider(
            response={
                "candidates": [
                    {"name": f"Candidate {i}", "house": "H"} for i in range(5)
                ]
            }
        )
        result = select_candidates(
            user_id=self.user.id,
            profile_id=self.profile.id,
            search_results="",
            provider=provider,
        )
        self.assertEqual(len(result), 5)

    def test_select_replacement_missing_field_rejected(self) -> None:
        provider = _FakeProvider(response={"name": "Only Has A Name"})
        with self.assertRaises(CandidateCountError):
            select_replacement(
                user_id=self.user.id,
                failed_candidate={"name": "Failed", "house": "Nowhere"},
                search_results="",
                profile=self.profile,
                provider=provider,
            )


def _make_run_with_pick(
    user: User, name: str = "Aventus", house: str = "Creed", pick_status: str = "confirmed"
) -> Recommendation:
    run = RecommendationRun.objects.create(user=user, status="done")
    return Recommendation.objects.create(
        user=user, run=run, name=name, house=house, status=pick_status, rationale="Smoky and bold."
    )


class RecommendationRateViewTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", password="hal-9000")
        self.other_user = User.objects.create_user(username="frank", password="hal-9000")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _rate_url(self, pick_id: int) -> str:
        return f"/api/v1/recommendations/{pick_id}/rate/"

    def test_unauthenticated_request_rejected(self) -> None:
        rec = _make_run_with_pick(self.user)
        anon_client = APIClient()
        response = anon_client.post(self._rate_url(rec.id), {"action": "own"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_other_users_pick_returns_404(self) -> None:
        rec = _make_run_with_pick(self.other_user)
        response = self.client.post(self._rate_url(rec.id), {"action": "own"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_first_rating_creates_fragrance(self) -> None:
        rec = _make_run_with_pick(self.user)
        response = self.client.post(self._rate_url(rec.id), {"action": "own"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["action"], "own")
        self.assertEqual(response.data["name"], "Aventus")

        fragrance = Fragrance.objects.get(user=self.user, name="Aventus")
        self.assertEqual(fragrance.status, "own")
        self.assertEqual(fragrance.source_recommendation_id, rec.id)

    def test_resubmitting_same_action_is_idempotent(self) -> None:
        rec = _make_run_with_pick(self.user)
        self.client.post(self._rate_url(rec.id), {"action": "like"}, format="json")

        response = self.client.post(self._rate_url(rec.id), {"action": "like"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["already_rated"])
        self.assertEqual(Fragrance.objects.filter(user=self.user, name="Aventus").count(), 1)

    def test_resubmitting_different_action_returns_conflict(self) -> None:
        rec = _make_run_with_pick(self.user)
        self.client.post(self._rate_url(rec.id), {"action": "like"}, format="json")

        response = self.client.post(self._rate_url(rec.id), {"action": "dislike"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["existing_action"], "like")

        fragrance = Fragrance.objects.get(user=self.user, name="Aventus")
        self.assertEqual(fragrance.status, "like")

    def test_replaced_pick_cannot_be_rated(self) -> None:
        rec = _make_run_with_pick(self.user, pick_status="replaced")
        response = self.client.post(self._rate_url(rec.id), {"action": "own"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Fragrance.objects.filter(user=self.user).exists())

    def test_invalid_action_value_rejected(self) -> None:
        rec = _make_run_with_pick(self.user)
        response = self.client.post(self._rate_url(rec.id), {"action": "meh"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_name_collision_with_existing_collection_entry_succeeds(self) -> None:
        Fragrance.objects.create(user=self.user, name="Aventus", house="Old House", status="dislike")
        rec = _make_run_with_pick(self.user)

        response = self.client.post(self._rate_url(rec.id), {"action": "own"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Fragrance.objects.filter(user=self.user, name="Aventus").count(), 1)

        fragrance = Fragrance.objects.get(user=self.user, name="Aventus")
        self.assertEqual(fragrance.status, "own")
        self.assertEqual(fragrance.house, "Creed")
        self.assertEqual(fragrance.source_recommendation_id, rec.id)

    def test_second_recommendation_with_same_name_does_not_hijack_first(self) -> None:
        # Two distinct recommendations (different runs) sharing the same name — a
        # realistic LLM re-emission across monthly runs, not a contrived collision.
        first_rec = _make_run_with_pick(self.user)
        second_rec = _make_run_with_pick(self.user)
        self.assertNotEqual(first_rec.id, second_rec.id)

        first_response = self.client.post(self._rate_url(first_rec.id), {"action": "own"}, format="json")
        self.assertEqual(first_response.status_code, status.HTTP_201_CREATED)

        second_response = self.client.post(
            self._rate_url(second_rec.id), {"action": "dislike"}, format="json"
        )
        self.assertEqual(second_response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(second_response.data["existing_action"], "own")

        # The first recommendation's link and rating must survive untouched.
        fragrance = Fragrance.objects.get(user=self.user, name="Aventus")
        self.assertEqual(fragrance.status, "own")
        self.assertEqual(fragrance.source_recommendation_id, first_rec.id)
        first_rec.refresh_from_db()
        self.assertIsNotNone(getattr(first_rec, "promoted_fragrance", None))

    def test_rerating_after_fragrance_rename_is_still_idempotent(self) -> None:
        # Regression: apply_recommendation_rating must resolve "already rated" via
        # the FK link, not a (user, name) lookup that a rename can defeat.
        rec = _make_run_with_pick(self.user)
        response = self.client.post(self._rate_url(rec.id), {"action": "like"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        fragrance = Fragrance.objects.get(user=self.user, source_recommendation_id=rec.id)
        fragrance.name = "Aventus Renamed"
        fragrance.save(update_fields=["name"])

        response = self.client.post(self._rate_url(rec.id), {"action": "like"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["already_rated"])
        self.assertEqual(Fragrance.objects.filter(user=self.user).count(), 1)

    def test_rerating_after_fragrance_rename_with_different_action_returns_conflict(self) -> None:
        rec = _make_run_with_pick(self.user)
        response = self.client.post(self._rate_url(rec.id), {"action": "like"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        fragrance = Fragrance.objects.get(user=self.user, source_recommendation_id=rec.id)
        fragrance.name = "Aventus Renamed"
        fragrance.save(update_fields=["name"])

        response = self.client.post(self._rate_url(rec.id), {"action": "dislike"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["existing_action"], "like")

        fragrance.refresh_from_db()
        self.assertEqual(fragrance.status, "like")
        self.assertEqual(fragrance.name, "Aventus Renamed")


class RecommendationConfirmViewTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", password="hal-9000")
        self.other_user = User.objects.create_user(username="frank", password="hal-9000")
        self.client = APIClient()  # deliberately unauthenticated throughout

    def _token(self, rec: Recommendation, action: str = "own") -> str:
        return generate_recommendation_token(recommendation=rec, action=action)

    def _get(self, token: str):
        return self.client.get(f"/api/v1/recommendations/confirm/?t={token}")

    def _post(self, token: str):
        return self.client.post(f"/api/v1/recommendations/confirm/?t={token}")

    def test_preview_works_without_authorization_header(self) -> None:
        rec = _make_run_with_pick(self.user)
        response = self._get(self._token(rec))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["already_rated"])
        self.assertIsNone(response.data["rated_as"])
        self.assertEqual(response.data["name"], "Aventus")

    def test_preview_reports_existing_rating(self) -> None:
        rec = _make_run_with_pick(self.user)
        Fragrance.objects.create(
            user=self.user, name=rec.name, house=rec.house, status="like", source_recommendation=rec
        )
        response = self._get(self._token(rec, action="own"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["already_rated"])
        self.assertEqual(response.data["rated_as"], "like")
        self.assertEqual(response.data["action"], "own")

    def test_token_attribution_is_only_from_signed_payload(self) -> None:
        rec = _make_run_with_pick(self.user)
        # A token whose uid claims a different owner than the recommendation's real
        # user_id must not resolve — attribution comes only from the signed payload.
        from django.core import signing

        from .services import RECOMMENDATION_FEEDBACK_SALT

        forged_token = signing.dumps(
            {"rid": rec.id, "action": "own", "uid": self.other_user.id},
            salt=RECOMMENDATION_FEEDBACK_SALT,
        )
        response = self._get(forged_token)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data, {"detail": "Not found."})

    def test_tampered_token_returns_generic_404(self) -> None:
        rec = _make_run_with_pick(self.user)
        token = self._token(rec)
        response = self._get(token[:-1] + ("x" if token[-1] != "x" else "y"))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data, {"detail": "Not found."})

    def test_expired_token_returns_generic_404(self) -> None:
        rec = _make_run_with_pick(self.user)
        token = self._token(rec)
        with patch("fragrance.services.RECOMMENDATION_FEEDBACK_MAX_AGE", -1):
            response = self._get(token)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data, {"detail": "Not found."})

    def test_replaced_pick_token_returns_404(self) -> None:
        rec = _make_run_with_pick(self.user, pick_status="replaced")
        response = self._get(self._token(rec))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_confirm_post_success_then_conflict_on_second_use(self) -> None:
        rec = _make_run_with_pick(self.user)
        token = self._token(rec, action="own")

        first = self._post(token)
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data["action"], "own")

        second = self._post(token)
        self.assertEqual(second.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(second.data["rated_as"], "own")

        self.assertEqual(Fragrance.objects.filter(user=self.user, name="Aventus").count(), 1)

    def test_confirm_post_conflict_on_different_action(self) -> None:
        rec = _make_run_with_pick(self.user)
        self._post(self._token(rec, action="like"))

        response = self._post(self._token(rec, action="dislike"))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["rated_as"], "like")


class _ScriptedProvider:
    """
    StructuredLLMProvider stand-in for full-pipeline tests: returns a
    per-tool-name canned response, so a single instance can stand in for all
    of a run's LLM calls (create_profile, select_candidates,
    generate_email_content, ...) without hitting a real SDK.
    """

    def __init__(self, responses_by_tool: dict[str, dict]) -> None:
        self._responses = responses_by_tool
        self.calls: list[dict] = []

    def generate_structured(
        self,
        *,
        prompt: str,
        tool_name: str,
        description: str,
        schema: dict,
        max_tokens: int,
    ) -> dict:
        self.calls.append({"tool_name": tool_name, "prompt": prompt})
        return self._responses[tool_name]


class _ScriptedProviderWithErrors:
    """
    Like _ScriptedProvider, but a response value that is an Exception
    instance is raised instead of returned — lets a single provider
    stand-in exercise run_verification_checks' three-way exception handling
    (StructuredOutputError / SDK BadRequestError / everything else) for one
    specific check while the other three succeed normally.
    """

    def __init__(self, responses_by_tool: dict[str, dict | Exception]) -> None:
        self._responses = responses_by_tool

    def generate_structured(
        self,
        *,
        prompt: str,
        tool_name: str,
        description: str,
        schema: dict,
        max_tokens: int,
    ) -> dict:
        value = self._responses[tool_name]
        if isinstance(value, Exception):
            raise value
        return value


def _echo_query_search(query: str, num_results: int = 5) -> list[dict]:
    # verify_candidates title-matches on candidate name-in-title; embedding the
    # query (which always starts with the candidate name) in the fake title
    # guarantees every candidate is 'confirmed' without exercising the
    # select_replacement LLM call, which is out of scope for this test.
    return [{"title": query, "url": "https://fragrantica.com/x", "content": "c"}]


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


_VALID_PROFILE_RESPONSE = {
    "loved_notes": "oud, amber",
    "liked_notes": "citrus",
    "disliked_notes": "aquatic",
    "owns_list": ["Aventus"],
    "search_angle_1": "niche oud fragrances",
    "search_angle_2": "amber fragrances 2026",
}
_VALID_REPLACEMENT_RESPONSE = {"name": "Oud Wood", "house": "Tom Ford"}


def _valid_candidates_response(count: int = 5) -> dict:
    return {
        "candidates": [
            {"name": f"Candidate {i}", "house": f"House {i}"} for i in range(count)
        ]
    }


def _valid_email_content_response() -> dict:
    return {
        "intro": "Welcome back.",
        "rationales": [
            {"name": name, "rationale": f"Suits {name}."}
            for name in ["Aventus", "Bleu de Chanel"]
        ],
    }


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

    def test_missing_config_propagates_for_celery_retry(self) -> None:
        self.config.delete()
        with self.assertRaises(FragranceConfig.DoesNotExist):
            render_and_send_email(user_id=self.user.id, run_id=self.run.id)


class ConcurrentTokenRefreshTests(TransactionTestCase):
    """
    Regression test for the refresh-token race: two requests presenting the
    same refresh token, fired as close to simultaneously as two real threads
    (each with its own DB connection) allow, must resolve to exactly one 200
    and one 401 — never two 200s minting independent token chains.

    Requires TransactionTestCase (real commits across threads) rather than
    TestCase (single wrapped, uncommitted transaction per test) because the
    fix's select_for_update() lock is only meaningful across real,
    independently-committing transactions.
    """

    REFRESH_URL = "/api/v1/auth/token/refresh/"

    def test_same_refresh_token_used_twice_concurrently(self) -> None:
        user = User.objects.create_user(username="dave", password="hal-9000")
        refresh_str = str(RefreshToken.for_user(user))

        results: list[int] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def _refresh() -> None:
            from django.db import connection

            try:
                barrier.wait()
                response = APIClient().post(
                    self.REFRESH_URL, {"refresh": refresh_str}, format="json"
                )
                with results_lock:
                    results.append(response.status_code)
            finally:
                # Each thread opens its own DB connection; Django only
                # auto-closes the main thread's connection after a test, so
                # leaving this open would dangle a session against the test
                # database and break its teardown (DROP DATABASE).
                connection.close()

        threads = [threading.Thread(target=_refresh) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(
            sorted(results), [status.HTTP_200_OK, status.HTTP_401_UNAUTHORIZED]
        )


class AdminURLValidationTests(TestCase):
    """
    Exercises _validated_admin_url() directly rather than reloading
    config.settings mid-suite — the validator runs once at settings-import
    time, so override_settings can't reach it, and re-executing the settings
    module against a live test run is a bad idea. See
    .claude/plans/2026-08-30-admin-hardening-plan.md, Finding 5.
    """

    def test_leading_and_trailing_slashes_are_stripped_to_one_segment(self) -> None:
        self.assertEqual(
            _validated_admin_url(raw="/my-admin-path/"), "my-admin-path"
        )

    def test_bare_valid_value_passes_unchanged(self) -> None:
        self.assertEqual(_validated_admin_url(raw="dev-admin-1"), "dev-admin-1")

    def test_too_short_rejected(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            _validated_admin_url(raw="abc123")

    def test_uppercase_rejected(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            _validated_admin_url(raw="SHORTPATH")

    def test_empty_after_stripping_slashes_rejected(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            _validated_admin_url(raw="///")

    def test_angle_brackets_rejected(self) -> None:
        # path() route strings parse "<...>" as converter syntax — a value
        # containing these must be rejected outright, not silently create an
        # unintended dynamic capture.
        with self.assertRaises(ImproperlyConfigured):
            _validated_admin_url(raw="abc<int:pk>")

    def test_leading_hyphen_rejected(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            _validated_admin_url(raw="-leadinghyphen")


class SecureProxySslHeaderTests(TestCase):
    """
    Regression test for the infra-reviewer/security-reviewer's
    2026-09-04 admin-hardening re-review, Finding A / Item 2:
    SECURE_PROXY_SSL_HEADER must be env-gated on DEBUG the same way
    SESSION_COOKIE_SECURE/CSRF_COOKIE_SECURE already are, since nginx is
    only guaranteed to overwrite X-Forwarded-Proto (rather than forward a
    client-supplied value) in prod — dev's docker-compose.override.yaml
    publishes `backend` directly, bypassing nginx entirely.

    Exercises _secure_proxy_ssl_header() directly rather than asserting
    against settings.SECURE_PROXY_SSL_HEADER/settings.DEBUG, mirroring
    AdminURLValidationTests' approach for _validated_admin_url() above —
    the settings module evaluates this once at import time using the real
    .env DEBUG value, and Django's own test runner separately forces
    settings.DEBUG=False for the duration of `manage.py test`, so a
    settings-module-level assertion here would compare two different
    timepoints and assert against the wrong one. Testing the helper
    directly avoids that trap entirely.
    """

    def test_none_when_debug_is_true(self) -> None:
        self.assertIsNone(_secure_proxy_ssl_header(debug=True))

    def test_forwarded_proto_tuple_when_debug_is_false(self) -> None:
        self.assertEqual(
            _secure_proxy_ssl_header(debug=False),
            ("HTTP_X_FORWARDED_PROTO", "https"),
        )


class AdminURLRoutingTests(TestCase):
    """
    Value-agnostic: asserts against whatever settings.DJANGO_ADMIN_URL
    actually holds in this environment, not a hardcoded literal, since the
    real value is deploy-specific and unguessable by design.
    """

    def test_admin_index_resolves_under_the_configured_admin_url(self) -> None:
        self.assertTrue(
            reverse("admin:index").startswith(f"/{settings.DJANGO_ADMIN_URL}/")
        )

    def test_legacy_admin_path_is_dead_unless_it_happens_to_be_the_configured_value(
        self,
    ) -> None:
        if settings.DJANGO_ADMIN_URL == "admin":
            self.skipTest(
                "DJANGO_ADMIN_URL is literally 'admin' in this environment; "
                "the min-length-8 regex forbids this in practice."
            )
        with self.assertRaises(Resolver404):
            resolve("/admin/")


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


class AdminHardeningExceptionReporterFilterTests(TestCase):
    """
    Regression test for .claude/plans/2026-08-30-admin-hardening-plan.md,
    Finding 13: Django's project-level HIDDEN_SETTINGS setting was removed
    in Django 3.1 (confirmed by reading the installed
    django.views.debug.SafeExceptionReporterFilter directly — the masking
    regex is now a hardcoded class attribute, not something settings.py can
    override by name). DEFAULT_EXCEPTION_REPORTER_FILTER must instead point
    at a SafeExceptionReporterFilter subclass, or DJANGO_ADMIN_URL renders
    unmasked in the debug settings dump on any unhandled production error.
    """

    def setUp(self) -> None:
        self.stock_filter = SafeExceptionReporterFilter()
        self.custom_filter = AdminHardeningExceptionReporterFilter()

    def test_extends_rather_than_replaces_djangos_default_pattern(self) -> None:
        self.assertTrue(
            self.custom_filter.hidden_settings.pattern.startswith(
                self.stock_filter.hidden_settings.pattern
            )
        )

    def test_django_admin_url_is_masked_by_the_custom_filter_but_not_stock(
        self,
    ) -> None:
        raw_value = "some-real-admin-path"
        self.assertEqual(
            self.stock_filter.cleanse_setting("DJANGO_ADMIN_URL", raw_value),
            raw_value,
        )
        self.assertEqual(
            self.custom_filter.cleanse_setting("DJANGO_ADMIN_URL", raw_value),
            self.custom_filter.cleansed_substitute,
        )

    def test_previously_masked_settings_remain_masked(self) -> None:
        for name in ("ANTHROPIC_API_KEY", "DB_PASSWORD", "DJANGO_SECRET_KEY"):
            self.assertEqual(
                self.custom_filter.cleanse_setting(name, "real-value"),
                self.custom_filter.cleansed_substitute,
            )

    def test_non_secret_operator_facing_settings_remain_visible(self) -> None:
        # CORS_ALLOWED_ORIGINS/FRONTEND_URL are meant to be visible/non-secret
        # — the custom pattern must not accidentally widen the match to catch
        # them just because it now also matches ADMIN_URL.
        self.assertEqual(
            self.custom_filter.cleanse_setting(
                "CORS_ALLOWED_ORIGINS", settings.CORS_ALLOWED_ORIGINS
            ),
            settings.CORS_ALLOWED_ORIGINS,
        )
        self.assertEqual(
            self.custom_filter.cleanse_setting(
                "FRONTEND_URL", settings.FRONTEND_URL
            ),
            settings.FRONTEND_URL,
        )

    def test_get_safe_settings_diff_touches_only_admin_url_settings(self) -> None:
        # The actual claim: comparing the full masked-key sets between the
        # stock and custom filters should show DJANGO_ADMIN_URL (and the
        # incidental _ADMIN_URL_PATTERN regex object — a non-secret compiled
        # pattern that happens to satisfy Django's settings-loader naming
        # rule) newly masked, and nothing newly unmasked.
        stock_masked = {
            key
            for key, value in self.stock_filter.get_safe_settings().items()
            if value == self.stock_filter.cleansed_substitute
        }
        custom_masked = {
            key
            for key, value in self.custom_filter.get_safe_settings().items()
            if value == self.custom_filter.cleansed_substitute
        }
        self.assertEqual(
            custom_masked - stock_masked,
            {"DJANGO_ADMIN_URL", "_ADMIN_URL_PATTERN"},
        )
        self.assertEqual(stock_masked - custom_masked, set())

    @override_settings(
        DEBUG=True,
        ALLOWED_HOSTS=["localhost"],
        DEFAULT_EXCEPTION_REPORTER_FILTER=(
            "config.debug.AdminHardeningExceptionReporterFilter"
        ),
    )
    def test_live_debug_page_masks_admin_url_and_still_renders(self) -> None:
        # End-to-end repro of the security-reviewer's original finding: an
        # unhandled DisallowedHost exception with DEBUG=True renders Django's
        # standard debug page. Client(raise_request_exception=False) lets the
        # response come back rendered instead of re-raising the exception,
        # matching what a real browser would receive.
        client = Client(raise_request_exception=False)
        response = client.get(
            f"/{settings.DJANGO_ADMIN_URL}/", HTTP_HOST="evil.example.com"
        )
        self.assertEqual(response.status_code, 400)
        body = response.content.decode()
        self.assertIn("DisallowedHost", body)
        row_match = re.search(
            r"<td>DJANGO_ADMIN_URL</td>\s*<td class=\"code\"><pre>(.*?)</pre></td>",
            body,
        )
        self.assertIsNotNone(row_match, "settings dump row not found on debug page")
        self.assertNotIn(settings.DJANGO_ADMIN_URL, row_match.group(1))
        self.assertIn("*", row_match.group(1))


class NormalizeEmailTests(TestCase):
    def test_lowercases(self) -> None:
        self.assertEqual(normalize_email(email="Ryan@Example.com"), "ryan@example.com")

    def test_strips_plus_tag(self) -> None:
        self.assertEqual(normalize_email(email="ryan+newsletter@example.com"), "ryan@example.com")

    def test_strips_dots_on_gmail(self) -> None:
        self.assertEqual(normalize_email(email="r.y.a.n@gmail.com"), "ryan@gmail.com")

    def test_strips_dots_on_googlemail(self) -> None:
        self.assertEqual(normalize_email(email="r.yan@googlemail.com"), "ryan@googlemail.com")

    def test_does_not_strip_dots_on_non_gmail_domain(self) -> None:
        self.assertEqual(normalize_email(email="r.yan@example.com"), "r.yan@example.com")

    def test_gmail_dot_and_plus_tag_collide(self) -> None:
        self.assertEqual(
            normalize_email(email="r.yan+1@gmail.com"), normalize_email(email="ryan@gmail.com")
        )


class CacheIsolationTests(SimpleTestCase):
    """
    Positive control for config.testing.RedisSafeTestRunner: asserts the
    *resolved* cache backend object, not settings.CACHES, so this fails
    loudly (rather than silently reconnecting to real Redis) if TEST_RUNNER
    is ever dropped from settings or the runner's override stops firing.
    """

    def test_default_cache_is_locmem_under_the_test_runner(self) -> None:
        self.assertIsInstance(caches["default"], LocMemCache)


def _run_reset_task_inline():
    """Patches .delay to run the task eagerly, so tests can inspect the mail it sends."""
    return patch(
        "fragrance.tasks.send_password_reset_email.delay",
        side_effect=lambda **kwargs: send_password_reset_email.apply(kwargs=kwargs),
    )


class PasswordResetUnusablePasswordTests(TestCase):
    """
    Regression coverage for the load-bearing behaviour documented on
    PasswordResetRequestView: approved-but-not-yet-converted beta users are
    created via set_unusable_password() (BetaSignupAdmin.approve_signups),
    and Django's own PasswordResetForm.get_users() filters such accounts
    out. This project's PasswordResetRequestView deliberately does its own
    User.objects.get(email=...) lookup instead, which is the only reason
    "forgot password" can recover one of these accounts -- and the new
    /beta/accept copy (see BetaAcceptViewTests) now actively points users
    down this path when their invite link is dead or expired.
    """

    def test_django_password_reset_form_excludes_unusable_password_users(self) -> None:
        # Empirical proof of the gap this project's custom view exists to
        # close -- if a future Django release changes get_users()'s
        # filtering, this is the test that should catch it.
        from django.contrib.auth.forms import PasswordResetForm

        User.objects.create_user(username="nopass", email="nopass@example.com", password=None)
        user = User.objects.get(username="nopass")
        self.assertFalse(user.has_usable_password())

        form = PasswordResetForm(data={"email": "nopass@example.com"})
        self.assertTrue(form.is_valid())
        self.assertEqual(list(form.get_users(email="nopass@example.com")), [])

    def test_reset_request_reaches_unusable_password_user_and_token_validates(self) -> None:
        user = User.objects.create_user(
            username="approved_not_converted", email="approved@example.com", password=None
        )
        self.assertFalse(user.has_usable_password())

        with _run_reset_task_inline():
            response = self.client.post(
                "/api/v1/auth/password/reset/", {"email": "approved@example.com"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

        # Asserts a literal "&", not "&amp;": render_to_string HTML-escapes
        # by default (Django's autoescaping applies to the template engine,
        # not the .txt extension), which silently breaks this link's second
        # query param for every real mail client -- password_reset_email.txt
        # wraps its body in {% autoescape off %} specifically to prevent
        # that. This assertion is what stops that fix from being "tidied"
        # away without anyone noticing the link breaks again.
        self.assertNotIn("&amp;", mail.outbox[0].body)
        match = re.search(r"uid=([^&\s]+)&token=([^&\s]+)", mail.outbox[0].body)
        self.assertIsNotNone(match)
        uid, token = match.group(1), match.group(2)

        confirm_response = self.client.post(
            "/api/v1/auth/password/reset/confirm/",
            {
                "uid": uid,
                "token": token,
                "new_password": "Str0ng-Passw0rd!",
                "new_password_confirm": "Str0ng-Passw0rd!",
            },
        )
        self.assertEqual(confirm_response.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password("Str0ng-Passw0rd!"))


class BetaSignupViewTests(TestCase):
    """
    Covers the public waitlist endpoint's core anti-abuse contract: a
    byte-identical response across every non-error outcome, the honeypot,
    and the Gmail-class dedup that normalize_email exists to enforce.
    """

    url = "/api/v1/beta/signup/"

    def setUp(self) -> None:
        # DRF's ScopedRateThrottle uses the default (process-wide) cache;
        # clear it so an earlier test's requests never count against this
        # test's 5/min budget.
        cache.clear()

    def test_new_signup_returns_202_and_creates_a_row(self) -> None:
        response = self.client.post(self.url, {"email": "newbie@example.com"})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(
            BetaSignup.objects.filter(normalized_email="newbie@example.com").exists()
        )

    def test_new_duplicate_and_existing_user_return_byte_identical_bodies(self) -> None:
        User.objects.create_user(
            username="already", email="already@example.com", password="pw12345!"
        )

        new_response = self.client.post(self.url, {"email": "fresh@example.com"})
        duplicate_response = self.client.post(self.url, {"email": "fresh@example.com"})
        existing_user_response = self.client.post(self.url, {"email": "already@example.com"})

        self.assertEqual(new_response.status_code, 202)
        self.assertEqual(duplicate_response.status_code, 202)
        self.assertEqual(existing_user_response.status_code, 202)
        self.assertEqual(new_response.content, duplicate_response.content)
        self.assertEqual(new_response.content, existing_user_response.content)

        # Duplicate submissions collapse to one row (unique on
        # normalized_email); the "belongs to an existing User" case still
        # gets a waitlist row too -- deliberately not special-cased, since
        # branching on it would create a query-count/timing signal on an
        # endpoint whose whole point is not distinguishing these cases. The
        # admin-approval step (a later slice) is where that guard belongs.
        self.assertEqual(
            BetaSignup.objects.filter(normalized_email="fresh@example.com").count(), 1
        )
        self.assertEqual(
            BetaSignup.objects.filter(normalized_email="already@example.com").count(), 1
        )

    def test_already_approved_signup_returns_identical_body_and_is_not_reset(self) -> None:
        BetaSignup.objects.create(
            email="vip@example.com", normalized_email="vip@example.com", status="approved"
        )

        response = self.client.post(self.url, {"email": "vip@example.com"})
        control = self.client.post(self.url, {"email": "someone-else@example.com"})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.content, control.content)
        signup = BetaSignup.objects.get(normalized_email="vip@example.com")
        self.assertEqual(signup.status, "approved")
        self.assertEqual(BetaSignup.objects.filter(normalized_email="vip@example.com").count(), 1)

    def test_malformed_email_returns_400_with_a_different_shape(self) -> None:
        response = self.client.post(self.url, {"email": "not-an-email"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.json())

    def test_honeypot_filled_returns_202_and_creates_no_row(self) -> None:
        response = self.client.post(
            self.url, {"email": "bot@example.com", "website": "http://spam.example"}
        )
        self.assertEqual(response.status_code, 202)
        self.assertFalse(BetaSignup.objects.exists())

    def test_honeypot_trip_is_logged_without_the_raw_email(self) -> None:
        with self.assertLogs("fragrance.views", level="INFO") as captured:
            self.client.post(
                self.url, {"email": "bot@example.com", "website": "http://spam.example"}
            )
        self.assertTrue(any("honeypot" in message for message in captured.output))
        self.assertFalse(any("bot@example.com" in message for message in captured.output))

    def test_gmail_dot_and_plus_variant_collides_on_normalized_email(self) -> None:
        first = self.client.post(self.url, {"email": "ryan@gmail.com"})
        second = self.client.post(self.url, {"email": "r.yan+1@gmail.com"})

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(
            BetaSignup.objects.filter(normalized_email="ryan@gmail.com").count(), 1
        )
        self.assertEqual(BetaSignup.objects.count(), 1)

    def test_signup_ip_hash_is_stored_and_is_not_the_raw_ip(self) -> None:
        self.client.post(
            self.url, {"email": "traceable@example.com"}, REMOTE_ADDR="203.0.113.5"
        )
        signup = BetaSignup.objects.get(normalized_email="traceable@example.com")
        self.assertNotEqual(signup.signup_ip_hash, "203.0.113.5")
        self.assertTrue(signup.signup_ip_hash)

    def test_unauthenticated_access_is_allowed(self) -> None:
        # No Authorization header on any request above, and none of them
        # 401'd -- this is a positive assertion that AllowAny is engaged.
        response = self.client.post(self.url, {"email": "anon@example.com"})
        self.assertNotEqual(response.status_code, 401)

    def test_sixth_request_in_a_minute_from_the_same_ip_is_throttled(self) -> None:
        # DEFAULT_THROTTLE_RATES["beta-signup"] = "5/min".
        for i in range(5):
            response = self.client.post(
                self.url, {"email": f"burst{i}@example.com"}, REMOTE_ADDR="198.51.100.7"
            )
            self.assertEqual(response.status_code, 202)

        blocked = self.client.post(
            self.url, {"email": "burst-over@example.com"}, REMOTE_ADDR="198.51.100.7"
        )
        self.assertEqual(blocked.status_code, 429)

    def test_throttle_key_is_not_defeated_by_a_forged_x_forwarded_for_header(self) -> None:
        # Regression test: the stock ScopedRateThrottle.get_ident trusts the
        # *entire* client-supplied X-Forwarded-For string when NUM_PROXIES is
        # unset, so a client that varies XFF on every request gets a fresh
        # bucket every time and the limit never engages. BetaSignupThrottle
        # must key on REMOTE_ADDR (via get_client_ip) instead, in dev where
        # there's no trusted proxy in front to have set XFF legitimately.
        for i in range(5):
            response = self.client.post(
                self.url,
                {"email": f"spoof{i}@example.com"},
                REMOTE_ADDR="198.51.100.9",
                HTTP_X_FORWARDED_FOR=f"10.0.0.{i}",
            )
            self.assertEqual(response.status_code, 202)

        blocked = self.client.post(
            self.url,
            {"email": "spoof-over@example.com"},
            REMOTE_ADDR="198.51.100.9",
            HTTP_X_FORWARDED_FOR="10.0.0.99",
        )
        self.assertEqual(blocked.status_code, 429)

    @override_settings(DEBUG=False)
    def test_prod_mode_throttles_on_x_real_ip_set_by_nginx(self) -> None:
        # With DEBUG=False, get_client_ip trusts X-Real-IP (what nginx's
        # realip-restored $remote_addr becomes upstream), not REMOTE_ADDR
        # (which would be nginx's own container address, since backend has
        # no published port in prod) and not X-Forwarded-For (client-visible
        # and only appended-to, so spoofable). Varying REMOTE_ADDR here
        # while holding X-Real-IP fixed simulates every request arriving
        # from the same one-hop nginx proxy.
        for i in range(5):
            response = self.client.post(
                self.url,
                {"email": f"prodburst{i}@example.com"},
                REMOTE_ADDR=f"172.18.0.{i}",
                HTTP_X_REAL_IP="203.0.113.42",
            )
            self.assertEqual(response.status_code, 202)

        blocked = self.client.post(
            self.url,
            {"email": "prodburst-over@example.com"},
            REMOTE_ADDR="172.18.0.99",
            HTTP_X_REAL_IP="203.0.113.42",
        )
        self.assertEqual(blocked.status_code, 429)

    def test_options_returns_405_and_does_not_leak_honeypot_field_name(self) -> None:
        # DEFAULT_METADATA_CLASS = None (settings.py) makes APIView.options()
        # return 405 instead of DRF's stock SimpleMetadata response, which
        # otherwise discloses the `website` honeypot field name (and this
        # view's docstring) to any anonymous OPTIONS probe -- defeating the
        # honeypot for a bot that checks OPTIONS before POST.
        response = self.client.options(self.url)
        self.assertEqual(response.status_code, 405)
        self.assertNotIn(b"website", response.content)

    def test_browser_style_get_does_not_leak_honeypot_field_name(self) -> None:
        # DEFAULT_METADATA_CLASS = None (above) only closes the OPTIONS path.
        # BrowsableAPIRenderer is a second, independent mechanism that renders
        # an HTML form -- including the `website` honeypot field name and this
        # view's docstring -- for any request whose Accept header prefers
        # text/html, which is what a browser sends by default when a URL is
        # opened directly. DEFAULT_RENDERER_CLASSES restricts rendering to
        # JSON only, so this must return no HTML form and no `website` string
        # anywhere in the body.
        response = self.client.get(
            self.url,
            HTTP_ACCEPT="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        self.assertEqual(response.status_code, 405)
        self.assertNotIn(b"<form", response.content)
        self.assertNotIn(b"website", response.content)

    def test_throttle_fails_open_when_cache_is_unavailable(self) -> None:
        # Django's built-in RedisCache (settings.CACHES) doesn't swallow
        # connection errors the way django-redis can with IGNORE_EXCEPTIONS.
        # BetaSignupThrottle.allow_request must catch that and allow the
        # request rather than 500ing this public, unauthenticated page.
        with patch.object(BetaSignupThrottle, "cache") as mock_cache:
            mock_cache.get.side_effect = redis.exceptions.ConnectionError("redis is down")
            response = self.client.post(self.url, {"email": "failopen@example.com"})

        self.assertEqual(response.status_code, 202)
        self.assertTrue(
            BetaSignup.objects.filter(normalized_email="failopen@example.com").exists()
        )


class RegisterRouteRemovedTests(TestCase):
    def test_register_endpoint_no_longer_resolves(self) -> None:
        with self.assertRaises(Resolver404):
            resolve("/api/v1/auth/register/")

    def test_register_endpoint_404s_over_http(self) -> None:
        response = self.client.post(
            "/api/v1/auth/register/",
            {"username": "sneaky", "email": "sneaky@example.com", "password": "pw12345!"},
        )
        self.assertEqual(response.status_code, 404)


class DeriveUniqueUsernameTests(TestCase):
    def test_simple_email_produces_local_part_username(self) -> None:
        self.assertEqual(derive_unique_username(email="ryan@example.com"), "ryan")

    def test_collision_appends_numeric_suffix(self) -> None:
        User.objects.create_user(username="ryan", email="ryan@gmail.com", password="pw12345!")
        self.assertEqual(derive_unique_username(email="ryan@example.com"), "ryan_2")

    def test_multiple_collisions_increment_suffix(self) -> None:
        User.objects.create_user(username="ryan", email="a@gmail.com", password="pw12345!")
        User.objects.create_user(username="ryan_2", email="b@example.com", password="pw12345!")
        self.assertEqual(derive_unique_username(email="ryan@another.com"), "ryan_3")

    def test_symbols_only_local_part_falls_back_to_user(self) -> None:
        self.assertEqual(derive_unique_username(email="+++@example.com"), "user")

    def test_two_distinct_addresses_reducing_to_the_same_local_part_get_distinct_usernames(
        self,
    ) -> None:
        first = derive_unique_username(email="ryan@gmail.com")
        User.objects.create_user(username=first, email="ryan@gmail.com", password="pw12345!")
        second = derive_unique_username(email="ryan@example.com")
        self.assertNotEqual(first, second)


class BetaInviteTokenTests(TestCase):
    def _signup_with_user(self) -> tuple[BetaSignup, User]:
        user = User.objects.create_user(username="invitee", email="invitee@example.com")
        signup = BetaSignup.objects.create(
            email="invitee@example.com",
            normalized_email="invitee@example.com",
            status="approved",
            invited_user=user,
        )
        return signup, user

    def test_round_trips(self) -> None:
        signup, user = self._signup_with_user()
        token = generate_beta_invite_token(signup=signup)
        payload = verify_beta_invite_token(token=token)
        self.assertEqual(payload["sid"], signup.id)
        self.assertEqual(payload["uid"], user.id)

    def test_tampered_token_rejected(self) -> None:
        signup, _ = self._signup_with_user()
        token = generate_beta_invite_token(signup=signup)
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        self.assertIsNone(verify_beta_invite_token(token=tampered))

    def test_expired_token_rejected(self) -> None:
        signup, _ = self._signup_with_user()
        token = generate_beta_invite_token(signup=signup)
        with patch("fragrance.services.BETA_INVITE_MAX_AGE", -1):
            self.assertIsNone(verify_beta_invite_token(token=token))

    def test_malformed_token_rejected(self) -> None:
        self.assertIsNone(verify_beta_invite_token(token="not-a-real-token"))

    def test_empty_token_rejected(self) -> None:
        self.assertIsNone(verify_beta_invite_token(token=""))

    def test_max_age_is_fourteen_days(self) -> None:
        self.assertEqual(BETA_INVITE_MAX_AGE, 60 * 60 * 24 * 14)


class PlanModelTests(TestCase):
    def test_quota_fields_are_independent_of_billing_interval(self) -> None:
        monthly = Plan.objects.create(
            name="Beta Monthly",
            slug="test-monthly",
            stripe_price_id="price_test_monthly",
            amount_cents=400,
            billing_interval="month",
            access_days_granted=30,
        )
        annual = Plan.objects.create(
            name="Beta Annual",
            slug="test-annual",
            stripe_price_id="price_test_annual",
            amount_cents=4400,
            billing_interval="year",
            access_days_granted=365,
        )
        # Usage terms must not follow billing terms -- an annual subscriber
        # gets one run per 30 days, not twelve on the first of January.
        self.assertEqual(monthly.quota_period_days, annual.quota_period_days)
        self.assertEqual(monthly.max_runs_per_quota_period, annual.max_runs_per_quota_period)
        self.assertNotEqual(monthly.access_days_granted, annual.access_days_granted)
        self.assertNotEqual(monthly.billing_interval, annual.billing_interval)


class SubscriptionHasActiveAccessTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="subscriber", email="subscriber@example.com", password="pw12345!"
        )

    def test_null_access_expires_at_is_false_even_when_status_is_active(self) -> None:
        sub = Subscription.objects.create(user=self.user, status="active", access_expires_at=None)
        self.assertFalse(sub.has_active_access)

    def test_active_status_and_future_expiry_is_true(self) -> None:
        sub = Subscription.objects.create(
            user=self.user,
            status="active",
            access_expires_at=timezone.now() + timedelta(days=5),
        )
        self.assertTrue(sub.has_active_access)

    def test_active_status_but_past_expiry_is_false(self) -> None:
        sub = Subscription.objects.create(
            user=self.user,
            status="active",
            access_expires_at=timezone.now() - timedelta(days=1),
        )
        self.assertFalse(sub.has_active_access)

    def test_non_active_status_with_future_expiry_is_false(self) -> None:
        sub = Subscription.objects.create(
            user=self.user,
            status="revoked",
            access_expires_at=timezone.now() + timedelta(days=5),
        )
        self.assertFalse(sub.has_active_access)


class SeedPlansCommandTests(TestCase):
    def test_creates_both_plans(self) -> None:
        out = StringIO()
        call_command(
            "seed_plans",
            "--monthly-price-id=price_mo",
            "--annual-price-id=price_yr",
            stdout=out,
        )
        monthly = Plan.objects.get(slug="beta-monthly")
        annual = Plan.objects.get(slug="beta-annual")
        self.assertEqual(monthly.stripe_price_id, "price_mo")
        self.assertEqual(annual.stripe_price_id, "price_yr")
        self.assertEqual(monthly.billing_interval, "month")
        self.assertEqual(annual.billing_interval, "year")
        self.assertEqual(monthly.access_days_granted, 30)
        self.assertEqual(annual.access_days_granted, 365)
        self.assertEqual(monthly.quota_period_days, 30)
        self.assertEqual(annual.quota_period_days, 30)
        self.assertEqual(monthly.max_runs_per_quota_period, 1)
        self.assertEqual(annual.max_runs_per_quota_period, 1)

    def test_rerun_is_idempotent_and_updates_in_place(self) -> None:
        call_command("seed_plans", "--monthly-price-id=price_mo", "--annual-price-id=price_yr")
        call_command("seed_plans", "--monthly-price-id=price_mo2", "--annual-price-id=price_yr2")
        self.assertEqual(Plan.objects.filter(slug="beta-monthly").count(), 1)
        self.assertEqual(Plan.objects.filter(slug="beta-annual").count(), 1)
        self.assertEqual(Plan.objects.get(slug="beta-monthly").stripe_price_id, "price_mo2")
        self.assertEqual(Plan.objects.get(slug="beta-annual").stripe_price_id, "price_yr2")

    def test_same_price_id_for_both_plans_raises(self) -> None:
        with self.assertRaises(CommandError):
            call_command("seed_plans", "--monthly-price-id=same", "--annual-price-id=same")


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
            access_days_granted=30,
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

    def test_approve_creates_subscription_with_default_plan_and_none_status(self) -> None:
        signup = BetaSignup.objects.create(email="sub@example.com", normalized_email="sub@example.com")
        with patch.object(send_beta_invite_email, "delay"):
            self._approve([signup.pk])

        signup.refresh_from_db()
        sub = Subscription.objects.get(user=signup.invited_user)
        self.assertEqual(sub.status, "none")
        self.assertEqual(sub.plan, self.plan)

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


class BetaAcceptViewTests(TestCase):
    """
    Covers POST /api/v1/beta/accept/: token round-trip, single-use
    enforcement (an already-converted token must be rejected, distinctly
    from a never-valid one -- 409 "already_used" vs. 400
    "invalid_or_expired", per the operator-approved 2026-09-20 contract),
    expired/tampered tokens, and that validate_password() actually runs --
    slice 1's removed UserRegistrationSerializer skipped validators, and
    this path must not repeat that.
    """

    url = "/api/v1/beta/accept/"

    def _approved_signup(self, email: str = "invitee@example.com") -> tuple[BetaSignup, User]:
        user = User.objects.create_user(username=email.split("@")[0], email=email)
        signup = BetaSignup.objects.create(
            email=email, normalized_email=email, status="approved", invited_user=user
        )
        return signup, user

    def test_valid_token_sets_password_marks_converted_and_returns_jwt_pair(self) -> None:
        signup, user = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)

        response = self.client.post(self.url, {"token": token, "password": "Str0ng-Passw0rd!"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("access", body)
        self.assertIn("refresh", body)

        signup.refresh_from_db()
        self.assertEqual(signup.status, "converted")
        self.assertIsNotNone(signup.converted_at)
        user.refresh_from_db()
        self.assertTrue(user.check_password("Str0ng-Passw0rd!"))

    def test_already_converted_token_returns_409_already_used(self) -> None:
        signup, _ = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)
        first = self.client.post(self.url, {"token": token, "password": "Str0ng-Passw0rd!"})
        self.assertEqual(first.status_code, 200)

        second = self.client.post(self.url, {"token": token, "password": "An0ther-Passw0rd!"})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json(), {"code": "already_used"})

    def test_expired_token_returns_400_invalid_or_expired(self) -> None:
        signup, _ = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)
        with patch("fragrance.services.BETA_INVITE_MAX_AGE", -1):
            response = self.client.post(self.url, {"token": token, "password": "Str0ng-Passw0rd!"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"code": "invalid_or_expired"})

    def test_tampered_token_returns_400_invalid_or_expired(self) -> None:
        signup, _ = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        response = self.client.post(self.url, {"token": tampered, "password": "Str0ng-Passw0rd!"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"code": "invalid_or_expired"})

    def test_weak_password_is_rejected_by_validators(self) -> None:
        signup, user = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)
        response = self.client.post(self.url, {"token": token, "password": "12345678"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.json())
        signup.refresh_from_db()
        self.assertEqual(signup.status, "approved")
        user.refresh_from_db()
        self.assertFalse(user.has_usable_password())

    def test_pending_signup_token_returns_400_invalid_or_expired(self) -> None:
        user = User.objects.create_user(username="pending_invitee", email="pending@example.com")
        signup = BetaSignup.objects.create(
            email="pending@example.com",
            normalized_email="pending@example.com",
            status="pending",
            invited_user=user,
        )
        token = generate_beta_invite_token(signup=signup)
        response = self.client.post(self.url, {"token": token, "password": "Str0ng-Passw0rd!"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"code": "invalid_or_expired"})

    def test_missing_token_key_returns_400_with_field_required_error(self) -> None:
        # A wholly absent "token" key hits DRF's own required-field check
        # before resolve_beta_invite_signup runs, so this is the one 400
        # shape that is *not* {"code": "invalid_or_expired"} -- see
        # BetaAcceptSerializer.token's docstring. Not reachable from the
        # real frontend form, which always submits a string.
        response = self.client.post(self.url, {"password": "Str0ng-Passw0rd!"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("token", response.json())

    def test_blank_token_returns_400_invalid_or_expired(self) -> None:
        response = self.client.post(
            self.url, {"token": "", "password": "Str0ng-Passw0rd!"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"code": "invalid_or_expired"})

    def test_unauthenticated_access_is_allowed(self) -> None:
        signup, _ = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)
        response = self.client.post(self.url, {"token": token, "password": "Str0ng-Passw0rd!"})
        self.assertNotEqual(response.status_code, 401)
        self.assertFalse(User.objects.filter(username="sneaky").exists())


class BetaAcceptPreCheckViewTests(TestCase):
    """Covers GET /api/v1/beta/accept/?token=... -- the pre-submit validity check."""

    url = "/api/v1/beta/accept/"

    def _approved_signup(self, email: str = "precheck@example.com") -> tuple[BetaSignup, User]:
        user = User.objects.create_user(username=email.split("@")[0], email=email)
        signup = BetaSignup.objects.create(
            email=email, normalized_email=email, status="approved", invited_user=user
        )
        return signup, user

    def test_valid_token_returns_200_valid_true(self) -> None:
        signup, _ = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)
        response = self.client.get(self.url, {"token": token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"valid": True})

    def test_valid_token_is_not_consumed_by_the_precheck(self) -> None:
        signup, _ = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)
        self.client.get(self.url, {"token": token})
        signup.refresh_from_db()
        self.assertEqual(signup.status, "approved")

        response = self.client.post(self.url, {"token": token, "password": "Str0ng-Passw0rd!"})
        self.assertEqual(response.status_code, 200)

    def test_converted_token_returns_409_already_used(self) -> None:
        signup, _ = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)
        self.client.post(self.url, {"token": token, "password": "Str0ng-Passw0rd!"})

        response = self.client.get(self.url, {"token": token})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"code": "already_used"})

    def test_bogus_token_returns_400_invalid_or_expired(self) -> None:
        response = self.client.get(self.url, {"token": "not-a-real-token"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"code": "invalid_or_expired"})

    def test_missing_token_returns_400_invalid_or_expired(self) -> None:
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"code": "invalid_or_expired"})

    def test_unauthenticated_access_is_allowed(self) -> None:
        response = self.client.get(self.url, {"token": "not-a-real-token"})
        self.assertNotEqual(response.status_code, 401)


# ─── Stripe billing (slice 2b) ────────────────────────────────────────────
#
# Every stripe-touching test class below carries these three settings so
# fragrance.billing._configure_stripe()'s fail-fast guard doesn't fire --
# mirrors how the rest of this file scopes settings overrides to the tests
# that actually need them.
_STRIPE_TEST_SETTINGS = {
    "STRIPE_SECRET_KEY": "sk_test_dummy",
    "STRIPE_WEBHOOK_SECRET": "whsec_test_dummy",
    "STRIPE_API_VERSION": "2026-08-26.dahlia",
}


def _fake_line(*, price_id: str | None, period_end: int | None) -> SimpleNamespace:
    """A minimal stand-in for an InvoiceLineItem, walked the same defensive way billing._match_plan_line does."""
    pricing = SimpleNamespace(price_details=SimpleNamespace(price=price_id)) if price_id else None
    period = SimpleNamespace(end=period_end) if period_end is not None else None
    return SimpleNamespace(pricing=pricing, period=period)


def _fake_invoice(
    *,
    invoice_id: str,
    customer_id: str,
    subscription_id: str | None,
    amount_paid: int,
    lines: list,
    parent_type: str = "subscription_details",
    status: str = "paid",
    amount_remaining: int = 0,
) -> SimpleNamespace:
    if parent_type == "subscription_details":
        parent = SimpleNamespace(
            type="subscription_details",
            subscription_details=SimpleNamespace(subscription=subscription_id),
        )
    else:
        parent = SimpleNamespace(type=parent_type, subscription_details=None)
    return SimpleNamespace(
        id=invoice_id,
        customer=customer_id,
        amount_paid=amount_paid,
        status=status,
        amount_remaining=amount_remaining,
        parent=parent,
        lines=SimpleNamespace(data=lines),
    )


def _fake_invoice_payment(*, invoice: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(invoice=invoice)


def _fake_payment_intent(*, pi_id: str, status: str, customer: str | None) -> SimpleNamespace:
    return SimpleNamespace(id=pi_id, status=status, customer=customer)


@override_settings(**_STRIPE_TEST_SETTINGS)
class BillingCheckoutViewTests(TestCase):
    url = "/api/v1/billing/checkout/"

    def setUp(self) -> None:
        # The lapsed-restart guard lists the customer's Stripe subscriptions; default to none.
        patcher = patch(
            "fragrance.billing.stripe.Subscription.list",
            return_value=stripe.ListObject.construct_from(
                {"object": "list", "data": [], "has_more": False, "url": "/v1/subscriptions"},
                "sk_test_dummy",
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.monthly = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, currency="usd", billing_interval="month", access_days_granted=30,
        )
        self.annual = Plan.objects.create(
            name="Beta Annual", slug="beta-annual", stripe_price_id="price_yr",
            amount_cents=4400, currency="usd", billing_interval="year", access_days_granted=365,
        )
        self.user = User.objects.create_user(
            username="checkoutuser", email="checkoutuser@example.com", password="pw12345!"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_get_returns_active_plans_without_price_id(self) -> None:
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        # Wrapped in an object rather than returned as a bare array: the frontend
        # composable reads `response.plans`, and an object can carry metadata later
        # without a breaking change.
        plans = response.json()["plans"]
        slugs = {plan["slug"] for plan in plans}
        self.assertEqual(slugs, {"beta-monthly", "beta-annual"})
        for plan in plans:
            self.assertNotIn("stripe_price_id", plan)

    def test_unauthenticated_get_returns_401(self) -> None:
        response = APIClient().get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_unauthenticated_post_returns_401(self) -> None:
        response = APIClient().post(self.url, {"slug": "beta-monthly"})
        self.assertEqual(response.status_code, 401)

    @patch("fragrance.billing.stripe.checkout.Session.create")
    @patch("fragrance.billing.stripe.Customer.create")
    def test_post_creates_checkout_session(self, mock_customer_create, mock_session_create) -> None:
        mock_customer_create.return_value = SimpleNamespace(id="cus_new_1")
        mock_session_create.return_value = SimpleNamespace(id="cs_new_1", url="https://checkout.stripe.com/cs_new_1")

        response = self.client.post(self.url, {"slug": "beta-monthly"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["url"], "https://checkout.stripe.com/cs_new_1")
        call_kwargs = mock_session_create.call_args.kwargs
        self.assertEqual(call_kwargs["mode"], "subscription")
        self.assertEqual(call_kwargs["line_items"], [{"price": "price_mo", "quantity": 1}])
        self.assertEqual(call_kwargs["client_reference_id"], str(self.user.id))
        self.assertEqual(call_kwargs["subscription_data"]["metadata"]["user_id"], str(self.user.id))
        subscription = Subscription.objects.get(user=self.user)
        self.assertEqual(subscription.status, "checkout_started")
        self.assertEqual(subscription.last_checkout_session_id, "cs_new_1")
        self.assertEqual(subscription.stripe_customer_id, "cus_new_1")

    @patch("fragrance.billing.stripe.checkout.Session.create")
    @patch("fragrance.billing.stripe.Customer.create")
    def test_post_ignores_client_supplied_price_id(self, mock_customer_create, mock_session_create) -> None:
        mock_customer_create.return_value = SimpleNamespace(id="cus_new_2")
        mock_session_create.return_value = SimpleNamespace(id="cs_new_2", url="https://checkout.stripe.com/cs_new_2")

        self.client.post(self.url, {"slug": "beta-monthly", "price_id": "price_evil_free_tier"})

        call_kwargs = mock_session_create.call_args.kwargs
        self.assertEqual(call_kwargs["line_items"], [{"price": "price_mo", "quantity": 1}])

    def test_post_unknown_slug_returns_400(self) -> None:
        response = self.client.post(self.url, {"slug": "does-not-exist"})
        self.assertEqual(response.status_code, 400)

    def test_post_inactive_slug_returns_400(self) -> None:
        self.monthly.is_active = False
        self.monthly.save(update_fields=["is_active"])
        response = self.client.post(self.url, {"slug": "beta-monthly"})
        self.assertEqual(response.status_code, 400)

    def test_post_already_active_returns_409(self) -> None:
        Subscription.objects.create(
            user=self.user, status="active", access_expires_at=timezone.now() + timedelta(days=10)
        )
        response = self.client.post(self.url, {"slug": "beta-monthly"})
        self.assertEqual(response.status_code, 409)

    @patch("fragrance.billing.stripe.checkout.Session.create")
    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_post_reuses_open_session_for_same_plan(self, mock_retrieve, mock_create) -> None:
        Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_existing", last_checkout_session_id="cs_existing"
        )
        mock_retrieve.return_value = SimpleNamespace(
            status="open", url="https://checkout.stripe.com/cs_existing",
            metadata={"plan_slug": "beta-monthly", "price_id": "price_mo"},
            consent_collection=SimpleNamespace(terms_of_service="required"),
        )

        response = self.client.post(self.url, {"slug": "beta-monthly"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["url"], "https://checkout.stripe.com/cs_existing")
        mock_create.assert_not_called()

    @patch("fragrance.billing.stripe.checkout.Session.create")
    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_reuse_path_works_with_a_real_sdk_session_object(self, mock_retrieve, mock_create) -> None:
        # Regression, found by the sandbox walkthrough: on a real session, `.metadata` is a
        # StripeObject, which is NOT a dict and has no `.get()`, so the reuse path raised
        # AttributeError (a 500 on the second click of "Subscribe"). Every other test here builds
        # the session from a SimpleNamespace with a plain-dict metadata, which is why nothing caught
        # it. This one builds a genuine SDK object.
        Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_real_obj", last_checkout_session_id="cs_real_obj"
        )
        real = stripe.checkout.Session.construct_from({
            "id": "cs_real_obj", "object": "checkout.session", "status": "open", "mode": "subscription",
            "url": "https://checkout.stripe.com/cs_real_obj",
            "metadata": {"plan_slug": "beta-monthly", "price_id": "price_mo"},
            "consent_collection": {"terms_of_service": "required"},
        }, "sk_test_dummy")
        self.assertFalse(hasattr(real.metadata, "get") and isinstance(real.metadata, dict))
        mock_retrieve.return_value = real

        response = self.client.post(self.url, {"slug": "beta-monthly"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["url"], "https://checkout.stripe.com/cs_real_obj")
        mock_create.assert_not_called()

    @patch("fragrance.billing.stripe.checkout.Session.expire")
    @patch("fragrance.billing.stripe.checkout.Session.create")
    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_post_expires_open_session_for_different_plan(
        self, mock_retrieve, mock_create, mock_expire
    ) -> None:
        Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_existing_2", last_checkout_session_id="cs_existing_2"
        )
        mock_retrieve.return_value = SimpleNamespace(
            status="open", url="https://checkout.stripe.com/cs_existing_2",
            metadata={"plan_slug": "beta-annual", "price_id": "price_yr"},
        )
        mock_create.return_value = SimpleNamespace(id="cs_new_3", url="https://checkout.stripe.com/cs_new_3")

        response = self.client.post(self.url, {"slug": "beta-monthly"})

        self.assertEqual(response.status_code, 200)
        mock_expire.assert_called_once_with("cs_existing_2")
        mock_create.assert_called_once()

    @patch("fragrance.billing.stripe.checkout.Session.expire")
    @patch("fragrance.billing.stripe.checkout.Session.create")
    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_post_expires_open_session_when_price_id_changed_under_same_slug(
        self, mock_retrieve, mock_create, mock_expire
    ) -> None:
        """
        A stale open session for the *same slug* but an old Price (the
        operator re-ran seed_plans with a new --monthly-price-id) must be
        expired, not reused -- reuse must key on price, not just slug, or a
        user could complete checkout against a retired Price.
        """
        Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_existing_3", last_checkout_session_id="cs_existing_3"
        )
        mock_retrieve.return_value = SimpleNamespace(
            status="open", url="https://checkout.stripe.com/cs_existing_3",
            metadata={"plan_slug": "beta-monthly", "price_id": "price_mo_RETIRED"},
        )
        mock_create.return_value = SimpleNamespace(id="cs_new_4", url="https://checkout.stripe.com/cs_new_4")

        response = self.client.post(self.url, {"slug": "beta-monthly"})

        self.assertEqual(response.status_code, 200)
        mock_expire.assert_called_once_with("cs_existing_3")
        mock_create.assert_called_once()


@override_settings(**_STRIPE_TEST_SETTINGS)
class BillingConfigurationTests(TestCase):
    """Fail-fast guard for missing Stripe credentials, mirroring the ANTHROPIC_API_KEY precedent."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="noconfig", email="noconfig@example.com")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, billing_interval="month", access_days_granted=30,
        )

    @override_settings(STRIPE_SECRET_KEY="")
    def test_checkout_returns_503_when_stripe_not_configured(self) -> None:
        response = self.client.post("/api/v1/billing/checkout/", {"slug": "beta-monthly"})
        self.assertEqual(response.status_code, 503)


class BillingSubscriptionViewTests(TestCase):
    """
    Covers GET /api/v1/billing/subscription/ -- the already-active read used
    by the /billing paywall page. No Stripe calls here (unlike
    BillingSessionView), so no _STRIPE_TEST_SETTINGS override is needed.
    """

    url = "/api/v1/billing/subscription/"

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="subview", email="subview@example.com")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_unauthenticated_returns_401(self) -> None:
        response = APIClient().get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_no_subscription_row_yet_returns_none_status_and_writes_nothing(self) -> None:
        self.assertFalse(Subscription.objects.filter(user=self.user).exists())
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "none",
                "plan": None,
                "access_expires_at": None,
                "has_active_access": False,
                "exempt": False,
                "can_manage_billing": False,
                "quota": {"runs_allowed": 0, "runs_used": 0, "next_eligible_at": None, "can_trigger": False},
            },
        )
        self.assertFalse(Subscription.objects.filter(user=self.user).exists())

    def test_get_never_creates_a_row_for_any_kind_of_user(self) -> None:
        staff = User.objects.create_user(username="substaff", password="pw12345!", is_staff=True)
        client = APIClient()
        client.force_authenticate(user=staff)
        body = client.get(self.url).json()
        self.assertEqual(Subscription.objects.count(), 0)
        self.assertEqual(body["status"], "none")
        self.assertTrue(body["exempt"])
        self.assertTrue(body["has_active_access"])
        self.assertIsNone(body["quota"]["runs_allowed"])

    def test_lapsed_payload_is_unchanged(self) -> None:
        Subscription.objects.create(
            user=self.user, status="active", access_expires_at=timezone.now() - timedelta(days=1)
        )
        body = self.client.get(self.url).json()
        self.assertEqual(body["status"], "active")
        self.assertFalse(body["has_active_access"])
        self.assertFalse(body["exempt"])
        self.assertEqual(body["quota"]["runs_allowed"], 0)
        self.assertFalse(body["quota"]["can_trigger"])

    def test_active_payload_reports_one_run_allowed(self) -> None:
        active_subscription_for(self.user)
        body = self.client.get(self.url).json()
        self.assertTrue(body["has_active_access"])
        self.assertEqual(body["quota"]["runs_allowed"], 1)
        self.assertTrue(body["quota"]["can_trigger"])

    def test_throttle_scope_is_configured_and_fails_open(self) -> None:
        self.assertEqual(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["billing-status"], "60/min")
        with patch.object(
            FailOpenScopedRateThrottle,
            "cache",
            **{"get.side_effect": redis.exceptions.ConnectionError("redis is down")},
        ), self.assertLogs("fragrance.views", level="WARNING") as logs:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("scope=billing-status" in m for m in logs.output))

    def test_active_subscription_reports_plan_and_access(self) -> None:
        plan = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, billing_interval="month", access_days_granted=30,
        )
        expiry = timezone.now() + timedelta(days=10)
        Subscription.objects.create(
            user=self.user, plan=plan, status="active", access_expires_at=expiry
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["plan"], {"slug": "beta-monthly", "name": "Beta Monthly"})
        self.assertTrue(body["has_active_access"])

    def test_scopes_to_the_requesting_user(self) -> None:
        other = User.objects.create_user(username="otheruser", email="other@example.com")
        Subscription.objects.create(
            user=other, status="active", access_expires_at=timezone.now() + timedelta(days=10)
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "none")


@override_settings(**_STRIPE_TEST_SETTINGS)
class FulfillPaymentIntentTests(TestCase):
    """
    One test per rejection branch of the payment_intent.succeeded validation
    chain (fragrance.billing.fulfill_payment_intent / _grant_period_for_invoice),
    since each branch is an authorization decision.
    """

    def setUp(self) -> None:
        self.plan = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, billing_interval="month", access_days_granted=30,
        )
        self.user = User.objects.create_user(username="payer", email="payer@example.com")
        BetaSignup.objects.create(
            email="payer@example.com", normalized_email="payer@example.com",
            status="converted", invited_user=self.user,
        )
        self.subscription = Subscription.objects.create(user=self.user, stripe_customer_id="cus_test_1")

    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_status_not_succeeded_grants_nothing(self, mock_retrieve) -> None:
        mock_retrieve.return_value = _fake_payment_intent(
            pi_id="pi_1", status="requires_action", customer="cus_test_1"
        )
        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 0)
        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.access_expires_at)

    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_null_customer_grants_nothing(self, mock_retrieve) -> None:
        mock_retrieve.return_value = _fake_payment_intent(pi_id="pi_1", status="succeeded", customer=None)
        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 0)

    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_unknown_customer_grants_nothing(self, mock_retrieve) -> None:
        mock_retrieve.return_value = _fake_payment_intent(
            pi_id="pi_1", status="succeeded", customer="cus_unknown"
        )
        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 0)

    @patch("fragrance.billing.stripe.InvoicePayment.list")
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_empty_invoice_payment_list_grants_nothing(self, mock_retrieve, mock_list) -> None:
        mock_retrieve.return_value = _fake_payment_intent(
            pi_id="pi_1", status="succeeded", customer="cus_test_1"
        )
        mock_list.return_value.auto_paging_iter.return_value = iter([])
        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 0)

    @patch("fragrance.billing.stripe.InvoicePayment.list")
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_non_subscription_invoice_parent_grants_nothing(self, mock_retrieve, mock_list) -> None:
        mock_retrieve.return_value = _fake_payment_intent(
            pi_id="pi_1", status="succeeded", customer="cus_test_1"
        )
        invoice = _fake_invoice(
            invoice_id="in_1", customer_id="cus_test_1", subscription_id=None,
            amount_paid=400, lines=[_fake_line(price_id="price_mo", period_end=None)],
            parent_type="quote_details",
        )
        mock_list.return_value.auto_paging_iter.return_value = iter([_fake_invoice_payment(invoice=invoice)])
        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 0)
        self.assertEqual(ProcessedPayment.objects.count(), 0)

    @patch("fragrance.billing.stripe.InvoicePayment.list")
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_unknown_price_id_grants_nothing(self, mock_retrieve, mock_list) -> None:
        mock_retrieve.return_value = _fake_payment_intent(
            pi_id="pi_1", status="succeeded", customer="cus_test_1"
        )
        invoice = _fake_invoice(
            invoice_id="in_1", customer_id="cus_test_1", subscription_id="sub_1",
            amount_paid=400, lines=[_fake_line(price_id="price_totally_unknown", period_end=None)],
        )
        mock_list.return_value.auto_paging_iter.return_value = iter([_fake_invoice_payment(invoice=invoice)])
        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 0)

    @patch("fragrance.billing.stripe.InvoicePayment.list")
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_unapproved_user_grants_nothing(self, mock_retrieve, mock_list) -> None:
        unapproved = User.objects.create_user(username="unapproved", email="unapproved@example.com")
        Subscription.objects.create(user=unapproved, stripe_customer_id="cus_unapproved")
        BetaSignup.objects.create(
            email="unapproved@example.com", normalized_email="unapproved@example.com", status="pending"
        )
        mock_retrieve.return_value = _fake_payment_intent(
            pi_id="pi_2", status="succeeded", customer="cus_unapproved"
        )
        invoice = _fake_invoice(
            invoice_id="in_2", customer_id="cus_unapproved", subscription_id="sub_2",
            amount_paid=400, lines=[_fake_line(price_id="price_mo", period_end=None)],
        )
        mock_list.return_value.auto_paging_iter.return_value = iter([_fake_invoice_payment(invoice=invoice)])
        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_2"), 0)

    @patch("fragrance.billing.stripe.InvoicePayment.list")
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_all_checks_pass_grants_one_period_and_persists_subscription_id(
        self, mock_retrieve, mock_list
    ) -> None:
        mock_retrieve.return_value = _fake_payment_intent(
            pi_id="pi_3", status="succeeded", customer="cus_test_1"
        )
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _fake_invoice(
            invoice_id="in_3", customer_id="cus_test_1", subscription_id="sub_3",
            amount_paid=400, lines=[_fake_line(price_id="price_mo", period_end=period_end)],
        )
        mock_list.return_value.auto_paging_iter.return_value = iter([_fake_invoice_payment(invoice=invoice)])

        granted = fulfill_payment_intent(payment_intent_id="pi_3")

        self.assertEqual(granted, 1)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "active")
        self.assertEqual(self.subscription.stripe_subscription_id, "sub_3")
        expected = datetime.fromtimestamp(period_end, tz=dt_timezone.utc) + timedelta(days=2)
        self.assertEqual(self.subscription.access_expires_at, expected)
        self.assertEqual(ProcessedPayment.objects.filter(stripe_invoice_id="in_3").count(), 1)

    @patch("fragrance.billing.stripe.InvoicePayment.list")
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_falls_back_to_access_days_granted_when_period_end_absent(
        self, mock_retrieve, mock_list
    ) -> None:
        mock_retrieve.return_value = _fake_payment_intent(
            pi_id="pi_4", status="succeeded", customer="cus_test_1"
        )
        invoice = _fake_invoice(
            invoice_id="in_4", customer_id="cus_test_1", subscription_id="sub_4",
            amount_paid=400, lines=[_fake_line(price_id="price_mo", period_end=None)],
        )
        mock_list.return_value.auto_paging_iter.return_value = iter([_fake_invoice_payment(invoice=invoice)])
        before = timezone.now()

        fulfill_payment_intent(payment_intent_id="pi_4")

        self.subscription.refresh_from_db()
        self.assertGreaterEqual(
            self.subscription.access_expires_at, before + timedelta(days=30) - timedelta(seconds=5)
        )
        self.assertLessEqual(
            self.subscription.access_expires_at, before + timedelta(days=30) + timedelta(seconds=5)
        )

    @patch("fragrance.billing.stripe.InvoicePayment.list")
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_two_invoices_grant_two_periods(self, mock_retrieve, mock_list) -> None:
        mock_retrieve.return_value = _fake_payment_intent(
            pi_id="pi_5", status="succeeded", customer="cus_test_1"
        )
        now_ts = int(timezone.now().timestamp())
        invoice_a = _fake_invoice(
            invoice_id="in_5a", customer_id="cus_test_1", subscription_id="sub_5",
            amount_paid=400, lines=[_fake_line(price_id="price_mo", period_end=now_ts + 30 * 86400)],
        )
        invoice_b = _fake_invoice(
            invoice_id="in_5b", customer_id="cus_test_1", subscription_id="sub_5",
            amount_paid=400, lines=[_fake_line(price_id="price_mo", period_end=now_ts + 60 * 86400)],
        )
        mock_list.return_value.auto_paging_iter.return_value = iter(
            [_fake_invoice_payment(invoice=invoice_a), _fake_invoice_payment(invoice=invoice_b)]
        )

        granted = fulfill_payment_intent(payment_intent_id="pi_5")

        self.assertEqual(granted, 2)
        self.assertEqual(ProcessedPayment.objects.count(), 2)
        self.subscription.refresh_from_db()
        expected = datetime.fromtimestamp(now_ts + 60 * 86400, tz=dt_timezone.utc) + timedelta(days=2)
        self.assertEqual(self.subscription.access_expires_at, expected)

    @patch("fragrance.billing.stripe.InvoicePayment.list")
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_same_invoice_processed_twice_grants_once(self, mock_retrieve, mock_list) -> None:
        mock_retrieve.return_value = _fake_payment_intent(
            pi_id="pi_6", status="succeeded", customer="cus_test_1"
        )
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _fake_invoice(
            invoice_id="in_6", customer_id="cus_test_1", subscription_id="sub_6",
            amount_paid=400, lines=[_fake_line(price_id="price_mo", period_end=period_end)],
        )
        mock_list.return_value.auto_paging_iter.side_effect = [
            iter([_fake_invoice_payment(invoice=invoice)]),
            iter([_fake_invoice_payment(invoice=invoice)]),
        ]

        first = fulfill_payment_intent(payment_intent_id="pi_6")
        second = fulfill_payment_intent(payment_intent_id="pi_6")

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(ProcessedPayment.objects.filter(stripe_invoice_id="in_6").count(), 1)
        self.subscription.refresh_from_db()
        expected = datetime.fromtimestamp(period_end, tz=dt_timezone.utc) + timedelta(days=2)
        self.assertEqual(self.subscription.access_expires_at, expected)


@override_settings(**_STRIPE_TEST_SETTINGS)
class FulfillCheckoutSessionTests(TestCase):
    def setUp(self) -> None:
        self.plan = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, billing_interval="month", access_days_granted=30,
        )
        self.user = User.objects.create_user(username="sessionuser", email="sessionuser@example.com")
        BetaSignup.objects.create(
            email="sessionuser@example.com", normalized_email="sessionuser@example.com",
            status="approved", invited_user=self.user,
        )
        self.subscription = Subscription.objects.create(user=self.user, stripe_customer_id="cus_session_1")

    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_client_reference_id_mismatch_raises_ownership_error(self, mock_retrieve) -> None:
        mock_retrieve.return_value = SimpleNamespace(
            client_reference_id="999999", customer="cus_session_1", invoice=None, subscription=None,
        )
        with self.assertRaises(SessionOwnershipError):
            fulfill_checkout_session(session_id="cs_test_1", user=self.user)

    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_unknown_session_id_raises_not_found(self, mock_retrieve) -> None:
        mock_retrieve.side_effect = stripe.InvalidRequestError("no such session", None)
        with self.assertRaises(SessionNotFoundError):
            fulfill_checkout_session(session_id="cs_bogus", user=self.user)

    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_grants_period_from_session_invoice(self, mock_retrieve) -> None:
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _fake_invoice(
            invoice_id="in_session_1", customer_id="cus_session_1", subscription_id="sub_session_1",
            amount_paid=400, lines=[_fake_line(price_id="price_mo", period_end=period_end)],
        )
        mock_retrieve.return_value = SimpleNamespace(
            client_reference_id=str(self.user.id), customer="cus_session_1",
            invoice=invoice, subscription="sub_session_1",
        )

        result = fulfill_checkout_session(session_id="cs_test_2", user=self.user)

        self.assertTrue(result["granted"])
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["plan"], {"slug": "beta-monthly", "name": "Beta Monthly"})
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.stripe_subscription_id, "sub_session_1")

    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    @patch("fragrance.billing.stripe.InvoicePayment.list")
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_same_invoice_via_webhook_and_success_page_grants_once(
        self, mock_pi_retrieve, mock_list, mock_session_retrieve
    ) -> None:
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _fake_invoice(
            invoice_id="in_dual_1", customer_id="cus_session_1", subscription_id="sub_dual_1",
            amount_paid=400, lines=[_fake_line(price_id="price_mo", period_end=period_end)],
        )
        mock_pi_retrieve.return_value = _fake_payment_intent(
            pi_id="pi_dual", status="succeeded", customer="cus_session_1"
        )
        mock_list.return_value.auto_paging_iter.return_value = iter([_fake_invoice_payment(invoice=invoice)])
        mock_session_retrieve.return_value = SimpleNamespace(
            client_reference_id=str(self.user.id), customer="cus_session_1",
            invoice=invoice, subscription="sub_dual_1",
        )

        fulfill_payment_intent(payment_intent_id="pi_dual")
        self.subscription.refresh_from_db()
        first_expiry = self.subscription.access_expires_at

        fulfill_checkout_session(session_id="cs_dual", user=self.user)
        self.subscription.refresh_from_db()

        self.assertEqual(self.subscription.access_expires_at, first_expiry)
        self.assertEqual(ProcessedPayment.objects.filter(stripe_invoice_id="in_dual_1").count(), 1)

    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_open_unpaid_invoice_grants_nothing(self, mock_retrieve) -> None:
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _fake_invoice(
            invoice_id="in_unpaid_1", customer_id="cus_session_1", subscription_id="sub_unpaid_1",
            amount_paid=0, status="open", amount_remaining=400,
            lines=[_fake_line(price_id="price_mo", period_end=period_end)],
        )
        mock_retrieve.return_value = SimpleNamespace(
            client_reference_id=str(self.user.id), customer="cus_session_1",
            invoice=invoice, subscription="sub_unpaid_1", payment_status="paid",
        )

        result = fulfill_checkout_session(session_id="cs_unpaid_inv", user=self.user)

        self.assertFalse(result["granted"])
        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.access_expires_at)
        self.assertFalse(self.subscription.has_active_access)
        self.assertNotEqual(self.subscription.status, "active")
        self.assertEqual(ProcessedPayment.objects.count(), 0)

    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_unpaid_session_grants_nothing(self, mock_retrieve) -> None:
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _fake_invoice(
            invoice_id="in_unpaid_2", customer_id="cus_session_1", subscription_id="sub_unpaid_2",
            amount_paid=400, lines=[_fake_line(price_id="price_mo", period_end=period_end)],
        )
        mock_retrieve.return_value = SimpleNamespace(
            client_reference_id=str(self.user.id), customer="cus_session_1",
            invoice=invoice, subscription="sub_unpaid_2", payment_status="unpaid",
        )

        result = fulfill_checkout_session(session_id="cs_unpaid_sess", user=self.user)

        self.assertFalse(result["granted"])
        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.access_expires_at)
        self.assertEqual(ProcessedPayment.objects.count(), 0)

    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_paid_session_grants_exactly_one_period(self, mock_retrieve) -> None:
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _fake_invoice(
            invoice_id="in_paid_1", customer_id="cus_session_1", subscription_id="sub_paid_1",
            amount_paid=400, lines=[_fake_line(price_id="price_mo", period_end=period_end)],
        )
        mock_retrieve.return_value = SimpleNamespace(
            client_reference_id=str(self.user.id), customer="cus_session_1",
            invoice=invoice, subscription="sub_paid_1", payment_status="paid",
        )

        fulfill_checkout_session(session_id="cs_paid", user=self.user)
        self.subscription.refresh_from_db()
        first_expiry = self.subscription.access_expires_at
        fulfill_checkout_session(session_id="cs_paid", user=self.user)
        self.subscription.refresh_from_db()

        self.assertIsNotNone(first_expiry)
        self.assertEqual(self.subscription.access_expires_at, first_expiry)
        self.assertEqual(ProcessedPayment.objects.filter(stripe_invoice_id="in_paid_1").count(), 1)

    @patch("fragrance.billing.stripe.InvoicePayment.list")
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_webhook_path_refuses_unpaid_invoice_too(self, mock_pi_retrieve, mock_list) -> None:
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _fake_invoice(
            invoice_id="in_wh_open", customer_id="cus_session_1", subscription_id="sub_wh_open",
            amount_paid=0, status="open", amount_remaining=400,
            lines=[_fake_line(price_id="price_mo", period_end=period_end)],
        )
        mock_pi_retrieve.return_value = _fake_payment_intent(
            pi_id="pi_wh_open", status="succeeded", customer="cus_session_1"
        )
        mock_list.return_value.auto_paging_iter.return_value = iter([_fake_invoice_payment(invoice=invoice)])

        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_wh_open"), 0)
        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.access_expires_at)


@override_settings(**_STRIPE_TEST_SETTINGS)
class BillingSessionViewTests(TestCase):
    """
    HTTP-layer coverage for GET /api/v1/billing/session/<id>/, complementing
    FulfillCheckoutSessionTests (which calls billing.fulfill_checkout_session
    directly): confirms the view returns the same
    {status, plan, access_expires_at, has_active_access} shape as
    BillingSubscriptionView, plus "granted", and that ownership/not-found
    both collapse to an identical 404.
    """

    url_tpl = "/api/v1/billing/session/{}/"

    def setUp(self) -> None:
        self.plan = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, billing_interval="month", access_days_granted=30,
        )
        self.user = User.objects.create_user(username="sessionview", email="sessionview@example.com")
        BetaSignup.objects.create(
            email="sessionview@example.com", normalized_email="sessionview@example.com",
            status="approved", invited_user=self.user,
        )
        self.subscription = Subscription.objects.create(user=self.user, stripe_customer_id="cus_view_1")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_unauthenticated_returns_401(self) -> None:
        response = APIClient().get(self.url_tpl.format("cs_anything"))
        self.assertEqual(response.status_code, 401)

    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_unknown_session_returns_404(self, mock_retrieve) -> None:
        mock_retrieve.side_effect = stripe.InvalidRequestError("no such session", None)
        response = self.client.get(self.url_tpl.format("cs_bogus"))
        self.assertEqual(response.status_code, 404)

    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_other_users_session_returns_404_not_403(self, mock_retrieve) -> None:
        mock_retrieve.return_value = SimpleNamespace(
            client_reference_id="999999", customer="cus_view_1", invoice=None, subscription=None,
        )
        response = self.client.get(self.url_tpl.format("cs_not_mine"))
        self.assertEqual(response.status_code, 404)

    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_success_returns_shared_subscription_shape_plus_granted(self, mock_retrieve) -> None:
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _fake_invoice(
            invoice_id="in_view_1", customer_id="cus_view_1", subscription_id="sub_view_1",
            amount_paid=400, lines=[_fake_line(price_id="price_mo", period_end=period_end)],
        )
        mock_retrieve.return_value = SimpleNamespace(
            client_reference_id=str(self.user.id), customer="cus_view_1",
            invoice=invoice, subscription="sub_view_1",
        )

        response = self.client.get(self.url_tpl.format("cs_view_1"))

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body.keys()),
            {"status", "plan", "access_expires_at", "has_active_access", "exempt", "can_manage_billing", "quota", "granted"},
        )
        self.assertTrue(body["granted"])
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["plan"], {"slug": "beta-monthly", "name": "Beta Monthly"})
        self.assertTrue(body["has_active_access"])

    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_active_access_short_circuits_without_calling_stripe(self, mock_retrieve) -> None:
        self.subscription.status = "active"
        self.subscription.plan = self.plan
        self.subscription.access_expires_at = timezone.now() + timedelta(days=10)
        self.subscription.save()

        response = self.client.get(self.url_tpl.format("cs_already_active"))

        mock_retrieve.assert_not_called()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body.keys()),
            {"status", "plan", "access_expires_at", "has_active_access", "exempt", "can_manage_billing", "quota", "granted"},
        )
        self.assertTrue(body["has_active_access"])
        self.assertFalse(body["granted"])

    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_throttled_after_scope_limit(self, mock_retrieve) -> None:
        from django.core.cache import cache
        cache.clear()
        mock_retrieve.side_effect = stripe.InvalidRequestError("no such session", None)
        codes = [self.client.get(self.url_tpl.format("cs_x")).status_code for _ in range(31)]
        self.assertEqual(codes[:30], [404] * 30)
        self.assertEqual(codes[30], 429)
        cache.clear()


@override_settings(**_STRIPE_TEST_SETTINGS)
class BillingThrottleFailOpenTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="failopen", password="pw12345!")
        Subscription.objects.create(user=self.user, stripe_customer_id="cus_failopen")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.plan = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_fo",
            amount_cents=400, currency="usd", billing_interval="month", access_days_granted=30,
        )

    def _down(self):
        return patch.object(
            FailOpenScopedRateThrottle,
            "cache",
            **{"get.side_effect": redis.exceptions.ConnectionError("redis is down")},
        )

    def test_checkout_get_served_and_warns_when_cache_down(self) -> None:
        with self._down(), self.assertLogs("fragrance.views", level="WARNING") as logs:
            response = self.client.get("/api/v1/billing/checkout/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("throttle cache unavailable" in m for m in logs.output))

    @patch("fragrance.billing.stripe.checkout.Session.create")
    def test_checkout_post_not_500_when_cache_down(self, mock_create) -> None:
        mock_create.return_value = type("S", (), {"url": "https://stripe.test/x", "id": "cs_1"})()
        with self._down(), self.assertLogs("fragrance.views", level="WARNING") as logs:
            response = self.client.post(
                "/api/v1/billing/checkout/", {"plan": "beta-monthly"}, format="json"
            )
        self.assertNotEqual(response.status_code, 500)
        self.assertTrue(any("throttle cache unavailable" in m for m in logs.output))

    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_session_not_500_when_cache_down(self, mock_retrieve) -> None:
        mock_retrieve.side_effect = stripe.InvalidRequestError("no such session", None)
        with self._down(), self.assertLogs("fragrance.views", level="WARNING") as logs:
            response = self.client.get("/api/v1/billing/session/cs_x/")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(any("throttle cache unavailable" in m for m in logs.output))


class PasswordResetEmailLookupTests(TestCase):
    url = "/api/v1/auth/password/reset/"

    def test_case_insensitive_email_match_sends_mail(self) -> None:
        User.objects.create_user(username="dave", email="Dave@example.com")
        mail.outbox.clear()
        with _run_reset_task_inline():
            response = APIClient().post(self.url, {"email": "dave@example.com"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

    def test_duplicate_emails_do_not_500(self) -> None:
        User.objects.create_user(username="dup1", email="dup@example.com")
        User.objects.create_user(username="dup2", email="dup@example.com")
        mail.outbox.clear()
        with _run_reset_task_inline():
            response = APIClient().post(self.url, {"email": "dup@example.com"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 2)


class PasswordResetQueuedTests(TestCase):
    url = "/api/v1/auth/password/reset/"

    def _post(self, email: str):
        return APIClient().post(self.url, {"email": email}, format="json")

    def test_zero_one_and_many_matches_return_identical_response(self) -> None:
        User.objects.create_user(username="one", email="one@example.com")
        User.objects.create_user(username="m1", email="many@example.com")
        User.objects.create_user(username="m2", email="Many@example.com")
        with patch("fragrance.tasks.send_password_reset_email.delay"):
            none = self._post("nobody@example.com")
            one = self._post("one@example.com")
            many = self._post("many@example.com")
        self.assertEqual(none.status_code, 200)
        self.assertEqual(one.status_code, 200)
        self.assertEqual(many.status_code, 200)
        self.assertEqual(none.content, one.content)
        self.assertEqual(none.content, many.content)

    def test_mail_is_queued_per_account_not_sent_inline(self) -> None:
        u1 = User.objects.create_user(username="q1", email="q@example.com")
        u2 = User.objects.create_user(username="q2", email="Q@example.com")
        mail.outbox.clear()
        with patch("fragrance.tasks.send_password_reset_email.delay") as mock_delay:
            response = self._post("q@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(mock_delay.call_count, 2)
        self.assertEqual(
            [c.kwargs for c in mock_delay.call_args_list],
            [{"user_id": u1.pk}, {"user_id": u2.pk}],
        )

    def test_no_match_enqueues_nothing(self) -> None:
        with patch("fragrance.tasks.send_password_reset_email.delay") as mock_delay:
            self._post("ghost@example.com")
        mock_delay.assert_not_called()

    def test_broker_failure_still_returns_uniform_response(self) -> None:
        from kombu.exceptions import OperationalError

        User.objects.create_user(username="b1", email="b@example.com")
        with patch("fragrance.tasks.send_password_reset_email.delay") as mock_delay:
            mock_delay.side_effect = OperationalError("broker down")
            with self.assertLogs("fragrance.views", level="ERROR"):
                response = self._post("b@example.com")
            control = self._post("ghost@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, control.content)

    def test_task_sends_mail_with_uid_and_token_in_query_string(self) -> None:
        from urllib.parse import parse_qs, urlparse

        user = User.objects.create_user(username="t1", email="t@example.com", password=None)
        self.assertFalse(user.has_usable_password())
        mail.outbox.clear()
        send_password_reset_email.apply(kwargs={"user_id": user.pk})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["t@example.com"])
        url = re.search(r"https?://\S+", mail.outbox[0].body).group(0)
        query = parse_qs(urlparse(url).query)
        self.assertIn("uid", query)
        self.assertIn("token", query)

    def test_task_with_missing_user_sends_nothing(self) -> None:
        mail.outbox.clear()
        send_password_reset_email.apply(kwargs={"user_id": 999999})
        self.assertEqual(len(mail.outbox), 0)

    def test_task_retries_on_send_failure(self) -> None:
        user = User.objects.create_user(username="r1", email="r@example.com")
        with patch("fragrance.tasks.send_mail", side_effect=RuntimeError("smtp down")):
            with patch.object(send_password_reset_email, "retry", side_effect=RuntimeError("retried")) as mock_retry:
                result = send_password_reset_email.apply(kwargs={"user_id": user.pk})
        self.assertTrue(result.failed())
        mock_retry.assert_called_once()

    def test_unknown_email_returns_same_response_and_sends_nothing(self) -> None:
        mail.outbox.clear()
        response = APIClient().post(self.url, {"email": "nobody@example.com"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)


@override_settings(**_STRIPE_TEST_SETTINGS)
class RevokeForDisputeTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="disputeduser", email="disputeduser@example.com")
        self.subscription = Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_dispute_1", status="active",
            access_expires_at=timezone.now() + timedelta(days=20),
        )

    @patch("fragrance.billing.stripe.Subscription.list", return_value=stripe.ListObject.construct_from({"object": "list", "data": [], "has_more": False}, "sk_test"))
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_resolvable_dispute_revokes_immediately(self, mock_retrieve, _mock_list) -> None:
        mock_retrieve.return_value = _fake_payment_intent(
            pi_id="pi_dispute", status="succeeded", customer="cus_dispute_1"
        )
        before = timezone.now()

        revoke_for_dispute(dispute={"payment_intent": "pi_dispute"})

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")
        self.assertGreaterEqual(self.subscription.access_expires_at, before)

    def test_missing_payment_intent_revokes_nothing(self) -> None:
        revoke_for_dispute(dispute={})
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "active")
        self.assertIsNotNone(self.subscription.access_expires_at)

    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_unknown_customer_revokes_nothing(self, mock_retrieve) -> None:
        mock_retrieve.return_value = _fake_payment_intent(pi_id="pi_x", status="succeeded", customer="cus_unknown")
        revoke_for_dispute(dispute={"payment_intent": "pi_x"})
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "active")

    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_unresolvable_charge_revokes_nothing(self, mock_retrieve) -> None:
        mock_retrieve.side_effect = stripe.InvalidRequestError("no such payment_intent", None)
        revoke_for_dispute(dispute={"payment_intent": "pi_missing"})
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "active")

    @patch("fragrance.billing.stripe.Subscription.list", return_value=stripe.ListObject.construct_from({"object": "list", "data": [], "has_more": False}, "sk_test"))
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_expanded_payment_intent_object_is_normalized_to_its_id(self, mock_retrieve, _mock_list) -> None:
        """
        Dispute.payment_intent is Optional[ExpandableField["PaymentIntent"]]
        -- if the event ever arrives with that field expanded, it's a nested
        object, not a bare id string. PaymentIntent.retrieve must still be
        called with the id, not the dict.
        """
        mock_retrieve.return_value = _fake_payment_intent(
            pi_id="pi_dispute", status="succeeded", customer="cus_dispute_1"
        )

        revoke_for_dispute(dispute={"payment_intent": {"id": "pi_dispute", "object": "payment_intent"}})

        mock_retrieve.assert_called_once_with("pi_dispute")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")


def _sub_list(*, statuses: dict[str, str]) -> stripe.ListObject:
    return stripe.ListObject.construct_from(
        {
            "object": "list",
            "has_more": False,
            "data": [
                {"id": sub_id, "object": "subscription", "status": status}
                for sub_id, status in statuses.items()
            ],
        },
        "sk_test",
    )


@override_settings(**_STRIPE_TEST_SETTINGS)
class DisputeCancelsStripeSubscriptionTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dispcancel", email="dispcancel@example.com")
        self.subscription = Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_dc_1", status="active",
            access_expires_at=timezone.now() + timedelta(days=20),
        )
        patcher = patch(
            "fragrance.billing.stripe.PaymentIntent.retrieve",
            return_value=_fake_payment_intent(pi_id="pi_dc", status="succeeded", customer="cus_dc_1"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch("fragrance.billing.stripe.Subscription.cancel")
    @patch("fragrance.billing.stripe.Subscription.list")
    def test_cancels_every_live_subscription_immediately_without_proration(self, mock_list, mock_cancel) -> None:
        mock_list.return_value = _sub_list(statuses={
            "sub_a": "active", "sub_b": "past_due", "sub_c": "unpaid", "sub_d": "trialing",
            "sub_e": "incomplete", "sub_f": "canceled", "sub_g": "incomplete_expired", "sub_h": "paused",
        })
        with self.assertLogs("fragrance.billing", level="CRITICAL") as logs:
            revoke_for_dispute(dispute={"payment_intent": "pi_dc"})
        cancelled = [call.args[0] for call in mock_cancel.call_args_list]
        self.assertEqual(cancelled, ["sub_a", "sub_b", "sub_c", "sub_d", "sub_e"])
        for call in mock_cancel.call_args_list:
            self.assertEqual(call.kwargs, {"invoice_now": False, "prorate": False})
        self.assertEqual(mock_list.call_args.kwargs["customer"], "cus_dc_1")
        self.assertIn("sub_a", logs.output[0])
        self.assertNotIn("sk_test", logs.output[0])
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")

    @patch("fragrance.billing.stripe.Subscription.cancel")
    @patch("fragrance.billing.stripe.Subscription.list")
    def test_no_live_subscriptions_is_fine_and_still_revokes(self, mock_list, mock_cancel) -> None:
        mock_list.return_value = _sub_list(statuses={"sub_old": "canceled"})
        revoke_for_dispute(dispute={"payment_intent": "pi_dc"})
        mock_cancel.assert_not_called()
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    @patch("fragrance.billing.stripe.Subscription.cancel")
    @patch("fragrance.billing.stripe.Subscription.list")
    def test_already_canceled_race_counts_as_done(self, mock_list, mock_cancel, mock_retrieve) -> None:
        mock_list.return_value = _sub_list(statuses={"sub_a": "active"})
        mock_cancel.side_effect = stripe.InvalidRequestError("already canceled", None)
        mock_retrieve.return_value = SimpleNamespace(status="canceled")
        revoke_for_dispute(dispute={"payment_intent": "pi_dc"})
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")

    @patch("fragrance.billing.stripe.Subscription.cancel")
    @patch("fragrance.billing.stripe.Subscription.list")
    def test_cancel_failure_raises_after_attempting_all_and_keeps_local_revoke(self, mock_list, mock_cancel) -> None:
        mock_list.return_value = _sub_list(statuses={"sub_a": "active", "sub_b": "active"})
        mock_cancel.side_effect = [stripe.APIConnectionError("down"), SimpleNamespace(id="sub_b")]
        with self.assertRaises(stripe.APIConnectionError):
            revoke_for_dispute(dispute={"payment_intent": "pi_dc"})
        self.assertEqual(mock_cancel.call_count, 2)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")

    @patch("fragrance.billing.stripe.Subscription.cancel")
    @patch("fragrance.billing.stripe.Subscription.list")
    def test_task_retries_on_cancel_failure_then_replay_is_inert(self, mock_list, mock_cancel) -> None:
        mock_list.return_value = _sub_list(statuses={"sub_a": "active"})
        mock_cancel.side_effect = stripe.APIConnectionError("down")
        StripeEvent.objects.create(
            stripe_event_id="evt_dc", event_type="charge.dispute.created",
            payload={"data": {"object": {"payment_intent": "pi_dc"}}},
        )
        with patch.object(process_stripe_event, "retry", side_effect=RuntimeError("retried")) as mock_retry:
            result = process_stripe_event.apply(kwargs={"stripe_event_id": "evt_dc"})
        self.assertTrue(result.failed())
        mock_retry.assert_called_once()
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_dc").status, "failed")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")
        first_expiry = self.subscription.access_expires_at

        # Retry: Stripe recovers; the sub is now canceled, so nothing left to cancel.
        mock_cancel.reset_mock(side_effect=True)
        mock_list.return_value = _sub_list(statuses={"sub_a": "canceled"})
        process_stripe_event(stripe_event_id="evt_dc")
        mock_cancel.assert_not_called()
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_dc").status, "processed")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.access_expires_at, first_expiry)

    @patch("fragrance.billing.stripe.Subscription.cancel")
    @patch("fragrance.billing.stripe.Subscription.list")
    def test_unresolved_customer_cancels_nothing(self, mock_list, mock_cancel) -> None:
        with patch(
            "fragrance.billing.stripe.PaymentIntent.retrieve",
            return_value=_fake_payment_intent(pi_id="pi_x", status="succeeded", customer="cus_unknown"),
        ):
            revoke_for_dispute(dispute={"payment_intent": "pi_x"})
        mock_list.assert_not_called()
        mock_cancel.assert_not_called()
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "active")

    @patch("fragrance.billing.stripe.Subscription.cancel")
    @patch("fragrance.billing.stripe.Subscription.list")
    def test_transient_payment_intent_failure_raises_and_cancels_nothing(self, mock_list, mock_cancel) -> None:
        with patch("fragrance.billing.stripe.PaymentIntent.retrieve", side_effect=stripe.APIConnectionError("down")):
            with self.assertRaises(stripe.APIConnectionError):
                revoke_for_dispute(dispute={"payment_intent": "pi_dc"})
        mock_cancel.assert_not_called()
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "active")


@override_settings(**_STRIPE_TEST_SETTINGS)
class StripeWebhookViewTests(TestCase):
    slash_url = "/api/v1/billing/webhook/stripe/"
    noslash_url = "/api/v1/billing/webhook/stripe"

    def _signed_post(self, url: str, body: bytes, secret: str = "whsec_test_dummy"):
        sig = stripe.WebhookSignature.generate_signature_header(payload=body.decode(), secret=secret)
        return self.client.post(url, data=body, content_type="application/json", HTTP_STRIPE_SIGNATURE=sig)

    @patch("fragrance.tasks.process_stripe_event.delay")
    def test_valid_signature_creates_event_and_queues_processing(self, mock_delay) -> None:
        body = json.dumps({
            "id": "evt_test_1", "object": "event", "type": "payment_intent.succeeded",
            "created": 1758300000, "data": {"object": {"id": "pi_1", "status": "succeeded"}},
        }).encode()

        response = self._signed_post(self.slash_url, body)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(StripeEvent.objects.filter(stripe_event_id="evt_test_1").exists())
        mock_delay.assert_called_once_with(stripe_event_id="evt_test_1")

    @patch("fragrance.tasks.process_stripe_event.delay")
    def test_event_with_decimal_typed_field_is_stored_not_500(self, mock_delay) -> None:
        # Regression, found by the sandbox walkthrough: the stripe SDK parses Stripe's `*_decimal`
        # string fields (Price.unit_amount_decimal, invoice line amounts, ...) into Python Decimal,
        # and `event.to_dict()` keeps them. StripeEvent.payload is a JSONField, so storing
        # `event.to_dict()` raised "Decimal is not JSON serializable" and returned a 500 for any
        # real event carrying such a field, which Stripe then retries for three days. The mocks
        # never caught it because no fake event had a decimal-typed field. We now store the
        # signature-verified request body as JSON, which is lossless and needs no SDK conversion.
        body = json.dumps({
            "id": "evt_decimal_1", "object": "event", "type": "price.created",
            "created": 1758300000,
            "data": {"object": {
                "id": "price_dec_1", "object": "price", "currency": "usd",
                "unit_amount": 400, "unit_amount_decimal": "400",
                "recurring": {"interval": "month"},
            }},
        }).encode()

        response = self._signed_post(self.slash_url, body)

        self.assertEqual(response.status_code, 200)
        stored = StripeEvent.objects.get(stripe_event_id="evt_decimal_1")
        self.assertEqual(stored.payload["data"]["object"]["unit_amount_decimal"], "400")
        mock_delay.assert_called_once_with(stripe_event_id="evt_decimal_1")

    def test_tampered_body_returns_400(self) -> None:
        body = json.dumps({
            "id": "evt_bad", "object": "event", "type": "payment_intent.succeeded",
            "created": 1758300000, "data": {"object": {}},
        }).encode()
        sig = stripe.WebhookSignature.generate_signature_header(payload=body.decode(), secret="whsec_test_dummy")
        tampered_body = body.replace(b"evt_bad", b"evt_evil!")

        response = self.client.post(
            self.slash_url, data=tampered_body, content_type="application/json", HTTP_STRIPE_SIGNATURE=sig
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(StripeEvent.objects.filter(stripe_event_id="evt_evil").exists())

    def test_wrong_secret_returns_400(self) -> None:
        body = json.dumps({
            "id": "evt_wrong_secret", "object": "event", "type": "payment_intent.succeeded",
            "created": 1758300000, "data": {"object": {}},
        }).encode()
        response = self._signed_post(self.slash_url, body, secret="whsec_totally_different")
        self.assertEqual(response.status_code, 400)

    @patch("fragrance.tasks.process_stripe_event.delay")
    def test_replayed_event_id_returns_200_without_second_queue(self, mock_delay) -> None:
        body = json.dumps({
            "id": "evt_replay", "object": "event", "type": "payment_intent.succeeded",
            "created": 1758300000, "data": {"object": {"id": "pi_1"}},
        }).encode()

        first = self._signed_post(self.slash_url, body)
        second = self._signed_post(self.slash_url, body)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(StripeEvent.objects.filter(stripe_event_id="evt_replay").count(), 1)
        mock_delay.assert_called_once_with(stripe_event_id="evt_replay")

    @patch("fragrance.tasks.process_stripe_event.delay")
    def test_unsubscribed_event_type_returns_200_and_is_stored(self, mock_delay) -> None:
        body = json.dumps({
            "id": "evt_unsub", "object": "event", "type": "customer.created",
            "created": 1758300000, "data": {"object": {"id": "cus_1"}},
        }).encode()

        response = self._signed_post(self.slash_url, body)

        self.assertEqual(response.status_code, 200)
        event_row = StripeEvent.objects.get(stripe_event_id="evt_unsub")
        self.assertEqual(event_row.event_type, "customer.created")
        mock_delay.assert_called_once()

    def test_both_url_spellings_reach_view_without_redirect(self) -> None:
        body_slash = json.dumps({
            "id": "evt_slash", "object": "event", "type": "customer.created",
            "created": 1758300000, "data": {"object": {}},
        }).encode()
        body_noslash = json.dumps({
            "id": "evt_noslash", "object": "event", "type": "customer.created",
            "created": 1758300000, "data": {"object": {}},
        }).encode()

        with patch("fragrance.tasks.process_stripe_event.delay"):
            response_slash = self._signed_post(self.slash_url, body_slash)
            response_noslash = self._signed_post(self.noslash_url, body_noslash)

        self.assertEqual(response_slash.status_code, 200)
        self.assertEqual(response_noslash.status_code, 200)

    def test_get_not_allowed(self) -> None:
        response = self.client.get(self.slash_url)
        self.assertEqual(response.status_code, 405)


@override_settings(**_STRIPE_TEST_SETTINGS)
class ProcessStripeEventTaskTests(TestCase):
    def test_missing_event_row_is_a_noop(self) -> None:
        process_stripe_event(stripe_event_id="evt_missing")  # must not raise

    @patch("fragrance.billing.fulfill_payment_intent")
    def test_payment_intent_succeeded_dispatches_to_fulfillment(self, mock_fulfill) -> None:
        StripeEvent.objects.create(
            stripe_event_id="evt_pi", event_type="payment_intent.succeeded",
            payload={"data": {"object": {"id": "pi_dispatch"}}},
        )
        process_stripe_event(stripe_event_id="evt_pi")
        mock_fulfill.assert_called_once_with(payment_intent_id="pi_dispatch")
        row = StripeEvent.objects.get(stripe_event_id="evt_pi")
        self.assertEqual(row.status, "processed")
        self.assertIsNotNone(row.processed_at)

    @patch("fragrance.billing.revoke_for_dispute")
    def test_charge_dispute_created_dispatches_to_revocation(self, mock_revoke) -> None:
        StripeEvent.objects.create(
            stripe_event_id="evt_dispute", event_type="charge.dispute.created",
            payload={"data": {"object": {"payment_intent": "pi_d"}}},
        )
        process_stripe_event(stripe_event_id="evt_dispute")
        mock_revoke.assert_called_once_with(dispute={"payment_intent": "pi_d"})
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_dispute").status, "processed")

    def test_unhandled_event_type_is_a_noop_and_marked_processed(self) -> None:
        StripeEvent.objects.create(
            stripe_event_id="evt_other", event_type="customer.created",
            payload={"data": {"object": {}}},
        )
        process_stripe_event(stripe_event_id="evt_other")
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_other").status, "processed")


class SanitizeStripeErrorMessageTests(TestCase):
    def test_stripe_error_is_sanitized(self) -> None:
        from fragrance.ai_providers.base import sanitize_sdk_error_message

        exc = stripe.InvalidRequestError("super secret internal detail sk_live_abc123", None)
        message = sanitize_sdk_error_message(exc)
        self.assertNotIn("sk_live_abc123", message)

    def test_connection_error_has_its_own_fixed_message_without_leaking(self) -> None:
        from fragrance.ai_providers.base import sanitize_sdk_error_message

        exc = stripe.APIConnectionError("could not connect to sk_live_abc123 at api.stripe.com")
        message = sanitize_sdk_error_message(exc)
        self.assertEqual(message, "Could not reach Stripe. Try again shortly.")
        self.assertNotIn("sk_live_abc123", message)
        self.assertNotIn("Check the Stripe configuration", message)

    def test_non_connection_stripe_error_keeps_config_message(self) -> None:
        from fragrance.ai_providers.base import sanitize_sdk_error_message

        message = sanitize_sdk_error_message(stripe.AuthenticationError("bad key sk_live_zzz"))
        self.assertIn("Check the Stripe configuration", message)
        self.assertNotIn("sk_live_zzz", message)

    def test_non_sdk_exception_passes_through_unchanged(self) -> None:
        from fragrance.ai_providers.base import sanitize_sdk_error_message

        exc = ValueError("plain application error")
        self.assertEqual(sanitize_sdk_error_message(exc), "plain application error")


class SubscriptionPermissionTests(TestCase):
    def setUp(self) -> None:
        self.paid = User.objects.create_user(username="paid", password="pw12345!")
        active_subscription_for(self.paid)
        self.lapsed = User.objects.create_user(username="lapsed", password="pw12345!")
        Subscription.objects.create(
            user=self.lapsed,
            status="active",
            access_expires_at=timezone.now() - timedelta(days=1),
        )
        self.staff = User.objects.create_user(
            username="staffer", password="pw12345!", is_staff=True
        )
        self.norow = User.objects.create_user(username="norow", password="pw12345!")

    def _client(self, user: User) -> APIClient:
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _assert_402(self, response) -> None:
        self.assertEqual(response.status_code, 402)
        self.assertEqual(
            response.json(),
            {
                "code": "subscription_required",
                "detail": "An active subscription is required for this action.",
            },
        )

    def test_unauthenticated_is_401_not_402(self) -> None:
        for url in (reverse("fragrance-list"), reverse("run-list"), reverse("import-collection")):
            self.assertEqual(APIClient().get(url).status_code, 401)
        self.assertEqual(APIClient().post(reverse("import-collection")).status_code, 401)

    def test_paid_user_can_write_collection(self) -> None:
        response = self._client(self.paid).post(
            reverse("fragrance-list"),
            {"name": "Aventus", "house": "Creed", "status": "own"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)

    def test_expired_subscription_reads_but_cannot_write(self) -> None:
        client = self._client(self.lapsed)
        Fragrance.objects.create(user=self.lapsed, name="Old", house="H", status="own")
        self.assertEqual(client.get(reverse("fragrance-list")).status_code, 200)
        self.assertEqual(client.get(reverse("run-list")).status_code, 200)
        self.assertEqual(client.get(reverse("fragrance-config")).status_code, 200)
        self.assertEqual(client.get(reverse("export-collection")).status_code, 200)
        self._assert_402(client.post(
            reverse("fragrance-list"),
            {"name": "New", "house": "H", "status": "own"},
            format="json",
        ))

    def test_lapsed_user_writes_all_402(self) -> None:
        client = self._client(self.lapsed)
        fragrance = Fragrance.objects.create(user=self.lapsed, name="Old", house="H", status="own")
        detail = reverse("fragrance-detail", args=[fragrance.pk])
        self._assert_402(client.patch(detail, {"notes": "x"}, format="json"))
        self._assert_402(client.put(
            detail, {"name": "Old", "house": "H", "status": "like"}, format="json"
        ))
        self._assert_402(client.delete(detail))
        self.assertTrue(Fragrance.objects.filter(pk=fragrance.pk).exists())
        self._assert_402(client.post(reverse("import-collection")))
        self._assert_402(client.patch(
            reverse("fragrance-config"), {"run_hour": 5}, format="json"
        ))
        with patch("fragrance.tasks.monthly_fragrance_run.delay") as delay:
            self._assert_402(client.post(reverse("run-trigger")))
        delay.assert_not_called()
        self.assertFalse(RecommendationRun.objects.filter(user=self.lapsed).exists())

    def test_lapsed_import_is_402_even_with_no_file(self) -> None:
        self._assert_402(self._client(self.lapsed).post(reverse("import-collection")))

    def test_staff_bypasses_without_subscription(self) -> None:
        client = self._client(self.staff)
        response = client.post(
            reverse("fragrance-list"),
            {"name": "Aventus", "house": "Creed", "status": "own"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertFalse(Subscription.objects.filter(user=self.staff).exists())

    def test_no_subscription_row_is_402_and_none_is_created(self) -> None:
        client = self._client(self.norow)
        self._assert_402(client.post(
            reverse("fragrance-list"),
            {"name": "X", "house": "H", "status": "own"},
            format="json",
        ))
        self.assertEqual(client.get(reverse("fragrance-list")).status_code, 200)
        self.assertEqual(client.get(reverse("export-collection")).status_code, 200)
        self.assertFalse(Subscription.objects.filter(user=self.norow).exists())

    def test_revoked_status_with_future_expiry_is_402(self) -> None:
        subscription = active_subscription_for(self.norow)
        subscription.status = "revoked"
        subscription.save()
        self._assert_402(self._client(self.norow).post(reverse("import-collection")))

    def test_rate_and_profile_views_stay_open_to_lapsed_users(self) -> None:
        client = self._client(self.lapsed)
        self.assertEqual(client.get(reverse("recommendations")).status_code, 200)
        self.assertEqual(client.get(reverse("preference-profile-history")).status_code, 200)
        self.assertEqual(client.get(reverse("preference-profile")).status_code, 404)


def _make_plan(*, slug: str, interval: str, access_days: int) -> Plan:
    return Plan.objects.create(
        name=slug, slug=slug, stripe_price_id=f"price_{slug}", amount_cents=400,
        billing_interval=interval, access_days_granted=access_days,
    )


def _backdate_runs(*, user: User, days_ago: float) -> None:
    RecommendationRun.objects.filter(user=user).update(
        triggered_at=timezone.now() - timedelta(days=days_ago)
    )


class StartRecommendationRunTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="runner", password="pw12345!")
        active_subscription_for(self.user)

    def _finish_latest(self, *, run_status: str) -> RecommendationRun:
        run = RecommendationRun.objects.filter(user=self.user).latest("triggered_at")
        run.status = run_status
        run.save(update_fields=["status"])
        return run

    def test_creates_pending_run(self) -> None:
        run = start_recommendation_run(user=self.user, source="test")
        self.assertEqual(run.status, "pending")
        self.assertEqual(run.user, self.user)

    def test_missing_subscription_row_is_created_at_the_chokepoint(self) -> None:
        staff = User.objects.create_user(username="ops", password="pw12345!", is_staff=True)
        self.assertFalse(Subscription.objects.filter(user=staff).exists())
        start_recommendation_run(user=staff, source="test")
        self.assertTrue(Subscription.objects.filter(user=staff).exists())

    def test_no_subscription_raises_subscription_required(self) -> None:
        from .permissions import SubscriptionRequired

        nobody = User.objects.create_user(username="nobody", password="pw12345!")
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
            ctx.exception.next_eligible_at, first.triggered_at + timedelta(days=30)
        )

    def test_run_allowed_again_after_window(self) -> None:
        start_recommendation_run(user=self.user, source="test")
        self._finish_latest(run_status="done")
        _backdate_runs(user=self.user, days_ago=31)
        self.assertEqual(
            start_recommendation_run(user=self.user, source="test").status, "pending"
        )

    def test_one_failed_run_earns_one_retry_two_do_not(self) -> None:
        start_recommendation_run(user=self.user, source="test")
        self._finish_latest(run_status="failed")
        start_recommendation_run(user=self.user, source="test")  # forgiven retry
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
        annual = _make_plan(slug="annual-q", interval="year", access_days=365)
        active_subscription_for(self.user, plan=annual, days=365)
        start_recommendation_run(user=self.user, source="test")
        self._finish_latest(run_status="done")
        with self.assertRaises(QuotaExceeded):
            start_recommendation_run(user=self.user, source="test")
        _backdate_runs(user=self.user, days_ago=31)
        start_recommendation_run(user=self.user, source="test")

    def test_larger_allowance_next_eligible_uses_correct_run(self) -> None:
        plan = _make_plan(slug="three-q", interval="month", access_days=30)
        plan.max_runs_per_quota_period = 3
        plan.save()
        active_subscription_for(self.user, plan=plan)
        stamps = []
        for days_ago in (20, 10, 1):
            run = RecommendationRun.objects.create(user=self.user, status="done")
            RecommendationRun.objects.filter(pk=run.pk).update(
                triggered_at=timezone.now() - timedelta(days=days_ago)
            )
            run.refresh_from_db()
            stamps.append(run.triggered_at)
        with self.assertRaises(QuotaExceeded) as ctx:
            start_recommendation_run(user=self.user, source="test")
        # Only the oldest must leave the window to get back under 3.
        self.assertEqual(ctx.exception.next_eligible_at, stamps[0] + timedelta(days=30))

    def test_staff_bypass_quota_but_not_in_flight_guard(self) -> None:
        staff = User.objects.create_user(username="ops", password="pw12345!", is_staff=True)
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
            start_recommendation_run(user=self.user, source="test").status, "pending"
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

    def test_two_concurrent_triggers_with_no_subscription_row_still_one_run(self) -> None:
        staff = User.objects.create_user(username="racer_ops", password="pw12345!", is_staff=True)
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
        self.assertEqual(RecommendationRun.objects.filter(user=staff).count(), 1)
        self.assertEqual(Subscription.objects.filter(user=staff).count(), 1)


class TriggerViewQuotaTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="trig", password="pw12345!")
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

    def test_second_trigger_in_window_is_429_quota_exceeded(self) -> None:
        self._post()
        RecommendationRun.objects.filter(user=self.user).update(status="done")
        response, delay = self._post()
        self.assertEqual(response.status_code, 429)
        body = response.json()
        self.assertEqual(body["code"], "quota_exceeded")
        self.assertIn("detail", body)
        run = RecommendationRun.objects.get(user=self.user)
        expected = (run.triggered_at + timedelta(days=30)).isoformat().replace("+00:00", "Z")
        self.assertEqual(body["next_eligible_at"], expected)
        delay.assert_not_called()

    def test_trigger_while_running_is_429_run_in_progress(self) -> None:
        self._post()
        response, _ = self._post()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(
            response.json(),
            {"code": "run_in_progress", "detail": "A run is already in progress."},
        )

    def test_broker_outage_fails_the_run_instead_of_stranding_it_pending(self) -> None:
        from kombu.exceptions import OperationalError

        with patch(
            "fragrance.tasks.monthly_fragrance_run.delay", side_effect=OperationalError("down")
        ):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(RecommendationRun.objects.get(user=self.user).status, "failed")
        # A failed run neither burns the allowance nor blocks a retry.
        response, _ = self._post()
        self.assertEqual(response.status_code, 202)


class BeatPathQuotaTests(TestCase):
    def setUp(self) -> None:
        AIModelConfig.objects.all().delete()
        self.user = User.objects.create_user(username="beat", password="pw12345!")
        FragranceConfig.objects.create(user=self.user, recipient_email="beat@example.com")

    def test_lapsed_user_is_skipped_quietly(self) -> None:
        with self.assertLogs("fragrance.tasks", level="INFO") as logs:
            monthly_fragrance_run(user_id=self.user.id)
        self.assertFalse(RecommendationRun.objects.filter(user=self.user).exists())
        self.assertTrue(any("skipped" in line for line in logs.output))

    def test_exhausted_quota_is_skipped_quietly(self) -> None:
        active_subscription_for(self.user)
        RecommendationRun.objects.create(user=self.user, status="done")
        monthly_fragrance_run(user_id=self.user.id)
        self.assertEqual(RecommendationRun.objects.filter(user=self.user).count(), 1)

    def test_run_in_progress_is_skipped_quietly(self) -> None:
        active_subscription_for(self.user)
        RecommendationRun.objects.create(user=self.user, status="running")
        monthly_fragrance_run(user_id=self.user.id)
        self.assertEqual(RecommendationRun.objects.filter(user=self.user).count(), 1)

    def test_deleted_user_is_skipped_quietly(self) -> None:
        monthly_fragrance_run(user_id=999999)

    def test_beat_path_goes_through_chokepoint_and_marks_running(self) -> None:
        active_subscription_for(self.user)
        with patch("fragrance.tasks.registry.get_active_config", side_effect=RuntimeError("stop")):
            monthly_fragrance_run.push_request(retries=monthly_fragrance_run.max_retries)
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
            patch("fragrance.tasks.registry.get_active_config", side_effect=RuntimeError("stop")),
        ):
            monthly_fragrance_run.push_request(retries=monthly_fragrance_run.max_retries)
            try:
                with self.assertRaises(RuntimeError):
                    monthly_fragrance_run(user_id=self.user.id, run_id=run.id)
            finally:
                monthly_fragrance_run.pop_request()
        chokepoint.assert_not_called()
        self.assertEqual(RecommendationRun.objects.filter(user=self.user).count(), 1)

    def test_retry_reuses_the_beat_created_run(self) -> None:
        active_subscription_for(self.user)
        with (
            patch("fragrance.tasks.registry.get_active_config", side_effect=RuntimeError("boom")),
            patch.object(
                monthly_fragrance_run, "retry", side_effect=RuntimeError("retried")
            ) as mock_retry,
        ):
            with self.assertRaises(RuntimeError):
                monthly_fragrance_run(user_id=self.user.id)
        run = RecommendationRun.objects.get(user=self.user)
        self.assertEqual(
            mock_retry.call_args.kwargs["kwargs"],
            {"user_id": self.user.id, "run_id": run.id},
        )


class GrantComplimentaryAccessTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="comp", password="pw12345!")

    def test_grants_active_access_and_logs_warning(self) -> None:
        with self.assertLogs("fragrance.billing", level="WARNING") as logs:
            subscription = grant_complimentary_access(
                user=self.user, days=30, reason="grandfathered beta user"
            )
        self.assertTrue(subscription.has_active_access)
        self.assertIn("grandfathered beta user", logs.output[0])
        self.assertIn("comp", logs.output[0])

    def test_extends_from_later_of_now_and_current_expiry(self) -> None:
        existing = active_subscription_for(self.user, days=10)
        before = existing.access_expires_at
        subscription = grant_complimentary_access(user=self.user, days=5, reason="extend")
        self.assertEqual(subscription.access_expires_at, before + timedelta(days=5))

    def test_refuses_revoked_subscription(self) -> None:
        subscription = active_subscription_for(self.user)
        subscription.status = "revoked"
        subscription.save()
        with self.assertRaises(ComplimentaryGrantError):
            grant_complimentary_access(user=self.user, days=5, reason="x")

    def test_rejects_non_positive_days_and_blank_reason(self) -> None:
        with self.assertRaises(ComplimentaryGrantError):
            grant_complimentary_access(user=self.user, days=0, reason="x")
        with self.assertRaises(ComplimentaryGrantError):
            grant_complimentary_access(user=self.user, days=5, reason="   ")

    def test_command_requires_reason(self) -> None:
        with self.assertRaises(CommandError):
            call_command("grant_access", username="comp", days=30)
        self.assertFalse(Subscription.objects.filter(user=self.user).exists())

    def test_command_grants_access(self) -> None:
        out = StringIO()
        call_command(
            "grant_access", username="comp", days=30, reason="operator comp", stdout=out
        )
        self.assertTrue(Subscription.objects.get(user=self.user).has_active_access)

    def test_command_unknown_user_errors(self) -> None:
        with self.assertRaises(CommandError):
            call_command("grant_access", username="ghost", days=30, reason="x")


class QuotaSnapshotPayloadTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="snap", password="pw12345!")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _quota(self) -> dict:
        return self.client.get(reverse("billing-subscription")).json()["quota"]

    def test_lapsed_user_has_zero_allowed(self) -> None:
        self.assertEqual(
            self._quota(), {"runs_allowed": 0, "runs_used": 0, "next_eligible_at": None, "can_trigger": False}
        )

    def test_planless_active_subscription_uses_defaults(self) -> None:
        active_subscription_for(self.user)
        self.assertEqual(
            self._quota(), {"runs_allowed": 1, "runs_used": 0, "next_eligible_at": None, "can_trigger": True}
        )

    def test_used_run_reports_next_eligible_at(self) -> None:
        active_subscription_for(self.user)
        run = RecommendationRun.objects.create(user=self.user, status="done")
        quota = self._quota()
        self.assertEqual(quota["runs_used"], 1)
        self.assertEqual(
            quota["next_eligible_at"],
            (run.triggered_at + timedelta(days=30)).isoformat().replace("+00:00", "Z"),
        )

    def test_annual_plan_reports_one_run_allowed(self) -> None:
        annual = _make_plan(slug="annual-s", interval="year", access_days=365)
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
        self.assertFalse(self.client.get(reverse("billing-subscription")).json()["exempt"])


class DefaultScheduleForTests(SimpleTestCase):
    def test_deterministic(self) -> None:
        self.assertEqual(default_schedule_for(user_id=42), default_schedule_for(user_id=42))

    def test_formula(self) -> None:
        self.assertEqual(default_schedule_for(user_id=1), (2, 0))
        self.assertEqual(default_schedule_for(user_id=28), (1, 1))

    def test_day_always_1_to_28_and_hour_0_to_23_across_wide_sweep(self) -> None:
        for user_id in range(1, 20001):
            day, hour = default_schedule_for(user_id=user_id)
            self.assertTrue(1 <= day <= 28, user_id)
            self.assertTrue(0 <= hour <= 23, user_id)

    def test_672_slots_spread_evenly(self) -> None:
        from collections import Counter

        slots = Counter(default_schedule_for(user_id=i) for i in range(1, 672 * 3 + 1))
        self.assertEqual(len(slots), 672)
        self.assertEqual(set(slots.values()), {3})
        days = Counter(day for day, _ in slots.elements())
        self.assertEqual(len(days), 28)
        self.assertEqual(len(set(days.values())), 1)


class FragranceConfigCreationJitterTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="jit", password="pw12345!")
        active_subscription_for(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_first_config_get_applies_jittered_default(self) -> None:
        response = self.client.get(reverse("fragrance-config"))
        day, hour = default_schedule_for(user_id=self.user.id)
        self.assertEqual(response.json()["run_day_of_month"], day)
        self.assertEqual(response.json()["run_hour"], hour)

    def test_existing_choice_is_never_overwritten(self) -> None:
        FragranceConfig.objects.create(
            user=self.user, recipient_email="j@example.com", run_day_of_month=15, run_hour=3
        )
        response = self.client.get(reverse("fragrance-config"))
        self.assertEqual(response.json()["run_day_of_month"], 15)
        self.assertEqual(response.json()["run_hour"], 3)

    def test_explicit_day_of_1_chosen_via_api_after_creation_is_kept(self) -> None:
        self.client.get(reverse("fragrance-config"))
        self.client.patch(
            reverse("fragrance-config"), {"run_day_of_month": 1, "run_hour": 9}, format="json"
        )
        response = self.client.get(reverse("fragrance-config"))
        self.assertEqual(response.json()["run_day_of_month"], 1)
        self.assertEqual(response.json()["run_hour"], 9)


class RebalanceRunSchedulesCommandTests(TestCase):
    def setUp(self) -> None:
        self.untouched = User.objects.create_user(username="untouched", password="pw12345!")
        self.custom = User.objects.create_user(username="custom", password="pw12345!")
        self.weekly = User.objects.create_user(username="weekly", password="pw12345!")
        FragranceConfig.objects.create(user=self.untouched, recipient_email="a@example.com")
        FragranceConfig.objects.create(
            user=self.custom, recipient_email="b@example.com", run_day_of_month=15, run_hour=9
        )
        FragranceConfig.objects.create(
            user=self.weekly, recipient_email="c@example.com", frequency="weekly"
        )

    def _run(self, **kwargs: object) -> str:
        out = StringIO()
        call_command("rebalance_run_schedules", stdout=out, **kwargs)
        return out.getvalue()

    def test_dry_run_by_default_writes_nothing_but_reports(self) -> None:
        output = self._run()
        config = FragranceConfig.objects.get(user=self.untouched)
        self.assertEqual((config.run_day_of_month, config.run_hour), (1, 9))
        self.assertIn("untouched", output)
        self.assertIn("Dry run", output)

    def test_apply_moves_untouched_row_and_skips_customised_and_weekly(self) -> None:
        self._run(apply=True)
        moved = FragranceConfig.objects.get(user=self.untouched)
        self.assertEqual(
            (moved.run_day_of_month, moved.run_hour),
            default_schedule_for(user_id=self.untouched.id),
        )
        custom = FragranceConfig.objects.get(user=self.custom)
        self.assertEqual((custom.run_day_of_month, custom.run_hour), (15, 9))
        weekly = FragranceConfig.objects.get(user=self.weekly)
        self.assertEqual((weekly.run_day_of_month, weekly.run_hour), (1, 9))

    def test_apply_syncs_the_periodic_task(self) -> None:
        from django_celery_beat.models import PeriodicTask

        self._run(apply=True)
        day, hour = default_schedule_for(user_id=self.untouched.id)
        task = PeriodicTask.objects.get(name=f"fragrance-run-{self.untouched.id}")
        self.assertEqual(task.crontab.day_of_month, str(day))
        self.assertEqual(task.crontab.hour, str(hour))


def _staff_not_superuser(*, username: str = "prod_user_2") -> User:
    return User.objects.create_user(
        username=username, password="pw12345!", is_staff=True, is_superuser=False
    )


class StaffNotSuperuserExemptionTests(TestCase):
    """The production exempt account: is_staff=True, is_superuser=False, no Subscription row."""

    def setUp(self) -> None:
        AIModelConfig.objects.all().delete()
        self.user = _staff_not_superuser()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_writes_pass_both_permission_classes_without_creating_a_row(self) -> None:
        self.assertFalse(self.user.is_superuser)
        response = self.client.post(
            reverse("fragrance-list"),
            {"name": "Aventus", "house": "Creed", "status": "own"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(self.client.post(reverse("import-collection")).status_code, 402)
        self.assertFalse(Subscription.objects.filter(user=self.user).exists())

    def test_quota_bypassed_but_in_flight_guard_is_not(self) -> None:
        start_recommendation_run(user=self.user, source="test")
        with self.assertRaises(RunInProgress):
            start_recommendation_run(user=self.user, source="test")
        for status_ in ("pending", "running"):
            RecommendationRun.objects.filter(user=self.user).update(status=status_)
            with self.assertRaises(RunInProgress):
                start_recommendation_run(user=self.user, source="test")
        RecommendationRun.objects.filter(user=self.user).update(status="done")
        start_recommendation_run(user=self.user, source="test")

    def test_beat_path_runs_for_them(self) -> None:
        FragranceConfig.objects.create(user=self.user, recipient_email="s@example.com")
        with patch("fragrance.tasks.registry.get_active_config", side_effect=RuntimeError("stop")):
            monthly_fragrance_run.push_request(retries=monthly_fragrance_run.max_retries)
            try:
                with self.assertRaises(RuntimeError):
                    monthly_fragrance_run(user_id=self.user.id)
            finally:
                monthly_fragrance_run.pop_request()
        self.assertEqual(RecommendationRun.objects.filter(user=self.user).count(), 1)

    def test_payload_reports_effective_access(self) -> None:
        body = self.client.get(reverse("billing-subscription")).json()
        self.assertTrue(body["has_active_access"])
        self.assertTrue(body["exempt"])
        self.assertIsNone(body["quota"]["runs_allowed"])
        self.assertIsNone(body["quota"]["next_eligible_at"])
        self.assertTrue(body["quota"]["can_trigger"])
        self.assertEqual(body["status"], "none")


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
        oldest = RecommendationRun.objects.filter(user=self.user).order_by("triggered_at").first()
        q = self._quota()
        self.assertEqual(q["runs_used"], 0)
        self.assertFalse(q["can_trigger"])
        self.assertEqual(
            q["next_eligible_at"],
            (oldest.triggered_at + timedelta(days=30)).isoformat().replace("+00:00", "Z"),
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
        for statuses in ([], ["done"], ["failed"], ["failed", "failed"], ["running"]):
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


class RevokedAndResendTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="rev", password="pw12345!")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_revoked_user_can_start_checkout_again(self) -> None:
        plan = _make_plan(slug="rev-plan", interval="month", access_days=30)
        Subscription.objects.create(
            user=self.user, status="revoked", access_expires_at=timezone.now(),
            stripe_customer_id="cus_rev",
        )
        with patch("fragrance.billing.stripe.checkout.Session.create") as create, \
                patch(
                    "fragrance.billing.stripe.Subscription.list",
                    return_value=stripe.ListObject.construct_from(
                        {"object": "list", "data": [], "has_more": False, "url": "/v1/subscriptions"},
                        "sk_test_x",
                    ),
                ), \
                override_settings(STRIPE_SECRET_KEY="sk_test_x", STRIPE_API_VERSION="2025-09-30.clover"):
            create.return_value = SimpleNamespace(id="cs_1", url="https://checkout.example/x")
            response = self.client.post(
                reverse("billing-checkout"), {"slug": plan.slug}, format="json"
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["url"], "https://checkout.example/x")

    def test_resend_email_is_402_for_lapsed_user(self) -> None:
        run = RecommendationRun.objects.create(user=self.user, status="done")
        response = self.client.post(reverse("run-resend-email", args=[run.pk]))
        self.assertEqual(response.status_code, 402)


class GrantStaffMigrationTests(TestCase):
    def test_forward_function(self) -> None:
        from django.apps import apps as real_apps

        migration = importlib.import_module(
            "fragrance.migrations.0009_grant_staff_to_existing_users"
        )
        before = datetime(2026, 6, 21, tzinfo=dt_timezone.utc)
        after = datetime(2026, 9, 20, 0, 0, tzinfo=dt_timezone.utc)
        plain = User.objects.create_user(username="old_plain", password="pw12345!")
        staff = User.objects.create_user(username="old_staff", password="pw12345!", is_staff=True)
        late = User.objects.create_user(username="late_joiner", password="pw12345!")
        User.objects.filter(pk__in=[plain.pk, staff.pk]).update(date_joined=before)
        User.objects.filter(pk=late.pk).update(date_joined=after)
        migration.grant_staff_to_existing_users(real_apps, None)
        for user in (plain, staff, late):
            user.refresh_from_db()
        self.assertTrue(plain.is_staff)
        self.assertTrue(staff.is_staff)
        self.assertFalse(plain.is_superuser)
        self.assertFalse(late.is_staff)
        newcomer = User.objects.create_user(username="new_after", password="pw12345!")
        self.assertFalse(newcomer.is_staff)

    def test_already_staff_user_after_cutoff_is_untouched(self) -> None:
        from django.apps import apps as real_apps

        migration = importlib.import_module(
            "fragrance.migrations.0009_grant_staff_to_existing_users"
        )
        staff = User.objects.create_user(username="late_staff", password="pw12345!", is_staff=True)
        User.objects.filter(pk=staff.pk).update(
            date_joined=datetime(2026, 10, 1, tzinfo=dt_timezone.utc)
        )
        migration.grant_staff_to_existing_users(real_apps, None)
        staff.refresh_from_db()
        self.assertTrue(staff.is_staff)


class ExemptCheckoutRefusalTests(TestCase):
    def test_staff_not_superuser_gets_409_with_no_side_effects(self) -> None:
        user = _staff_not_superuser(username="exempt_checkout")
        _make_plan(slug="ex-plan", interval="month", access_days=30)
        client = APIClient()
        client.force_authenticate(user=user)
        with patch("fragrance.billing.stripe.Customer.create") as customer, \
                patch("fragrance.billing.stripe.checkout.Session.create") as session:
            response = client.post(reverse("billing-checkout"), {"slug": "ex-plan"}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {
                "code": "exempt",
                "detail": "This account has full access, so no subscription is needed.",
            },
        )
        customer.assert_not_called()
        session.assert_not_called()
        self.assertFalse(Subscription.objects.filter(user=user).exists())


class ExemptCanTriggerTests(TestCase):
    def test_can_trigger_false_only_while_in_flight(self) -> None:
        user = _staff_not_superuser(username="exempt_ct")
        client = APIClient()
        client.force_authenticate(user=user)
        url = reverse("billing-subscription")
        RecommendationRun.objects.create(user=user, status="done")
        self.assertIs(client.get(url).json()["quota"]["can_trigger"], True)
        RecommendationRun.objects.create(user=user, status="pending")
        quota = client.get(url).json()["quota"]
        self.assertIs(quota["can_trigger"], False)
        self.assertIsNone(quota["runs_allowed"])
        self.assertIsNone(quota["next_eligible_at"])


@override_settings(**_STRIPE_TEST_SETTINGS)
class ReconcileSubscriptionsTests(TestCase):
    def setUp(self) -> None:
        from .billing import reconcile_subscriptions

        self.reconcile = reconcile_subscriptions
        self.plan = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, billing_interval="month", access_days_granted=30,
        )
        self.period_end = int(timezone.now().timestamp()) + 30 * 86400

    def _make_user(self, *, name: str, customer_id: str | None, status: str = "checkout_started",
                   expires_in: timedelta | None = None) -> Subscription:
        user = User.objects.create_user(username=name, email=f"{name}@example.com")
        BetaSignup.objects.create(
            email=f"{name}@example.com", normalized_email=f"{name}@example.com",
            status="converted", invited_user=user,
        )
        return Subscription.objects.create(
            user=user, stripe_customer_id=customer_id, status=status,
            access_expires_at=timezone.now() + expires_in if expires_in is not None else None,
        )

    def _invoice(self, *, invoice_id: str, customer_id: str, **kwargs) -> SimpleNamespace:
        return _fake_invoice(
            invoice_id=invoice_id, customer_id=customer_id, subscription_id=f"sub_{invoice_id}", amount_paid=400,
            lines=[_fake_line(price_id="price_mo", period_end=self.period_end)], **kwargs,
        )

    @staticmethod
    def _listing(*invoices: SimpleNamespace) -> SimpleNamespace:
        return SimpleNamespace(auto_paging_iter=lambda: iter(invoices))

    @patch("fragrance.billing.stripe.Invoice.list")
    def test_lost_renewal_and_paid_never_loaded_are_granted_and_rerun_is_noop(self, mock_list) -> None:
        renewal = self._make_user(name="renew", customer_id="cus_renew", status="active", expires_in=timedelta(days=-3))
        loaded = self._make_user(name="missed", customer_id="cus_missed")
        mock_list.return_value = self._listing(
            self._invoice(invoice_id="in_renew", customer_id="cus_renew"),
            self._invoice(invoice_id="in_missed", customer_id="cus_missed"),
        )

        first = self.reconcile()
        renewal.refresh_from_db()
        loaded.refresh_from_db()
        expiry_after_first = loaded.access_expires_at
        second = self.reconcile()
        loaded.refresh_from_db()

        self.assertEqual(first["grants"], 2)
        self.assertEqual(renewal.status, "active")
        self.assertGreater(renewal.access_expires_at, timezone.now())
        self.assertEqual(loaded.status, "active")
        self.assertEqual(second["grants"], 0)
        self.assertEqual(second["already_applied"], 2)
        self.assertEqual(loaded.access_expires_at, expiry_after_first)
        self.assertEqual(ProcessedPayment.objects.count(), 2)

    @patch("fragrance.billing.stripe.Invoice.list")
    def test_active_subscriber_long_past_expiry_is_still_reconciled(self, mock_list) -> None:
        sub = self._make_user(name="ancient", customer_id="cus_old", status="active", expires_in=-timedelta(days=40))
        mock_list.return_value = self._listing(self._invoice(invoice_id="in_old", customer_id="cus_old"))
        self.assertEqual(self.reconcile()["grants"], 1)
        sub.refresh_from_db()
        self.assertGreater(sub.access_expires_at, timezone.now())

    @patch("fragrance.billing.stripe.Invoice.list")
    def test_processed_invoice_counts_as_already_applied(self, mock_list) -> None:
        sub = self._make_user(name="dup", customer_id="cus_dup", status="active", expires_in=timedelta(days=1))
        ProcessedPayment.objects.create(
            stripe_invoice_id="in_dup", subscription=sub, amount_paid_cents=400,
            period_end=timezone.now() + timedelta(days=1),
        )
        before = sub.access_expires_at
        mock_list.return_value = self._listing(self._invoice(invoice_id="in_dup", customer_id="cus_dup"))
        summary = self.reconcile()
        sub.refresh_from_db()
        self.assertEqual(summary["already_applied"], 1)
        self.assertEqual(summary["grants"], 0)
        self.assertEqual(sub.access_expires_at, before)

    @patch("fragrance.billing.stripe.Invoice.list")
    def test_unpaid_invoice_grants_nothing(self, mock_list) -> None:
        sub = self._make_user(name="unpaid", customer_id="cus_unpaid")
        mock_list.return_value = self._listing(
            self._invoice(invoice_id="in_open", customer_id="cus_unpaid", status="open", amount_remaining=400)
        )
        summary = self.reconcile()
        sub.refresh_from_db()
        self.assertEqual(summary["grants"], 0)
        self.assertIsNone(sub.access_expires_at)
        self.assertEqual(ProcessedPayment.objects.count(), 0)

    @patch("fragrance.billing.stripe.Invoice.list")
    def test_unapproved_user_grants_nothing(self, mock_list) -> None:
        sub = self._make_user(name="pending", customer_id="cus_pending")
        BetaSignup.objects.filter(invited_user=sub.user).update(status="pending")
        mock_list.return_value = self._listing(self._invoice(invoice_id="in_pend", customer_id="cus_pending"))
        summary = self.reconcile()
        sub.refresh_from_db()
        self.assertEqual(summary["grants"], 0)
        self.assertEqual(sub.status, "checkout_started")

    @patch("fragrance.billing.stripe.Invoice.list")
    def test_invoice_list_is_one_account_wide_call(self, mock_list) -> None:
        self._make_user(name="params", customer_id="cus_params")
        mock_list.return_value = self._listing()
        self.reconcile()
        mock_list.assert_called_once()
        kwargs = mock_list.call_args.kwargs
        self.assertNotIn("customer", kwargs)
        self.assertEqual(kwargs["status"], "paid")
        self.assertEqual(kwargs["limit"], 100)
        now = int(timezone.now().timestamp())
        self.assertAlmostEqual(kwargs["created"]["gte"], now - 7 * 86400, delta=5)

    @patch("fragrance.billing.stripe.Invoice.list")
    def test_lookback_is_configurable(self, mock_list) -> None:
        mock_list.return_value = self._listing()
        self.reconcile(lookback=timedelta(days=30))
        now = int(timezone.now().timestamp())
        self.assertAlmostEqual(mock_list.call_args.kwargs["created"]["gte"], now - 30 * 86400, delta=5)

    @patch("fragrance.billing.stripe.Invoice.list")
    def test_revoked_customers_paid_invoice_is_never_granted(self, mock_list) -> None:
        sub = self._make_user(name="revoked", customer_id="cus_rev", status="revoked", expires_in=timedelta(days=1))
        before = sub.access_expires_at
        mock_list.return_value = self._listing(self._invoice(invoice_id="in_rev", customer_id="cus_rev"))
        summary = self.reconcile()
        sub.refresh_from_db()
        self.assertEqual(summary["grants"], 0)
        self.assertEqual(summary["invoices_matched"], 0)
        self.assertEqual(sub.status, "revoked")
        self.assertEqual(sub.access_expires_at, before)
        self.assertEqual(ProcessedPayment.objects.count(), 0)

    @patch("fragrance.billing.stripe.Invoice.list")
    def test_invoice_for_unknown_customer_is_ignored(self, mock_list) -> None:
        mock_list.return_value = self._listing(self._invoice(invoice_id="in_x", customer_id="cus_stranger"))
        summary = self.reconcile()
        self.assertEqual(summary["invoices_seen"], 1)
        self.assertEqual(summary["invoices_matched"], 0)
        self.assertEqual(summary["grants"], 0)
        self.assertEqual(ProcessedPayment.objects.count(), 0)

    @patch("fragrance.billing.stripe.Invoice.list")
    def test_abandoned_checkout_costs_no_per_customer_call(self, mock_list) -> None:
        for i in range(25):
            self._make_user(name=f"abandoned{i}", customer_id=f"cus_ab{i}")
        mock_list.return_value = self._listing()
        self.reconcile()
        self.assertEqual(mock_list.call_count, 1)

    @patch("fragrance.billing._grant_period_for_invoice")
    @patch("fragrance.billing.stripe.Invoice.list")
    def test_one_error_does_not_stop_the_batch(self, mock_list, mock_grant) -> None:
        self._make_user(name="first", customer_id="cus_first")
        self._make_user(name="second", customer_id="cus_second")
        mock_list.return_value = self._listing(
            self._invoice(invoice_id="in_first", customer_id="cus_first"),
            self._invoice(invoice_id="in_second", customer_id="cus_second"),
        )
        mock_grant.side_effect = [RuntimeError("boom"), True]
        summary = self.reconcile()
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["grants"], 1)

    @patch("fragrance.billing.stripe.Invoice.list")
    def test_listing_failure_is_contained_and_events_still_requeued(self, mock_list) -> None:
        mock_list.side_effect = stripe.APIConnectionError("boom")
        with self.assertLogs("fragrance.billing", level="ERROR"):
            summary = self.reconcile()
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["grants"], 0)

    @patch("fragrance.billing._grant_period_for_invoice", side_effect=RuntimeError("boom"))
    @patch("fragrance.billing.stripe.Invoice.list")
    def test_errors_with_no_grants_and_pending_invoices_log_at_error(self, mock_list, _mock_grant) -> None:
        self._make_user(name="stuck", customer_id="cus_stuck")
        mock_list.return_value = self._listing(self._invoice(invoice_id="in_stuck", customer_id="cus_stuck"))
        with self.assertLogs("fragrance.billing", level="ERROR") as logs:
            self.reconcile()
        self.assertTrue(any("NO grants" in line for line in logs.output))

    @patch("fragrance.billing.stripe.Invoice.list")
    def test_deadline_stops_the_loop(self, mock_list) -> None:
        for name in ("a", "b", "c"):
            self._make_user(name=name, customer_id=f"cus_{name}")
        mock_list.return_value = self._listing(
            *(self._invoice(invoice_id=f"in_{n}", customer_id=f"cus_{n}") for n in ("a", "b", "c"))
        )
        with self.assertLogs("fragrance.billing", level="ERROR") as logs:
            summary = self.reconcile(time_budget_seconds=0)
        self.assertTrue(summary["deadline_exceeded"])
        self.assertEqual(summary["grants"], 0)
        self.assertTrue(any("budget exhausted" in line for line in logs.output))

    @patch("fragrance.billing.RECONCILE_MAX_INVOICES", 2)
    @patch("fragrance.billing.stripe.Invoice.list")
    def test_pagination_sanity_cap_warns(self, mock_list) -> None:
        mock_list.return_value = self._listing(
            *(self._invoice(invoice_id=f"in_{i}", customer_id="cus_none") for i in range(5))
        )
        with self.assertLogs("fragrance.billing", level="WARNING") as logs:
            summary = self.reconcile()
        self.assertTrue(summary["capped"])
        self.assertEqual(summary["invoices_seen"], 2)
        self.assertTrue(any("sanity cap" in line for line in logs.output))

    @override_settings(STRIPE_SECRET_KEY="")
    @patch("fragrance.billing.stripe.Invoice.list")
    def test_unset_key_returns_cleanly(self, mock_list) -> None:
        self._make_user(name="nokey", customer_id="cus_nokey")
        with self.assertLogs("fragrance.billing", level="WARNING"):
            summary = self.reconcile()
        self.assertTrue(summary["skipped"])
        mock_list.assert_not_called()

    @patch("fragrance.billing.stripe.Invoice.list")
    def test_dry_run_writes_nothing(self, mock_list) -> None:
        sub = self._make_user(name="dry", customer_id="cus_dry")
        mock_list.return_value = self._listing(self._invoice(invoice_id="in_dry", customer_id="cus_dry"))
        with patch("fragrance.tasks.process_stripe_event.delay") as mock_delay:
            StripeEvent.objects.create(
                stripe_event_id="evt_dry", event_type="payment_intent.succeeded",
                payload={"data": {"object": {"id": "pi_1"}}},
            )
            StripeEvent.objects.filter(stripe_event_id="evt_dry").update(
                received_at=timezone.now() - timedelta(hours=1)
            )
            summary = self.reconcile(dry_run=True)
            mock_delay.assert_not_called()
        sub.refresh_from_db()
        self.assertEqual(summary["would_grant"], 1)
        self.assertEqual(summary["events_requeued"], 1)
        self.assertEqual(sub.status, "checkout_started")
        self.assertIsNone(sub.access_expires_at)
        self.assertEqual(ProcessedPayment.objects.count(), 0)

    @patch("fragrance.billing.stripe.Invoice.list")
    def test_management_command_dry_run_and_days(self, mock_list) -> None:
        self._make_user(name="cmd", customer_id="cus_cmd")
        mock_list.return_value = self._listing(self._invoice(invoice_id="in_cmd", customer_id="cus_cmd"))
        out = StringIO()
        call_command("reconcile_stripe", "--dry-run", "--days", "30", stdout=out)
        self.assertIn("would_grant: 1", out.getvalue())
        self.assertEqual(ProcessedPayment.objects.count(), 0)
        now = int(timezone.now().timestamp())
        self.assertAlmostEqual(mock_list.call_args.kwargs["created"]["gte"], now - 30 * 86400, delta=5)

    def test_management_command_rejects_non_positive_days(self) -> None:
        with self.assertRaises(CommandError):
            call_command("reconcile_stripe", "--days", "0")

    def test_reconcile_task_has_time_limits(self) -> None:
        from .tasks import reconcile_stripe_subscriptions

        self.assertEqual(reconcile_stripe_subscriptions.soft_time_limit, 240)
        self.assertEqual(reconcile_stripe_subscriptions.time_limit, 300)


@override_settings(**_STRIPE_TEST_SETTINGS)
class ReconcileStuckEventsTests(TestCase):
    def _event(self, *, evt_id: str, status: str, age: timedelta, payload: dict | None) -> None:
        StripeEvent.objects.create(
            stripe_event_id=evt_id, event_type="payment_intent.succeeded", status=status,
            payload=payload if payload is not None else {},
        )
        StripeEvent.objects.filter(stripe_event_id=evt_id).update(received_at=timezone.now() - age)

    @patch("fragrance.tasks.process_stripe_event.delay")
    def test_only_stuck_events_in_window_are_requeued(self, mock_delay) -> None:
        from .billing import reconcile_subscriptions

        payload = {"data": {"object": {"id": "pi_x"}}}
        self._event(evt_id="evt_recv", status="received", age=timedelta(hours=1), payload=payload)
        self._event(evt_id="evt_fail", status="failed", age=timedelta(hours=5), payload=payload)
        self._event(evt_id="evt_fresh", status="received", age=timedelta(minutes=2), payload=payload)
        self._event(evt_id="evt_old", status="failed", age=timedelta(days=5), payload=payload)
        self._event(evt_id="evt_done", status="processed", age=timedelta(hours=2), payload=payload)
        self._event(evt_id="evt_nopayload", status="failed", age=timedelta(hours=2), payload=None)

        with self.assertLogs("fragrance.billing", level="WARNING"):
            summary = reconcile_subscriptions()

        queued = sorted(call.kwargs["stripe_event_id"] for call in mock_delay.call_args_list)
        self.assertEqual(queued, ["evt_fail", "evt_recv"])
        self.assertEqual(summary["events_requeued"], 2)
        self.assertEqual(summary["events_skipped_no_payload"], 1)

    @patch("fragrance.billing.reconcile_subscriptions")
    def test_celery_task_delegates(self, mock_reconcile) -> None:
        from .tasks import reconcile_stripe_subscriptions

        mock_reconcile.return_value = {"grants": 0}
        reconcile_stripe_subscriptions()
        mock_reconcile.assert_called_once_with(dry_run=False)


class NightlyHousekeepingTests(TestCase):
    def test_old_payloads_are_emptied_rows_kept_recent_untouched(self) -> None:
        from .tasks import prune_stripe_event_payloads

        payload = {"data": {"object": {"id": "pi_x"}}}
        for evt_id in ("evt_old", "evt_recent"):
            StripeEvent.objects.create(
                stripe_event_id=evt_id, event_type="payment_intent.succeeded",
                status="processed", payload=payload,
            )
        StripeEvent.objects.filter(stripe_event_id="evt_old").update(
            received_at=timezone.now() - timedelta(days=91)
        )

        self.assertEqual(prune_stripe_event_payloads(), 1)

        old = StripeEvent.objects.get(stripe_event_id="evt_old")
        self.assertEqual(old.payload, {})
        self.assertEqual(old.event_type, "payment_intent.succeeded")
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_recent").payload, payload)
        self.assertEqual(prune_stripe_event_payloads(), 0)

    def test_replay_guard_survives_pruning(self) -> None:
        from django.db import IntegrityError, transaction

        from .tasks import prune_stripe_event_payloads

        StripeEvent.objects.create(stripe_event_id="evt_guard", event_type="charge.dispute.created", payload={"a": 1})
        StripeEvent.objects.filter(stripe_event_id="evt_guard").update(
            received_at=timezone.now() - timedelta(days=120)
        )
        prune_stripe_event_payloads()
        with self.assertRaises(IntegrityError), transaction.atomic():
            StripeEvent.objects.create(stripe_event_id="evt_guard", event_type="charge.dispute.created")
        self.assertEqual(StripeEvent.objects.filter(stripe_event_id="evt_guard").count(), 1)

    def test_flush_expired_tokens_deletes_expired_outstanding_only(self) -> None:
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

        from .tasks import flush_expired_jwt_tokens

        user = User.objects.create_user(username="tok", password="pw12345!")
        expired = RefreshToken.for_user(user)
        expired_jti = expired["jti"]
        OutstandingToken.objects.filter(jti=expired_jti).update(expires_at=timezone.now() - timedelta(days=1))
        live_jti = RefreshToken.for_user(user)["jti"]

        flush_expired_jwt_tokens()

        self.assertFalse(OutstandingToken.objects.filter(jti=expired_jti).exists())
        self.assertTrue(OutstandingToken.objects.filter(jti=live_jti).exists())


class StaleInFlightRunTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="stale", password="pw12345!")
        active_subscription_for(self.user)

    def _stranded(self, *, hours_ago: float, run_status: str = "running") -> RecommendationRun:
        run = RecommendationRun.objects.create(user=self.user, status=run_status)
        RecommendationRun.objects.filter(pk=run.pk).update(
            triggered_at=timezone.now() - timedelta(hours=hours_ago)
        )
        return run

    def test_run_older_than_bound_no_longer_blocks_trigger(self) -> None:
        self._stranded(hours_ago=3)
        # The stale row is neither in flight nor (once reaped) quota-consuming;
        # un-reaped it still counts toward the quota, so use a staff user for
        # the pure in-flight check below and the reaper test for the quota one.
        staff = User.objects.create_user(username="stale-staff", password="pw12345!", is_staff=True)
        RecommendationRun.objects.create(user=staff, status="running")
        RecommendationRun.objects.filter(user=staff).update(
            triggered_at=timezone.now() - timedelta(hours=3)
        )
        self.assertEqual(start_recommendation_run(user=staff, source="test").status, "pending")

    def test_run_younger_than_bound_still_blocks(self) -> None:
        staff = User.objects.create_user(username="fresh-staff", password="pw12345!", is_staff=True)
        RecommendationRun.objects.create(user=staff, status="running")
        RecommendationRun.objects.filter(user=staff).update(
            triggered_at=timezone.now() - timedelta(hours=1)
        )
        with self.assertRaises(RunInProgress):
            start_recommendation_run(user=staff, source="test")

    def test_reaper_fails_only_stale_rows(self) -> None:
        from .tasks import STALE_RUN_ERROR_MESSAGE, reap_stale_runs

        stale_running = self._stranded(hours_ago=3, run_status="running")
        stale_pending = self._stranded(hours_ago=5, run_status="pending")
        fresh = self._stranded(hours_ago=1, run_status="running")
        old_done = self._stranded(hours_ago=9, run_status="done")

        self.assertEqual(reap_stale_runs(), 2)

        for run in (stale_running, stale_pending):
            run.refresh_from_db()
            self.assertEqual(run.status, "failed")
            self.assertEqual(run.error_message, STALE_RUN_ERROR_MESSAGE)
        fresh.refresh_from_db()
        old_done.refresh_from_db()
        self.assertEqual(fresh.status, "running")
        self.assertEqual(old_done.status, "done")
        self.assertEqual(reap_stale_runs(), 0)

    def test_reaped_run_does_not_count_toward_quota(self) -> None:
        from .tasks import reap_stale_runs

        self._stranded(hours_ago=3, run_status="running")
        with self.assertRaises(QuotaExceeded):
            start_recommendation_run(user=self.user, source="test")
        reap_stale_runs()
        self.assertEqual(quota_snapshot(user=self.user, subscription=self.user.subscription)["runs_used"], 0)
        self.assertEqual(start_recommendation_run(user=self.user, source="test").status, "pending")

    def test_snapshot_can_trigger_agrees_with_chokepoint(self) -> None:
        staff = User.objects.create_user(username="agree-staff", password="pw12345!", is_staff=True)
        for hours_ago, expected in ((1, False), (3, True)):
            RecommendationRun.objects.filter(user=staff).delete()
            RecommendationRun.objects.create(user=staff, status="running")
            RecommendationRun.objects.filter(user=staff).update(
                triggered_at=timezone.now() - timedelta(hours=hours_ago)
            )
            snapshot = quota_snapshot(user=staff, subscription=None)
            self.assertEqual(snapshot["can_trigger"], expected)
            try:
                start_recommendation_run(user=staff, source="test")
                started = True
            except RunInProgress:
                started = False
            self.assertEqual(started, expected)

    def test_reaper_is_registered_in_beat_schedule(self) -> None:
        from config.celery import app

        entry = app.conf.beat_schedule["system-reap-stale-runs"]
        self.assertEqual(entry["task"], "fragrance.tasks.reap_stale_runs")

    def test_monthly_run_has_time_limits(self) -> None:
        self.assertEqual(monthly_fragrance_run.soft_time_limit, 1200)
        self.assertEqual(monthly_fragrance_run.time_limit, 1500)

    def test_soft_time_limit_fails_the_run_without_retry(self) -> None:
        from celery.exceptions import SoftTimeLimitExceeded

        run = RecommendationRun.objects.create(user=self.user, status="pending")
        with patch(
            "fragrance.tasks.registry.get_active_config", side_effect=SoftTimeLimitExceeded()
        ), patch.object(monthly_fragrance_run, "retry") as retry:
            monthly_fragrance_run(user_id=self.user.id, run_id=run.id)
        retry.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertTrue(run.error_message)


@override_settings(**_STRIPE_TEST_SETTINGS)
class GrantPeriodAllowRevokedTests(TestCase):
    def setUp(self) -> None:
        Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, billing_interval="month", access_days_granted=30,
        )
        user = User.objects.create_user(username="disputed", email="disputed@example.com")
        BetaSignup.objects.create(
            email="disputed@example.com", normalized_email="disputed@example.com",
            status="converted", invited_user=user,
        )
        self.subscription = Subscription.objects.create(
            user=user, stripe_customer_id="cus_disp", status="revoked"
        )
        self.invoice = _fake_invoice(
            invoice_id="in_disp", customer_id="cus_disp", subscription_id="sub_disp", amount_paid=400,
            lines=[_fake_line(price_id="price_mo", period_end=int(timezone.now().timestamp()) + 30 * 86400)],
        )

    def test_revoked_is_not_regranted_when_disallowed(self) -> None:
        from .billing import _grant_period_for_invoice

        self.assertFalse(_grant_period_for_invoice(invoice=self.invoice, allow_revoked=False))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")
        self.assertIsNone(self.subscription.access_expires_at)
        self.assertEqual(ProcessedPayment.objects.count(), 0)

    def test_revoked_customer_can_resubscribe_by_default(self) -> None:
        from .billing import _grant_period_for_invoice

        self.assertTrue(_grant_period_for_invoice(invoice=self.invoice))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "active")
        self.assertEqual(ProcessedPayment.objects.count(), 1)

    def test_reconcile_passes_allow_revoked_false(self) -> None:
        from .billing import reconcile_subscriptions

        Subscription.objects.filter(pk=self.subscription.pk).update(status="active")
        listing = SimpleNamespace(auto_paging_iter=lambda: iter([self.invoice]))
        with patch("fragrance.billing.stripe.Invoice.list", return_value=listing), patch(
            "fragrance.billing._grant_period_for_invoice", return_value=True
        ) as grant:
            reconcile_subscriptions()
        self.assertIs(grant.call_args.kwargs["allow_revoked"], False)

    def test_dispute_landing_mid_reconcile_is_not_overwritten(self) -> None:
        from .billing import reconcile_subscriptions

        Subscription.objects.filter(pk=self.subscription.pk).update(status="active")

        def _revoke_then_yield():
            # The customer is a candidate when the listing starts; the dispute
            # revoke lands before the grant reaches the row lock.
            Subscription.objects.filter(pk=self.subscription.pk).update(status="revoked")
            yield self.invoice

        listing = SimpleNamespace(auto_paging_iter=_revoke_then_yield)
        with patch("fragrance.billing.stripe.Invoice.list", return_value=listing):
            reconcile_subscriptions()
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")
        self.assertEqual(ProcessedPayment.objects.count(), 0)


class FragranceConfigScheduleValidationTests(TestCase):
    url = "/api/v1/config/"

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="sched", password="pw12345!")
        active_subscription_for(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _put(self, **fields: int) -> object:
        return self.client.patch(self.url, fields, format="json")

    def test_out_of_range_values_are_rejected(self) -> None:
        for field, bad in (
            ("run_hour", 24), ("run_hour", 99), ("run_hour", -1),
            ("run_day_of_week", 7), ("run_day_of_week", -1),
            ("run_day_of_month", 0), ("run_day_of_month", 32),
            ("run_month", 0), ("run_month", 13),
        ):
            with self.subTest(field=field, value=bad):
                response = self._put(**{field: bad})
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.json())

    def test_boundary_values_are_accepted(self) -> None:
        for field, good in (
            ("run_hour", 0), ("run_hour", 23),
            ("run_day_of_week", 0), ("run_day_of_week", 6),
            ("run_day_of_month", 1), ("run_day_of_month", 31),
            ("run_month", 1), ("run_month", 12),
        ):
            with self.subTest(field=field, value=good):
                response = self._put(**{field: good})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()[field], good)

    def test_rejected_value_is_not_persisted(self) -> None:
        self._put(run_hour=99)
        self.assertNotEqual(FragranceConfig.objects.get(user=self.user).run_hour, 99)

    def test_existing_default_config_still_saves(self) -> None:
        response = self._put(recipient_email="ok@example.com", run_hour=9, run_day_of_week=1, run_day_of_month=1, run_month=1)
        self.assertEqual(response.status_code, 200)
