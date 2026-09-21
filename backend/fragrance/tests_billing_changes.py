"""
Tests for the webhook-race fix (InvoiceNotSettledError), the Stripe Customer
Portal endpoint, the configure_stripe_portal command and the deferred plan
change endpoints.

Every Stripe object here is a real SDK object built with
`stripe.<Class>.construct_from`, never a SimpleNamespace or a bare dict: the
project's earlier billing bugs were all mock-vs-real-object mismatches
(StripeObject has no `.get()`, nested expandables, etc.).
"""

import time
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

import stripe
from celery.exceptions import Retry
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.cache.backends.locmem import LocMemCache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .billing import (
    InvoiceNotSettledError,
    _grant_period_for_invoice,
    fulfill_payment_intent,
)
from .models import BetaSignup, Plan, ProcessedPayment, RecommendationRun, StripeEvent, Subscription
from .tasks import process_stripe_event, send_beta_invite_email, send_recommendation_email
from .views import FailOpenScopedRateThrottle

_KEY = "sk_test_dummy"
_SETTINGS = {
    "STRIPE_SECRET_KEY": _KEY,
    "STRIPE_WEBHOOK_SECRET": "whsec_test_dummy",
    "STRIPE_API_VERSION": "2026-08-26.dahlia",
    "STRIPE_PORTAL_CONFIGURATION_ID": "bpc_test_config",
    "FRONTEND_URL": "https://app.example.test",
}


def _invoice_dict(
    *,
    invoice_id: str,
    customer_id: str,
    subscription_id: str,
    price_id: str,
    period_end: int,
    status: str,
    amount_remaining: int,
    amount_paid: int = 0,
) -> dict:
    return {
        "id": invoice_id,
        "object": "invoice",
        "customer": customer_id,
        "status": status,
        "amount_remaining": amount_remaining,
        "amount_paid": amount_paid,
        "parent": {
            "type": "subscription_details",
            "subscription_details": {"subscription": subscription_id},
        },
        "lines": {
            "object": "list",
            "has_more": False,
            "url": f"/v1/invoices/{invoice_id}/lines",
            "data": [
                {
                    "id": f"il_{invoice_id}",
                    "object": "line_item",
                    "pricing": {
                        "type": "price_details",
                        "price_details": {"price": price_id, "product": "prod_x"},
                    },
                    "period": {"start": period_end - 86400 * 30, "end": period_end},
                }
            ],
        },
    }


def _paid_invoice_dict(*, invoice_id: str, period_end: int, price_id: str = "price_mo") -> dict:
    return _invoice_dict(
        invoice_id=invoice_id, customer_id="cus_test_1", subscription_id="sub_1",
        price_id=price_id, period_end=period_end, status="paid",
        amount_remaining=0, amount_paid=400,
    )


def _paid_invoice(*, invoice_id: str, period_end: int, price_id: str = "price_mo") -> stripe.Invoice:
    return stripe.Invoice.construct_from(
        _paid_invoice_dict(invoice_id=invoice_id, period_end=period_end, price_id=price_id), _KEY
    )


def _open_invoice_dict(*, invoice_id: str, period_end: int, price_id: str = "price_mo") -> dict:
    return _invoice_dict(
        invoice_id=invoice_id, customer_id="cus_test_1", subscription_id="sub_1",
        price_id=price_id, period_end=period_end, status="open", amount_remaining=400,
    )


def _invoice_payment_list(*, invoice_dicts: list[dict], payment_status: str = "paid") -> stripe.ListObject:
    return stripe.ListObject.construct_from(
        {
            "object": "list",
            "has_more": False,
            "url": "/v1/invoice_payments",
            "data": [
                {
                    "id": f"inpay_{i}",
                    "object": "invoice_payment",
                    "status": payment_status,
                    "invoice": invoice_dict,
                }
                for i, invoice_dict in enumerate(invoice_dicts)
            ],
        },
        _KEY,
    )


def _payment_intent(*, customer: str = "cus_test_1") -> stripe.PaymentIntent:
    return stripe.PaymentIntent.construct_from(
        {"id": "pi_1", "object": "payment_intent", "status": "succeeded", "customer": customer},
        _KEY,
    )


def _make_user(*, username: str, customer_id: str | None, approved: bool = True) -> User:
    user = User.objects.create_user(username=username, email=f"{username}@example.com", password="pw12345!")
    if approved:
        BetaSignup.objects.create(
            email=user.email, normalized_email=user.email, status="converted", invited_user=user
        )
    Subscription.objects.create(user=user, stripe_customer_id=customer_id)
    return user


def _make_plans() -> tuple[Plan, Plan]:
    monthly = Plan.objects.create(
        name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
        amount_cents=400, billing_interval="month", access_days_granted=30,
    )
    annual = Plan.objects.create(
        name="Beta Annual", slug="beta-annual", stripe_price_id="price_yr",
        amount_cents=4400, billing_interval="year", access_days_granted=365,
    )
    return monthly, annual


