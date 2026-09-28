import json
import codecs
import csv
import io
import logging
from datetime import datetime
from datetime import timezone as dt_timezone

import redis
import stripe
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from kombu.exceptions import OperationalError
from rest_framework import generics, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ParseError
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from . import billing
from .ai_providers.base import sanitize_sdk_error_message
from .models import (
    BetaSignup,
    Fragrance,
    FragranceConfig,
    PreferenceProfile,
    Recommendation,
    RecommendationRun,
    StripeEvent,
    Subscription,
)
from .permissions import HasActiveSubscription, HasActiveSubscriptionOrReadOnly
from .serializers import (
    BetaAcceptSerializer,
    BetaSignupSerializer,
    ChangePasswordSerializer,
    ChangePlanSerializer,
    CheckoutCreateSerializer,
    FragranceConfigSerializer,
    FragranceSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    PlanSerializer,
    PreferenceProfileSerializer,
    RecommendationRatingSerializer,
    RecommendationRunSerializer,
    RecommendationSerializer,
)
from .services import (
    BetaInviteTokenAlreadyUsedError,
    BetaInviteTokenInvalidError,
    QuotaExceeded,
    RatingConflict,
    RunInProgress,
    apply_recommendation_rating,
    get_client_ip,
    get_or_create_fragrance_config,
    hash_signup_ip,
    import_collection_from_csv,
    normalize_email,
    resolve_beta_invite_signup,
    start_recommendation_run,
    verify_recommendation_token,
)

logger = logging.getLogger(__name__)

# Characters that spreadsheet apps interpret as formula triggers.
_FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')


def _safe_cell(value: str) -> str:
    """Prefix formula-trigger chars to prevent CSV injection in spreadsheet apps."""
    if value and value[0] in _FORMULA_PREFIXES:
        return "'" + value
    return value


_BETA_SIGNUP_RESPONSE = {
    "detail": "Thanks for your interest. If a spot opens up, we'll be in touch."
}


class FailOpenScopedRateThrottle(ScopedRateThrottle):
    """
    ScopedRateThrottle that fails open on a Redis-backed cache error.

    settings.CACHES["default"] points at Redis so throttles are enforced
    correctly across gunicorn's multiple worker processes. Django's built-in
    RedisCache does not swallow connection errors the way django-redis can
    with IGNORE_EXCEPTIONS -- SimpleRateThrottle.allow_request calls
    self.cache.get()/.set() with no try/except of its own, so a Redis outage
    would otherwise 500 every throttled view.

    The trade-off differs per view:
    - /api/v1/beta/ (public, unauthenticated): nginx `zone=beta` limit_req is
      the authoritative limiter; this throttle is defense-in-depth and must
      never be what takes the page down.
    - Billing checkout / session views: /api/v1/billing/ has nginx
      `zone=billing` limit_req as a backstop, but it is coarser than these
      per-scope limits, so during a Redis outage failing open leaves only that. That is still the right call: callers are authenticated,
      approved beta users, and 500ing a customer who has just paid (session
      confirmation) or is about to pay (checkout) is the worse failure.
    """

    def allow_request(self, request: Request, view: APIView) -> bool:
        try:
            return super().allow_request(request, view)
        except redis.exceptions.RedisError:
            logger.warning(
                "throttle cache unavailable; allowing request without throttling (scope=%s)",
                getattr(view, "throttle_scope", None),
            )
            return True


class BetaSignupThrottle(FailOpenScopedRateThrottle):
    """
    Keys the throttle on the same DEBUG-gated proxy-trust resolution as
    services.get_client_ip, rather than the stock BaseThrottle.get_ident.

    With NUM_PROXIES unset (the project's default, and unset deliberately --
    no other throttle exists yet to need it), get_ident trusts the *entire*
    client-supplied X-Forwarded-For string when present. In production nginx
    only appends to XFF (proxy_set_header X-Forwarded-For
    $proxy_add_x_forwarded_for), so a client that sends its own XFF value
    gets a fresh bucket on every request and the rate limit never engages.
    Reusing get_client_ip also makes this throttle's key agree with the
    signup_ip_hash stored on BetaSignup, instead of the two quietly
    disagreeing about which address is "the" client.
    """

    def get_ident(self, request: Request) -> str:
        return get_client_ip(request=request)


