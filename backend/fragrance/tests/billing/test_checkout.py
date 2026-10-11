import threading
import time
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import redis
import stripe
from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from fragrance.billing import (
    AccountLockedError,
    CheckoutSessionUrlMissingError,
    SubscriptionAlreadyActiveError,
    create_checkout_session,
)
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
            amount_cents=400, currency="usd", billing_interval="month",
        )
        self.annual = Plan.objects.create(
            name="Beta Annual", slug="beta-annual", stripe_price_id="price_yr",
            amount_cents=4400, currency="usd", billing_interval="year",
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

    @override_settings(SUPPORT_EMAIL="help@example.test")
    def test_post_revoked_account_returns_409_account_locked_with_no_stripe_calls(self) -> None:
        Subscription.objects.create(
            user=self.user, status="revoked", access_expires_at=timezone.now(),
            stripe_customer_id="cus_locked", last_checkout_session_id="cs_before_dispute",
        )

        response = self.client.post(self.url, {"slug": "beta-monthly"})

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["code"], "account_locked")
        self.assertIn("help@example.test", body["detail"])
        self.stripe.v1.checkout.sessions.retrieve.assert_not_called()
        self.stripe.v1.checkout.sessions.expire.assert_not_called()
        self.stripe.v1.checkout.sessions.create.assert_not_called()
        self.stripe.v1.customers.create.assert_not_called()
        subscription = Subscription.objects.get(user=self.user)
        self.assertEqual(subscription.status, "revoked")
        self.assertEqual(subscription.last_checkout_session_id, "cs_before_dispute")

    def test_post_revoked_account_is_refused_even_with_a_reusable_open_session(self) -> None:
        Subscription.objects.create(
            user=self.user, status="revoked", stripe_customer_id="cus_locked_2",
            last_checkout_session_id="cs_open",
        )
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            session_id="cs_open", url="https://checkout.stripe.com/cs_open", status="open",
            metadata={"plan_slug": "beta-monthly", "price_id": "price_mo"},
        )

        response = self.client.post(self.url, {"slug": "beta-monthly"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "account_locked")

    def test_post_unknown_slug_on_revoked_account_is_still_400(self) -> None:
        Subscription.objects.create(user=self.user, status="revoked")
        response = self.client.post(self.url, {"slug": "does-not-exist"})
        self.assertEqual(response.status_code, 400)

    def test_post_after_unlock_status_none_checks_out_normally(self) -> None:
        Subscription.objects.create(
            user=self.user, status="none", access_expires_at=timezone.now() - timedelta(days=3),
            stripe_customer_id="cus_unlocked",
        )
        self.stripe.v1.checkout.sessions.create.return_value = _session(
            session_id="cs_unlocked", url="https://checkout.stripe.com/cs_unlocked"
        )

        response = self.client.post(self.url, {"slug": "beta-monthly"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["url"], "https://checkout.stripe.com/cs_unlocked")

    def test_post_session_created_without_a_url_returns_502_and_stores_nothing(self) -> None:
        self.stripe.v1.customers.create.return_value = _customer(customer_id="cus_nourl")
        for empty_url in ("", None):
            with self.subTest(url=empty_url):
                self.stripe.v1.checkout.sessions.create.return_value = stripe.checkout.Session.construct_from(
                    {"id": "cs_nourl", "object": "checkout.session", "url": empty_url}, _KEY
                )

                with self.assertLogs("fragrance.billing", level="ERROR") as logs:
                    response = self.client.post(self.url, {"slug": "beta-monthly"})

                self.assertEqual(response.status_code, 502)
                self.assertEqual(
                    response.json(), {"detail": "Could not start checkout. Please try again shortly."}
                )
                self.assertIn("cs_nourl", logs.output[0])
                subscription = Subscription.objects.get(user=self.user)
                self.assertNotEqual(subscription.status, "checkout_started")
                self.assertEqual(subscription.last_checkout_session_id, "")

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
        A stale open session for the *same slug* but an old Price (a Plan
        row replaced on the same slug with a different Stripe Price) must be
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
class CustomerCreateRetryOptionTests(StripeClientTestCase):
    """Only customers.create carries the one-retry option; see also the wire-level proof in test_stripe_client_seam."""

    def setUp(self) -> None:
        super().setUp()
        _make_plan(slug="retry-plan", interval="month")
        self.user = User.objects.create_user(username="retryopt", email="retryopt@example.com")

    def test_customer_create_is_given_exactly_one_network_retry(self) -> None:
        self.stripe.v1.customers.create.return_value = _customer(customer_id="cus_opt")
        self.stripe.v1.checkout.sessions.create.return_value = _session(
            session_id="cs_opt", url="https://checkout.stripe.com/cs_opt"
        )
        from fragrance.billing import create_checkout_session

        create_checkout_session(user=self.user, plan_slug="retry-plan")

        self.assertEqual(
            self.stripe.v1.customers.create.call_args.kwargs["options"], {"max_network_retries": 1}
        )
        self.assertNotIn("options", self.stripe.v1.checkout.sessions.create.call_args.kwargs)

    def test_existing_customer_is_reused_without_any_create_call(self) -> None:
        Subscription.objects.create(user=self.user, stripe_customer_id="cus_have")
        self.stripe.v1.checkout.sessions.create.return_value = _session(
            session_id="cs_opt2", url="https://checkout.stripe.com/cs_opt2"
        )
        from fragrance.billing import create_checkout_session

        create_checkout_session(user=self.user, plan_slug="retry-plan")

        self.stripe.v1.customers.create.assert_not_called()


@override_settings(**_STRIPE_TEST_SETTINGS)
class CheckoutDisputeRaceTests(StripeClientTestCase):
    """
    A dispute (revoke_for_dispute's single UPDATE) can land at any point during
    create_checkout_session. It must never be overwritten with
    "checkout_started", however slow Stripe is.
    """

    def setUp(self) -> None:
        super().setUp()
        _make_plan(slug="race-plan", interval="month")
        self.user = User.objects.create_user(username="raceuser", email="raceuser@example.com")
        self.subscription = Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_race", last_checkout_session_id="cs_before"
        )
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            session_id="cs_before", url="https://checkout.stripe.com/cs_before", status="expired"
        )

    def _revoke(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(
            status="revoked", access_expires_at=timezone.now()
        )

    def _assert_locked_and_untouched(self) -> None:
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")
        self.assertEqual(self.subscription.last_checkout_session_id, "cs_before")

    def test_revoke_between_the_customer_step_and_session_create_is_not_overwritten(self) -> None:
        def _create_while_dispute_lands(**kwargs: object) -> stripe.checkout.Session:
            self._revoke()
            return _session(session_id="cs_mid", url="https://checkout.stripe.com/cs_mid")

        self.stripe.v1.checkout.sessions.create.side_effect = _create_while_dispute_lands
        self.stripe.v1.checkout.sessions.expire.return_value = None

        with self.assertRaises(AccountLockedError), self.assertLogs("fragrance.billing", level="WARNING"):
            create_checkout_session(user=self.user, plan_slug="race-plan")

        self._assert_locked_and_untouched()
        self.stripe.v1.checkout.sessions.expire.assert_called_once_with("cs_mid")

    def test_the_view_answers_409_account_locked_for_a_mid_request_revoke(self) -> None:
        def _create_while_dispute_lands(**kwargs: object) -> stripe.checkout.Session:
            self._revoke()
            return _session(session_id="cs_mid", url="https://checkout.stripe.com/cs_mid")

        self.stripe.v1.checkout.sessions.create.side_effect = _create_while_dispute_lands
        self.stripe.v1.checkout.sessions.expire.return_value = None
        client = APIClient()
        client.force_authenticate(user=self.user)

        with self.assertLogs("fragrance.billing", level="WARNING"):
            response = client.post("/api/v1/billing/checkout/", {"slug": "race-plan"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "account_locked")
        self._assert_locked_and_untouched()

    def test_a_failing_expire_never_masks_the_lock(self) -> None:
        def _create_while_dispute_lands(**kwargs: object) -> stripe.checkout.Session:
            self._revoke()
            return _session(session_id="cs_mid", url="https://checkout.stripe.com/cs_mid")

        self.stripe.v1.checkout.sessions.create.side_effect = _create_while_dispute_lands
        self.stripe.v1.checkout.sessions.expire.side_effect = stripe.APIConnectionError("down")

        with self.assertRaises(AccountLockedError), self.assertLogs("fragrance.billing", level="ERROR") as logs:
            create_checkout_session(user=self.user, plan_slug="race-plan")

        self.stripe.v1.checkout.sessions.expire.assert_called_once_with("cs_mid")
        self.assertTrue(any("cs_mid" in line and "could not expire" in line for line in logs.output))
        self._assert_locked_and_untouched()

    def test_revoke_between_the_first_check_and_the_lock_is_refused_before_any_stripe_write(self) -> None:
        # The open-session lookup runs after the unlocked check and before the lock.
        def _retrieve_while_dispute_lands(*args: object, **kwargs: object) -> stripe.checkout.Session:
            self._revoke()
            return _session(session_id="cs_before", url="https://checkout.stripe.com/cs_before", status="expired")

        self.stripe.v1.checkout.sessions.retrieve.side_effect = _retrieve_while_dispute_lands

        with self.assertRaises(AccountLockedError):
            create_checkout_session(user=self.user, plan_slug="race-plan")

        self.stripe.v1.customers.create.assert_not_called()
        self.stripe.v1.checkout.sessions.create.assert_not_called()
        self.stripe.v1.checkout.sessions.expire.assert_not_called()
        self._assert_locked_and_untouched()

    def test_without_a_race_the_row_moves_to_checkout_started(self) -> None:
        self.stripe.v1.checkout.sessions.create.return_value = _session(
            session_id="cs_fine", url="https://checkout.stripe.com/cs_fine"
        )

        url = create_checkout_session(user=self.user, plan_slug="race-plan")

        self.assertEqual(url, "https://checkout.stripe.com/cs_fine")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "checkout_started")
        self.assertEqual(self.subscription.last_checkout_session_id, "cs_fine")
        self.stripe.v1.checkout.sessions.expire.assert_not_called()


@override_settings(**_STRIPE_TEST_SETTINGS)
class CheckoutGrantRaceTests(StripeClientTestCase):
    """
    A payment grant can land during Session.create. The final write must not
    turn "active" into "checkout_started" (has_active_access needs "active"),
    and a lapsed "active" row must stay "active" so the frontend keeps
    treating the customer as lapsed rather than never-subscribed.
    """

    def setUp(self) -> None:
        super().setUp()
        _make_plan(slug="race-plan", interval="month")
        self.stripe.v1.subscriptions.list.return_value = _empty_subscription_list()
        self.user = User.objects.create_user(username="grantuser", email="grantuser@example.com")
        self.subscription = Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_grant", last_checkout_session_id="cs_before"
        )
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            session_id="cs_before", url="https://checkout.stripe.com/cs_before", status="expired"
        )
        self.grant_expiry = timezone.now() + timedelta(days=30)

    def _grant(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(
            status="active", access_expires_at=self.grant_expiry
        )

    def _create_while_grant_lands(self, **kwargs: object) -> stripe.checkout.Session:
        self._grant()
        return _session(session_id="cs_mid", url="https://checkout.stripe.com/cs_mid")

    def _assert_grant_untouched(self) -> None:
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "active")
        self.assertEqual(self.subscription.access_expires_at, self.grant_expiry)
        self.assertEqual(self.subscription.last_checkout_session_id, "cs_before")
        self.assertTrue(self.subscription.has_active_access)

    def test_a_grant_during_session_create_is_kept_and_the_new_session_expired(self) -> None:
        self.stripe.v1.checkout.sessions.create.side_effect = self._create_while_grant_lands
        self.stripe.v1.checkout.sessions.expire.return_value = None

        with self.assertRaises(SubscriptionAlreadyActiveError), self.assertLogs(
            "fragrance.billing", level="WARNING"
        ):
            create_checkout_session(user=self.user, plan_slug="race-plan")

        self._assert_grant_untouched()
        self.stripe.v1.checkout.sessions.expire.assert_called_once_with("cs_mid")

    def test_the_view_answers_409_already_active_for_a_mid_request_grant(self) -> None:
        self.stripe.v1.checkout.sessions.create.side_effect = self._create_while_grant_lands
        self.stripe.v1.checkout.sessions.expire.return_value = None
        client = APIClient()
        client.force_authenticate(user=self.user)

        with self.assertLogs("fragrance.billing", level="WARNING"):
            response = client.post("/api/v1/billing/checkout/", {"slug": "race-plan"})

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "You already have an active subscription."})
        self._assert_grant_untouched()

    def test_a_failing_expire_is_logged_and_never_masks_the_refusal(self) -> None:
        self.stripe.v1.checkout.sessions.create.side_effect = self._create_while_grant_lands
        self.stripe.v1.checkout.sessions.expire.side_effect = stripe.APIConnectionError("down")

        with self.assertRaises(SubscriptionAlreadyActiveError), self.assertLogs(
            "fragrance.billing", level="ERROR"
        ) as logs:
            create_checkout_session(user=self.user, plan_slug="race-plan")

        self.assertTrue(any("cs_mid" in line and "could not expire" in line for line in logs.output))
        self._assert_grant_untouched()

    def test_the_session_is_expired_after_the_row_lock_is_released(self) -> None:
        self.stripe.v1.checkout.sessions.create.side_effect = self._create_while_grant_lands
        # TestCase wraps each test in savepoints, so compare depth to the baseline:
        # an expire call made inside create_checkout_session's own atomic() block
        # would see one more.
        baseline_depth = len(connection.savepoint_ids)
        depth_at_expire: list[int] = []

        def _expire(session_id: str) -> None:
            depth_at_expire.append(len(connection.savepoint_ids))

        self.stripe.v1.checkout.sessions.expire.side_effect = _expire
        with self.assertRaises(SubscriptionAlreadyActiveError), self.assertLogs(
            "fragrance.billing", level="WARNING"
        ):
            create_checkout_session(user=self.user, plan_slug="race-plan")

        self.assertEqual(depth_at_expire, [baseline_depth])

    def test_a_lapsed_active_row_stays_active_and_stores_the_session_id(self) -> None:
        lapsed_at = timezone.now() - timedelta(days=3)
        Subscription.objects.filter(pk=self.subscription.pk).update(
            status="active", access_expires_at=lapsed_at
        )
        self.stripe.v1.checkout.sessions.create.return_value = _session(
            session_id="cs_lapsed", url="https://checkout.stripe.com/cs_lapsed"
        )

        url = create_checkout_session(user=self.user, plan_slug="race-plan")

        self.assertEqual(url, "https://checkout.stripe.com/cs_lapsed")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "active")
        self.assertEqual(self.subscription.access_expires_at, lapsed_at)
        self.assertEqual(self.subscription.last_checkout_session_id, "cs_lapsed")
        self.assertFalse(self.subscription.has_active_access)
        self.stripe.v1.checkout.sessions.expire.assert_not_called()

    def test_a_none_row_moves_to_checkout_started(self) -> None:
        self.stripe.v1.checkout.sessions.create.return_value = _session(
            session_id="cs_none", url="https://checkout.stripe.com/cs_none"
        )

        create_checkout_session(user=self.user, plan_slug="race-plan")

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "checkout_started")
        self.assertEqual(self.subscription.last_checkout_session_id, "cs_none")

    def test_a_checkout_started_row_keeps_its_status_and_gets_the_new_session_id(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(status="checkout_started")
        self.stripe.v1.checkout.sessions.create.return_value = _session(
            session_id="cs_again", url="https://checkout.stripe.com/cs_again"
        )

        create_checkout_session(user=self.user, plan_slug="race-plan")

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "checkout_started")
        self.assertEqual(self.subscription.last_checkout_session_id, "cs_again")

    def test_a_session_without_a_url_writes_nothing_even_when_a_grant_landed(self) -> None:
        def _create_without_url_while_grant_lands(**kwargs: object) -> stripe.checkout.Session:
            self._grant()
            return _session(session_id="cs_nourl", url="")

        self.stripe.v1.checkout.sessions.create.side_effect = _create_without_url_while_grant_lands

        with self.assertRaises(CheckoutSessionUrlMissingError), self.assertLogs(
            "fragrance.billing", level="ERROR"
        ):
            create_checkout_session(user=self.user, plan_slug="race-plan")

        self._assert_grant_untouched()
        self.stripe.v1.checkout.sessions.expire.assert_not_called()


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
            amount_cents=400, currency="usd", billing_interval="month",
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
        _make_plan(slug="ex-plan", interval="month")
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

    def test_revoked_account_reports_status_revoked_and_cannot_manage_billing(self) -> None:
        Subscription.objects.create(
            user=self.user, status="revoked", stripe_customer_id="cus_locked",
            access_expires_at=timezone.now(),
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "revoked")
        self.assertFalse(body["has_active_access"])
        self.assertFalse(body["can_manage_billing"])

    def test_existing_row_does_not_query_auth_user_again(self) -> None:
        from django.test.utils import CaptureQueriesContext

        Subscription.objects.create(
            user=self.user, status="active", stripe_customer_id="cus_q",
            access_expires_at=timezone.now() + timedelta(days=5),
        )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            [q["sql"] for q in queries.captured_queries if 'FROM "auth_user"' in q["sql"]]
        )

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
                "is_staff": False,
                "can_manage_billing": False,
                "quota": {
                    "runs_allowed": 0,
                    "runs_used": 0,
                    "next_eligible_at": None,
                    "can_trigger": False,
                    "first_run_available": False,
                    "next_run_at": None,
                },
                "lapse_reason": None,
                "payment_recovery": None,
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
            amount_cents=400, billing_interval="month",
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
            amount_cents=400, currency="usd", billing_interval="month",
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
