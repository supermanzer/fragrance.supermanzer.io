"""
fragrance/ai_providers/verification.py

Shared conformance-check logic for a (family, model_id) pair, extracted out
of manage.py verify_ai_model so it has exactly one implementation shared by
two callers: the CLI command and the AIModelConfig admin change-page button.

Runs all four real pipeline schema shapes (profile, candidates, replacement,
email-content-with-enum) against a provider and validates each response
structurally — including the enum constraint on email-content, the one call
where a wrong/hallucinated name would silently corrupt a RecommendationRun if
enum weren't actually grammar-enforced (see registry.py's
EnumEnforcementUnsupportedError docstring).

`persist_verification_results` is the single place that decides how a
conformance run changes an AIModelConfig row's trust fields:

- Full 4/4 pass: verified_at is stamped, supports_enum_enforcement is set
  from the outcome of the email-content-with-enum check specifically (that's
  the one check that actually exercises enum-constrained output), and
  supports_strict_schema is set to the same full-pass outcome — see the
  docstring on ENUM_CHECK_LABEL below for why "full pass" is the right proxy
  for strict-schema support given how the three provider adapters differ.
- Partial pass, every check reached the provider: verified_at is left
  untouched (preserves whatever value it already had), but both capability
  flags are reset to False. A stale True on either flag left standing after
  a re-verification that just failed would let
  registry.get_active_config(enum_required=True) keep trusting a model that
  no longer earns that trust.
- Inconclusive (any check never got a real answer — connectivity, auth, or
  rate-limit failure rather than the model actually responding): nothing is
  touched at all. verified_at, supports_enum_enforcement, and
  supports_strict_schema are all left exactly as they were. A transport
  failure carries zero information about the model's actual capability, so
  it must never be scored the same as "the model answered and got it
  wrong" — see CheckResult.inconclusive below.

Never creates an AIModelConfig row — both callers only ever act on a row
that already exists for (family, model_id); an operator creates the row via
/admin/ first.
"""

from dataclasses import dataclass

import anthropic
import openai
from django.utils import timezone

from ..llm import (
    CANDIDATES_PROMPT,
    CANDIDATES_SCHEMA,
    CANDIDATES_TOOL_DESCRIPTION,
    CANDIDATES_TOOL_NAME,
    EMAIL_CONTENT_PROMPT,
    EMAIL_CONTENT_TOOL_DESCRIPTION,
    EMAIL_CONTENT_TOOL_NAME,
    PROFILE_PROMPT,
    PROFILE_SCHEMA,
    PROFILE_TOOL_DESCRIPTION,
    PROFILE_TOOL_NAME,
    REPLACEMENT_PROMPT,
    REPLACEMENT_SCHEMA,
    REPLACEMENT_TOOL_DESCRIPTION,
    REPLACEMENT_TOOL_NAME,
    build_email_content_tool_schema,
)
from ..models import AIModelConfig
from .base import StructuredLLMProvider, StructuredOutputError, sanitize_sdk_error_message

DUMMY_COLLECTION = (
    "Aventus by Creed [own] - my signature scent\n"
    "Bleu de Chanel by Chanel [like]\n"
    "Sauvage by Dior [dislike] - too generic"
)
DUMMY_PAST_RECOMMENDATIONS = "Silver Mountain Water"
# Must offer more than EXPECTED_CANDIDATE_COUNT distinct houses: the candidates
# prompt asks for exactly 5 picks, one per house, from these results, so a
# fixture with fewer would fail any model that declines to invent fragrances
# (claude-haiku-5-5 returned 2 of 2 real ones against a 2-entry fixture).
DUMMY_SEARCH_RESULTS = (
    "Search 1: niche oud fragrances:\n"
    "- Oud Wood by Tom Ford: A smoky, creamy oud blend. (https://example.com/oud-wood)\n"
    "- Black Afgano by Nasomatto: A dense, resinous oud. (https://example.com/black-afgano)\n"
    "- Oud for Greatness by Initio: A bold oud with saffron and musk. (https://example.com/oud-for-greatness)\n"
    "Search 2: amber and warm woody fragrances:\n"
    "- Layton by Parfums de Marly: Apple, vanilla and warm woods. (https://example.com/layton)\n"
    "- Hacivat by Nishane: Fruity pineapple over moss and woods. (https://example.com/hacivat)\n"
    "- Grand Soir by Maison Francis Kurkdjian: A rich amber with benzoin. (https://example.com/grand-soir)\n"
    "- Santal 33 by Le Labo: Sandalwood, leather and cedar. (https://example.com/santal-33)"
)
DUMMY_CONFIRMED_NAMES = ["Aventus", "Bleu de Chanel"]
DUMMY_PICKS_TEXT = "- Aventus by Creed\n- Bleu de Chanel by Chanel"

