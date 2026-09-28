from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from fragrance.models import BetaSignup, Plan, Subscription
from fragrance.tests.support.stripe_fakes import (
    _STRIPE_TEST_SETTINGS,
    _sdk_invoice,
    _sdk_line,
)
from rest_framework.test import APIClient


class PlanModelTests(TestCase):
    def test_quota_fields_are_independent_of_billing_interval(self) -> None:
        monthly = Plan.objects.create(
            name="Beta Monthly",
            slug="test-monthly",
            stripe_price_id="price_test_monthly",
            amount_cents=400,
            billing_interval="month",
            access_days_granted=30,
        )
        annual = Plan.objects.create(
            name="Beta Annual",
            slug="test-annual",
            stripe_price_id="price_test_annual",
            amount_cents=4400,
            billing_interval="year",
            access_days_granted=365,
        )
        # Usage terms must not follow billing terms -- an annual subscriber
        # gets one run per 30 days, not twelve on the first of January.
        self.assertEqual(monthly.quota_period_days, annual.quota_period_days)
        self.assertEqual(monthly.max_runs_per_quota_period, annual.max_runs_per_quota_period)
        self.assertNotEqual(monthly.access_days_granted, annual.access_days_granted)
        self.assertNotEqual(monthly.billing_interval, annual.billing_interval)


class SubscriptionHasActiveAccessTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="subscriber", email="subscriber@example.com", password="pw12345!"
        )

    def test_null_access_expires_at_is_false_even_when_status_is_active(self) -> None:
        sub = Subscription.objects.create(user=self.user, status="active", access_expires_at=None)
        self.assertFalse(sub.has_active_access)

    def test_active_status_and_future_expiry_is_true(self) -> None:
        sub = Subscription.objects.create(
            user=self.user,
            status="active",
            access_expires_at=timezone.now() + timedelta(days=5),
        )
        self.assertTrue(sub.has_active_access)

    def test_active_status_but_past_expiry_is_false(self) -> None:
        sub = Subscription.objects.create(
            user=self.user,
            status="active",
            access_expires_at=timezone.now() - timedelta(days=1),
        )
        self.assertFalse(sub.has_active_access)

    def test_non_active_status_with_future_expiry_is_false(self) -> None:
        sub = Subscription.objects.create(
            user=self.user,
            status="revoked",
            access_expires_at=timezone.now() + timedelta(days=5),
        )
        self.assertFalse(sub.has_active_access)


