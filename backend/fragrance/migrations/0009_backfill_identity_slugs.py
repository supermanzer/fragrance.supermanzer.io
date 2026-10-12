"""
Data migration: populate name_slug / house_slug on existing rows.

Idempotent (slugs are recomputed deterministically from name/house on every
run, so migration 0010 re-runs this same function) and batched.

Why this is its own migration, separate from the schema steps around it:

- 0008 (ADD COLUMN) takes ACCESS EXCLUSIVE on both tables until its
  transaction commits. Backfilling in that same transaction would hold that
  lock (blocking all reads and writes) for the whole backfill; run alone,
  the backfill only holds row locks.
- 0010 (constraint swap) must not run until no old code can write. 0008 and
  this migration are safe while old containers serve, so the deploy applies
  them first ("migrate fragrance 0009"), recreates the containers, and only
  then applies 0010, which re-runs this same function as a catch-up pass.
  (The rows UPDATEd here queue no deferred FK trigger events, so a slug-only
  backfill followed by ALTER TABLE in one transaction works -- verified on
  Postgres, including rows with source_recommendation set. An INSERT would
  not.)

Collision handling: the old uniqueness was (user, name). The new one is
(user, name_slug, house_slug), so two rows of one user that differ only by
case/accents/punctuation of name (e.g. "Ombre Leather" and "Ombré Leather",
same house) now collide. Rows are processed in (user_id, id) order; the first
keeps the plain slug and every later colliding row gets "<slug>-dup<id>",
retried as "<slug>-dup<id>-<n>" until the key is unused (a plain slug can
equal another row's natural or already assigned dup slug: "Foo", "Foo Dup3",
"foo"). The planning lives in fragrance.slugs.assign_identity_slugs so the
report_repeat_recommendations command predicts exactly this result. A
post-check query aborts the migration (rolling it back) if any identity is
still duplicated, instead of letting 0010 fail on the unique index.
The matching code recomputes slugs from name/house, so a disambiguated stored
slug does not change dedup behaviour. Every disambiguation is logged with ids
and slugs; capture the output (`migrate 2>&1 | tee`), one-shot containers
discard it.
"""

import logging

from django.db import migrations
from django.db.models import Count

from fragrance.slugs import assign_identity_slugs, slugify_house, slugify_name

logger = logging.getLogger(__name__)
BATCH_SIZE = 500


def _flush(model, rows: list) -> None:
    if rows:
        model.objects.bulk_update(rows, ["name_slug", "house_slug"], batch_size=BATCH_SIZE)
        rows.clear()


def backfill_identity_slugs(apps, schema_editor) -> None:
    Fragrance = apps.get_model("fragrance", "Fragrance")
    Recommendation = apps.get_model("fragrance", "Recommendation")

    pending: list = []
    for rec in Recommendation.objects.only("id", "name", "house", "name_slug", "house_slug").iterator(
        chunk_size=BATCH_SIZE
    ):
        name_slug = slugify_name(rec.name)
        house_slug = slugify_house(rec.house)
        if rec.name_slug != name_slug or rec.house_slug != house_slug:
            rec.name_slug = name_slug
            rec.house_slug = house_slug
            pending.append(rec)
        if len(pending) >= BATCH_SIZE:
            _flush(Recommendation, pending)
    _flush(Recommendation, pending)

    frags = list(
        Fragrance.objects.only("id", "user_id", "name", "house", "name_slug", "house_slug").order_by(
            "user_id", "id"
        )
    )
    assigned, disambiguated = assign_identity_slugs(
        rows=[(f.id, f.user_id, f.name, f.house) for f in frags]
    )
    for row_id, user_id, natural, stored, needed_counter in disambiguated:
        logger.warning(
            "backfill_identity_slugs: fragrance id=%s user_id=%s slug collision on "
            "name_slug=%r; stored name_slug=%r%s",
            row_id, user_id, natural, stored, " (dup slug was taken, counter used)" if needed_counter else "",
        )
    for frag in frags:
        name_slug, house_slug = assigned[frag.id]
        if frag.name_slug != name_slug or frag.house_slug != house_slug:
            frag.name_slug = name_slug
            frag.house_slug = house_slug
            pending.append(frag)
        if len(pending) >= BATCH_SIZE:
            _flush(Fragrance, pending)
    _flush(Fragrance, pending)

    if disambiguated:
        logger.warning(
            "backfill_identity_slugs: disambiguated %s colliding fragrance row(s)", len(disambiguated)
        )

    still_duplicated = list(
        Fragrance.objects.values("user_id", "name_slug", "house_slug")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .values_list("user_id", "name_slug", "house_slug")[:10]
    )
    if still_duplicated:
        raise RuntimeError(
            "backfill_identity_slugs: identities still duplicated after disambiguation "
            f"(user_id, name_slug, house_slug): {still_duplicated}"
        )


class Migration(migrations.Migration):

    dependencies = [
        ("fragrance", "0008_add_identity_slugs"),
    ]

    operations = [
        migrations.RunPython(backfill_identity_slugs, migrations.RunPython.noop),
    ]
