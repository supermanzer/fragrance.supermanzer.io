"""
Tests for the Stripe transport seam itself: client construction, which
accessor each endpoint uses (interactive vs default), and that no global
stripe.* state is ever mutated -- plus a self-test of the FakeStripeClient
fixture used by the rest of fragrance/tests/billing/.

Client-construction assertions (StripeClientConstructionTests) go through
_wire_capture(): a real, simulated network failure forced through the actual
retry loop, so the Authorization/Stripe-Version headers, the timeout, the
attempt count, and which pooled http_client instance handled a call are all
read off the real request the SDK made -- never off
stripe.StripeClient/_APIRequestor's private `_requestor`/`_options`/`_client`
attributes, which have no public accessor and are not a stable contract
across an SDK upgrade.
"""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import requests
import stripe
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import TestCase, override_settings
from fragrance import billing
from fragrance.models import Subscription
from fragrance.tests.support.factories import _active_user, _make_plans
from fragrance.tests.support.stripe_fakes import (
    _BILLING_CHANGES_SETTINGS as _SETTINGS,
)
from fragrance.tests.support.stripe_fakes import (
    _KEY,
    _STRIPE_TEST_SETTINGS,
    _bare_schedule,
    _configuration,
    _configuration_list,
    _customer,
    _list_object,
    _pending_schedule_dict,
    _portal_session,
    _price_for,
    _real_schedule,
    _stripe_subscription,
    build_fake_stripe_client,
    patch_stripe_clients,
)
from requests.structures import CaseInsensitiveDict
from rest_framework.test import APIClient


def _session(**fields: object) -> stripe.checkout.Session:
    return stripe.checkout.Session.construct_from(
        {"id": fields.pop("id", "cs_seam"), "object": "checkout.session", **fields}, _KEY
    )


# Unbound reference to the real implementation, captured before any test
# patches the class -- lets _wire_capture's spy call through to the genuine
# retry-loop logic instead of replacing it.
_REAL_REQUESTS_CLIENT_REQUEST = stripe.RequestsClient.request


@contextmanager
def _wire_capture():
    """
    Forces one real (simulated) network failure through the actual retry
    ladder in stripe._http_client, rather than asserting on
    stripe.StripeClient/_APIRequestor's private `_requestor`/`_options`/
    `_client` attributes (no public accessor exists for a constructed
    client's key, retry count, or bound http_client -- see the 2026-09-26
    infra-review Improvement item on this file).

    Two patches, both autospec=True so `self` is passed through exactly as
    it would be on the real bound method (the same technique already used
    below by test_globals_stay_untouched_even_after_a_real_call_through_the_http_layer):

    - stripe.RequestsClient.request: the method stripe's own retry loop
      calls once per attempt (see _request_with_retries_internal). The spy
      records which pooled http_client instance (billing._HTTP_CLIENT or
      billing._INTERACTIVE_HTTP_CLIENT) handled the attempt, then calls
      through to the real implementation -- so the real retry ladder,
      sleep-and-retry count, and error classification all run for real.
    - requests.Session.request: the actual point a header dict and a
      timeout value reach the underlying HTTP layer. Raising
      requests.exceptions.ConnectionError here is classified by stripe's
      own _handle_request_error as retryable (should_retry=True), so the
      resulting attempt count is the SDK's real decision, not a number this
      test asserts by fiat.

    stripe._http_client.time.sleep is patched to a no-op so the real
    inter-retry backoff doesn't slow the test suite.

    Yields (attempts, wire_calls): attempts records one dict per call to
    RequestsClient.request (with the pooled client instance under "self");
    wire_calls records one dict per call that actually reached
    requests.Session.request (with "headers" and "timeout" exactly as
    passed to the wire).
    """
    attempts: list[dict] = []
    wire_calls: list[dict] = []

    def _spy_request(self, method, url, headers, post_data=None):
        attempts.append({"self": self, "method": method, "url": url})
        return _REAL_REQUESTS_CLIENT_REQUEST(self, method, url, headers, post_data)

    def _reach_the_wire_and_fail(self, method, url, **kwargs):
        wire_calls.append({"headers": kwargs.get("headers"), "timeout": kwargs.get("timeout")})
        raise requests.exceptions.ConnectionError("simulated network failure")

    with patch.object(
        stripe.RequestsClient, "request", autospec=True, side_effect=_spy_request
    ), patch.object(
        requests.Session, "request", autospec=True, side_effect=_reach_the_wire_and_fail
    ), patch("stripe._http_client.time.sleep", return_value=None):
        yield attempts, wire_calls