@override_settings(**_SETTINGS)
class UnsettledInvoiceRaceTests(TestCase):
    """
    Verified live: payment_intent.succeeded can arrive while the expanded
    invoice still reads status='open'. The paid gate in
    _grant_period_for_invoice must stay strict; fulfill_payment_intent
    re-fetches, and raises a retryable error if the invoice is still open.
    """

    def setUp(self) -> None:
        self.monthly, self.annual = _make_plans()
        self.user = _make_user(username="racer", customer_id="cus_test_1")
        self.period_end = int(time.time()) + 30 * 86400

    @patch("fragrance.billing.stripe.Invoice.retrieve")
    @patch("fragrance.billing.stripe.InvoicePayment.list")
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_stale_open_invoice_is_refetched_and_granted(self, mock_pi, mock_list, mock_invoice_retrieve) -> None:
        mock_pi.return_value = _payment_intent()
        mock_list.return_value = _invoice_payment_list(
            invoice_dicts=[_open_invoice_dict(invoice_id="in_race", period_end=self.period_end)]
        )
        mock_invoice_retrieve.return_value = _paid_invoice(invoice_id="in_race", period_end=self.period_end)

        granted = fulfill_payment_intent(payment_intent_id="pi_1")

        self.assertEqual(granted, 1)
        mock_invoice_retrieve.assert_called_once()
        self.assertEqual(mock_invoice_retrieve.call_args.args[0], "in_race")
        subscription = Subscription.objects.get(user=self.user)
        self.assertEqual(subscription.status, "active")
        self.assertEqual(
            subscription.access_expires_at,
            datetime.fromtimestamp(self.period_end, tz=dt_timezone.utc) + timedelta(days=2),
        )

    @patch("fragrance.billing.stripe.Invoice.retrieve")
    @patch("fragrance.billing.stripe.InvoicePayment.list")
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_already_paid_invoice_is_not_refetched(self, mock_pi, mock_list, mock_invoice_retrieve) -> None:
        mock_pi.return_value = _payment_intent()
        mock_list.return_value = _invoice_payment_list(
            invoice_dicts=[_paid_invoice_dict(invoice_id="in_ok", period_end=self.period_end)]
        )

        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 1)
        mock_invoice_retrieve.assert_not_called()

    @patch("fragrance.billing.stripe.Invoice.retrieve")
    @patch("fragrance.billing.stripe.InvoicePayment.list")
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_still_open_after_refetch_raises_and_grants_nothing(self, mock_pi, mock_list, mock_invoice_retrieve) -> None:
        mock_pi.return_value = _payment_intent()
        open_dict = _open_invoice_dict(invoice_id="in_open", period_end=self.period_end)
        mock_list.return_value = _invoice_payment_list(invoice_dicts=[open_dict])
        mock_invoice_retrieve.return_value = stripe.Invoice.construct_from(open_dict, _KEY)

        with self.assertRaises(InvoiceNotSettledError):
            fulfill_payment_intent(payment_intent_id="pi_1")

        self.assertEqual(ProcessedPayment.objects.count(), 0)
        self.assertIsNone(Subscription.objects.get(user=self.user).access_expires_at)

    @patch("fragrance.billing.stripe.Invoice.retrieve")
    @patch("fragrance.billing.stripe.InvoicePayment.list")
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_settled_sibling_is_granted_before_the_unsettled_one_raises(
        self, mock_pi, mock_list, mock_invoice_retrieve
    ) -> None:
        mock_pi.return_value = _payment_intent()
        paid_dict = _paid_invoice_dict(invoice_id="in_good", period_end=self.period_end)
        open_dict = _open_invoice_dict(invoice_id="in_stuck", period_end=self.period_end + 86400)
        mock_list.return_value = _invoice_payment_list(
            invoice_dicts=[open_dict, paid_dict]
        )
        mock_invoice_retrieve.return_value = stripe.Invoice.construct_from(open_dict, _KEY)

        with self.assertRaises(InvoiceNotSettledError):
            fulfill_payment_intent(payment_intent_id="pi_1")

        self.assertEqual(
            list(ProcessedPayment.objects.values_list("stripe_invoice_id", flat=True)), ["in_good"]
        )

    @patch("fragrance.billing.stripe.Invoice.retrieve")
    @patch("fragrance.billing.stripe.InvoicePayment.list")
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_genuinely_unpaid_invoice_payment_never_grants(self, mock_pi, mock_list, mock_invoice_retrieve) -> None:
        mock_pi.return_value = _payment_intent()
        mock_list.return_value = _invoice_payment_list(
            invoice_dicts=[_open_invoice_dict(invoice_id="in_unpaid", period_end=self.period_end)],
            payment_status="canceled",
        )

        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 0)
        mock_invoice_retrieve.assert_not_called()
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
class ProcessStripeEventUnsettledRetryTests(TestCase):
    def setUp(self) -> None:
        self.event = StripeEvent.objects.create(
            stripe_event_id="evt_race", event_type="payment_intent.succeeded",
            payload={"data": {"object": {"id": "pi_1"}}},
        )

    def _run(self, *, retries: int) -> None:
        process_stripe_event.push_request(retries=retries)
        try:
            process_stripe_event(stripe_event_id="evt_race")
        finally:
            process_stripe_event.pop_request()

    @patch("fragrance.billing.fulfill_payment_intent", side_effect=InvoiceNotSettledError("in_x"))
    def test_unsettled_retries_on_a_short_ladder(self, _mock_fulfill) -> None:
        for retries, expected_countdown in ((0, 30), (1, 60), (2, 120)):
            with self.subTest(retries=retries):
                with patch.object(process_stripe_event, "retry", side_effect=Retry()) as mock_retry:
                    with self.assertRaises(Retry):
                        self._run(retries=retries)
                self.assertEqual(mock_retry.call_args.kwargs["countdown"], expected_countdown)
                self.event.refresh_from_db()
                self.assertEqual(self.event.status, "failed")

    @patch("fragrance.billing.fulfill_payment_intent", side_effect=RuntimeError("boom"))
    def test_other_errors_keep_the_300_second_countdown(self, _mock_fulfill) -> None:
        with patch.object(process_stripe_event, "retry", side_effect=Retry()) as mock_retry:
            with self.assertRaises(Retry):
                self._run(retries=0)
        self.assertEqual(mock_retry.call_args.kwargs["countdown"], 300)

    @patch("fragrance.billing.fulfill_payment_intent", side_effect=InvoiceNotSettledError("in_x"))
    def test_exhausted_retries_raise_and_leave_the_event_failed(self, _mock_fulfill) -> None:
        with patch.object(process_stripe_event, "retry") as mock_retry:
            with self.assertRaises(InvoiceNotSettledError):
                self._run(retries=process_stripe_event.max_retries)
        mock_retry.assert_not_called()
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, "failed")

    def test_event_ends_processed_after_a_successful_retry(self) -> None:
        with patch(
            "fragrance.billing.fulfill_payment_intent",
            side_effect=[InvoiceNotSettledError("in_x"), 1],
        ):
            with patch.object(process_stripe_event, "retry", side_effect=Retry()):
                with self.assertRaises(Retry):
                    self._run(retries=0)
            self.event.refresh_from_db()
            self.assertEqual(self.event.status, "failed")
            self._run(retries=1)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, "processed")
        self.assertIsNotNone(self.event.processed_at)
        self.assertEqual(self.event.error, "")


# ---------------------------------------------------------------------------
# Real-object builders for the portal / schedule tests
# ---------------------------------------------------------------------------

_NOW = int(time.time())
_PERIOD_START = _NOW - 5 * 86400
_PERIOD_END = _NOW + 25 * 86400


def _phase_dict(*, start: int, end: int, price_id: str) -> dict:
    return {
        "start_date": start,
        "end_date": end,
        "items": [{"price": price_id, "quantity": 1}],
    }


def _schedule_dict(
    *,
    schedule_id: str = "sub_sched_1",
    status: str = "active",
    phases: list[dict],
    current_start: int | None,
) -> dict:
    current = next((p for p in phases if p["start_date"] == current_start), None)
    return {
        "id": schedule_id,
        "object": "subscription_schedule",
        "status": status,
        "end_behavior": "release",
        "current_phase": (
            {"start_date": current["start_date"], "end_date": current["end_date"]} if current else None
        ),
        "phases": phases,
    }


def _pending_schedule_dict(*, current_price: str = "price_mo", target_price: str = "price_yr") -> dict:
    return _schedule_dict(
        phases=[
            _phase_dict(start=_PERIOD_START, end=_PERIOD_END, price_id=current_price),
            _phase_dict(start=_PERIOD_END, end=_PERIOD_END + 365 * 86400, price_id=target_price),
        ],
        current_start=_PERIOD_START,
    )


def _spent_schedule_dict(*, price: str = "price_yr") -> dict:
    """A schedule sitting on its final phase: the change already happened."""
    return _schedule_dict(
        phases=[
            _phase_dict(start=_PERIOD_START - 30 * 86400, end=_PERIOD_START, price_id="price_mo"),
            _phase_dict(start=_PERIOD_START, end=_PERIOD_END, price_id=price),
        ],
        current_start=_PERIOD_START,
    )


def _stripe_subscription(
    *,
    price_id: str = "price_mo",
    status: str = "active",
    schedule: dict | None = None,
    cancel_at_period_end: bool = False,
    sub_id: str = "sub_local",
) -> stripe.Subscription:
    return stripe.Subscription.construct_from(
        {
            "id": sub_id,
            "object": "subscription",
            "status": status,
            "cancel_at_period_end": cancel_at_period_end,
            "cancel_at": None,
            "schedule": schedule,
            "items": {
                "object": "list",
                "has_more": False,
                "url": "/v1/subscription_items",
                "data": [
                    {
                        "id": "si_1",
                        "object": "subscription_item",
                        "price": {"id": price_id, "object": "price"},
                        "quantity": 1,
                        "current_period_start": _PERIOD_START,
                        "current_period_end": _PERIOD_END,
                    }
                ],
            },
        },
        _KEY,
    )


def _real_schedule(*, schedule: dict) -> stripe.SubscriptionSchedule:
    return stripe.SubscriptionSchedule.construct_from(schedule, _KEY)


def _portal_session(*, url: str = "https://billing.stripe.com/p/session/test_abc") -> stripe.billing_portal.Session:
    return stripe.billing_portal.Session.construct_from(
        {"id": "bps_1", "object": "billing_portal.session", "url": url}, _KEY
    )


def _price(*, price_id: str, interval: str | None, interval_count: int = 1) -> stripe.Price:
    data: dict = {"id": price_id, "object": "price", "recurring": None}
    if interval is not None:
        data["recurring"] = {"interval": interval, "interval_count": interval_count}
    return stripe.Price.construct_from(data, _KEY)


