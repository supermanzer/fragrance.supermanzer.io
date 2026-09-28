import threading
import time
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import redis
import stripe
from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from fragrance.models import Plan, Subscription
from fragrance.tests.support.bases import StripeClientTestCase
from fragrance.tests.support.factories import (
    _active_user,
    _make_plan,
    _staff_not_superuser,
    active_subscription_for,
)
from fragrance.tests.support.stripe_fakes import _BILLING_CHANGES_SETTINGS as _SETTINGS
from fragrance.tests.support.stripe_fakes import _STRIPE_TEST_SETTINGS, patch_stripe_clients
from fragrance.views import FailOpenScopedRateThrottle
from rest_framework.test import APIClient

_KEY = "sk_test_dummy"


def _empty_subscription_list() -> stripe.ListObject:
    return stripe.ListObject.construct_from(
        {"object": "list", "data": [], "has_more": False, "url": "/v1/subscriptions"}, _KEY
    )


def _customer(*, customer_id: str) -> stripe.Customer:
    return stripe.Customer.construct_from({"id": customer_id, "object": "customer"}, _KEY)


def _session(*, session_id: str, url: str, **extra: object) -> stripe.checkout.Session:
    return stripe.checkout.Session.construct_from(
        {"id": session_id, "object": "checkout.session", "url": url, **extra}, _KEY
    )