class StripeClientConstructionTests(TestCase):
    """Neither accessor ever touches global stripe.* state, and both honour settings."""

    def _assert_globals_untouched(self) -> None:
        self.assertIsNone(stripe.api_key)
        self.assertIsNone(stripe.default_http_client)

    @override_settings(STRIPE_SECRET_KEY="sk_test_construction", STRIPE_API_VERSION="2026-08-26.dahlia")
    def test_default_client_honours_settings_and_touches_no_globals(self) -> None:
        """
        Wire-level, not stripe.StripeClient/_APIRequestor internals: forces a
        real (simulated) network failure and reads the Authorization/
        Stripe-Version headers and retry count the SDK's own retry loop
        actually produced.
        """
        client = billing.get_stripe_client()
        with _wire_capture() as (attempts, wire_calls):
            with self.assertRaises(stripe.APIConnectionError):
                client.v1.customers.create(params={"email": "wire-default@example.test"})
        self.assertEqual(len(attempts), 3)  # 1 + max_network_retries=2
        self.assertTrue(all(attempt["self"] is billing._HTTP_CLIENT for attempt in attempts))
        self.assertEqual(wire_calls[0]["headers"]["Authorization"], "Bearer sk_test_construction")
        self.assertEqual(wire_calls[0]["headers"]["Stripe-Version"], "2026-08-26.dahlia")
        self.assertEqual(wire_calls[0]["timeout"], 20)
        self._assert_globals_untouched()

    @override_settings(STRIPE_SECRET_KEY="sk_test_construction", STRIPE_API_VERSION="2026-08-26.dahlia")
    def test_interactive_client_honours_settings_and_touches_no_globals(self) -> None:
        """Wire-level equivalent of the default-client test above; see its docstring."""
        client = billing.get_interactive_stripe_client()
        with _wire_capture() as (attempts, wire_calls):
            with self.assertRaises(stripe.APIConnectionError):
                client.v1.customers.create(params={"email": "wire-interactive@example.test"})
        self.assertEqual(len(attempts), 1)  # max_network_retries=0
        self.assertTrue(all(attempt["self"] is billing._INTERACTIVE_HTTP_CLIENT for attempt in attempts))
        self.assertEqual(wire_calls[0]["headers"]["Authorization"], "Bearer sk_test_construction")
        self.assertEqual(wire_calls[0]["headers"]["Stripe-Version"], "2026-08-26.dahlia")
        self.assertEqual(wire_calls[0]["timeout"], (3.05, 10))
        self._assert_globals_untouched()

    @override_settings(STRIPE_SECRET_KEY="")
    def test_default_client_missing_key_raises_improperly_configured(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            billing.get_stripe_client()

    @override_settings(STRIPE_SECRET_KEY="")
    def test_interactive_client_missing_key_raises_improperly_configured(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            billing.get_interactive_stripe_client()

    @override_settings(STRIPE_SECRET_KEY="sk_test_pool_a")
    def test_both_accessors_reuse_the_same_pooled_http_client(self) -> None:
        """
        Two client-construction calls through the same accessor route every
        attempt to the identical pooled http_client instance (proven by
        `self` on the real stripe.RequestsClient.request, not by reading
        `_requestor._client`); the two accessors route to two distinct
        instances.
        """
        with _wire_capture() as (attempts, _wire_calls):
            with self.assertRaises(stripe.APIConnectionError):
                billing.get_stripe_client().v1.customers.create(params={"email": "pool-a@example.test"})
            with self.assertRaises(stripe.APIConnectionError):
                billing.get_stripe_client().v1.customers.create(params={"email": "pool-b@example.test"})
        # Two default-client calls x 3 attempts each (1 + max_network_retries=2).
        # Without this count check, all() over an empty `attempts` list would
        # still pass if _wire_capture's patches silently failed to intercept
        # anything (e.g. a future stripe-python upgrade renames the patched
        # method), so this is what actually makes the test non-vacuous.
        self.assertEqual(len(attempts), 6)
        self.assertTrue(all(attempt["self"] is billing._HTTP_CLIENT for attempt in attempts))

        with _wire_capture() as (interactive_attempts, _wire_calls2):
            with self.assertRaises(stripe.APIConnectionError):
                billing.get_interactive_stripe_client().v1.customers.create(
                    params={"email": "pool-c@example.test"}
                )
            with self.assertRaises(stripe.APIConnectionError):
                billing.get_interactive_stripe_client().v1.customers.create(
                    params={"email": "pool-d@example.test"}
                )
        # Two interactive-client calls x 1 attempt each (max_network_retries=0).
        self.assertEqual(len(interactive_attempts), 2)
        self.assertTrue(
            all(attempt["self"] is billing._INTERACTIVE_HTTP_CLIENT for attempt in interactive_attempts)
        )

        # The two accessors are still two *different* http_clients (different budgets).
        self.assertIsNot(billing._HTTP_CLIENT, billing._INTERACTIVE_HTTP_CLIENT)

    @override_settings(STRIPE_SECRET_KEY="sk_test_budget")
    def test_default_and_interactive_timeouts_and_retries_differ(self) -> None:
        """
        Wire-level: the timeout value that actually reached requests, and the
        attempt count the SDK's own retry loop produced, for each accessor --
        not `._timeout`/`_INTERACTIVE_MAX_NETWORK_RETRIES` read directly off
        the module-level http_client objects.
        """
        with _wire_capture() as (default_attempts, default_wire):
            with self.assertRaises(stripe.APIConnectionError):
                billing.get_stripe_client().v1.customers.create(
                    params={"email": "budget-default@example.test"}
                )
        with _wire_capture() as (interactive_attempts, interactive_wire):
            with self.assertRaises(stripe.APIConnectionError):
                billing.get_interactive_stripe_client().v1.customers.create(
                    params={"email": "budget-interactive@example.test"}
                )
        self.assertEqual(len(default_attempts), 3)
        self.assertEqual(len(interactive_attempts), 1)
        self.assertEqual(default_wire[0]["timeout"], 20)
        self.assertEqual(interactive_wire[0]["timeout"], (3.05, 10))

    @override_settings(STRIPE_SECRET_KEY="sk_test_wire", STRIPE_API_VERSION="2026-08-26.dahlia")
    def test_globals_stay_untouched_even_after_a_real_call_through_the_http_layer(self) -> None:
        """
        Not just "the accessor never sets a global" -- a full round trip
        through the http layer (recorded, not real network) confirms nothing
        downstream of client construction reaches for stripe.api_key either.
        """

        def _fake(self, method, url, headers, post_data=None, max_network_retries=None, *, _usage=None):
            return json.dumps({"id": "cus_wire", "object": "customer"}), 200, {}

        with patch("stripe.RequestsClient.request_with_retries", autospec=True) as mock_request:
            mock_request.side_effect = _fake
            billing.get_stripe_client().v1.customers.create(params={"email": "wire@example.test"})
            billing.get_interactive_stripe_client().v1.customers.create(params={"email": "wire2@example.test"})
        self._assert_globals_untouched()


class _FakeStripeServer:
    """
    Stands in for api.stripe.com at the requests layer (requests.Session.request),
    with Stripe's server-side idempotency: the first request carrying a given
    Idempotency-Key creates a Customer and stores the response body; any later
    request with the same key gets that stored body back and creates nothing.

    lose_responses=N makes the first N wire calls raise requests' Timeout AFTER
    the server has processed them -- the worst case for a retry: the request
    reached Stripe, the response was lost.
    """

    def __init__(self, *, lose_responses: int = 0) -> None:
        self.lose_responses = lose_responses
        self.wire_calls: list[dict] = []
        self.customers_created = 0
        self._stored_by_key: dict[str, str] = {}

    def handle(self, session, method, url, **kwargs):
        headers = kwargs["headers"]
        key = headers.get("Idempotency-Key")
        self.wire_calls.append(
            {"method": method, "url": url, "idempotency_key": key, "timeout": kwargs.get("timeout")}
        )
        if key not in self._stored_by_key:
            self.customers_created += 1
            self._stored_by_key[key] = json.dumps(
                {"id": f"cus_srv_{self.customers_created}", "object": "customer"}
            )
        if len(self.wire_calls) <= self.lose_responses:
            raise requests.exceptions.Timeout("simulated: response lost after the server processed it")
        return SimpleNamespace(
            content=self._stored_by_key[key], status_code=200, headers=CaseInsensitiveDict()
        )


@contextmanager
def _fake_stripe_server(*, lose_responses: int = 0):
    server = _FakeStripeServer(lose_responses=lose_responses)
    with patch.object(
        requests.Session, "request", autospec=True, side_effect=server.handle
    ), patch("stripe._http_client.time.sleep", return_value=None):
        yield server


@override_settings(STRIPE_SECRET_KEY="sk_test_retry", STRIPE_API_VERSION="2026-08-26.dahlia")
class CustomerCreateRetryWireTests(TestCase):
    """
    Checkout's interactive client has 0 retries, but _get_or_create_customer
    grants customers.create ONE retry via the per-request max_network_retries
    option. What makes that safe is that the SDK reuses one Idempotency-Key
    across the attempts of a single call; proven here on the wire, against a
    server that models Stripe's idempotency, never by reading SDK internals.
    """

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="retrier", email="retrier@example.com")
        self.subscription = Subscription.objects.create(user=self.user)

    def _create(self) -> str:
        return billing._get_or_create_customer(
            user=self.user,
            subscription=self.subscription,
            client=billing.get_interactive_stripe_client(),
        )

    def test_timeout_then_success_creates_exactly_one_customer_under_one_idempotency_key(self) -> None:
        with _fake_stripe_server(lose_responses=1) as server:
            customer_id = self._create()

        self.assertEqual(len(server.wire_calls), 2)
        keys = [call["idempotency_key"] for call in server.wire_calls]
        self.assertTrue(keys[0])
        self.assertEqual(keys[0], keys[1])
        self.assertTrue(all(call["url"].endswith("/v1/customers") for call in server.wire_calls))
        self.assertTrue(all(call["timeout"] == (3.05, 10) for call in server.wire_calls))
        self.assertEqual(server.customers_created, 1)
        self.assertEqual(customer_id, "cus_srv_1")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.stripe_customer_id, "cus_srv_1")

    def test_retry_budget_is_exactly_one(self) -> None:
        with _fake_stripe_server(lose_responses=2) as server:
            with self.assertRaises(stripe.APIConnectionError):
                self._create()

        self.assertEqual(len(server.wire_calls), 2)
        self.assertEqual(server.wire_calls[0]["idempotency_key"], server.wire_calls[1]["idempotency_key"])
        self.assertEqual(server.customers_created, 1)
        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.stripe_customer_id)

    def test_a_separate_call_gets_a_fresh_idempotency_key(self) -> None:
        with _fake_stripe_server() as server:
            client = billing.get_interactive_stripe_client()
            client.v1.customers.create(params={"email": "a@example.test"})
            client.v1.customers.create(params={"email": "b@example.test"})

        keys = [call["idempotency_key"] for call in server.wire_calls]
        self.assertEqual(len(set(keys)), 2)

    def test_other_interactive_calls_still_get_no_retry(self) -> None:
        with _fake_stripe_server(lose_responses=5) as server:
            client = billing.get_interactive_stripe_client()
            with self.assertRaises(stripe.APIConnectionError):
                client.v1.checkout.sessions.create(params={"mode": "subscription"})
            with self.assertRaises(stripe.APIConnectionError):
                client.v1.customers.create(params={"email": "no-retry@example.test"})

        self.assertEqual(len(server.wire_calls), 2)


