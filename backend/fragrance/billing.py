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
"""

import logging
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from typing import Any, cast

import stripe
from stripe.params import (
    CustomerCreateParams,
    SubscriptionScheduleCreateParams,
    SubscriptionScheduleUpdateParams,
    SubscriptionScheduleUpdateParamsPhaseDuration,
)
from stripe.params.billing_portal import SessionCreateParams as PortalSessionCreateParams
from stripe.params.checkout import SessionCreateParams as CheckoutSessionCreateParams
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone

from .ai_providers.base import sanitize_sdk_error_message
from .models import (
    BetaSignup,
    Plan,
    ProcessedPayment,
    StripeEvent,
    Subscription,
)


logger = logging.getLogger(__name__)


# The interactive client is for every Stripe call made synchronously inside a
# gunicorn request: the portal and plan-change endpoints (which chain up to
# six sequential Stripe calls in one request), and, as of 2026-09-27, checkout
# session creation (BillingCheckoutView) and the success-page fallback
# (BillingSessionView) -- both previously ran on the default client's 20s x 2
# retries budget, whose worst case (~60s per call) could sum past gunicorn's
# --timeout 120 across a multi-call sequence (the infra-reviewer's 2026-09-26
# Reliability finding on create_checkout_session). stripe-python has no
# per-request timeout -- only per-client -- which is exactly why this is a
# second, separately configured StripeClient/http_client rather than a knob
# toggled around each call: no global/contextvar state to race against other
# requests, just a different client object.
#
# (connect, read): stripe 15.6.1's RequestsClient annotates timeout as
# Union[float, Tuple[float, float]] and passes it straight to requests
# (_http_client.py: `timeout=self._timeout`), so a tuple is supported. A dead
# host fails in ~3s instead of burning the whole 10s. requests applies the read
# value per socket read, not as a total deadline, so the per-call worst case is
# roughly connect + read = ~13s (0 retries, so exactly one attempt). Per
# request-path endpoint, worst case: BillingSessionView ~13s (1 call);
# BillingCheckoutView ~66s (4 calls: reusable-session retrieve, expire a
# stale session, create-customer, create-session -- create-customer alone is
# given ONE network retry, see _CUSTOMER_CREATE_MAX_NETWORK_RETRIES, so it is
# ~13s x 2 attempts plus <=0.5s backoff, ~27s; the other three ~13s each);
# BillingPortalView and
# BillingChangePlanView up to ~91s (7 calls on the rare path where an existing
# schedule must be released before a new one is built) -- all comfortably
# under gunicorn's 120s (and nginx's 110s billing proxy_read_timeout).
_INTERACTIVE_TIMEOUT_SECONDS: tuple[float, float] = (3.05, 10)


DISPUTE_LIVE_SUBSCRIPTION_STATUSES = frozenset(
    {"active", "past_due", "unpaid", "trialing", "incomplete"}
)


# Stripe subscription statuses in which an open invoice is a failed renewal a
# new card can settle. `incomplete` (first checkout, where payment_method.attached
# also fires) and `canceled` (paying would take money for a subscription that
# never renews) are deliberately absent.
PAYMENT_RECOVERY_PAYABLE_STATUSES = frozenset({"past_due", "unpaid"})


# How long a "retrying" recovery keeps showing after the invoice was paid: the
# grant arrives by payment_intent.succeeded within seconds, so anything older
# is a lost webhook the nightly reconcile will repair, not something to keep
# telling the customer is in progress.
PAYMENT_RECOVERY_RETRYING_WINDOW = timedelta(hours=1)


# Far longer than Stripe's 3-day smart-retry window and tolerant of six missed
# nightly runs; --days widens it for a manual sweep.
RECONCILE_INVOICE_LOOKBACK = timedelta(days=7)


RECONCILE_INVOICE_PAGE_SIZE = 100


RECONCILE_MAX_CANDIDATES = 200


# Sanity ceiling on invoices walked per run; hitting it means the lookback or
# the account has outgrown the nightly sweep and someone should look.
RECONCILE_MAX_INVOICES = 5000


# Wall-clock budget, below the task's soft_time_limit (240s) so the loop stops
# itself cleanly before Celery does.
RECONCILE_TIME_BUDGET_SECONDS = 200.0


STUCK_EVENT_MAX_AGE = timedelta(days=3)


STUCK_EVENT_MIN_AGE = timedelta(minutes=10)


# Checkout runs on the interactive client with 0 retries, but a Customer
# create that times out would otherwise 502 the whole checkout. This is safe
# to retry: the SDK sends the same Idempotency-Key on every attempt of one
# call (proven at the wire in tests/billing/test_stripe_client_seam.py), so a
# request that reached Stripe before the timeout is not created twice. Applied
# per request, only to customers.create, via RequestOptions.
_CUSTOMER_CREATE_MAX_NETWORK_RETRIES = 1


# Two module-level http_client instances, constructed once at import time and
# reused by every StripeClient built from the accessors below -- a fresh
# stripe.RequestsClient per call would discard the pooled requests.Session and
# add a TLS handshake to every Stripe call, the opposite of "keep the call
# fast." stripe.RequestsClient (not stripe.http_client.RequestsClient) is the
# public, lazily-exported name in stripe-python 15.x's __init__.py.
#
# Default: 20s x up to 2 retries (worst case ~60s per call: 3 attempts x 20s,
# plus backoff sleep between attempts). Operator-approved rule (2026-09-27):
# every Stripe call made SYNCHRONOUSLY inside a user's HTTP request uses the
# interactive client below instead -- this default client is reserved for
# background/async work (webhook processing in tasks.process_stripe_event,
# the nightly reconcile_subscriptions sweep, the restore_stripe_customer_email
# task, and the configure_stripe_portal management command), none of which is
# bound by gunicorn's --timeout 120. Those are governed by their own Celery
# soft_time_limit/time_limit instead (see tasks.py), which already accounts
# for this budget.
_HTTP_CLIENT = stripe.RequestsClient(timeout=20)


_INTERACTIVE_HTTP_CLIENT = stripe.RequestsClient(
    timeout=_INTERACTIVE_TIMEOUT_SECONDS
)


_INTERACTIVE_MAX_NETWORK_RETRIES = 0


# Grace window added on top of Stripe's own period_end so a renewal that
# settles a few hours late (card retry, clock skew) doesn't read as lapsed.
_PERIOD_END_GRACE = timedelta(days=2)


# A one-phase schedule that is mid-way through schedule_plan_change (created,
# not yet modified) is indistinguishable from a spent shell, so any concurrent
# request that releases "spent shells" (portal, second POST, DELETE) would
# destroy it. This lock serialises those four Stripe sequences per user. The
# TTL is above gunicorn's 120s --timeout (the worker, hence the lock's owner,
# is killed by then), so the lock cannot expire while its owner is alive; if
# the worker dies, it self-heals within the TTL. Django cache, Redis DB 1.
#
# Release is owner-safe: each acquisition stores a uuid4 token and release only
# deletes if the stored value is still that token. Django's cache API has no
# atomic compare-and-delete, so get-then-delete leaves a microsecond window; it
# is only reachable after TTL expiry, which the TTL choice rules out. redis-py's
# lock() was not used: it would need a second, separately configured Redis
# connection outside the cache abstraction (and could not run under the
# LocMemCache the test runner swaps in).
_PLAN_CHANGE_LOCK_TTL_SECONDS = 130


class CheckoutError(Exception):
    """Base for checkout-flow errors BillingCheckoutView translates into HTTP responses."""


class PlanChangeError(Exception):
    """Base for plan-change errors; `code` is the machine-readable 409/404 code."""

    code = "plan_change_error"


class PortalError(Exception):
    """Base for portal-session errors BillingPortalView translates into HTTP responses."""


class SessionLookupError(Exception):
    """Base for BillingSessionView errors."""


class AccountLockedError(CheckoutError):
    """
    The Subscription is `revoked` (a dispute). Checkout is refused with a 409
    until the operator unlocks the account in Django admin; a payment on a
    locked account is recorded but never grants access.
    """


class CancellationScheduledError(PlanChangeError):
    code = "cancellation_scheduled"


class ChangeAlreadyScheduledError(PlanChangeError):
    code = "already_scheduled"


class ChangeInProgressError(PlanChangeError):
    """Another plan-change request for this user holds the lock -- a 409 the client may retry."""

    code = "change_in_progress"


class CheckoutSessionUrlMissingError(CheckoutError):
    """Stripe returned a created Checkout Session with no url (typed Optional[str])."""


class ComplimentaryGrantError(Exception):
    pass


class ExemptAccountError(PortalError):
    """Staff/superusers have full access and no billing to manage or change."""


class InvoiceNotSettledError(Exception):
    """
    A paid InvoicePayment carried an invoice that Stripe still reports as not
    paid, even after a fresh re-fetch. This is a webhook-vs-invoice-finalization
    race, not a rejection: process_stripe_event retries it on a short ladder.
    Retrying is safe because ProcessedPayment's unique constraint on
    stripe_invoice_id makes any re-grant of an already-applied invoice inert.
    """

    def __init__(self, *invoice_ids: str) -> None:
        self.invoice_ids = invoice_ids
        super().__init__(
            f"invoice(s) not yet settled: {', '.join(invoice_ids)}"
        )


class NoActiveSubscriptionError(PlanChangeError):
    code = "no_active_subscription"


class NoBillingAccountError(PortalError):
    """The user has never started a checkout, so there is no Stripe Customer."""


class NoPendingChangeError(PlanChangeError):
    code = "no_pending_change"


class PlanPriceMismatchError(Exception):
    """
    The target Plan row disagrees with its Stripe Price (billing interval, or
    no recurring interval at all). A data/config fault, not a client error:
    the view answers 502 with a fixed message.
    """


class SamePlanError(PlanChangeError):
    code = "same_plan"


class SessionNotFoundError(SessionLookupError):
    """Raised when the Stripe session id doesn't resolve (deleted, malformed, or unknown to Stripe)."""


class SessionOwnershipError(SessionLookupError):
    """
    Raised when a session's client_reference_id doesn't match the requesting
    user. Treated identically to SessionNotFoundError by the view (both -> 404)
    so a guessed session id carries no signal about whether it belongs to
    someone else -- matching this project's other anti-enumeration surfaces.
    """


class SubscriptionAlreadyActiveError(CheckoutError):
    """Raised when the requesting user already has active access -- checkout is a 409."""


class TermsUrlNotConfiguredError(CheckoutError):
    """STRIPE_REQUIRE_TOS_CONSENT is on but the Stripe account has no Terms URL."""


class UnknownPlanError(CheckoutError):
    """Raised when the requested plan slug doesn't resolve to an active Plan."""


