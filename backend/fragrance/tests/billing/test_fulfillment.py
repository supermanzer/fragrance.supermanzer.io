import time
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

import stripe
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from fragrance.billing import (
    ComplimentaryGrantError,
    InvoiceNotSettledError,
    SessionNotFoundError,
    SessionOwnershipError,
    _grant_period_for_invoice,
    fulfill_checkout_session,
    fulfill_payment_intent,
    grant_complimentary_access,
)
from fragrance.models import BetaSignup, Plan, ProcessedPayment, Subscription
from fragrance.tests.support.bases import StripeClientTestCase
from fragrance.tests.support.factories import (
    _make_plans,
    _make_user,
    active_subscription_for,
)
from fragrance.tests.support.stripe_fakes import _BILLING_CHANGES_SETTINGS as _SETTINGS
from fragrance.tests.support.stripe_fakes import (
    _KEY,
    _STRIPE_TEST_SETTINGS,
    _invoice_payment_list,
    _list_object,
    _open_invoice_dict,
    _paid_invoice,
    _paid_invoice_dict,
    _payment_intent,
    _sdk_invoice,
    _sdk_invoice_payment,
    _sdk_line,
    _sdk_payment_intent,
)
from rest_framework.test import APIClient


def _session(**fields: object) -> stripe.checkout.Session:
    return stripe.checkout.Session.construct_from(
        {"id": fields.pop("id", "cs_test"), "object": "checkout.session", **fields}, _KEY
    )


