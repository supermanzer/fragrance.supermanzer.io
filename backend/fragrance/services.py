import csv
import hashlib
import hmac
import logging
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from typing import IO, Literal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth.models import User
from django.core import signing
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify
from django_celery_beat.models import crontab_schedule_celery_timezone
from rest_framework.request import Request

from .models import (
    FRAGRANCE_STATUSES,
    BetaSignup,
    Fragrance,
    FragranceConfig,
    Plan,
    Recommendation,
    RecommendationRun,
    Subscription,
)
from .permissions import SubscriptionRequired


logger = logging.getLogger(__name__)


# The beat schedule is calendar-monthly (day_of_month), but quota_period_days
# is a rolling window measured in exact days. A 28- or 30-day month, or
# sub-second scheduling jitter around a 30-day boundary, can put the
# previous scheduled run inside the new rolling window even though a full
# calendar month has genuinely elapsed. Only the beat path gets this slack;
# a manual API trigger keeps the strict rolling quota_period_days so a user
# cannot use the "Run Now" button to get extra runs.
BEAT_QUOTA_TOLERANCE = timedelta(days=4)


BETA_INVITE_MAX_AGE = 60 * 60 * 24 * 14  # 14 days


BETA_INVITE_SALT = "beta-invite"


DEFAULT_FAILED_RUN_GRACE = 1


DEFAULT_MAX_RUNS_PER_QUOTA_PERIOD = 1


# Terms used when a subscription has no Plan (staff, comped/plan-less rows).
DEFAULT_QUOTA_PERIOD_DAYS = 30


EMAIL_REVERT_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


EMAIL_REVERT_MAX_TOKEN_LENGTH = 2048


EMAIL_REVERT_SALT = "email-revert"


# A pending/running row older than this is presumed stranded (worker OOM,
# SIGKILL, container recreate) and no longer blocks a new trigger; the
# reap_stale_runs task marks it failed. Comfortably above the task's worst
# case: 3 attempts x 1500s hard limit + 2 x 300s retry backoff.
IN_FLIGHT_MAX_AGE = timedelta(hours=2)


# Days 29-31 do not exist in every month, so a crontab on one of them would
# silently skip short months. Shared by the serializer, the beat schedule
# builder (signals.py) and the next-run computation below.
MAX_RUN_DAY_OF_MONTH = 28


RECOMMENDATION_FEEDBACK_MAX_AGE = 60 * 60 * 24 * 90  # 90 days


RECOMMENDATION_FEEDBACK_SALT = "recommendation-feedback"


REQUIRED_COLUMNS = {"fragrance", "status", "house", "notes"}


# Maps normalized CSV values → internal status. Keys are lowercased + stripped.
STATUS_ALIASES: dict[str, str] = {
    "own": "own",
    "like": "like",
    "dislike": "dislike",
    "don't like": "dislike",
    "dont like": "dislike",
    "hate": "dislike",
}


TriggerSource = Literal["api", "beat"]


# Domains where the mailbox provider itself ignores dots in the local part,
# so "r.yan@gmail.com" and "ryan@gmail.com" are the same inbox. Only Gmail's
# own domains are known to do this widely enough to normalize on safely --
# stripping dots elsewhere would risk merging two genuinely distinct addresses.
_DOT_INSENSITIVE_DOMAINS = {"gmail.com", "googlemail.com"}


_IN_FLIGHT_STATUSES = ("pending", "running")


_MAX_USERNAME_LENGTH = 150


# A failed run does not burn the allowance; everything else that was
# dispatched does.
_QUOTA_CONSUMING_STATUSES = ("pending", "running", "done")


class BetaInviteTokenError(Exception):
    """Base for resolve_beta_invite_signup rejections."""


class BetaInviteTokenAlreadyUsedError(BetaInviteTokenError):
    """
    Signup status is already 'converted'. The only rejection reason this
    project's BetaAcceptView tells the caller apart from a generic invalid
    link: a signed invite token is not something a third party can guess or
    type in the way an email address can, so holding one already proves
    receipt of the one email it was sent in, and "already used" leaks
    nothing that holder didn't already have. Maps to 409
    {"code": "already_used"}.
    """


