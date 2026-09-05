import importlib
import json
import re
import threading
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.apps import apps as django_apps
from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import resolve, reverse
from django.urls.exceptions import Resolver404
from django.views.debug import SafeExceptionReporterFilter
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from config.debug import AdminHardeningExceptionReporterFilter
from config.settings import _secure_proxy_ssl_header, _validated_admin_url

from .ai_providers import registry
from .ai_providers.anthropic_provider import AnthropicProvider
from .ai_providers.base import StructuredLLMProvider, StructuredOutputError
from .ai_providers.openai_provider import OpenAIProvider, _to_strict_schema
from .ai_providers.qwen_provider import DASHSCOPE_BASE_URL_DEFAULT, QwenProvider
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
    Fragrance,
    FragranceConfig,
    PreferenceProfile,
    Recommendation,
    RecommendationRun,
)
from .services import generate_recommendation_token
from .tasks import monthly_fragrance_run, render_and_send_email

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
