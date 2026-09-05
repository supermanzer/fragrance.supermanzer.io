"""
fragrance/ai_providers/qwen_provider.py

DashScope (Alibaba Cloud Model Studio) exposes an OpenAI-compatible endpoint,
so Qwen composes over OpenAIProvider with a different base_url + API key
rather than hand-rolling a fourth calling convention. Note from Alibaba's
docs: json_schema/strict structured-output support is tier-gated to certain
Qwen-Max/Plus models — this is exactly why AIModelConfig carries
`supports_enum_enforcement` per (family, model_id) row rather than assuming
every Qwen model behaves the same way.
"""

from django.conf import settings

from .openai_provider import OpenAIProvider

# Alibaba splits China vs. international accounts onto separate base URLs with
# non-interchangeable API keys. Default to the international endpoint since
# that's what a US-registered account uses; override via DASHSCOPE_BASE_URL
# for a China-region account.
DASHSCOPE_BASE_URL_DEFAULT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


class QwenProvider:
    def __init__(
        self,
        *,
        model_id: str,
        api_key: str,
        base_url: str | None = None,
        token_param_name: str = "max_completion_tokens",
    ) -> None:
        resolved_base_url = base_url or getattr(
            settings, "DASHSCOPE_BASE_URL", DASHSCOPE_BASE_URL_DEFAULT
        )
        # Defaults to OpenAI's modern param name (DashScope's compatibility
        # layer is documented as OpenAI-compatible, so this is the reasonable
        # default), but is overridable per-instance: if a live
        # `manage.py verify_ai_model qwen <model_id>` run shows DashScope
        # rejects it, construct with token_param_name="max_tokens" instead —
        # no change to OpenAIProvider required either way.
        self._delegate = OpenAIProvider(
            model_id=model_id,
            api_key=api_key,
            base_url=resolved_base_url,
            token_param_name=token_param_name,
        )

    def generate_structured(
        self,
        *,
        prompt: str,
        tool_name: str,
        description: str,
        schema: dict,
        max_tokens: int,
    ) -> dict:
        return self._delegate.generate_structured(
            prompt=prompt,
            tool_name=tool_name,
            description=description,
            schema=schema,
            max_tokens=max_tokens,
        )