class SeedPlansCommandTests(TestCase):
    def test_creates_both_plans(self) -> None:
        out = StringIO()
        call_command(
            "seed_plans",
            "--monthly-price-id=price_mo",
            "--annual-price-id=price_yr",
            stdout=out,
        )
        monthly = Plan.objects.get(slug="beta-monthly")
        annual = Plan.objects.get(slug="beta-annual")
        self.assertEqual(monthly.stripe_price_id, "price_mo")
        self.assertEqual(annual.stripe_price_id, "price_yr")
        self.assertEqual(monthly.billing_interval, "month")
        self.assertEqual(annual.billing_interval, "year")
        self.assertEqual(monthly.access_days_granted, 30)
        self.assertEqual(annual.access_days_granted, 365)
        self.assertEqual(monthly.quota_period_days, 30)
        self.assertEqual(annual.quota_period_days, 30)
        self.assertEqual(monthly.max_runs_per_quota_period, 1)
        self.assertEqual(annual.max_runs_per_quota_period, 1)

    def test_rerun_with_same_price_id_updates_other_fields_in_place(self) -> None:
        call_command("seed_plans", "--monthly-price-id=price_mo", "--annual-price-id=price_yr")
        Plan.objects.filter(slug="beta-monthly").update(name="stale name", amount_cents=1)
        call_command("seed_plans", "--monthly-price-id=price_mo", "--annual-price-id=price_yr")
        self.assertEqual(Plan.objects.filter(slug="beta-monthly").count(), 1)
        monthly = Plan.objects.get(slug="beta-monthly")
        self.assertEqual(monthly.stripe_price_id, "price_mo")
        self.assertEqual(monthly.name, "Beta Monthly")
        self.assertEqual(monthly.amount_cents, 400)

    def test_same_price_id_for_both_plans_raises(self) -> None:
        with self.assertRaises(CommandError):
            call_command("seed_plans", "--monthly-price-id=same", "--annual-price-id=same")

    def test_rerun_with_different_price_without_rotate_flag_refuses(self) -> None:
        call_command("seed_plans", "--monthly-price-id=price_mo", "--annual-price-id=price_yr")
        with self.assertRaises(CommandError):
            call_command("seed_plans", "--monthly-price-id=price_mo2", "--annual-price-id=price_yr")
        # Refused, so nothing was changed: still one row, still on the old price.
        self.assertEqual(Plan.objects.filter(slug="beta-monthly").count(), 1)
        self.assertEqual(Plan.objects.get(slug="beta-monthly").stripe_price_id, "price_mo")

    def test_rotate_price_retires_old_row_and_creates_new_active_one(self) -> None:
        call_command("seed_plans", "--monthly-price-id=price_mo", "--annual-price-id=price_yr")
        call_command(
            "seed_plans",
            "--monthly-price-id=price_mo2",
            "--annual-price-id=price_yr",
            "--rotate-price",
        )
        active = Plan.objects.get(slug="beta-monthly")
        self.assertEqual(active.stripe_price_id, "price_mo2")
        self.assertTrue(active.is_active)

        retired = Plan.objects.exclude(pk=active.pk).get(stripe_price_id="price_mo")
        self.assertFalse(retired.is_active)
        self.assertNotEqual(retired.slug, "beta-monthly")
        self.assertTrue(retired.slug.startswith("beta-monthly-retired-"))

    def test_rotate_price_old_row_still_matches_renewals(self) -> None:
        from fragrance.billing import _grant_period_for_invoice

        call_command("seed_plans", "--monthly-price-id=price_mo", "--annual-price-id=price_yr")
        call_command(
            "seed_plans",
            "--monthly-price-id=price_mo2",
            "--annual-price-id=price_yr",
            "--rotate-price",
        )
        user = User.objects.create_user(username="rotated", email="rotated@example.com")
        BetaSignup.objects.create(
            email="rotated@example.com", normalized_email="rotated@example.com",
            status="converted", invited_user=user,
        )
        subscription = Subscription.objects.create(user=user, stripe_customer_id="cus_rot")
        invoice = _sdk_invoice(
            invoice_id="in_rot_1", customer_id="cus_rot", subscription_id="sub_rot",
            amount_paid=400,
            lines=[_sdk_line(price_id="price_mo", period_end=int(timezone.now().timestamp()) + 30 * 86400)],
        )
        self.assertTrue(_grant_period_for_invoice(invoice=invoice))
        subscription.refresh_from_db()
        self.assertTrue(subscription.has_active_access)

    def test_rotate_price_checkout_offers_only_the_new_price(self) -> None:
        from fragrance.billing import get_active_plans

        call_command("seed_plans", "--monthly-price-id=price_mo", "--annual-price-id=price_yr")
        call_command(
            "seed_plans",
            "--monthly-price-id=price_mo2",
            "--annual-price-id=price_yr",
            "--rotate-price",
        )
        offered_price_ids = {p.stripe_price_id for p in get_active_plans()}
        self.assertIn("price_mo2", offered_price_ids)
        self.assertNotIn("price_mo", offered_price_ids)


@override_settings(**_STRIPE_TEST_SETTINGS)
class BillingConfigurationTests(TestCase):
    """Fail-fast guard for missing Stripe credentials, mirroring the ANTHROPIC_API_KEY precedent."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="noconfig", email="noconfig@example.com")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, billing_interval="month", access_days_granted=30,
        )

    @override_settings(STRIPE_SECRET_KEY="")
    def test_checkout_returns_503_when_stripe_not_configured(self) -> None:
        response = self.client.post("/api/v1/billing/checkout/", {"slug": "beta-monthly"})
        self.assertEqual(response.status_code, 503)
