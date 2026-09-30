"""
Creates or updates the Stripe Customer Portal configuration as code, so the
sandbox and live accounts are reproducible and reviewable rather than hand-set
in the Dashboard (where Stripe would otherwise auto-create a permissive default
that lets customers edit name, address and phone).

Idempotent: our configuration is found by metadata (app=fragrances,
managed_by=configure_stripe_portal) and modified to the desired state on
re-runs. Prints the configuration id; the operator puts it in
STRIPE_PORTAL_CONFIGURATION_ID. Sandbox and live keep separate configurations,
so run it once per environment. A live key requires --yes.

Only *active* configurations are considered (Configuration.list supports an
`active` filter), so deactivating a duplicate in the Dashboard clears the
"more than one tagged" guard, and a deactivated configuration is never reused.

What customers may do: update their payment method, view invoices, update their
email, and cancel at period end with no proration. Everything else is off:
plan switching (our own change-plan endpoint defers it to period end without
prorations; the portal's own switching cannot control the billing anchor and
can create a free period), promotion codes and the hosted login page.
"""

import argparse
import json
from typing import cast

import stripe
from stripe.params.billing_portal import (
    ConfigurationCreateParams,
    ConfigurationUpdateParams,
)
from django.core.management.base import BaseCommand, CommandError

from fragrance import billing
from fragrance.ai_providers.base import sanitize_sdk_error_message


_METADATA = {"app": "fragrances", "managed_by": "configure_stripe_portal"}


class Command(BaseCommand):
    help = "Create/update the Stripe Customer Portal configuration. Idempotent."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            dest="dry_run",
            help="Print the desired configuration and whether it would be created or modified.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            dest="yes",
            help="Required to run against a live-mode key.",
        )

    def handle(self, *args: object, **options: object) -> None:
        from django.conf import settings

        key = settings.STRIPE_SECRET_KEY
        if not key:
            raise CommandError("STRIPE_SECRET_KEY is not set.")
        live = not key.startswith(("sk_test_", "rk_test_"))
        self.stdout.write(f"Stripe mode: {'LIVE' if live else 'test (sandbox)'}")
        if live and not options["yes"] and not options["dry_run"]:
            raise CommandError("Refusing to modify a LIVE Stripe account without --yes.")

        client = billing.get_stripe_client()
        desired = desired_configuration()

        try:
            ours = [
                configuration
                for configuration in client.v1.billing_portal.configurations.list(
                    params={"limit": 100, "active": True}
                ).auto_paging_iter()
                if _is_ours(configuration=configuration)
            ]
        except stripe.StripeError as exc:
            raise CommandError(f"Could not list portal configurations: {sanitize_sdk_error_message(exc)}")
        if len(ours) > 1:
            ids = ", ".join(configuration.id for configuration in ours)
            raise CommandError(
                f"More than one configuration is tagged as ours ({ids}); "
                "deactivate the extras in the Dashboard and re-run."
            )

        action = "modify" if ours else "create"
        self.stdout.write(json.dumps(desired, indent=2, sort_keys=True))
        if options["dry_run"]:
            target = f" {ours[0].id}" if ours else ""
            self.stdout.write(f"DRY RUN: would {action}{target}. Nothing was changed.")
            return

        try:
            if ours:
                configuration = client.v1.billing_portal.configurations.update(
                    ours[0].id, params=desired
                )
            else:
                configuration = client.v1.billing_portal.configurations.create(
                    params=cast(ConfigurationCreateParams, desired)
                )
        except stripe.StripeError as exc:
            raise CommandError(f"Stripe rejected the configuration: {sanitize_sdk_error_message(exc)}")

        self.stdout.write(
            self.style.SUCCESS(
                f"{'Updated' if ours else 'Created'} portal configuration {configuration.id}"
            )
        )
        self.stdout.write(f"Set STRIPE_PORTAL_CONFIGURATION_ID={configuration.id} in backend/.env")


def desired_configuration() -> ConfigurationUpdateParams:
    """
    Returned type is ConfigurationUpdateParams (the superset of the two
    Stripe accepts -- it additionally has "active", which this dict never
    sets). The .create() call site below casts to ConfigurationCreateParams:
    both are all-NotRequired TypedDicts describing the identical wire body,
    but their nested sub-TypedDicts (e.g. *Features) are nominally distinct
    types, so a type checker won't treat one as freely assignable to the
    other even though the JSON shapes are the same -- cast documents that
    deliberately, rather than re-declaring this whole dict twice.
    """
    from django.conf import settings

    frontend_url = settings.FRONTEND_URL.rstrip("/")
    config: ConfigurationUpdateParams = {
        "business_profile": {
            "headline": "Manage your Fragrances subscription",
            "terms_of_service_url": f"{frontend_url}/terms",
            "privacy_policy_url": f"{frontend_url}/privacy",
        },
        "features": {
            "payment_method_update": {"enabled": True},
            "invoice_history": {"enabled": True},
            "customer_update": {"enabled": True, "allowed_updates": ["email"]},
            "subscription_cancel": {
                "enabled": True,
                "mode": "at_period_end",
                "proration_behavior": "none",
                "cancellation_reason": {
                    "enabled": True,
                    "options": [
                        "too_expensive",
                        "missing_features",
                        "unused",
                        "switched_service",
                        "other",
                    ],
                },
            },
            "subscription_update": {"enabled": False},
        },
        "login_page": {"enabled": False},
        "metadata": dict(_METADATA),
    }
    return config


def _is_ours(*, configuration: object) -> bool:
    metadata = getattr(configuration, "metadata", None)
    md = metadata.to_dict() if metadata is not None and hasattr(metadata, "to_dict") else {}
    return all(md.get(key) == value for key, value in _METADATA.items())