class BetaSignupView(generics.GenericAPIView):
    """
    Public waitlist capture. Account creation happens only via admin approval
    (a later slice) -- this endpoint never mints a User or a token.

    Returns the exact same 202 body regardless of whether the email is new,
    already pending, already approved, or belongs to an existing User:
    anything that differs by case is an account-enumeration oracle. Same
    rule already applied to verify_recommendation_token in services.py.
    """

    permission_classes = [permissions.AllowAny]
    serializer_class = BetaSignupSerializer
    throttle_classes = [BetaSignupThrottle]
    throttle_scope = "beta-signup"

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if serializer.validated_data.get("website"):
            # Honeypot tripped -- persist nothing, but respond identically.
            # Logged (not surfaced to the client) so a false-positive trip --
            # e.g. a password manager/autofill heuristic populating a field
            # literally named "website" for a real visitor -- is visible in
            # ops instead of silently losing that person's signup. The raw
            # email isn't logged, matching this project's existing restraint
            # (sanitize_sdk_error_message) about what reaches log output.
            logger.info("beta signup honeypot tripped")
            return Response(_BETA_SIGNUP_RESPONSE, status=status.HTTP_202_ACCEPTED)

        email = serializer.validated_data["email"]
        normalized = normalize_email(email=email)
        ip_hash = hash_signup_ip(ip=get_client_ip(request=request))
        # Deliberately no "does a User already own this email" branch: that
        # would query on the raw address while dedup is keyed on the
        # normalized one, so the two could disagree (a Gmail-dot variant of
        # an existing user's address would slip through and create a row
        # anyway) -- and branching on it at all gives a timing/query-count
        # signal on an endpoint whose whole point is not distinguishing
        # these cases. The admin-approval step (a later slice) is where
        # "does a User already exist for this email" actually gets guarded.
        #
        # bulk_create(..., ignore_conflicts=True) instead of get_or_create:
        # on PostgreSQL this compiles to a single INSERT ... ON CONFLICT DO
        # NOTHING, so the query shape is identical whether normalized_email
        # is new or already present -- get_or_create's SELECT-then-maybe-
        # INSERT gave "already on the waitlist" a measurably smaller query
        # footprint than "new to the waitlist," a narrow but real timing
        # signal. This narrows, not closes, that channel: a successful
        # insert still does more index/WAL work than a no-op conflict.
        BetaSignup.objects.bulk_create(  # ty: ignore[unresolved-attribute]
            [BetaSignup(email=email, normalized_email=normalized, signup_ip_hash=ip_hash)],
            ignore_conflicts=True,
        )
        return Response(_BETA_SIGNUP_RESPONSE, status=status.HTTP_202_ACCEPTED)


_BETA_ACCEPT_INVALID_RESPONSE = {"code": "invalid_or_expired"}
_BETA_ACCEPT_ALREADY_USED_RESPONSE = {"code": "already_used"}


class BetaAcceptView(generics.GenericAPIView):
    """
    GET is a read-only pre-check so a dead invite link resolves on page load
    rather than after the user fills in a password form -- it never touches
    signup.status, only resolve_beta_invite_signup's read path.

    POST sets the password for an admin-approved beta signup and returns a
    JWT pair, landing the user logged in on the paywall. Single-use: the
    token itself is re-validated by the serializer, but the status
    transition here is what actually enforces single-use, under a row lock,
    so a leaked link replayed concurrently can't succeed twice.

    Both verbs report the same two-way split: 400 "invalid_or_expired" for
    every rejection reason except one, and 409 "already_used" specifically
    when the signup has already converted -- see
    services.resolve_beta_invite_signup and BetaInviteTokenAlreadyUsedError
    for why that one distinction is safe to surface.
    """

    permission_classes = [permissions.AllowAny]
    serializer_class = BetaAcceptSerializer

    def get(self, request, *args, **kwargs):
        try:
            resolve_beta_invite_signup(token=request.query_params.get("token", ""))
        except BetaInviteTokenAlreadyUsedError:
            return Response(_BETA_ACCEPT_ALREADY_USED_RESPONSE, status=status.HTTP_409_CONFLICT)
        except BetaInviteTokenInvalidError:
            return Response(_BETA_ACCEPT_INVALID_RESPONSE, status=status.HTTP_400_BAD_REQUEST)
        return Response({"valid": True})

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except BetaInviteTokenAlreadyUsedError:
            return Response(_BETA_ACCEPT_ALREADY_USED_RESPONSE, status=status.HTTP_409_CONFLICT)
        except BetaInviteTokenInvalidError:
            return Response(_BETA_ACCEPT_INVALID_RESPONSE, status=status.HTTP_400_BAD_REQUEST)
        password = serializer.validated_data["password"]

        try:
            with transaction.atomic():
                # No select_related("invited_user") here: it's a nullable FK,
                # and Postgres refuses SELECT ... FOR UPDATE on the nullable
                # side of an outer join. Fetching it via the accessor below
                # is a separate, unlocked query -- fine, since only the
                # BetaSignup row's status transition needs the lock.
                signup = BetaSignup.objects.select_for_update().get(  # ty: ignore[unresolved-attribute]
                    pk=serializer.validated_data["signup_id"]
                )
                # Re-checked under the lock: a concurrent replay of the same
                # leaked link may have already converted (or otherwise
                # changed) this signup between the serializer's read and
                # this lock being acquired.
                if signup.status == "converted":
                    return Response(
                        _BETA_ACCEPT_ALREADY_USED_RESPONSE, status=status.HTTP_409_CONFLICT
                    )
                if signup.status != "approved" or signup.invited_user is None:
                    return Response(
                        _BETA_ACCEPT_INVALID_RESPONSE, status=status.HTTP_400_BAD_REQUEST
                    )
                user = signup.invited_user
                user.set_password(password)
                user.save(update_fields=["password"])
                signup.status = "converted"
                signup.converted_at = timezone.now()
                signup.save(update_fields=["status", "converted_at"])
        except BetaSignup.DoesNotExist:  # ty: ignore[unresolved-attribute]
            return Response(_BETA_ACCEPT_INVALID_RESPONSE, status=status.HTTP_400_BAD_REQUEST)

        refresh = RefreshToken.for_user(user)
        return Response({"access": str(refresh.access_token), "refresh": str(refresh)})


