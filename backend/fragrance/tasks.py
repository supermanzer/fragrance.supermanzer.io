"""
fragrance/tasks.py

Implements Celery tasks for Fragrance Recommendation Runs
"""

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMessage
from django.db.models import Max
from django.template.loader import render_to_string
from django.utils import timezone

from .ai_providers import registry
from .ai_providers.base import StructuredLLMProvider, sanitize_sdk_error_message
from .ai_providers.registry import (
    EnumEnforcementUnsupportedError,
    MissingCredentialError,
    NoActiveAIModelConfigError,
)
from .llm import generate_email_content, generate_preference_profile, select_candidates
from .models import Fragrance, FragranceConfig, PreferenceProfile, RecommendationRun
from .search import run_discovery_searches, verify_candidates
from .services import generate_recommendation_token

RATING_ACTIONS = ('own', 'like', 'dislike')

# Configuration errors: retrying on the same backoff as transient
# network/API errors cannot fix a missing AIModelConfig row, a missing API
# key, or an active model that can't grammar-enforce enum — so these fail
# the run on the first attempt instead of after 2 retries x 300s (up to 10
# minutes of a config error being invisible in run.error_message).
NON_RETRYABLE_CONFIG_EXCEPTIONS = (
    NoActiveAIModelConfigError,
    MissingCredentialError,
    EnumEnforcementUnsupportedError,
)


def render_and_send_email(user_id: int, run_id: int) -> None:
    """Pipeline step 6. Renders the email HTML and sends via the centralized sender."""
    run = (
        RecommendationRun.objects
        .select_related('profile')
        .prefetch_related('picks')
        .get(id=run_id)
    )
    config = FragranceConfig.objects.get(user_id=user_id)

    # Attach ad-hoc confirm_url_* attributes directly onto the prefetched pick instances
    # so the template can reference pick.confirm_url_own/_like/_dislike without a second
    # query or a parallel context structure. Only 'confirmed' picks are rated from the
    # email — 'replaced' picks are never shown, so they get no tokens.
    for pick in run.picks.all():
        if pick.status != 'confirmed':
            continue
        for rating_action in RATING_ACTIONS:
            token = generate_recommendation_token(recommendation=pick, action=rating_action)
            setattr(
                pick,
                f'confirm_url_{rating_action}',
                f'{settings.FRONTEND_URL}/recommendations/confirm?t={token}',
            )

    html = render_to_string(
        template_name='fragrance/recommendation_email.html',
        context={'run': run},
    )
    run.email_html = html
    run.save(update_fields=['email_html'])

    email = EmailMessage(
        subject=f'Fragrance Picks — {run.triggered_at.strftime("%B %Y")}',
        body=html,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[config.recipient_email],
    )
    email.content_subtype = 'html'
    email.send()

    run.sent_at = timezone.now()
    run.email_status = 'sent'
    run.save(update_fields=['sent_at', 'email_status'])


def _resolve_profile(
    user_id: int, run_id: int, provider: StructuredLLMProvider
) -> PreferenceProfile:
    """
    Return the profile to use for this run. Generates a new one via LLM only when the
    user's fragrance collection has changed since the last profile was produced; otherwise
    reuses the latest existing profile to avoid an unnecessary LLM call.
    """
    latest_change = (
        Fragrance.objects
        .filter(user_id=user_id)
        .aggregate(Max('updated_at'))['updated_at__max']
    )
    latest_profile = (
        PreferenceProfile.objects
        .filter(user_id=user_id)
        .order_by('-generated_at')
        .first()
    )

    needs_new_profile = (
        latest_profile is None
        or latest_change is None
        or latest_change > latest_profile.generated_at
    )

    if needs_new_profile:
        return generate_preference_profile(
            user_id=user_id, run_id=run_id, provider=provider
        )

    # Reuse the existing profile but still link it to this run.
    RecommendationRun.objects.filter(id=run_id).update(profile=latest_profile)
    return latest_profile


@shared_task(bind=True, max_retries=5)
def send_recommendation_email(self: 'send_recommendation_email', user_id: int, run_id: int) -> None:
    """Sends the recommendation email for a completed run. Retried independently of generation."""
    try:
        render_and_send_email(user_id=user_id, run_id=run_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            RecommendationRun.objects.filter(id=run_id).update(
                email_status='failed',
                error_message=f"Email delivery failed after {self.max_retries} retries: {exc}",
            )
            raise
        raise self.retry(exc=exc, countdown=300)


@shared_task(bind=True, max_retries=2)
def monthly_fragrance_run(self: 'monthly_fragrance_run', user_id: int, run_id: int | None = None) -> None:
    from .models import Recommendation
    if run_id is None:
        run = RecommendationRun.objects.create(user_id=user_id, status='running')
    else:
        run = RecommendationRun.objects.get(id=run_id)
        run.status = 'running'
        run.error_message = ''
        run.save(update_fields=['status', 'error_message'])
        # Wipe any picks from a previous attempt so retries start clean.
        Recommendation.objects.filter(run=run).delete()
    try:
        # Resolved once per run, not once per call: a run must not straddle two
        # providers if an admin flips AIModelConfig.is_active mid-run. Resolved
        # inside this try so a misconfigured/unverified active model (no active
        # row, missing API key, or a model that can't grammar-enforce enum for
        # the email-content call) lands in run.error_message via the except
        # block below, instead of raising before the run row is even 'running'.
        config = registry.get_active_config(enum_required=True)
        provider = registry.build_provider(config=config)
        run.provider_family = config.family
        run.provider_model_id = config.model_id
        run.save(update_fields=['provider_family', 'provider_model_id'])

        profile = _resolve_profile(user_id=user_id, run_id=run.id, provider=provider)
        results = run_discovery_searches(user_id=user_id, profile_id=profile.id)
        candidates = select_candidates(
            user_id=user_id,
            profile_id=profile.id,
            search_results=results,
            provider=provider,
        )
        picks = verify_candidates(
            user_id=user_id,
            run_id=run.id,
            candidates=candidates,
            profile=profile,
            provider=provider,
        )
        generate_email_content(run_id=run.id, verified_picks=picks, provider=provider)
        run.status = 'done'
        run.email_status = 'pending'
        run.save(update_fields=['status', 'email_status'])
        send_recommendation_email.delay(user_id=user_id, run_id=run.id)
    except Exception as exc:
        # Config errors fail immediately, on any attempt — retrying can't fix
        # them. Everything else keeps the existing retry-then-fail behavior.
        # Either way, the message that lands in run.error_message (served to
        # the frontend via the authenticated /runs/ API) is sanitized: a raw
        # str(exc) on an OpenAI/Anthropic SDK APIError can otherwise carry a
        # vendor-masked key fragment.
        is_config_error = isinstance(exc, NON_RETRYABLE_CONFIG_EXCEPTIONS)
        is_final_attempt = self.request.retries >= self.max_retries
        if is_config_error or is_final_attempt:
            run.status = 'failed'
            run.error_message = sanitize_sdk_error_message(exc)
            run.save(update_fields=['status', 'error_message'])
            raise
        raise self.retry(exc=exc, countdown=300)
