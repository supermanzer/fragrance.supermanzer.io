from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from fragrance.ai_providers import registry
from fragrance.ai_providers.anthropic_provider import AnthropicProvider
from fragrance.ai_providers.base import (
    PROVIDER_TIMEOUT_SECONDS,
    StructuredLLMProvider,
    StructuredOutputError,
)
from fragrance.ai_providers.openai_provider import OpenAIProvider, _to_strict_schema
from fragrance.ai_providers.qwen_provider import (
    DASHSCOPE_BASE_URL_DEFAULT,
    QwenProvider,
)
from fragrance.llm import build_email_content_tool_schema
from fragrance.models import AIModelConfig
from fragrance.tests.support.llm_fakes import (
    _fake_anthropic_response,
    _fake_openai_response,
)


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
