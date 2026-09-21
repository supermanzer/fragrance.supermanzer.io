"""
fragrance/billing.py

All Stripe integration code

Checkout Session creation, and the payment_intent.succeeded /
charge.dispute.created fulfillment logic.

Two entry points converge on _grant_period_for_invoice, the one function that
ever extends Subscription.access_expires_at: fulfill_payment_intent (the
webhook path, called from tasks.process_stripe_event) and
fulfill_checkout_session (the success-page fallback, called synchronously
from BillingSessionView). Keeping them on one function is what makes a
double call inert rather than granting two periods -- the ProcessedPayment
unique constraint on stripe_invoice_id is the actual guard, enforced inside
a row-locked transaction so a concurrent webhook-vs-success-page race loses
on the constraint, not the read.

Deviation from the plan worth flagging explicitly: `### Success-page
fallback` says to hand `session.payment_intent` to the fulfillment function.
Verified against the installed stripe==15.6.1 wheel: `Session.payment_intent`
is documented "for Checkout Sessions in `payment` mode" only -- our sessions
are always mode="subscription", so that field is null in practice and
would make the fallback dead code. `Session.invoice` ("ID of the invoice
created by the Checkout Session, if it exists") is what's actually populated,
so fulfill_checkout_session resolves the invoice directly and reuses
_grant_period_for_invoice, the same convergence point the plan asks for,
just entered from the invoice side rather than through a PaymentIntent.
"""

import logging
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from typing import Any

import stripe
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import BetaSignup, Plan, ProcessedPayment, Subscription

logger = logging.getLogger(__name__)

# Grace window added on top of Stripe's own period_end so a renewal that
# settles a few hours late (card retry, clock skew) doesn't read as lapsed.
_PERIOD_END_GRACE = timedelta(days=2)

# Constructed once at import time, not per call inside _configure_stripe --
# a fresh RequestsClient per call would discard the pooled requests.Session
# and add a TLS handshake to every Stripe call, the opposite of "keep the
# call fast." Default is 80s; gunicorn's --timeout 120 sits behind
# Cloudflare's 125s edge ceiling (docker-compose.yaml, and the still-open
# verify_ai_model timeout finding this project already has one of), so the
# Stripe leg of a checkout request needs real headroom below that.
# stripe.RequestsClient (not stripe.http_client.RequestsClient) is the
# public, lazily-exported name in stripe-python 15.x's __init__.py.
_HTTP_CLIENT = stripe.RequestsClient(timeout=20)


def _configure_stripe() -> None:
    """
    Fails at call time with a clear error, matching the ANTHROPIC_API_KEY /
    registry._get_api_key precedent: STRIPE_SECRET_KEY is read in settings.py
    with default="" so Django can boot before the operator has provisioned
    it, and the missing-credential error surfaces here, at first use,
    instead of at import time.
    """
    if not settings.STRIPE_SECRET_KEY:
        raise ImproperlyConfigured(
            "STRIPE_SECRET_KEY is not set. Add it to backend/.env before using billing endpoints."
        )
    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.api_version = settings.STRIPE_API_VERSION
    stripe.max_network_retries = 2
    stripe.default_http_client = _HTTP_CLIENT


class CheckoutError(Exception):
    """Base for checkout-flow errors BillingCheckoutView translates into HTTP responses."""


class SubscriptionAlreadyActiveError(CheckoutError):
    """Raised when the requesting user already has active access -- checkout is a 409."""


class UnknownPlanError(CheckoutError):
    """Raised when the requested plan slug doesn't resolve to an active Plan."""


class SessionLookupError(Exception):
    """Base for BillingSessionView errors."""


class SessionNotFoundError(SessionLookupError):
    """Raised when the Stripe session id doesn't resolve (deleted, malformed, or unknown to Stripe)."""


class SessionOwnershipError(SessionLookupError):
    """
    Raised when a session's client_reference_id doesn't match the requesting
    user. Treated identically to SessionNotFoundError by the view (both -> 404)
    so a guessed session id carries no signal about whether it belongs to
    someone else -- matching this project's other anti-enumeration surfaces.
    """


def get_active_plans() -> list[Plan]:
    return list(Plan.objects.filter(is_active=True).order_by("amount_cents"))