def _price_for(price_id: str, **_kwargs: object) -> stripe.Price:
    return _price(price_id=price_id, interval={"price_mo": "month", "price_yr": "year"}[price_id])


def _active_user(*, username: str, customer_id: str = "cus_a", sub_id: str | None = "sub_local") -> User:
    user = User.objects.create_user(username=username, email=f"{username}@example.com", password="pw12345!")
    Subscription.objects.create(
        user=user,
        stripe_customer_id=customer_id,
        stripe_subscription_id=sub_id,
        status="active",
        access_expires_at=timezone.now() + timedelta(days=20),
    )
    return user


@override_settings(**_SETTINGS)
class BoundaryInvoiceGrantTests(TestCase):
    """The scheduled-change boundary invoice is an ordinary paid subscription invoice."""

    @patch("fragrance.billing.stripe.InvoicePayment.list")
    @patch("fragrance.billing.stripe.PaymentIntent.retrieve")
    def test_boundary_invoice_switches_plan_and_extends_to_period_end_plus_grace(self, mock_pi, mock_list) -> None:
        monthly, annual = _make_plans()
        user = _make_user(username="switcher", customer_id="cus_test_1")
        Subscription.objects.filter(user=user).update(plan=monthly)
        mock_pi.return_value = _payment_intent()
        period_end = int(time.time()) + 365 * 86400
        invoice_dict = _paid_invoice_dict(invoice_id="in_boundary", period_end=period_end, price_id="price_yr")
        invoice_dict["billing_reason"] = "subscription_update"
        mock_list.return_value = _invoice_payment_list(invoice_dicts=[invoice_dict])

        self.assertEqual(fulfill_payment_intent(payment_intent_id="pi_1"), 1)

        subscription = Subscription.objects.get(user=user)
        self.assertEqual(subscription.plan, annual)
        self.assertEqual(
            subscription.access_expires_at,
            datetime.fromtimestamp(period_end, tz=dt_timezone.utc) + timedelta(days=2),
        )


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


@override_settings(**_SETTINGS)
class BillingPortalViewTests(TestCase):
    url = "/api/v1/billing/portal/"

    def setUp(self) -> None:
        self.user = _active_user(username="portaluser", customer_id="cus_mine")
        self.other = _active_user(username="someoneelse", customer_id="cus_theirs", sub_id="sub_theirs")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_unauthenticated_returns_401(self) -> None:
        self.assertEqual(APIClient().post(self.url).status_code, 401)

    def test_get_is_not_allowed(self) -> None:
        self.assertEqual(self.client.get(self.url).status_code, 405)

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    @patch("fragrance.billing.stripe.billing_portal.Session.create")
    def test_returns_url_for_the_callers_own_customer_with_server_built_params(
        self, mock_create, mock_retrieve
    ) -> None:
        mock_retrieve.return_value = _stripe_subscription()
        mock_create.return_value = _portal_session()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"url": "https://billing.stripe.com/p/session/test_abc"})
        mock_create.assert_called_once_with(
            customer="cus_mine",
            return_url="https://app.example.test/billing",
            configuration="bpc_test_config",
        )

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    @patch("fragrance.billing.stripe.billing_portal.Session.create")
    def test_client_supplied_fields_are_ignored(self, mock_create, mock_retrieve) -> None:
        mock_retrieve.return_value = _stripe_subscription()
        mock_create.return_value = _portal_session()

        self.client.post(
            self.url,
            {
                "customer": "cus_theirs",
                "configuration": "bpc_evil",
                "return_url": "https://evil.example/",
                "flow_data": {"type": "subscription_cancel"},
            },
            format="json",
        )

        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["customer"], "cus_mine")
        self.assertEqual(kwargs["configuration"], "bpc_test_config")
        self.assertEqual(kwargs["return_url"], "https://app.example.test/billing")
        self.assertNotIn("flow_data", kwargs)

    @override_settings(STRIPE_PORTAL_CONFIGURATION_ID="", DEBUG=True)
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    @patch("fragrance.billing.stripe.billing_portal.Session.create")
    def test_omits_configuration_when_unset_in_debug(self, mock_create, mock_retrieve) -> None:
        mock_retrieve.return_value = _stripe_subscription()
        mock_create.return_value = _portal_session()
        with self.assertLogs("fragrance.billing", level="WARNING"):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("configuration", mock_create.call_args.kwargs)

    @patch("fragrance.billing.stripe.billing_portal.Session.create")
    def test_no_customer_id_returns_409_no_billing_account(self, mock_create) -> None:
        Subscription.objects.filter(user=self.user).update(stripe_customer_id=None)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "no_billing_account")
        mock_create.assert_not_called()

    @patch("fragrance.billing.stripe.billing_portal.Session.create")
    def test_no_subscription_row_returns_409_no_billing_account(self, mock_create) -> None:
        fresh = User.objects.create_user(username="fresh", password="pw12345!")
        self.client.force_authenticate(user=fresh)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "no_billing_account")
        mock_create.assert_not_called()

    @patch("fragrance.billing.stripe.billing_portal.Session.create")
    def test_staff_returns_409_exempt_without_calling_stripe(self, mock_create) -> None:
        User.objects.filter(pk=self.user.pk).update(is_staff=True)
        self.user.refresh_from_db()
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "exempt")
        mock_create.assert_not_called()

    @override_settings(STRIPE_SECRET_KEY="")
    def test_unconfigured_stripe_returns_503(self) -> None:
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 503)

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    @patch("fragrance.billing.stripe.billing_portal.Session.create")
    def test_stripe_error_returns_502_without_leaking_the_message(self, mock_create, mock_retrieve) -> None:
        mock_retrieve.return_value = _stripe_subscription()
        mock_create.side_effect = stripe.APIConnectionError("secret-detail sk_test_leak")
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("secret-detail", response.content.decode())
        self.assertNotIn("sk_test_leak", response.content.decode())

    @patch("fragrance.billing.stripe.SubscriptionSchedule.release")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    @patch("fragrance.billing.stripe.billing_portal.Session.create")
    def test_spent_schedule_shell_is_released_before_the_session(
        self, mock_create, mock_retrieve, mock_release
    ) -> None:
        mock_retrieve.return_value = _stripe_subscription(
            price_id="price_yr", schedule=_spent_schedule_dict()
        )
        mock_create.return_value = _portal_session()
        self.client.post(self.url)
        mock_release.assert_called_once_with("sub_sched_1")

    @patch("fragrance.billing.stripe.SubscriptionSchedule.release")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    @patch("fragrance.billing.stripe.billing_portal.Session.create")
    def test_pending_schedule_is_not_released_by_the_portal(
        self, mock_create, mock_retrieve, mock_release
    ) -> None:
        mock_retrieve.return_value = _stripe_subscription(schedule=_pending_schedule_dict())
        mock_create.return_value = _portal_session()
        self.client.post(self.url)
        mock_release.assert_not_called()

    @patch("fragrance.billing.stripe.SubscriptionSchedule.release")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    @patch("fragrance.billing.stripe.billing_portal.Session.create")
    def test_failure_to_release_a_spent_shell_does_not_block_the_portal(
        self, mock_create, mock_retrieve, mock_release
    ) -> None:
        mock_retrieve.return_value = _stripe_subscription(
            price_id="price_yr", schedule=_spent_schedule_dict()
        )
        mock_release.side_effect = stripe.APIConnectionError("down")
        mock_create.return_value = _portal_session()
        with self.assertLogs("fragrance.billing", level="WARNING"):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    @patch("fragrance.billing.stripe.billing_portal.Session.create")
    def test_throttled_after_the_scope_limit(self, mock_create, mock_retrieve) -> None:
        mock_retrieve.return_value = _stripe_subscription()
        mock_create.return_value = _portal_session()
        with patch.object(FailOpenScopedRateThrottle, "cache", LocMemCache("portal-throttle", {})), patch.object(
            FailOpenScopedRateThrottle, "THROTTLE_RATES", {"billing-portal": "2/min"}
        ):
            statuses = [self.client.post(self.url).status_code for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 429])