@override_settings(**_STRIPE_TEST_SETTINGS)
class FulfillPaymentIntentTests(StripeClientTestCase):
    """
    One test per rejection branch of the payment_intent.succeeded validation
    chain (fragrance.billing.fulfill_payment_intent / _grant_period_for_invoice),
    since each branch is an authorization decision.
    """

    def setUp(self) -> None:
        super().setUp()
        self.plan = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, billing_interval="month", access_days_granted=30,
        )
        self.user = User.objects.create_user(username="payer", email="payer@example.com")
        BetaSignup.objects.create(
            email="payer@example.com", normalized_email="payer@example.com",
            status="converted", invited_user=self.user,
        )
        self.subscription = Subscription.objects.create(user=self.user, stripe_customer_id="cus_test_1")

    def test_status_not_succeeded_grants_nothing(self) -> None:
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_1", status="requires_action", customer="cus_test_1"
        )
        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 0)
        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.access_expires_at)

    def test_null_customer_grants_nothing(self) -> None:
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_1", status="succeeded", customer=None
        )
        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 0)

    def test_unknown_customer_grants_nothing(self) -> None:
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_1", status="succeeded", customer="cus_unknown"
        )
        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 0)

    def test_empty_invoice_payment_list_grants_nothing(self) -> None:
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_1", status="succeeded", customer="cus_test_1"
        )
        self.stripe.v1.invoice_payments.list.return_value = _list_object(data=[])
        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 0)

    def test_non_subscription_invoice_parent_grants_nothing(self) -> None:
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_1", status="succeeded", customer="cus_test_1"
        )
        invoice = _sdk_invoice(
            invoice_id="in_1", customer_id="cus_test_1", subscription_id=None,
            amount_paid=400, lines=[_sdk_line(price_id="price_mo", period_end=None)],
            parent_type="quote_details",
        )
        self.stripe.v1.invoice_payments.list.return_value = _list_object(
            data=[_sdk_invoice_payment(invoice=invoice)]
        )
        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 0)
        self.assertEqual(ProcessedPayment.objects.count(), 0)

    def test_unknown_price_id_grants_nothing(self) -> None:
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_1", status="succeeded", customer="cus_test_1"
        )
        invoice = _sdk_invoice(
            invoice_id="in_1", customer_id="cus_test_1", subscription_id="sub_1",
            amount_paid=400, lines=[_sdk_line(price_id="price_totally_unknown", period_end=None)],
        )
        self.stripe.v1.invoice_payments.list.return_value = _list_object(
            data=[_sdk_invoice_payment(invoice=invoice)]
        )
        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 0)

    def test_unapproved_user_grants_nothing(self) -> None:
        unapproved = User.objects.create_user(username="unapproved", email="unapproved@example.com")
        Subscription.objects.create(user=unapproved, stripe_customer_id="cus_unapproved")
        BetaSignup.objects.create(
            email="unapproved@example.com", normalized_email="unapproved@example.com", status="pending"
        )
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_2", status="succeeded", customer="cus_unapproved"
        )
        invoice = _sdk_invoice(
            invoice_id="in_2", customer_id="cus_unapproved", subscription_id="sub_2",
            amount_paid=400, lines=[_sdk_line(price_id="price_mo", period_end=None)],
        )
        self.stripe.v1.invoice_payments.list.return_value = _list_object(
            data=[_sdk_invoice_payment(invoice=invoice)]
        )
        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_2"), 0)

    def test_all_checks_pass_grants_one_period_and_persists_subscription_id(self) -> None:
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_3", status="succeeded", customer="cus_test_1"
        )
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _sdk_invoice(
            invoice_id="in_3", customer_id="cus_test_1", subscription_id="sub_3",
            amount_paid=400, lines=[_sdk_line(price_id="price_mo", period_end=period_end)],
        )
        self.stripe.v1.invoice_payments.list.return_value = _list_object(
            data=[_sdk_invoice_payment(invoice=invoice)]
        )

        granted = fulfill_payment_intent(payment_intent_id="pi_3")

        self.assertEqual(granted, 1)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "active")
        self.assertEqual(self.subscription.stripe_subscription_id, "sub_3")
        expected = datetime.fromtimestamp(period_end, tz=dt_timezone.utc) + timedelta(days=2)
        self.assertEqual(self.subscription.access_expires_at, expected)
        self.assertEqual(ProcessedPayment.objects.filter(stripe_invoice_id="in_3").count(), 1)

    def test_falls_back_to_access_days_granted_when_period_end_absent(self) -> None:
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_4", status="succeeded", customer="cus_test_1"
        )
        invoice = _sdk_invoice(
            invoice_id="in_4", customer_id="cus_test_1", subscription_id="sub_4",
            amount_paid=400, lines=[_sdk_line(price_id="price_mo", period_end=None)],
        )
        self.stripe.v1.invoice_payments.list.return_value = _list_object(
            data=[_sdk_invoice_payment(invoice=invoice)]
        )
        before = timezone.now()

        fulfill_payment_intent(payment_intent_id="pi_4")

        self.subscription.refresh_from_db()
        self.assertGreaterEqual(
            self.subscription.access_expires_at, before + timedelta(days=30) - timedelta(seconds=5)
        )
        self.assertLessEqual(
            self.subscription.access_expires_at, before + timedelta(days=30) + timedelta(seconds=5)
        )

    def test_two_invoices_grant_two_periods(self) -> None:
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_5", status="succeeded", customer="cus_test_1"
        )
        now_ts = int(timezone.now().timestamp())
        invoice_a = _sdk_invoice(
            invoice_id="in_5a", customer_id="cus_test_1", subscription_id="sub_5",
            amount_paid=400, lines=[_sdk_line(price_id="price_mo", period_end=now_ts + 30 * 86400)],
        )
        invoice_b = _sdk_invoice(
            invoice_id="in_5b", customer_id="cus_test_1", subscription_id="sub_5",
            amount_paid=400, lines=[_sdk_line(price_id="price_mo", period_end=now_ts + 60 * 86400)],
        )
        self.stripe.v1.invoice_payments.list.return_value = _list_object(
            data=[_sdk_invoice_payment(invoice=invoice_a), _sdk_invoice_payment(invoice=invoice_b)]
        )

        granted = fulfill_payment_intent(payment_intent_id="pi_5")

        self.assertEqual(granted, 2)
        self.assertEqual(ProcessedPayment.objects.count(), 2)
        self.subscription.refresh_from_db()
        expected = datetime.fromtimestamp(now_ts + 60 * 86400, tz=dt_timezone.utc) + timedelta(days=2)
        self.assertEqual(self.subscription.access_expires_at, expected)

    def test_two_invoices_grant_two_periods_reversed_order(self) -> None:
        """
        Same as test_two_invoices_grant_two_periods but the InvoicePayment
        listing yields the longer-period invoice first -- Stripe's ordering
        for this case is not documented, and _grant_period_for_invoice must
        not care: access_expires_at is a max() over whatever it's handed, in
        whatever order.
        """
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_5r", status="succeeded", customer="cus_test_1"
        )
        now_ts = int(timezone.now().timestamp())
        invoice_a = _sdk_invoice(
            invoice_id="in_5ra", customer_id="cus_test_1", subscription_id="sub_5",
            amount_paid=400, lines=[_sdk_line(price_id="price_mo", period_end=now_ts + 30 * 86400)],
        )
        invoice_b = _sdk_invoice(
            invoice_id="in_5rb", customer_id="cus_test_1", subscription_id="sub_5",
            amount_paid=400, lines=[_sdk_line(price_id="price_mo", period_end=now_ts + 60 * 86400)],
        )
        self.stripe.v1.invoice_payments.list.return_value = _list_object(
            data=[_sdk_invoice_payment(invoice=invoice_b), _sdk_invoice_payment(invoice=invoice_a)]
        )

        granted = fulfill_payment_intent(payment_intent_id="pi_5r")

        self.assertEqual(granted, 2)
        self.assertEqual(ProcessedPayment.objects.count(), 2)
        self.subscription.refresh_from_db()
        expected = datetime.fromtimestamp(now_ts + 60 * 86400, tz=dt_timezone.utc) + timedelta(days=2)
        self.assertEqual(self.subscription.access_expires_at, expected)

    def test_same_invoice_processed_twice_grants_once(self) -> None:
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_6", status="succeeded", customer="cus_test_1"
        )
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _sdk_invoice(
            invoice_id="in_6", customer_id="cus_test_1", subscription_id="sub_6",
            amount_paid=400, lines=[_sdk_line(price_id="price_mo", period_end=period_end)],
        )
        self.stripe.v1.invoice_payments.list.side_effect = [
            _list_object(data=[_sdk_invoice_payment(invoice=invoice)]),
            _list_object(data=[_sdk_invoice_payment(invoice=invoice)]),
        ]

        first = fulfill_payment_intent(payment_intent_id="pi_6")
        second = fulfill_payment_intent(payment_intent_id="pi_6")

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(ProcessedPayment.objects.filter(stripe_invoice_id="in_6").count(), 1)
        self.subscription.refresh_from_db()
        expected = datetime.fromtimestamp(period_end, tz=dt_timezone.utc) + timedelta(days=2)
        self.assertEqual(self.subscription.access_expires_at, expected)