def blacklist_all_refresh_tokens(*, user: User) -> int:
    """
    Blacklists every outstanding, not-yet-blacklisted simplejwt refresh token
    of `user`. Must run inside transaction.atomic(). Locks the user's
    OutstandingToken rows first: AtomicTokenRefreshSerializer holds that same
    row lock while it rotates, so an in-flight refresh either finishes (and
    its freshly minted successor is picked up by the second, separate
    statement below) or blocks until this transaction commits and then sees
    its token blacklisted. Already-blacklisted rows are skipped and conflicts
    ignored (BlacklistedToken.token is unique), so a concurrent blacklist is
    harmless.
    """
    from rest_framework_simplejwt.token_blacklist.models import (
        BlacklistedToken,
        OutstandingToken,
    )

    list(
        OutstandingToken.objects.select_for_update()  # ty: ignore[unresolved-attribute]
        .filter(user=user)
        .values_list("pk", flat=True)
    )
    pending = list(
        OutstandingToken.objects.filter(  # ty: ignore[unresolved-attribute]
            user=user, blacklistedtoken__isnull=True
        )
    )
    BlacklistedToken.objects.bulk_create(  # ty: ignore[unresolved-attribute]
        [BlacklistedToken(token=outstanding) for outstanding in pending],
        ignore_conflicts=True,
    )
    return len(pending)


def cancel_plan_change(
    *, user: User, client: stripe.StripeClient | None = None
) -> None:
    """Releases the caller's pending scheduled change; the subscription carries on unchanged."""
    if is_exempt(user=user):
        raise ExemptAccountError()
    local = Subscription.objects.filter(user=user).first()  # ty: ignore[unresolved-attribute]
    if local is None or not local.stripe_subscription_id:
        raise NoPendingChangeError()

    if client is None:
        client = get_interactive_stripe_client()
    with _plan_change_lock(user=user, required=True):
        stripe_sub = _stripe_subscription_with_schedule(
            stripe_subscription_id=str(local.stripe_subscription_id), client=client
        )
        schedule = _live_schedule(stripe_subscription=stripe_sub, client=client)
        if schedule is None or _pending_phase(schedule=schedule) is None:
            raise NoPendingChangeError()
        client.v1.subscription_schedules.release(schedule.id)


def create_checkout_session(
    *, user: User, plan_slug: str, client: stripe.StripeClient | None = None
) -> str:
    try:
        plan = Plan.objects.get(slug=plan_slug, is_active=True)  # ty: ignore[unresolved-attribute]
    except Plan.DoesNotExist:  # ty: ignore[unresolved-attribute]
        raise UnknownPlanError(f"No active plan with slug {plan_slug!r}.")

    # BillingCheckoutView.post calls this synchronously inside a gunicorn
    # request -- the interactive client, not the default one, per the
    # request-path rule above _INTERACTIVE_HTTP_CLIENT.
    if client is None:
        client = get_interactive_stripe_client()

    subscription, _ = Subscription.objects.get_or_create(user=user)  # ty: ignore[unresolved-attribute]
    if subscription.status == "revoked":
        raise AccountLockedError()
    if subscription.has_active_access:
        raise SubscriptionAlreadyActiveError()

    reusable_url = _reusable_open_session_url(
        subscription=subscription, plan=plan, client=client
    )
    if reusable_url:
        return reusable_url

    # Locked only for the check-and-create-if-missing Customer step. This
    # does span one Stripe network call (Customer.create, up to two attempts
    # of ~13s each on the interactive client, ~27s), but only the
    # first time ever for a given user -- every subsequent checkout for them
    # finds stripe_customer_id already set and returns immediately, still
    # under the lock. It deliberately does NOT extend across Session.create
    # below: that Stripe call happens on every checkout, and holding a DB
    # row lock across it would serialize unrelated requests (and risk any
    # Postgres statement/lock timeout) on Stripe's latency rather than ours.
    with transaction.atomic():
        subscription = Subscription.objects.select_for_update().get(  # ty: ignore[unresolved-attribute]
            pk=subscription.pk
        )
        # The unlocked check above can be stale by now (a dispute may have
        # landed while _reusable_open_session_url talked to Stripe).
        if subscription.status == "revoked":
            raise AccountLockedError()
        customer_id = _get_or_create_customer(
            user=user, subscription=subscription, client=client
        )

    session_params: CheckoutSessionCreateParams = {
        "mode": "subscription",
        "line_items": [{"price": str(plan.stripe_price_id), "quantity": 1}],
        "customer": customer_id,
        "client_reference_id": str(user.id),  # ty: ignore[unresolved-attribute]
        "subscription_data": {"metadata": {"user_id": str(user.id)}},  # ty: ignore[unresolved-attribute]
        # plan_slug + price_id (in addition to user_id) let
        # _reusable_open_session_url tell "same plan, same price, safe to
        # reuse" from "stale, must expire" without a second Stripe round
        # trip to fetch the Price.
        "metadata": {
            "user_id": str(user.id),  # ty: ignore[unresolved-attribute]
            "plan_slug": str(plan.slug),
            "price_id": str(plan.stripe_price_id),
        },
        "success_url": f"{settings.FRONTEND_URL}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{settings.FRONTEND_URL}/billing",
    }
    if settings.STRIPE_REQUIRE_TOS_CONSENT:
        session_params["consent_collection"] = {"terms_of_service": "required"}
    try:
        session = client.v1.checkout.sessions.create(params=session_params)
    except stripe.InvalidRequestError as exc:
        if (
            settings.STRIPE_REQUIRE_TOS_CONSENT
            and "terms of service" in str(exc).lower()
        ):
            logger.error(
                "checkout failed: STRIPE_REQUIRE_TOS_CONSENT is on but the Stripe account has no "
                "Terms of Service URL. Set it in the Stripe Dashboard (Settings > Business > Public "
                "details) for this mode, or set STRIPE_REQUIRE_TOS_CONSENT=False. Stripe said: %s",
                sanitize_sdk_error_message(exc),
            )
            raise TermsUrlNotConfiguredError() from exc
        raise

    # stripe-python types checkout.Session.url as Optional[str]. Checked
    # before any local state is written: a session the user cannot be sent to
    # must not leave the row at "checkout_started" pointing at it.
    url = session.url
    if not url:
        logger.error(
            "checkout session %s was created with no url; refusing", session.id
        )
        raise CheckoutSessionUrlMissingError()

    # A dispute (revoke) or a payment grant can land while Session.create was
    # talking to Stripe, so the row is re-read under a short lock and the
    # decision made on that fresh state. The lock covers only local writes:
    # the Stripe expire call for a refused session happens after it is released.
    # checkout_started is set only from "none" -- a lapsed customer's "active"
    # row must stay "active" so the frontend still sees a lapsed subscriber.
    refusal: AccountLockedError | SubscriptionAlreadyActiveError | None = None
    reason = ""
    with transaction.atomic():
        locked = Subscription.objects.select_for_update().get(  # ty: ignore[unresolved-attribute]
            pk=subscription.pk
        )
        if locked.status == "revoked":
            refusal = AccountLockedError()
            reason = "revoked"
        elif locked.has_active_access:
            refusal = SubscriptionAlreadyActiveError()
            reason = "active"
        else:
            changes: dict[str, str] = {"last_checkout_session_id": session.id}
            if locked.status == "none":
                changes["status"] = "checkout_started"
            Subscription.objects.filter(pk=locked.pk).update(**changes)  # ty: ignore[unresolved-attribute]
    if refusal is not None:
        logger.warning(
            "checkout session %s created for a subscription that became %s mid-request; expiring it",
            session.id,
            reason,
        )
        try:
            client.v1.checkout.sessions.expire(session.id)
        except stripe.StripeError as exc:
            logger.error(
                "could not expire checkout session %s on a %s account: %s",
                session.id,
                reason,
                sanitize_sdk_error_message(exc),
            )
        raise refusal
    return url


def create_portal_session(
    *, user: User, client: stripe.StripeClient | None = None
) -> str:
    """
    Creates a Customer Portal session for the caller's *own* stored Stripe
    customer and returns its short-lived URL. The customer id comes only from
    the user's Subscription row; nothing from the client (customer,
    configuration, return_url, flow_data) is ever accepted. Deliberately not
    gated on active access: a lapsed customer still needs invoices and a card
    update.

    A spent schedule shell (see _pending_phase) is released first, because
    Stripe refuses portal cancellation while any schedule is attached. This is
    best-effort: a Stripe failure is logged and swallowed, and if another
    plan-change request holds the per-user lock the release is skipped (that
    request may own a half-built schedule that only looks like a shell). Neither
    may take the portal down.

    Outside DEBUG an unset STRIPE_PORTAL_CONFIGURATION_ID fails closed: Stripe's
    account-default configuration is more permissive than ours (name/address
    edits, possibly plan switching, which can create a free period).
    """
    if is_exempt(user=user):
        raise ExemptAccountError()
    subscription = Subscription.objects.filter(user=user).first()  # ty: ignore[unresolved-attribute]
    if subscription is None or not subscription.stripe_customer_id:
        raise NoBillingAccountError()

    configuration_id = settings.STRIPE_PORTAL_CONFIGURATION_ID
    if not configuration_id and not settings.DEBUG:
        raise ImproperlyConfigured(
            "STRIPE_PORTAL_CONFIGURATION_ID is not set; refusing to open the portal on "
            "Stripe's permissive default configuration outside DEBUG."
        )

    if client is None:
        client = get_interactive_stripe_client()

    if subscription.stripe_subscription_id:
        with _plan_change_lock(user=user, required=False) as locked:
            if locked:
                try:
                    _release_spent_schedule(
                        stripe_subscription=_stripe_subscription_with_schedule(
                            stripe_subscription_id=str(subscription.stripe_subscription_id),
                            client=client,
                        ),
                        client=client,
                    )
                except stripe.StripeError as exc:
                    logger.warning(
                        "could not check/release spent schedule before portal: %s",
                        sanitize_sdk_error_message(exc),
                    )

    params: PortalSessionCreateParams = {
        "customer": subscription.stripe_customer_id,
        "return_url": f"{settings.FRONTEND_URL}/billing?from=portal",
    }
    if configuration_id:
        params["configuration"] = configuration_id
    else:
        logger.warning(
            "STRIPE_PORTAL_CONFIGURATION_ID is not set; the portal falls back to Stripe's "
            "default configuration (allowed only because DEBUG is on)"
        )
    session = client.v1.billing_portal.sessions.create(params=params)
    return session.url