def _configuration(*, config_id: str, metadata: dict) -> stripe.billing_portal.Configuration:
    return stripe.billing_portal.Configuration.construct_from(
        {"id": config_id, "object": "billing_portal.configuration", "metadata": metadata}, _KEY
    )


def _configuration_list(*, configurations: list) -> stripe.ListObject:
    return stripe.ListObject.construct_from(
        {
            "object": "list",
            "has_more": False,
            "url": "/v1/billing_portal/configurations",
            "data": [
                {"id": c.id, "object": "billing_portal.configuration", "metadata": c.metadata.to_dict()}
                for c in configurations
            ],
        },
        _KEY,
    )


_OURS = {"app": "fragrances", "managed_by": "configure_stripe_portal"}


@override_settings(**_SETTINGS)
class ConfigureStripePortalCommandTests(TestCase):
    def _run(self, *args: str) -> str:
        out = StringIO()
        call_command("configure_stripe_portal", *args, stdout=out)
        return out.getvalue()

    @patch("fragrance.billing.stripe.billing_portal.Configuration.create")
    @patch("fragrance.billing.stripe.billing_portal.Configuration.list")
    def test_creates_when_none_is_ours_and_ignores_foreign_configurations(self, mock_list, mock_create) -> None:
        mock_list.return_value = _configuration_list(
            configurations=[
                _configuration(config_id="bpc_default", metadata={}),
                _configuration(config_id="bpc_other", metadata={"app": "fragrances"}),
            ]
        )
        mock_create.return_value = _configuration(config_id="bpc_new", metadata=_OURS)

        output = self._run()

        self.assertIn("bpc_new", output)
        mock_list.assert_called_once_with(limit=100, active=True)
        kwargs = mock_create.call_args.kwargs
        features = kwargs["features"]
        self.assertTrue(features["payment_method_update"]["enabled"])
        self.assertTrue(features["invoice_history"]["enabled"])
        self.assertEqual(features["customer_update"], {"enabled": True, "allowed_updates": ["email"]})
        cancel = features["subscription_cancel"]
        self.assertEqual((cancel["enabled"], cancel["mode"], cancel["proration_behavior"]), (True, "at_period_end", "none"))
        self.assertFalse(features["subscription_update"]["enabled"])
        self.assertFalse(features["subscription_pause"]["enabled"])
        self.assertFalse(kwargs["login_page"]["enabled"])
        self.assertEqual(kwargs["business_profile"]["headline"], "Manage your Fragrances subscription")
        self.assertEqual(kwargs["business_profile"]["terms_of_service_url"], "https://app.example.test/terms")
        self.assertEqual(kwargs["business_profile"]["privacy_policy_url"], "https://app.example.test/privacy")
        self.assertEqual(
            kwargs["features"]["subscription_cancel"]["cancellation_reason"],
            {
                "enabled": True,
                "options": ["too_expensive", "missing_features", "unused", "switched_service", "other"],
            },
        )
        self.assertEqual(kwargs["metadata"], _OURS)
        self.assertNotIn("default_return_url", kwargs.get("business_profile", {}))

    @patch("fragrance.billing.stripe.billing_portal.Configuration.create")
    @patch("fragrance.billing.stripe.billing_portal.Configuration.modify")
    @patch("fragrance.billing.stripe.billing_portal.Configuration.list")
    def test_rerun_modifies_the_existing_configuration_instead_of_creating_another(
        self, mock_list, mock_modify, mock_create
    ) -> None:
        mock_list.return_value = _configuration_list(
            configurations=[_configuration(config_id="bpc_ours", metadata=_OURS)]
        )
        mock_modify.return_value = _configuration(config_id="bpc_ours", metadata=_OURS)

        output = self._run()

        self.assertIn("Updated portal configuration bpc_ours", output)
        self.assertEqual(mock_modify.call_args.args[0], "bpc_ours")
        mock_create.assert_not_called()

    @patch("fragrance.billing.stripe.billing_portal.Configuration.create")
    @patch("fragrance.billing.stripe.billing_portal.Configuration.modify")
    @patch("fragrance.billing.stripe.billing_portal.Configuration.list")
    def test_dry_run_writes_nothing(self, mock_list, mock_modify, mock_create) -> None:
        mock_list.return_value = _configuration_list(configurations=[])
        output = self._run("--dry-run")
        self.assertIn("would create", output)
        mock_create.assert_not_called()
        mock_modify.assert_not_called()

    @patch("fragrance.billing.stripe.billing_portal.Configuration.list")
    def test_two_tagged_configurations_is_an_error(self, mock_list) -> None:
        mock_list.return_value = _configuration_list(
            configurations=[
                _configuration(config_id="bpc_1", metadata=_OURS),
                _configuration(config_id="bpc_2", metadata=_OURS),
            ]
        )
        with self.assertRaises(CommandError):
            self._run()

    @override_settings(STRIPE_SECRET_KEY="sk_live_dummy")
    @patch("fragrance.billing.stripe.billing_portal.Configuration.create")
    def test_live_key_requires_yes(self, mock_create) -> None:
        with self.assertRaises(CommandError):
            self._run()
        mock_create.assert_not_called()

    @override_settings(STRIPE_SECRET_KEY="")
    def test_missing_key_is_an_error(self) -> None:
        with self.assertRaises(CommandError):
            self._run()


