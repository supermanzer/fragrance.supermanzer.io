"""
Spread existing users' scheduled runs across the month.

Every FragranceConfig created before schedule jitter sits on the model
defaults (day 1, hour 9), so all their runs land in one slot. This moves only
rows still on those untouched defaults to services.default_schedule_for's
deterministic slot. Dry-run unless --apply is given; anything customised is
left alone.

Saves go through FragranceConfig.save, so the post_save signal upserts each
user's PeriodicTask.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from fragrance.models import FragranceConfig
from fragrance.services import default_schedule_for


_UNTOUCHED_DAY = 1


_UNTOUCHED_HOUR = 9


class Command(BaseCommand):
    help = "Move monthly FragranceConfig rows still on the default slot (day 1, hour 9) to their jittered slot. Dry-run unless --apply."

    def add_arguments(self, parser: object) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            dest="apply",
            help="Write the changes. Without it, only print what would change.",
        )

    def handle(self, *args: object, **options: object) -> None:
        apply: bool = options["apply"]
        changed = 0
        skipped = 0
        for config in FragranceConfig.objects.select_related("user").order_by("user_id"):
            untouched = (
                config.frequency == "monthly"
                and config.run_day_of_month == _UNTOUCHED_DAY
                and config.run_hour == _UNTOUCHED_HOUR
            )
            if not untouched:
                skipped += 1
                continue
            day, hour = default_schedule_for(user_id=config.user_id)
            if (day, hour) == (config.run_day_of_month, config.run_hour):
                skipped += 1
                continue
            self.stdout.write(
                f"{config.user.username} (id={config.user_id}): "
                f"day {config.run_day_of_month} hour {config.run_hour} -> day {day} hour {hour}"
            )
            changed += 1
            if apply:
                with transaction.atomic():
                    config.run_day_of_month = day
                    config.run_hour = hour
                    config.save(update_fields=["run_day_of_month", "run_hour"])
        verb = "Updated" if apply else "Would update"
        self.stdout.write(f"{verb} {changed} config(s); left {skipped} untouched.")
        if not apply:
            self.stdout.write("Dry run -- re-run with --apply to write.")