def fulfill_checkout_session(
    *,
    session_id: str,
    user: User,
    client: stripe.StripeClient | None = None,
) -> dict:
    """
    GET /billing/session/<id>/ handler. Verifies client_reference_id belongs
    to the caller, then resolves the session's *invoice* directly (see the
    module docstring for why this is invoice, not payment_intent, despite
    the plan's original wording) and reuses _grant_period_for_invoice --
    the same convergence point the webhook path uses, so a double call from
    both routes stays inert via the ProcessedPayment unique constraint.

    BillingSessionView.get calls this synchronously inside a gunicorn
    request (the success-page poll), so it uses the interactive client, not
    the default one, per the request-path rule above _INTERACTIVE_HTTP_CLIENT.
    """
    if client is None:
        client = get_interactive_stripe_client()
    try:
        session = client.v1.checkout.sessions.retrieve(
            session_id, params={"expand": ["invoice"]}
        )
    except stripe.InvalidRequestError:
        raise SessionNotFoundError()

    if session.client_reference_id != str(user.id):  # ty: ignore[unresolved-attribute]
        logger.warning(
            "checkout session %s client_reference_id does not match requesting user %s",
            session_id,
            user.id,  # ty: ignore[unresolved-attribute]
        )
        raise SessionOwnershipError()

    consent = getattr(session, "consent", None)
    if getattr(consent, "terms_of_service", None) == "accepted":
        # Not persisted (no model for it); the log line is the only record.
        logger.info(
            "checkout session %s: user %s accepted the terms of service",
            session_id,
            user.id,  # ty: ignore[unresolved-attribute]
        )

    subscription, _ = Subscription.objects.get_or_create(user=user)  # ty: ignore[unresolved-attribute]
    recorded = False
    invoice = getattr(session, "invoice", None)
    if getattr(session, "payment_status", None) == "unpaid":
        logger.info(
            "checkout session %s payment_status is unpaid; recording nothing",
            session_id,
        )
    elif invoice is not None:
        # Same ExpandableField[Customer] shape as fulfill_payment_intent's
        # pi.customer normalization above -- never expanded here, so always
        # a plain id string in practice; None passes through unchanged.
        customer_id = (
            session.customer
            if isinstance(session.customer, str) or session.customer is None
            else session.customer.id
        )
        recorded = _grant_period_for_invoice(
            invoice=invoice, expected_customer_id=customer_id
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
        # The JSON key stays "granted" (frontend contract); its value is
        # "a payment was recorded", which is also True for a payment
        # recorded on a locked account without extending access.
        "granted": recorded,
    }


def fulfill_payment_intent(
    *, payment_intent_id: str, client: stripe.StripeClient | None = None
) -> int:
    """
    payment_intent.succeeded handler (steps 1-4 of the plan's validation
    chain), invoked from tasks.process_stripe_event. Returns the number of
    invoices whose payment was recorded -- 0, 1, or more, since one
    PaymentIntent can settle several invoices. "Recorded" is not the same as
    "extended access": grants are monotonic (see _grant_period_for_invoice),
    so an invoice that is not the new maximum still counts here even though
    it left access_expires_at, status and stripe_subscription_id unchanged,
    and so does a payment recorded on a locked (revoked) account or for an
    invoice line with no period.
    Raises InvoiceNotSettledError (retryable) when a paid InvoicePayment's
    invoice is still not paid after a re-fetch.
    """
    if client is None:
        client = get_stripe_client()
    pi = client.v1.payment_intents.retrieve(payment_intent_id)

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
    # pi.customer is Optional[ExpandableField["Customer"]] on the SDK type --
    # a plain id string unless this call ever expands it, which it never
    # does. Normalized once here (same idiom as revoke_for_dispute's
    # payment_intent normalization) so every use below is a plain str.
    customer_id = pi.customer if isinstance(pi.customer, str) else pi.customer.id

    try:
        Subscription.objects.get(stripe_customer_id=customer_id)  # ty: ignore[unresolved-attribute]
    except Subscription.DoesNotExist:  # ty: ignore[unresolved-attribute]
        logger.warning(
            "payment_intent %s customer %s has no local Subscription; granting nothing",
            payment_intent_id,
            customer_id,
        )
        return 0

    payments = client.v1.invoice_payments.list(
        params={
            "payment": {"type": "payment_intent", "payment_intent": pi.id},
            "status": "paid",
            "expand": ["data.invoice"],
        }
    ).auto_paging_iter()

    recorded = 0
    saw_any = False
    unsettled: list[str] = []
    for invoice_payment in payments:
        saw_any = True
        invoice = getattr(invoice_payment, "invoice", None)
        if invoice is None:
            continue
        # The payment succeeded, but the expanded invoice can still read
        # status=open / amount_remaining>0 for a moment (verified live at a
        # scheduled plan-change boundary). Re-fetch it; the paid gate in
        # _grant_period_for_invoice stays strict either way.
        if getattr(
            invoice_payment, "status", None
        ) == "paid" and not _invoice_is_settled(invoice=invoice):
            invoice = client.v1.invoices.retrieve(invoice.id)
            if not _invoice_is_settled(invoice=invoice):
                logger.warning(
                    "payment_intent %s: invoice %s still not settled after re-fetch "
                    "(status=%r, amount_remaining=%r); will retry",
                    payment_intent_id,
                    invoice.id,
                    getattr(invoice, "status", None),
                    getattr(invoice, "amount_remaining", None),
                )
                unsettled.append(invoice.id)
                continue
        if _grant_period_for_invoice(
            invoice=invoice,
            expected_customer_id=customer_id,
            payment_intent_id=pi.id,
        ):
            recorded += 1

    if unsettled:
        # Raised only after every settled sibling invoice has been recorded.
        raise InvoiceNotSettledError(*unsettled)

    if not saw_any:
        # A clean rejection, not an error: this PaymentIntent isn't an
        # invoice payment at all (e.g. a one-off charge), so it grants
        # nothing -- more principled than amount-matching, per the plan.
        logger.info(
            "payment_intent %s has no paid InvoicePayments; not a subscription payment",
            payment_intent_id,
        )
    return recorded


def get_active_plans() -> list[Plan]:
    return list(Plan.objects.filter(is_active=True).order_by("amount_cents"))  # ty: ignore[unresolved-attribute]


def get_interactive_stripe_client() -> stripe.StripeClient:
    """
    The interactive Stripe client: (3.05, 10)s connect/read timeout, no
    retries -- see _INTERACTIVE_HTTP_CLIENT above for why. Used by every
    Stripe call made synchronously inside a user's HTTP request: the portal
    and plan-change service functions, and, as of 2026-09-27,
    create_checkout_session and fulfill_checkout_session. Same fail-fast
    contract as get_stripe_client().
    """
    if not settings.STRIPE_SECRET_KEY:
        raise ImproperlyConfigured(
            "STRIPE_SECRET_KEY is not set. Add it to backend/.env before using billing endpoints."
        )
    return stripe.StripeClient(
        api_key=settings.STRIPE_SECRET_KEY,
        stripe_version=settings.STRIPE_API_VERSION,
        max_network_retries=_INTERACTIVE_MAX_NETWORK_RETRIES,
        http_client=_INTERACTIVE_HTTP_CLIENT,
    )


def get_plan_change_state(
    *, user: User, client: stripe.StripeClient | None = None
) -> dict:
    """
    Read from Stripe on demand (one Subscription.retrieve with the schedule
    expanded), never cached locally and never part of the polled status
    payload. `available` is empty when nothing can be scheduled: a change is
    already pending, or the subscription is set to cancel.
    """
    local = _changeable_subscription(user=user)
    if client is None:
        client = get_interactive_stripe_client()
    stripe_sub = _stripe_subscription_with_schedule(
        stripe_subscription_id=str(local.stripe_subscription_id), client=client
    )
    if getattr(stripe_sub, "status", None) != "active":
        raise NoActiveSubscriptionError()

    period_end = _subscription_period_end(stripe_subscription=stripe_sub)
    cancel_at_period_end = bool(
        getattr(stripe_sub, "cancel_at_period_end", False)
        or getattr(stripe_sub, "cancel_at", None)
    )
    schedule = _live_schedule(stripe_subscription=stripe_sub, client=client)
    pending = (
        _pending_change_payload(schedule=schedule)
        if schedule is not None
        else None
    )

    available: list[Plan] = []
    if pending is None and not cancel_at_period_end:
        current = _current_price_ids(stripe_subscription=stripe_sub)
        available = [
            plan
            for plan in get_active_plans()
            if plan.stripe_price_id not in current
        ]
    return {
        "pending": pending,
        "available": available,
        "current_period_end": datetime.fromtimestamp(
            period_end, tz=dt_timezone.utc
        ),
        "cancel_at_period_end": cancel_at_period_end,
    }


