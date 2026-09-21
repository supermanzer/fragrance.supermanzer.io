import csv
import hashlib
import hmac
from typing import IO

from django.conf import settings
from django.contrib.auth.models import User
from django.core import signing
from django.db import IntegrityError, transaction
from django.utils.text import slugify
from rest_framework.request import Request

from .models import FRAGRANCE_STATUSES, BetaSignup, Fragrance, Recommendation

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


class RatingConflict(Exception):
    """Raised when a recommendation already has a different rating recorded than the one requested."""

    def __init__(self, existing_action: str) -> None:
        self.existing_action = existing_action
        super().__init__(f"Recommendation already rated as {existing_action}.")


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


RECOMMENDATION_FEEDBACK_SALT = "recommendation-feedback"
RECOMMENDATION_FEEDBACK_MAX_AGE = 60 * 60 * 24 * 90  # 90 days


def generate_recommendation_token(recommendation: Recommendation, action: str) -> str:
    """Signs a payload identifying which recommendation/action a feedback link represents."""
    payload = {"rid": recommendation.id, "action": action, "uid": recommendation.user_id}
    return signing.dumps(payload, salt=RECOMMENDATION_FEEDBACK_SALT)


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


# Domains where the mailbox provider itself ignores dots in the local part,
# so "r.yan@gmail.com" and "ryan@gmail.com" are the same inbox. Only Gmail's
# own domains are known to do this widely enough to normalize on safely --
# stripping dots elsewhere would risk merging two genuinely distinct addresses.
_DOT_INSENSITIVE_DOMAINS = {"gmail.com", "googlemail.com"}


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


def hash_signup_ip(*, ip: str) -> str:
    """HMACs a client IP with SECRET_KEY so BetaSignup never stores the raw address."""
    return hmac.new(
        key=settings.SECRET_KEY.encode(), msg=ip.encode(), digestmod=hashlib.sha256
    ).hexdigest()


_MAX_USERNAME_LENGTH = 150


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


BETA_INVITE_SALT = "beta-invite"
BETA_INVITE_MAX_AGE = 60 * 60 * 24 * 14  # 14 days


def generate_beta_invite_token(*, signup: BetaSignup) -> str:
    """
    Signs a payload identifying which BetaSignup/User an invite-accept link
    represents. Deliberately not Django's PasswordResetTokenGenerator --
    PASSWORD_RESET_TIMEOUT is 1 hour, and raising it globally to suit a
    14-day invite window would weaken actual password resets.
    """
    payload = {"sid": signup.id, "uid": signup.invited_user_id}
    return signing.dumps(payload, salt=BETA_INVITE_SALT)


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


class BetaInviteTokenError(Exception):
    """Base for resolve_beta_invite_signup rejections."""


class BetaInviteTokenInvalidError(BetaInviteTokenError):
    """
    Token missing/tampered/expired, unknown signup, wrong status (anything
    other than 'approved' or 'converted'), or invited_user missing/inactive/
    mismatched. Maps to 400 {"code": "invalid_or_expired"}.
    """


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