@override_settings(**_SETTINGS)
class WhichClientIsUsedTests(TestCase):
    """
    Portal and plan-change service functions use the interactive client; so
    do checkout and the session fallback (2026-09-27, since both run
    synchronously inside a user's HTTP request); fulfill_payment_intent
    (background/webhook only) uses the default client.
    """

    def setUp(self) -> None:
        self.monthly, self.annual = _make_plans()
        self.user = _active_user(username="whichclient")
        Subscription.objects.filter(user=self.user).update(plan=self.monthly)
        self._cm = patch_stripe_clients()
        self.stripe, self.mock_default, self.mock_interactive = self._cm.__enter__()
        self.addCleanup(self._cm.__exit__, None, None, None)

    def test_create_portal_session_uses_the_interactive_client_only(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        self.stripe.v1.billing_portal.sessions.create.return_value = _portal_session()
        billing.create_portal_session(user=self.user)
        self.mock_interactive.assert_called()
        self.mock_default.assert_not_called()

    def test_schedule_plan_change_uses_the_interactive_client_only(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        self.stripe.v1.prices.retrieve.side_effect = _price_for
        self.stripe.v1.subscription_schedules.create.return_value = _bare_schedule()
        self.stripe.v1.subscription_schedules.update.return_value = _real_schedule(
            schedule=_pending_schedule_dict()
        )
        billing.schedule_plan_change(user=self.user, plan_slug="beta-annual")
        self.mock_interactive.assert_called()
        self.mock_default.assert_not_called()

    def test_get_plan_change_state_uses_the_interactive_client_only(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        billing.get_plan_change_state(user=self.user)
        self.mock_interactive.assert_called()
        self.mock_default.assert_not_called()

    def test_cancel_plan_change_uses_the_interactive_client_only(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(
            schedule=_pending_schedule_dict()
        )
        self.stripe.v1.subscription_schedules.release.return_value = None
        billing.cancel_plan_change(user=self.user)
        self.mock_interactive.assert_called()
        self.mock_default.assert_not_called()

    def test_fulfill_payment_intent_uses_the_default_client_only(self) -> None:
        from fragrance.tests.support.stripe_fakes import _payment_intent

        self.stripe.v1.payment_intents.retrieve.return_value = _payment_intent(customer="cus_nomatch")
        billing.fulfill_payment_intent(payment_intent_id="pi_1")
        self.mock_default.assert_called()
        self.mock_interactive.assert_not_called()

    def test_create_checkout_session_uses_the_interactive_client_only(self) -> None:
        checkout_user = User.objects.create_user(
            username="whichclient_checkout", email="whichclient_checkout@example.com"
        )
        self.stripe.v1.customers.create.return_value = _customer(email=checkout_user.email)
        self.stripe.v1.checkout.sessions.create.return_value = _session(
            url="https://checkout.stripe.com/x"
        )
        billing.create_checkout_session(user=checkout_user, plan_slug=self.monthly.slug)
        self.mock_interactive.assert_called()
        self.mock_default.assert_not_called()

    def test_fulfill_checkout_session_uses_the_interactive_client_only(self) -> None:
        session_user = User.objects.create_user(
            username="whichclient_session", email="whichclient_session@example.com"
        )
        Subscription.objects.create(user=session_user, stripe_customer_id="cus_wc_session")
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            client_reference_id=str(session_user.id),
            customer="cus_wc_session",
            invoice=None,
            subscription=None,
        )
        billing.fulfill_checkout_session(session_id="cs_seam", user=session_user)
        self.mock_interactive.assert_called()
        self.mock_default.assert_not_called()


@override_settings(**_SETTINGS)
class WhichClientIsUsedOverHTTPTests(TestCase):
    """Same as WhichClientIsUsedTests, but driven through the real views/URLs."""

    def setUp(self) -> None:
        self.monthly, self.annual = _make_plans()
        self.user = _active_user(username="httpwhich")
        Subscription.objects.filter(user=self.user).update(plan=self.monthly)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self._cm = patch_stripe_clients()
        self.stripe, self.mock_default, self.mock_interactive = self._cm.__enter__()
        self.addCleanup(self._cm.__exit__, None, None, None)

    def test_portal_post_uses_the_interactive_client_only(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        self.stripe.v1.billing_portal.sessions.create.return_value = _portal_session()
        response = self.client.post("/api/v1/billing/portal/")
        self.assertEqual(response.status_code, 200)
        self.mock_interactive.assert_called()
        self.mock_default.assert_not_called()

    def test_change_plan_get_uses_the_interactive_client_only(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        response = self.client.get("/api/v1/billing/change-plan/")
        self.assertEqual(response.status_code, 200)
        self.mock_interactive.assert_called()
        self.mock_default.assert_not_called()

    def test_change_plan_post_uses_the_interactive_client_only(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        self.stripe.v1.prices.retrieve.side_effect = _price_for
        self.stripe.v1.subscription_schedules.create.return_value = _bare_schedule()
        self.stripe.v1.subscription_schedules.update.return_value = _real_schedule(
            schedule=_pending_schedule_dict()
        )
        response = self.client.post("/api/v1/billing/change-plan/", {"plan": "beta-annual"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.mock_interactive.assert_called()
        self.mock_default.assert_not_called()

    def test_change_plan_delete_uses_the_interactive_client_only(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(
            schedule=_pending_schedule_dict()
        )
        self.stripe.v1.subscription_schedules.release.return_value = None
        response = self.client.delete("/api/v1/billing/change-plan/")
        self.assertEqual(response.status_code, 204)
        self.mock_interactive.assert_called()
        self.mock_default.assert_not_called()

    def test_checkout_post_uses_the_interactive_client_only(self) -> None:
        checkout_user = User.objects.create_user(
            username="httpwhich_checkout", email="httpwhich_checkout@example.com", password="pw12345!"
        )
        self.stripe.v1.customers.create.return_value = _customer(email=checkout_user.email)
        self.stripe.v1.checkout.sessions.create.return_value = _session(
            url="https://checkout.stripe.com/x"
        )
        checkout_client = APIClient()
        checkout_client.force_authenticate(user=checkout_user)
        response = checkout_client.post(
            "/api/v1/billing/checkout/", {"slug": self.monthly.slug}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.mock_interactive.assert_called()
        self.mock_default.assert_not_called()

    def test_session_get_uses_the_interactive_client_only(self) -> None:
        session_user = User.objects.create_user(
            username="httpwhich_session", email="httpwhich_session@example.com", password="pw12345!"
        )
        Subscription.objects.create(user=session_user, stripe_customer_id="cus_httpwhich_session")
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            client_reference_id=str(session_user.id),
            customer="cus_httpwhich_session",
            invoice=None,
            subscription=None,
        )
        session_client = APIClient()
        session_client.force_authenticate(user=session_user)
        response = session_client.get("/api/v1/billing/session/cs_seam/")
        self.assertEqual(response.status_code, 200)
        self.mock_interactive.assert_called()
        self.mock_default.assert_not_called()


class DefaultOnlyEndpointsNeverUseTheInteractiveClientTests(TestCase):
    """
    Reconcile, the restore task, revoke_for_dispute, and the portal-config
    command all use the default client -- none of them runs synchronously
    inside a user's HTTP request.

    Checkout (create_checkout_session) and the session fallback
    (fulfill_checkout_session) moved to the interactive client on
    2026-09-27 (see the request-path rule documented above
    billing._INTERACTIVE_HTTP_CLIENT); they are covered, under accurate
    names, by WhichClientIsUsedTests and WhichClientIsUsedOverHTTPTests.
    """

    def setUp(self) -> None:
        self._cm = patch_stripe_clients()
        self.stripe, self.mock_default, self.mock_interactive = self._cm.__enter__()
        self.addCleanup(self._cm.__exit__, None, None, None)

    @override_settings(**_STRIPE_TEST_SETTINGS)
    def test_reconcile_subscriptions_never_uses_the_interactive_client(self) -> None:
        self.stripe.v1.invoices.list.return_value = _list_object(data=[])
        billing.reconcile_subscriptions()
        self.mock_default.assert_called()
        self.mock_interactive.assert_not_called()

    @override_settings(**_STRIPE_TEST_SETTINGS)
    def test_revoke_for_dispute_never_uses_the_interactive_client(self) -> None:
        from fragrance.tests.support.stripe_fakes import _payment_intent

        user = User.objects.create_user(username="disputewhich", email="disputewhich@example.com")
        Subscription.objects.create(user=user, stripe_customer_id="cus_dispute_which")
        self.stripe.v1.payment_intents.retrieve.return_value = _payment_intent(
            customer="cus_dispute_which"
        )
        self.stripe.v1.subscriptions.list.return_value = _list_object(data=[])
        self.stripe.v1.checkout.sessions.list.return_value = _list_object(data=[])
        billing.revoke_for_dispute(dispute={"payment_intent": "pi_dispute_which"})
        self.mock_default.assert_called()
        self.mock_interactive.assert_not_called()

    @override_settings(**_STRIPE_TEST_SETTINGS)
    def test_restore_stripe_customer_email_task_never_uses_the_interactive_client(self) -> None:
        from fragrance.tasks import restore_stripe_customer_email

        user = User.objects.create_user(username="restorewhich", email="old@example.test")
        Subscription.objects.create(user=user, stripe_customer_id="cus_restore")
        self.stripe.v1.customers.retrieve.return_value = _customer(email="attacker@evil.example")
        self.stripe.v1.customers.update.return_value = _customer(email="old@example.test")
        restore_stripe_customer_email(user_id=user.pk, customer_id="cus_restore", email="old@example.test")
        self.mock_default.assert_called()
        self.mock_interactive.assert_not_called()

    @override_settings(**_STRIPE_TEST_SETTINGS)
    def test_configure_stripe_portal_command_never_uses_the_interactive_client(self) -> None:
        self.stripe.v1.billing_portal.configurations.list.return_value = _configuration_list(configurations=[])
        self.stripe.v1.billing_portal.configurations.create.return_value = _configuration(
            config_id="bpc_seam", metadata={"app": "fragrances", "managed_by": "configure_stripe_portal"}
        )
        call_command("configure_stripe_portal")
        self.mock_default.assert_called()
        self.mock_interactive.assert_not_called()


class FakeStripeClientSelfTests(TestCase):
    """The fixture itself: typos and wrong call shapes must fail loudly, not silently."""

    def test_top_level_deprecated_accessor_is_not_exposed(self) -> None:
        fake = build_fake_stripe_client()
        with self.assertRaises(AttributeError):
            fake.customers

    def test_removed_or_renamed_method_raises_attribute_error(self) -> None:
        fake = build_fake_stripe_client()
        with self.assertRaises(AttributeError):
            fake.v1.subscription_schedules.modify

    def test_pre_migration_call_shape_raises_type_error(self) -> None:
        fake = build_fake_stripe_client()
        with self.assertRaises(TypeError):
            fake.v1.checkout.sessions.retrieve("cs_1", expand=["invoice"])

    def test_unconfigured_guarded_method_raises_instead_of_returning_a_magicmock(self) -> None:
        fake = build_fake_stripe_client()
        with self.assertRaises(stripe.APIConnectionError):
            fake.v1.subscriptions.retrieve("sub_1")

    def test_configuring_return_value_makes_the_guard_stand_down(self) -> None:
        fake = build_fake_stripe_client()
        fake.v1.subscriptions.retrieve.return_value = "configured"
        self.assertEqual(fake.v1.subscriptions.retrieve("sub_1"), "configured")
