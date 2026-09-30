from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from fragrance.models import Plan, Subscription
from fragrance.tests.support.stripe_fakes import _STRIPE_TEST_SETTINGS
from rest_framework.test import APIClient


class PlanModelTests(TestCase):
    def test_quota_fields_are_independent_of_billing_interval(self) -> None:
        monthly = Plan.objects.create(
            name="Beta Monthly",
            slug="test-monthly",
            stripe_price_id="price_test_monthly",
            amount_cents=400,
            billing_interval="month",
        )
        annual = Plan.objects.create(
            name="Beta Annual",
            slug="test-annual",
            stripe_price_id="price_test_annual",
            amount_cents=4400,
            billing_interval="year",
        )
        # Usage terms must not follow billing terms -- an annual subscriber
        # gets one run per 30 days, not twelve on the first of January.
        self.assertEqual(monthly.quota_period_days, annual.quota_period_days)
        self.assertEqual(monthly.max_runs_per_quota_period, annual.max_runs_per_quota_period)
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


@override_settings(**_STRIPE_TEST_SETTINGS)
class BillingConfigurationTests(TestCase):
    """Fail-fast guard for missing Stripe credentials, mirroring the ANTHROPIC_API_KEY precedent."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="noconfig", email="noconfig@example.com")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, billing_interval="month",
        )

    @override_settings(STRIPE_SECRET_KEY="")
    def test_checkout_returns_503_when_stripe_not_configured(self) -> None:
        response = self.client.post("/api/v1/billing/checkout/", {"slug": "beta-monthly"})
        self.assertEqual(response.status_code, 503)


class PlanlessSubscriptionTests(TestCase):
    """
    Approval no longer assigns a plan, so every reader of Subscription.plan
    must cope with None before the first payment: the status payload (plan
    already null for never-subscribed users), quota_snapshot and _quota_terms.
    """

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="planless", email="planless@example.com", password="pw12345!"
        )
        self.subscription = Subscription.objects.create(user=self.user, status="none")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_status_payload_has_null_plan_and_lapsed_quota(self) -> None:
        response = self.client.get("/api/v1/billing/subscription/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body["plan"])
        self.assertEqual(body["status"], "none")
        self.assertFalse(body["has_active_access"])
        self.assertEqual(body["quota"]["runs_allowed"], 0)
        self.assertFalse(body["quota"]["can_trigger"])

    def test_quota_terms_fall_back_to_the_defaults_without_a_plan(self) -> None:
        from fragrance.services import (
            DEFAULT_FAILED_RUN_GRACE,
            DEFAULT_MAX_RUNS_PER_QUOTA_PERIOD,
            DEFAULT_QUOTA_PERIOD_DAYS,
            _quota_terms,
        )

        self.assertEqual(
            _quota_terms(plan=None),
            (
                timedelta(days=DEFAULT_QUOTA_PERIOD_DAYS),
                DEFAULT_MAX_RUNS_PER_QUOTA_PERIOD,
                DEFAULT_FAILED_RUN_GRACE,
            ),
        )

    def test_active_plan_less_subscription_can_trigger_under_default_terms(self) -> None:
        from fragrance.services import quota_snapshot

        Subscription.objects.filter(pk=self.subscription.pk).update(
            status="active", access_expires_at=timezone.now() + timedelta(days=5)
        )
        self.subscription.refresh_from_db()

        snapshot = quota_snapshot(user=self.user, subscription=self.subscription)

        self.assertEqual(snapshot["runs_allowed"], 1)
        self.assertTrue(snapshot["can_trigger"])

    def test_first_status_poll_after_approval_shows_no_plan_without_an_error(self) -> None:
        # The frontend contract: plan is null until the first grant sets it.
        self.assertIsNone(self.client.get("/api/v1/billing/subscription/").json()["plan"])
