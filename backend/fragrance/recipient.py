"""
fragrance/recipient.py

Verified changes of the two outbound addresses a user controls separately from
their login email:

- `delivery`: FragranceConfig.recipient_email, where picks and failure notices
  go (read only through tasks._delivery_address).
- `receipt`: the Stripe Customer's email, where Stripe sends receipts,
  invoices and failed-payment notices; mirrored in Subscription.receipt_email.

A change takes effect only when the owner of the new mailbox opens a signed,
single-use, 24 h link. Until then the verified address keeps working and the
requested one sits in a PendingEmailChange row (one per user and purpose).
Nothing here ever reads or writes User.email or a password.

The policy switches an operator may want to flip are the module constants
below; each is read in exactly one place.
"""

import hmac
import logging
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Literal

import stripe
from django.contrib.auth.models import User
from django.core import signing
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from . import billing
from .ai_providers.base import sanitized_exc_info
from .models import FragranceConfig, PendingEmailChange, RecipientChangeAttempt, Subscription
from .services import get_or_create_fragrance_config


logger = logging.getLogger(__name__)


# Starting a change needs the account password. Not asked on resend, cancel or
# confirm. A wrong password still spends the change throttle. Per purpose so
# the operator can relax either independently (spec Amendment B2 recommends
# yes for both; the delivery spec itself does not ask for it).
DELIVERY_CHANGE_REQUIRES_PASSWORD = True
RECEIPT_CHANGE_REQUIRES_PASSWORD = True


# A receipt address whose cache is blank is read from Stripe once, on the
# first view, and cached. Off means "cache only": a blank cache stays unknown
# until a confirmed change or customer creation fills it (no backfill exists).
RECEIPT_EMAIL_LAZY_FILL = True


CONFIRMATION_LINK_MAX_AGE = timedelta(hours=24)


# The nightly purge_expired_pending_email_changes task deletes pending rows
# whose link expired at least this long ago (confirmed and cancelled rows are
# already deleted on the spot).
PENDING_CHANGE_PURGE_AFTER = timedelta(days=7)


RESEND_MIN_GAP = timedelta(seconds=60)


# DB-backed cap behind the (Redis, fail-open) DRF throttles: this many accepted
# change requests, and this many resends, per user and purpose per window.
# Wrong passwords spend it; see consume_attempt.
ATTEMPTS_PER_WINDOW = 5
ATTEMPT_WINDOW = timedelta(hours=1)


# While the cached receipt address is blank, Stripe is asked for it at most
# once per interval (empty, deleted and failed lookups all count).
RECEIPT_EMAIL_RECHECK_INTERVAL = timedelta(minutes=5)


ADDRESS_MAX_LENGTH = 254


TOKEN_MAX_LENGTH = 2048


Purpose = Literal["delivery", "receipt"]


AttemptAction = Literal["change", "resend"]


ConfirmOutcome = Literal[
    "confirmed", "already_confirmed", "invalid", "superseded", "expired", "stripe_unavailable"
]


# One salt per purpose, and the purpose is repeated in the signed payload:
# a delivery token fails to verify under the receipt salt, so it can never be
# used against a receipt change (or the reverse), and the confirm endpoint
# takes the purpose from whichever salt verified, never from the request.
_TOKEN_SALTS: dict[str, str] = {
    "delivery": "recipient-confirm-delivery",
    "receipt": "recipient-confirm-receipt",
}


class RecipientError(Exception):
    code = "recipient_error"


class SameAsCurrentError(RecipientError):
    code = "same_as_current"


class NoPendingChangeError(RecipientError):
    code = "no_pending"


class NoStripeCustomerError(RecipientError):
    code = "no_stripe_customer"


class RateLimitedError(RecipientError):
    code = "rate_limited"

    def __init__(self, *, retry_after: int) -> None:
        super().__init__(f"too many attempts; retry in {retry_after}s")
        self.retry_after = retry_after