@override_settings(**_SETTINGS)
class BillingChangePlanViewTests(TestCase):
    url = "/api/v1/billing/change-plan/"

    def setUp(self) -> None:
        self.monthly, self.annual = _make_plans()
        self.user = _active_user(username="changer")
        Subscription.objects.filter(user=self.user).update(plan=self.monthly)
        self.other = _active_user(username="bystander", customer_id="cus_b", sub_id="sub_bystander")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        price_patcher = patch("fragrance.billing.stripe.Price.retrieve", side_effect=_price_for)
        self.mock_price = price_patcher.start()
        self.addCleanup(price_patcher.stop)
        self.addCleanup(cache.clear)

    # -- auth / eligibility ------------------------------------------------

    def test_unauthenticated_is_401_on_every_method(self) -> None:
        anon = APIClient()
        self.assertEqual(anon.get(self.url).status_code, 401)
        self.assertEqual(anon.post(self.url, {"plan": "beta-annual"}, format="json").status_code, 401)
        self.assertEqual(anon.delete(self.url).status_code, 401)

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_staff_is_409_exempt_on_every_method(self, mock_retrieve) -> None:
        User.objects.filter(pk=self.user.pk).update(is_staff=True)
        self.user.refresh_from_db()
        self.client.force_authenticate(user=self.user)
        for response in (
            self.client.get(self.url),
            self.client.post(self.url, {"plan": "beta-annual"}, format="json"),
            self.client.delete(self.url),
        ):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["code"], "exempt")
        mock_retrieve.assert_not_called()

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_lapsed_or_missing_subscription_is_409_no_active_subscription(self, mock_retrieve) -> None:
        Subscription.objects.filter(user=self.user).update(access_expires_at=timezone.now() - timedelta(days=1))
        for response in (
            self.client.get(self.url),
            self.client.post(self.url, {"plan": "beta-annual"}, format="json"),
        ):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["code"], "no_active_subscription")
        mock_retrieve.assert_not_called()

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_no_stripe_subscription_id_is_409_no_active_subscription(self, mock_retrieve) -> None:
        Subscription.objects.filter(user=self.user).update(stripe_subscription_id=None)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "no_active_subscription")
        mock_retrieve.assert_not_called()

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_stripe_side_canceled_subscription_is_409(self, mock_retrieve) -> None:
        mock_retrieve.return_value = _stripe_subscription(status="canceled")
        self.assertEqual(self.client.get(self.url).json()["code"], "no_active_subscription")
        self.assertEqual(
            self.client.post(self.url, {"plan": "beta-annual"}, format="json").json()["code"],
            "no_active_subscription",
        )

    # -- GET ---------------------------------------------------------------

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_get_without_pending_lists_other_plans_and_period_end(self, mock_retrieve) -> None:
        mock_retrieve.return_value = _stripe_subscription()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body["pending"])
        self.assertEqual([p["slug"] for p in body["available"]], ["beta-annual"])
        self.assertEqual(
            set(body["available"][0]),
            {"slug", "name", "amount_cents", "currency", "billing_interval"},
        )
        self.assertEqual(
            datetime.fromisoformat(body["current_period_end"].replace("Z", "+00:00")),
            datetime.fromtimestamp(_PERIOD_END, tz=dt_timezone.utc),
        )
        self.assertIs(body["cancel_at_period_end"], False)
        mock_retrieve.assert_called_once()
        self.assertEqual(mock_retrieve.call_args.args[0], "sub_local")

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_get_with_pending_returns_flat_pending_and_no_available(self, mock_retrieve) -> None:
        mock_retrieve.return_value = _stripe_subscription(schedule=_pending_schedule_dict())
        body = self.client.get(self.url).json()
        self.assertEqual(body["available"], [])
        self.assertEqual(set(body["pending"]), {"slug", "name", "effective_at"})
        self.assertEqual(body["pending"]["slug"], "beta-annual")
        self.assertEqual(body["pending"]["name"], "Beta Annual")
        self.assertEqual(
            datetime.fromisoformat(body["pending"]["effective_at"].replace("Z", "+00:00")),
            datetime.fromtimestamp(_PERIOD_END, tz=dt_timezone.utc),
        )

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_get_treats_a_spent_schedule_shell_as_no_pending_change(self, mock_retrieve) -> None:
        mock_retrieve.return_value = _stripe_subscription(price_id="price_yr", schedule=_spent_schedule_dict())
        body = self.client.get(self.url).json()
        self.assertIsNone(body["pending"])
        self.assertEqual([p["slug"] for p in body["available"]], ["beta-monthly"])

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_get_ignores_a_canceled_schedule_left_on_the_subscription(self, mock_retrieve) -> None:
        canceled = _pending_schedule_dict()
        canceled["status"] = "canceled"
        mock_retrieve.return_value = _stripe_subscription(schedule=canceled)
        self.assertIsNone(self.client.get(self.url).json()["pending"])

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_get_offers_nothing_when_the_subscription_is_set_to_cancel(self, mock_retrieve) -> None:
        mock_retrieve.return_value = _stripe_subscription(cancel_at_period_end=True)
        body = self.client.get(self.url).json()
        self.assertEqual(body["available"], [])
        self.assertIs(body["cancel_at_period_end"], True)

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_get_excludes_inactive_plans(self, mock_retrieve) -> None:
        Plan.objects.filter(pk=self.annual.pk).update(is_active=False)
        mock_retrieve.return_value = _stripe_subscription()
        self.assertEqual(self.client.get(self.url).json()["available"], [])

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_get_stripe_error_is_502_without_leak(self, mock_retrieve) -> None:
        mock_retrieve.side_effect = stripe.APIConnectionError("boom sk_test_leak")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("sk_test_leak", response.content.decode())

    @override_settings(STRIPE_SECRET_KEY="")
    def test_get_unconfigured_is_503(self) -> None:
        self.assertEqual(self.client.get(self.url).status_code, 503)

    # -- POST --------------------------------------------------------------

    def _post(self, plan: str = "beta-annual", **extra: object):
        return self.client.post(self.url, {"plan": plan, **extra}, format="json")

    @patch("fragrance.billing.stripe.SubscriptionSchedule.modify")
    @patch("fragrance.billing.stripe.SubscriptionSchedule.create")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_post_builds_the_verified_two_phase_no_proration_schedule(
        self, mock_retrieve, mock_create, mock_modify
    ) -> None:
        mock_retrieve.return_value = _stripe_subscription()
        mock_create.return_value = _real_schedule(
            schedule=_schedule_dict(
                phases=[_phase_dict(start=_PERIOD_START, end=_PERIOD_END, price_id="price_mo")],
                current_start=_PERIOD_START,
            )
        )
        mock_modify.return_value = _real_schedule(schedule=_pending_schedule_dict())

        response = self._post(price_id="price_evil")

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["pending"]["slug"], "beta-annual")
        self.assertEqual(body["pending"]["name"], "Beta Annual")
        mock_create.assert_called_once_with(from_subscription="sub_local")
        self.assertEqual(mock_modify.call_args.args[0], "sub_sched_1")
        kwargs = mock_modify.call_args.kwargs
        self.assertEqual(kwargs["end_behavior"], "release")
        self.assertEqual(kwargs["proration_behavior"], "none")
        first, second = kwargs["phases"]
        self.assertEqual(
            first,
            {
                "items": [{"price": "price_mo", "quantity": 1}],
                "start_date": _PERIOD_START,
                "end_date": _PERIOD_END,
            },
        )
        self.assertEqual(
            second,
            {
                "items": [{"price": "price_yr", "quantity": 1}],
                "duration": {"interval": "year", "interval_count": 1},
                "billing_cycle_anchor": "phase_start",
            },
        )
        self.assertNotIn("price_evil", str(kwargs))

    @patch("fragrance.billing.stripe.SubscriptionSchedule.modify")
    @patch("fragrance.billing.stripe.SubscriptionSchedule.create")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_annual_to_monthly_uses_a_month_duration(self, mock_retrieve, mock_create, mock_modify) -> None:
        mock_retrieve.return_value = _stripe_subscription(price_id="price_yr")
        mock_create.return_value = _real_schedule(
            schedule=_schedule_dict(
                phases=[_phase_dict(start=_PERIOD_START, end=_PERIOD_END, price_id="price_yr")],
                current_start=_PERIOD_START,
            )
        )
        mock_modify.return_value = _real_schedule(
            schedule=_pending_schedule_dict(current_price="price_yr", target_price="price_mo")
        )

        response = self._post("beta-monthly")

        self.assertEqual(response.status_code, 201)
        second = mock_modify.call_args.kwargs["phases"][1]
        self.assertEqual(second["duration"], {"interval": "month", "interval_count": 1})
        self.assertEqual(second["billing_cycle_anchor"], "phase_start")
        self.assertEqual(second["items"], [{"price": "price_mo", "quantity": 1}])

    def test_post_missing_plan_is_400(self) -> None:
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 400)

    def test_post_price_id_alone_is_400(self) -> None:
        self.assertEqual(self.client.post(self.url, {"price_id": "price_yr"}, format="json").status_code, 400)

    @patch("fragrance.billing.stripe.SubscriptionSchedule.create")
    def test_post_unknown_or_inactive_slug_is_400(self, mock_create) -> None:
        self.assertEqual(self._post("nope").status_code, 400)
        Plan.objects.filter(pk=self.annual.pk).update(is_active=False)
        self.assertEqual(self._post("beta-annual").status_code, 400)
        mock_create.assert_not_called()

    @patch("fragrance.billing.stripe.SubscriptionSchedule.create")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_post_same_plan_is_409(self, mock_retrieve, mock_create) -> None:
        mock_retrieve.return_value = _stripe_subscription()
        response = self._post("beta-monthly")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "same_plan")
        mock_create.assert_not_called()

    @patch("fragrance.billing.stripe.SubscriptionSchedule.create")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_post_with_existing_pending_change_is_409_already_scheduled(self, mock_retrieve, mock_create) -> None:
        mock_retrieve.return_value = _stripe_subscription(schedule=_pending_schedule_dict())
        response = self._post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "already_scheduled")
        mock_create.assert_not_called()

    @patch("fragrance.billing.stripe.SubscriptionSchedule.create")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_post_when_set_to_cancel_is_409_cancellation_scheduled(self, mock_retrieve, mock_create) -> None:
        mock_retrieve.return_value = _stripe_subscription(cancel_at_period_end=True)
        response = self._post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "cancellation_scheduled")
        mock_create.assert_not_called()

    @patch("fragrance.billing.stripe.SubscriptionSchedule.modify")
    @patch("fragrance.billing.stripe.SubscriptionSchedule.create")
    @patch("fragrance.billing.stripe.SubscriptionSchedule.release")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_post_releases_a_spent_shell_then_schedules(
        self, mock_retrieve, mock_release, mock_create, mock_modify
    ) -> None:
        mock_retrieve.return_value = _stripe_subscription(price_id="price_yr", schedule=_spent_schedule_dict())
        mock_create.return_value = _real_schedule(
            schedule=_schedule_dict(
                schedule_id="sub_sched_2",
                phases=[_phase_dict(start=_PERIOD_START, end=_PERIOD_END, price_id="price_yr")],
                current_start=_PERIOD_START,
            )
        )
        mock_modify.return_value = _real_schedule(
            schedule=_pending_schedule_dict(current_price="price_yr", target_price="price_mo")
        )
        response = self._post("beta-monthly")
        self.assertEqual(response.status_code, 201)
        mock_release.assert_called_once_with("sub_sched_1")

    @patch("fragrance.billing.stripe.SubscriptionSchedule.create")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_post_losing_a_double_submit_race_is_409_already_scheduled(self, mock_retrieve, mock_create) -> None:
        mock_retrieve.side_effect = [
            _stripe_subscription(),
            _stripe_subscription(schedule=_pending_schedule_dict()),
        ]
        mock_create.side_effect = stripe.InvalidRequestError("already has a schedule", None)
        response = self._post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "already_scheduled")

    @patch("fragrance.billing.stripe.SubscriptionSchedule.release")
    @patch("fragrance.billing.stripe.SubscriptionSchedule.modify")
    @patch("fragrance.billing.stripe.SubscriptionSchedule.create")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_post_releases_the_half_built_schedule_when_modify_fails(
        self, mock_retrieve, mock_create, mock_modify, mock_release
    ) -> None:
        mock_retrieve.return_value = _stripe_subscription()
        mock_create.return_value = _real_schedule(
            schedule=_schedule_dict(
                phases=[_phase_dict(start=_PERIOD_START, end=_PERIOD_END, price_id="price_mo")],
                current_start=_PERIOD_START,
            )
        )
        mock_modify.side_effect = stripe.InvalidRequestError("nope sk_test_leak", None)
        response = self._post()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("sk_test_leak", response.content.decode())
        mock_release.assert_called_once_with("sub_sched_1")

    @patch("fragrance.billing.stripe.SubscriptionSchedule.create")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_post_only_ever_touches_the_callers_own_subscription(self, mock_retrieve, mock_create) -> None:
        mock_retrieve.return_value = _stripe_subscription(schedule=_pending_schedule_dict())
        self._post(subscription="sub_bystander", customer="cus_b")
        self.assertEqual(mock_retrieve.call_args.args[0], "sub_local")

    @override_settings(STRIPE_SECRET_KEY="")
    def test_post_unconfigured_is_503(self) -> None:
        self.assertEqual(self._post().status_code, 503)

    # -- DELETE ------------------------------------------------------------

    @patch("fragrance.billing.stripe.SubscriptionSchedule.release")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_delete_releases_the_pending_schedule_with_204(self, mock_retrieve, mock_release) -> None:
        mock_retrieve.return_value = _stripe_subscription(schedule=_pending_schedule_dict())
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        mock_release.assert_called_once_with("sub_sched_1")

    @patch("fragrance.billing.stripe.SubscriptionSchedule.release")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_delete_with_nothing_pending_is_404(self, mock_retrieve, mock_release) -> None:
        mock_retrieve.return_value = _stripe_subscription()
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "no_pending_change")
        mock_release.assert_not_called()

    @patch("fragrance.billing.stripe.SubscriptionSchedule.release")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_delete_does_not_treat_a_spent_shell_as_a_pending_change(self, mock_retrieve, mock_release) -> None:
        mock_retrieve.return_value = _stripe_subscription(price_id="price_yr", schedule=_spent_schedule_dict())
        self.assertEqual(self.client.delete(self.url).status_code, 404)
        mock_release.assert_not_called()

    @patch("fragrance.billing.stripe.SubscriptionSchedule.release")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_delete_release_error_is_502(self, mock_retrieve, mock_release) -> None:
        mock_retrieve.return_value = _stripe_subscription(schedule=_pending_schedule_dict())
        mock_release.side_effect = stripe.APIConnectionError("down")
        self.assertEqual(self.client.delete(self.url).status_code, 502)

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_delete_without_a_stripe_subscription_is_404(self, mock_retrieve) -> None:
        Subscription.objects.filter(user=self.user).update(stripe_subscription_id=None)
        self.assertEqual(self.client.delete(self.url).status_code, 404)
        mock_retrieve.assert_not_called()

    # -- throttle ----------------------------------------------------------

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_throttled_after_the_scope_limit(self, mock_retrieve) -> None:
        mock_retrieve.return_value = _stripe_subscription()
        with patch.object(FailOpenScopedRateThrottle, "cache", LocMemCache("plan-throttle", {})), patch.object(
            FailOpenScopedRateThrottle, "THROTTLE_RATES", {"billing-change-plan": "2/min"}
        ):
            statuses = [self.client.get(self.url).status_code for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 429])


