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
from collections.abc import Iterable

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
INVALID_NAME_SLUGS: frozenset[str] = frozenset({"", "unknown"})

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


def has_identity(name: str) -> bool:
    """
    False for names that slugify to nothing (symbols/emoji only). Such a name
    never matches anything in same_fragrance (an empty core is not an identity)
    and is rejected when written (serializer, CSV import, pipeline candidates).
    """
    return bool(core_name(name))


def is_invalid_name(name: str) -> bool:
    """Blank, symbol-only, or an '<UNKNOWN>'/'unknown' placeholder name."""
    return slugify_name(name) in INVALID_NAME_SLUGS


def disambiguated_name_slug(name_slug: str, pk: int, attempt: int = 1) -> str:
    """
    Stored slug for a row the backfill had to disambiguate (see migration
    0009): '<slug>-dup<pk>', or '<slug>-dup<pk>-<attempt>' (attempt >= 2) when
    that slug was itself already taken by another row of the same user/house.
    Always <= SLUG_MAX_LENGTH: the base is truncated, never the suffix.
    """
    suffix = f"-dup{pk}" if attempt <= 1 else f"-dup{pk}-{attempt}"
    return name_slug[: SLUG_MAX_LENGTH - len(suffix)] + suffix


def is_disambiguated_slug(stored_slug: str, name_slug: str, pk: int) -> bool:
    """True when `stored_slug` is a disambiguated_name_slug() form of `name_slug` for row `pk`."""
    match = re.search(rf"-dup{pk}(?:-(\d+))?$", stored_slug)
    if match is None:
        return False
    attempt = int(match.group(1)) if match.group(1) else 1
    return stored_slug == disambiguated_name_slug(name_slug=name_slug, pk=pk, attempt=attempt)


def assign_identity_slugs(
    rows: Iterable[tuple[int, int, str, str]],
) -> tuple[dict[int, tuple[str, str]], list[tuple[int, int, str, str, bool]]]:
    """
    Pure planning step shared by migrations 0009/0010 and the report command.

    `rows` are (id, user_id, name, house). Rows are processed in
    (user_id, id) order; the first row of an identity keeps the plain slug and
    every later one gets a disambiguated slug, retried with a counter until
    its (user_id, name_slug, house_slug) key is unused -- a plain slug can
    itself equal another row's natural or already-assigned '-dup<id>' slug
    ("Foo", "Foo Dup3", "foo").

    Returns ({id: (name_slug, house_slug)},
             [(id, user_id, natural_slug, stored_slug, needed_counter)]).
    """
    assigned: dict[int, tuple[str, str]] = {}
    disambiguated: list[tuple[int, int, str, str, bool]] = []
    seen: set[tuple[int, str, str]] = set()
    for row_id, user_id, name, house in sorted(rows, key=lambda r: (r[1], r[0])):
        natural = slugify_name(name)
        house_slug = slugify_house(house)
        stored = natural
        needed_counter = False
        if (user_id, stored, house_slug) in seen:
            attempt = 1
            stored = disambiguated_name_slug(name_slug=natural, pk=row_id, attempt=attempt)
            while (user_id, stored, house_slug) in seen:
                attempt += 1
                needed_counter = True
                stored = disambiguated_name_slug(name_slug=natural, pk=row_id, attempt=attempt)
            disambiguated.append((row_id, user_id, natural, stored, needed_counter))
        seen.add((user_id, stored, house_slug))
        assigned[row_id] = (stored, house_slug)
    return assigned, disambiguated


def name_in_text(name: str, text: str) -> bool:
    """
    Normalized form of the verify_candidates title check. Matches when either

    - the candidate's full name slug (concentration words kept) appears in the
      title's slug on token boundaries -- the original `.lower() in` check,
      accent- and punctuation-folded; or
    - its core name (concentration words dropped) appears in the title's core
      form, again on token boundaries.

    Boundaries on both branches so that a short name such as "Y" cannot match
    inside unrelated words or titles ("Yves Saint Laurent Libre").
    """
    full_name = slugify_name(name)
    if not full_name:
        return False
    if f"-{full_name}-" in f"-{slugify_name(text)}-":
        return True
    needle = core_name(name)
    title_tokens = _tokens(text)
    haystack = _join(_strip_concentration(title_tokens) or title_tokens)
    return f"-{needle}-" in f"-{haystack}-"