class ResendTooSoonError(RecipientError):
    code = "resend_too_soon"

    def __init__(self, *, retry_after: int) -> None:
        super().__init__(f"resend available in {retry_after}s")
        self.retry_after = retry_after


@dataclass(frozen=True)
class TokenPayload:
    user_id: int
    purpose: str
    address: str
    nonce: str
    issued_at: datetime


@dataclass(frozen=True)
class ConfirmResult:
    outcome: ConfirmOutcome
    purpose: str | None = None
    address: str | None = None


def normalized(*, address: str | None) -> str:
    """strip + lower only. Never services.normalize_email: that strips +tags and Gmail dots, i.e. changes the mailbox."""
    return (address or "").strip().lower()


def parse_address(*, raw: object) -> str | None:
    """The stored form of a requested address (trimmed, lower-cased), or None if it is not a plain mailbox."""
    if not isinstance(raw, str):
        return None
    value = normalized(address=raw)
    if not value or len(value) > ADDRESS_MAX_LENGTH:
        return None
    if any(ch.isspace() or ch in '<>,;"()[]\\' or ord(ch) < 32 for ch in value):
        return None
    try:
        validate_email(value)
    except ValidationError:
        return None
    return value


def password_accepted(*, user: User, purpose: Purpose, raw: object) -> bool:
    required = (
        DELIVERY_CHANGE_REQUIRES_PASSWORD if purpose == "delivery" else RECEIPT_CHANGE_REQUIRES_PASSWORD
    )
    if not required:
        return True
    return isinstance(raw, str) and bool(raw) and user.check_password(raw)


def verified_address(*, user: User, purpose: Purpose) -> str:
    """
    The address in force now. Delivery: the config's recipient, falling back to
    the account email exactly as tasks._delivery_address does (existing
    non-blank values are grandfathered as verified). Receipt: the cached
    Stripe Customer email, "" while unknown.
    """
    if purpose == "delivery":
        configured = (
            FragranceConfig.objects.filter(user_id=user.pk)
            .values_list("recipient_email", flat=True)
            .first()
        )
        return str(configured or user.email or "").strip()
    cached = (
        Subscription.objects.filter(user_id=user.pk)
        .values_list("receipt_email", flat=True)
        .first()
    )
    return str(cached or "").strip()


def get_pending(*, user_id: int, purpose: Purpose) -> PendingEmailChange | None:
    return PendingEmailChange.objects.filter(user_id=user_id, purpose=purpose).first()


def pending_fields(*, pending: PendingEmailChange | None, now: datetime | None = None) -> dict[str, Any]:
    """
    The purpose-independent pending block (the caller names the address key).
    `state` is computed here so clients never compare clocks;
    `resend_available_at` is null once the gap has elapsed.
    """
    if pending is None:
        return {"address": None, "state": "none", "expires_at": None, "resend_available_at": None}
    now = now or timezone.now()
    resend_at = pending.last_sent_at + RESEND_MIN_GAP if pending.last_sent_at else None
    return {
        "address": pending.address,
        "state": "expired" if pending.expires_at <= now else "pending",
        "expires_at": pending.expires_at,
        "resend_available_at": resend_at if resend_at and resend_at > now else None,
    }


def delivery_fields(*, user: User, now: datetime | None = None) -> dict[str, Any]:
    block = pending_fields(pending=get_pending(user_id=user.pk, purpose="delivery"), now=now)
    return {
        "pending_recipient_email": block["address"],
        "pending_state": block["state"],
        "pending_expires_at": block["expires_at"],
        "resend_available_at": block["resend_available_at"],
    }


def receipt_fields(
    *, user: User, subscription: Subscription | None, now: datetime | None = None
) -> dict[str, Any]:
    has_customer = bool(subscription and subscription.stripe_customer_id)
    pending = get_pending(user_id=user.pk, purpose="receipt") if has_customer else None
    block = pending_fields(pending=pending, now=now)
    return {
        "has_customer": has_customer,
        "receipt_email": (subscription.receipt_email or None) if subscription and has_customer else None,
        "pending_receipt_email": block["address"],
        "pending_state": block["state"],
        "pending_expires_at": block["expires_at"],
        "resend_available_at": block["resend_available_at"],
    }


