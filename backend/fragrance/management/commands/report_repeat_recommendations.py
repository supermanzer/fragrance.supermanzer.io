"""
Read-only report of repeat groups: fragrances a user has been recommended more
than once, or recommended while already in their collection, judged by the
same slug rules the pipeline now enforces (fragrance/slugs.py).

Output is ids and slugs only (user ids, row ids, name/house slugs, counts) --
no emails, usernames or free text. Performs no writes. Run it before and after
deploying the dedup change to compare:

    docker compose exec backend uv run python manage.py report_repeat_recommendations
    docker compose exec backend uv run python manage.py report_repeat_recommendations --user-id 3
"""

from collections import defaultdict
from typing import Any

from django.core.management.base import BaseCommand

from fragrance.models import Fragrance, Recommendation
from fragrance.slugs import core_name, houses_compatible, is_unknown_house, slugify_house


def _cluster(rows: list[dict]) -> list[list[dict]]:
    """
    Cluster same-core-name rows by known-house compatibility. Unknown-house
    rows are attached to every cluster (wildcard) rather than used to chain
    clusters together, so Vetiver/guerlain and Vetiver/tom-ford stay apart.
    """
    known = [r for r in rows if not is_unknown_house(r["house"])]
    unknown = [r for r in rows if is_unknown_house(r["house"])]
    clusters: list[list[dict]] = []
    for row in known:
        placed = [c for c in clusters if any(houses_compatible(row["house"], m["house"]) for m in c)]
        if not placed:
            clusters.append([row])
            continue
        placed[0].append(row)
        for extra in placed[1:]:
            placed[0].extend(extra)
            clusters.remove(extra)
    if not clusters:
        return [unknown] if unknown else []
    return [[*c, *unknown] for c in clusters]


class Command(BaseCommand):
    help = "Report repeated recommendations per user (ids and slugs only; read-only)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--user-id", type=int, default=None)

    def handle(self, *args: Any, **options: Any) -> None:
        recs = Recommendation.objects.all()
        frags = Fragrance.objects.all()
        if options["user_id"] is not None:
            recs = recs.filter(user_id=options["user_id"])
            frags = frags.filter(user_id=options["user_id"])

        by_user: dict[int, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
        for rec in recs.order_by("id").values("id", "user_id", "run_id", "name", "house", "status"):
            by_user[rec["user_id"]][core_name(rec["name"])].append({**rec, "kind": "rec"})
        for frag in frags.order_by("id").values("id", "user_id", "name", "house", "status", "source_recommendation_id"):
            by_user[frag["user_id"]][core_name(frag["name"])].append({**frag, "kind": "frag"})

        groups = 0
        for user_id in sorted(by_user):
            for key in sorted(by_user[user_id]):
                for cluster in _cluster(by_user[user_id][key]):
                    rec_ids = [r["id"] for r in cluster if r["kind"] == "rec"]
                    # A Fragrance promoted from one of these picks (rated by the
                    # user) is that same pick, not a second occurrence.
                    frag_ids = [
                        r["id"]
                        for r in cluster
                        if r["kind"] == "frag" and r["source_recommendation_id"] not in rec_ids
                    ]
                    if len(rec_ids) + len(frag_ids) < 2 or not rec_ids:
                        continue
                    groups += 1
                    houses = sorted({slugify_house(r["house"]) or "-" for r in cluster})
                    run_ids = sorted({r["run_id"] for r in cluster if r["kind"] == "rec"})
                    self.stdout.write(
                        f"user_id={user_id} name_slug={key} house_slugs={','.join(houses)} "
                        f"recommendation_ids={rec_ids} run_ids={run_ids} "
                        f"fragrance_ids={frag_ids} count={len(rec_ids) + len(frag_ids)}"
                    )
        self.stdout.write(f"repeat_groups={groups}")