def get_stripe_client() -> stripe.StripeClient:
    """
    The default Stripe client: 20s timeout, up to 2 retries. Built fresh on
    every call (cheap -- StripeClient itself holds only config and a
    reference to the shared, pooled http_client below, so this still honours
    settings changed after process start, e.g. override_settings in tests)
    but always wired to the same pooled _HTTP_CLIENT, so connection pooling
    survives across calls.

    Reserved for background/async work -- webhook processing, the nightly
    reconcile sweep, the email-restore task, and the portal-config management
    command -- never for a Stripe call made synchronously inside a user's
    HTTP request; see get_interactive_stripe_client() for that case.

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
    return stripe.StripeClient(
        api_key=settings.STRIPE_SECRET_KEY,
        stripe_version=settings.STRIPE_API_VERSION,
        max_network_retries=2,
        http_client=_HTTP_CLIENT,
    )


def grant_complimentary_access(
    *, user: User, days: int, reason: str
) -> Subscription:
    """
    Explicit operator grant of access with no payment behind it -- the only
    non-Stripe writer of access_expires_at, kept here beside
    _grant_period_for_invoice so "access is only written in billing.py"
    stays checkable by grep. Never called automatically; only the
    grant_access management command reaches it. Extends from the later of
    now and the current expiry (so it never shortens paid time), under the
    same Subscription row lock the payment path uses. Refuses a `revoked`
    subscription: a dispute revocation should not be silently undone by a
    comp.
    """
    if days <= 0:
        raise ComplimentaryGrantError("days must be a positive integer.")
    if not reason.strip():
        raise ComplimentaryGrantError("A reason is required.")
    with transaction.atomic():
        Subscription.objects.get_or_create(user=user)  # ty: ignore[unresolved-attribute]
        locked = Subscription.objects.select_for_update().get(user=user)  # ty: ignore[unresolved-attribute]
        if locked.status == "revoked":
            raise ComplimentaryGrantError(
                f"Subscription for {user.username} is revoked (dispute); resolve that first."
            )
        now = timezone.now()
        previous_expiry = locked.access_expires_at
        base = max(now, previous_expiry) if previous_expiry else now
        locked.access_expires_at = base + timedelta(days=days)
        locked.status = "active"
        locked.save(update_fields=["access_expires_at", "status"])
    logger.warning(
        "COMPLIMENTARY ACCESS granted to user %s (%s) for %s days, until %s (was %s). Reason: %s",
        user.pk,
        user.username,
        days,
        locked.access_expires_at.isoformat(),
        previous_expiry.isoformat() if previous_expiry else None,
        reason,
    )
    return locked


def is_exempt(*, user: User) -> bool:
    return bool(user.is_staff or user.is_superuser)


def payment_recovery_payload(*, subscription: Subscription) -> dict | None:
    """
    The `payment_recovery` block of the subscription payload: non-null only for
    a retry recover_past_due_invoice started after a payment method was added.
    Hidden once access is back, on exempt or revoked accounts, and whenever a
    payment has been recorded since the attempt (last_paid_at), which is what
    clears it after recovery without touching the grant path and keeps a stale
    decline from resurfacing on a later lapse. A "retrying" older than
    PAYMENT_RECOVERY_RETRYING_WINDOW is dropped too.
    """
    attempted_at = subscription.payment_recovery_attempted_at
    state = subscription.payment_recovery_state
    if not state or attempted_at is None:
        return None
    user = subscription.user
    if subscription.status == "revoked" or user.is_staff or user.is_superuser:  # ty: ignore[unresolved-attribute]
        return None
    if subscription.has_active_access:
        return None
    if subscription.last_paid_at is not None and attempted_at <= subscription.last_paid_at:
        return None
    if state == "retrying" and timezone.now() - attempted_at > PAYMENT_RECOVERY_RETRYING_WINDOW:
        return None
    return {"state": state, "attempted_at": attempted_at}


def reconcile_subscriptions(
    *,
    dry_run: bool = False,
    lookback: timedelta = RECONCILE_INVOICE_LOOKBACK,
    time_budget_seconds: float = RECONCILE_TIME_BUDGET_SECONDS,
    client: stripe.StripeClient | None = None,
) -> dict:
    """
    Backstop for a lost webhook, the only recovery once Stripe's retry window
    has passed. Walks ONE account-wide, paginated list of recently PAID
    invoices, keeps those whose customer maps to a known local Subscription
    (revoked ones included), skips any already in ProcessedPayment, and feeds
    the rest through _grant_period_for_invoice -- the single grant path -- so
    a re-run cannot double-grant. summary["recorded"] counts payments
    recorded, which includes one recorded on a locked (revoked) account
    without granting access -- that case also logs at CRITICAL, so money paid
    on a locked account is recorded and paged even when its webhook was lost,
    and can never be granted by a later reconcile after an unlock. Cost is one
    Stripe call per 100 invoices regardless
    of how many subscribers are abandoned or idle. Bounded by a wall-clock
    budget and an invoice-count sanity cap. In dry_run mode nothing is
    written or enqueued: an invoice counts as would_grant when it is paid,
    fully settled and has no ProcessedPayment yet (the remaining guards --
    price, parent, approval -- are only evaluated by a real run). Also
    re-enqueues stuck StripeEvent rows. Returns a summary dict.
    """
    summary = {
        "invoices_seen": 0,
        "invoices_matched": 0,
        "recorded": 0,
        "would_grant": 0,
        "already_applied": 0,
        "rejected": 0,
        "errors": 0,
        "events_requeued": 0,
        "events_skipped_no_payload": 0,
        "capped": False,
        "deadline_exceeded": False,
        "skipped": False,
        "dry_run": dry_run,
    }
    if not settings.STRIPE_SECRET_KEY:
        logger.warning(
            "reconcile_subscriptions: STRIPE_SECRET_KEY is not set; nothing to do"
        )
        summary["skipped"] = True
        return summary
    if client is None:
        client = get_stripe_client()

    now = timezone.now()
    deadline = time.monotonic() + time_budget_seconds
    known_customers = set(
        Subscription.objects.exclude(stripe_customer_id__isnull=True)  # ty: ignore[unresolved-attribute]
        .exclude(stripe_customer_id="")
        .values_list("stripe_customer_id", flat=True)
    )
    pending = 0

    try:
        invoices = client.v1.invoices.list(
            params={
                "status": "paid",
                "created": {"gte": int((now - lookback).timestamp())},
                "limit": RECONCILE_INVOICE_PAGE_SIZE,
            }
        ).auto_paging_iter()
        for invoice in invoices:
            if time.monotonic() >= deadline:
                summary["deadline_exceeded"] = True
                logger.error(
                    "reconcile: %.0fs budget exhausted after %d invoices; stopping",
                    time_budget_seconds,
                    summary["invoices_seen"],
                )
                break
            if summary["invoices_seen"] >= RECONCILE_MAX_INVOICES:
                summary["capped"] = True
                logger.warning(
                    "reconcile: hit the %d-invoice sanity cap; remaining invoices not examined",
                    RECONCILE_MAX_INVOICES,
                )
                break
            summary["invoices_seen"] += 1
            customer_id = _invoice_customer_id(invoice=invoice)
            if customer_id not in known_customers:
                continue
            summary["invoices_matched"] += 1
            try:
                if ProcessedPayment.objects.filter(  # ty: ignore[unresolved-attribute]
                    stripe_invoice_id=invoice.id
                ).exists():
                    summary["already_applied"] += 1
                    continue
                pending += 1
                if dry_run:
                    if (
                        getattr(invoice, "status", None) == "paid"
                        and getattr(invoice, "amount_remaining", None) == 0
                    ):
                        summary["would_grant"] += 1
                        logger.info(
                            "reconcile dry-run: would grant invoice %s for customer %s",
                            invoice.id,
                            customer_id,
                        )
                    else:
                        summary["rejected"] += 1
                    continue
                if _grant_period_for_invoice(
                    invoice=invoice,
                    expected_customer_id=customer_id,
                ):
                    summary["recorded"] += 1
                    logger.warning(
                        "reconcile: recorded missed payment for customer %s from invoice %s",
                        customer_id,
                        invoice.id,
                    )
                else:
                    summary["rejected"] += 1
            except Exception as exc:
                summary["errors"] += 1
                logger.error(
                    "reconcile: invoice %s failed: %s",
                    getattr(invoice, "id", "?"),
                    sanitize_sdk_error_message(exc),
                )
    except Exception as exc:
        summary["errors"] += 1
        logger.error(
            "reconcile: listing invoices failed: %s",
            sanitize_sdk_error_message(exc),
        )

    if not dry_run and summary["errors"] and not summary["recorded"] and pending:
        logger.error(
            "reconcile: %d error(s) and NO payments recorded while %d paid invoice(s) were pending; "
            "paying customers may still lack access",
            summary["errors"],
            pending,
        )

    _requeue_stuck_events(summary=summary, now=now, dry_run=dry_run)

    logger.info(
        "reconcile summary: invoices_seen=%d matched=%d recorded=%d would_grant=%d "
        "already_applied=%d rejected=%d errors=%d events_requeued=%d capped=%s "
        "deadline_exceeded=%s dry_run=%s",
        summary["invoices_seen"],
        summary["invoices_matched"],
        summary["recorded"],
        summary["would_grant"],
        summary["already_applied"],
        summary["rejected"],
        summary["errors"],
        summary["events_requeued"],
        summary["capped"],
        summary["deadline_exceeded"],
        dry_run,
    )
    return summary


def recover_past_due_invoice(
    *, payment_method: dict, client: stripe.StripeClient | None = None
) -> str:
    """
    payment_method.attached handler, invoked from tasks.process_stripe_event
    with the event's stored `data.object`. When the customer's Stripe
    subscription is past_due, pays its open invoice(s) with the NEW payment
    method explicitly: left alone, Stripe keeps retrying the subscription's own
    default_payment_method. A no-op in every other case, notably the first
    checkout (subscription `incomplete`), a `canceled` subscription, a disputed
    or exempt account, and an unknown customer. Returns the outcome name.

    Idempotent: the pay call carries an idempotency key of invoice + method, a
    replay finds no open invoice once it is paid, and an invoice Stripe's own
    retry paid first is treated as success. A declined or unauthenticated card
    is recorded (payment_recovery_state="failed") and returns normally, since
    replaying it cannot succeed; connection and server errors are raised so
    the task retries. Access itself is granted by the payment_intent.succeeded
    the payment produces (or the nightly reconcile), never here.
    """
    customer_id = payment_method.get("customer")
    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")
    payment_method_id = payment_method.get("id")
    if not customer_id or not payment_method_id:
        logger.info("payment_method.attached had no customer or id; nothing to recover")
        return "ignored"

    local = (
        Subscription.objects.select_related("user")  # ty: ignore[unresolved-attribute]
        .filter(stripe_customer_id=customer_id)
        .first()
    )
    if local is None:
        logger.info("payment_method.attached for customer %s has no local Subscription", customer_id)
        return "unknown_customer"
    if local.status == "revoked" or is_exempt(user=cast(User, local.user)):
        return "not_eligible"
    if not local.stripe_subscription_id:
        return "no_subscription"

    if client is None:
        client = get_stripe_client()

    stripe_subscription = client.v1.subscriptions.retrieve(local.stripe_subscription_id)
    if stripe_subscription.status not in PAYMENT_RECOVERY_PAYABLE_STATUSES:
        return "not_past_due"

    open_invoices = [
        invoice
        for invoice in client.v1.invoices.list(
            params={
                "customer": customer_id,
                "subscription": local.stripe_subscription_id,
                "status": "open",
                "limit": 10,
            }
        ).auto_paging_iter()
        if invoice.amount_remaining > 0
    ]
    if not open_invoices:
        return "no_open_invoice"

    attempted_at = timezone.now()
    Subscription.objects.filter(pk=local.pk).exclude(status="revoked").update(  # ty: ignore[unresolved-attribute]
        payment_recovery_state="retrying", payment_recovery_attempted_at=attempted_at
    )

    def _settle(*, state: str) -> None:
        Subscription.objects.filter(  # ty: ignore[unresolved-attribute]
            pk=local.pk, payment_recovery_attempted_at=attempted_at
        ).update(payment_recovery_state=state)

    outcome = "paid"
    for invoice in reversed(open_invoices):
        try:
            client.v1.invoices.pay(
                invoice=invoice.id,
                params={"payment_method": payment_method_id},
                options={"idempotency_key": f"recover-{invoice.id}-{payment_method_id}"},
            )
        except stripe.CardError:
            logger.warning(
                "payment recovery for customer %s: card declined on invoice %s",
                customer_id,
                invoice.id,
            )
            _settle(state="failed")
            return "declined"
        except stripe.InvalidRequestError:
            if client.v1.invoices.retrieve(invoice.id).status == "paid":
                continue
            logger.warning(
                "payment recovery for customer %s: invoice %s could not be paid with %s",
                customer_id,
                invoice.id,
                payment_method_id,
            )
            _settle(state="")
            outcome = "rejected"
            break
    if outcome == "paid":
        logger.info("payment recovery for customer %s: paid %d invoice(s)", customer_id, len(open_invoices))
    return outcome


def revert_customer_email(*, token: object) -> bool:
    """
    Consumes the "this wasn't me" token from the old-address notice. Returns
    False for every rejection reason alike (the view maps that to one 400 body)
    and True once the token was honoured.

    Single-use without a table: valid only while User.email still equals the
    token's new address, so a revert (email is now the old address) or any
    later change kills it. Staff/superusers are never touched, mirroring
    sync_customer_email. The password is deliberately not modified.

    In one transaction, under the user row lock: restore User.email and
    blacklist every refresh token. If another account has taken the old
    address in the meantime the email is left alone (User.email is not
    unique, and a duplicate would let one owner receive the other's reset
    links) but the sessions are still killed; that path pushes nothing to
    Stripe and mails nobody, since the old address now belongs to someone
    else. After commit, best-effort: queue tasks.restore_stripe_customer_email
    (pushes the old address back to the Stripe Customer, with retries; the
    resulting customer.updated is a no-op in sync_customer_email) and the
    confirmation notice. A queueing failure cannot fail the request. Log lines
    carry ids only.
    """
    from .services import verify_email_revert_token
    from .tasks import restore_stripe_customer_email, send_email_reverted_notice

    payload = verify_email_revert_token(token=token)
    if payload is None:
        return False
    old_email = payload["old"].strip()
    new_email = payload["new"]
    if not old_email:
        return False

    with transaction.atomic():
        user = (
            User.objects.select_for_update().filter(pk=payload["uid"]).first()
        )
        if user is None or is_exempt(user=user):
            return False
        if _normalized_email(email=user.email) != _normalized_email(
            email=new_email
        ):
            return False

        conflict = (
            User.objects.filter(email__iexact=old_email)
            .exclude(pk=user.pk)
            .exists()
        )
        if not conflict:
            user.email = old_email
            user.save(update_fields=["email"])
        revoked = blacklist_all_refresh_tokens(user=user)
        customer_id = (
            Subscription.objects.filter(user_id=user.pk)  # ty: ignore[unresolved-attribute]
            .values_list("stripe_customer_id", flat=True)
            .first()
            or ""
        )

    if conflict:
        logger.warning(
            "email revert for user %s: old address now belongs to another account; "
            "email left unchanged, %s refresh token(s) revoked",
            user.pk,
            revoked,
        )
        return True
    logger.info(
        "reverted email for user %s; %s refresh token(s) revoked",
        user.pk,
        revoked,
    )

    if customer_id:
        try:
            restore_stripe_customer_email.delay(
                user_id=user.pk, customer_id=customer_id, email=old_email
            )
        except Exception:
            logger.error(
                "could not enqueue stripe email restore for user %s; broker unavailable",
                user.pk,
            )
    try:
        send_email_reverted_notice.delay(user_id=user.pk, recipient=old_email)
    except Exception:
        logger.error(
            "could not enqueue email-restored notice for user %s; broker unavailable",
            user.pk,
        )
    return True


def revoke_for_dispute(
    *, dispute: dict, client: stripe.StripeClient | None = None
) -> None:
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

    if client is None:
        client = get_stripe_client()
    try:
        pi = client.v1.payment_intents.retrieve(payment_intent_id)
    except stripe.InvalidRequestError:
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

    # Same ExpandableField[Customer] shape normalized in fulfill_payment_intent --
    # never expanded here, so always a plain id string in practice.
    customer_id = pi.customer if isinstance(pi.customer, str) else pi.customer.id
    local = Subscription.objects.filter(stripe_customer_id=customer_id)  # ty: ignore[unresolved-attribute]
    if not local.exists():
        logger.warning(
            "disputed payment_intent %s customer %s has no local Subscription; "
            "revoking and cancelling nothing",
            payment_intent_id,
            customer_id,
        )
        return

    # Local revoke first and in its own statement: it must land even if the
    # Stripe cancel below fails, and no DB lock is held across Stripe calls.
    # Already-revoked rows are excluded so a replay does not move
    # access_expires_at.
    local.exclude(status="revoked").update(
        status="revoked", access_expires_at=timezone.now()
    )

    # Both Stripe clean-ups are attempted whatever the other does; the first
    # failure is raised afterwards so process_stripe_event retries the whole
    # (idempotent) handler.
    failure: stripe.StripeError | None = None
    cancelled: list[str] = []
    expired_sessions = 0
    try:
        cancelled = _cancel_live_stripe_subscriptions(
            customer_id=customer_id, client=client
        )
    except stripe.StripeError as exc:
        failure = exc
    try:
        expired_sessions = _expire_open_checkout_sessions(
            customer_id=customer_id,
            known_session_id=str(local.values_list("last_checkout_session_id", flat=True).first() or ""),
            client=client,
        )
    except stripe.StripeError as exc:
        failure = failure or exc
    if failure is not None:
        raise failure
    # This is the "flag for the operator" step from the plan -- there is
    # no ticketing/ops-alert integration in this codebase yet, so a
    # loud, unambiguous log line is the mechanism.
    logger.critical(
        "REVOKED subscription for stripe customer %s due to charge.dispute.created and "
        "cancelled stripe subscription(s) %s immediately and expired %d open checkout "
        "session(s) -- operator review required",
        customer_id,
        ", ".join(cancelled) or "(none live)",
        expired_sessions,
    )


def schedule_plan_change(
    *,
    user: User,
    plan_slug: str,
    client: stripe.StripeClient | None = None,
) -> dict | None:
    """
    Schedules a switch to `plan_slug` at the end of the current period, with no
    proration. Verified recipe (sandbox, API 2026-08-26.dahlia): a schedule
    created from_subscription, then modified into two phases -- the current
    price until its period end, then the target price with
    billing_cycle_anchor="phase_start". That anchor is what makes Stripe raise
    one invoice for the target price at the boundary; an immediate switch, or
    a second phase on the default anchor, creates NO invoice (a free period in
    Stripe and a lock-out here, since access is granted only by payments).
    The boundary invoice reaches us as an ordinary payment_intent.succeeded.

    The second phase's length comes from the target Stripe Price's recurring
    interval, not from Plan.billing_interval alone; a disagreement between the
    two raises PlanPriceMismatchError before anything is created in Stripe.
    The whole Stripe sequence runs under the per-user plan-change lock.

    Returns the pending-change payload, or None if the schedule Stripe handed
    back has no pending phase (should not happen; the view then answers
    {"pending": null} and the frontend refetches).
    """
    if is_exempt(user=user):
        raise ExemptAccountError()
    try:
        target = Plan.objects.get(slug=plan_slug, is_active=True)  # ty: ignore[unresolved-attribute]
    except Plan.DoesNotExist:  # ty: ignore[unresolved-attribute]
        raise UnknownPlanError(f"No active plan with slug {plan_slug!r}.")

    if client is None:
        client = get_interactive_stripe_client()
    local = _changeable_subscription(user=user)
    with _plan_change_lock(user=user, required=True):
        return _schedule_plan_change_locked(
            user=user, local=local, target=target, client=client
        )


def subscription_status_payload(*, subscription: Subscription) -> dict:
    """
    Shared response shape for GET /billing/subscription/ and GET
    /billing/session/<id>/ -- one function, so the two endpoints can never
    quietly drift into two different shapes for the same underlying state.
    fulfill_checkout_session adds its own "granted" key on top of this.
    """
    from .services import quota_snapshot

    plan = subscription.plan
    exempt = bool(subscription.user.is_staff or subscription.user.is_superuser)  # ty: ignore[unresolved-attribute]
    return {
        "status": subscription.status,
        "plan": {"slug": plan.slug, "name": plan.name} if plan else None,  # ty: ignore[unresolved-attribute]
        "access_expires_at": subscription.access_expires_at,
        "has_active_access": exempt or subscription.has_active_access,
        "exempt": exempt,
        # Strictly User.is_staff, unlike `exempt` (staff or superuser): only
        # staff get the manual Run Now button.
        "is_staff": bool(subscription.user.is_staff),  # ty: ignore[unresolved-attribute]
        # True once a Stripe Customer exists and there is billing to manage.
        # Reads only the in-memory row, so the unsaved placeholder used by
        # BillingSubscriptionView stays DB-free.
        "can_manage_billing": bool(subscription.stripe_customer_id)
        and not exempt,
        # cast, not a runtime check: subscription.user is a required FK, always
        # a real User at runtime -- the static type is only a FK descriptor
        # because no django-stubs is installed (see [tool.ty] in pyproject.toml).
        "quota": quota_snapshot(
            user=cast(User, subscription.user), subscription=subscription
        ),
        # Not derivable locally: nothing here records whether the lapse was a
        # failed payment or a cancellation (that needs Stripe's subscription
        # status and cancellation_details.reason, which no handled event
        # delivers). Null is the frontend's defined "unknown" fallback.
        "lapse_reason": None,
        "payment_recovery": payment_recovery_payload(subscription=subscription),
    }


def sync_customer_email(
    *, customer: dict, previous_attributes: dict | None
) -> bool:
    """
    customer.updated handler, invoked from tasks.process_stripe_event with the
    stored `data.object` (a plain dict) and `data.previous_attributes`. Mirrors
    an email edited in the Stripe Customer Portal onto User.email, since
    PasswordResetRequestView keys on it. Returns True only when User.email
    was actually changed.

    Acts only on an event whose `previous_attributes` is a non-empty mapping
    containing `email`, i.e. one that really changed the email. Absent, empty,
    or email-less previous_attributes is a no-op with no value comparison:
    otherwise, if restoring the old address on the Stripe customer after a
    revert failed, any later customer.updated would re-sync the attacker's
    address onto the user.

    Resolves the account by Subscription.stripe_customer_id and nothing else
    (never by email). Idempotent: once User.email equals the Stripe value a
    replay is a no-op, so the notices go out exactly once. Never pushes
    anything back to Stripe. Log lines carry ids only, never addresses.

    The notices are queued inside the transaction on purpose: if the broker
    is down the exception rolls the email change back and process_stripe_event
    retries the whole thing, instead of committing a change nobody is told
    about. The tasks are handed the addresses, not a DB read, so a worker
    that runs before commit still sends the right thing.
    """
    customer_id = customer.get("id") or ""
    if not customer_id:
        logger.warning("customer.updated had no customer id; nothing to sync")
        return False
    if (
        not isinstance(previous_attributes, Mapping)
        or "email" not in previous_attributes
    ):
        logger.info(
            "customer.updated for %s did not change the email; nothing to sync",
            customer_id,
        )
        return False

    new_email = (customer.get("email") or "").strip()

    from .services import generate_email_revert_token
    from .tasks import send_account_email_change_notice

    with transaction.atomic():
        subscription = Subscription.objects.filter(  # ty: ignore[unresolved-attribute]
            stripe_customer_id=customer_id
        ).first()
        if subscription is None:
            logger.info(
                "customer.updated for %s has no local Subscription; ignoring",
                customer_id,
            )
            return False
        user = User.objects.select_for_update().get(pk=subscription.user_id)
        if is_exempt(user=user):
            logger.info(
                "customer.updated for exempt user %s; email not synced", user.pk
            )
            return False
        if _normalized_email(email=new_email) == _normalized_email(
            email=user.email
        ):
            return False
        if not new_email:
            logger.warning(
                "customer.updated for user %s carried an empty email; skipping",
                user.pk,
            )
            return False
        try:
            validate_email(new_email)
        except ValidationError:
            logger.warning(
                "customer.updated for user %s carried an invalid email; skipping",
                user.pk,
            )
            return False
        if len(new_email) > User._meta.get_field("email").max_length:  # ty: ignore[unresolved-attribute]
            logger.warning(
                "customer.updated for user %s carried an over-long email; skipping",
                user.pk,
            )
            return False
        # User.email is not unique. Creating a second holder would make password
        # reset mail every holder and let one account's owner receive another's link.
        if (
            User.objects.filter(email__iexact=new_email)
            .exclude(pk=user.pk)
            .exists()
        ):
            logger.warning(
                "customer.updated for user %s wanted an email another account already uses; skipping",
                user.pk,
            )
            return False

        old_email = user.email
        user.email = new_email
        user.save(update_fields=["email"])
        logger.info(
            "synced email for user %s from stripe customer %s",
            user.pk,
            customer_id,
        )

        if old_email:
            # Minted here, in the same transaction as the change it undoes, and
            # handed to the old-address notice only -- never the new address's.
            send_account_email_change_notice.delay(
                user_id=user.pk,
                recipient=old_email,
                old_email=old_email,
                new_email=new_email,
                is_new_address=False,
                revert_token=generate_email_revert_token(
                    user_id=user.pk, old_email=old_email, new_email=new_email
                ),
            )
        send_account_email_change_notice.delay(
            user_id=user.pk,
            recipient=new_email,
            old_email=old_email,
            new_email=new_email,
            is_new_address=True,
        )
    return True


def validate_plan_price(
    *,
    price_id: str,
    amount_cents: int,
    currency: str,
    billing_interval: str,
    client: stripe.StripeClient | None = None,
) -> list[str]:
    """
    Checks a Plan's Stripe Price against what the operator typed in Django
    admin, and returns every problem as a human-readable line (empty list ==
    the price is acceptable). Fails closed: a missing key or an unreachable
    Stripe is a problem too, never a silent skip. Runs inside an admin HTTP
    request, so it uses the interactive client (fast fail, no retries).

    Checks: the Price exists; is active; unit_amount and currency match the
    Plan; it is recurring with the Plan's interval and interval_count 1; its
    livemode matches the configured key's mode; it is licensed (not metered),
    has no transform_quantity and no trial period; and its Product (expanded)
    is not archived/deleted. Messages never contain the key or raw SDK error
    text -- failures are logged through sanitize_sdk_error_message instead.
    """
    unverified = (
        "The price could not be verified with Stripe, so nothing was saved. "
    )
    try:
        if client is None:
            client = get_interactive_stripe_client()
        price = client.v1.prices.retrieve(
            price_id, params={"expand": ["product"]}
        )
    except ImproperlyConfigured:
        return [
            unverified
            + "Stripe is not configured on this server (STRIPE_SECRET_KEY is unset)."
        ]
    except stripe.InvalidRequestError as exc:
        if exc.code == "resource_missing":
            return [
                f"Stripe has no Price with id {price_id!r} in this account and mode "
                f"(a {'live' if _key_is_live() else 'test'}-mode key is configured)."
            ]
        logger.error(
            "plan price check: Stripe rejected the lookup of %s: %s",
            price_id,
            sanitize_sdk_error_message(exc),
        )
        return [unverified + "Stripe rejected the lookup; check the price id."]
    except stripe.StripeError as exc:
        logger.error(
            "plan price check: could not reach Stripe for %s: %s",
            price_id,
            sanitize_sdk_error_message(exc),
        )
        return [unverified + "Stripe could not be reached; try again shortly."]

    problems: list[str] = []
    if not price.active:
        problems.append("The Price is not active in Stripe.")
    if price.unit_amount is None:
        problems.append("The Price has no fixed unit_amount (tiered or custom pricing).")
    elif price.unit_amount != amount_cents:
        problems.append(
            f"Stripe's amount is {price.unit_amount} cents but this Plan says {amount_cents}."
        )
    if (price.currency or "").lower() != currency.lower():
        problems.append(
            f"Stripe's currency is {price.currency.upper()} but this Plan says {currency.upper()}."
        )
    recurring = price.recurring
    if recurring is None:
        problems.append("The Price is not recurring (a one-time Price cannot back a subscription).")
    else:
        if recurring.interval != billing_interval:
            problems.append(
                f"Stripe's billing interval is {recurring.interval!r} but this Plan says "
                f"{billing_interval!r}."
            )
        if recurring.interval_count != 1:
            problems.append(
                f"The Price bills every {recurring.interval_count} {recurring.interval}s; "
                "only an interval count of 1 is supported."
            )
        if recurring.usage_type != "licensed":
            problems.append(
                f"The Price is metered (usage_type {recurring.usage_type!r}); invoices would "
                "not equal unit_amount. Only licensed pricing is supported."
            )
        if recurring.trial_period_days:
            problems.append(
                f"The Price carries a {recurring.trial_period_days}-day free trial "
                "(trial_period_days); the first invoice would be for nothing. "
                "Trials are not supported."
            )
    if price.transform_quantity:
        problems.append(
            "The Price has a transform_quantity, so what is billed would not equal unit_amount."
        )
    if price.livemode != _key_is_live():
        problems.append(
            f"The Price is {'live' if price.livemode else 'test'}-mode but the configured "
            f"Stripe key is {'live' if _key_is_live() else 'test'}-mode."
        )
    product = price.product
    if not isinstance(product, str) and (
        getattr(product, "deleted", False)
        or getattr(product, "active", True) is False
    ):
        problems.append("The Price's Product is archived or deleted in Stripe.")
    return problems


def _access_expires_at_for(*, line: Any) -> datetime | None:
    """
    The matched line's own period.end (its billing-period boundary) plus
    grace, or None when the line carries no period. There is deliberately no
    locally computed fallback: access is dated only by Stripe, and a locally
    guessed date would drift from Stripe's billing date (and, being always
    later than the current expiry, defeat the monotonic-grant guard). The
    caller records the payment and grants nothing on None.
    """
    period = getattr(line, "period", None)
    period_end = getattr(period, "end", None) if period else None
    if not period_end:
        return None
    return (
        datetime.fromtimestamp(period_end, tz=dt_timezone.utc)
        + _PERIOD_END_GRACE
    )


def _cancel_live_stripe_subscriptions(
    *, customer_id: str, client: stripe.StripeClient
) -> list[str]:
    """
    Cancels every live Stripe subscription of the customer immediately, with no
    proration credit and no final invoice (the funds are being withdrawn).
    Idempotent: a subscription that is already canceled counts as done. Any
    other Stripe failure is raised after all subscriptions were attempted, so
    process_stripe_event's retry re-runs the handler.
    """
    subscriptions = list(
        client.v1.subscriptions.list(
            params={"customer": customer_id, "status": "all", "limit": 100}
        ).auto_paging_iter()
    )
    live_ids = [
        sub.id
        for sub in subscriptions
        if sub.status in DISPUTE_LIVE_SUBSCRIPTION_STATUSES
    ]
    cancelled: list[str] = []
    failure: stripe.StripeError | None = None
    for subscription_id in live_ids:
        try:
            client.v1.subscriptions.cancel(
                subscription_id,
                params={"invoice_now": False, "prorate": False},
            )
        except stripe.InvalidRequestError as exc:
            try:
                already_done = (
                    client.v1.subscriptions.retrieve(subscription_id).status
                    == "canceled"
                )
            except stripe.StripeError:
                already_done = False
            if not already_done:
                logger.error(
                    "dispute: could not cancel stripe subscription %s",
                    subscription_id,
                )
                failure = failure or exc
                continue
        except stripe.StripeError as exc:
            logger.error(
                "dispute: could not cancel stripe subscription %s",
                subscription_id,
            )
            failure = failure or exc
            continue
        cancelled.append(subscription_id)
    if failure is not None:
        raise failure
    return cancelled


def _changeable_subscription(*, user: User) -> Subscription:
    """The caller's own Subscription, or a refusal. Ownership is by FK, never by client input."""
    if is_exempt(user=user):
        raise ExemptAccountError()
    subscription = (
        Subscription.objects.select_related("plan").filter(user=user).first()  # ty: ignore[unresolved-attribute]
    )
    if (
        subscription is None
        or not subscription.stripe_subscription_id
        or not subscription.has_active_access
    ):
        raise NoActiveSubscriptionError()
    return subscription


