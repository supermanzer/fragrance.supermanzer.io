"""
fragrance/dedup.py

Deterministic repeat enforcement for the recommendation pipeline. The
exclusion set is built from the database (every Recommendation of the user,
any status and run, plus every Fragrance in their collection) and candidates
from the LLM are checked against it in plain Python. The model's `owns_list`
and its own reading of the prompt are never relied on. No network calls.
"""

import logging
from dataclasses import dataclass

from .models import Fragrance, Recommendation
from .slugs import core_name, is_unknown_house, same_fragrance, slugify_house

logger = logging.getLogger(__name__)

REASON_OWNED = "owned"
REASON_COLLECTION = "in_collection"
REASON_PREVIOUSLY_RECOMMENDED = "previously_recommended"
REASON_DUPLICATE_IN_BATCH = "duplicate_in_batch"


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


def fragrance_label(name: str, house: str) -> str:
    return f"{name} by {house}" if not is_unknown_house(house) else name


def build_known_fragrances(user_id: int) -> list[KnownFragrance]:
    known: list[KnownFragrance] = []
    for frag in Fragrance.objects.filter(user_id=user_id).order_by("id"):
        reason = REASON_OWNED if frag.status == "own" else REASON_COLLECTION
        known.append(
            KnownFragrance(
                name=frag.name, house=frag.house, reason=reason, ref=f"fragrance:{frag.id}"
            )
        )
    for rec in Recommendation.objects.filter(user_id=user_id).order_by("id"):
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
            core_name(name),
            slugify_house(house) or "-",
            rejection.reason,
            rejection.ref,
            rejection.wildcard,
        )


def describe_rejections(rejections: list[Rejection]) -> str:
    return "; ".join(
        f"{fragrance_label(r.candidate['name'], r.candidate.get('house', ''))} ({r.reason})"
        for r in rejections
    )