def subscription_status_payload(*, subscription: Subscription) -> dict:
    """
    Shared response shape for GET /billing/subscription/ and GET
    /billing/session/<id>/ -- one function, so the two endpoints can never
    quietly drift into two different shapes for the same underlying state.
    fulfill_checkout_session adds its own "granted" key on top of this.
    """
    plan = subscription.plan
    return {
        "status": subscription.status,
        "plan": {"slug": plan.slug, "name": plan.name} if plan else None,
        "access_expires_at": subscription.access_expires_at,
        "has_active_access": subscription.has_active_access,
    }


def _get_or_create_customer(*, user: User, subscription: Subscription) -> str:
    if subscription.stripe_customer_id:
        return subscription.stripe_customer_id
    customer = stripe.Customer.create(
        email=user.email,
        metadata={"user_id": str(user.id)},
    )
    subscription.stripe_customer_id = customer.id
    subscription.save(update_fields=["stripe_customer_id"])
    return customer.id


def _reusable_open_session_url(
    *, subscription: Subscription, plan: Plan
) -> str | None:
    """
    Resolves subscription.last_checkout_session_id against Stripe:

    - No stored id, or Stripe no longer recognizes it (stale/expired enough
      to 404): nothing to reuse, return None.
    - Open and for the *same* plan: this is a legitimate retry of an
      abandoned checkout -- return its url rather than minting a second
      session (the double-submit protection the plan calls for instead of
      an idempotency_key).
    - Open but for a *different* plan (the user changed their mind on the
      paywall before finishing checkout): expire it so it can't be
      completed against stale pricing, then fall through to minting a new
      one.
    - Anything else (complete, expired): nothing to reuse.
    """
    session_id = subscription.last_checkout_session_id
    if not session_id:
        return None
    try:
        existing = stripe.checkout.Session.retrieve(session_id)
    except stripe.InvalidRequestError:
        return None
    if existing.status != "open":
        return None
    # Both the slug *and* the price id must match. The slug alone survives
    # exactly the change that matters: if the operator re-runs seed_plans
    # with a new --monthly-price-id (test -> live, or a genuine price
    # change), a stale open session for "beta-monthly" would otherwise be
    # handed back and the user would check out against the old Price --
    # the same "validate the price, not a proxy for it" reasoning the plan
    # applies everywhere else in this file.
    if (
        existing.metadata.get("plan_slug") == plan.slug
        and existing.metadata.get("price_id") == plan.stripe_price_id
    ):
        return existing.url
    stripe.checkout.Session.expire(session_id)
    return None


def create_checkout_session(*, user: User, plan_slug: str) -> str:
    try:
        plan = Plan.objects.get(slug=plan_slug, is_active=True)
    except Plan.DoesNotExist:
        raise UnknownPlanError(f"No active plan with slug {plan_slug!r}.")

    _configure_stripe()

    subscription, _ = Subscription.objects.get_or_create(user=user)
    if subscription.has_active_access:
        raise SubscriptionAlreadyActiveError()

    reusable_url = _reusable_open_session_url(
        subscription=subscription, plan=plan
    )
    if reusable_url:
        return reusable_url

    customer_id = _get_or_create_customer(user=user, subscription=subscription)

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": plan.stripe_price_id, "quantity": 1}],
        customer=customer_id,
        client_reference_id=str(user.id),
        subscription_data={"metadata": {"user_id": str(user.id)}},
        # plan_slug + price_id (in addition to user_id) let
        # _reusable_open_session_url tell "same plan, same price, safe to
        # reuse" from "stale, must expire" without a second Stripe round
        # trip to fetch the Price.
        metadata={
            "user_id": str(user.id),
            "plan_slug": plan.slug,
            "price_id": plan.stripe_price_id,
        },
        success_url=f"{settings.FRONTEND_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.FRONTEND_URL}/billing",
    )

    subscription.status = "checkout_started"
    subscription.last_checkout_session_id = session.id
    subscription.save(update_fields=["status", "last_checkout_session_id"])
    return session.url


