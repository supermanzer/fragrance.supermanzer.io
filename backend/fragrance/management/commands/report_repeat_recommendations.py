"""
Read-only report of repeat groups: fragrances a user has been recommended more
than once, or recommended while already in their collection, judged by the
same slug rules the pipeline now enforces (fragrance/slugs.py).

Also lists Fragrance rows of one user that share an identical
(name_slug, house_slug): the rows the backfill migration will disambiguate.
It then runs the backfill's own planning step (slugs.assign_identity_slugs)
and reports rows whose first "-dup<id>" slug was itself taken (the "Foo",
"Foo Dup3", "foo" case, resolved with a counter) and any identity that would
STILL collide after disambiguation (`post_disambiguation_clashes`, expected 0;
non-zero means 0009/0010 would abort).

Output is ids and slugs only (user ids, row ids, name/house slugs, counts) --
no emails, usernames or free text. Performs no writes. Run it before and after
deploying the dedup change to compare:

    docker compose exec backend uv run python manage.py report_repeat_recommendations
    docker compose exec backend uv run python manage.py report_repeat_recommendations --user-id 3

Production images have no uv: use /app/.venv/bin/python manage.py ... . It
selects only id/user/name/house/status columns, so it also runs with the NEW
image before the slug migrations are applied.
"""

from collections import defaultdict
from typing import Any

from django.core.management.base import BaseCommand

from fragrance.models import Fragrance, Recommendation
from fragrance.slugs import (
    assign_identity_slugs,
    core_name,
    houses_compatible,
    is_unknown_house,
    slugify_house,
    slugify_name,
)


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

        # Fragrance rows of one user whose (name_slug, house_slug) identity is
        # equal: rows the 0009 backfill disambiguates (stored "-dup<id>") and
        # the ones migration 0010's unique constraint would otherwise reject.
        identities: dict[tuple[int, str, str], list[int]] = defaultdict(list)
        plan_rows: list[tuple[int, int, str, str]] = []
        for frag in frags.order_by("id").values("id", "user_id", "name", "house").iterator():
            key = (frag["user_id"], slugify_name(frag["name"]), slugify_house(frag["house"]))
            identities[key].append(frag["id"])
            plan_rows.append((frag["id"], frag["user_id"], frag["name"], frag["house"]))
        collisions = 0
        for (user_id, name_slug, house_slug), ids in sorted(identities.items()):
            if len(ids) > 1:
                collisions += 1
                self.stdout.write(
                    f"fragrance_identity_collision user_id={user_id} name_slug={name_slug} "
                    f"house_slug={house_slug or '-'} fragrance_ids={ids}"
                )
        self.stdout.write(f"fragrance_identity_collisions={collisions}")

        assigned, disambiguated = assign_identity_slugs(rows=plan_rows)
        for row_id, user_id, natural, stored, needed_counter in disambiguated:
            if needed_counter:
                self.stdout.write(
                    f"dup_slug_taken user_id={user_id} fragrance_id={row_id} "
                    f"name_slug={natural} stored_name_slug={stored}"
                )
        owners = {row_id: user_id for row_id, user_id, _, _ in plan_rows}
        final_keys: dict[tuple[int, str, str], int] = defaultdict(int)
        for row_id, (name_slug, house_slug) in assigned.items():
            final_keys[(owners[row_id], name_slug, house_slug)] += 1
        clashes = sum(1 for count in final_keys.values() if count > 1)
        self.stdout.write(f"post_disambiguation_clashes={clashes}")
