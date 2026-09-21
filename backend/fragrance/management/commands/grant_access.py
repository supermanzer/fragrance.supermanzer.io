"""
Explicit operator grant of complimentary access. Never runs automatically.

Exists because access is otherwise granted only by payment: an account that
predates the subscription gate (and is not staff) would become read-only the
moment enforcement ships. The write itself lives in billing.py.
"""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from fragrance.billing import ComplimentaryGrantError, grant_complimentary_access


class Command(BaseCommand):
    help = "Grant a user complimentary access for N days. --reason is mandatory."

    def add_arguments(self, parser: object) -> None:
        parser.add_argument("--username", required=True, dest="username")
        parser.add_argument("--days", required=True, type=int, dest="days")
        parser.add_argument(
            "--reason",
            required=True,
            dest="reason",
            help="Why access is being granted (logged at WARNING).",
        )

    def handle(self, *args: object, **options: object) -> None:
        user = User.objects.filter(username=options["username"]).first()
        if user is None:
            raise CommandError(f"No such user: {options['username']}")
        try:
            subscription = grant_complimentary_access(
                user=user, days=options["days"], reason=options["reason"]
            )
        except ComplimentaryGrantError as exc:
            raise CommandError(str(exc))
        self.stdout.write(
            self.style.SUCCESS(
                f"Granted {user.username} access until {subscription.access_expires_at.isoformat()}."
            )
        )
