"""Shared Stripe fakes and builders (SimpleNamespace- and construct_from-based) for fragrance tests."""


import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import DEFAULT, MagicMock, create_autospec, patch

import stripe
from django.utils import timezone
from fragrance import billing
from fragrance.models import StripeEvent


# ─── Fake StripeClient (autospecced against the real SDK) ─────────────────
#
# Built once against a real, throwaway stripe.StripeClient so every mocked
# method's signature (positional id, `params=`, `options=`) is enforced by
# autospec -- a typo'd method name (`.modify` instead of `.update`) raises
# AttributeError at fake-construction/configuration time, and a call shaped
# like the pre-migration SDK (`retrieve(id, expand=[...])` instead of
# `retrieve(id, params={"expand": [...]})`) raises TypeError, instead of both
# silently returning a MagicMock the way a bare `MagicMock()` would.
_SPEC_CLIENT = stripe.StripeClient(api_key="sk_test_autospec_dummy")

# (attribute path on client.v1, [method names our production code actually calls])
# Only these are given the "raise until configured" guard (see _guard below):
# an unconfigured call to one of them is exactly the bug class that made
# _schedule_plan_change_locked walk empty phases and return 201 against a bare
# MagicMock in an earlier draft of this fixture (see PlanChangeHardeningTests.
# test_lock_is_per_user, which depends on an unmocked SubscriptionSchedule.create
# reaching a real, failing Stripe call).
_GUARDED_METHODS: dict[str, list[str]] = {
    "customers": ["create", "retrieve", "update"],
    "checkout.sessions": ["create", "retrieve", "expire"],
    "invoice_payments": ["list"],
    "invoices": ["list", "retrieve"],
    "payment_intents": ["retrieve"],
    "subscriptions": ["list", "retrieve", "cancel"],
    "subscription_schedules": ["create", "update", "release", "retrieve"],
    "prices": ["retrieve"],
    "billing_portal.sessions": ["create"],
    "billing_portal.configurations": ["list", "create", "update"],
}


def _real_service(path: str) -> Any:
    obj: Any = _SPEC_CLIENT.v1
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _guard(mock_method: MagicMock, *, label: str, fired: list[str]) -> None:
    """
    Makes `mock_method` raise stripe.APIConnectionError until a test sets its
    `.return_value` (or its own `.side_effect`, which simply replaces this one).
    Mirrors "an unconfigured real call fails" instead of "an unconfigured fake
    call quietly succeeds with a MagicMock".

    Also appends `label` to `fired` every time the guard actually raises --
    independent of whether the caller then catches that exception. Production
    code has legitimate best-effort try/except StripeError blocks (e.g.
    _release_spent_schedule), and a caught-and-swallowed guard exception would
    otherwise let a test silently exercise the *failure* branch while still
    reading as a pass. patch_stripe_clients() checks `fired` on exit and fails
    the test unless it opted out via `allow_unconfigured_stripe_calls`.
    """
    unset = object()
    mock_method.return_value = unset

    def _raise_until_configured(*args: object, **kwargs: object) -> object:
        if mock_method.return_value is unset:
            fired.append(label)
            raise stripe.APIConnectionError(f"unconfigured fake stripe call: {label}")
        return DEFAULT

    mock_method.side_effect = _raise_until_configured