@override_settings(**_STRIPE_TEST_SETTINGS)
class BillingCheckoutViewTests(StripeClientTestCase):
    url = "/api/v1/billing/checkout/"

    def setUp(self) -> None:
        super().setUp()
        # The lapsed-restart guard lists the customer's Stripe subscriptions; default to none.
        self.stripe.v1.subscriptions.list.return_value = _empty_subscription_list()
        self.monthly = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, currency="usd", billing_interval="month", access_days_granted=30,
        )
        self.annual = Plan.objects.create(
            name="Beta Annual", slug="beta-annual", stripe_price_id="price_yr",
            amount_cents=4400, currency="usd", billing_interval="year", access_days_granted=365,
        )
        self.user = User.objects.create_user(
            username="checkoutuser", email="checkoutuser@example.com", password="pw12345!"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_get_returns_active_plans_without_price_id(self) -> None:
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        # Wrapped in an object rather than returned as a bare array: the frontend
        # composable reads `response.plans`, and an object can carry metadata later
        # without a breaking change.
        plans = response.json()["plans"]
        slugs = {plan["slug"] for plan in plans}
        self.assertEqual(slugs, {"beta-monthly", "beta-annual"})
        for plan in plans:
            self.assertNotIn("stripe_price_id", plan)

    def test_unauthenticated_get_returns_401(self) -> None:
        response = APIClient().get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_unauthenticated_post_returns_401(self) -> None:
        response = APIClient().post(self.url, {"slug": "beta-monthly"})
        self.assertEqual(response.status_code, 401)

    def test_post_creates_checkout_session(self) -> None:
        self.stripe.v1.customers.create.return_value = _customer(customer_id="cus_new_1")
        self.stripe.v1.checkout.sessions.create.return_value = _session(
            session_id="cs_new_1", url="https://checkout.stripe.com/cs_new_1"
        )

        response = self.client.post(self.url, {"slug": "beta-monthly"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["url"], "https://checkout.stripe.com/cs_new_1")
        params = self.stripe.v1.checkout.sessions.create.call_args.kwargs["params"]
        self.assertEqual(params["mode"], "subscription")
        self.assertEqual(params["line_items"], [{"price": "price_mo", "quantity": 1}])
        self.assertEqual(params["client_reference_id"], str(self.user.id))
        self.assertEqual(params["subscription_data"]["metadata"]["user_id"], str(self.user.id))
        subscription = Subscription.objects.get(user=self.user)
        self.assertEqual(subscription.status, "checkout_started")
        self.assertEqual(subscription.last_checkout_session_id, "cs_new_1")
        self.assertEqual(subscription.stripe_customer_id, "cus_new_1")

    def test_post_ignores_client_supplied_price_id(self) -> None:
        self.stripe.v1.customers.create.return_value = _customer(customer_id="cus_new_2")
        self.stripe.v1.checkout.sessions.create.return_value = _session(
            session_id="cs_new_2", url="https://checkout.stripe.com/cs_new_2"
        )

        self.client.post(self.url, {"slug": "beta-monthly", "price_id": "price_evil_free_tier"})

        params = self.stripe.v1.checkout.sessions.create.call_args.kwargs["params"]
        self.assertEqual(params["line_items"], [{"price": "price_mo", "quantity": 1}])

    def test_post_unknown_slug_returns_400(self) -> None:
        response = self.client.post(self.url, {"slug": "does-not-exist"})
        self.assertEqual(response.status_code, 400)

    def test_post_inactive_slug_returns_400(self) -> None:
        self.monthly.is_active = False
        self.monthly.save(update_fields=["is_active"])
        response = self.client.post(self.url, {"slug": "beta-monthly"})
        self.assertEqual(response.status_code, 400)

    def test_post_already_active_returns_409(self) -> None:
        Subscription.objects.create(
            user=self.user, status="active", access_expires_at=timezone.now() + timedelta(days=10)
        )
        response = self.client.post(self.url, {"slug": "beta-monthly"})
        self.assertEqual(response.status_code, 409)

    def test_post_reuses_open_session_for_same_plan(self) -> None:
        Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_existing", last_checkout_session_id="cs_existing"
        )
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            session_id="cs_existing",
            url="https://checkout.stripe.com/cs_existing",
            status="open",
            metadata={"plan_slug": "beta-monthly", "price_id": "price_mo"},
            consent_collection={"terms_of_service": "required"},
        )

        response = self.client.post(self.url, {"slug": "beta-monthly"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["url"], "https://checkout.stripe.com/cs_existing")
        self.stripe.v1.checkout.sessions.create.assert_not_called()

    def test_reuse_path_works_with_a_real_sdk_session_object(self) -> None:
        # Regression, found by the sandbox walkthrough: on a real session, `.metadata` is a
        # StripeObject, which is NOT a dict and has no `.get()`, so the reuse path raised
        # AttributeError (a 500 on the second click of "Subscribe"). Building a genuine SDK
        # object here (as every fixture in this file now does) is what catches it.
        Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_real_obj", last_checkout_session_id="cs_real_obj"
        )
        real = _session(
            session_id="cs_real_obj",
            url="https://checkout.stripe.com/cs_real_obj",
            status="open",
            mode="subscription",
            metadata={"plan_slug": "beta-monthly", "price_id": "price_mo"},
            consent_collection={"terms_of_service": "required"},
        )
        self.assertFalse(hasattr(real.metadata, "get") and isinstance(real.metadata, dict))
        self.stripe.v1.checkout.sessions.retrieve.return_value = real

        response = self.client.post(self.url, {"slug": "beta-monthly"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["url"], "https://checkout.stripe.com/cs_real_obj")
        self.stripe.v1.checkout.sessions.create.assert_not_called()

    def test_post_expires_open_session_for_different_plan(self) -> None:
        Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_existing_2", last_checkout_session_id="cs_existing_2"
        )
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            session_id="cs_existing_2",
            url="https://checkout.stripe.com/cs_existing_2",
            status="open",
            metadata={"plan_slug": "beta-annual", "price_id": "price_yr"},
        )
        self.stripe.v1.checkout.sessions.create.return_value = _session(
            session_id="cs_new_3", url="https://checkout.stripe.com/cs_new_3"
        )
        self.stripe.v1.checkout.sessions.expire.return_value = None

        response = self.client.post(self.url, {"slug": "beta-monthly"})

        self.assertEqual(response.status_code, 200)
        self.stripe.v1.checkout.sessions.expire.assert_called_once_with("cs_existing_2")
        self.stripe.v1.checkout.sessions.create.assert_called_once()

    def test_post_expires_open_session_when_price_id_changed_under_same_slug(self) -> None:
        """
        A stale open session for the *same slug* but an old Price (the
        operator re-ran seed_plans with a new --monthly-price-id) must be
        expired, not reused -- reuse must key on price, not just slug, or a
        user could complete checkout against a retired Price.
        """
        Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_existing_3", last_checkout_session_id="cs_existing_3"
        )
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            session_id="cs_existing_3",
            url="https://checkout.stripe.com/cs_existing_3",
            status="open",
            metadata={"plan_slug": "beta-monthly", "price_id": "price_mo_RETIRED"},
        )
        self.stripe.v1.checkout.sessions.create.return_value = _session(
            session_id="cs_new_4", url="https://checkout.stripe.com/cs_new_4"
        )
        self.stripe.v1.checkout.sessions.expire.return_value = None

        response = self.client.post(self.url, {"slug": "beta-monthly"})

        self.assertEqual(response.status_code, 200)
        self.stripe.v1.checkout.sessions.expire.assert_called_once_with("cs_existing_3")
        self.stripe.v1.checkout.sessions.create.assert_called_once()