# The one check that actually requests enum-constrained structured output
# (see anthropic_provider.py's _contains_enum: it's the only schema of the
# four containing an `enum`, so it's the only call where Anthropic's adapter
# sets tool_def["strict"] = True at all). supports_enum_enforcement is set
# from this check's outcome alone, never from the other three.
ENUM_CHECK_LABEL = "email-content-with-enum"


@dataclass(frozen=True)
class CheckResult:
    label: str
    passed: bool
    detail: str
    # True when this check never actually got a real answer from the model
    # (a connectivity/auth/rate-limit/timeout failure from the SDK) — as
    # opposed to a genuine schema failure, where the model did respond and
    # the response was simply wrong. `passed` stays False either way (so
    # pass_count/total math is unaffected), but `inconclusive` is what lets
    # persist_verification_results tell "this model can't be trusted" apart
    # from "we never found out". Defaults to False so existing positional/
    # keyword CheckResult(...) construction elsewhere (tests, callers) is
    # unaffected.
    inconclusive: bool = False


def _validate_against_schema(data: object, schema: dict, path: str = "$") -> list[str]:
    """
    Minimal JSON Schema validator covering exactly the subset our four
    pipeline schemas use (object/array/string, properties/required/items/enum/
    additionalProperties/minItems/maxItems) — not a general-purpose validator.
    """
    errors: list[str] = []
    expected_type = schema.get("type")

    if expected_type == "object":
        if not isinstance(data, dict):
            return [f"{path}: expected object, got {type(data).__name__}"]
        for required_key in schema.get("required", []):
            if required_key not in data:
                errors.append(f"{path}: missing required property '{required_key}'")
        properties = schema.get("properties", {})
        for key, value in data.items():
            if key in properties:
                errors.extend(
                    _validate_against_schema(
                        data=value, schema=properties[key], path=f"{path}.{key}"
                    )
                )
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property '{key}'")

    elif expected_type == "array":
        if not isinstance(data, list):
            return [f"{path}: expected array, got {type(data).__name__}"]
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(data):
                errors.extend(
                    _validate_against_schema(
                        data=item, schema=item_schema, path=f"{path}[{index}]"
                    )
                )
        min_items = schema.get("minItems")
        if min_items is not None and len(data) < min_items:
            errors.append(f"{path}: expected at least {min_items} items, got {len(data)}")
        max_items = schema.get("maxItems")
        if max_items is not None and len(data) > max_items:
            errors.append(f"{path}: expected at most {max_items} items, got {len(data)}")

    elif expected_type == "string":
        if not isinstance(data, str):
            errors.append(f"{path}: expected string, got {type(data).__name__}")
        elif "enum" in schema and data not in schema["enum"]:
            errors.append(f"{path}: value {data!r} not in enum {schema['enum']}")

    return errors