class FakeStripeClient:
    """
    A test double shaped like stripe.StripeClient, restricted to `.v1` (and
    intentionally NOT exposing top-level shortcuts like `.customers` -- a
    deprecated access pattern must fail here exactly as it would on a real
    client). Each leaf service is `create_autospec`'d against a real
    `stripe.StripeClient`'s `.v1` tree.

    allow_unconfigured_stripe_calls: set True in a test that deliberately
    exercises an unconfigured guarded method (e.g. to prove a best-effort
    try/except path is reached) to opt out of patch_stripe_clients()'s
    end-of-test tripwire for that one test.
    """

    def __init__(self) -> None:
        self.allow_unconfigured_stripe_calls = False
        self.fired_unconfigured_guards: list[str] = []
        services: dict[str, MagicMock] = {}
        for path in _GUARDED_METHODS:
            real = _real_service(path)
            services[path] = create_autospec(real, spec_set=True, instance=True)
            for method_name in _GUARDED_METHODS[path]:
                _guard(
                    getattr(services[path], method_name),
                    label=f"v1.{path}.{method_name}",
                    fired=self.fired_unconfigured_guards,
                )

        self.v1 = SimpleNamespace(
            customers=services["customers"],
            checkout=SimpleNamespace(sessions=services["checkout.sessions"]),
            invoice_payments=services["invoice_payments"],
            invoices=services["invoices"],
            payment_intents=services["payment_intents"],
            subscriptions=services["subscriptions"],
            subscription_schedules=services["subscription_schedules"],
            prices=services["prices"],
            billing_portal=SimpleNamespace(
                sessions=services["billing_portal.sessions"],
                configurations=services["billing_portal.configurations"],
            ),
        )


def build_fake_stripe_client() -> FakeStripeClient:
    return FakeStripeClient()


@contextmanager
def patch_stripe_clients():
    """
    Patches fragrance.billing.get_stripe_client / get_interactive_stripe_client
    to hand back one shared FakeStripeClient, while still calling through to the
    REAL accessors first -- so settings.STRIPE_SECRET_KEY's fail-fast
    (ImproperlyConfigured) and any override_settings(STRIPE_SECRET_KEY="") test
    still exercise production code, not the test double. The two accessor
    mocks stay inspectable (call_count, assert_called/assert_not_called) so a
    test can assert which of the two clients a code path used.

    Yields (fake_client, mock_get_stripe_client, mock_get_interactive_stripe_client).
    """
    fake = build_fake_stripe_client()
    real_get_stripe_client = billing.get_stripe_client
    real_get_interactive_stripe_client = billing.get_interactive_stripe_client

    def _resolve_default() -> FakeStripeClient:
        real_get_stripe_client()  # raises ImproperlyConfigured if unconfigured; else discarded
        return fake

    def _resolve_interactive() -> FakeStripeClient:
        real_get_interactive_stripe_client()
        return fake

    with patch(
        "fragrance.billing.get_stripe_client", side_effect=_resolve_default
    ) as mock_default, patch(
        "fragrance.billing.get_interactive_stripe_client", side_effect=_resolve_interactive
    ) as mock_interactive:
        yield fake, mock_default, mock_interactive

    if fake.fired_unconfigured_guards and not fake.allow_unconfigured_stripe_calls:
        raise AssertionError(
            "unconfigured fake Stripe call(s) were made and reached no assertion: "
            f"{fake.fired_unconfigured_guards}. Either configure a .return_value/"
            ".side_effect for each, or set "
            "self.stripe.allow_unconfigured_stripe_calls = True if this is deliberate "
            "(e.g. asserting a best-effort try/except path)."
        )


# ─── Stripe billing (slice 2b) ────────────────────────────────────────────
#
# Every stripe-touching test class below carries these three settings so
# fragrance.billing.get_stripe_client()/get_interactive_stripe_client()'s
# fail-fast guard doesn't fire -- mirrors how the rest of this file scopes
# settings overrides to the tests that actually need them.
_STRIPE_TEST_SETTINGS = {
    "STRIPE_SECRET_KEY": "sk_test_dummy",
    "STRIPE_WEBHOOK_SECRET": "whsec_test_dummy",
    "STRIPE_API_VERSION": "2026-08-26.dahlia",
}


def _sdk_line(*, price_id: str | None, period_end: int | None) -> dict:
    """Plain-dict line item, nested into _sdk_invoice's construct_from call so the SDK converts it too."""
    return {
        "pricing": {"price_details": {"price": price_id}} if price_id else None,
        "period": {"end": period_end} if period_end is not None else None,
    }


