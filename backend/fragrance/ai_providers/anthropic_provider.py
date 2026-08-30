"""
fragrance/ai_providers/anthropic_provider.py

Wraps the forced-tool_choice calling pattern that lived in llm.py before this
refactor. Behavior for the Anthropic path must not change: same tool_choice
forcing, same single tool_use-block extraction.
"""

import anthropic

from .base import StructuredOutputError


def extract_tool_result(response: "anthropic.types.Message") -> dict:
    # tool_choice forces exactly one tool_use block; this is always the first content item.
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    raise StructuredOutputError(
        f"No tool_use block in Anthropic response. Stop reason: {response.stop_reason}"
    )


def _contains_enum(node: object) -> bool:
    """
    True if `node` (a JSON Schema fragment) declares an `enum` anywhere. Used
    to decide whether to request Anthropic's `strict: true` tool mode, which
    grammar-enforces `enum` but requires `additionalProperties: false`
    throughout and does not support minItems/maxItems — so it's requested only
    for schemas that actually need enum enforcement (currently: email content
    only), preserving the pre-refactor request shape for the other three calls.
    """
    if isinstance(node, dict):
        if "enum" in node:
            return True
        return any(_contains_enum(node=value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_enum(node=item) for item in node)
    return False


class AnthropicProvider:
    def __init__(self, *, model_id: str, api_key: str) -> None:
        self._model_id = model_id
        self._api_key = api_key
        self._client: anthropic.Anthropic | None = None

    def _get_client(self) -> anthropic.Anthropic:
        # Constructed lazily so importing this module — and anything that
        # imports it — never requires ANTHROPIC_API_KEY to be set.
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=self._api_key)
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
        tool_def: dict = {
            "name": tool_name,
            "description": description,
            "input_schema": schema,
        }
        if _contains_enum(node=schema):
            tool_def["strict"] = True

        response = self._get_client().messages.create(
            model=self._model_id,
            max_tokens=max_tokens,
            tools=[tool_def],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": prompt}],
        )
        return extract_tool_result(response=response)
