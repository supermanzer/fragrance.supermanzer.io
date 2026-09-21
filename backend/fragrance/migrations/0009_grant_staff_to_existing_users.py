# Generated skeleton by Django 6.0.6; RunPython filled in by hand (data migration).

from datetime import datetime, timezone

from django.conf import settings
from django.db import migrations

# Accounts created at or after this instant are never grandfathered. Open
# registration was still live in production until this shipped, so an
# unbounded promotion would hand free, unlimited, admin-capable access to
# anyone who registered before the deploy. Production's pre-existing accounts
# were created 2026-06-21.
GRANDFATHER_CUTOFF = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)


def grant_staff_to_existing_users(apps, schema_editor) -> None:
    """
    is_staff is the exemption flag for the subscription gate (permission
    classes and run quota). This grandfathers users who joined before
    GRANDFATHER_CUTOFF (2026-09-20 00:00 UTC), so nobody who existed when the
    gate was built loses access at deploy. Users who joined at or after the
    cutoff -- including anyone who registered through the then-still-open
    endpoint -- are NOT staff and go through the paywall.

    Reverse is a deliberate no-op: undoing it would require knowing which rows
    this changed (versus rows that were already staff), and that is not
    recorded. Reversing must not strip legitimate staff.
    """
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(app_label, model_name)
    User.objects.filter(
        is_staff=False, date_joined__lt=GRANDFATHER_CUTOFF
    ).update(is_staff=True)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('fragrance', '0008_plan_stripeevent_subscription_processedpayment'),
    ]

    operations = [
        migrations.RunPython(grant_staff_to_existing_users, migrations.RunPython.noop),
    ]