@override_settings(**_STRIPE_TEST_SETTINGS)
class FulfillCheckoutSessionTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.plan = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, billing_interval="month", access_days_granted=30,
        )
        self.user = User.objects.create_user(username="sessionuser", email="sessionuser@example.com")
        BetaSignup.objects.create(
            email="sessionuser@example.com", normalized_email="sessionuser@example.com",
            status="approved", invited_user=self.user,
        )
        self.subscription = Subscription.objects.create(user=self.user, stripe_customer_id="cus_session_1")

    def test_client_reference_id_mismatch_raises_ownership_error(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            id="cs_test_1", client_reference_id="999999", customer="cus_session_1", invoice=None, subscription=None,
        )
        with self.assertRaises(SessionOwnershipError):
            fulfill_checkout_session(session_id="cs_test_1", user=self.user)

    def test_unknown_session_id_raises_not_found(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.side_effect = stripe.InvalidRequestError(
            "no such session", None
        )
        with self.assertRaises(SessionNotFoundError):
            fulfill_checkout_session(session_id="cs_bogus", user=self.user)

    def test_grants_period_from_session_invoice(self) -> None:
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _sdk_invoice(
            invoice_id="in_session_1", customer_id="cus_session_1", subscription_id="sub_session_1",
            amount_paid=400, lines=[_sdk_line(price_id="price_mo", period_end=period_end)],
        )
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            id="cs_test_2", client_reference_id=str(self.user.id), customer="cus_session_1",
            invoice=invoice, subscription="sub_session_1",
        )

        result = fulfill_checkout_session(session_id="cs_test_2", user=self.user)

        self.assertTrue(result["granted"])
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["plan"], {"slug": "beta-monthly", "name": "Beta Monthly"})
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.stripe_subscription_id, "sub_session_1")

    def test_same_invoice_via_webhook_and_success_page_grants_once(self) -> None:
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _sdk_invoice(
            invoice_id="in_dual_1", customer_id="cus_session_1", subscription_id="sub_dual_1",
            amount_paid=400, lines=[_sdk_line(price_id="price_mo", period_end=period_end)],
        )
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_dual", status="succeeded", customer="cus_session_1"
        )
        self.stripe.v1.invoice_payments.list.return_value = _list_object(
            data=[_sdk_invoice_payment(invoice=invoice)]
        )
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            id="cs_dual", client_reference_id=str(self.user.id), customer="cus_session_1",
            invoice=invoice, subscription="sub_dual_1",
        )

        fulfill_payment_intent(payment_intent_id="pi_dual")
        self.subscription.refresh_from_db()
        first_expiry = self.subscription.access_expires_at

        fulfill_checkout_session(session_id="cs_dual", user=self.user)
        self.subscription.refresh_from_db()

        self.assertEqual(self.subscription.access_expires_at, first_expiry)
        self.assertEqual(ProcessedPayment.objects.filter(stripe_invoice_id="in_dual_1").count(), 1)

    def test_open_unpaid_invoice_grants_nothing(self) -> None:
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _sdk_invoice(
            invoice_id="in_unpaid_1", customer_id="cus_session_1", subscription_id="sub_unpaid_1",
            amount_paid=0, status="open", amount_remaining=400,
            lines=[_sdk_line(price_id="price_mo", period_end=period_end)],
        )
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            id="cs_unpaid_inv", client_reference_id=str(self.user.id), customer="cus_session_1",
            invoice=invoice, subscription="sub_unpaid_1", payment_status="paid",
        )

        result = fulfill_checkout_session(session_id="cs_unpaid_inv", user=self.user)

        self.assertFalse(result["granted"])
        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.access_expires_at)
        self.assertFalse(self.subscription.has_active_access)
        self.assertNotEqual(self.subscription.status, "active")
        self.assertEqual(ProcessedPayment.objects.count(), 0)

    def test_unpaid_session_grants_nothing(self) -> None:
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _sdk_invoice(
            invoice_id="in_unpaid_2", customer_id="cus_session_1", subscription_id="sub_unpaid_2",
            amount_paid=400, lines=[_sdk_line(price_id="price_mo", period_end=period_end)],
        )
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            id="cs_unpaid_sess", client_reference_id=str(self.user.id), customer="cus_session_1",
            invoice=invoice, subscription="sub_unpaid_2", payment_status="unpaid",
        )

        result = fulfill_checkout_session(session_id="cs_unpaid_sess", user=self.user)

        self.assertFalse(result["granted"])
        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.access_expires_at)
        self.assertEqual(ProcessedPayment.objects.count(), 0)

    def test_paid_session_grants_exactly_one_period(self) -> None:
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _sdk_invoice(
            invoice_id="in_paid_1", customer_id="cus_session_1", subscription_id="sub_paid_1",
            amount_paid=400, lines=[_sdk_line(price_id="price_mo", period_end=period_end)],
        )
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            id="cs_paid", client_reference_id=str(self.user.id), customer="cus_session_1",
            invoice=invoice, subscription="sub_paid_1", payment_status="paid",
        )

        fulfill_checkout_session(session_id="cs_paid", user=self.user)
        self.subscription.refresh_from_db()
        first_expiry = self.subscription.access_expires_at
        fulfill_checkout_session(session_id="cs_paid", user=self.user)
        self.subscription.refresh_from_db()

        self.assertIsNotNone(first_expiry)
        self.assertEqual(self.subscription.access_expires_at, first_expiry)
        self.assertEqual(ProcessedPayment.objects.filter(stripe_invoice_id="in_paid_1").count(), 1)

    def test_webhook_path_refuses_unpaid_invoice_too(self) -> None:
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _sdk_invoice(
            invoice_id="in_wh_open", customer_id="cus_session_1", subscription_id="sub_wh_open",
            amount_paid=0, status="open", amount_remaining=400,
            lines=[_sdk_line(price_id="price_mo", period_end=period_end)],
        )
        self.stripe.v1.payment_intents.retrieve.return_value = _sdk_payment_intent(
            pi_id="pi_wh_open", status="succeeded", customer="cus_session_1"
        )
        # No "status" field on the InvoicePayment itself (unlike _sdk_invoice_payment,
        # which always carries status="paid"): the point of this test is an invoice
        # payment whose *own* status is anything other than "paid", so
        # fulfill_payment_intent's re-fetch-on-race branch is never entered and this
        # exercises the plain "invoice.status != paid" rejection in
        # _grant_period_for_invoice instead.
        invoice_payment = stripe.InvoicePayment.construct_from(
            {"object": "invoice_payment", "invoice": invoice}, _KEY
        )
        self.stripe.v1.invoice_payments.list.return_value = _list_object(data=[invoice_payment])

        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_wh_open"), 0)
        self.subscription.refresh_from_db()
        self.assertIsNone(self.subscription.access_expires_at)