class BetaInviteTokenInvalidError(BetaInviteTokenError):
    """
    Token missing/tampered/expired, unknown signup, wrong status (anything
    other than 'approved' or 'converted'), or invited_user missing/inactive/
    mismatched. Maps to 400 {"code": "invalid_or_expired"}.
    """


class QuotaExceeded(Exception):
    def __init__(self, *, next_eligible_at: datetime | None) -> None:
        super().__init__("Run quota exhausted for this period.")
        self.next_eligible_at = next_eligible_at


class RatingConflict(Exception):
    """Raised when a recommendation already has a different rating recorded than the one requested."""

    def __init__(self, existing_action: str) -> None:
        self.existing_action = existing_action
        super().__init__(f"Recommendation already rated as {existing_action}.")


class RunInProgress(Exception):
    pass


@transaction.atomic
def apply_recommendation_rating(recommendation: Recommendation, action: str) -> tuple[Fragrance, bool]:
    """
    Records a rating for a confirmed recommendation pick as a Fragrance collection record.

    Resolution order:

    1. FK check first: look up any Fragrance row already linked to *this*
       recommendation via source_recommendation. This is the authoritative,
       rename-proof answer to "has this recommendation already been rated" —
       unlike a (user, name) lookup, it can't be defeated by the user renaming
       the linked Fragrance row after the fact. If found: idempotent resubmission
       if the action matches, RatingConflict otherwise.
    2. Only if nothing is linked yet, fall back to the (user, name) lookup, rather
       than via get_or_create or the recommendation's own promoted_fragrance
       reverse accessor, because the row that needs protecting is keyed on
       (user, name) and a different recommendation (same name, different run)
       can already own it. Branches on that row's source_recommendation:

       - No existing row: create one, linked to this recommendation.
       - Existing row with no source_recommendation (manually added or
         CSV-imported, never linked to a recommendation): claim it for this
         recommendation.
       - Existing row already linked to *this* recommendation (only reachable
         under the lock: a second racer can observe the row become linked
         between the FK-first check above and acquiring select_for_update,
         since that first check runs outside the lock): idempotent
         resubmission if the action matches, RatingConflict otherwise.
       - Existing row linked to a *different* recommendation: always
         RatingConflict, even if the action matches — this recommendation never
         overwrites another recommendation's link, since doing so silently
         un-rates the other one.

       This branch runs inside a nested transaction.atomic() savepoint with
       select_for_update() on the (user, name) read, and catches IntegrityError
       on the create/claim writes. Two races are covered here, and both matter:
       select_for_update() serializes two concurrent requests that both find the
       same *unlinked* row (source_recommendation_id is None) so the second
       re-reads post-commit and correctly sees the row claimed. It does nothing
       for two concurrent requests that both find *no* row at all — that race is
       closed by the IntegrityError catch, which re-resolves from committed state
       via _resolve_after_race.

    Returns (fragrance, is_new_rating). is_new_rating is False only for the idempotent
    resubmission case (same recommendation, same action already recorded).
    Raises RatingConflict if a different action was already recorded for this
    recommendation, or if the (user, name) row is already claimed by a different
    recommendation — first rating for a given (user, name, recommendation) wins.
    """
    linked = Fragrance.objects.filter(source_recommendation_id=recommendation.id).first()
    if linked is not None:
        if linked.status == action:
            return linked, False
        raise RatingConflict(existing_action=linked.status)

    try:
        with transaction.atomic():
            existing = Fragrance.objects.select_for_update().filter(
                user=recommendation.user, name=recommendation.name
            ).first()

            if existing is None:
                fragrance = Fragrance.objects.create(
                    user=recommendation.user,
                    name=recommendation.name,
                    house=recommendation.house,
                    status=action,
                    source_recommendation=recommendation,
                )
                return fragrance, True

            if existing.source_recommendation_id is None:
                existing.house = recommendation.house
                existing.status = action
                existing.source_recommendation = recommendation
                existing.save(update_fields=["house", "status", "source_recommendation", "updated_at"])
                return existing, True

            if existing.source_recommendation_id == recommendation.id:
                if existing.status == action:
                    return existing, False
                raise RatingConflict(existing_action=existing.status)

            raise RatingConflict(existing_action=existing.status)
    except IntegrityError as exc:
        return _resolve_after_race(recommendation=recommendation, action=action, original=exc)


