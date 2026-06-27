import codecs
import csv
import io

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.db.models import Prefetch
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import generics, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .models import (
    Fragrance,
    FragranceConfig,
    PreferenceProfile,
    Recommendation,
    RecommendationRun,
)
from .serializers import (
    ChangePasswordSerializer,
    FragranceConfigSerializer,
    FragranceSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    PreferenceProfileSerializer,
    RecommendationRunSerializer,
    RecommendationSerializer,
    UserRegistrationSerializer,
)
from .services import import_collection_from_csv

# Characters that spreadsheet apps interpret as formula triggers.
_FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')


def _safe_cell(value: str) -> str:
    """Prefix formula-trigger chars to prevent CSV injection in spreadsheet apps."""
    if value and value[0] in _FORMULA_PREFIXES:
        return "'" + value
    return value


class RegisterView(generics.CreateAPIView):
    serializer_class = UserRegistrationSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "refresh": str(refresh),
                "access": str(refresh.access_token),
            },
            status=status.HTTP_201_CREATED,
        )


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


class FragranceViewSet(viewsets.ModelViewSet):
    serializer_class = FragranceSerializer

    def get_queryset(self):
        qs = Fragrance.objects.filter(user=self.request.user)
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
        qs = Recommendation.objects.filter(user=self.request.user)
        if self.request.query_params.get("include_replaced") != "true":
            qs = qs.filter(status="confirmed")
        return qs.order_by("-run__triggered_at")


class RecommendationRunViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = RecommendationRunSerializer

    def get_queryset(self):
        return RecommendationRun.objects.filter(
            user=self.request.user
        ).prefetch_related(
            Prefetch("picks", queryset=Recommendation.objects.filter(status="confirmed"))
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