@override_settings(**_STRIPE_TEST_SETTINGS)
class BillingSessionViewTests(StripeClientTestCase):
    """
    HTTP-layer coverage for GET /api/v1/billing/session/<id>/, complementing
    FulfillCheckoutSessionTests (which calls billing.fulfill_checkout_session
    directly): confirms the view returns the same
    {status, plan, access_expires_at, has_active_access} shape as
    BillingSubscriptionView, plus "granted", and that ownership/not-found
    both collapse to an identical 404.
    """

    url_tpl = "/api/v1/billing/session/{}/"

    def setUp(self) -> None:
        super().setUp()
        self.plan = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, billing_interval="month", access_days_granted=30,
        )
        self.user = User.objects.create_user(username="sessionview", email="sessionview@example.com")
        BetaSignup.objects.create(
            email="sessionview@example.com", normalized_email="sessionview@example.com",
            status="approved", invited_user=self.user,
        )
        self.subscription = Subscription.objects.create(user=self.user, stripe_customer_id="cus_view_1")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_unauthenticated_returns_401(self) -> None:
        response = APIClient().get(self.url_tpl.format("cs_anything"))
        self.assertEqual(response.status_code, 401)

    def test_unknown_session_returns_404(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.side_effect = stripe.InvalidRequestError(
            "no such session", None
        )
        response = self.client.get(self.url_tpl.format("cs_bogus"))
        self.assertEqual(response.status_code, 404)

    def test_other_users_session_returns_404_not_403(self) -> None:
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            id="cs_not_mine", client_reference_id="999999", customer="cus_view_1", invoice=None, subscription=None,
        )
        response = self.client.get(self.url_tpl.format("cs_not_mine"))
        self.assertEqual(response.status_code, 404)

    def test_success_returns_shared_subscription_shape_plus_granted(self) -> None:
        period_end = int(timezone.now().timestamp()) + 30 * 86400
        invoice = _sdk_invoice(
            invoice_id="in_view_1", customer_id="cus_view_1", subscription_id="sub_view_1",
            amount_paid=400, lines=[_sdk_line(price_id="price_mo", period_end=period_end)],
        )
        self.stripe.v1.checkout.sessions.retrieve.return_value = _session(
            id="cs_view_1", client_reference_id=str(self.user.id), customer="cus_view_1",
            invoice=invoice, subscription="sub_view_1",
        )

        response = self.client.get(self.url_tpl.format("cs_view_1"))

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body.keys()),
            {"status", "plan", "access_expires_at", "has_active_access", "exempt", "can_manage_billing", "quota", "granted"},
        )
        self.assertTrue(body["granted"])
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["plan"], {"slug": "beta-monthly", "name": "Beta Monthly"})
        self.assertTrue(body["has_active_access"])

    def test_active_access_short_circuits_without_calling_stripe(self) -> None:
        self.subscription.status = "active"
        self.subscription.plan = self.plan
        self.subscription.access_expires_at = timezone.now() + timedelta(days=10)
        self.subscription.save()

        response = self.client.get(self.url_tpl.format("cs_already_active"))

        self.stripe.v1.checkout.sessions.retrieve.assert_not_called()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body.keys()),
            {"status", "plan", "access_expires_at", "has_active_access", "exempt", "can_manage_billing", "quota", "granted"},
        )
        self.assertTrue(body["has_active_access"])
        self.assertFalse(body["granted"])

    def test_throttled_after_scope_limit(self) -> None:
        from django.core.cache import cache
        cache.clear()
        self.stripe.v1.checkout.sessions.retrieve.side_effect = stripe.InvalidRequestError(
            "no such session", None
        )
        codes = [self.client.get(self.url_tpl.format("cs_x")).status_code for _ in range(31)]
        self.assertEqual(codes[:30], [404] * 30)
        self.assertEqual(codes[30], 429)
        cache.clear()


