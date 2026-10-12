"""
fragrance/slugs.py

Pure (no DB, no network) identity helpers for fragrances. The recommendation
pipeline compares LLM-produced (name, house) pairs against the user's history
and collection with these functions; nothing here trusts model output to have
done the comparison.

Two name forms exist on purpose:

- `slugify_name` keeps concentration words ("... Eau de Parfum"). It is what is
  persisted as `name_slug` and what the Fragrance uniqueness constraint keys
  on, so a user can own both the EDT and the EDP of one scent.
- `core_name` additionally strips pure concentration/format words. It is used
  only to decide "is this the same fragrance", where "Ombre Leather Parfum"
  and "Ombré Leather" must collide.
"""

import re
import unicodedata

CONCENTRATION_PHRASES: tuple[tuple[str, ...], ...] = (
    ("extrait", "de", "parfum"),
    ("eau", "de", "parfum"),
    ("eau", "de", "toilette"),
)
CONCENTRATION_WORDS: frozenset[str] = frozenset(
    {"parfum", "perfume", "edp", "edt", "extrait"}
)
UNKNOWN_HOUSE_SLUGS: frozenset[str] = frozenset({"", "unknown"})
SLUG_MAX_LENGTH = 300

_NON_WORD = re.compile(r"[^\w\s]+", re.UNICODE)
_APOSTROPHES = re.compile(r"['‘’ʼ`´]")


def _tokens(text: str) -> list[str]:
    """Accent-folded, casefolded word tokens; & -> and; apostrophes dropped."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("&", " and ")
    text = _APOSTROPHES.sub("", text)
    text = text.replace("-", " ").replace("_", " ")
    text = _NON_WORD.sub(" ", text)
    return text.split()


def _strip_concentration(tokens: list[str]) -> list[str]:
    kept: list[str] = []
    i = 0
    while i < len(tokens):
        for phrase in CONCENTRATION_PHRASES:
            if tuple(tokens[i : i + len(phrase)]) == phrase:
                i += len(phrase)
                break
        else:
            if tokens[i] not in CONCENTRATION_WORDS:
                kept.append(tokens[i])
            i += 1
    return kept


def _join(tokens: list[str]) -> str:
    return "-".join(tokens)[:SLUG_MAX_LENGTH]


def slugify_name(name: str) -> str:
    """Full normalized name slug; concentration words are retained."""
    return _join(_tokens(name))


def core_name(name: str) -> str:
    """Name slug with concentration/format words removed (matching form)."""
    tokens = _tokens(name)
    stripped = _strip_concentration(tokens)
    return _join(stripped or tokens)


def slugify_house(house: str) -> str:
    """House slug; blank / '<UNKNOWN>' / 'unknown' all normalize to ''."""
    slug = _join(_tokens(house))
    return "" if slug in UNKNOWN_HOUSE_SLUGS else slug


def is_unknown_house(house: str) -> bool:
    return slugify_house(house) == ""


def combined_slug(name: str, house: str) -> str:
    """e.g. 'leather-oud-dior'; built from the core name; house omitted if unknown."""
    house_slug = slugify_house(house)
    core = core_name(name)
    return f"{core}-{house_slug}" if house_slug else core


def houses_compatible(house_a: str, house_b: str) -> bool:
    """
    Equal slugs, or one token set a subset of the other (dior < christian-dior),
    or either side unknown (wildcard: same name then counts as the same fragrance).
    """
    slug_a = slugify_house(house_a)
    slug_b = slugify_house(house_b)
    if not slug_a or not slug_b or slug_a == slug_b:
        return True
    tokens_a = set(slug_a.split("-"))
    tokens_b = set(slug_b.split("-"))
    return tokens_a <= tokens_b or tokens_b <= tokens_a


def same_fragrance(name_a: str, house_a: str, name_b: str, house_b: str) -> bool:
    core_a = core_name(name_a)
    return bool(core_a) and core_a == core_name(name_b) and houses_compatible(house_a, house_b)


def name_in_text(name: str, text: str) -> bool:
    """
    Normalized form of the verify_candidates title check: the candidate's core
    name slug appears (as a substring, like the original `.lower() in`) within
    the text's slug with concentration words removed.
    """
    needle = core_name(name)
    if not needle:
        return False
    haystack = _join(_strip_concentration(_tokens(text)) or _tokens(text))
    return needle in haystack
