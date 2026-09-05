"""
fragrance/llm.py

Implements LLM components of fragrance recommendation generation.
Each function corresponds to one of the four structured LLM calls in the pipeline,
issued through a resolved `StructuredLLMProvider` (see fragrance/ai_providers) rather
than a hardcoded Anthropic client — the active provider is resolved once per run by
fragrance/tasks.py and threaded through every call so a run never straddles two
providers.
"""

import logging

from .ai_providers.base import StructuredLLMProvider
from .models import (
    Fragrance,
    PreferenceProfile,
    Recommendation,
    RecommendationRun,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schemas — define the structured output shape for each LLM call.
#
# `*_SCHEMA` constants are the inner JSON Schema object only (no vendor
# envelope: no {"name", "input_schema"} Anthropic wrapper, no {"type":
# "json_schema", "json_schema": {...}} OpenAI wrapper). Each provider adapter
# assembles its own request shape from (tool_name, description, schema) —
# keeping the envelope out of these constants is what stops a vendor-specific
# shape from leaking into every adapter.
# ---------------------------------------------------------------------------

PROFILE_TOOL_NAME = "create_profile"
PROFILE_TOOL_DESCRIPTION = (
    "Create a fragrance preference profile from the user collection."
)
PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "loved_notes": {"type": "string"},
        "liked_notes": {"type": "string"},
        "disliked_notes": {"type": "string"},
        "owns_list": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Names of fragrances the user currently owns",
        },
        "search_angle_1": {
            "type": "string",
            "description": "First targeted search query for discovery",
        },
        "search_angle_2": {
            "type": "string",
            "description": "Second targeted search query for discovery",
        },
    },
    "required": [
        "loved_notes",
        "liked_notes",
        "disliked_notes",
        "owns_list",
        "search_angle_1",
        "search_angle_2",
    ],
}

CANDIDATES_TOOL_NAME = "select_candidates"
CANDIDATES_TOOL_DESCRIPTION = (
    "Select exactly 5 fragrances to recommend, one per fragrance house."
)
CANDIDATES_SCHEMA = {
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
        },
    },
    "required": ["candidates"],
}
EXPECTED_CANDIDATE_COUNT = 5

REPLACEMENT_TOOL_NAME = "select_replacement"
REPLACEMENT_TOOL_DESCRIPTION = (
    "Select one replacement fragrance for a candidate that failed verification."
)
REPLACEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "house": {"type": "string"},
    },
    "required": ["name", "house"],
}

EMAIL_CONTENT_TOOL_NAME = "generate_email_content"
EMAIL_CONTENT_TOOL_DESCRIPTION = (
    "Generate a personalized intro and per-fragrance rationale for the "
    "recommendation email."
)


class CandidateCountError(ValueError):
    """
    Raised when an LLM response's candidate list doesn't have the exact
    expected count. No provider family reliably enforces minItems/maxItems at
    generation time (confirmed unsupported in OpenAI's documented strict
    subset behavior we could verify; unconfirmed but not to be trusted for
    Qwen; never enforced for Anthropic even pre-refactor) — this is an
    explicit app-level check, not schema-level trust.
    """