# ---------------------------------------------------------------------------
# Review-fix regression tests: lock, fail-closed portal, price-derived phase,
# Stripe budget, email-task retries
# ---------------------------------------------------------------------------

_SCHEDULE_PATCHES = {
    "retrieve": "fragrance.billing.stripe.Subscription.retrieve",
    "create": "fragrance.billing.stripe.SubscriptionSchedule.create",
    "modify": "fragrance.billing.stripe.SubscriptionSchedule.modify",
    "release": "fragrance.billing.stripe.SubscriptionSchedule.release",
}


def _bare_schedule() -> stripe.SubscriptionSchedule:
    return _real_schedule(
        schedule=_schedule_dict(
            phases=[_phase_dict(start=_PERIOD_START, end=_PERIOD_END, price_id="price_mo")],
            current_start=_PERIOD_START,
        )
    )


@override_settings(**_SETTINGS)
class PlanChangeHardeningTests(TestCase):
    url = "/api/v1/billing/change-plan/"
    portal_url = "/api/v1/billing/portal/"

    def setUp(self) -> None:
        self.monthly, self.annual = _make_plans()
        self.user = _active_user(username="hardened")
        Subscription.objects.filter(user=self.user).update(plan=self.monthly)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.lock_key = f"plan-change-lock:{self.user.pk}"
        price_patcher = patch("fragrance.billing.stripe.Price.retrieve", side_effect=_price_for)
        self.mock_price = price_patcher.start()
        self.addCleanup(price_patcher.stop)
        self.addCleanup(cache.clear)

    def _post(self, plan: str = "beta-annual"):
        return self.client.post(self.url, {"plan": plan}, format="json")

    # -- lock: change-plan ----------------------------------------------------

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_post_while_locked_is_409_change_in_progress_and_touches_no_stripe(self, mock_retrieve) -> None:
        cache.add(self.lock_key, "1", timeout=60)
        response = self._post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "change_in_progress")
        mock_retrieve.assert_not_called()
        self.assertIsNotNone(cache.get(self.lock_key), "a contended request must not release someone else's lock")

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_delete_while_locked_is_409_change_in_progress(self, mock_retrieve) -> None:
        cache.add(self.lock_key, "1", timeout=60)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "change_in_progress")
        mock_retrieve.assert_not_called()

    def test_lock_is_per_user(self) -> None:
        other = _active_user(username="neighbour", customer_id="cus_n", sub_id="sub_neighbour")
        cache.add(f"plan-change-lock:{other.pk}", "1", timeout=60)
        with patch(_SCHEDULE_PATCHES["retrieve"]) as mock_retrieve:
            mock_retrieve.return_value = _stripe_subscription(price_id="price_yr")
            self.assertEqual(self._post("beta-monthly").status_code, 502)  # reached Stripe (create unmocked -> not 409)
        mock_retrieve.assert_called()

    @patch("fragrance.billing.stripe.SubscriptionSchedule.modify")
    @patch("fragrance.billing.stripe.SubscriptionSchedule.create")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_lock_is_held_during_the_stripe_calls_and_released_after_success(
        self, mock_retrieve, mock_create, mock_modify
    ) -> None:
        held: list[bool] = []
        mock_retrieve.side_effect = lambda *a, **k: (
            held.append(cache.get(self.lock_key) is not None) or _stripe_subscription()
        )
        mock_create.return_value = _bare_schedule()
        mock_modify.return_value = _real_schedule(schedule=_pending_schedule_dict())
        self.assertEqual(self._post().status_code, 201)
        self.assertEqual(held, [True])
        self.assertIsNone(cache.get(self.lock_key))

    @patch("fragrance.billing.stripe.SubscriptionSchedule.release")
    @patch("fragrance.billing.stripe.SubscriptionSchedule.modify")
    @patch("fragrance.billing.stripe.SubscriptionSchedule.create")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_lock_is_released_after_a_stripe_failure(self, mock_retrieve, mock_create, mock_modify, _rel) -> None:
        mock_retrieve.return_value = _stripe_subscription()
        mock_create.return_value = _bare_schedule()
        mock_modify.side_effect = stripe.APIConnectionError("down")
        self.assertEqual(self._post().status_code, 502)
        self.assertIsNone(cache.get(self.lock_key))

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_lock_is_released_after_a_domain_refusal(self, mock_retrieve) -> None:
        mock_retrieve.return_value = _stripe_subscription(schedule=_pending_schedule_dict())
        self.assertEqual(self._post().status_code, 409)
        self.assertIsNone(cache.get(self.lock_key))

    @patch("fragrance.billing.stripe.SubscriptionSchedule.release")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_delete_releases_the_lock(self, mock_retrieve, mock_release) -> None:
        mock_retrieve.return_value = _stripe_subscription(schedule=_pending_schedule_dict())
        self.assertEqual(self.client.delete(self.url).status_code, 204)
        self.assertIsNone(cache.get(self.lock_key))

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_cache_failure_fails_closed_with_503_on_post_and_delete(self, mock_retrieve) -> None:
        with patch("fragrance.billing.cache.add", side_effect=ConnectionError("redis down")):
            post = self._post()
            delete = self.client.delete(self.url)
        self.assertEqual(post.status_code, 503)
        self.assertEqual(delete.status_code, 503)
        self.assertNotIn("redis", post.content.decode().lower())
        mock_retrieve.assert_not_called()

    # -- lock: portal ---------------------------------------------------------

    @patch("fragrance.billing.stripe.SubscriptionSchedule.release")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    @patch("fragrance.billing.stripe.billing_portal.Session.create")
    def test_portal_while_locked_skips_the_release_but_still_opens(
        self, mock_create, mock_retrieve, mock_release
    ) -> None:
        cache.add(self.lock_key, "1", timeout=60)
        mock_create.return_value = _portal_session()
        response = self.client.post(self.portal_url)
        self.assertEqual(response.status_code, 200)
        mock_retrieve.assert_not_called()
        mock_release.assert_not_called()
        self.assertIsNotNone(cache.get(self.lock_key))

    @patch("fragrance.billing.stripe.SubscriptionSchedule.release")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    @patch("fragrance.billing.stripe.billing_portal.Session.create")
    def test_portal_cache_failure_skips_the_release_but_still_opens(
        self, mock_create, mock_retrieve, mock_release
    ) -> None:
        mock_create.return_value = _portal_session()
        with patch("fragrance.billing.cache.add", side_effect=ConnectionError("redis down")):
            response = self.client.post(self.portal_url)
        self.assertEqual(response.status_code, 200)
        mock_release.assert_not_called()

    @patch("fragrance.billing.stripe.SubscriptionSchedule.release")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    @patch("fragrance.billing.stripe.billing_portal.Session.create")
    def test_portal_releases_the_lock_after_its_release(self, mock_create, mock_retrieve, mock_release) -> None:
        mock_retrieve.return_value = _stripe_subscription(price_id="price_yr", schedule=_spent_schedule_dict())
        mock_create.return_value = _portal_session()
        self.assertEqual(self.client.post(self.portal_url).status_code, 200)
        mock_release.assert_called_once_with("sub_sched_1")
        self.assertIsNone(cache.get(self.lock_key))

    # -- portal fails closed without a configuration id ----------------------

    @override_settings(STRIPE_PORTAL_CONFIGURATION_ID="", DEBUG=False)
    @patch("fragrance.billing.stripe.SubscriptionSchedule.release")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    @patch("fragrance.billing.stripe.billing_portal.Session.create")
    def test_portal_without_configuration_id_is_503_outside_debug_and_mutates_nothing(
        self, mock_create, mock_retrieve, mock_release
    ) -> None:
        response = self.client.post(self.portal_url)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Billing management is temporarily unavailable."})
        mock_create.assert_not_called()
        mock_retrieve.assert_not_called()
        mock_release.assert_not_called()

    # -- phase duration derived from the Stripe price --------------------------

    @patch("fragrance.billing.stripe.SubscriptionSchedule.modify")
    @patch("fragrance.billing.stripe.SubscriptionSchedule.create")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_duration_comes_from_the_stripe_price(self, mock_retrieve, mock_create, mock_modify) -> None:
        mock_retrieve.return_value = _stripe_subscription()
        mock_create.return_value = _bare_schedule()
        mock_modify.return_value = _real_schedule(schedule=_pending_schedule_dict())
        self.mock_price.side_effect = lambda price_id, **k: _price(price_id=price_id, interval="year", interval_count=2)
        self.assertEqual(self._post().status_code, 201)
        self.mock_price.assert_called_once_with("price_yr")
        self.assertEqual(
            mock_modify.call_args.kwargs["phases"][1]["duration"], {"interval": "year", "interval_count": 2}
        )

    @patch("fragrance.billing.stripe.SubscriptionSchedule.create")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_interval_mismatch_is_502_before_anything_is_created(self, mock_retrieve, mock_create) -> None:
        mock_retrieve.return_value = _stripe_subscription()
        self.mock_price.side_effect = lambda price_id, **k: _price(price_id=price_id, interval="month")
        response = self._post()
        self.assertEqual(response.status_code, 502)
        mock_create.assert_not_called()
        self.assertIsNone(cache.get(self.lock_key))
        self.assertNotIn("month", response.content.decode())

    @patch("fragrance.billing.stripe.SubscriptionSchedule.create")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_price_without_a_recurring_interval_is_502(self, mock_retrieve, mock_create) -> None:
        mock_retrieve.return_value = _stripe_subscription()
        self.mock_price.side_effect = lambda price_id, **k: _price(price_id=price_id, interval=None)
        self.assertEqual(self._post().status_code, 502)
        mock_create.assert_not_called()

    @patch("fragrance.billing.stripe.SubscriptionSchedule.create")
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_price_lookup_failure_is_502(self, mock_retrieve, mock_create) -> None:
        mock_retrieve.return_value = _stripe_subscription()
        self.mock_price.side_effect = stripe.APIConnectionError("down")
        self.assertEqual(self._post().status_code, 502)
        mock_create.assert_not_called()

    # -- empty subscription items ----------------------------------------------

    @patch("fragrance.billing.stripe.Subscription.retrieve")
    def test_get_with_no_subscription_items_is_a_clean_409(self, mock_retrieve) -> None:
        sub = _stripe_subscription()
        sub["items"]["data"] = []
        mock_retrieve.return_value = sub
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "no_active_subscription")


