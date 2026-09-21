"""
Idempotently creates/updates the two beta Plan rows (beta-monthly,
beta-annual). Stripe price ids are taken as command arguments rather than
hardcoded -- they differ between Stripe test and live mode, and the operator
owns those values, not this codebase.

quota_period_days/max_runs_per_quota_period are identical on both plans
(30 days, 1 run) regardless of billing_interval -- see Plan's docstring in
models.py for why that separation matters.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError

from fragrance.models import Plan

_MONTHLY_SLUG = "beta-monthly"
_ANNUAL_SLUG = "beta-annual"


class Command(BaseCommand):
    help = "Create/update the beta-monthly and beta-annual Plan rows. Idempotent."

    def add_arguments(self, parser: object) -> None:
        parser.add_argument(
            "--monthly-price-id",
            required=True,
            dest="monthly_price_id",
            help="Stripe Price id for the $4.00/month beta plan",
        )
        parser.add_argument(
            "--annual-price-id",
            required=True,
            dest="annual_price_id",
            help="Stripe Price id for the $44.00/year beta plan",
        )

    def handle(self, *args: object, **options: object) -> None:
        monthly_price_id = options["monthly_price_id"]
        annual_price_id = options["annual_price_id"]

        if monthly_price_id == annual_price_id:
            raise CommandError(
                "--monthly-price-id and --annual-price-id must be different Stripe Price ids."
            )

        try:
            monthly, monthly_created = Plan.objects.update_or_create(
                slug=_MONTHLY_SLUG,
                defaults={
                    "name": "Beta Monthly",
                    "stripe_price_id": monthly_price_id,
                    "amount_cents": 400,
                    "currency": "usd",
                    "billing_interval": "month",
                    "access_days_granted": 30,
                    "quota_period_days": 30,
                    "max_runs_per_quota_period": 1,
                    "failed_run_grace": 1,
                    "is_active": True,
                },
            )
            annual, annual_created = Plan.objects.update_or_create(
                slug=_ANNUAL_SLUG,
                defaults={
                    "name": "Beta Annual",
                    "stripe_price_id": annual_price_id,
                    "amount_cents": 4400,
                    "currency": "usd",
                    "billing_interval": "year",
                    "access_days_granted": 365,
                    "quota_period_days": 30,
                    "max_runs_per_quota_period": 1,
                    "failed_run_grace": 1,
                    "is_active": True,
                },
            )
        except IntegrityError as exc:
            raise CommandError(
                f"Could not seed plans -- a Stripe price id is already used by a "
                f"different plan slug: {exc}"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"{'Created' if monthly_created else 'Updated'} {monthly}. "
                f"{'Created' if annual_created else 'Updated'} {annual}."
            )
        )