@override_settings(**_STRIPE_TEST_SETTINGS)
class CheckoutCustomerRaceTests(TransactionTestCase):
    """
    Item 6: two concurrent double-submits of Checkout must not each create a
    separate Stripe Customer. create_checkout_session now locks the
    Subscription row (select_for_update) around the check-and-create-if-
    missing step in _get_or_create_customer, so the second thread must
    observe the first thread's stripe_customer_id rather than racing it.
    """

    def test_concurrent_checkout_creates_exactly_one_stripe_customer(self) -> None:
        Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, currency="usd", billing_interval="month", access_days_granted=30,
        )
        user = User.objects.create_user(
            username="racer_checkout", email="racer_checkout@example.com", password="pw12345!"
        )
        barrier = threading.Barrier(2)
        call_count = {"n": 0}
        count_lock = threading.Lock()

        def _slow_customer_create(**kwargs: object) -> Any:
            with count_lock:
                call_count["n"] += 1
                n = call_count["n"]
            # Widen the race window so both threads are inside
            # _get_or_create_customer's unlocked read at the same time if the
            # row lock isn't actually serializing them.
            time.sleep(0.1)
            return _customer(customer_id=f"cus_race_{n}")

        results: list[str] = []
        results_lock = threading.Lock()

        def _checkout() -> None:
            from django.db import connection

            from fragrance.billing import create_checkout_session

            try:
                barrier.wait()
                url = create_checkout_session(user=user, plan_slug="beta-monthly")
                with results_lock:
                    results.append(url)
            finally:
                connection.close()

        with patch_stripe_clients() as (fake, _default, _interactive):
            fake.v1.customers.create.side_effect = _slow_customer_create
            fake.v1.checkout.sessions.create.return_value = _session(
                session_id="cs_race_1", url="https://checkout.stripe.com/cs_race_1"
            )

            threads = [threading.Thread(target=_checkout) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(call_count["n"], 1)
        self.assertEqual(len(results), 2)
        subscription = Subscription.objects.get(user=user)
        self.assertEqual(subscription.stripe_customer_id, "cus_race_1")


class ExemptCheckoutRefusalTests(TestCase):
    def test_staff_not_superuser_gets_409_with_no_side_effects(self) -> None:
        user = _staff_not_superuser(username="exempt_checkout")
        _make_plan(slug="ex-plan", interval="month", access_days=30)
        client = APIClient()
        client.force_authenticate(user=user)
        with patch_stripe_clients() as (fake, _default, _interactive):
            response = client.post(reverse("billing-checkout"), {"slug": "ex-plan"}, format="json")
            fake.v1.customers.create.assert_not_called()
            fake.v1.checkout.sessions.create.assert_not_called()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {
                "code": "exempt",
                "detail": "This account has full access, so no subscription is needed.",
            },
        )
        self.assertFalse(Subscription.objects.filter(user=user).exists())


class BillingSubscriptionViewTests(TestCase):
    """
    Covers GET /api/v1/billing/subscription/ -- the already-active read used
    by the /billing paywall page. No Stripe calls here (unlike
    BillingSessionView), so no _STRIPE_TEST_SETTINGS override is needed.
    """

    url = "/api/v1/billing/subscription/"

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="subview", email="subview@example.com")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_unauthenticated_returns_401(self) -> None:
        response = APIClient().get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_no_subscription_row_yet_returns_none_status_and_writes_nothing(self) -> None:
        self.assertFalse(Subscription.objects.filter(user=self.user).exists())
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "none",
                "plan": None,
                "access_expires_at": None,
                "has_active_access": False,
                "exempt": False,
                "can_manage_billing": False,
                "quota": {"runs_allowed": 0, "runs_used": 0, "next_eligible_at": None, "can_trigger": False},
            },
        )
        self.assertFalse(Subscription.objects.filter(user=self.user).exists())

    def test_get_never_creates_a_row_for_any_kind_of_user(self) -> None:
        staff = User.objects.create_user(username="substaff", password="pw12345!", is_staff=True)
        client = APIClient()
        client.force_authenticate(user=staff)
        body = client.get(self.url).json()
        self.assertEqual(Subscription.objects.count(), 0)
        self.assertEqual(body["status"], "none")
        self.assertTrue(body["exempt"])
        self.assertTrue(body["has_active_access"])
        self.assertIsNone(body["quota"]["runs_allowed"])

    def test_lapsed_payload_is_unchanged(self) -> None:
        Subscription.objects.create(
            user=self.user, status="active", access_expires_at=timezone.now() - timedelta(days=1)
        )
        body = self.client.get(self.url).json()
        self.assertEqual(body["status"], "active")
        self.assertFalse(body["has_active_access"])
        self.assertFalse(body["exempt"])
        self.assertEqual(body["quota"]["runs_allowed"], 0)
        self.assertFalse(body["quota"]["can_trigger"])

    def test_active_payload_reports_one_run_allowed(self) -> None:
        active_subscription_for(self.user)
        body = self.client.get(self.url).json()
        self.assertTrue(body["has_active_access"])
        self.assertEqual(body["quota"]["runs_allowed"], 1)
        self.assertTrue(body["quota"]["can_trigger"])

    def test_throttle_scope_is_configured_and_fails_open(self) -> None:
        self.assertEqual(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["billing-status"], "60/min")
        with patch.object(
            FailOpenScopedRateThrottle,
            "cache",
            **{"get.side_effect": redis.exceptions.ConnectionError("redis is down")},
        ), self.assertLogs("fragrance.views", level="WARNING") as logs:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("scope=billing-status" in m for m in logs.output))

    def test_active_subscription_reports_plan_and_access(self) -> None:
        plan = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, billing_interval="month", access_days_granted=30,
        )
        expiry = timezone.now() + timedelta(days=10)
        Subscription.objects.create(
            user=self.user, plan=plan, status="active", access_expires_at=expiry
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["plan"], {"slug": "beta-monthly", "name": "Beta Monthly"})
        self.assertTrue(body["has_active_access"])

    def test_scopes_to_the_requesting_user(self) -> None:
        other = User.objects.create_user(username="otheruser", email="other@example.com")
        Subscription.objects.create(
            user=other, status="active", access_expires_at=timezone.now() + timedelta(days=10)
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "none")


