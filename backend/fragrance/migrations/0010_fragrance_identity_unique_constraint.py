"""
Swap Fragrance uniqueness from (user, name) to (user, name_slug, house_slug).

DEPLOY ORDER (the three steps are not interchangeable; see report/commit):

  1. migrate fragrance 0009   -- adds the slug columns (0008, db_default '' so
                                 old code can still INSERT) and backfills (0009).
                                 Old containers keep serving.
  2. recreate backend + celery-worker + celery-beat with the new image.
  3. plain `migrate`          -- applies THIS migration, immediately after (2).

Between (1) and (3) old code can insert rows whose slugs are '' (the DB
default) and, between (2) and (3), new code still runs against the old
unique(user, name). So this migration starts with the same idempotent
backfill as 0009: those rows get real slugs, and any that collide are
disambiguated, BEFORE the constraint is created. The backfill only UPDATEs
slug columns, which queues no deferred FK trigger events, so it can share a
transaction with the ALTER TABLEs below; it runs first, so the ACCESS
EXCLUSIVE lock the ALTERs take is held only for the constraint swap, not for
the backfill. 0009 stays a separate migration because it must be applied
BEFORE the container swap while this one must wait until after it.

Rollback after release is a restore from the pre-deploy dump, not `migrate`
backwards: reversing fails once a user has one name under two houses.
"""

from importlib import import_module

from django.conf import settings
from django.db import migrations, models

backfill_identity_slugs = import_module(
    "fragrance.migrations.0009_backfill_identity_slugs"
).backfill_identity_slugs


class Migration(migrations.Migration):

    dependencies = [
        ('fragrance', '0009_backfill_identity_slugs'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(backfill_identity_slugs, migrations.RunPython.noop),
        migrations.AlterUniqueTogether(
            name='fragrance',
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name='fragrance',
            constraint=models.UniqueConstraint(fields=('user', 'name_slug', 'house_slug'), name='unique_fragrance_identity_per_user'),
        ),
    ]
