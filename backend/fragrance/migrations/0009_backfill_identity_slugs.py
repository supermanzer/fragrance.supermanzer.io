"""
Data migration: populate name_slug / house_slug on existing rows.

Idempotent (slugs are recomputed deterministically from name/house on every
run) and batched. Runs on its own, before the constraint swap in 0010: the
Fragrance.source_recommendation FK has deferred constraint triggers, so
UPDATEs followed by ALTER TABLE in one transaction can fail on Postgres with
"pending trigger events".

Collision handling: the old uniqueness was (user, name). The new one is
(user, name_slug, house_slug), so two rows of one user that differ only by
case/accents/punctuation of name (e.g. "Ombre Leather" and "Ombré Leather",
same house) now collide. Rows are processed in id order; the lowest id keeps
the plain slug and every later colliding row gets name_slug
"<slug>-dup<id>" so the 0010 constraint can be created. The matching code
recomputes slugs from name/house, so a disambiguated stored slug does not
change dedup behaviour. Every disambiguation is logged with ids and slugs.
"""

import logging

from django.db import migrations

from fragrance.slugs import slugify_house, slugify_name

logger = logging.getLogger(__name__)
BATCH_SIZE = 500
SLUG_BASE_MAX = 280


def _flush(model, rows: list) -> None:
    if rows:
        model.objects.bulk_update(rows, ["name_slug", "house_slug"])
        rows.clear()


def backfill_identity_slugs(apps, schema_editor) -> None:
    Fragrance = apps.get_model("fragrance", "Fragrance")
    Recommendation = apps.get_model("fragrance", "Recommendation")

    pending: list = []
    for rec in Recommendation.objects.order_by("id").iterator(chunk_size=BATCH_SIZE):
        name_slug = slugify_name(rec.name)
        house_slug = slugify_house(rec.house)
        if rec.name_slug != name_slug or rec.house_slug != house_slug:
            rec.name_slug = name_slug
            rec.house_slug = house_slug
            pending.append(rec)
        if len(pending) >= BATCH_SIZE:
            _flush(Recommendation, pending)
    _flush(Recommendation, pending)

    seen: set[tuple[int, str, str]] = set()
    collisions = 0
    for frag in Fragrance.objects.order_by("user_id", "id").iterator(chunk_size=BATCH_SIZE):
        name_slug = slugify_name(frag.name)
        house_slug = slugify_house(frag.house)
        key = (frag.user_id, name_slug, house_slug)
        if key in seen:
            collisions += 1
            original = name_slug
            name_slug = f"{name_slug[:SLUG_BASE_MAX]}-dup{frag.id}"
            logger.warning(
                "backfill_identity_slugs: fragrance id=%s user_id=%s slug collision on "
                "name_slug=%r house_slug=%r; stored name_slug=%r",
                frag.id, frag.user_id, original, house_slug, name_slug,
            )
            key = (frag.user_id, name_slug, house_slug)
        seen.add(key)
        if frag.name_slug != name_slug or frag.house_slug != house_slug:
            frag.name_slug = name_slug
            frag.house_slug = house_slug
            pending.append(frag)
        if len(pending) >= BATCH_SIZE:
            _flush(Fragrance, pending)
    _flush(Fragrance, pending)

    if collisions:
        logger.warning("backfill_identity_slugs: disambiguated %s colliding fragrance row(s)", collisions)


class Migration(migrations.Migration):

    dependencies = [
        ("fragrance", "0008_add_identity_slugs"),
    ]

    operations = [
        migrations.RunPython(backfill_identity_slugs, migrations.RunPython.noop),
    ]
