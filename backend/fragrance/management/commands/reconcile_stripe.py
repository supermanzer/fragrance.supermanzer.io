"""
fragrance/management/commands/reconcile_stripe.py

Manual run of the Stripe reconciliation backstop. --dry-run reports what
would be granted and writes nothing.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError

from ...billing import RECONCILE_INVOICE_LOOKBACK, reconcile_subscriptions


class Command(BaseCommand):
    help = "Re-check Stripe for paid invoices missed by the webhook and re-enqueue stuck events."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
        parser.add_argument(
            "--days",
            type=int,
            default=RECONCILE_INVOICE_LOOKBACK.days,
            help="How many days of paid invoices to examine (manual wider sweep).",
        )

    def handle(self, *args, **options) -> None:
        if options["days"] < 1:
            raise CommandError("--days must be at least 1.")
        summary = reconcile_subscriptions(
            dry_run=options["dry_run"], lookback=timedelta(days=options["days"])
        )
        if summary["skipped"]:
            self.stdout.write(self.style.WARNING("STRIPE_SECRET_KEY is not set; nothing done."))
            return
        for key, value in summary.items():
            self.stdout.write(f"{key}: {value}")