@override_settings(**_SETTINGS)
class CanManageBillingPayloadTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()

    def _get(self, *, user: User) -> dict:
        self.client.force_authenticate(user=user)
        response = self.client.get("/api/v1/billing/subscription/")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_true_with_a_customer_id(self) -> None:
        user = _active_user(username="payer")
        self.assertIs(self._get(user=user)["can_manage_billing"], True)

    def test_false_without_a_customer_id(self) -> None:
        user = User.objects.create_user(username="nocus", password="pw12345!")
        Subscription.objects.create(user=user)
        self.assertIs(self._get(user=user)["can_manage_billing"], False)

    def test_false_for_user_with_no_subscription_row(self) -> None:
        user = User.objects.create_user(username="norow", password="pw12345!")
        self.assertIs(self._get(user=user)["can_manage_billing"], False)

    def test_false_for_exempt_even_with_a_customer_id(self) -> None:
        user = _active_user(username="boss", customer_id="cus_boss")
        User.objects.filter(pk=user.pk).update(is_staff=True)
        user.refresh_from_db()
        self.assertIs(self._get(user=user)["can_manage_billing"], False)

    def test_true_for_a_lapsed_customer(self) -> None:
        user = _active_user(username="lapsed")
        Subscription.objects.filter(user=user).update(
            status="none", access_expires_at=timezone.now() - timedelta(days=1)
        )
        self.assertIs(self._get(user=user)["can_manage_billing"], True)


@override_settings(**_STRIPE_TEST_SETTINGS)
class BillingThrottleFailOpenTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="failopen", password="pw12345!")
        Subscription.objects.create(user=self.user, stripe_customer_id="cus_failopen")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.plan = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_fo",
            amount_cents=400, currency="usd", billing_interval="month", access_days_granted=30,
        )

    def _down(self):
        return patch.object(
            FailOpenScopedRateThrottle,
            "cache",
            **{"get.side_effect": redis.exceptions.ConnectionError("redis is down")},
        )

    def test_checkout_get_served_and_warns_when_cache_down(self) -> None:
        with self._down(), self.assertLogs("fragrance.views", level="WARNING") as logs:
            response = self.client.get("/api/v1/billing/checkout/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("throttle cache unavailable" in m for m in logs.output))

    def test_checkout_post_not_500_when_cache_down(self) -> None:
        self.stripe.v1.checkout.sessions.create.return_value = _session(
            session_id="cs_1", url="https://stripe.test/x"
        )
        with self._down(), self.assertLogs("fragrance.views", level="WARNING") as logs:
            response = self.client.post(
                "/api/v1/billing/checkout/", {"plan": "beta-monthly"}, format="json"
            )
        self.assertNotEqual(response.status_code, 500)
        self.assertTrue(any("throttle cache unavailable" in m for m in logs.output))

    def test_session_not_500_when_cache_down(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.side_effect = stripe.InvalidRequestError(
            "no such session", None
        )
        with self._down(), self.assertLogs("fragrance.views", level="WARNING") as logs:
            response = self.client.get("/api/v1/billing/session/cs_x/")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(any("throttle cache unavailable" in m for m in logs.output))