class ChangePasswordView(generics.GenericAPIView):
    serializer_class = ChangePasswordSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        user.set_password(serializer.validated_data['new_password'])
        user.save()
        refresh_token = serializer.validated_data.get('refresh', '')
        if refresh_token:
            try:
                RefreshToken(token=refresh_token).blacklist()
            except Exception:
                pass
        return Response({'detail': 'Password changed successfully.'})


_RESET_RESPONSE = {'detail': 'If an account with that email exists, a reset link has been sent.'}


class PasswordResetRequestView(generics.GenericAPIView):
    """
    Deliberately does not use django.contrib.auth.forms.PasswordResetForm:
    that form's get_users() filters out any account with an unusable
    password (django.contrib.auth.forms, is_active/has_usable_password
    check), which would silently exclude every approved-but-not-yet-
    converted beta user -- they're created via set_unusable_password() in
    BetaSignupAdmin.approve_signups and stay that way until BetaAcceptView
    sets a real one. The User.objects.filter(email__iexact=...) lookup below has no
    such filter, and default_token_generator happily generates and
    validates a token against an unusable password, so this is the only
    working recovery path for a dead/expired invite link -- the copy on
    /beta/accept now actively tells users to use this endpoint for exactly
    that reason. Do not "simplify" this back to PasswordResetForm; doing so
    would quietly strip every approved-but-not-yet-accepted user of their
    only way in.
    """

    permission_classes = [permissions.AllowAny]
    serializer_class = PasswordResetRequestSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # iexact to match BetaSignupAdmin.approve_signups; filter() rather than
        # get() because User.email is not unique, and one mail per matching
        # account (each with its own uid/token) beats a 500 on duplicates.
        users = User.objects.filter(
            email__iexact=serializer.validated_data['email']
        ).order_by('pk')
        from .tasks import send_password_reset_email

        # Mail is sent from Celery so a Postmark failure or slow round trip
        # can neither 500 this request nor make its latency depend on whether
        # (or how many) accounts match -- both would be an enumeration
        # oracle. Only the user id is queued; the token is minted in the task
        # so it never sits in the Redis broker. A broker outage is logged and
        # swallowed for the same reason: the response must stay uniform.
        for user in users:
            try:
                send_password_reset_email.delay(user_id=user.pk)
            except OperationalError:
                logger.error(
                    "could not enqueue password reset email for user_id=%s; broker unavailable",
                    user.pk,
                )
        return Response(_RESET_RESPONSE)


class PasswordResetConfirmView(generics.GenericAPIView):
    permission_classes = [permissions.AllowAny]
    serializer_class = PasswordResetConfirmSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']
        user.set_password(serializer.validated_data['new_password'])
        user.save()
        return Response({'detail': 'Password has been reset successfully.'})


_REVERT_EMAIL_INVALID_RESPONSE = {"detail": "This link is invalid or has expired."}