def require_stripe_customer(*, subscription: Subscription | None) -> Subscription:
    """
    The single place that refuses a receipt change before first checkout
    (409 no_stripe_customer). Choosing an address earlier is deliberately not
    built: it would need a verified address stored before any Customer exists
    and applied by billing._get_or_create_customer.
    """
    if subscription is None or not subscription.stripe_customer_id:
        raise NoStripeCustomerError()
    return subscription


def ensure_receipt_email_cached(
    *, subscription: Subscription, now: datetime | None = None
) -> str:
    """
    The Customer's email as cached locally, filling a blank cache from Stripe
    at most once per RECEIPT_EMAIL_RECHECK_INTERVAL: the attempt is claimed
    with a conditional UPDATE of receipt_email_checked_at before Stripe is
    called, so a blank/deleted Customer, a failure, or concurrent requests
    cannot make every GET a Stripe round trip. The cache write is conditional
    on it still being blank so it can never clobber a confirmed change landing
    at the same moment. A Stripe failure is logged (ids only) and reported as
    unknown (""), never raised.
    """
    cached = str(subscription.receipt_email or "")
    if cached or not subscription.stripe_customer_id or not RECEIPT_EMAIL_LAZY_FILL:
        return cached
    now = now or timezone.now()
    claimed = (
        Subscription.objects.filter(pk=subscription.pk, receipt_email="")
        .filter(
            Q(receipt_email_checked_at__isnull=True)
            | Q(receipt_email_checked_at__lte=now - RECEIPT_EMAIL_RECHECK_INTERVAL)
        )
        .update(receipt_email_checked_at=now)
    )
    if not claimed:
        return ""
    try:
        fetched = billing.retrieve_customer_email(customer_id=str(subscription.stripe_customer_id))
    except (stripe.StripeError, ImproperlyConfigured) as exc:
        logger.warning(
            "could not read receipt email for subscription %s: %s",
            subscription.pk,
            type(exc).__name__,
            exc_info=sanitized_exc_info(exc=exc, drop_message=True),
        )
        return ""
    if not fetched:
        return ""
    Subscription.objects.filter(pk=subscription.pk, receipt_email="").update(
        receipt_email=fetched
    )
    refreshed = (
        Subscription.objects.filter(pk=subscription.pk)
        .values_list("receipt_email", flat=True)
        .first()
    )
    subscription.receipt_email = str(refreshed or "")
    return str(refreshed or "")


def make_token(*, pending: PendingEmailChange, now: datetime | None = None) -> str:
    now = now or timezone.now()
    payload = {
        "uid": pending.user_id,
        "purpose": pending.purpose,
        "addr": pending.address,
        "nonce": pending.nonce,
        "iat": int(now.timestamp()),
    }
    return signing.dumps(payload, salt=_TOKEN_SALTS[pending.purpose])