def _current_price_ids(*, stripe_subscription: Any) -> set[str]:
    return {
        _price_id_of(price=item.price)
        for item in stripe_subscription["items"].data
    }


def _expire_open_checkout_sessions(
    *, customer_id: str, known_session_id: str, client: stripe.StripeClient
) -> int:
    """
    Expires every OPEN Checkout Session of a disputed customer (plus the
    locally remembered last_checkout_session_id if Stripe still reports it
    open), so a session created before the dispute cannot be paid into the
    locked account. Returns how many this call expired. Idempotent: a session
    that is already expired or complete counts as done (and is not counted).
    Any other Stripe failure is raised after every session was attempted, so
    process_stripe_event's retry re-runs the handler.
    """
    open_ids = [
        session.id
        for session in client.v1.checkout.sessions.list(
            params={"customer": customer_id, "status": "open", "limit": 100}
        ).auto_paging_iter()
    ]
    failure: stripe.StripeError | None = None
    if known_session_id and known_session_id not in open_ids:
        try:
            if client.v1.checkout.sessions.retrieve(known_session_id).status == "open":
                open_ids.append(known_session_id)
        except stripe.InvalidRequestError:
            pass
        except stripe.StripeError as exc:
            logger.error(
                "dispute: could not look up checkout session %s", known_session_id
            )
            failure = exc
    expired = 0
    for session_id in open_ids:
        try:
            client.v1.checkout.sessions.expire(session_id)
        except stripe.InvalidRequestError as exc:
            try:
                already_done = (
                    client.v1.checkout.sessions.retrieve(session_id).status != "open"
                )
            except stripe.StripeError:
                already_done = False
            if not already_done:
                logger.error(
                    "dispute: could not expire checkout session %s", session_id
                )
                failure = failure or exc
            continue
        except stripe.StripeError as exc:
            logger.error("dispute: could not expire checkout session %s", session_id)
            failure = failure or exc
            continue
        expired += 1
    if failure is not None:
        raise failure
    return expired