class StripeInteractiveBudgetTests(TestCase):
    def test_timeout_and_retries_are_capped_only_inside_the_budget(self) -> None:
        from . import billing

        client = billing._HTTP_CLIENT
        self.assertEqual(client._timeout, 20)
        with billing._interactive_stripe_budget():
            self.assertEqual(client._timeout, billing._INTERACTIVE_TIMEOUT_SECONDS)
            with patch("stripe._http_client.HTTPClient.request_with_retries", return_value=("", 200, {})) as mock_super:
                client.request_with_retries("get", "https://x", {}, None, 2)
            self.assertEqual(mock_super.call_args.args[4], billing._INTERACTIVE_MAX_NETWORK_RETRIES)
        self.assertEqual(client._timeout, 20)
        with patch("stripe._http_client.HTTPClient.request_with_retries", return_value=("", 200, {})) as mock_super:
            client.request_with_retries("get", "https://x", {}, None, 2)
        self.assertEqual(mock_super.call_args.args[4], 2)

    def test_worst_case_interactive_sequence_stays_well_under_gunicorn_timeout(self) -> None:
        from . import billing

        worst_calls = 6  # subscription, price, release, create, modify, cleanup release
        connect, read = billing._INTERACTIVE_TIMEOUT_SECONDS
        worst_case = worst_calls * (connect + read) * (1 + billing._INTERACTIVE_MAX_NETWORK_RETRIES)
        self.assertLessEqual(worst_case, 120 * 0.75)
        self.assertGreaterEqual(billing._PLAN_CHANGE_LOCK_TTL_SECONDS, 130)

    def test_interactive_timeout_is_a_connect_read_tuple_only_inside_the_budget(self) -> None:
        from . import billing

        self.assertEqual(billing._HTTP_CLIENT._timeout, 20)
        with billing._interactive_stripe_budget():
            self.assertEqual(billing._HTTP_CLIENT._timeout, (3.05, 10))
        self.assertEqual(billing._HTTP_CLIENT._timeout, 20)

    def test_zero_retries_forced_inside_the_budget(self) -> None:
        from . import billing

        with billing._interactive_stripe_budget():
            with patch("stripe._http_client.HTTPClient.request_with_retries", return_value=("", 200, {})) as m:
                billing._HTTP_CLIENT.request_with_retries("get", "https://x", {}, None, 2)
        self.assertEqual(m.call_args.args[4], 0)


class PlanChangeLockTokenTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.user = User.objects.create_user(username="locktok", password="pw")

    def test_release_does_not_delete_a_lock_acquired_by_another_owner(self) -> None:
        from . import billing

        key = f"plan-change-lock:{self.user.pk}"
        with billing._plan_change_lock(user=self.user, required=True) as a_locked:
            self.assertTrue(a_locked)
            cache.delete(key)  # simulated TTL expiry
            self.assertTrue(cache.add(key, "b-token", timeout=60))  # B acquires
        self.assertEqual(cache.get(key), "b-token")

    def test_release_deletes_own_lock(self) -> None:
        from . import billing

        key = f"plan-change-lock:{self.user.pk}"
        with billing._plan_change_lock(user=self.user, required=True):
            self.assertIsNotNone(cache.get(key))
        self.assertIsNone(cache.get(key))

    @override_settings(**_SETTINGS)
    @patch("fragrance.billing.stripe.Subscription.retrieve")
    @patch("fragrance.billing.stripe.billing_portal.Session.create")
    def test_portal_runs_inside_the_budget_and_leaves_it_after(self, mock_create, mock_retrieve) -> None:
        from . import billing

        seen: list[bool] = []
        mock_retrieve.return_value = _stripe_subscription()
        mock_create.side_effect = lambda **k: seen.append(billing._interactive_budget.get()) or _portal_session()
        user = _active_user(username="budgeted")
        billing.create_portal_session(user=user)
        self.assertEqual(seen, [True])
        self.assertFalse(billing._interactive_budget.get())


# ---------------------------------------------------------------------------
# Email-task retry regression (InvoiceNotSettledError NameError)
# ---------------------------------------------------------------------------


class EmailTaskRetryTests(TestCase):
    def _run(self, *, task, retries: int, **kwargs: object) -> None:
        task.push_request(retries=retries)
        try:
            task(**kwargs)
        finally:
            task.pop_request()

    def test_beta_invite_smtp_failure_schedules_a_300s_retry(self) -> None:
        invited = User.objects.create_user(username="invitee", email="i@example.com", password="pw12345!")
        signup = BetaSignup.objects.create(
            email="i@example.com", normalized_email="i@example.com", invited_user=invited
        )
        with patch("fragrance.tasks.send_mail", side_effect=OSError("smtp down")):
            with patch.object(send_beta_invite_email, "retry", side_effect=Retry()) as mock_retry:
                with self.assertRaises(Retry):
                    self._run(task=send_beta_invite_email, retries=0, signup_id=signup.id)
        self.assertEqual(mock_retry.call_args.kwargs["countdown"], 300)
        self.assertIsInstance(mock_retry.call_args.kwargs["exc"], OSError)

    def test_beta_invite_reraises_the_original_error_once_retries_are_exhausted(self) -> None:
        invited = User.objects.create_user(username="invitee2", email="i2@example.com", password="pw12345!")
        signup = BetaSignup.objects.create(
            email="i2@example.com", normalized_email="i2@example.com", invited_user=invited
        )
        with patch("fragrance.tasks.send_mail", side_effect=OSError("smtp down")):
            with patch.object(send_beta_invite_email, "retry") as mock_retry:
                with self.assertRaises(OSError):
                    self._run(
                        task=send_beta_invite_email,
                        retries=send_beta_invite_email.max_retries,
                        signup_id=signup.id,
                    )
        mock_retry.assert_not_called()

    def test_recommendation_email_failure_schedules_a_300s_retry_and_leaves_email_status_alone(self) -> None:
        user = User.objects.create_user(username="recip", email="r@example.com", password="pw12345!")
        run = RecommendationRun.objects.create(user=user, status="done")
        with patch("fragrance.tasks.render_and_send_email", side_effect=OSError("smtp down")):
            with patch.object(send_recommendation_email, "retry", side_effect=Retry()) as mock_retry:
                with self.assertRaises(Retry):
                    self._run(task=send_recommendation_email, retries=0, user_id=user.id, run_id=run.id)
        self.assertEqual(mock_retry.call_args.kwargs["countdown"], 300)
        run.refresh_from_db()
        self.assertIsNone(run.email_status)

    def test_recommendation_email_marks_the_run_failed_once_retries_are_exhausted(self) -> None:
        user = User.objects.create_user(username="recip2", email="r2@example.com", password="pw12345!")
        run = RecommendationRun.objects.create(user=user, status="done")
        with patch("fragrance.tasks.render_and_send_email", side_effect=OSError("smtp down")):
            with patch.object(send_recommendation_email, "retry") as mock_retry:
                with self.assertRaises(OSError):
                    self._run(
                        task=send_recommendation_email,
                        retries=send_recommendation_email.max_retries,
                        user_id=user.id,
                        run_id=run.id,
                    )
        mock_retry.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.email_status, "failed")


def _stripe_subscription_list(*, subs: list[dict]) -> stripe.ListObject:
    return stripe.ListObject.construct_from(
        {
            "object": "list",
            "has_more": False,
            "url": "/v1/subscriptions",
            "data": [{"object": "subscription", "customer": "cus_test_1", **sub} for sub in subs],
        },
        _KEY,
    )
