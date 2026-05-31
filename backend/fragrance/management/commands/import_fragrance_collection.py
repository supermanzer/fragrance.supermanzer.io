from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from fragrance.services import import_collection_from_csv


class Command(BaseCommand):
    help = "Seed the Fragrance table from a Google Sheet CSV export (one-time migration)."

    def add_arguments(self, parser):
        parser.add_argument("--user-id", type=int, required=True, help="PK of the target User")
        parser.add_argument("--csv", required=True, dest="csv_path", help="Path to the CSV file")

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
            try:
                result = import_collection_from_csv(user=user, file_obj=csv_file)
            except ValueError as exc:
                raise CommandError(str(exc))

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Created: {result['created']}  "
                f"Updated: {result['updated']}  "
                f"Skipped: {result['skipped']}"
            )
        )
