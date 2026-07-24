"""
fragrance/llm.py

Implements LLM components of fragrance recommendation generation.
Each function corresponds to one of the four structured Anthropic SDK calls in the pipeline.
"""

import logging

import anthropic

from .models import (
    Fragrance,
    PreferenceProfile,
    Recommendation,
    RecommendationRun,
)

client = anthropic.Anthropic()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schemas — define the structured output shape for each LLM call.
# Anthropic's tool_choice forces the model to invoke the named tool, so the
# response is always parseable as JSON without any fallback string parsing.
# ---------------------------------------------------------------------------

MODEL = "claude-haiku-4-5-20251001"

PROFILE_TOOL_SCHEMA = {
    "name": "create_profile",
    "description": "Create a fragrance preference profile from the user collection.",
    "input_schema": {
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
    },
}

CANDIDATES_TOOL_SCHEMA = {
    "name": "select_candidates",
    "description": "Select exactly 5 fragrances to recommend, one per fragrance house.",
    "input_schema": {
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
    },
}

REPLACEMENT_TOOL_SCHEMA = {
    "name": "select_replacement",
    "description": "Select one replacement fragrance for a candidate that failed verification.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "house": {"type": "string"},
        },
        "required": ["name", "house"],
    },
}


def build_email_content_tool_schema(confirmed_names: list[str]) -> dict:
    """
    Builds the generate_email_content tool schema for one call, constraining each
    rationale's "name" to the exact confirmed Recommendation names for this run via
    JSON Schema enum. "strict" is set so the enum is grammar-enforced by the API rather
    than advisory guidance the model can still paraphrase away from (bare enum without
    strict is not sampling-constrained) — see Anthropic's strict tool use docs.
    """
    name_schema: dict = {
        "type": "string",
        "description": "Must be exactly one of the confirmed fragrance names given in the prompt.",
    }
    if confirmed_names:
        name_schema["enum"] = confirmed_names

    return {
        "name": "generate_email_content",
        "description": "Generate a personalized intro and per-fragrance rationale for the recommendation email.",
        "strict": True,
        "input_schema": {
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
        },
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
# Helper
# ---------------------------------------------------------------------------


def extract_tool_result(response: anthropic.types.Message) -> dict:
    # tool_choice forces exactly one tool_use block; this is always the first content item.
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    raise ValueError(
        f"No tool_use block in LLM response. Stop reason: {response.stop_reason}"
    )


# ---------------------------------------------------------------------------
# LLM pipeline functions — called in order by fragrance/tasks.py
# ---------------------------------------------------------------------------


def generate_preference_profile(user_id: int, run_id: int) -> PreferenceProfile:
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

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        tools=[PROFILE_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "create_profile"},
        messages=[
            {
                "role": "user",
                "content": PROFILE_PROMPT.format(
                    collection=collection_text,
                    past_recommendations=", ".join(past_recommendations),
                ),
            }
        ],
    )
    profile_data = extract_tool_result(response=response)
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
    user_id: int, profile_id: int, search_results: str
) -> list[dict]:
    """LLM call #2. Picks 5 fragrances from discovery search results."""
    profile = PreferenceProfile.objects.get(id=profile_id)
    past_recommendations = list(
        Recommendation.objects.filter(user_id=user_id).values_list(
            "name", flat=True
        )
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        tools=[CANDIDATES_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "select_candidates"},
        messages=[
            {
                "role": "user",
                "content": CANDIDATES_PROMPT.format(
                    loved_notes=profile.loved_notes,
                    liked_notes=profile.liked_notes,
                    disliked_notes=profile.disliked_notes,
                    owns_list=", ".join(profile.owns_list),
                    past_recommendations=", ".join(past_recommendations),
                    search_results=search_results,
                ),
            }
        ],
    )
    result = extract_tool_result(response=response)
    return result["candidates"]


def select_replacement(
    user_id: int,
    failed_candidate: dict,
    search_results: str,
    profile: PreferenceProfile,
) -> dict:
    """LLM call #3. Called by verify_candidates in search.py when a candidate fails title-match."""
    past_recommendations = list(
        Recommendation.objects.filter(user_id=user_id).values_list(
            "name", flat=True
        )
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=512,
        tools=[REPLACEMENT_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "select_replacement"},
        messages=[
            {
                "role": "user",
                "content": REPLACEMENT_PROMPT.format(
                    failed_name=failed_candidate["name"],
                    loved_notes=profile.loved_notes,
                    liked_notes=profile.liked_notes,
                    disliked_notes=profile.disliked_notes,
                    past_recommendations=", ".join(past_recommendations),
                    search_results=search_results,
                ),
            }
        ],
    )
    return extract_tool_result(response=response)


def generate_email_content(run_id: int, verified_picks: list[dict]) -> None:
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

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        tools=[build_email_content_tool_schema(confirmed_names=confirmed_names)],
        tool_choice={"type": "tool", "name": "generate_email_content"},
        messages=[
            {
                "role": "user",
                "content": EMAIL_CONTENT_PROMPT.format(
                    loved_notes=profile.loved_notes,
                    liked_notes=profile.liked_notes,
                    picks=picks_text,
                ),
            }
        ],
    )
    result = extract_tool_result(response=response)

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