def _sdk_invoice(
    *,
    invoice_id: str,
    customer_id: str,
    subscription_id: str | None,
    amount_paid: int,
    lines: list[dict],
    parent_type: str = "subscription_details",
    status: str = "paid",
    amount_remaining: int = 0,
) -> Any:
    """A genuine stripe.Invoice (via construct_from), walked the same defensive way billing._match_plan_line does."""
    parent = (
        {"type": "subscription_details", "subscription_details": {"subscription": subscription_id}}
        if parent_type == "subscription_details"
        else {"type": parent_type, "subscription_details": None}
    )
    return stripe.Invoice.construct_from(
        {
            "id": invoice_id,
            "object": "invoice",
            "customer": customer_id,
            "amount_paid": amount_paid,
            "status": status,
            "amount_remaining": amount_remaining,
            "parent": parent,
            "lines": {
                "object": "list",
                "data": lines,
                "has_more": False,
                "url": f"/v1/invoices/{invoice_id}/lines",
            },
        },
        "sk_test_dummy",
    )


def _sdk_invoice_payment(*, invoice: Any) -> Any:
    return stripe.InvoicePayment.construct_from(
        {"object": "invoice_payment", "status": "paid", "invoice": invoice}, "sk_test_dummy"
    )


def _sdk_payment_intent(*, pi_id: str, status: str, customer: str | None) -> Any:
    return stripe.PaymentIntent.construct_from(
        {"id": pi_id, "object": "payment_intent", "status": status, "customer": customer}, "sk_test_dummy"
    )


def _list_object(*, data: list) -> stripe.ListObject:
    """
    A real, single-page stripe.ListObject (has_more=False) wrapping already-built
    SDK objects. construct_from happily nests already-constructed StripeObjects
    (verified: it does not try to re-flatten them), so this is the general-purpose
    replacement for the `mock.return_value.auto_paging_iter.return_value = iter([...])`
    chaining idiom -- assigning a real ListObject as `.return_value` gives real,
    working pagination instead of a mocked one.
    """
    return stripe.ListObject.construct_from(
        {"object": "list", "data": data, "has_more": False, "url": ""}, _KEY
    )


def _sub_list(*, statuses: dict[str, str]) -> stripe.ListObject:
    return stripe.ListObject.construct_from(
        {
            "object": "list",
            "has_more": False,
            "data": [
                {"id": sub_id, "object": "subscription", "status": status}
                for sub_id, status in statuses.items()
            ],
        },
        "sk_test",
    )

_KEY = "sk_test_dummy"
_BILLING_CHANGES_SETTINGS = {
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


# ---------------------------------------------------------------------------
# Review-fix regression tests: lock, fail-closed portal, price-derived phase,
# Stripe budget, email-task retries
# ---------------------------------------------------------------------------

def _bare_schedule() -> stripe.SubscriptionSchedule:
    return _real_schedule(
        schedule=_schedule_dict(
            phases=[_phase_dict(start=_PERIOD_START, end=_PERIOD_END, price_id="price_mo")],
            current_start=_PERIOD_START,
        )
    )


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


def _customer(*, email: str) -> stripe.Customer:
    return stripe.Customer.construct_from({"id": "cus_alice", "object": "customer", "email": email}, _KEY)
_EMAIL_SYNC_SETTINGS = {
    "STRIPE_SECRET_KEY": _KEY,
    "STRIPE_API_VERSION": "2026-08-26.dahlia",
    "FRONTEND_URL": "https://app.example.test",
}


def _store_customer_updated(
    *, event_id: str, customer_id: str, email: str | None, previous: dict | None
) -> None:
    data: dict = {"object": {"id": customer_id, "object": "customer", "email": email}}
    if previous is not None:
        data["previous_attributes"] = previous
    StripeEvent.objects.create(
        stripe_event_id=event_id,
        event_type="customer.updated",
        stripe_created=timezone.now(),
        payload={"id": event_id, "type": "customer.updated", "data": data},
    )