def build_email_content_tool_schema(confirmed_names: list[str]) -> dict:
    """
    Builds the generate_email_content schema for one call, constraining each
    rationale's "name" to the exact confirmed Recommendation names for this run
    via JSON Schema enum. This is the one schema where enum must be
    grammar-enforced, not advisory guidance the model can paraphrase away from
    — see registry.get_active_provider(enum_required=True), which refuses to
    resolve a model whose AIModelConfig.supports_enum_enforcement is False for
    exactly this call.
    """
    name_schema: dict = {
        "type": "string",
        "description": "Must be exactly one of the confirmed fragrance names given in the prompt.",
    }
    if confirmed_names:
        name_schema["enum"] = confirmed_names

    return {
        "type": "object",
        "properties": {
            "intro": {
                "type": "string",
                "description": "Personalized 2-3 sentence introduction paragraph",
            },
            "rationales": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": name_schema,
                        "rationale": {
                            "type": "string",
                            "description": "One sentence explaining why this suits the user",
                        },
                    },
                    "required": ["name", "rationale"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["intro", "rationales"],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

PROFILE_PROMPT = """\
You are a fragrance expert analyzing a user's collection to build a preference profile.

Fragrance collection:
{collection}

Previously recommended (never recommend these again):
{past_recommendations}

Analyze the collection to identify loved, liked, and disliked notes, then derive two distinct \
search queries that would surface fragrances matching this user's taste. \
Use the create_profile tool to return your analysis."""

CANDIDATES_PROMPT = """\
You are a fragrance expert selecting 5 fragrances to recommend.

User preference profile:
- Loved notes: {loved_notes}
- Liked notes: {liked_notes}
- Disliked notes: {disliked_notes}
- Currently owns: {owns_list}

Previously recommended (do not select): {past_recommendations}

Search results:
{search_results}

Select exactly 5 specific, purchasable fragrances from the search results. Requirements:
- Match the user's taste profile
- Not already owned or previously recommended
- One fragrance per house

Use the select_candidates tool to return your selections."""

REPLACEMENT_PROMPT = """\
A fragrance candidate could not be verified in search results and must be replaced.

Failed candidate: {failed_name}

User preference profile:
- Loved notes: {loved_notes}
- Liked notes: {liked_notes}
- Disliked notes: {disliked_notes}

Previously recommended (do not select): {past_recommendations}

Search results for the failed candidate:
{search_results}

Select one alternative fragrance that appears in the search results, matches the user's taste, \
and is not in the previously recommended list. \
Use the select_replacement tool to return your selection."""

EMAIL_CONTENT_PROMPT = """\
You are a fragrance expert writing a personalized monthly recommendation email.

User preference profile:
- Loved notes: {loved_notes}
- Liked notes: {liked_notes}

This month's fragrances:
{picks}

Write a warm 2-3 sentence intro referencing specific aspects of the user's taste, then one concise \
rationale sentence per fragrance explaining why it suits them. \
Use the generate_email_content tool to return your response."""


# ---------------------------------------------------------------------------
# LLM pipeline functions — called in order by fragrance/tasks.py
# ---------------------------------------------------------------------------


def generate_preference_profile(
    user_id: int, run_id: int, provider: StructuredLLMProvider
) -> PreferenceProfile:
    """LLM call #1. Reads the fragrance collection and writes a PreferenceProfile row."""
    fragrances = Fragrance.objects.filter(user_id=user_id)
    collection_text = "\n".join(
        f"{f.name} by {f.house} [{f.status}]"
        + (f" - {f.notes}" if f.notes else "")
        for f in fragrances
    )
    past_recommendations = list(
        Recommendation.objects.filter(user_id=user_id).values_list(
            "name", flat=True
        )
    )

    profile_data = provider.generate_structured(
        prompt=PROFILE_PROMPT.format(
            collection=collection_text,
            past_recommendations=", ".join(past_recommendations),
        ),
        tool_name=PROFILE_TOOL_NAME,
        description=PROFILE_TOOL_DESCRIPTION,
        schema=PROFILE_SCHEMA,
        max_tokens=1024,
    )
    profile = PreferenceProfile.objects.create(
        user_id=user_id,
        loved_notes=profile_data["loved_notes"],
        liked_notes=profile_data["liked_notes"],
        disliked_notes=profile_data["disliked_notes"],
        owns_list=profile_data["owns_list"],
        search_angle_1=profile_data["search_angle_1"],
        search_angle_2=profile_data["search_angle_2"],
    )
    # Link the profile back to the run so render_and_send_email can load it via select_related.
    RecommendationRun.objects.filter(id=run_id).update(profile=profile)
    return profile


def select_candidates(
    user_id: int,
    profile_id: int,
    search_results: str,
    provider: StructuredLLMProvider,
) -> list[dict]:
    """LLM call #2. Picks 5 fragrances from discovery search results."""
    profile = PreferenceProfile.objects.get(id=profile_id)
    past_recommendations = list(
        Recommendation.objects.filter(user_id=user_id).values_list(
            "name", flat=True
        )
    )

    result = provider.generate_structured(
        prompt=CANDIDATES_PROMPT.format(
            loved_notes=profile.loved_notes,
            liked_notes=profile.liked_notes,
            disliked_notes=profile.disliked_notes,
            owns_list=", ".join(profile.owns_list),
            past_recommendations=", ".join(past_recommendations),
            search_results=search_results,
        ),
        tool_name=CANDIDATES_TOOL_NAME,
        description=CANDIDATES_TOOL_DESCRIPTION,
        schema=CANDIDATES_SCHEMA,
        max_tokens=1024,
    )
    candidates = result["candidates"]
    if len(candidates) != EXPECTED_CANDIDATE_COUNT:
        raise CandidateCountError(
            f"select_candidates expected exactly {EXPECTED_CANDIDATE_COUNT} "
            f"candidates, got {len(candidates)}."
        )
    return candidates


def select_replacement(
    user_id: int,
    failed_candidate: dict,
    search_results: str,
    profile: PreferenceProfile,
    provider: StructuredLLMProvider,
) -> dict:
    """LLM call #3. Called by verify_candidates in search.py when a candidate fails title-match."""
    past_recommendations = list(
        Recommendation.objects.filter(user_id=user_id).values_list(
            "name", flat=True
        )
    )

    result = provider.generate_structured(
        prompt=REPLACEMENT_PROMPT.format(
            failed_name=failed_candidate["name"],
            loved_notes=profile.loved_notes,
            liked_notes=profile.liked_notes,
            disliked_notes=profile.disliked_notes,
            past_recommendations=", ".join(past_recommendations),
            search_results=search_results,
        ),
        tool_name=REPLACEMENT_TOOL_NAME,
        description=REPLACEMENT_TOOL_DESCRIPTION,
        schema=REPLACEMENT_SCHEMA,
        max_tokens=512,
    )
    if "name" not in result or "house" not in result:
        raise CandidateCountError(
            f"select_replacement response missing required field(s): {result!r}"
        )
    return result


def generate_email_content(
    run_id: int, verified_picks: list[dict], provider: StructuredLLMProvider
) -> None:
    """
    LLM call #4. Updates Recommendation rows with rationale and stores the intro on the run.
    verified_picks contains both 'confirmed' and 'replaced' entries; only confirmed picks
    receive rationale and appear in the email.
    """
    run = RecommendationRun.objects.select_related("profile").get(id=run_id)
    profile = run.profile

    # id order matches creation order — recs are inserted sequentially in search.py.
    recs = list(
        Recommendation.objects.filter(run_id=run_id, status="confirmed").order_by(
            "id"
        )
    )
    confirmed_names = [rec.name for rec in recs]

    confirmed = [p for p in verified_picks if p["status"] == "confirmed"]
    picks_text = "\n".join(f"- {p['name']} by {p['house']}" for p in confirmed)

    result = provider.generate_structured(
        prompt=EMAIL_CONTENT_PROMPT.format(
            loved_notes=profile.loved_notes,
            liked_notes=profile.liked_notes,
            picks=picks_text,
        ),
        tool_name=EMAIL_CONTENT_TOOL_NAME,
        description=EMAIL_CONTENT_TOOL_DESCRIPTION,
        schema=build_email_content_tool_schema(confirmed_names=confirmed_names),
        max_tokens=2048,
    )

    rationale_map = {r["name"]: r["rationale"] for r in result["rationales"]}
    for rec in recs:
        rec.rationale = rationale_map.get(rec.name, "")
    Recommendation.objects.bulk_update(objs=recs, fields=["rationale"])

    missing_names = [rec.name for rec in recs if not rec.rationale]
    if missing_names:
        logger.warning(
            "generate_email_content: run_id=%s missing rationale for pick(s): %s",
            run_id,
            ", ".join(missing_names),
        )

    run.intro = result["intro"]
    run.save(update_fields=["intro"])
