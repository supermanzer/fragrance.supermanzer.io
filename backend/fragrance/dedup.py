"""
fragrance/dedup.py

Deterministic repeat enforcement for the recommendation pipeline. The
exclusion set is built from the database (every Recommendation of the user,
any status and run, plus every Fragrance in their collection) and candidates
from the LLM are checked against it in plain Python. The model's `owns_list`
and its own reading of the prompt are never relied on. No network calls.
"""

import logging
import unicodedata
from dataclasses import dataclass

from .models import Fragrance, Recommendation
from .slugs import (
    combined_slug,
    core_name,
    is_invalid_name,
    is_unknown_house,
    same_fragrance,
    slugify_house,
)

logger = logging.getLogger(__name__)

REASON_OWNED = "owned"
REASON_COLLECTION = "in_collection"
REASON_PREVIOUSLY_RECOMMENDED = "previously_recommended"
REASON_DUPLICATE_IN_BATCH = "duplicate_in_batch"
REASON_INVALID_NAME = "invalid_name"

# Prompt-size policy for the exclusion lists. Only the TEXT sent to the model
# is bounded; deterministic enforcement (partition_candidates) always runs
# against the full known set, so an entry the prompt omitted is still rejected
# and re-asked. Lists are rendered most-recent-first and cut at whichever limit
# is hit first, with a "(+N older ... not shown)" marker.
MAX_PROMPT_LABEL_CHARS = 80
MAX_PROMPT_LIST_ENTRIES = 200
MAX_PROMPT_LIST_CHARS = 6000
MAX_PROMPT_NOTES_CHARS = 500


@dataclass(frozen=True)
class KnownFragrance:
    name: str
    house: str
    reason: str
    ref: str
    # Batch members of the current run block the same core name for any
    # house: the email rationale map and enum are keyed by name alone.
    any_house: bool = False


@dataclass(frozen=True)
class Rejection:
    candidate: dict
    reason: str
    ref: str
    wildcard: bool


def normalize_label(text: str, max_chars: int = MAX_PROMPT_LABEL_CHARS) -> str:
    """
    Untrusted free text (user rows, model output, web-derived names) made safe
    to interpolate into a prompt line: control/format/line-separator characters
    removed, whitespace collapsed to single spaces, at most `max_chars`.
    """
    cleaned = "".join(
        " " if unicodedata.category(ch) in {"Zs", "Zl", "Zp"} or ch in "\t\n\r" else ch
        for ch in (text or "")
        if unicodedata.category(ch) not in {"Cc", "Cf", "Cs", "Co", "Cn"} or ch in "\t\n\r"
    )
    return " ".join(cleaned.split())[:max_chars].rstrip()


def fragrance_label(name: str, house: str) -> str:
    name_label = normalize_label(name)
    if is_unknown_house(house):
        return name_label
    return f"{name_label} by {normalize_label(house)}"


def format_exclusion_list(labels: list[str]) -> str:
    """
    Comma-joined prompt text for an exclusion list given oldest-first; emitted
    most-recent-first, capped at MAX_PROMPT_LIST_ENTRIES / MAX_PROMPT_LIST_CHARS.
    """
    kept: list[str] = []
    used = 0
    for label in reversed(labels):
        if len(kept) >= MAX_PROMPT_LIST_ENTRIES or used + len(label) + 2 > MAX_PROMPT_LIST_CHARS:
            break
        kept.append(label)
        used += len(label) + 2
    text = ", ".join(kept)
    omitted = len(labels) - len(kept)
    if omitted:
        text += f" (+{omitted} older entries not shown; they are still excluded)"
    return text


def delivered_recommendations(user_id: int):
    """
    The user's Recommendation rows that count as history: every status
    ('replaced' included), but not rows of a run that FAILED -- those picks
    never reached the user, so they must not shrink the pool forever. The
    in-flight run is 'running', so its own picks stay in the set.
    """
    return Recommendation.objects.filter(user_id=user_id).exclude(run__status="failed")


def build_known_fragrances(user_id: int) -> list[KnownFragrance]:
    known: list[KnownFragrance] = []
    for frag in Fragrance.objects.filter(user_id=user_id).order_by("id"):
        reason = REASON_OWNED if frag.status == "own" else REASON_COLLECTION
        known.append(
            KnownFragrance(
                name=frag.name, house=frag.house, reason=reason, ref=f"fragrance:{frag.id}"
            )
        )
    for rec in delivered_recommendations(user_id=user_id).order_by("id"):
        known.append(
            KnownFragrance(
                name=rec.name,
                house=rec.house,
                reason=REASON_PREVIOUSLY_RECOMMENDED,
                ref=f"recommendation:{rec.id}",
            )
        )
    return known


def batch_known(candidates: list[dict]) -> list[KnownFragrance]:
    return [
        KnownFragrance(
            name=c["name"],
            house=c.get("house", ""),
            reason=REASON_DUPLICATE_IN_BATCH,
            ref=f"batch:{index}",
            any_house=True,
        )
        for index, c in enumerate(candidates)
    ]


def _match(candidate: dict, known: KnownFragrance) -> bool:
    if known.any_house:
        return core_name(candidate["name"]) == core_name(known.name) != ""
    return same_fragrance(
        candidate["name"], candidate.get("house", ""), known.name, known.house
    )


def partition_candidates(
    candidates: list[dict], known: list[KnownFragrance]
) -> tuple[list[dict], list[Rejection]]:
    """Split into (kept, rejected). A candidate is also checked against earlier kept ones."""
    kept: list[dict] = []
    rejections: list[Rejection] = []
    for candidate in candidates:
        if is_invalid_name(candidate.get("name", "")):
            rejections.append(
                Rejection(
                    candidate=candidate, reason=REASON_INVALID_NAME, ref="-", wildcard=False
                )
            )
            continue
        rejection = _first_rejection(candidate, known + batch_known(kept))
        if rejection is None:
            kept.append(candidate)
        else:
            rejections.append(rejection)
    return kept, rejections


def _first_rejection(candidate: dict, known: list[KnownFragrance]) -> Rejection | None:
    for entry in known:
        if _match(candidate, entry):
            wildcard = not entry.any_house and (
                is_unknown_house(candidate.get("house", "")) or is_unknown_house(entry.house)
            )
            return Rejection(
                candidate=candidate, reason=entry.reason, ref=entry.ref, wildcard=wildcard
            )
    return None


def log_rejections(*, user_id: int, stage: str, attempt: int, rejections: list[Rejection]) -> None:
    """ids and slugs only: no emails, no free text from the model beyond slugs."""
    for rejection in rejections:
        name = rejection.candidate["name"]
        house = rejection.candidate.get("house", "")
        logger.warning(
            "dedup rejected candidate: user_id=%s stage=%s attempt=%s name_slug=%s "
            "house_slug=%s reason=%s match=%s unknown_house_wildcard=%s",
            user_id,
            stage,
            attempt,
            core_name(name) or "-",
            slugify_house(house) or "-",
            rejection.reason,
            rejection.ref,
            rejection.wildcard,
        )


def describe_rejections(rejections: list[Rejection]) -> str:
    return "; ".join(
        f"{fragrance_label(r.candidate['name'], r.candidate.get('house', '')) or '(blank name)'} "
        f"({r.reason})"
        for r in rejections
    )


def describe_rejections_as_slugs(rejections: list[Rejection]) -> str:
    """Slug-only form for exception text (ends up in run.error_message / tracebacks)."""
    return "; ".join(
        f"{combined_slug(name=r.candidate['name'], house=r.candidate.get('house', '')) or '-'} "
        f"({r.reason})"
        for r in rejections
    )
