import codecs
import csv
import io
import logging

import redis
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import generics, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .models import (
    BetaSignup,
    Fragrance,
    FragranceConfig,
    PreferenceProfile,
    Recommendation,
    RecommendationRun,
)
from .serializers import (
    BetaSignupSerializer,
    ChangePasswordSerializer,
    FragranceConfigSerializer,
    FragranceSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    PreferenceProfileSerializer,
    RecommendationRatingSerializer,
    RecommendationRunSerializer,
    RecommendationSerializer,
)
from .services import (
    RatingConflict,
    apply_recommendation_rating,
    get_client_ip,
    hash_signup_ip,
    import_collection_from_csv,
    normalize_email,
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


class BetaSignupThrottle(ScopedRateThrottle):
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

    def allow_request(self, request: Request, view: generics.GenericAPIView) -> bool:
        """
        Fails open on a Redis-backed cache error rather than propagating it.

        settings.CACHES["default"] now points at Redis (see settings.py) so
        this throttle can be enforced correctly across gunicorn's multiple
        worker processes. Django's built-in RedisCache does not swallow
        connection errors the way django-redis can with IGNORE_EXCEPTIONS --
        SimpleRateThrottle.allow_request calls self.cache.get()/.set() with
        no try/except of its own. Left uncaught, a Redis outage would 500 this
        public, unauthenticated signup page. nginx's `zone=beta` limit_req is
        the authoritative rate limit in production regardless (this DRF
        throttle is defense-in-depth for the dev/no-nginx path and for
        per-process dilution) -- defense-in-depth must never be the thing
        that takes the page down, so an unreachable cache means "don't
        throttle," not "error."
        """
        try:
            return super().allow_request(request, view)
        except redis.exceptions.RedisError:
            logger.warning(
                "beta signup throttle cache unavailable; allowing request without throttling"
            )
            return True


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
        BetaSignup.objects.bulk_create(
            [BetaSignup(email=email, normalized_email=normalized, signup_ip_hash=ip_hash)],
            ignore_conflicts=True,
        )
        return Response(_BETA_SIGNUP_RESPONSE, status=status.HTTP_202_ACCEPTED)


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
    permission_classes = [permissions.AllowAny]
    serializer_class = PasswordResetRequestSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = User.objects.get(email=serializer.validated_data['email'])
        except User.DoesNotExist:
            return Response(_RESET_RESPONSE)
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user=user)
        reset_url = f"{settings.FRONTEND_URL}/auth/reset-password?uid={uid}&token={token}"
        send_mail(
            subject='Reset your password',
            message=render_to_string(
                template_name='fragrance/password_reset_email.txt',
                context={'username': user.username, 'reset_url': reset_url},
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
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


class FragranceConfigView(generics.RetrieveUpdateAPIView):
    serializer_class = FragranceConfigSerializer

    def get_object(self):
        obj, _ = FragranceConfig.objects.get_or_create(user=self.request.user)
        return obj


class FragranceConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = 'A fragrance with this name and house is already in your collection.'
    default_code = 'fragrance_conflict'


class FragranceViewSet(viewsets.ModelViewSet):
    serializer_class = FragranceSerializer

    def get_queryset(self):
        qs = Fragrance.objects.filter(user=self.request.user)
        status = self.request.query_params.get("status")
        if status:
            qs = qs.filter(status=status)
        return qs

    def perform_create(self, serializer):
        self._save_or_conflict(serializer=serializer, user=self.request.user)

    def perform_update(self, serializer):
        self._save_or_conflict(serializer=serializer)

    @staticmethod
    def _save_or_conflict(serializer, **extra) -> None:
        # Backstop for the race the serializer's identity check cannot close
        # (two concurrent writes of one identity): the unique constraint wins
        # and the loser gets a 409, not a 500. The savepoint keeps the
        # surrounding transaction usable.
        try:
            with transaction.atomic():
                serializer.save(**extra)
        except IntegrityError:
            raise FragranceConflict()


class PreferenceProfileView(generics.RetrieveAPIView):
    serializer_class = PreferenceProfileSerializer

    def get_object(self):
        try:
            return PreferenceProfile.objects.filter(
                user=self.request.user
            ).latest("generated_at")
        except PreferenceProfile.DoesNotExist:
            raise NotFound("No preference profile found.")


class PreferenceProfileHistoryView(generics.ListAPIView):
    serializer_class = PreferenceProfileSerializer

    def get_queryset(self):
        return PreferenceProfile.objects.filter(
            user=self.request.user
        ).order_by("-generated_at")


class RecommendationListView(generics.ListAPIView):
    serializer_class = RecommendationSerializer

    def get_queryset(self):
        qs = Recommendation.objects.filter(
            user=self.request.user
        ).select_related("promoted_fragrance")
        if self.request.query_params.get("include_replaced") != "true":
            qs = qs.filter(status="confirmed")
        return qs.order_by("-run__triggered_at")


class RecommendationRunViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = RecommendationRunSerializer

    def get_queryset(self):
        return RecommendationRun.objects.filter(
            user=self.request.user
        ).prefetch_related(
            Prefetch(
                "picks",
                queryset=Recommendation.objects.filter(
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

        if RecommendationRun.objects.filter(
            user=request.user, status__in=["pending", "running"]
        ).exists():
            return Response(
                {"detail": "A run is already in progress."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        run = RecommendationRun.objects.create(
            user=request.user, status="pending"
        )
        task = monthly_fragrance_run.delay(
            user_id=request.user.id, run_id=run.id
        )
        run.celery_task_id = task.id
        run.save(update_fields=["celery_task_id"])
        return Response({"run_id": run.id}, status=status.HTTP_202_ACCEPTED)


class ExportCollectionView(generics.GenericAPIView):
    def get(self, request) -> HttpResponse:
        fragrances = Fragrance.objects.filter(user=request.user).order_by('name')

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
            recommendation = Recommendation.objects.select_related(
                "promoted_fragrance"
            ).get(id=payload["rid"], user_id=payload["uid"], status="confirmed")
        except Recommendation.DoesNotExist:
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
