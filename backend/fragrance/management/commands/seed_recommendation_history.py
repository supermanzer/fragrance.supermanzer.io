import json
from datetime import datetime, timezone

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fragrance.models import Recommendation, RecommendationRun

# Distinguishes the sentinel from real runs so get_or_create is idempotent across re-runs.
SENTINEL_TASK_ID = "pre-migration-sentinel"
SENTINEL_DATE = datetime(2000, 1, 1, tzinfo=timezone.utc)


class Command(BaseCommand):
    help = (
        "Seed past recommendation names from recommended.json into the Recommendation table "
        "(one-time migration — prevents re-recommending picks made on the old Pi cluster)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--user-id", type=int, required=True, help="PK of the target User")
        parser.add_argument(
            "--json", required=True, dest="json_path", help="Path to recommended.json"
        )

    @transaction.atomic
    def handle(self, *args, **options):
        user_id = options["user_id"]
        json_path = options["json_path"]

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            raise CommandError(f"User with id={user_id} does not exist.")

        try:
            with open(json_path, encoding="utf-8") as f:
                names = json.load(f)
        except FileNotFoundError:
            raise CommandError(f"JSON file not found: {json_path}")
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSON in {json_path}: {exc}")

        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise CommandError("Expected a flat JSON array of strings, e.g. [\"Name 1\", \"Name 2\"].")

        sentinel_run, created = RecommendationRun.objects.get_or_create(
            user=user,
            celery_task_id=SENTINEL_TASK_ID,
            defaults={"status": "done"},
        )
        if created:
            # auto_now_add blocks passing triggered_at to create(); update() bypasses save().
            RecommendationRun.objects.filter(pk=sentinel_run.pk).update(triggered_at=SENTINEL_DATE)
            self.stdout.write("Created sentinel RecommendationRun (triggered_at=2000-01-01).")
        else:
            self.stdout.write("Sentinel RecommendationRun already exists — reusing it.")

        seeded = skipped = 0

        for name in names:
            name = name.strip()
            if not name:
                skipped += 1
                continue

            _, was_created = Recommendation.objects.get_or_create(
                user=user,
                run=sentinel_run,
                name=name,
                defaults={
                    "house": "",
                    "status": "confirmed",
                    "rationale": "Pre-migration history.",
                },
            )
            if was_created:
                seeded += 1
            else:
                skipped += 1

        self.stdout.write(
            self.style.SUCCESS(f"Done. Seeded: {seeded}  Already present: {skipped}")
        )