def _get_or_create_customer(
    *, user: User, subscription: Subscription, client: stripe.StripeClient
) -> str:
    """
    Caller must hold a row lock (select_for_update) on `subscription` for the
    duration of this call -- two concurrent double-submits of Checkout would
    otherwise both read stripe_customer_id as empty and each create a
    separate Stripe Customer, with the last local .save() winning and
    orphaning the other Customer (see create_checkout_session).

    The one call in checkout given a network retry (see
    _CUSTOMER_CREATE_MAX_NETWORK_RETRIES): the retry reuses the call's
    Idempotency-Key, so a create that timed out after reaching Stripe
    returns the same Customer instead of making a second one.
    """
    if subscription.stripe_customer_id:
        return str(subscription.stripe_customer_id)
    customer = client.v1.customers.create(
        params=CustomerCreateParams(
            # str(...): user.email/user.id are Django field descriptors at
            # the static level (EmailField, no django-stubs installed), but
            # plain str at runtime -- see [tool.ty] in pyproject.toml.
            email=str(user.email),
            metadata={"user_id": str(user.id)},  # ty: ignore[unresolved-attribute]
        ),
        options={"max_network_retries": _CUSTOMER_CREATE_MAX_NETWORK_RETRIES},
    )
    subscription.stripe_customer_id = customer.id  # ty: ignore[invalid-assignment]
    subscription.save(update_fields=["stripe_customer_id"])
    return str(customer.id)


