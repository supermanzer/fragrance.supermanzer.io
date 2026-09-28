import logging

import stripe
from django.contrib.auth.models import User
from django.test import override_settings
from fragrance.billing import fulfill_checkout_session
from fragrance.models import Plan, Subscription
from fragrance.tests.support.bases import StripeClientTestCase
from fragrance.tests.support.stripe_fakes import _EMAIL_SYNC_SETTINGS as _SETTINGS
from fragrance.tests.support.stripe_fakes import _KEY, _list_object
from rest_framework.test import APIClient


@override_settings(**_SETTINGS)
class ToSConsentCheckoutTests(StripeClientTestCase):
    url = "/api/v1/billing/checkout/"

    def setUp(self) -> None:
        super().setUp()
        self.stripe.v1.subscriptions.list.return_value = _list_object(data=[])
        Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, currency="usd", billing_interval="month", access_days_granted=30,
        )
        self.user = User.objects.create_user(username="u", email="u@example.com", password="pw12345!")
        Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_1", last_checkout_session_id="cs_old"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _session(self, *, consent: dict | None, session_id: str = "cs_old") -> stripe.checkout.Session:
        data = {
            "id": session_id, "object": "checkout.session", "status": "open", "mode": "subscription",
            "url": f"https://checkout.stripe.com/{session_id}",
            "metadata": {"plan_slug": "beta-monthly", "price_id": "price_mo"},
            "consent_collection": consent,
        }
        return stripe.checkout.Session.construct_from(data, _KEY)

    def _new_session(self) -> stripe.checkout.Session:
        return self._session(consent=None, session_id="cs_new")

    @override_settings(STRIPE_REQUIRE_TOS_CONSENT=True)
    def test_param_present_when_flag_on(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.side_effect = stripe.InvalidRequestError("gone", "id")
        self.stripe.v1.checkout.sessions.create.return_value = self._new_session()
        self.assertEqual(self.client.post(self.url, {"slug": "beta-monthly"}).status_code, 200)
        params = self.stripe.v1.checkout.sessions.create.call_args.kwargs["params"]
        self.assertEqual(params["consent_collection"], {"terms_of_service": "required"})

    @override_settings(STRIPE_REQUIRE_TOS_CONSENT=False)
    def test_param_absent_when_flag_off(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.side_effect = stripe.InvalidRequestError("gone", "id")
        self.stripe.v1.checkout.sessions.create.return_value = self._new_session()
        self.assertEqual(self.client.post(self.url, {"slug": "beta-monthly"}).status_code, 200)
        params = self.stripe.v1.checkout.sessions.create.call_args.kwargs["params"]
        self.assertNotIn("consent_collection", params)

    @override_settings(STRIPE_REQUIRE_TOS_CONSENT=True)
    def test_open_session_without_consent_is_expired_when_flag_on(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.return_value = self._session(consent=None)
        self.stripe.v1.checkout.sessions.create.return_value = self._new_session()
        self.stripe.v1.checkout.sessions.expire.return_value = None
        response = self.client.post(self.url, {"slug": "beta-monthly"})
        self.assertEqual(response.json()["url"], "https://checkout.stripe.com/cs_new")
        self.stripe.v1.checkout.sessions.expire.assert_called_once_with("cs_old")

    @override_settings(STRIPE_REQUIRE_TOS_CONSENT=True)
    def test_open_session_with_consent_is_reused_when_flag_on(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.return_value = self._session(
            consent={"terms_of_service": "required"}
        )
        response = self.client.post(self.url, {"slug": "beta-monthly"})
        self.assertEqual(response.json()["url"], "https://checkout.stripe.com/cs_old")
        self.stripe.v1.checkout.sessions.create.assert_not_called()
        self.stripe.v1.checkout.sessions.expire.assert_not_called()

    @override_settings(STRIPE_REQUIRE_TOS_CONSENT=False)
    def test_open_session_with_consent_is_expired_when_flag_off(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.return_value = self._session(
            consent={"terms_of_service": "required"}
        )
        self.stripe.v1.checkout.sessions.create.return_value = self._new_session()
        self.stripe.v1.checkout.sessions.expire.return_value = None
        self.client.post(self.url, {"slug": "beta-monthly"})
        self.stripe.v1.checkout.sessions.expire.assert_called_once_with("cs_old")
        self.stripe.v1.checkout.sessions.create.assert_called_once()

    @override_settings(STRIPE_REQUIRE_TOS_CONSENT=False)
    def test_open_session_without_consent_is_reused_when_flag_off(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.return_value = self._session(consent=None)
        response = self.client.post(self.url, {"slug": "beta-monthly"})
        self.assertEqual(response.json()["url"], "https://checkout.stripe.com/cs_old")
        self.stripe.v1.checkout.sessions.create.assert_not_called()

    @override_settings(STRIPE_REQUIRE_TOS_CONSENT=True)
    def test_missing_terms_url_maps_to_502_with_fixed_message_and_actionable_log(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.side_effect = stripe.InvalidRequestError("gone", "id")
        self.stripe.v1.checkout.sessions.create.side_effect = stripe.InvalidRequestError(
            "You cannot collect consent to a terms of service agreement until a URL is set in "
            "the Stripe Dashboard at https://dashboard.stripe.com/settings/public.",
            "consent_collection",
        )
        with self.assertLogs("fragrance.billing", level=logging.ERROR) as logs:
            response = self.client.post(self.url, {"slug": "beta-monthly"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"detail": "Could not start checkout. Please try again shortly."})
        self.assertIn("STRIPE_REQUIRE_TOS_CONSENT=False", logs.output[0])
        self.assertIn("Public details", logs.output[0])
        # State is not advanced by a failed create.
        self.assertEqual(Subscription.objects.get(user=self.user).last_checkout_session_id, "cs_old")

    @override_settings(STRIPE_REQUIRE_TOS_CONSENT=True)
    def test_other_invalid_request_errors_still_502_via_generic_path(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.side_effect = stripe.InvalidRequestError("gone", "id")
        self.stripe.v1.checkout.sessions.create.side_effect = stripe.InvalidRequestError(
            "No such price: 'price_mo'", "price"
        )
        with self.assertLogs("fragrance.views", level=logging.ERROR):
            response = self.client.post(self.url, {"slug": "beta-monthly"})
        self.assertEqual(response.status_code, 502)


@override_settings(**_SETTINGS)
class ToSAcceptanceLogTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="u2", email="u2@example.com", password="pw12345!")

    def _fulfill(self, *, consent: dict | None) -> None:
        session = stripe.checkout.Session.construct_from({
            "id": "cs_done", "object": "checkout.session", "client_reference_id": str(self.user.id),
            "payment_status": "unpaid", "consent": consent, "invoice": None,
        }, _KEY)
        self.stripe.v1.checkout.sessions.retrieve.return_value = session
        fulfill_checkout_session(session_id="cs_done", user=self.user)

    def test_accepted_terms_are_logged_with_the_session_id(self) -> None:
        with self.assertLogs("fragrance.billing", level=logging.INFO) as logs:
            self._fulfill(consent={"terms_of_service": "accepted", "promotions": None})
        self.assertTrue(any("cs_done" in line and "terms of service" in line for line in logs.output))

    def test_no_consent_logs_no_acceptance(self) -> None:
        with self.assertLogs("fragrance.billing", level=logging.INFO) as logs:
            self._fulfill(consent=None)
        self.assertFalse(any("accepted the terms" in line for line in logs.output))