def _match_plan_line(*, invoice: Any) -> tuple[Plan, Any] | None:
    """
    Walks invoice.lines defensively, matching each line's price id against
    an active Plan -- never lines.data[0]: a proration line or a second
    subscription item would make position 0 the wrong line, and this is the
    exact validation the plan asks for ("Validate the Price, not the
    amount"). Returns (plan, matched_line) for the first match, so the
    caller can also read the matched line's own period for the grant
    boundary, or None if no line matches any active Plan price.
    """
    plan_by_price = {
        p.stripe_price_id: p for p in Plan.objects.filter(is_active=True)
    }
    for line in invoice.lines.data:
        pricing = getattr(line, "pricing", None)
        price_details = (
            getattr(pricing, "price_details", None) if pricing else None
        )
        price_id = (
            getattr(price_details, "price", None) if price_details else None
        )
        if isinstance(price_id, str) and price_id in plan_by_price:
            return plan_by_price[price_id], line
    return None


def _access_expires_at_for(
    *, line: Any, plan: Plan, current_expiry: datetime | None
) -> datetime:
    """
    Prefers the matched line's own period.end (its billing-period boundary,
    plus grace) over a locally computed offset, which would drift from
    Stripe's actual billing date over successive renewals. Falls back to
    extending from the *later* of now and the current expiry -- so early
    renewal is safe -- only when period.end is absent. Deliberately reads
    plan.access_days_granted here, never quota_period_days: the latter is
    the run-quota window (30 days on every plan) and using it here would
    hand an annual subscriber one month of access.
    """
    period = getattr(line, "period", None)
    period_end = getattr(period, "end", None) if period else None
    if period_end:
        return (
            datetime.fromtimestamp(period_end, tz=dt_timezone.utc)
            + _PERIOD_END_GRACE
        )
    base = max(timezone.now(), current_expiry or timezone.now())
    return base + timedelta(days=plan.access_days_granted)


def _grant_period_for_invoice(
    *,
    invoice: Any,
    expected_customer_id: str | None = None,
    payment_intent_id: str = "",
) -> bool:
    """
    Steps 5-10 of the payment_intent.succeeded validation chain -- shared by
    fulfill_payment_intent (one call per invoice found via InvoicePayments)
    and fulfill_checkout_session (a single invoice resolved directly from
    the Checkout Session). Every rejection here is documented in the plan as
    "log and grant nothing," never an exception -- this function returns
    False rather than raising for any of them.

    Gates on the invoice itself being paid (status == "paid" and
    amount_remaining == 0, per stripe==15.6.1 Invoice.status / amount_remaining)
    so every entry path, present or future, inherits the payment check. A
    subscription whose first payment fails leaves an "open" invoice with
    amount_remaining > 0 attached to the Checkout Session.
    """
    if (
        getattr(invoice, "status", None) != "paid"
        or getattr(invoice, "amount_remaining", None) != 0
    ):
        logger.info(
            "stripe invoice %s is not paid (status=%r, amount_remaining=%r); granting nothing",
            invoice.id,
            getattr(invoice, "status", None),
            getattr(invoice, "amount_remaining", None),
        )
        return False

    parent = getattr(invoice, "parent", None)
    if (
        parent is None
        or getattr(parent, "type", None) != "subscription_details"
    ):
        logger.info(
            "stripe invoice %s is not a subscription invoice; skipping",
            invoice.id,
        )
        return False
    subscription_details = getattr(parent, "subscription_details", None)
    stripe_subscription_id = (
        getattr(subscription_details, "subscription", None)
        if subscription_details
        else None
    )
    if not stripe_subscription_id:
        logger.warning(
            "stripe invoice %s has no subscription id on its parent; skipping",
            invoice.id,
        )
        return False

    matched = _match_plan_line(invoice=invoice)
    if matched is None:
        logger.warning(
            "stripe invoice %s matches no active Plan price; skipping",
            invoice.id,
        )
        return False
    plan, line = matched

    invoice_customer_id = invoice.customer
    if (
        expected_customer_id is not None
        and invoice_customer_id != expected_customer_id
    ):
        logger.warning(
            "stripe invoice %s customer %s does not match expected customer %s; skipping",
            invoice.id,
            invoice_customer_id,
            expected_customer_id,
        )
        return False

    try:
        local_subscription = Subscription.objects.select_related("user").get(
            stripe_customer_id=invoice_customer_id
        )
    except Subscription.DoesNotExist:
        logger.warning(
            "stripe invoice %s customer %s has no local Subscription; skipping",
            invoice.id,
            invoice_customer_id,
        )
        return False

    beta_status = (
        BetaSignup.objects.filter(invited_user=local_subscription.user)
        .values_list("status", flat=True)
        .first()
    )
    if beta_status not in ("approved", "converted"):
        logger.warning(
            "stripe invoice %s paid for user %s whose BetaSignup status is %r; granting nothing",
            invoice.id,
            local_subscription.user_id,
            beta_status,
        )
        return False

    with transaction.atomic():
        locked = Subscription.objects.select_for_update().get(
            pk=local_subscription.pk
        )
        new_expiry = _access_expires_at_for(
            line=line, plan=plan, current_expiry=locked.access_expires_at
        )
        try:
            # Nested atomic() -> a savepoint, not the outer transaction: an
            # IntegrityError here (the unique constraint on
            # stripe_invoice_id, i.e. this invoice was already applied)
            # must only roll back this insert, not the Subscription lock
            # acquired above. Scoped to this one statement so a later
            # IntegrityError on locked.save() (e.g. a stripe_subscription_id
            # collision -- a real data problem) is never caught here and
            # misread as "already applied."
            with transaction.atomic():
                ProcessedPayment.objects.create(
                    stripe_invoice_id=invoice.id,
                    stripe_payment_intent_id=payment_intent_id,
                    stripe_subscription_id=stripe_subscription_id,
                    subscription=locked,
                    amount_paid_cents=invoice.amount_paid,
                    period_end=new_expiry,
                )
        except IntegrityError:
            logger.info(
                "stripe invoice %s already applied; no second grant", invoice.id
            )
            return False

        locked.access_expires_at = new_expiry
        locked.status = "active"
        locked.last_paid_at = timezone.now()
        locked.stripe_subscription_id = stripe_subscription_id
        locked.plan = plan
        locked.save(
            update_fields=[
                "access_expires_at",
                "status",
                "last_paid_at",
                "stripe_subscription_id",
                "plan",
            ]
        )
    return True


