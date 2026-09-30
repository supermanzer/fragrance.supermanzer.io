"""
Idempotently creates/updates the two beta Plan rows (beta-monthly,
beta-annual). Stripe price ids are taken as command arguments rather than
hardcoded -- they differ between Stripe test and live mode, and the operator
owns those values, not this codebase.

quota_period_days/max_runs_per_quota_period are identical on both plans
(30 days, 1 run) regardless of billing_interval -- see Plan's docstring in
models.py for why that separation matters.

Price rotation: `is_active` means "offered at checkout / plan-change
target", never "honoured at renewal" (billing._match_plan_line matches any
Plan row, active or not, on purpose -- see its docstring). Rewriting
stripe_price_id on an existing row in place would silently break renewals
for every subscriber still billing on the old Price, with no way for
reconciliation to recover them. So this command refuses that rewrite unless
--rotate-price is given, in which case it retires the existing row (slug
renamed, is_active set False, stripe_price_id left untouched so old renewals
keep matching) and creates a new active row on the canonical slug with the
new price.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone

from fragrance.models import Plan


_ANNUAL_SLUG = "beta-annual"


_MONTHLY_SLUG = "beta-monthly"


_PLAN_FIELDS: dict[str, dict[str, Any]] = {
    _MONTHLY_SLUG: {
        "name": "Beta Monthly",
        "amount_cents": 400,
        "currency": "usd",
        "billing_interval": "month",
        "access_days_granted": 30,
        "quota_period_days": 30,
        "max_runs_per_quota_period": 1,
        "failed_run_grace": 1,
    },
    _ANNUAL_SLUG: {
        "name": "Beta Annual",
        "amount_cents": 4400,
        "currency": "usd",
        "billing_interval": "year",
        "access_days_granted": 365,
        "quota_period_days": 30,
        "max_runs_per_quota_period": 1,
        "failed_run_grace": 1,
    },
}


class Command(BaseCommand):
    help = "Create/update the beta-monthly and beta-annual Plan rows. Idempotent."

    def add_arguments(self, parser: Any) -> None:
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
        parser.add_argument(
            "--rotate-price",
            action="store_true",
            dest="rotate_price",
            default=False,
            help=(
                "Required when a slug already exists with a DIFFERENT stripe_price_id. "
                "Retires the existing row (kept matchable for renewals on the old price, "
                "slug renamed) and creates a new active row on the canonical slug with the "
                "new price. Without this flag, a price change on an existing slug is refused."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        monthly_price_id: str = options["monthly_price_id"]
        annual_price_id: str = options["annual_price_id"]
        rotate_price: bool = options["rotate_price"]

        if monthly_price_id == annual_price_id:
            raise CommandError(
                "--monthly-price-id and --annual-price-id must be different Stripe Price ids."
            )

        try:
            with transaction.atomic():
                monthly, monthly_created, monthly_retired = self._upsert_plan(
                    slug=_MONTHLY_SLUG,
                    stripe_price_id=monthly_price_id,
                    fields=_PLAN_FIELDS[_MONTHLY_SLUG],
                    rotate_price=rotate_price,
                )
                annual, annual_created, annual_retired = self._upsert_plan(
                    slug=_ANNUAL_SLUG,
                    stripe_price_id=annual_price_id,
                    fields=_PLAN_FIELDS[_ANNUAL_SLUG],
                    rotate_price=rotate_price,
                )
        except IntegrityError as exc:
            raise CommandError(
                f"Could not seed plans -- a Stripe price id is already used by a "
                f"different plan slug: {exc}"
            )

        def _verb(*, created: bool, retired: bool) -> str:
            if retired:
                return "Rotated (retired old row, created)"
            return "Created" if created else "Updated"

        self.stdout.write(
            self.style.SUCCESS(
                f"{_verb(created=monthly_created, retired=monthly_retired)} {monthly}. "
                f"{_verb(created=annual_created, retired=annual_retired)} {annual}."
            )
        )

    def _upsert_plan(
        self, *, slug: str, stripe_price_id: str, fields: dict[str, Any], rotate_price: bool
    ) -> tuple[Plan, bool, bool]:
        """Returns (plan, created, retired_previous)."""
        existing = Plan.objects.filter(slug=slug).first()

        if existing is None:
            plan = Plan.objects.create(
                slug=slug, stripe_price_id=stripe_price_id, is_active=True, **fields
            )
            return plan, True, False

        if existing.stripe_price_id == stripe_price_id:
            for field, value in fields.items():
                setattr(existing, field, value)
            existing.is_active = True
            existing.save(update_fields=[*fields.keys(), "is_active"])
            return existing, False, False

        if not rotate_price:
            raise CommandError(
                f"Plan '{slug}' already exists with stripe_price_id={existing.stripe_price_id!r}, "
                f"which differs from the requested {stripe_price_id!r}. Rewriting stripe_price_id "
                f"in place would strand renewals already billing on the old price -- no subscriber "
                f"on it would ever grant again. Re-run with --rotate-price to retire the existing "
                f"row (kept matchable for old renewals, slug renamed) and create a new active row "
                f"on '{slug}' with the new price."
            )

        retired_slug = f"{slug}-retired-{timezone.now():%Y%m%d%H%M%S}"
        existing.slug = retired_slug
        existing.is_active = False
        existing.save(update_fields=["slug", "is_active"])
        self.stdout.write(
            self.style.WARNING(
                f"Retired {existing} (was slug={slug!r}, price={existing.stripe_price_id!r}); "
                f"still matchable for renewals on that price."
            )
        )
        new_plan = Plan.objects.create(
            slug=slug, stripe_price_id=stripe_price_id, is_active=True, **fields
        )
        return new_plan, True, True