def _build_checks() -> list[dict]:
    return [
        {
            "label": "profile",
            "prompt": PROFILE_PROMPT.format(
                collection=DUMMY_COLLECTION,
                past_recommendations=DUMMY_PAST_RECOMMENDATIONS,
            ),
            "tool_name": PROFILE_TOOL_NAME,
            "description": PROFILE_TOOL_DESCRIPTION,
            "schema": PROFILE_SCHEMA,
            "max_tokens": 1024,
        },
        {
            "label": "candidates",
            "prompt": CANDIDATES_PROMPT.format(
                loved_notes="oud, amber",
                liked_notes="citrus",
                disliked_notes="aquatic",
                owns_list="Aventus",
                past_recommendations=DUMMY_PAST_RECOMMENDATIONS,
                search_results=DUMMY_SEARCH_RESULTS,
            ),
            "tool_name": CANDIDATES_TOOL_NAME,
            "description": CANDIDATES_TOOL_DESCRIPTION,
            "schema": CANDIDATES_SCHEMA,
            "max_tokens": 1024,
        },
        {
            "label": "replacement",
            "prompt": REPLACEMENT_PROMPT.format(
                failed_name="Silver Mountain Water",
                loved_notes="oud, amber",
                liked_notes="citrus",
                disliked_notes="aquatic",
                past_recommendations=DUMMY_PAST_RECOMMENDATIONS,
                search_results=DUMMY_SEARCH_RESULTS,
            ),
            "tool_name": REPLACEMENT_TOOL_NAME,
            "description": REPLACEMENT_TOOL_DESCRIPTION,
            "schema": REPLACEMENT_SCHEMA,
            "max_tokens": 512,
        },
        {
            "label": ENUM_CHECK_LABEL,
            "prompt": EMAIL_CONTENT_PROMPT.format(
                loved_notes="oud, amber",
                liked_notes="citrus",
                picks=DUMMY_PICKS_TEXT,
            ),
            "tool_name": EMAIL_CONTENT_TOOL_NAME,
            "description": EMAIL_CONTENT_TOOL_DESCRIPTION,
            "schema": build_email_content_tool_schema(
                confirmed_names=DUMMY_CONFIRMED_NAMES
            ),
            "max_tokens": 2048,
        },
    ]


def run_verification_checks(*, provider: StructuredLLMProvider) -> list[CheckResult]:
    """
    Issues all four pipeline schema shapes against `provider` and validates
    each response. Never raises for a per-check provider/schema failure —
    those become a failing CheckResult so the caller can report all four
    outcomes together; only truly unexpected bugs in this function itself
    would propagate.
    """
    results: list[CheckResult] = []
    for check in _build_checks():
        label = check["label"]
        try:
            response = provider.generate_structured(
                prompt=check["prompt"],
                tool_name=check["tool_name"],
                description=check["description"],
                schema=check["schema"],
                max_tokens=check["max_tokens"],
            )
        except StructuredOutputError as exc:
            # The model answered and the answer couldn't be turned into the
            # requested shape — a genuine, definitive failure.
            results.append(CheckResult(label=label, passed=False, detail=f"provider error: {exc}"))
            continue
        except (anthropic.BadRequestError, openai.BadRequestError) as exc:
            # A BadRequestError only carries capability signal on the one
            # check that actually requests strict/enum-constrained tool mode
            # (ENUM_CHECK_LABEL — see anthropic_provider.py's _contains_enum
            # gating and openai_provider.py's unconditional strict mode). On
            # that check, a 400 rejecting the request shape IS the signal
            # this harness exists to catch, so it's a definitive failure.
            #
            # On the other three checks (profile, candidates, replacement),
            # nothing about the request ever asks for strict/enum mode, so a
            # BadRequestError there cannot mean "the model rejected
            # strict-schema mode" — it means something else entirely
            # (malformed request, invalid model_id, missing required header,
            # bad max_tokens, a misconfigured workspace id, ...). Treating
            # that as capability evidence is wrong, so it's routed to the
            # same inconclusive=True treatment as the general `except
            # Exception` branch below: uninformative about model capability
            # either way.
            results.append(
                CheckResult(
                    label=label,
                    passed=False,
                    inconclusive=label != ENUM_CHECK_LABEL,
                    detail=f"provider rejected request: {sanitize_sdk_error_message(exc)}",
                )
            )
            continue
        except Exception as exc:  # connectivity/auth/rate-limit/timeout — no signal about model capability
            # Sanitized: an OpenAI/Qwen auth failure's exception text can
            # carry a vendor-masked key fragment, and both the command's
            # stdout and the admin's message_user() are known capture
            # surfaces for this project. inconclusive=True: the request
            # never reached the model (or never got a real response back),
            # so this carries zero information about the model's actual
            # capability — persist_verification_results must not treat it as
            # evidence of anything.
            results.append(
                CheckResult(
                    label=label,
                    passed=False,
                    inconclusive=True,
                    detail=f"unexpected error: {sanitize_sdk_error_message(exc)}",
                )
            )
            continue

        errors = _validate_against_schema(data=response, schema=check["schema"])
        if errors:
            results.append(CheckResult(label=label, passed=False, detail="; ".join(errors)))
        else:
            results.append(CheckResult(label=label, passed=True, detail="ok"))

    return results