def fulfill_payment_intent(*, payment_intent_id: str) -> int:
    """
    payment_intent.succeeded handler (steps 1-4 of the plan's validation
    chain), invoked from tasks.process_stripe_event. Returns the number of
    periods granted -- 0, 1, or more, since one PaymentIntent can settle
    several invoices and each is an independent grant.
    """
    _configure_stripe()
    pi = stripe.PaymentIntent.retrieve(payment_intent_id)

    if pi.status != "succeeded":
        logger.info(
            "payment_intent %s status is %r, not succeeded; skipping",
            payment_intent_id,
            pi.status,
        )
        return 0
    if not pi.customer:
        logger.warning(
            "payment_intent %s has no customer; granting nothing",
            payment_intent_id,
        )
        return 0

    try:
        Subscription.objects.get(stripe_customer_id=pi.customer)
    except Subscription.DoesNotExist:
        logger.warning(
            "payment_intent %s customer %s has no local Subscription; granting nothing",
            payment_intent_id,
            pi.customer,
        )
        return 0

    payments = stripe.InvoicePayment.list(
        payment={"type": "payment_intent", "payment_intent": pi.id},
        status="paid",
        expand=["data.invoice"],
    ).auto_paging_iter()

    granted = 0
    saw_any = False
    for invoice_payment in payments:
        saw_any = True
        invoice = getattr(invoice_payment, "invoice", None)
        if invoice is None:
            continue
        if _grant_period_for_invoice(
            invoice=invoice,
            expected_customer_id=pi.customer,
            payment_intent_id=pi.id,
        ):
            granted += 1

    if not saw_any:
        # A clean rejection, not an error: this PaymentIntent isn't an
        # invoice payment at all (e.g. a one-off charge), so it grants
        # nothing -- more principled than amount-matching, per the plan.
        logger.info(
            "payment_intent %s has no paid InvoicePayments; not a subscription payment",
            payment_intent_id,
        )
    return granted