@override_settings(**_SETTINGS)
class UnsettledInvoiceRaceTests(StripeClientTestCase):
    """
    Verified live: payment_intent.succeeded can arrive while the expanded
    invoice still reads status='open'. The paid gate in
    _grant_period_for_invoice must stay strict; fulfill_payment_intent
    re-fetches, and raises a retryable error if the invoice is still open.
    """

    def setUp(self) -> None:
        super().setUp()
        self.monthly, self.annual = _make_plans()
        self.user = _make_user(username="racer", customer_id="cus_test_1")
        self.period_end = int(time.time()) + 30 * 86400

    def test_stale_open_invoice_is_refetched_and_granted(self) -> None:
        self.stripe.v1.payment_intents.retrieve.return_value = _payment_intent()
        self.stripe.v1.invoice_payments.list.return_value = _invoice_payment_list(
            invoice_dicts=[_open_invoice_dict(invoice_id="in_race", period_end=self.period_end)]
        )
        self.stripe.v1.invoices.retrieve.return_value = _paid_invoice(
            invoice_id="in_race", period_end=self.period_end
        )

        granted = fulfill_payment_intent(payment_intent_id="pi_1")

        self.assertEqual(granted, 1)
        self.stripe.v1.invoices.retrieve.assert_called_once()
        self.assertEqual(self.stripe.v1.invoices.retrieve.call_args.args[0], "in_race")
        subscription = Subscription.objects.get(user=self.user)
        self.assertEqual(subscription.status, "active")
        self.assertEqual(
            subscription.access_expires_at,
            datetime.fromtimestamp(self.period_end, tz=dt_timezone.utc) + timedelta(days=2),
        )

    def test_already_paid_invoice_is_not_refetched(self) -> None:
        self.stripe.v1.payment_intents.retrieve.return_value = _payment_intent()
        self.stripe.v1.invoice_payments.list.return_value = _invoice_payment_list(
            invoice_dicts=[_paid_invoice_dict(invoice_id="in_ok", period_end=self.period_end)]
        )

        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 1)
        self.stripe.v1.invoices.retrieve.assert_not_called()

    def test_still_open_after_refetch_raises_and_grants_nothing(self) -> None:
        self.stripe.v1.payment_intents.retrieve.return_value = _payment_intent()
        open_dict = _open_invoice_dict(invoice_id="in_open", period_end=self.period_end)
        self.stripe.v1.invoice_payments.list.return_value = _invoice_payment_list(invoice_dicts=[open_dict])
        self.stripe.v1.invoices.retrieve.return_value = stripe.Invoice.construct_from(open_dict, _KEY)

        with self.assertRaises(InvoiceNotSettledError):
            fulfill_payment_intent(payment_intent_id="pi_1")

        self.assertEqual(ProcessedPayment.objects.count(), 0)
        self.assertIsNone(Subscription.objects.get(user=self.user).access_expires_at)

    def test_settled_sibling_is_granted_before_the_unsettled_one_raises(self) -> None:
        self.stripe.v1.payment_intents.retrieve.return_value = _payment_intent()
        paid_dict = _paid_invoice_dict(invoice_id="in_good", period_end=self.period_end)
        open_dict = _open_invoice_dict(invoice_id="in_stuck", period_end=self.period_end + 86400)
        self.stripe.v1.invoice_payments.list.return_value = _invoice_payment_list(
            invoice_dicts=[open_dict, paid_dict]
        )
        self.stripe.v1.invoices.retrieve.return_value = stripe.Invoice.construct_from(open_dict, _KEY)

        with self.assertRaises(InvoiceNotSettledError):
            fulfill_payment_intent(payment_intent_id="pi_1")

        self.assertEqual(
            list(ProcessedPayment.objects.values_list("stripe_invoice_id", flat=True)), ["in_good"]
        )

    def test_genuinely_unpaid_invoice_payment_never_grants(self) -> None:
        self.stripe.v1.payment_intents.retrieve.return_value = _payment_intent()
        self.stripe.v1.invoice_payments.list.return_value = _invoice_payment_list(
            invoice_dicts=[_open_invoice_dict(invoice_id="in_unpaid", period_end=self.period_end)],
            payment_status="canceled",
        )

        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 0)
        self.stripe.v1.invoices.retrieve.assert_not_called()
        self.assertEqual(ProcessedPayment.objects.count(), 0)

    def test_paid_gate_in_grant_function_is_not_loosened(self) -> None:
        open_invoice = stripe.Invoice.construct_from(
            _open_invoice_dict(invoice_id="in_gate", period_end=self.period_end), _KEY
        )
        self.assertFalse(
            _grant_period_for_invoice(invoice=open_invoice, expected_customer_id="cus_test_1")
        )
        self.assertEqual(ProcessedPayment.objects.count(), 0)