def _grant_period_for_invoice(
    *,
    invoice: Any,
    expected_customer_id: str | None = None,
    payment_intent_id: str = "",
) -> bool:
    """
    Steps 5-10 of the payment_intent.succeeded validation chain -- shared by
    fulfill_payment_intent (one call per invoice found via InvoicePayments),
    fulfill_checkout_session (a single invoice resolved directly from the
    Checkout Session) and reconcile_subscriptions. Every rejection here is
    documented in the plan as "log and grant nothing," never an exception.

    Returns True when a payment was RECORDED (a ProcessedPayment row was
    written by this call), whether or not access was extended; False when
    nothing was recorded -- a rejection, or an invoice already applied. Both
    "recorded but grants nothing" cases below return True.

    Gates on the invoice itself being paid (status == "paid" and
    amount_remaining == 0, per stripe==15.6.1 Invoice.status / amount_remaining)
    so every entry path, present or future, inherits the payment check. A
    subscription whose first payment fails leaves an "open" invoice with
    amount_remaining > 0 attached to the Checkout Session.

    A `revoked` (disputed) Subscription can never gain access through a
    payment, by any path, until the operator unlocks it in Django admin. The
    check is made under the row lock so a dispute revoke landing mid-loop
    cannot be overwritten. The payment is still recorded (never reprocessed)
    and logged at CRITICAL: money reached a locked account and needs a manual
    refund. Likewise an invoice line with no period.end is recorded, grants
    nothing and logs at CRITICAL -- access is dated only by Stripe.

    Grants are monotonic: access_expires_at only ever moves to
    max(current, computed for this invoice). A ProcessedPayment row is
    always recorded for any invoice that reaches this point, because it
    proved a real successful payment even if it wasn't the invoice that
    produced the furthest-out expiry (e.g. an old subscription's late
    renewal arriving after a newer one was already applied). status, plan
    and stripe_subscription_id, however, are only overwritten when this
    invoice *is* the new maximum -- they describe the subscription that
    earned the current expiry, and must not be dragged back to an older
    subscription's values just because its payment settled late.
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
            "stripe invoice %s matches no known Plan price; skipping",
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
        local_subscription = Subscription.objects.select_related("user").get(  # ty: ignore[unresolved-attribute]
            stripe_customer_id=invoice_customer_id
        )
    except Subscription.DoesNotExist:  # ty: ignore[unresolved-attribute]
        logger.warning(
            "stripe invoice %s customer %s has no local Subscription; skipping",
            invoice.id,
            invoice_customer_id,
        )
        return False

    beta_status = (
        BetaSignup.objects.filter(invited_user=local_subscription.user)  # ty: ignore[unresolved-attribute]
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
        locked = Subscription.objects.select_for_update().get(  # ty: ignore[unresolved-attribute]
            pk=local_subscription.pk
        )
        new_expiry = _access_expires_at_for(line=line)
        if not _record_payment(
            invoice=invoice,
            payment_intent_id=payment_intent_id,
            stripe_subscription_id=stripe_subscription_id,
            subscription=locked,
            period_end=new_expiry,
        ):
            return False

        if locked.status == "revoked":
            logger.critical(
                "MONEY RECEIVED ON A LOCKED ACCOUNT: stripe invoice %s (%s cents paid) for "
                "revoked subscription %s (stripe customer %s) was recorded but granted NOTHING. "
                "Needs a manual refund in Stripe; unlock the account in Django admin only "
                "after that is settled.",
                invoice.id,
                invoice.amount_paid,
                locked.pk,
                invoice_customer_id,
            )
            return True
        if new_expiry is None:
            logger.critical(
                "PAID INVOICE WITH NO PERIOD: stripe invoice %s (%s cents paid) for "
                "subscription %s (stripe customer %s) matched plan %s but its line carries no "
                "period.end. The payment was recorded and access was NOT granted; the operator "
                "must resolve it (comp access with grant_access, or refund).",
                invoice.id,
                invoice.amount_paid,
                locked.pk,
                invoice_customer_id,
                plan.slug,
            )
            return True

        current_expiry = locked.access_expires_at
        # Access is a projection over a set of payments, and payments can
        # arrive out of order (a lapsed subscriber's old subscription
        # settling late, a PaymentIntent that lists its invoices in no
        # guaranteed order, a wide reconciliation sweep). The projection
        # must be commutative: applying invoices in any order must land on
        # the same state. ProcessedPayment (the unique constraint above)
        # already makes it idempotent; taking the max here makes it
        # commutative too. An invoice that isn't the new maximum still
        # proves a real, successful payment, so it still records a
        # ProcessedPayment row -- but it must not touch status, plan or
        # stripe_subscription_id, which would roll them back to whatever
        # that older invoice was billed against. All three must only ever
        # be written by the invoice that actually produced the current
        # (latest) expiry.
        produces_new_max = current_expiry is None or new_expiry > current_expiry
        final_expiry = new_expiry if produces_new_max else current_expiry
        locked.access_expires_at = final_expiry
        locked.last_paid_at = timezone.now()
        if produces_new_max:
            locked.status = "active"
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
        if not produces_new_max:
            logger.info(
                "stripe invoice %s recorded for subscription %s but did not extend "
                "access (existing expiry %s >= this invoice's %s); "
                "status/plan/stripe_subscription_id left unchanged",
                invoice.id,
                locked.pk,
                current_expiry,
                new_expiry,
            )
    return True


def _invoice_customer_id(*, invoice: Any) -> str:
    customer = getattr(invoice, "customer", None)
    if isinstance(customer, str):
        return customer
    return getattr(customer, "id", "") or ""


def _invoice_is_settled(*, invoice: Any) -> bool:
    return (
        getattr(invoice, "status", None) == "paid"
        and getattr(invoice, "amount_remaining", None) == 0
    )


def _key_is_live() -> bool:
    """Same test/live rule configure_stripe_portal uses: anything but an sk_test_/rk_test_ key is live."""
    return not settings.STRIPE_SECRET_KEY.startswith(("sk_test_", "rk_test_"))


def _live_schedule(
    *, stripe_subscription: Any, client: stripe.StripeClient
) -> Any | None:
    """
    The attached schedule if it is still in force, else None. A canceled or
    released schedule can linger on `subscription.schedule`, and must not be
    mistaken for one.
    """
    schedule = getattr(stripe_subscription, "schedule", None)
    if schedule is None:
        return None
    if isinstance(schedule, str):
        schedule = client.v1.subscription_schedules.retrieve(schedule)
    if getattr(schedule, "status", None) not in ("active", "not_started"):
        return None
    return schedule


def _match_plan_line(*, invoice: Any) -> tuple[Plan, Any] | None:
    """
    Walks invoice.lines defensively, matching each line's price id against
    ANY known Plan -- never lines.data[0]: a proration line or a second
    subscription item would make position 0 the wrong line, and this is the
    exact validation the plan asks for ("Validate the Price, not the
    amount"). Returns (plan, matched_line) for the first match, so the
    caller can also read the matched line's own period for the grant
    boundary, or None if no line matches any known Plan price.

    Deliberately not filtered to is_active=True: that flag means "offered at
    checkout / plan-change target", not "honoured at renewal". A retired
    Plan row (is_active switched off in admin) must stay matchable forever, or
    every existing subscriber still billing on the old Price would renew
    into a grant of nothing at their very next invoice.
    """
    plan_by_price = {p.stripe_price_id: p for p in Plan.objects.all()}  # ty: ignore[unresolved-attribute]
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


def _metadata_dict(obj: Any) -> dict:
    """
    Stripe metadata as a plain dict. On a real SDK object `.metadata` is a StripeObject, which is
    NOT a dict and has no `.get()` (calling it raises AttributeError); it must go through
    `.to_dict()`. Tests and older code paths hand in plain dicts, so accept both.
    """
    md = getattr(obj, "metadata", None)
    if not md:
        return {}
    return md.to_dict() if hasattr(md, "to_dict") else dict(md)


def _normalized_email(*, email: str | None) -> str:
    return (email or "").strip().lower()


def _pending_change_payload(*, schedule: Any) -> dict | None:
    phase = _pending_phase(schedule=schedule)
    if phase is None:
        return None
    items = phase["items"]
    price_id = _price_id_of(price=items[0]["price"]) if items else ""
    plan = (
        Plan.objects.filter(stripe_price_id=price_id).first()  # ty: ignore[unresolved-attribute]
        if price_id
        else None
    )
    return {
        "slug": plan.slug if plan else None,
        "name": plan.name if plan else None,
        "effective_at": datetime.fromtimestamp(
            phase.start_date, tz=dt_timezone.utc
        ),
    }


def _pending_phase(*, schedule: Any) -> Any | None:
    """
    The next phase that has not started yet, or None if the schedule is on its
    last phase. Deliberately relative to schedule.current_phase rather than
    the wall clock, so it also holds for test-clock customers.

    A schedule on its last phase is a *spent shell*: with end_behavior=release
    it stays attached for the whole final phase (a year, for an annual
    switch), changes nothing, and yet blocks Stripe-side cancellation of the
    subscription.
    """
    phases = list(getattr(schedule, "phases", None) or [])
    current = getattr(schedule, "current_phase", None)
    if current is None:
        return phases[1] if len(phases) > 1 else None
    later = [phase for phase in phases if phase.start_date > current.start_date]
    return later[0] if later else None


@contextmanager
def _plan_change_lock(*, user: User, required: bool) -> Iterator[bool]:
    """
    Yields whether this caller holds the per-user lock (cache.add is atomic).

    required=True (change-plan POST/DELETE): contention raises
    ChangeInProgressError (409), and a cache failure raises ImproperlyConfigured
    (the existing 503 mapping). It fails CLOSED there, because proceeding
    unlocked is exactly the race the lock exists to prevent, and the user can
    simply retry.

    required=False (the portal's optional spent-shell release): contention or a
    cache failure yields False and the caller skips the release. The portal
    itself must never depend on the lock.
    """
    key = f"plan-change-lock:{user.pk}"
    token = uuid.uuid4().hex
    acquired = False
    cache_failed = False
    try:
        acquired = bool(
            cache.add(key, token, timeout=_PLAN_CHANGE_LOCK_TTL_SECONDS)
        )
    except Exception as exc:
        cache_failed = True
        cache_exc = exc
    if not acquired and required:
        if cache_failed:
            raise ImproperlyConfigured(
                "plan-change lock unavailable (cache error)"
            ) from cache_exc
        raise ChangeInProgressError()
    if cache_failed:
        logger.warning(
            "plan-change lock unavailable, skipping optional work: %s",
            type(cache_exc).__name__,
        )
    if not acquired:
        yield False
        return
    try:
        yield True
    finally:
        try:
            if cache.get(key) == token:
                cache.delete(key)
        except Exception:
            logger.warning(
                "could not release plan-change lock for user %s; it will expire",
                user.pk,
            )


def _price_id_of(*, price: Any) -> str:
    return price if isinstance(price, str) else getattr(price, "id", "")


def _record_payment(
    *,
    invoice: Any,
    payment_intent_id: str,
    stripe_subscription_id: str,
    subscription: Subscription,
    period_end: datetime | None,
) -> bool:
    """
    Writes the ProcessedPayment row for `invoice`; False if it already exists
    (this invoice was already applied). Must run inside the caller's
    transaction.atomic() with the Subscription row locked.

    Nested atomic() -> a savepoint, not the outer transaction: an
    IntegrityError here (the unique constraint on stripe_invoice_id) must
    only roll back this insert, not the Subscription lock acquired by the
    caller. Scoped to this one statement so a later IntegrityError on
    subscription.save() (e.g. a stripe_subscription_id collision -- a real
    data problem) is never caught here and misread as "already applied."
    """
    try:
        with transaction.atomic():
            ProcessedPayment.objects.create(  # ty: ignore[unresolved-attribute]
                stripe_invoice_id=invoice.id,
                stripe_payment_intent_id=payment_intent_id,
                stripe_subscription_id=stripe_subscription_id,
                subscription=subscription,
                amount_paid_cents=invoice.amount_paid,
                period_end=period_end,
            )
    except IntegrityError:
        logger.info(
            "stripe invoice %s already applied; recording nothing", invoice.id
        )
        return False
    return True


def _release_spent_schedule(
    *, stripe_subscription: Any, client: stripe.StripeClient
) -> None:
    """Release a schedule that has no pending phase so it stops blocking cancellation."""
    schedule = _live_schedule(stripe_subscription=stripe_subscription, client=client)
    if schedule is not None and _pending_phase(schedule=schedule) is None:
        client.v1.subscription_schedules.release(schedule.id)
        logger.info("released spent subscription schedule %s", schedule.id)


def _requeue_stuck_events(
    *, summary: dict, now: datetime, dry_run: bool
) -> None:
    from .tasks import process_stripe_event

    stuck = StripeEvent.objects.filter(  # ty: ignore[unresolved-attribute]
        status__in=["received", "failed"],
        received_at__lte=now - STUCK_EVENT_MIN_AGE,
        received_at__gte=now - STUCK_EVENT_MAX_AGE,
    ).order_by("pk")[:RECONCILE_MAX_CANDIDATES]
    for event in stuck:
        if not event.payload:
            summary["events_skipped_no_payload"] += 1
            logger.warning(
                "reconcile: stuck StripeEvent %s has no payload; cannot replay",
                event.stripe_event_id,
            )
            continue
        if not dry_run:
            process_stripe_event.delay(stripe_event_id=event.stripe_event_id)
        summary["events_requeued"] += 1


def _reusable_open_session_url(
    *, subscription: Subscription, plan: Plan, client: stripe.StripeClient
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
    - Open but created under the other STRIPE_REQUIRE_TOS_CONSENT setting
      (e.g. before the flag existed): expire it the same way. A reused
      session would otherwise skip the Terms checkbox, or keep demanding
      one the operator has switched off. The session's own
      `consent_collection` is the record of what it was created with, so no
      extra metadata marker is needed.
    - Anything else (complete, expired): nothing to reuse.
    """
    session_id = subscription.last_checkout_session_id
    if not session_id:
        return None
    session_id = str(session_id)
    try:
        existing = client.v1.checkout.sessions.retrieve(session_id)
    except stripe.InvalidRequestError:
        return None
    if existing.status != "open":
        return None
    # Both the slug *and* the price id must match. The slug alone survives
    # exactly the change that matters: if a Plan row is replaced on the same
    # slug with a different Stripe Price (test -> live, or a genuine price
    # change), a stale open session for "beta-monthly" would otherwise be
    # handed back and the user would check out against the old Price --
    # the same "validate the price, not a proxy for it" reasoning the plan
    # applies everywhere else in this file.
    metadata = _metadata_dict(existing)
    consent_collection = getattr(existing, "consent_collection", None)
    has_tos_consent = (
        getattr(consent_collection, "terms_of_service", None) == "required"
    )
    if (
        metadata.get("plan_slug") == plan.slug
        and metadata.get("price_id") == plan.stripe_price_id
        and has_tos_consent == settings.STRIPE_REQUIRE_TOS_CONSENT
    ):
        return existing.url
    client.v1.checkout.sessions.expire(session_id)
    return None


def _schedule_plan_change_locked(
    *,
    user: User,
    local: Subscription,
    target: Plan,
    client: stripe.StripeClient,
) -> dict | None:
    stripe_sub = _stripe_subscription_with_schedule(
        stripe_subscription_id=str(local.stripe_subscription_id), client=client
    )
    if getattr(stripe_sub, "status", None) != "active":
        raise NoActiveSubscriptionError()
    if getattr(stripe_sub, "cancel_at_period_end", False) or getattr(
        stripe_sub, "cancel_at", None
    ):
        raise CancellationScheduledError()
    if target.stripe_price_id in _current_price_ids(
        stripe_subscription=stripe_sub
    ):
        raise SamePlanError()

    duration = _target_phase_duration(target=target, client=client)

    existing = _live_schedule(stripe_subscription=stripe_sub, client=client)
    if existing is not None:
        if _pending_phase(schedule=existing) is not None:
            raise ChangeAlreadyScheduledError()
        # A spent shell from an earlier change: harmless, but it blocks a new
        # schedule, so release it and carry on.
        client.v1.subscription_schedules.release(existing.id)

    create_params: SubscriptionScheduleCreateParams = {
        "from_subscription": str(local.stripe_subscription_id)
    }
    try:
        schedule = client.v1.subscription_schedules.create(params=create_params)
    except stripe.InvalidRequestError:
        # Lost a double-submit race: the other request created the schedule.
        refreshed = _stripe_subscription_with_schedule(
            stripe_subscription_id=str(local.stripe_subscription_id), client=client
        )
        if _live_schedule(stripe_subscription=refreshed, client=client) is not None:
            raise ChangeAlreadyScheduledError()
        raise

    try:
        current_phase = schedule.phases[0]
        # Phase 1 is rebuilt from a whitelist (price + quantity, dates) only.
        # Any phase-level discounts, default_tax_rates, metadata or
        # automatic_tax on the original phase would be DROPPED here, and
        # proration_behavior="none" hides the change. Nothing in the checkout
        # flow sets those today; round-trip them here before introducing
        # coupons or tax handling.
        update_params: SubscriptionScheduleUpdateParams = {
            "end_behavior": "release",
            "proration_behavior": "none",
            "phases": [
                {
                    "items": [
                        {
                            "price": _price_id_of(price=item["price"]),
                            "quantity": getattr(item, "quantity", None) or 1,
                        }
                        for item in current_phase["items"]
                    ],
                    "start_date": current_phase.start_date,
                    "end_date": current_phase.end_date,
                },
                {
                    "items": [
                        {"price": str(target.stripe_price_id), "quantity": 1}
                    ],
                    "duration": duration,
                    "billing_cycle_anchor": "phase_start",
                },
            ],
        }
        schedule = client.v1.subscription_schedules.update(
            schedule.id, params=update_params
        )
    except stripe.StripeError:
        # Never leave a bare one-phase schedule behind: it would block
        # cancellation for the rest of the period.
        try:
            client.v1.subscription_schedules.release(schedule.id)
        except stripe.StripeError as cleanup_exc:
            logger.error(
                "could not release half-built schedule %s: %s",
                schedule.id,
                sanitize_sdk_error_message(cleanup_exc),
            )
        raise

    payload = _pending_change_payload(schedule=schedule)
    logger.info(
        "scheduled plan change for user %s to %s at %s",
        user.pk,
        target.slug,
        payload["effective_at"] if payload else None,
    )
    # None only if Stripe echoed the schedule back without a pending phase --
    # not provable here, so it is returned as-is rather than asserted.
    return payload


def _stripe_subscription_with_schedule(
    *, stripe_subscription_id: str, client: stripe.StripeClient
) -> Any:
    return client.v1.subscriptions.retrieve(
        stripe_subscription_id, params={"expand": ["schedule"]}
    )


def _subscription_period_end(*, stripe_subscription: Any) -> int:
    items = list(stripe_subscription["items"].data)
    if not items:
        raise NoActiveSubscriptionError()
    return items[0].current_period_end


def _target_phase_duration(
    *, target: Plan, client: stripe.StripeClient
) -> SubscriptionScheduleUpdateParamsPhaseDuration:
    price = client.v1.prices.retrieve(str(target.stripe_price_id))
    recurring = getattr(price, "recurring", None)
    interval = (
        getattr(recurring, "interval", None) if recurring is not None else None
    )
    if interval != target.billing_interval:
        logger.error(
            "plan %s billing_interval %r disagrees with Stripe price %s recurring interval %r",
            target.slug,
            target.billing_interval,
            target.stripe_price_id,
            interval,
        )
        raise PlanPriceMismatchError(target.slug)
    # Provably non-None here: the equality check above already ruled out
    # interval=None (target.billing_interval is never None), it just isn't
    # expressed in a way ty can narrow on its own.
    assert interval is not None
    return {
        "interval": interval,
        "interval_count": getattr(recurring, "interval_count", None) or 1,
    }
