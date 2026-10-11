"""
Row 15: a repeat Subscribe resumes the user's still-open Checkout Session
(Retrieve API regenerates its url), handles expired / complete / unknown
sessions, and settles the two-tabs race so only one session stays tracked.
"""

from datetime import timedelta

import stripe
from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone
from fragrance.billing import (
    CheckoutPaymentPendingError,
    CheckoutRaceUnresolvedError,
    SubscriptionAlreadyActiveError,
    start_checkout_session,
)
from fragrance.models import Plan, Subscription
from fragrance.tests.support.bases import StripeClientTestCase
from fragrance.tests.support.stripe_fakes import _STRIPE_TEST_SETTINGS
from rest_framework.test import APIClient

_KEY = "sk_test_dummy"
_URL = "/api/v1/billing/checkout/"


def _session(*, session_id: str, status: str = "open", url: str | None = "", **extra: object) -> stripe.checkout.Session:
    return stripe.checkout.Session.construct_from(
        {
            "id": session_id,
            "object": "checkout.session",
            "status": status,
            "url": url if url != "" else f"https://checkout.stripe.com/{session_id}",
            "metadata": {"plan_slug": "beta-monthly", "price_id": "price_mo"},
            **extra,
        },
        _KEY,
    )


@override_settings(**_STRIPE_TEST_SETTINGS, STRIPE_REQUIRE_TOS_CONSENT=False)
class CheckoutResumeTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.monthly = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, currency="usd", billing_interval="month",
        )
        self.annual = Plan.objects.create(
            name="Beta Annual", slug="beta-annual", stripe_price_id="price_yr",
            amount_cents=4400, currency="usd", billing_interval="year",
        )
        self.user = User.objects.create_user(username="resumer", email="resumer@example.com")
        self.subscription = Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_resume", status="checkout_started",
            last_checkout_session_id="cs_open",
        )
        self.stripe.v1.checkout.sessions.expire.return_value = None
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _stored(self) -> str:
        self.subscription.refresh_from_db()
        return self.subscription.last_checkout_session_id

    def test_open_session_is_resumed_via_retrieve_without_creating_anything(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            session_id="cs_open", url="https://checkout.stripe.com/regenerated"
        )
        response = self.client.post(_URL, {"slug": "beta-monthly"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"url": "https://checkout.stripe.com/regenerated", "resumed": True}
        )
        self.stripe.v1.checkout.sessions.create.assert_not_called()
        self.stripe.v1.customers.create.assert_not_called()
        self.stripe.v1.checkout.sessions.expire.assert_not_called()
        self.assertEqual(self._stored(), "cs_open")

    def test_new_session_reports_resumed_false(self) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(last_checkout_session_id="")
        self.stripe.v1.checkout.sessions.create.return_value = _session(session_id="cs_fresh")
        response = self.client.post(_URL, {"slug": "beta-monthly"})
        self.assertEqual(response.json(), {"url": "https://checkout.stripe.com/cs_fresh", "resumed": False})
        self.assertEqual(self._stored(), "cs_fresh")

    def test_expired_session_is_forgotten_and_replaced(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(session_id="cs_open", status="expired")
        self.stripe.v1.checkout.sessions.create.return_value = _session(session_id="cs_new")
        result = start_checkout_session(user=self.user, plan_slug="beta-monthly")
        self.assertFalse(result.resumed)
        self.assertEqual(self._stored(), "cs_new")
        self.stripe.v1.customers.create.assert_not_called()

    def test_unknown_session_is_forgotten_and_replaced(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.side_effect = stripe.InvalidRequestError(
            "No such checkout.session", param="id"
        )
        self.stripe.v1.checkout.sessions.create.return_value = _session(session_id="cs_new")
        result = start_checkout_session(user=self.user, plan_slug="beta-monthly")
        self.assertEqual(result.url, "https://checkout.stripe.com/cs_new")
        self.assertEqual(self._stored(), "cs_new")

    def test_open_session_for_another_plan_is_expired_then_replaced(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(session_id="cs_open")
        self.stripe.v1.checkout.sessions.create.return_value = _session(session_id="cs_annual")
        result = start_checkout_session(user=self.user, plan_slug="beta-annual")
        self.assertFalse(result.resumed)
        self.stripe.v1.checkout.sessions.expire.assert_called_once_with("cs_open")
        self.assertEqual(self._stored(), "cs_annual")

    def test_open_session_without_a_url_is_treated_as_stale(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(session_id="cs_open", url=None)
        self.stripe.v1.checkout.sessions.create.return_value = _session(session_id="cs_new")
        result = start_checkout_session(user=self.user, plan_slug="beta-monthly")
        self.assertFalse(result.resumed)
        self.stripe.v1.checkout.sessions.expire.assert_called_once_with("cs_open")

    def test_session_completed_between_retrieve_and_expire_does_not_502(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.side_effect = [
            _session(session_id="cs_open", metadata={"plan_slug": "beta-annual", "price_id": "price_yr"}),
            _session(session_id="cs_open", status="expired"),
        ]
        self.stripe.v1.checkout.sessions.expire.side_effect = stripe.InvalidRequestError(
            "cannot expire", param="id"
        )
        self.stripe.v1.checkout.sessions.create.return_value = _session(session_id="cs_new")
        result = start_checkout_session(user=self.user, plan_slug="beta-monthly")
        self.assertEqual(result.url, "https://checkout.stripe.com/cs_new")

    def test_recently_paid_session_awaiting_its_grant_refuses_a_second_checkout(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            session_id="cs_open", status="complete", payment_status="paid",
            created=int((timezone.now() - timedelta(minutes=2)).timestamp()),
            client_reference_id=str(self.user.id), invoice=None,
        )
        with self.assertRaises(CheckoutPaymentPendingError):
            start_checkout_session(user=self.user, plan_slug="beta-monthly")
        self.stripe.v1.checkout.sessions.create.assert_not_called()

    def test_payment_pending_is_a_409_with_a_code(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            session_id="cs_open", status="complete", payment_status="paid",
            created=int((timezone.now() - timedelta(minutes=2)).timestamp()),
            client_reference_id=str(self.user.id), invoice=None,
        )
        response = self.client.post(_URL, {"slug": "beta-monthly"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "payment_pending")
        self.stripe.v1.checkout.sessions.create.assert_not_called()

    def test_old_complete_session_is_history_and_does_not_block(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            session_id="cs_open", status="complete", payment_status="paid",
            created=int((timezone.now() - timedelta(days=40)).timestamp()),
        )
        self.stripe.v1.checkout.sessions.create.return_value = _session(session_id="cs_new")
        result = start_checkout_session(user=self.user, plan_slug="beta-monthly")
        self.assertFalse(result.resumed)

    def test_transient_stripe_failure_on_lookup_fails_closed(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.side_effect = stripe.APIConnectionError("down")
        response = self.client.post(_URL, {"slug": "beta-monthly"})
        self.assertEqual(response.status_code, 502)
        self.stripe.v1.checkout.sessions.create.assert_not_called()

    def test_wrong_user_never_sees_another_users_session(self) -> None:
        other = User.objects.create_user(username="intruder", email="intruder@example.com")
        other_client = APIClient()
        other_client.force_authenticate(user=other)
        self.stripe.v1.customers.create.return_value = stripe.Customer.construct_from(
            {"id": "cus_other", "object": "customer"}, _KEY
        )
        self.stripe.v1.checkout.sessions.create.return_value = _session(session_id="cs_other")
        response = other_client.post(_URL, {"slug": "beta-monthly"})
        self.assertEqual(response.json(), {"url": "https://checkout.stripe.com/cs_other", "resumed": False})
        self.stripe.v1.checkout.sessions.retrieve.assert_not_called()
        self.assertEqual(self._stored(), "cs_open")

    def test_unauthenticated_is_401(self) -> None:
        self.assertEqual(APIClient().post(_URL, {"slug": "beta-monthly"}).status_code, 401)


@override_settings(**_STRIPE_TEST_SETTINGS, STRIPE_REQUIRE_TOS_CONSENT=False)
class CheckoutTwoTabRaceTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, currency="usd", billing_interval="month",
        )
        Plan.objects.create(
            name="Beta Annual", slug="beta-annual", stripe_price_id="price_yr",
            amount_cents=4400, currency="usd", billing_interval="year",
        )
        self.user = User.objects.create_user(username="racer", email="racer@example.com")
        self.subscription = Subscription.objects.create(user=self.user, stripe_customer_id="cus_race2")
        self.stripe.v1.checkout.sessions.expire.return_value = None

    def _other_tab_stores(self, *, session_id: str) -> None:
        Subscription.objects.filter(pk=self.subscription.pk).update(
            last_checkout_session_id=session_id, status="checkout_started"
        )

    def test_loser_expires_its_own_session_and_returns_the_winners(self) -> None:
        def _create_while_other_tab_wins(**kwargs: object) -> stripe.checkout.Session:
            self._other_tab_stores(session_id="cs_winner")
            return _session(session_id="cs_loser")

        self.stripe.v1.checkout.sessions.create.side_effect = _create_while_other_tab_wins
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            session_id="cs_winner", url="https://checkout.stripe.com/cs_winner"
        )
        result = start_checkout_session(user=self.user, plan_slug="beta-monthly")
        self.assertEqual(result.url, "https://checkout.stripe.com/cs_winner")
        self.assertTrue(result.resumed)
        self.stripe.v1.checkout.sessions.expire.assert_called_once_with("cs_loser")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.last_checkout_session_id, "cs_winner")

    def test_other_tab_chose_another_plan_so_the_later_choice_wins(self) -> None:
        def _create_while_other_tab_wins(**kwargs: object) -> stripe.checkout.Session:
            self._other_tab_stores(session_id="cs_other_plan")
            return _session(session_id="cs_mine")

        self.stripe.v1.checkout.sessions.create.side_effect = _create_while_other_tab_wins
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            session_id="cs_other_plan", metadata={"plan_slug": "beta-annual", "price_id": "price_yr"}
        )
        result = start_checkout_session(user=self.user, plan_slug="beta-monthly")
        self.assertEqual(result.url, "https://checkout.stripe.com/cs_mine")
        self.assertFalse(result.resumed)
        self.stripe.v1.checkout.sessions.expire.assert_called_once_with("cs_other_plan")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.last_checkout_session_id, "cs_mine")

    def test_winner_already_paid_expires_ours_and_refuses(self) -> None:
        def _create_while_other_tab_wins(**kwargs: object) -> stripe.checkout.Session:
            self._other_tab_stores(session_id="cs_paid")
            return _session(session_id="cs_loser")

        self.stripe.v1.checkout.sessions.create.side_effect = _create_while_other_tab_wins
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            session_id="cs_paid", status="complete", payment_status="paid",
            created=int(timezone.now().timestamp()), client_reference_id=str(self.user.id), invoice=None,
        )
        with self.assertRaises(CheckoutPaymentPendingError):
            start_checkout_session(user=self.user, plan_slug="beta-monthly")
        self.stripe.v1.checkout.sessions.expire.assert_called_once_with("cs_loser")

    def test_lookup_failure_of_the_winner_fails_closed_and_expires_ours(self) -> None:
        def _create_while_other_tab_wins(**kwargs: object) -> stripe.checkout.Session:
            self._other_tab_stores(session_id="cs_unknown")
            return _session(session_id="cs_mine")

        self.stripe.v1.checkout.sessions.create.side_effect = _create_while_other_tab_wins
        self.stripe.v1.checkout.sessions.retrieve.side_effect = stripe.APIConnectionError("down")
        with self.assertLogs("fragrance.billing", level="WARNING"):
            with self.assertRaises(CheckoutRaceUnresolvedError):
                start_checkout_session(user=self.user, plan_slug="beta-monthly")
        self.stripe.v1.checkout.sessions.expire.assert_called_once_with("cs_mine")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.last_checkout_session_id, "cs_unknown")

    def test_lookup_failure_of_the_winner_is_a_retryable_502_over_http(self) -> None:
        def _create_while_other_tab_wins(**kwargs: object) -> stripe.checkout.Session:
            self._other_tab_stores(session_id="cs_unknown")
            return _session(session_id="cs_mine")

        self.stripe.v1.checkout.sessions.create.side_effect = _create_while_other_tab_wins
        self.stripe.v1.checkout.sessions.retrieve.side_effect = stripe.APIConnectionError("down")
        client = APIClient()
        client.force_authenticate(user=self.user)
        with self.assertLogs("fragrance.billing", level="WARNING"):
            response = client.post(_URL, {"slug": "beta-monthly"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json(), {"detail": "Could not start checkout. Please try again shortly."}
        )

    def test_lookup_failure_still_raises_when_expiring_ours_also_fails(self) -> None:
        def _create_while_other_tab_wins(**kwargs: object) -> stripe.checkout.Session:
            self._other_tab_stores(session_id="cs_unknown")
            return _session(session_id="cs_mine")

        self.stripe.v1.checkout.sessions.create.side_effect = _create_while_other_tab_wins
        self.stripe.v1.checkout.sessions.retrieve.side_effect = stripe.APIConnectionError("down")
        self.stripe.v1.checkout.sessions.expire.side_effect = stripe.APIConnectionError("down")
        with self.assertLogs("fragrance.billing", level="WARNING"):
            with self.assertRaises(CheckoutRaceUnresolvedError):
                start_checkout_session(user=self.user, plan_slug="beta-monthly")

    def test_no_race_leaves_exactly_one_tracked_session_and_no_duplicate_customer(self) -> None:
        self.stripe.v1.checkout.sessions.create.return_value = _session(session_id="cs_only")
        start_checkout_session(user=self.user, plan_slug="beta-monthly")
        self.stripe.v1.customers.create.assert_not_called()
        self.stripe.v1.checkout.sessions.expire.assert_not_called()
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.last_checkout_session_id, "cs_only")


@override_settings(**_STRIPE_TEST_SETTINGS)
class CheckoutAlreadyActiveStillWinsTests(StripeClientTestCase):
    def test_active_subscriber_is_refused_before_any_stripe_call(self) -> None:
        Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, currency="usd", billing_interval="month",
        )
        user = User.objects.create_user(username="activeone", email="activeone@example.com")
        Subscription.objects.create(
            user=user, status="active", access_expires_at=timezone.now() + timedelta(days=5),
            last_checkout_session_id="cs_open",
        )
        with self.assertRaises(SubscriptionAlreadyActiveError):
            start_checkout_session(user=user, plan_slug="beta-monthly")
        self.stripe.v1.checkout.sessions.retrieve.assert_not_called()
