from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    ExportCollectionView,
    FragranceConfigView,
    FragranceViewSet,
    ImportCollectionView,
    PreferenceProfileHistoryView,
    PreferenceProfileView,
    RecommendationListView,
    RecommendationRunViewSet,
)

router = DefaultRouter()
router.register('collection', FragranceViewSet, basename='fragrance')
router.register('runs', RecommendationRunViewSet, basename='run')

urlpatterns = router.urls + [
    path('export/', ExportCollectionView.as_view(), name='export-collection'),
    path('import/', ImportCollectionView.as_view(), name='import-collection'),
    path('config/', FragranceConfigView.as_view(), name='fragrance-config'),
    path('profile/', PreferenceProfileView.as_view(), name='preference-profile'),
    path('profile/history/', PreferenceProfileHistoryView.as_view(), name='preference-profile-history'),
    path('recommendations/', RecommendationListView.as_view(), name='recommendations'),
]