@override_settings(**_SETTINGS)
class BoundaryInvoiceGrantTests(StripeClientTestCase):
    """The scheduled-change boundary invoice is an ordinary paid subscription invoice."""

    def test_boundary_invoice_switches_plan_and_extends_to_period_end_plus_grace(self) -> None:
        monthly, annual = _make_plans()
        user = _make_user(username="switcher", customer_id="cus_test_1")
        Subscription.objects.filter(user=user).update(plan=monthly)
        self.stripe.v1.payment_intents.retrieve.return_value = _payment_intent()
        period_end = int(time.time()) + 365 * 86400
        invoice_dict = _paid_invoice_dict(invoice_id="in_boundary", period_end=period_end, price_id="price_yr")
        invoice_dict["billing_reason"] = "subscription_update"
        self.stripe.v1.invoice_payments.list.return_value = _invoice_payment_list(invoice_dicts=[invoice_dict])

        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 1)

        subscription = Subscription.objects.get(user=user)
        self.assertEqual(subscription.plan, annual)
        self.assertEqual(
            subscription.access_expires_at,
            datetime.fromtimestamp(period_end, tz=dt_timezone.utc) + timedelta(days=2),
        )


@override_settings(**_STRIPE_TEST_SETTINGS)
class GrantPeriodAllowRevokedTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, billing_interval="month", access_days_granted=30,
        )
        user = User.objects.create_user(username="disputed", email="disputed@example.com")
        BetaSignup.objects.create(
            email="disputed@example.com", normalized_email="disputed@example.com",
            status="converted", invited_user=user,
        )
        self.subscription = Subscription.objects.create(
            user=user, stripe_customer_id="cus_disp", status="revoked"
        )
        self.invoice = _sdk_invoice(
            invoice_id="in_disp", customer_id="cus_disp", subscription_id="sub_disp", amount_paid=400,
            lines=[_sdk_line(price_id="price_mo", period_end=int(timezone.now().timestamp()) + 30 * 86400)],
        )

    def test_late_low_invoice_after_revoke_does_not_un_revoke(self) -> None:
        """
        A revoke sets access_expires_at to "now" (see revoke_for_dispute). If
        an old, lower invoice for the same (now-revoked) customer settles
        late -- computed expiry earlier than the revoke timestamp -- it must
        not become the new maximum, and therefore must not flip status back
        to "active". Without the produces_new_max guard on status this would
        silently un-revoke a disputed row.
        """
        revoke_time = timezone.now()
        user = User.objects.create_user(username="revoke_late", email="revoke_late@example.com")
        BetaSignup.objects.create(
            email="revoke_late@example.com", normalized_email="revoke_late@example.com",
            status="converted", invited_user=user,
        )
        subscription = Subscription.objects.create(
            user=user, stripe_customer_id="cus_revlate", status="revoked", access_expires_at=revoke_time,
        )
        old_invoice = _sdk_invoice(
            invoice_id="in_revlate_old", customer_id="cus_revlate", subscription_id="sub_revlate",
            amount_paid=400,
            lines=[
                _sdk_line(
                    price_id="price_mo",
                    period_end=int((revoke_time - timedelta(days=10)).timestamp()),
                )
            ],
        )

        self.assertTrue(_grant_period_for_invoice(invoice=old_invoice))  # allow_revoked defaults True

        subscription.refresh_from_db()
        self.assertEqual(subscription.status, "revoked")
        self.assertEqual(subscription.access_expires_at, revoke_time)
        self.assertEqual(ProcessedPayment.objects.filter(stripe_invoice_id="in_revlate_old").count(), 1)

    def test_revoked_is_not_regranted_when_disallowed(self) -> None:
        self.assertFalse(_grant_period_for_invoice(invoice=self.invoice, allow_revoked=False))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")
        self.assertIsNone(self.subscription.access_expires_at)
        self.assertEqual(ProcessedPayment.objects.count(), 0)

    def test_revoked_customer_can_resubscribe_by_default(self) -> None:
        self.assertTrue(_grant_period_for_invoice(invoice=self.invoice))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "active")
        self.assertEqual(ProcessedPayment.objects.count(), 1)

    def test_reconcile_passes_allow_revoked_false(self) -> None:
        from fragrance.billing import reconcile_subscriptions

        Subscription.objects.filter(pk=self.subscription.pk).update(status="active")
        self.stripe.v1.invoices.list.return_value = _list_object(data=[self.invoice])
        with patch("fragrance.billing._grant_period_for_invoice", return_value=True) as grant:
            reconcile_subscriptions()
        self.assertIs(grant.call_args.kwargs["allow_revoked"], False)

    def test_dispute_landing_mid_reconcile_is_not_overwritten(self) -> None:
        """
        reconcile_subscriptions snapshots known_customers *before* it calls
        client.v1.invoices.list(...). A side_effect on that call reproduces the
        same race with a real stripe.ListObject as the eventual return value:
        the customer is already a candidate by the time this side_effect runs
        (the snapshot happened earlier), and the dispute revoke landing here
        is exactly "before the grant reaches the row lock".
        """
        from fragrance.billing import reconcile_subscriptions

        Subscription.objects.filter(pk=self.subscription.pk).update(status="active")

        def _revoke_then_list(*args, **kwargs) -> stripe.ListObject:
            Subscription.objects.filter(pk=self.subscription.pk).update(status="revoked")
            return _list_object(data=[self.invoice])

        self.stripe.v1.invoices.list.side_effect = _revoke_then_list
        reconcile_subscriptions()
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "revoked")
        self.assertEqual(ProcessedPayment.objects.count(), 0)


