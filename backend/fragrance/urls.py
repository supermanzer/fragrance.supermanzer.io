from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    BetaAcceptView,
    BetaSignupView,
    BillingChangePlanView,
    BillingCheckoutView,
    BillingPortalView,
    BillingSessionView,
    BillingSubscriptionView,
    ExportCollectionView,
    FragranceConfigView,
    FragranceViewSet,
    ImportCollectionView,
    PreferenceProfileHistoryView,
    PreferenceProfileView,
    RecommendationConfirmView,
    RecommendationListView,
    RecommendationRateView,
    RecommendationRunViewSet,
    stripe_webhook,
)

router = DefaultRouter()
router.register('collection', FragranceViewSet, basename='fragrance')
router.register('runs', RecommendationRunViewSet, basename='run')

urlpatterns = router.urls + [
    path('beta/signup/', BetaSignupView.as_view(), name='beta-signup'),
    path('beta/accept/', BetaAcceptView.as_view(), name='beta-accept'),
    path('export/', ExportCollectionView.as_view(), name='export-collection'),
    path('import/', ImportCollectionView.as_view(), name='import-collection'),
    path('config/', FragranceConfigView.as_view(), name='fragrance-config'),
    path('profile/', PreferenceProfileView.as_view(), name='preference-profile'),
    path('profile/history/', PreferenceProfileHistoryView.as_view(), name='preference-profile-history'),
    path('recommendations/', RecommendationListView.as_view(), name='recommendations'),
    path('recommendations/confirm/', RecommendationConfirmView.as_view(), name='recommendation-confirm'),
    path('recommendations/<int:pk>/rate/', RecommendationRateView.as_view(), name='recommendation-rate'),
    path('billing/checkout/', BillingCheckoutView.as_view(), name='billing-checkout'),
    path('billing/portal/', BillingPortalView.as_view(), name='billing-portal'),
    path('billing/change-plan/', BillingChangePlanView.as_view(), name='billing-change-plan'),
    path('billing/subscription/', BillingSubscriptionView.as_view(), name='billing-subscription'),
    path('billing/session/<str:session_id>/', BillingSessionView.as_view(), name='billing-session'),
    # Both spellings route to the same view: CommonMiddleware + APPEND_SLASH
    # would otherwise 301 a slashless POST, and Stripe treats a redirect
    # response to a webhook delivery as a failure.
    path('billing/webhook/stripe/', stripe_webhook, name='stripe-webhook'),
    path('billing/webhook/stripe', stripe_webhook, name='stripe-webhook-noslash'),
]