def fulfill_checkout_session(*, session_id: str, user: User) -> dict:
    """
    GET /billing/session/<id>/ handler. Verifies client_reference_id belongs
    to the caller, then resolves the session's *invoice* directly (see the
    module docstring for why this is invoice, not payment_intent, despite
    the plan's original wording) and reuses _grant_period_for_invoice --
    the same convergence point the webhook path uses, so a double call from
    both routes stays inert via the ProcessedPayment unique constraint.
    """
    _configure_stripe()
    try:
        session = stripe.checkout.Session.retrieve(
            session_id, expand=["invoice"]
        )
    except stripe.InvalidRequestError:
        raise SessionNotFoundError()

    if session.client_reference_id != str(user.id):
        logger.warning(
            "checkout session %s client_reference_id does not match requesting user %s",
            session_id,
            user.id,
        )
        raise SessionOwnershipError()

    subscription, _ = Subscription.objects.get_or_create(user=user)
    granted = False
    invoice = getattr(session, "invoice", None)
    if getattr(session, "payment_status", None) == "unpaid":
        logger.info(
            "checkout session %s payment_status is unpaid; granting nothing",
            session_id,
        )
    elif invoice is not None:
        granted = _grant_period_for_invoice(
            invoice=invoice, expected_customer_id=session.customer
        )

        session_subscription = getattr(session, "subscription", None)
        session_subscription_id = (
            session_subscription
            if isinstance(session_subscription, str)
            or session_subscription is None
            else session_subscription.id
        )
        parent = getattr(invoice, "parent", None)
        subscription_details = (
            getattr(parent, "subscription_details", None) if parent else None
        )
        resolved_subscription_id = (
            getattr(subscription_details, "subscription", None)
            if subscription_details
            else None
        )
        if (
            session_subscription_id
            and resolved_subscription_id
            and session_subscription_id != resolved_subscription_id
        ):
            logger.error(
                "checkout session %s subscription mismatch: invoice resolved to %s but "
                "session reports %s -- investigate, this should never happen",
                session_id,
                resolved_subscription_id,
                session_subscription_id,
            )

    subscription.refresh_from_db()
    return {
        **subscription_status_payload(subscription=subscription),
        "granted": granted,
    }


def revoke_for_dispute(*, dispute: dict) -> None:
    """
    charge.dispute.created handler, invoked from tasks.process_stripe_event
    with the event's stored `data.object` payload (a plain dict, not a live
    SDK object -- see StripeEvent.payload). Resolves the disputed charge's
    PaymentIntent -> customer -> local Subscription and revokes immediately
    if found. If the chain can't be resolved at any step, logs and revokes
    nothing: revoking the wrong account is worse than a delayed revocation.
    """
    payment_intent_id = dispute.get("payment_intent")
    # payment_intent is Optional[ExpandableField["PaymentIntent"]] on the
    # Dispute object -- if this event was ever produced with an expand on
    # data.object, it arrives as a nested object rather than a bare id
    # string. Same normalization fulfill_checkout_session already applies
    # to session.subscription, applied here for consistency.
    if isinstance(payment_intent_id, dict):
        payment_intent_id = payment_intent_id.get("id")
    if not payment_intent_id:
        logger.warning(
            "charge.dispute.created had no payment_intent; cannot resolve a "
            "Subscription, revoking nothing"
        )
        return

    _configure_stripe()
    try:
        pi = stripe.PaymentIntent.retrieve(payment_intent_id)
    except stripe.StripeError:
        logger.error(
            "could not retrieve payment_intent %s for dispute; revoking nothing",
            payment_intent_id,
        )
        return

    if not pi.customer:
        logger.warning(
            "disputed payment_intent %s has no customer; revoking nothing",
            payment_intent_id,
        )
        return

    updated = Subscription.objects.filter(
        stripe_customer_id=pi.customer
    ).update(status="revoked", access_expires_at=timezone.now())
    if updated == 0:
        logger.warning(
            "disputed payment_intent %s customer %s has no local Subscription; revoking nothing",
            payment_intent_id,
            pi.customer,
        )
    else:
        # This is the "flag for the operator" step from the plan -- there is
        # no ticketing/ops-alert integration in this codebase yet, so a
        # loud, unambiguous log line is the mechanism.
        logger.critical(
            "REVOKED subscription for stripe customer %s due to charge.dispute.created "
            "-- operator review required",
            pi.customer,
        )