class GrantMonotonicityTests(TestCase):
    """
    access_expires_at, plan and stripe_subscription_id must land on the same
    final state regardless of the order two invoices for the same customer
    are applied in -- the projection (see _grant_period_for_invoice's
    docstring) must be commutative, not just idempotent. Modeled on the
    lapsed-restart scenario: a newer annual invoice applied, then an older
    monthly invoice for a different (lapsed) Stripe subscription settling
    late must not roll access, plan or stripe_subscription_id backwards.
    """

    def setUp(self) -> None:
        self.monthly = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, billing_interval="month", access_days_granted=30,
        )
        self.annual = Plan.objects.create(
            name="Beta Annual", slug="beta-annual", stripe_price_id="price_yr",
            amount_cents=4400, billing_interval="year", access_days_granted=365,
        )
        self.user = User.objects.create_user(username="outoforder", email="outoforder@example.com")
        BetaSignup.objects.create(
            email="outoforder@example.com", normalized_email="outoforder@example.com",
            status="converted", invited_user=self.user,
        )
        self.subscription = Subscription.objects.create(user=self.user, stripe_customer_id="cus_ooo")
        now_ts = int(timezone.now().timestamp())
        self.newer_period_end = now_ts + 365 * 86400
        self.older_period_end = now_ts + 10 * 86400
        self.newer_invoice = _sdk_invoice(
            invoice_id="in_ooo_new", customer_id="cus_ooo", subscription_id="sub_new",
            amount_paid=4400, lines=[_sdk_line(price_id="price_yr", period_end=self.newer_period_end)],
        )
        self.older_invoice = _sdk_invoice(
            invoice_id="in_ooo_old", customer_id="cus_ooo", subscription_id="sub_old",
            amount_paid=400, lines=[_sdk_line(price_id="price_mo", period_end=self.older_period_end)],
        )
        self.expected_expiry = (
            datetime.fromtimestamp(self.newer_period_end, tz=dt_timezone.utc) + timedelta(days=2)
        )

    def test_newer_then_older_does_not_roll_back(self) -> None:
        self.assertTrue(_grant_period_for_invoice(invoice=self.newer_invoice))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.access_expires_at, self.expected_expiry)
        self.assertEqual(self.subscription.plan, self.annual)
        self.assertEqual(self.subscription.stripe_subscription_id, "sub_new")

        self.assertTrue(_grant_period_for_invoice(invoice=self.older_invoice))
        self.subscription.refresh_from_db()
        # The older, lower invoice still processed (it was a real payment)
        # but must not move expiry, plan or stripe_subscription_id backwards.
        self.assertEqual(self.subscription.access_expires_at, self.expected_expiry)
        self.assertEqual(self.subscription.plan, self.annual)
        self.assertEqual(self.subscription.stripe_subscription_id, "sub_new")
        self.assertEqual(self.subscription.status, "active")
        self.assertEqual(ProcessedPayment.objects.count(), 2)

    def test_older_then_newer_lands_on_same_final_state(self) -> None:
        self.assertTrue(_grant_period_for_invoice(invoice=self.older_invoice))
        self.assertTrue(_grant_period_for_invoice(invoice=self.newer_invoice))
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.access_expires_at, self.expected_expiry)
        self.assertEqual(self.subscription.plan, self.annual)
        self.assertEqual(self.subscription.stripe_subscription_id, "sub_new")
        self.assertEqual(self.subscription.status, "active")
        self.assertEqual(ProcessedPayment.objects.count(), 2)

    def test_processed_payment_period_end_records_this_invoices_own_period(self) -> None:
        """
        ProcessedPayment.period_end is a per-invoice historical record, not
        the applied maximum -- the older invoice's row must still read its
        own (shorter) period, even though it never became the subscription's
        access_expires_at.
        """
        _grant_period_for_invoice(invoice=self.newer_invoice)
        _grant_period_for_invoice(invoice=self.older_invoice)
        older_record = ProcessedPayment.objects.get(stripe_invoice_id="in_ooo_old")
        expected_older = (
            datetime.fromtimestamp(self.older_period_end, tz=dt_timezone.utc) + timedelta(days=2)
        )
        self.assertEqual(older_record.period_end, expected_older)


class GrantComplimentaryAccessTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="comp", password="pw12345!")

    def test_grants_active_access_and_logs_warning(self) -> None:
        with self.assertLogs("fragrance.billing", level="WARNING") as logs:
            subscription = grant_complimentary_access(
                user=self.user, days=30, reason="grandfathered beta user"
            )
        self.assertTrue(subscription.has_active_access)
        self.assertIn("grandfathered beta user", logs.output[0])
        self.assertIn("comp", logs.output[0])

    def test_extends_from_later_of_now_and_current_expiry(self) -> None:
        existing = active_subscription_for(self.user, days=10)
        before = existing.access_expires_at
        subscription = grant_complimentary_access(user=self.user, days=5, reason="extend")
        self.assertEqual(subscription.access_expires_at, before + timedelta(days=5))

    def test_refuses_revoked_subscription(self) -> None:
        subscription = active_subscription_for(self.user)
        subscription.status = "revoked"
        subscription.save()
        with self.assertRaises(ComplimentaryGrantError):
            grant_complimentary_access(user=self.user, days=5, reason="x")

    def test_rejects_non_positive_days_and_blank_reason(self) -> None:
        with self.assertRaises(ComplimentaryGrantError):
            grant_complimentary_access(user=self.user, days=0, reason="x")
        with self.assertRaises(ComplimentaryGrantError):
            grant_complimentary_access(user=self.user, days=5, reason="   ")

    def test_command_requires_reason(self) -> None:
        with self.assertRaises(CommandError):
            call_command("grant_access", username="comp", days=30)
        self.assertFalse(Subscription.objects.filter(user=self.user).exists())

    def test_command_grants_access(self) -> None:
        out = StringIO()
        call_command(
            "grant_access", username="comp", days=30, reason="operator comp", stdout=out
        )
        self.assertTrue(Subscription.objects.get(user=self.user).has_active_access)

    def test_command_unknown_user_errors(self) -> None:
        with self.assertRaises(CommandError):
            call_command("grant_access", username="ghost", days=30, reason="x")