def default_schedule_for(*, user_id: int) -> tuple[int, int]:
    """
    Stable, evenly-spread (day_of_month, hour) derived from the user id: 672
    slots. Day is 1-28 only -- 29-31 would silently skip short months.
    Deterministic, so a user always lands in the same slot.
    """
    return (user_id % 28) + 1, (user_id // 28) % 24


def derive_unique_username(*, email: str) -> str:
    """
    Derives a unique Django username from an email's local part.

    Two different addresses can reduce to the same local part (ryan@gmail.com
    and ryan@example.com both slugify to "ryan"), so this always checks for an
    existing row and appends a numeric suffix on collision. The base is
    truncated up front to leave room for that suffix, rather than truncating
    the final candidate -- truncating after appending could silently reproduce
    a collision by chopping the suffix back off.
    """
    local = email.split("@", 1)[0]
    base = slugify(local).replace("-", "_") or "user"
    base = base[: _MAX_USERNAME_LENGTH - 5]

    candidate = base
    suffix = 1
    while User.objects.filter(username=candidate).exists():
        suffix += 1
        candidate = f"{base}_{suffix}"[:_MAX_USERNAME_LENGTH]
    return candidate


def generate_beta_invite_token(*, signup: BetaSignup) -> str:
    """
    Signs a payload identifying which BetaSignup/User an invite-accept link
    represents. Deliberately not Django's PasswordResetTokenGenerator --
    PASSWORD_RESET_TIMEOUT is 1 hour, and raising it globally to suit a
    14-day invite window would weaken actual password resets.
    """
    payload = {"sid": signup.id, "uid": signup.invited_user_id}
    return signing.dumps(payload, salt=BETA_INVITE_SALT)


def generate_email_revert_token(*, user_id: int, old_email: str, new_email: str) -> str:
    """
    Signs the "this wasn't me" link put in the old-address notice. The signed
    addresses are what make the token single-use without a table: it is only
    honoured while User.email still equals `new`, see billing.revert_customer_email.
    """
    return signing.dumps({"uid": user_id, "old": old_email, "new": new_email}, salt=EMAIL_REVERT_SALT)


def generate_recommendation_token(recommendation: Recommendation, action: str) -> str:
    """Signs a payload identifying which recommendation/action a feedback link represents."""
    payload = {"rid": recommendation.id, "action": action, "uid": recommendation.user_id}
    return signing.dumps(payload, salt=RECOMMENDATION_FEEDBACK_SALT)


def get_client_ip(*, request: Request) -> str:
    """
    Best-available client IP for abuse correlation, not an authorization
    control.

    Typed as rest_framework.request.Request, not django.http.HttpRequest,
    because both call sites (BetaSignupView.post, BetaSignupThrottle.get_ident)
    receive the DRF-wrapped request -- it only worked against the Django type
    via Request's passthrough .META property, which duck-typing papered over
    but didn't document.

    In production nginx is the only path to `backend` (no published port),
    and its realip module rewrites `$remote_addr` from Cloudflare's trusted
    `CF-Connecting-IP` before forwarding `X-Real-IP: $remote_addr` upstream --
    so that header carries the true client IP there. In dev,
    docker-compose.override publishes `backend` directly, bypassing nginx
    entirely: there is no trusted proxy in front, so trusting a
    client-supplied X-Real-IP there would be spoofable, and REMOTE_ADDR (the
    actual TCP peer) is the honest value. Gated on DEBUG, matching the same
    proxy-trust pattern as config.settings._secure_proxy_ssl_header.
    """
    if not settings.DEBUG:
        real_ip = request.META.get("HTTP_X_REAL_IP")
        if real_ip:
            return real_ip
    return request.META.get("REMOTE_ADDR", "")


def get_or_create_fragrance_config(*, user: User) -> FragranceConfig:
    """
    The one place a FragranceConfig is first created, with a jittered schedule
    and the account email as the delivery address. Applied only at creation
    via `defaults` -- get_or_create never overwrites an existing row, so a
    user's own choice is never touched.
    """
    day, hour = default_schedule_for(user_id=user.pk)
    config, _ = FragranceConfig.objects.get_or_create(
        user=user,
        defaults={
            "run_day_of_month": day,
            "run_hour": hour,
            "recipient_email": user.email,
        },
    )
    return config


def has_completed_run(*, user: User) -> bool:
    """True once the user has had a successful ("done") run; failed runs never count."""
    return RecommendationRun.objects.filter(user=user, status="done").exists()


def hash_signup_ip(*, ip: str) -> str:
    """HMACs a client IP with SECRET_KEY so BetaSignup never stores the raw address."""
    return hmac.new(
        key=settings.SECRET_KEY.encode(), msg=ip.encode(), digestmod=hashlib.sha256
    ).hexdigest()


@transaction.atomic
def import_collection_from_csv(user: User, file_obj: IO[str]) -> dict:
    """
    Parse a CSV and upsert Fragrance records for the given user.

    Accepts any text-mode file-like object (open(), InMemoryUploadedFile wrapped
    in a UTF-8 codec reader, StringIO, etc.).

    Returns {'created': int, 'updated': int, 'skipped': int}.
    Raises ValueError for structural problems (missing columns, empty file).
    Row-level problems (empty name, invalid status) increment skipped silently.
    """
    reader = csv.DictReader(file_obj)

    if not reader.fieldnames:
        raise ValueError("CSV file is empty or has no header row.")

    lowered_fieldnames = {f.lower() for f in reader.fieldnames}
    missing = REQUIRED_COLUMNS - lowered_fieldnames
    if missing:
        raise ValueError(
            f"Missing required columns: {', '.join(sorted(missing))}. "
            f"Found: {', '.join(reader.fieldnames)}"
        )

    created = updated = skipped = 0

    for row in reader:
        row = {k.lower(): v for k, v in row.items()}
        name = row["fragrance"].strip().removeprefix("'")
        status = STATUS_ALIASES.get(row["status"].strip().lower())
        house = row.get("house", "").strip().removeprefix("'")
        notes = row.get("notes", "").strip().removeprefix("'")

        if not name or status is None:
            skipped += 1
            continue

        _, was_created = Fragrance.objects.update_or_create(
            user=user,
            name=name,
            defaults={"house": house, "status": status, "notes": notes},
        )
        if was_created:
            created += 1
        else:
            updated += 1

    if created == 0 and updated == 0 and skipped == 0:
        raise ValueError("CSV contains no data rows.")

    return {"created": created, "updated": updated, "skipped": skipped}


def in_flight_runs(*, user: User, now: datetime):
    """Sole definition of "a run is in flight", shared by every guard so they cannot drift."""
    return RecommendationRun.objects.filter(
        user=user,
        status__in=_IN_FLIGHT_STATUSES,
        triggered_at__gte=now - IN_FLIGHT_MAX_AGE,
    )


def normalize_email(*, email: str) -> str:
    """
    Lowercase, strip +tags, and strip dots for Gmail-class domains.

    A `unique` constraint on the raw email does nothing against
    `ryan+1@gmail.com`, `ryan+2@gmail.com`, or `r.y.a.n@gmail.com` -- all of
    which land in the same inbox as `ryan@gmail.com`. `BetaSignup`'s unique
    constraint is keyed on this function's output, not the raw address, so
    this is the anti-abuse primitive that actually stops one person from
    occupying many waitlist slots.
    """
    email = email.strip().lower()
    local, sep, domain = email.partition("@")
    if not sep:
        return email
    local = local.split("+", 1)[0]
    if domain in _DOT_INSENSITIVE_DOMAINS:
        local = local.replace(".", "")
    return f"{local}@{domain}"


def quota_snapshot(*, user: User, subscription: Subscription | None) -> dict:
    """
    Frontend-facing quota state. Read-only: no writes, no locks.

    Always evaluated with source="api" (the default), deliberately: this
    backs the "Run Now" button and the "next eligible" display, both of
    which describe what a *manual* trigger would do right now. Reporting
    the beat-tolerant window here would show can_trigger=True for a user
    whose actual manual trigger would still be refused by the strict
    rolling window in start_recommendation_run.
    """
    now = timezone.now()
    staff = _is_staff(user=user)
    plan = subscription.plan if subscription else None
    _, max_runs, _ = _quota_terms(plan=plan)
    has_access = staff or bool(subscription and subscription.has_active_access)
    consumed, blocked, quota_next = _evaluate_quota(user=user, plan=plan, now=now)
    refusal, next_eligible_at = _check_can_start(
        user=user, subscription=subscription, now=now, quota=(consumed, blocked, quota_next)
    )
    if isinstance(refusal, RunInProgress):
        # A run in flight hides the quota verdict; still report when a quota block lifts.
        next_eligible_at = None if (staff or not blocked) else quota_next
    return {
        # None means unlimited (exempt staff); 0 means lapsed.
        "runs_allowed": None if staff else (max_runs if has_access else 0),
        "runs_used": consumed,
        "next_eligible_at": next_eligible_at,
        "can_trigger": refusal is None,
        # The one manual run a non-staff user gets: refusal already covers
        # access, an in-flight run and quota. Decided on is_staff alone (a
        # non-staff superuser follows the non-staff rule).
        "first_run_available": (
            refusal is None
            and not user.is_staff
            and not has_completed_run(user=user)
        ),
        "next_run_at": _next_run_at(
            user=user, plan=plan, has_access=has_access, exempt=staff, now=now
        ),
    }


def resolve_beta_invite_signup(*, token: str) -> BetaSignup:
    """
    Decodes a beta-invite token and resolves it to a still-invitable
    BetaSignup. Shared by BetaAcceptView's GET pre-check and its POST
    serializer so both paths agree on exactly what counts as invalid vs.
    already-used.
    """
    payload = verify_beta_invite_token(token=token)
    if payload is None:
        raise BetaInviteTokenInvalidError()
    try:
        signup = BetaSignup.objects.select_related('invited_user').get(pk=payload['sid'])
    except BetaSignup.DoesNotExist:
        raise BetaInviteTokenInvalidError()

    if signup.status == 'converted':
        raise BetaInviteTokenAlreadyUsedError()

    user = signup.invited_user
    if (
        signup.status != 'approved'
        or user is None
        or user.id != payload['uid']
        or not user.is_active
    ):
        raise BetaInviteTokenInvalidError()

    return signup


def start_recommendation_run(*, user: User, source: TriggerSource) -> RecommendationRun:
    """
    The single chokepoint both trigger paths (the API view and the beat task)
    go through. Serialised per user by a row lock on their Subscription, which
    get_or_create guarantees exists first -- select_for_update on a missing
    row would lock nothing. The OneToOne unique constraint makes concurrent
    creation safe. Raises SubscriptionRequired, RunInProgress or QuotaExceeded;
    otherwise returns a new 'pending' run created inside the lock.

    source distinguishes the beat-tolerant quota window from the strict one
    (see _evaluate_quota) -- it is not just a log label.
    """
    with transaction.atomic():
        subscription, _ = Subscription.objects.get_or_create(user=user)
        subscription = Subscription.objects.select_for_update().get(pk=subscription.pk)
        refusal, _ = _check_can_start(
            user=user, subscription=subscription, now=timezone.now(), source=source
        )
        if refusal is not None:
            raise refusal
        run = RecommendationRun.objects.create(user=user, status="pending")
    logger.info("started recommendation run %s for user %s (source=%s)", run.id, user.id, source)
    return run


def verify_beta_invite_token(*, token: str) -> dict | None:
    """
    Decodes and verifies a beta-invite token. Returns the payload dict on
    success, or None if the token is missing, tampered, expired, or
    malformed -- callers should treat None as a generic "invalid or expired
    link" so a bad token carries no signal about whether a matching signup
    exists, matching verify_recommendation_token's rule.
    """
    if not token:
        return None
    try:
        payload = signing.loads(token, salt=BETA_INVITE_SALT, max_age=BETA_INVITE_MAX_AGE)
    except (signing.BadSignature, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if {"sid", "uid"} - payload.keys():
        return None
    return payload


def verify_email_revert_token(*, token: object) -> dict | None:
    """
    Returns {"uid": int, "old": str, "new": str} or None for a missing,
    non-string, oversized, tampered, expired or malformed token. Callers must
    treat None as one undifferentiated "invalid or expired" outcome.
    """
    if not isinstance(token, str) or not token or len(token) > EMAIL_REVERT_MAX_TOKEN_LENGTH:
        return None
    try:
        payload = signing.loads(token, salt=EMAIL_REVERT_SALT, max_age=EMAIL_REVERT_MAX_AGE)
    except (signing.BadSignature, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if (
        not isinstance(payload.get("uid"), int)
        or isinstance(payload.get("uid"), bool)
        or not isinstance(payload.get("old"), str)
        or not isinstance(payload.get("new"), str)
    ):
        return None
    return payload


def verify_recommendation_token(token: str) -> dict | None:
    """
    Decodes and verifies a recommendation feedback token.

    Returns the payload dict on success, or None if the token is missing, tampered,
    expired, or malformed in any way. Callers should treat None as a generic "not found"
    so an invalid link carries no signal about whether a matching recommendation exists.
    """
    if not token:
        return None
    try:
        payload = signing.loads(
            token, salt=RECOMMENDATION_FEEDBACK_SALT, max_age=RECOMMENDATION_FEEDBACK_MAX_AGE
        )
    except (signing.BadSignature, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if {"rid", "action", "uid"} - payload.keys():
        return None
    if payload["action"] not in {value for value, _ in FRAGRANCE_STATUSES}:
        return None
    return payload


def _beat_timezone() -> ZoneInfo:
    """The timezone the per-user crontabs are evaluated in (django-celery-beat's default for new schedules)."""
    return ZoneInfo(crontab_schedule_celery_timezone())


def _check_can_start(
    *,
    user: User,
    subscription: Subscription | None,
    now: datetime,
    quota: tuple[int, bool, datetime | None] | None = None,
    source: TriggerSource = "api",
) -> tuple[Exception | None, datetime | None]:
    """
    The single rule set behind both start_recommendation_run (which calls it
    under the row lock) and quota_snapshot (read-only, no lock), so the two
    cannot drift. Returns (refusal or None, next_eligible_at). Creates nothing.

    source only matters when the caller doesn't already have a precomputed
    quota tuple -- it picks the strict or beat-tolerant window in
    _evaluate_quota. If a caller passes `quota` in, that tuple's source
    already won and this parameter is ignored for the quota check.
    """
    staff = _is_staff(user=user)
    if not (staff or (subscription is not None and subscription.has_active_access)):
        return SubscriptionRequired(), None
    if in_flight_runs(user=user, now=now).exists():
        return RunInProgress(), None
    if staff:
        return None, None
    if quota is None:
        quota = _evaluate_quota(user=user, plan=subscription.plan, now=now, source=source)
    _, blocked, next_eligible_at = quota
    if blocked:
        return QuotaExceeded(next_eligible_at=next_eligible_at), next_eligible_at
    return None, None


def _evaluate_quota(
    *, user: User, plan: Plan | None, now: datetime, source: TriggerSource = "api"
) -> tuple[int, bool, datetime | None]:
    """
    Returns (consumed, blocked, next_eligible_at) for the rolling window.

    consumed = pending/running/done runs in the window; blocked once that
    reaches max_runs, or once *all* dispatched runs (failures included) reach
    max_runs + failed_run_grace -- so one failure earns one retry and two
    stop it. next_eligible_at is when enough of the oldest relevant runs
    have left the window to drop back under the limit; None if not blocked
    (or if a zero allowance means it never will be).

    source="beat" shrinks the window by BEAT_QUOTA_TOLERANCE so a calendar-
    monthly scheduled run is never refused just because the rolling window
    hasn't quite finished elapsing (see BEAT_QUOTA_TOLERANCE). Manual/API
    triggers always use the full, strict quota_period_days.
    """
    period, max_runs, grace = _quota_terms(plan=plan)
    if source == "beat":
        period = period - BEAT_QUOTA_TOLERANCE
    windowed = list(
        RecommendationRun.objects.filter(
            user=user, triggered_at__gte=now - period
        )
        .order_by("triggered_at")
        .values_list("triggered_at", "status")
    )
    counted = [at for at, run_status in windowed if run_status in _QUOTA_CONSUMING_STATUSES]
    consumed = len(counted)

    frees_at: list[datetime] = []
    blocked = False
    if consumed >= max_runs:
        blocked = True
        if consumed - max_runs < consumed:
            frees_at.append(counted[consumed - max_runs] + period)
    dispatch_limit = max_runs + grace
    if len(windowed) >= dispatch_limit:
        blocked = True
        if len(windowed) - dispatch_limit < len(windowed):
            frees_at.append(windowed[len(windowed) - dispatch_limit][0] + period)
    return consumed, blocked, (max(frees_at) if frees_at else None)


def _is_staff(*, user: User) -> bool:
    return bool(user.is_staff or user.is_superuser)


def _next_monthly_slot(
    *, config: FragranceConfig, after: datetime, not_before: datetime | None
) -> datetime | None:
    """
    First monthly slot strictly after `after` and at or after `not_before`,
    in UTC; None for a legacy weekly/yearly row (its slot is not modelled).
    The day is capped exactly as signals.py caps it when building the crontab.
    """
    if config.frequency in ("weekly", "yearly"):
        return None
    tz = _beat_timezone()
    day = max(1, min(config.run_day_of_month, MAX_RUN_DAY_OF_MONTH))
    local_after = after.astimezone(tz)
    year, month = local_after.year, local_after.month
    # 26 months covers the longest quota period (an annual plan's 365 days) plus slack.
    for _ in range(26):
        slot = datetime(year, month, day, config.run_hour, tzinfo=tz)
        if slot > after and (not_before is None or slot >= not_before):
            return slot.astimezone(dt_timezone.utc)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return None


def _next_run_at(
    *, user: User, plan: Plan | None, has_access: bool, exempt: bool, now: datetime
) -> datetime | None:
    """
    When the beat scheduler will next start this user's run, or None when
    that is unknown (lapsed/revoked, no FragranceConfig yet, a legacy
    non-monthly schedule, or a quota block that never lifts). Read-only.

    The scheduled path is quota-checked with source="beat", so eligibility
    comes from _evaluate_quota(source="beat"). Beat only fires at slots and
    does not retry a refused one, so this is the first slot at or after
    eligibility, not eligibility itself.
    """
    if not has_access:
        return None
    config = FragranceConfig.objects.filter(user=user).first()
    if config is None:
        return None
    not_before: datetime | None = None
    if not exempt:
        _, blocked, eligible_at = _evaluate_quota(user=user, plan=plan, now=now, source="beat")
        if blocked:
            if eligible_at is None:
                return None
            not_before = eligible_at
    return _next_monthly_slot(config=config, after=now, not_before=not_before)


def _quota_terms(*, plan: Plan | None) -> tuple[timedelta, int, int]:
    """Reads quota_period_days, never billing_interval -- an annual plan must not allow twelve runs on day one."""
    if plan is None:
        return (
            timedelta(days=DEFAULT_QUOTA_PERIOD_DAYS),
            DEFAULT_MAX_RUNS_PER_QUOTA_PERIOD,
            DEFAULT_FAILED_RUN_GRACE,
        )
    return (
        timedelta(days=plan.quota_period_days),
        plan.max_runs_per_quota_period,
        plan.failed_run_grace,
    )


def _resolve_after_race(
    recommendation: Recommendation, action: str, original: IntegrityError
) -> tuple[Fragrance, bool]:
    """Re-resolves from committed state after apply_recommendation_rating loses a create/claim race."""
    linked = Fragrance.objects.filter(source_recommendation_id=recommendation.id).first()
    if linked is not None:
        if linked.status == action:
            return linked, False
        raise RatingConflict(existing_action=linked.status)

    existing = Fragrance.objects.filter(
        user=recommendation.user, name=recommendation.name
    ).first()
    if existing is not None:
        raise RatingConflict(existing_action=existing.status)

    raise original
