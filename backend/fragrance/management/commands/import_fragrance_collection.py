import csv

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fragrance.models import Fragrance

VALID_STATUSES = {"own", "like", "dislike"}
REQUIRED_COLUMNS = {"fragrance", "status", "house", "notes"}


class Command(BaseCommand):
    help = "Seed the Fragrance table from a Google Sheet CSV export (one-time migration)."

    def add_arguments(self, parser):
        parser.add_argument("--user-id", type=int, required=True, help="PK of the target User")
        parser.add_argument("--csv", required=True, dest="csv_path", help="Path to the CSV file")

    @transaction.atomic
    def handle(self, *args, **options):
        user_id = options["user_id"]
        csv_path = options["csv_path"]

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            raise CommandError(f"User with id={user_id} does not exist.")

        try:
            csv_file = open(csv_path, newline="", encoding="utf-8")
        except FileNotFoundError:
            raise CommandError(f"CSV file not found: {csv_path}")

        with csv_file:
            reader = csv.DictReader(csv_file)

            if not reader.fieldnames:
                raise CommandError("CSV file is empty or has no headers.")

            missing = REQUIRED_COLUMNS - set(reader.fieldnames)
            if missing:
                raise CommandError(
                    f"CSV is missing required columns: {', '.join(sorted(missing))}. "
                    f"Found: {', '.join(reader.fieldnames)}"
                )

            created = updated = skipped = 0

            for i, row in enumerate(reader, start=2):  # start=2: row 1 is headers
                name = row["fragrance"].strip()
                status = row["status"].strip().lower()
                house = row.get("house", "").strip()
                notes = row.get("notes", "").strip()

                if not name:
                    self.stdout.write(self.style.WARNING(f"Row {i}: empty name — skipped."))
                    skipped += 1
                    continue

                if status not in VALID_STATUSES:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Row {i}: '{name}' has unknown status '{status}' — skipped."
                        )
                    )
                    skipped += 1
                    continue

                _, was_created = Fragrance.objects.update_or_create(
                    user=user,
                    name=name,
                    defaults={
                        "house": house,
                        "status": status,
                        "notes": notes,
                    },
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Created: {created}  Updated: {updated}  Skipped: {skipped}"
            )
        )