class RevertEmailView(APIView):
    """
    POST {"token"} from the "this wasn't me" link in the old-address notice.
    POST-only on purpose: mail scanners prefetch links with GET, and a state
    change on GET would fire the revert for them (GET is 405). Every failure
    -- bad body, bad/expired signature, unknown user, email no longer equal to
    the token's new address, staff account -- returns the same 400 body, so
    the endpoint is not an oracle. See billing.revert_customer_email.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes: list = []
    throttle_classes = [BetaSignupThrottle]
    throttle_scope = "revert-email"

    def post(self, request: Request) -> Response:
        try:
            data = request.data
        except ParseError:
            data = None
        token = data.get("token") if isinstance(data, dict) else None
        if not billing.revert_customer_email(token=token):
            return Response(_REVERT_EMAIL_INVALID_RESPONSE, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {
                "detail": "Your previous email address was restored and all sessions were signed out. "
                "Reset your password now."
            }
        )


class FragranceConfigView(generics.RetrieveUpdateAPIView):
    serializer_class = FragranceConfigSerializer
    permission_classes = [HasActiveSubscriptionOrReadOnly]

    def get_object(self):
        return get_or_create_fragrance_config(user=self.request.user)


class FragranceViewSet(viewsets.ModelViewSet):
    serializer_class = FragranceSerializer
    permission_classes = [HasActiveSubscriptionOrReadOnly]

    def get_queryset(self):
        qs = Fragrance.objects.filter(user=self.request.user)  # ty: ignore[unresolved-attribute]
        status = self.request.query_params.get("status")
        if status:
            qs = qs.filter(status=status)
        return qs

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class PreferenceProfileView(generics.RetrieveAPIView):
    serializer_class = PreferenceProfileSerializer

    def get_object(self):
        try:
            return PreferenceProfile.objects.filter(  # ty: ignore[unresolved-attribute]
                user=self.request.user
            ).latest("generated_at")
        except PreferenceProfile.DoesNotExist:  # ty: ignore[unresolved-attribute]
            raise NotFound("No preference profile found.")


class PreferenceProfileHistoryView(generics.ListAPIView):
    serializer_class = PreferenceProfileSerializer

    def get_queryset(self):
        return PreferenceProfile.objects.filter(  # ty: ignore[unresolved-attribute]
            user=self.request.user
        ).order_by("-generated_at")


class RecommendationListView(generics.ListAPIView):
    serializer_class = RecommendationSerializer

    def get_queryset(self):
        qs = Recommendation.objects.filter(  # ty: ignore[unresolved-attribute]
            user=self.request.user
        ).select_related("promoted_fragrance")
        if self.request.query_params.get("include_replaced") != "true":
            qs = qs.filter(status="confirmed")
        return qs.order_by("-run__triggered_at")


class RecommendationRunViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = RecommendationRunSerializer
    permission_classes = [HasActiveSubscriptionOrReadOnly]

    def get_queryset(self):
        return RecommendationRun.objects.filter(  # ty: ignore[unresolved-attribute]
            user=self.request.user
        ).prefetch_related(
            Prefetch(
                "picks",
                queryset=Recommendation.objects.filter(  # ty: ignore[unresolved-attribute]
                    status="confirmed"
                ).select_related("promoted_fragrance"),
            )
        ).order_by("-triggered_at")

    @action(detail=True, methods=["post"])
    def resend_email(self, request, pk=None):
        from fragrance.tasks import send_recommendation_email

        run = self.get_object()
        if run.status != 'done':
            return Response(
                {"detail": "Email can only be sent for completed runs."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if run.email_status in ('pending', 'sent'):
            return Response(
                {"detail": "Email is already sent or delivery is in progress."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        run.email_status = 'pending'
        run.error_message = ''
        run.save(update_fields=['email_status', 'error_message'])
        send_recommendation_email.delay(user_id=request.user.id, run_id=run.id)
        return Response({"detail": "Email resend queued."}, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=["post"])
    def trigger(self, request):
        from fragrance.tasks import monthly_fragrance_run

        try:
            run = start_recommendation_run(user=request.user, source="api")
        except QuotaExceeded as exc:
            return Response(
                {
                    "code": "quota_exceeded",
                    "detail": "The run quota for this period has been used.",
                    "next_eligible_at": exc.next_eligible_at,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        except RunInProgress:
            return Response(
                {"code": "run_in_progress", "detail": "A run is already in progress."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        # SubscriptionRequired propagates: DRF renders it as the 402 body.
        try:
            task = monthly_fragrance_run.delay(
                user_id=request.user.id, run_id=run.id  # ty: ignore[unresolved-attribute]
            )
        except OperationalError:
            # Broker down: don't leave a 'pending' run that would block this
            # user via the in-flight guard forever. 'failed' doesn't burn quota.
            # Django noise, not a Stripe params-shape issue: RecommendationRun's
            # CharField/TextField attributes read to ty as their descriptor
            # class rather than plain str (no django-stubs installed -- see
            # [tool.ty] in pyproject.toml).
            run.status = "failed"  # ty: ignore[invalid-assignment]
            run.error_message = "Could not queue the run; please try again shortly."  # ty: ignore[invalid-assignment]
            run.save(update_fields=["status", "error_message"])
            return Response(
                {"detail": "Could not queue the run; please try again shortly."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        run.celery_task_id = task.id
        run.save(update_fields=["celery_task_id"])
        return Response({"run_id": run.id}, status=status.HTTP_202_ACCEPTED)  # ty: ignore[unresolved-attribute]


class ExportCollectionView(generics.GenericAPIView):
    def get(self, request) -> HttpResponse:
        fragrances = Fragrance.objects.filter(user=request.user).order_by('name')  # ty: ignore[unresolved-attribute]

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['fragrance', 'status', 'house', 'notes'])
        for f in fragrances:
            writer.writerow([
                _safe_cell(f.name),
                f.status,  # enum — never a formula trigger
                _safe_cell(f.house),
                _safe_cell(f.notes),
            ])

        response = HttpResponse(output.getvalue(), content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="fragrances.csv"'
        response['X-Content-Type-Options'] = 'nosniff'
        return response


class ImportCollectionView(generics.GenericAPIView):
    permission_classes = [HasActiveSubscription]
    parser_classes = [MultiPartParser]

    def post(self, request):
        file = request.FILES.get("file")
        if not file:
            return Response(
                {"detail": "No file provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not file.name.lower().endswith(".csv"):
            return Response(
                {"detail": "File must be a CSV."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if file.size > 2 * 1024 * 1024:
            return Response(
                {"detail": "File too large (max 2 MB)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            reader = codecs.getreader("utf-8")(file)
            result = import_collection_from_csv(
                user=request.user, file_obj=reader
            )
            return Response(result, status=status.HTTP_200_OK)
        except (UnicodeDecodeError, LookupError):
            return Response(
                {"detail": "File must be UTF-8 encoded."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )


class RecommendationRateView(APIView):
    """Authenticated rating endpoint for the runs page UI."""

    def post(self, request, pk: int | None = None) -> Response:
        recommendation = get_object_or_404(
            Recommendation, pk=pk, user=request.user
        )
        serializer = RecommendationRatingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        action_value = serializer.validated_data["action"]

        if recommendation.status != "confirmed":
            return Response(
                {"detail": "This pick can no longer be rated."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            fragrance, is_new = apply_recommendation_rating(
                recommendation=recommendation, action=action_value
            )
        except RatingConflict as exc:
            return Response(
                {
                    "detail": "This pick was already rated.",
                    "reason": "already_rated",
                    "existing_action": exc.existing_action,
                },
                status=status.HTTP_409_CONFLICT,
            )

        payload = {
            "id": fragrance.id,
            "action": fragrance.status,
            "name": fragrance.name,
            "house": fragrance.house,
        }
        if is_new:
            return Response(payload, status=status.HTTP_201_CREATED)
        payload["already_rated"] = True
        return Response(payload, status=status.HTTP_200_OK)


_CONFIRM_NOT_FOUND = {"detail": "Not found."}


class RecommendationConfirmView(APIView):
    """
    Unauthenticated token-based rating surface reached from recommendation emails.

    GET is a read-only preview (no side effects) so mail clients/scanners that
    prefetch links can never silently record a rating; POST performs the write,
    only ever triggered by an explicit user tap on the confirm page.
    """

    permission_classes = [permissions.AllowAny]

    def _resolve(self, request) -> tuple[Recommendation, dict] | None:
        token = request.query_params.get("t", "")
        payload = verify_recommendation_token(token=token)
        if payload is None:
            return None
        try:
            recommendation = Recommendation.objects.select_related(  # ty: ignore[unresolved-attribute]
                "promoted_fragrance"
            ).get(id=payload["rid"], user_id=payload["uid"], status="confirmed")
        except Recommendation.DoesNotExist:  # ty: ignore[unresolved-attribute]
            return None
        return recommendation, payload

    def get(self, request) -> Response:
        resolved = self._resolve(request)
        if resolved is None:
            return Response(_CONFIRM_NOT_FOUND, status=status.HTTP_404_NOT_FOUND)
        recommendation, payload = resolved
        existing = getattr(recommendation, "promoted_fragrance", None)
        return Response(
            {
                "name": recommendation.name,
                "house": recommendation.house,
                "action": payload["action"],
                "already_rated": existing is not None,
                "rated_as": existing.status if existing else None,
            }
        )

    def post(self, request) -> Response:
        resolved = self._resolve(request)
        if resolved is None:
            return Response(_CONFIRM_NOT_FOUND, status=status.HTTP_404_NOT_FOUND)
        recommendation, payload = resolved

        try:
            fragrance, is_new = apply_recommendation_rating(
                recommendation=recommendation, action=payload["action"]
            )
        except RatingConflict as exc:
            return Response(
                {"detail": "This pick was already rated.", "rated_as": exc.existing_action},
                status=status.HTTP_409_CONFLICT,
            )

        if not is_new:
            # Same action already recorded via this or another surface — unlike the
            # authenticated /rate/ endpoint, a token resubmission is never treated as
            # success: the confirm page always shows the "already rated" state instead.
            return Response(
                {"detail": "This pick was already rated.", "rated_as": fragrance.status},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            {
                "name": fragrance.name,
                "house": fragrance.house,
                "action": fragrance.status,
            }
        )


class BillingCheckoutView(generics.GenericAPIView):
    """
    GET renders the paywall from one server-owned source (active Plan rows)
    rather than duplicating prices into the frontend. POST resolves the
    request's plan slug server-side to a Plan and its stripe_price_id --
    never a client-supplied price_id, which would let anyone check out
    against any Price on the account. Default IsAuthenticated (project
    default), deliberately not subscription-gated: a user must be able to
    reach checkout precisely because they don't have a subscription yet.
    """

    serializer_class = CheckoutCreateSerializer
    throttle_classes = [FailOpenScopedRateThrottle]
    throttle_scope = "checkout"

    def get(self, request: Request) -> Response:
        plans = billing.get_active_plans()
        return Response({"plans": PlanSerializer(plans, many=True).data})

    def post(self, request: Request) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Exempt (staff) accounts already have full access: refuse before any
        # Stripe call, Customer, or Subscription row so a refusal has no side effects.
        if request.user.is_staff or request.user.is_superuser:
            return Response(
                {
                    "code": "exempt",
                    "detail": "This account has full access, so no subscription is needed.",
                },
                status=status.HTTP_409_CONFLICT,
            )
        try:
            url = billing.create_checkout_session(
                user=request.user, plan_slug=serializer.validated_data["slug"]
            )
        except billing.SubscriptionAlreadyActiveError:
            return Response(
                {"detail": "You already have an active subscription."},
                status=status.HTTP_409_CONFLICT,
            )
        except billing.UnknownPlanError:
            return Response({"detail": "Unknown plan."}, status=status.HTTP_400_BAD_REQUEST)
        except ImproperlyConfigured as exc:
            logger.error("checkout unavailable: %s", exc)
            return Response(
                {"detail": "Checkout is temporarily unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except billing.TermsUrlNotConfiguredError:
            # Already logged with the fix by billing.create_checkout_session.
            return Response(
                {"detail": "Could not start checkout. Please try again shortly."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except stripe.StripeError as exc:
            logger.error("stripe checkout session creation failed: %s", sanitize_sdk_error_message(exc))
            return Response(
                {"detail": "Could not start checkout. Please try again shortly."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"url": url}, status=status.HTTP_200_OK)


class BillingSubscriptionView(generics.GenericAPIView):
    """
    Already-active detection for the /billing paywall page: a user who
    already has a live subscription should never be shown the plan picker.
    Read-only, unlike BillingSessionView -- it never talks to Stripe, it
    only reads the local Subscription row. A user with no row gets an
    unsaved placeholder (status "none", plan None) so the response is the
    same {"status": "none", ...} rather than 404 -- and a GET never writes.

    Returns the same shape as BillingSessionView (billing.
    subscription_status_payload) plus that view's own "granted" flag --
    one shape for both endpoints is easier for the frontend to consume and
    harder to get wrong.
    """

    throttle_classes = [FailOpenScopedRateThrottle]
    throttle_scope = "billing-status"

    def get(self, request: Request) -> Response:
        subscription = (
            Subscription.objects.select_related("plan").filter(user=request.user).first()  # ty: ignore[unresolved-attribute]
            or Subscription(user=request.user)
        )
        return Response(billing.subscription_status_payload(subscription=subscription))


class BillingSessionView(generics.GenericAPIView):
    """
    Success-page fallback: a user who lands on /billing/success before the
    webhook arrives isn't left staring at a paywall they already paid for.
    Fulfillment runs through the exact same code path the webhook uses
    (fragrance.billing._grant_period_for_invoice), so calling this twice, or
    calling it after the webhook already fired, grants at most one period --
    see ProcessedPayment's uniqueness on stripe_invoice_id.
    """

    throttle_classes = [FailOpenScopedRateThrottle]
    throttle_scope = "billing-session"

    def get(self, request: Request, session_id: str) -> Response:
        # Local short-circuit: the success page polls this up to 18 times per
        # load, and once access is granted (typically by the webhook) every
        # further poll would otherwise cost a Stripe round trip for nothing.
        subscription = (
            Subscription.objects.select_related("plan")  # ty: ignore[unresolved-attribute]
            .filter(user=request.user)
            .first()
        )
        if subscription is not None and subscription.has_active_access:
            return Response(
                {
                    **billing.subscription_status_payload(subscription=subscription),
                    "granted": False,
                }
            )
        try:
            result = billing.fulfill_checkout_session(session_id=session_id, user=request.user)
        except billing.SessionLookupError:
            # SessionNotFoundError and SessionOwnershipError both land here,
            # deliberately identical: a session id belonging to someone else
            # must read the same as one that doesn't exist at all.
            return Response(
                {"detail": "Checkout session not found."}, status=status.HTTP_404_NOT_FOUND
            )
        except ImproperlyConfigured as exc:
            logger.error("billing session lookup unavailable: %s", exc)
            return Response(
                {"detail": "Billing is temporarily unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except stripe.StripeError as exc:
            logger.error("stripe session lookup failed: %s", sanitize_sdk_error_message(exc))
            return Response(
                {"detail": "Could not verify checkout session. Please try again shortly."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(result)


_EXEMPT_RESPONSE_BODY = {
    "code": "exempt",
    "detail": "This account has full access, so there is no billing to manage.",
}


def _stripe_failure_response(*, exc: Exception, action: str, unavailable: str, failed: str) -> Response:
    """
    Shared 503/502 mapping for the billing views, matching BillingCheckoutView:
    an unconfigured Stripe key is a 503, any Stripe error a 502, and neither
    ever leaks str(exc) into the response body.
    """
    if isinstance(exc, ImproperlyConfigured):
        logger.error("%s unavailable: %s", action, exc)
        return Response({"detail": unavailable}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    logger.error("stripe %s failed: %s", action, sanitize_sdk_error_message(exc))
    return Response({"detail": failed}, status=status.HTTP_502_BAD_GATEWAY)


class BillingPortalView(generics.GenericAPIView):
    """
    POST -> a one-shot Stripe Customer Portal URL for the caller's own
    customer. No request body is read: customer, configuration, return_url
    and flow_data are all server-derived (see billing.create_portal_session).
    Deliberately not subscription-gated, so a lapsed customer can still reach
    their invoices and update a card.
    """

    throttle_classes = [FailOpenScopedRateThrottle]
    throttle_scope = "billing-portal"

    def post(self, request: Request) -> Response:
        try:
            url = billing.create_portal_session(user=request.user)
        except billing.ExemptAccountError:
            return Response(_EXEMPT_RESPONSE_BODY, status=status.HTTP_409_CONFLICT)
        except billing.NoBillingAccountError:
            return Response(
                {
                    "code": "no_billing_account",
                    "detail": "There is no billing account to manage yet.",
                },
                status=status.HTTP_409_CONFLICT,
            )
        except (ImproperlyConfigured, stripe.StripeError) as exc:
            return _stripe_failure_response(
                exc=exc,
                action="billing portal",
                unavailable="Billing management is temporarily unavailable.",
                failed="Could not open billing management. Please try again shortly.",
            )
        return Response({"url": url}, status=status.HTTP_200_OK)


class BillingChangePlanView(generics.GenericAPIView):
    """
    GET reads the pending change from Stripe on demand (kept out of the
    frequently polled /billing/subscription/ payload), POST schedules a switch
    to the requested plan at the end of the current period with no proration,
    DELETE releases the pending change. The plan is a slug resolved against
    active Plan rows; a Stripe price id is never accepted. The Stripe
    subscription id always comes from the caller's own Subscription row.
    """

    serializer_class = ChangePlanSerializer
    throttle_classes = [FailOpenScopedRateThrottle]
    throttle_scope = "billing-change-plan"

    @staticmethod
    def _plan_change_conflict(*, exc: billing.PlanChangeError) -> Response:
        details = {
            "no_active_subscription": "You don't have an active subscription to change.",
            "same_plan": "You're already on that plan.",
            "already_scheduled": "A plan change is already scheduled.",
            "cancellation_scheduled": "Your subscription is set to end. Reactivate it before changing plans.",
            "change_in_progress": "Another plan change is being processed. Please try again in a moment.",
        }
        if isinstance(exc, billing.NoPendingChangeError):
            return Response(
                {"code": exc.code, "detail": "There is no scheduled plan change."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            {"code": exc.code, "detail": details.get(exc.code, "Plan change not possible.")},
            status=status.HTTP_409_CONFLICT,
        )

    def get(self, request: Request) -> Response:
        try:
            state = billing.get_plan_change_state(user=request.user)
        except billing.ExemptAccountError:
            return Response(_EXEMPT_RESPONSE_BODY, status=status.HTTP_409_CONFLICT)
        except billing.PlanChangeError as exc:
            return self._plan_change_conflict(exc=exc)
        except (ImproperlyConfigured, stripe.StripeError) as exc:
            return _stripe_failure_response(
                exc=exc,
                action="plan change lookup",
                unavailable="Plan changes are temporarily unavailable.",
                failed="Could not load your plan options. Please try again shortly.",
            )
        return Response(
            {
                "pending": state["pending"],
                "available": PlanSerializer(state["available"], many=True).data,
                "current_period_end": state["current_period_end"],
                "cancel_at_period_end": state["cancel_at_period_end"],
            }
        )

    def post(self, request: Request) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            pending = billing.schedule_plan_change(
                user=request.user, plan_slug=serializer.validated_data["plan"]
            )
        except billing.ExemptAccountError:
            return Response(_EXEMPT_RESPONSE_BODY, status=status.HTTP_409_CONFLICT)
        except billing.UnknownPlanError:
            return Response({"detail": "Unknown plan."}, status=status.HTTP_400_BAD_REQUEST)
        except billing.PlanChangeError as exc:
            return self._plan_change_conflict(exc=exc)
        except billing.PlanPriceMismatchError:
            return Response(
                {"detail": "Could not schedule the plan change. Please try again shortly."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except (ImproperlyConfigured, stripe.StripeError) as exc:
            return _stripe_failure_response(
                exc=exc,
                action="plan change",
                unavailable="Plan changes are temporarily unavailable.",
                failed="Could not schedule the plan change. Please try again shortly.",
            )
        return Response({"pending": pending}, status=status.HTTP_201_CREATED)

    def delete(self, request: Request) -> Response:
        try:
            billing.cancel_plan_change(user=request.user)
        except billing.ExemptAccountError:
            return Response(_EXEMPT_RESPONSE_BODY, status=status.HTTP_409_CONFLICT)
        except billing.PlanChangeError as exc:
            return self._plan_change_conflict(exc=exc)
        except (ImproperlyConfigured, stripe.StripeError) as exc:
            return _stripe_failure_response(
                exc=exc,
                action="plan change cancellation",
                unavailable="Plan changes are temporarily unavailable.",
                failed="Could not cancel the plan change. Please try again shortly.",
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


@csrf_exempt
@require_POST
def stripe_webhook(request: HttpRequest) -> HttpResponse:
    """
    Plain Django function view, not a DRF APIView: signature verification
    needs the untouched raw request.body, and staying outside DRF keeps this
    endpoint clear of DEFAULT_AUTHENTICATION_CLASSES / DEFAULT_PERMISSION_CLASSES.
    Registered twice in urls.py (with and without a trailing slash) -- see
    that file for why a slashless POST would otherwise 301 and be treated by
    Stripe as a delivery failure.

    Never do real work here: checkout blocks up to 10s on this response
    before redirecting the customer, and Stripe retries non-2xx responses
    for three days. Idempotency (via StripeEvent) happens synchronously so a
    replayed delivery is inert even if the queued task never runs; the
    actual fulfillment work is queued to process_stripe_event and this view
    returns immediately.
    """
    from .tasks import process_stripe_event

    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")

    try:
        client = billing.get_stripe_client()
        event = client.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError, ImproperlyConfigured):
        # Never log the raw body or the Stripe-Signature header here.
        logger.warning("stripe webhook signature verification failed")
        return HttpResponse(status=400)

    try:
        # Nested atomic() -> a savepoint: an IntegrityError from the unique
        # constraint on stripe_event_id must roll back only this insert, not
        # whatever transaction this view happens to run inside (e.g. under
        # ATOMIC_REQUESTS, or Django's TestCase-wrapped test transactions) --
        # the same reasoning as the nested atomic() in
        # billing._grant_period_for_invoice and services.apply_recommendation_rating.
        with transaction.atomic():
            StripeEvent.objects.create(  # ty: ignore[unresolved-attribute]
                stripe_event_id=event["id"],
                event_type=event["type"],
                stripe_created=datetime.fromtimestamp(event["created"], tz=dt_timezone.utc),
                # The signature-verified request body, parsed as plain JSON. NOT event.to_dict():
                # the SDK parses Stripe's `*_decimal` string fields into Decimal, which a JSONField
                # cannot store (a 500 on real price.*/invoice.* events, retried by Stripe for 3 days).
                payload=json.loads(payload),
            )
    except IntegrityError:
        # Already seen -- Stripe explicitly does not guarantee once-only
        # delivery. Return 200 immediately without re-queuing processing.
        return HttpResponse(status=200)

    process_stripe_event.delay(stripe_event_id=event["id"])
    return HttpResponse(status=200)
