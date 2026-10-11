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

from typing import Any, Protocol, runtime_checkable

import anthropic
import openai
import stripe
from config.log_setup import redact


# Both SDKs default to a 600s read timeout with 2 retries, far exceeding
# gunicorn's --timeout 120 (docker-compose.yaml). Every provider's client
# construction should pass this explicitly so a single slow/hung call fails
# fast into a reported CheckResult/StructuredOutputError instead of relying
# on gunicorn's worker-kill as the only backstop. Comfortably under 120s even
# accounting for the SDKs' own internal retries.
PROVIDER_TIMEOUT_SECONDS = 90.0


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


def sanitize_sdk_error_message(exc: Exception) -> str:
    """
    Returns a fixed, non-parameterized message for Anthropic/OpenAI/Stripe
    SDK error exceptions instead of str(exc)/f"{exc}". OpenAI's own 401
    response body embeds a vendor-masked fragment of the submitted key (e.g.
    "Incorrect API key provided: sk-****...ZXY"); Anthropic and DashScope
    don't leak any key material in theirs, but every SDK error is treated
    the same way here so this stays the single place that decides what's
    safe to surface, rather than trusting each call site to know which
    vendor's error body leaks what. Non-SDK exceptions (config errors raised
    by this codebase, e.g. NoActiveAIModelConfigError) pass through as
    str(exc) unchanged — those messages are already ours and contain no
    vendor response text.

    Used everywhere an exception this code doesn't fully control might land
    on a capture surface — a DB field served to the frontend
    (RecommendationRun.error_message, StripeEvent.error) or an API response
    body (BillingCheckoutView, BillingSessionView) — all of which this
    project has already had one real key-exposure incident against.
    Note: stripe.StripeError, not stripe.error.StripeError -- the latter is
    the pre-v8 stripe-python layout and doesn't exist in the pinned version.
    """
    if isinstance(exc, (anthropic.APIError, openai.APIError)):
        return (
            "The configured AI provider rejected the request (authentication "
            "or API error). Check the active family's API key and try again."
        )
    if isinstance(exc, stripe.APIConnectionError):
        return "Could not reach Stripe. Try again shortly."
    if isinstance(exc, stripe.StripeError):
        return "Stripe rejected the request. Check the Stripe configuration and try again."
    return str(exc)


def sanitized_exc_info(
    exc: Exception, drop_message: bool = False
) -> tuple[type[BaseException], BaseException, Any]:
    """
    An exc_info tuple for logging that keeps the traceback frames but swaps the
    exception (and anything chained to it) for one carrying only
    sanitize_sdk_error_message(exc). Passing the real exception as exc_info
    would put the raw SDK message, which sanitize_sdk_error_message exists to
    keep out of logs, straight back into the traceback's last line.

    drop_message=True keeps only the exception's class name: for failures
    whose text this code does not control and that sanitize_sdk_error_message
    would pass through unchanged (a broker or cache error can echo its
    connection URL).
    """
    stand_in_type = type(type(exc).__name__, (Exception,), {'__module__': type(exc).__module__})
    stand_in = stand_in_type('' if drop_message else sanitize_sdk_error_message(exc=exc))
    return stand_in_type, stand_in, exc.__traceback__


def sanitized_exception(*, exc: Exception, drop_message: bool = False) -> Exception:
    """
    A stand-in for exc that is safe to hand to Celery's self.retry(exc=...) or
    to re-raise out of a task. Celery logs a retry as "Retry in Ns: <repr(exc)>"
    (no exc_info, so no log filter can repair it) and writes the exception to
    the result backend; the stand-in carries only the redacted
    sanitize_sdk_error_message(exc), or nothing when drop_message is set.

    The stand-in subclasses type(exc), so isinstance checks and
    `except SomeError` in callers keep working, and keeps exc's traceback
    frames. The original __init__ is never run (SDK constructors need request
    and response objects), and __str__/__repr__ are BaseException's because
    SDK overrides read attributes the stand-in does not have. Never raises: if
    the subclass cannot be built it falls back to a plain Exception subclass
    with the same name and module.
    """
    try:
        message = '' if drop_message else redact(text=sanitize_sdk_error_message(exc=exc))
    except Exception:
        message = ''
    namespace = {
        '__module__': type(exc).__module__,
        '__str__': BaseException.__str__,
        '__repr__': BaseException.__repr__,
    }
    try:
        stand_in_type = type(type(exc).__name__, (type(exc),), namespace)
        stand_in = stand_in_type.__new__(stand_in_type, message)
        BaseException.__init__(stand_in, message)
    except Exception:
        stand_in_type = type(type(exc).__name__, (Exception,), namespace)
        stand_in = stand_in_type(message)
    return stand_in.with_traceback(exc.__traceback__)
