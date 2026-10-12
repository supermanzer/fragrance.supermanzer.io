"""
fragrance/search.py

Implements fragrance web search functionality. All SearXNG calls read from
settings.SEARXNG_URL — the URL is shared infrastructure, not per-user config.
"""

from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings

from .ai_providers.base import StructuredLLMProvider
from .models import PreferenceProfile, Recommendation
from .slugs import name_in_text


def searxng_search(query: str, num_results: int = 5) -> list[dict]:
    resp = requests.get(
        url=f"{settings.SEARXNG_URL}/search",
        params={"q": query, "format": "json", "categories": "general"},
        headers={"X-Real-IP": "127.0.0.1"},
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])[:num_results]
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
        }
        for r in results
    ]


def _format_results(label: str, results: list[dict]) -> str:
    lines = "\n".join(
        f"- {r['title']}: {r['content']} ({r['url']})" for r in results
    )
    return f"{label}:\n{lines}"


def run_discovery_searches(user_id: int, profile_id: int) -> str:
    """Pipeline step 2. Fires both search angles and returns combined result text."""
    profile = PreferenceProfile.objects.get(id=profile_id)
    results_1 = searxng_search(query=profile.search_angle_1)
    results_2 = searxng_search(query=profile.search_angle_2)
    return "\n\n".join(
        [
            _format_results(
                label=f"Search 1: {profile.search_angle_1}", results=results_1
            ),
            _format_results(
                label=f"Search 2: {profile.search_angle_2}", results=results_2
            ),
        ]
    )


def verify_candidates(
    user_id: int,
    run_id: int,
    candidates: list[dict],
    profile: PreferenceProfile,
    provider: StructuredLLMProvider,
) -> list[dict]:
    """
    Pipeline step 4. Fires one search per candidate, up to 2 in parallel
    (ThreadPoolExecutor(max_workers=2) below — deliberately reduced from 5
    per commit 0bb5bfd to lessen memory pressure on the droplet's 1 vCPU/2GB;
    do not raise this back to 5).

    For each candidate:
    - Confirmed (name appears in a result title): create a Recommendation with
      status='confirmed' and the matching URL as search_source_url.
    - Unconfirmed: create a Recommendation with status='replaced' for the original,
      then call LLM #3 (select_replacement) to pick an alternative from the same
      search results and create a second Recommendation with status='confirmed'.
      The replacement is dedup-checked inside select_replacement against the DB
      history/collection and every other pick in this run.

    The title check compares the candidate's normalized core name (accents,
    punctuation and concentration words folded) with the normalized title; it
    does not check the house.

    Returns the flat list of all picks (both confirmed and replaced) so
    generate_email_content knows which picks to write rationale for.
    """
    # Lazy import breaks the potential circular dependency:
    # search.py imports llm.py; llm.py imports models.py (not search.py).
    from .llm import select_replacement

    # Keyed by position, not name: two same-named candidates from different
    # houses are legitimate and must not share (or overwrite) one future.
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                searxng_search,
                query=f"{c['name']} fragrance review site:fragrantica.com OR site:basenotes.net",
            )
            for c in candidates
        ]

    all_picks = []
    replacements: list[dict] = []
    for index, candidate in enumerate(candidates):
        results = futures[index].result()
        results_text = "\n".join(
            f"- {r['title']}: {r['content']} ({r['url']})" for r in results
        )
        matching = next(
            (
                r
                for r in results
                if name_in_text(name=candidate["name"], text=r["title"])
            ),
            None,
        )

        if matching:
            Recommendation.objects.create(
                user_id=user_id,
                run_id=run_id,
                name=candidate["name"],
                house=candidate["house"],
                status="confirmed",
                rationale="",
                search_source_url=matching["url"],
            )
            all_picks.append(
                {
                    **candidate,
                    "status": "confirmed",
                    "search_source_url": matching["url"],
                }
            )
        else:
            Recommendation.objects.create(
                user_id=user_id,
                run_id=run_id,
                name=candidate["name"],
                house=candidate["house"],
                status="replaced",
                rationale="",
                search_source_url="",
            )
            all_picks.append(
                {**candidate, "status": "replaced", "search_source_url": ""}
            )

            replacement = select_replacement(
                user_id=user_id,
                failed_candidate=candidate,
                search_results=results_text,
                profile=profile,
                provider=provider,
                batch_candidates=[*candidates, *replacements],
            )
            replacements.append(replacement)
            Recommendation.objects.create(
                user_id=user_id,
                run_id=run_id,
                name=replacement["name"],
                house=replacement["house"],
                status="confirmed",
                rationale="",
                search_source_url="",
            )
            all_picks.append(
                {**replacement, "status": "confirmed", "search_source_url": ""}
            )

    return all_picks
