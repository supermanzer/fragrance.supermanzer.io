from django.contrib.auth.models import User
from rest_framework import generics, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .models import FragranceConfig, Fragrance, PreferenceProfile, Recommendation, RecommendationRun
from .serializers import (
    FragranceConfigSerializer,
    FragranceSerializer,
    PreferenceProfileSerializer,
    RecommendationRunSerializer,
    RecommendationSerializer,
    UserRegistrationSerializer,
)


class RegisterView(generics.CreateAPIView):
    serializer_class = UserRegistrationSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        refresh = RefreshToken.for_user(user)
        return Response({
            'refresh': str(refresh),
            'access': str(refresh.access_token),
        }, status=status.HTTP_201_CREATED)


class FragranceConfigView(generics.RetrieveUpdateAPIView):
    serializer_class = FragranceConfigSerializer

    def get_object(self):
        obj, _ = FragranceConfig.objects.get_or_create(user=self.request.user)
        return obj


class FragranceViewSet(viewsets.ModelViewSet):
    serializer_class = FragranceSerializer

    def get_queryset(self):
        qs = Fragrance.objects.filter(user=self.request.user)
        status = self.request.query_params.get('status')
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
            ).latest('generated_at')
        except PreferenceProfile.DoesNotExist:
            raise NotFound('No preference profile found.')


class PreferenceProfileHistoryView(generics.ListAPIView):
    serializer_class = PreferenceProfileSerializer

    def get_queryset(self):
        return PreferenceProfile.objects.filter(
            user=self.request.user
        ).order_by('-generated_at')


class RecommendationListView(generics.ListAPIView):
    serializer_class = RecommendationSerializer

    def get_queryset(self):
        return Recommendation.objects.filter(
            user=self.request.user
        ).order_by('-run__triggered_at')


class RecommendationRunViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = RecommendationRunSerializer

    def get_queryset(self):
        return RecommendationRun.objects.filter(
            user=self.request.user
        ).order_by('-triggered_at')

    @action(detail=False, methods=['post'])
    def trigger(self, request):
        from fragrance.tasks import monthly_fragrance_run
        run = RecommendationRun.objects.create(user=request.user, status='pending')
        task = monthly_fragrance_run.delay(user_id=request.user.id, run_id=run.id)
        run.celery_task_id = task.id
        run.save(update_fields=['celery_task_id'])
        return Response({'run_id': run.id}, status=status.HTTP_202_ACCEPTED)
