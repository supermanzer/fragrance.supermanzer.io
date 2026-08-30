"""
fragrance/ai_providers/base.py

The port every LLM family adapts to. `schema` is always the *inner* JSON
Schema object alone (type/properties/required/...) — never a family-specific
wrapper like Anthropic's {"name", "description", "input_schema"} or OpenAI's
{"type": "json_schema", "json_schema": {...}}. Each adapter is responsible for
assembling its own family-specific request shape from (tool_name, description,
schema); keeping the wrapper out of the protocol is what stops the abstraction
from leaking a specific vendor's schema envelope into callers.
"""

from typing import Protocol, runtime_checkable

import anthropic
import openai


def sanitize_sdk_error_message(exc: Exception) -> str:
    """
    Returns a fixed, non-parameterized message for Anthropic/OpenAI SDK
    APIError exceptions instead of str(exc)/f"{exc}". OpenAI's own 401
    response body embeds a vendor-masked fragment of the submitted key (e.g.
    "Incorrect API key provided: sk-****...ZXY"); Anthropic and DashScope
    don't leak any key material in theirs, but every SDK APIError is treated
    the same way here so this stays the single place that decides what's
    safe to surface, rather than trusting each call site to know which
    vendor's error body leaks what. Non-SDK exceptions (config errors raised
    by this codebase, e.g. NoActiveAIModelConfigError) pass through as
    str(exc) unchanged — those messages are already ours and contain no
    vendor response text.

    Used everywhere an exception this code doesn't fully control might land
    on a capture surface — a DB field served to the frontend
    (RecommendationRun.error_message) or command stdout
    (verify_ai_model.py) — both of which this project has already had one
    real key-exposure incident against.
    """
    if isinstance(exc, (anthropic.APIError, openai.APIError)):
        return (
            "The configured AI provider rejected the request (authentication "
            "or API error). Check the active family's API key and try again."
        )
    return str(exc)


class StructuredOutputError(Exception):
    """
    Raised by an adapter when a provider's response cannot be parsed into the
    dict the caller asked for — malformed JSON, a refusal, a missing content
    block, etc. Never a bare KeyError/AttributeError/IndexError bubbling out of
    SDK response internals.
    """


@runtime_checkable
class StructuredLLMProvider(Protocol):
    def generate_structured(
        self,
        *,
        prompt: str,
        tool_name: str,
        description: str,
        schema: dict,
        max_tokens: int,
    ) -> dict:
        """
        Issue one structured-output call and return the parsed response as a
        dict matching `schema`. Must force the model to return exactly that
        shape (Anthropic: forced tool_choice; OpenAI/Qwen: strict json_schema
        response_format) — never fall back to free-text parsing.
        """
        ...
