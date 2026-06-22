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

from .llm import generate_email_content, generate_preference_profile, select_candidates
from .models import Fragrance, FragranceConfig, PreferenceProfile, RecommendationRun
from .search import run_discovery_searches, verify_candidates


def render_and_send_email(user_id: int, run_id: int) -> None:
    """Pipeline step 6. Renders the email HTML and sends via the centralized sender."""
    run = (
        RecommendationRun.objects
        .select_related('profile')
        .prefetch_related('picks')
        .get(id=run_id)
    )
    config = FragranceConfig.objects.get(user_id=user_id)

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
    run.save(update_fields=['sent_at'])


def _resolve_profile(user_id: int, run_id: int) -> PreferenceProfile:
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
        return generate_preference_profile(user_id=user_id, run_id=run_id)

    # Reuse the existing profile but still link it to this run.
    RecommendationRun.objects.filter(id=run_id).update(profile=latest_profile)
    return latest_profile


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
        profile = _resolve_profile(user_id=user_id, run_id=run.id)
        results = run_discovery_searches(user_id=user_id, profile_id=profile.id)
        candidates = select_candidates(
            user_id=user_id,
            profile_id=profile.id,
            search_results=results,
        )
        picks = verify_candidates(
            user_id=user_id,
            run_id=run.id,
            candidates=candidates,
            profile=profile,
        )
        generate_email_content(run_id=run.id, verified_picks=picks)
        render_and_send_email(user_id=user_id, run_id=run.id)
        run.status = 'done'
        run.save(update_fields=['status'])
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            run.status = 'failed'
            run.error_message = str(exc)
            run.save(update_fields=['status', 'error_message'])
            raise
        raise self.retry(exc=exc, countdown=300)
