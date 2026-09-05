"""
fragrance/ai_providers/openai_provider.py

Uses the Chat Completions API's `response_format={"type": "json_schema", ...,
"strict": True}` mode, which grammar-enforces the schema at decode time —
including `enum` — the same guarantee the Anthropic path gets from forced
tool_choice.

Schema normalization: OpenAI's strict mode requires `additionalProperties:
false` on every object and *every* property to be listed in `required` (no
optional properties). `_to_strict_schema` adds both recursively so the four
schemas in llm.py don't need family-specific duplicates. It also strips
`minItems`/`maxItems`: those keywords are not reliably part of OpenAI's
documented supported subset for strict mode, and candidate-count enforcement
is handled uniformly for every provider at the application level (see
llm.py's post-generation count check) rather than leaned on here.
"""

import json
from copy import deepcopy
from typing import Any

import openai

from .base import StructuredOutputError


def _to_strict_schema(schema: dict) -> dict:
    """Returns a deep copy of `schema` normalized for OpenAI strict json_schema mode."""
    return _strict_walk(node=deepcopy(schema))


def _strict_walk(node: Any) -> Any:
    if isinstance(node, dict):
        node.pop("minItems", None)
        node.pop("maxItems", None)
        properties = node.get("properties")
        if isinstance(properties, dict):
            node["additionalProperties"] = False
            node["required"] = list(properties.keys())
            for value in properties.values():
                _strict_walk(node=value)
        items = node.get("items")
        if items is not None:
            _strict_walk(node=items)
        return node
    return node


def _parse_response(response: "openai.types.chat.ChatCompletion", tool_name: str) -> dict:
    try:
        message = response.choices[0].message
    except (IndexError, AttributeError) as exc:
        raise StructuredOutputError(
            f"OpenAI response for '{tool_name}' had no choices/message"
        ) from exc

    if getattr(message, "refusal", None):
        raise StructuredOutputError(
            f"OpenAI refused to generate '{tool_name}': {message.refusal}"
        )

    content = message.content
    if not content:
        raise StructuredOutputError(
            f"OpenAI response for '{tool_name}' had empty content"
        )

    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(
            f"OpenAI response for '{tool_name}' was not valid JSON: {exc}"
        ) from exc


class OpenAIProvider:
    def __init__(
        self,
        *,
        model_id: str,
        api_key: str,
        base_url: str | None = None,
        token_param_name: str = "max_completion_tokens",
    ) -> None:
        self._model_id = model_id
        self._api_key = api_key
        self._base_url = base_url
        self._client: openai.OpenAI | None = None
        # `max_tokens` is deprecated and rejected outright by OpenAI's
        # o-series reasoning models; `max_completion_tokens` is the modern,
        # universally-accepted name. Exposed as a constructor override (not
        # hardcoded) because QwenProvider composes over this class against
        # DashScope's OpenAI-compatible endpoint, which is not guaranteed to
        # accept the newer name — verify via `manage.py verify_ai_model qwen
        # <model_id>` against a real key before assuming it does; if it
        # doesn't, QwenProvider can pass token_param_name="max_tokens".
        self._token_param_name = token_param_name

    def _get_client(self) -> openai.OpenAI:
        # Constructed lazily so importing this module never requires an API key.
        if self._client is None:
            kwargs: dict = {"api_key": self._api_key}
            if self._base_url is not None:
                kwargs["base_url"] = self._base_url
            self._client = openai.OpenAI(**kwargs)
        return self._client

    def generate_structured(
        self,
        *,
        prompt: str,
        tool_name: str,
        description: str,
        schema: dict,
        max_tokens: int,
    ) -> dict:
        response = self._get_client().chat.completions.create(
            model=self._model_id,
            messages=[{"role": "user", "content": prompt}],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": tool_name,
                    "description": description,
                    "schema": _to_strict_schema(schema=schema),
                    "strict": True,
                },
            },
            **{self._token_param_name: max_tokens},
        )
        return _parse_response(response=response, tool_name=tool_name)