@dataclass(frozen=True)
class PersistOutcome:
    """
    Return value of persist_verification_results. `status` is one of:

    - "no_matching_row": no AIModelConfig exists for (family, model_id);
      config is None. Nothing was persisted.
    - "inconclusive": at least one check in `results` never got a real
      answer from the provider (connectivity/auth/rate-limit/timeout —
      CheckResult.inconclusive). Nothing was persisted — verified_at and
      both capability flags are exactly as they were before this call.
    - "full_pass": every check reached the provider and all passed.
      verified_at was stamped and both capability flags were set.
    - "partial_fail": every check reached the provider and at least one
      failed on its own terms (schema violation or a rejected request
      shape). Both capability flags were reset to False; verified_at was
      left untouched.
    """

    config: AIModelConfig | None
    status: str


def persist_verification_results(
    *, family: str, model_id: str, results: list[CheckResult]
) -> PersistOutcome:
    """
    Applies `results` to the matching AIModelConfig row for (family, model_id),
    if one exists. See PersistOutcome for the possible outcomes — callers are
    responsible for telling the operator to create a row via /admin/ on
    "no_matching_row", and for surfacing "inconclusive" as a distinct message
    from "verification ran and the model failed some checks"; this function
    never creates a row itself.
    """
    config = AIModelConfig.objects.filter(family=family, model_id=model_id).first()
    if config is None:
        return PersistOutcome(config=None, status="no_matching_row")

    if any(result.inconclusive for result in results):
        # At least one check never reached the model at all — a
        # connectivity/auth/rate-limit/timeout failure carries zero
        # information about whether this model actually supports enum
        # enforcement or strict schema mode. Touching verified_at or either
        # capability flag here would corrupt real trust state based on no
        # actual evidence, including on the currently active row.
        return PersistOutcome(config=config, status="inconclusive")

    total = len(results)
    pass_count = sum(1 for result in results if result.passed)
    full_pass = total > 0 and pass_count == total

    if full_pass:
        enum_check_passed = any(
            result.label == ENUM_CHECK_LABEL and result.passed for result in results
        )
        config.verified_at = timezone.now()
        config.supports_enum_enforcement = enum_check_passed
        # "Strict schema" support has no single cross-family definition in
        # this codebase: Anthropic's adapter only ever requests strict tool
        # mode for the one schema that contains an enum (see
        # anthropic_provider.py's _contains_enum), while the OpenAI/Qwen
        # adapter requests strict json_schema mode for *every* call
        # (openai_provider.py's _to_strict_schema is applied unconditionally).
        # A full 4/4 pass is the one signal that generalizes across both
        # designs: for OpenAI/Qwen it directly proves strict mode held across
        # all four shapes, and for Anthropic it proves the one call that
        # exercises strict mode passed alongside three calls that didn't need
        # it. Anything narrower would either be family-specific logic living
        # in a family-agnostic function, or would double as a second copy of
        # the enum check with a different name.
        config.supports_strict_schema = True
        config.save(
            update_fields=[
                "verified_at",
                "supports_enum_enforcement",
                "supports_strict_schema",
            ]
        )
        return PersistOutcome(config=config, status="full_pass")

    # Partial pass, every check reached the provider: verified_at is
    # deliberately left untouched (preserve whatever it already was), but a
    # re-verification that just failed must not leave a stale True standing
    # on either capability flag — registry.get_active_config(enum_required=True)
    # trusts supports_enum_enforcement at face value.
    config.supports_enum_enforcement = False
    config.supports_strict_schema = False
    config.save(update_fields=["supports_enum_enforcement", "supports_strict_schema"])
    return PersistOutcome(config=config, status="partial_fail")