def read_token(*, token: object) -> TokenPayload | None:
    """
    Verifies the signature only. Age is deliberately not enforced here
    (no max_age): an old token must still be told apart from a bad one so the
    caller can answer already_confirmed / superseded before expired. The
    issue time is a signed payload field.
    """
    if not isinstance(token, str) or not token or len(token) > TOKEN_MAX_LENGTH:
        return None
    for purpose, salt in _TOKEN_SALTS.items():
        try:
            payload = signing.loads(token, salt=salt)
        except (signing.BadSignature, ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or payload.get("purpose") != purpose:
            return None
        uid, addr, nonce, iat = (payload.get(k) for k in ("uid", "addr", "nonce", "iat"))
        if (
            not isinstance(uid, int)
            or isinstance(uid, bool)
            or not isinstance(addr, str)
            or not isinstance(nonce, str)
            or not nonce
            or not isinstance(iat, int)
            or isinstance(iat, bool)
        ):
            return None
        try:
            issued_at = datetime.fromtimestamp(iat, tz=dt_timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return TokenPayload(user_id=uid, purpose=purpose, address=addr, nonce=nonce, issued_at=issued_at)
    return None


def consume_attempt(
    *, user: User, purpose: Purpose, action: AttemptAction, now: datetime | None = None
) -> None:
    """
    The DB-backed hourly cap: records one attempt or raises RateLimitedError
    with the seconds until the oldest counted attempt leaves the window. Does
    not depend on Redis. The count and the insert share one transaction under
    the User row lock, and it commits before the caller checks a password, so
    a wrong password still spends the budget and concurrent requests cannot
    both take the last slot. A refused request records nothing: retrying
    never extends the lockout. Expired rows are pruned here (this user's under
    the lock, everyone's best-effort).
    """
    now = now or timezone.now()
    cutoff = now - ATTEMPT_WINDOW
    RecipientChangeAttempt.objects.filter(created_at__lte=cutoff).delete()
    with transaction.atomic():
        User.objects.select_for_update().get(pk=user.pk)
        recent = list(
            RecipientChangeAttempt.objects.filter(
                user_id=user.pk, purpose=purpose, action=action, created_at__gt=cutoff
            )
            .order_by("created_at")
            .values_list("created_at", flat=True)
        )
        if len(recent) >= ATTEMPTS_PER_WINDOW:
            frees_at = recent[len(recent) - ATTEMPTS_PER_WINDOW] + ATTEMPT_WINDOW
            raise RateLimitedError(
                retry_after=max(1, math.ceil((frees_at - now).total_seconds()))
            )
        RecipientChangeAttempt.objects.create(
            user_id=user.pk, purpose=purpose, action=action, created_at=now
        )


def start_change(
    *, user: User, purpose: Purpose, address: str, now: datetime | None = None
) -> PendingEmailChange:
    """
    Records a requested change, replacing any pending one for this purpose
    with a fresh nonce (which is what retires the older link). Serialised per
    user on the User row, which always exists: two concurrent first requests
    would otherwise both find no pending row to lock and race to INSERT.
    A request is refused (ResendTooSoonError) while the pending row was
    created, replaced or re-sent less than RESEND_MIN_GAP ago; a row whose
    last queue attempt failed (last_sent_at blank) never blocks.
    `address` must already be parsed (parse_address).
    """
    now = now or timezone.now()
    with transaction.atomic():
        User.objects.select_for_update().get(pk=user.pk)
        if normalized(address=verified_address(user=user, purpose=purpose)) == address:
            raise SameAsCurrentError()
        _refuse_inside_gap(
            pending=PendingEmailChange.objects.select_for_update()
            .filter(user_id=user.pk, purpose=purpose)
            .first(),
            now=now,
        )
        pending, _ = PendingEmailChange.objects.select_for_update().get_or_create(
            user_id=user.pk,
            purpose=purpose,
            defaults={
                "address": address,
                "nonce": secrets.token_urlsafe(24),
                "expires_at": now + CONFIRMATION_LINK_MAX_AGE,
                "last_sent_at": now,
            },
        )
        pending.address = address
        pending.nonce = secrets.token_urlsafe(24)
        pending.expires_at = now + CONFIRMATION_LINK_MAX_AGE
        pending.last_sent_at = now
        pending.confirm_attempts = 0
        pending.save()
    return pending


def _refuse_inside_gap(*, pending: PendingEmailChange | None, now: datetime) -> None:
    if pending is None or pending.last_sent_at is None:
        return
    if now < pending.last_sent_at + RESEND_MIN_GAP:
        wait = (pending.last_sent_at + RESEND_MIN_GAP - now).total_seconds()
        raise ResendTooSoonError(retry_after=max(1, math.ceil(wait)))


def resend(*, user: User, purpose: Purpose, now: datetime | None = None) -> PendingEmailChange:
    """
    Same nonce and address, fresh 24 h expiry; the caller mints a fresh
    signature by queueing the mail. The hourly cap is spent only once the
    request passes the gap, so a double click does not burn it.
    """
    now = now or timezone.now()
    with transaction.atomic():
        User.objects.select_for_update().get(pk=user.pk)
        pending = (
            PendingEmailChange.objects.select_for_update()
            .filter(user_id=user.pk, purpose=purpose)
            .first()
        )
        if pending is None:
            raise NoPendingChangeError()
        _refuse_inside_gap(pending=pending, now=now)
        consume_attempt(user=user, purpose=purpose, action="resend", now=now)
        pending.expires_at = now + CONFIRMATION_LINK_MAX_AGE
        pending.last_sent_at = now
        pending.save(update_fields=["expires_at", "last_sent_at"])
    return pending


def cancel(*, user: User, purpose: Purpose) -> None:
    """Idempotent. Waits for a confirm in flight on the same row, then finds nothing."""
    PendingEmailChange.objects.filter(user_id=user.pk, purpose=purpose).delete()


def clear_pending(*, user_id: int, purpose: Purpose) -> None:
    PendingEmailChange.objects.filter(user_id=user_id, purpose=purpose).delete()


def queue_confirmation_email(*, pending: PendingEmailChange) -> bool:
    """
    Queues the link email after the pending row is committed (so the task
    always finds the nonce it was queued with). False when the broker refused
    it; the resend gap is then lifted, since nothing was actually sent.
    """
    from .tasks import send_recipient_confirmation_email

    try:
        send_recipient_confirmation_email.delay(
            user_id=pending.user_id, purpose=pending.purpose, nonce=pending.nonce
        )
    except Exception as exc:
        logger.error(
            "could not enqueue %s confirmation email for user %s; broker unavailable",
            pending.purpose,
            pending.user_id,
            exc_info=sanitized_exc_info(exc=exc, drop_message=True),
        )
        PendingEmailChange.objects.filter(pk=pending.pk, nonce=pending.nonce).update(
            last_sent_at=None
        )
        return False
    return True


def confirm_change(*, token: object, now: datetime | None = None) -> ConfirmResult:
    """
    Consumes a confirmation link. Check order is part of the contract:
    invalid -> already_confirmed (any token age) -> superseded -> expired.
    The checks run under a lock on the pending row, so a double click or a
    cancel racing the confirm resolves one way: a loser finds the row gone and
    the address already in force (already_confirmed) or the change cancelled
    (superseded). Never touches User.email or a password. An inactive account
    is answered "invalid", like an unknown one.

    Delivery is one short transaction. Receipt is three phases so no row lock
    or transaction spans the Stripe call:
      1. lock the row, run the checks, copy nonce/attempt/customer id;
      2. customers.update outside any transaction, under the key
         receipt-email-<nonce>-<attempt>;
      3. a short transaction: delete the row if the nonce still matches and
         write the cache. The cache is written even when the row was replaced
         or cancelled meanwhile, because Stripe has already changed.
    A Stripe failure keeps the row and nonce (the link stays valid) and bumps
    the attempt counter, because Stripe replays a failed first attempt for a
    reused idempotency key.
    """
    now = now or timezone.now()
    payload = read_token(token=token)
    if payload is None:
        return ConfirmResult("invalid")
    user = User.objects.filter(pk=payload.user_id, is_active=True).first()
    if user is None:
        return ConfirmResult("invalid")
    purpose: Purpose = "receipt" if payload.purpose == "receipt" else "delivery"
    address = normalized(address=payload.address)
    if purpose == "delivery":
        return _confirm_delivery(user=user, payload=payload, address=address, now=now)
    return _confirm_receipt(user=user, payload=payload, address=address, now=now)


def _check_pending(
    *, user: User, purpose: Purpose, payload: TokenPayload, address: str, now: datetime
) -> tuple[PendingEmailChange | None, ConfirmResult | None]:
    """The shared decision under the row lock: either a live pending row, or the final answer."""
    pending = (
        PendingEmailChange.objects.select_for_update()
        .filter(user_id=user.pk, purpose=purpose)
        .first()
    )
    current = normalized(address=verified_address(user=user, purpose=purpose))
    if current and current == address:
        return None, ConfirmResult("already_confirmed", purpose, address)
    if (
        pending is None
        or not hmac.compare_digest(pending.nonce.encode(), payload.nonce.encode())
        or normalized(address=pending.address) != address
    ):
        return None, ConfirmResult("superseded", purpose)
    if now - payload.issued_at > CONFIRMATION_LINK_MAX_AGE or now >= pending.expires_at:
        return None, ConfirmResult("expired", purpose)
    return pending, None


def _confirm_delivery(
    *, user: User, payload: TokenPayload, address: str, now: datetime
) -> ConfirmResult:
    with transaction.atomic():
        pending, answer = _check_pending(
            user=user, purpose="delivery", payload=payload, address=address, now=now
        )
        if answer is not None or pending is None:
            return answer or ConfirmResult("superseded", "delivery")
        config = (
            FragranceConfig.objects.select_for_update().filter(user_id=user.pk).first()
        ) or get_or_create_fragrance_config(user=user)
        previous = str(config.recipient_email or user.email or "").strip()
        config.recipient_email = address
        config.save(update_fields=["recipient_email"])
        pending.delete()
        previous_copy = _notice_recipients(candidates=[previous], new_address=address)
        # The login mailbox also hears of a delivery change when the previous
        # delivery address was somewhere else.
        account_copy = _notice_recipients(
            candidates=[str(user.email or "")], new_address=address, exclude=[previous]
        )
    for recipient_address in previous_copy:
        _queue_previous_address_notice(
            user_id=user.pk,
            purpose="delivery",
            recipient=recipient_address,
            new_address=address,
        )
    for recipient_address in account_copy:
        _queue_previous_address_notice(
            user_id=user.pk,
            purpose="delivery",
            recipient=recipient_address,
            new_address=address,
            account_copy=True,
        )
    return ConfirmResult("confirmed", "delivery", address)


def _confirm_receipt(
    *, user: User, payload: TokenPayload, address: str, now: datetime
) -> ConfirmResult:
    with transaction.atomic():
        pending, answer = _check_pending(
            user=user, purpose="receipt", payload=payload, address=address, now=now
        )
        if answer is not None or pending is None:
            return answer or ConfirmResult("superseded", "receipt")
        subscription = Subscription.objects.filter(user_id=user.pk).first()
        customer_id = str(subscription.stripe_customer_id or "") if subscription else ""
        if subscription is None or not customer_id:
            pending.delete()
            return ConfirmResult("superseded", "receipt")
        nonce, attempt, subscription_pk = pending.nonce, pending.confirm_attempts, subscription.pk

    try:
        billing.update_customer_receipt_email(
            customer_id=customer_id, email=address, nonce=nonce, attempt=attempt
        )
    except stripe.InvalidRequestError as exc:
        if exc.http_status == 404 and exc.code == "resource_missing":
            return _classify_missing_customer(
                user=user, customer_id=customer_id, nonce=nonce, exc=exc
            )
        if exc.http_status in (400, 404):
            # Stripe will never accept this: the link is dead, not retryable.
            logger.error(
                "stripe rejected receipt email change for user %s (%s, http %s, code %s); pending change cleared",
                user.pk,
                type(exc).__name__,
                exc.http_status,
                exc.code,
            )
            PendingEmailChange.objects.filter(
                user_id=user.pk, purpose="receipt", nonce=nonce
            ).delete()
            return ConfirmResult("superseded", "receipt")
        _bump_attempt(user_id=user.pk, nonce=nonce)
        return _stripe_unavailable(user_id=user.pk, exc=exc)
    except (stripe.StripeError, ImproperlyConfigured) as exc:
        # Includes 401/403/409/429 and connection failures: a fault on our
        # side or a transient one, so the link must stay usable.
        _bump_attempt(user_id=user.pk, nonce=nonce)
        return _stripe_unavailable(user_id=user.pk, exc=exc)

    with transaction.atomic():
        PendingEmailChange.objects.filter(user_id=user.pk, purpose="receipt", nonce=nonce).delete()
        previous = str(
            Subscription.objects.filter(pk=subscription_pk)
            .values_list("receipt_email", flat=True)
            .first()
            or ""
        ).strip()
        changed = (
            Subscription.objects.filter(pk=subscription_pk)
            .exclude(receipt_email=address)
            .update(receipt_email=address)
        )
    if not changed:
        return ConfirmResult("already_confirmed", "receipt", address)
    for recipient_address in _notice_recipients(candidates=[previous], new_address=address):
        _queue_previous_address_notice(
            user_id=user.pk,
            purpose="receipt",
            recipient=recipient_address,
            new_address=address,
        )
    return ConfirmResult("confirmed", "receipt", address)


def _notice_recipients(
    *, candidates: list[str], new_address: str, exclude: list[str] | None = None
) -> list[str]:
    """Distinct (case-insensitive) non-blank addresses other than the new one and `exclude`, in order."""
    seen = {new_address, *(normalized(address=value) for value in exclude or [])}
    result: list[str] = []
    for candidate in candidates:
        value = candidate.strip()
        key = normalized(address=value)
        if not value or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _bump_attempt(*, user_id: int, nonce: str) -> None:
    PendingEmailChange.objects.filter(user_id=user_id, purpose="receipt", nonce=nonce).update(
        confirm_attempts=F("confirm_attempts") + 1
    )


def _classify_missing_customer(
    *, user: User, customer_id: str, nonce: str, exc: stripe.InvalidRequestError
) -> ConfirmResult:
    """
    customers.update answered 404 resource_missing. Stripe retrieves a deleted
    Customer as 200 `deleted`, so a Customer the account does not know at all
    means the configured key belongs to another account or mode (CRITICAL, row
    kept: clearing it would silently void every user's pending change). A
    Customer the account does know is gone for good: the link is dead.
    """
    try:
        known = billing.customer_known_to_stripe(customer_id=customer_id)
    except (stripe.StripeError, ImproperlyConfigured) as probe_exc:
        _bump_attempt(user_id=user.pk, nonce=nonce)
        return _stripe_unavailable(user_id=user.pk, exc=probe_exc)
    if not known:
        billing.report_key_mode_anomaly(
            user_id=user.pk, resource="customer", stripe_id=customer_id
        )
        _bump_attempt(user_id=user.pk, nonce=nonce)
        return ConfirmResult("stripe_unavailable", "receipt")
    logger.error(
        "stripe no longer has the customer for user %s (%s, http %s, code %s); pending change cleared",
        user.pk,
        type(exc).__name__,
        exc.http_status,
        exc.code,
    )
    PendingEmailChange.objects.filter(user_id=user.pk, purpose="receipt", nonce=nonce).delete()
    return ConfirmResult("superseded", "receipt")


def _stripe_unavailable(*, user_id: int, exc: Exception) -> ConfirmResult:
    logger.error(
        "stripe receipt email update failed for user %s (%s, http %s); pending change kept",
        user_id,
        type(exc).__name__,
        getattr(exc, "http_status", None),
        exc_info=sanitized_exc_info(exc=exc, drop_message=True),
    )
    return ConfirmResult("stripe_unavailable", "receipt")


def _queue_previous_address_notice(
    *, user_id: int, purpose: str, recipient: str, new_address: str, account_copy: bool = False
) -> None:
    from .tasks import send_recipient_changed_notice

    extra: dict[str, bool] = {"account_copy": True} if account_copy else {}
    try:
        send_recipient_changed_notice.delay(
            user_id=user_id, purpose=purpose, recipient=recipient, new_address=new_address, **extra
        )
    except Exception as exc:
        logger.error(
            "could not enqueue %s changed notice for user %s; broker unavailable",
            purpose,
            user_id,
            exc_info=sanitized_exc_info(exc=exc, drop_message=True),
        )
