"""
fragrance/tasks.py

Implements Celery tasks for Fragrance Recommendation Runs
"""

import logging
from datetime import timedelta

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMessage, EmailMultiAlternatives, send_mail
from django.db.models import Max
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .ai_providers import registry
from .ai_providers.base import StructuredLLMProvider, sanitize_sdk_error_message
from .ai_providers.registry import (
    EnumEnforcementUnsupportedError,
    MissingCredentialError,
    NoActiveAIModelConfigError,
)
from .llm import generate_email_content, generate_preference_profile, select_candidates
from .models import (
    BetaSignup,
    Fragrance,
    FragranceConfig,
    PreferenceProfile,
    RecommendationRun,
    StripeEvent,
    Subscription,
)
from .search import run_discovery_searches, verify_candidates
from .permissions import SubscriptionRequired
from .services import (
    QuotaExceeded,
    RunInProgress,
    generate_beta_invite_token,
    IN_FLIGHT_MAX_AGE,
    generate_recommendation_token,
    get_or_create_fragrance_config,
    start_recommendation_run,
)


logger = logging.getLogger(__name__)


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


RATING_ACTIONS = ('own', 'like', 'dislike')


STALE_RUN_ERROR_MESSAGE = "Run did not finish (worker restarted or timed out)."


STALE_RUN_TIMEOUT_MESSAGE = "Run took too long and was stopped."


STRIPE_EVENT_PAYLOAD_RETENTION_DAYS = 90


# The only event types the endpoint is configured for in the Stripe
# Dashboard -- kept here, not in views.stripe_webhook, since this is the
# point that decides whether to act, not just whether to store. Anything
# else that reaches this task (a Dashboard misconfiguration, or an event
# type added there without a matching handler) is a documented no-op, not
# an error: the StripeEvent row is still marked 'processed'.
STRIPE_HANDLED_EVENT_TYPES = {
    "payment_intent.succeeded",
    "charge.dispute.created",
    "customer.updated",
}


# Seconds before each retry when Stripe's invoice hasn't settled yet: this is a
# finalization race that clears in seconds, so it must not wait the generic 300s.
# Used only by process_stripe_event.
STRIPE_UNSETTLED_RETRY_COUNTDOWNS = (30, 60, 120)


@shared_task
def flush_expired_jwt_tokens() -> None:
    """
    The token_blacklist tables grow ~2.3 kB per refresh and nothing else
    prunes them; AtomicTokenRefreshView row-locks that table on every refresh.
    """
    from django.core.management import call_command

    call_command('flushexpiredtokens')


@shared_task(bind=True, max_retries=2, soft_time_limit=1200, time_limit=1500)
def monthly_fragrance_run(self: 'monthly_fragrance_run', user_id: int, run_id: int | None = None) -> None:
    from .models import Recommendation
    if run_id is None:
        # Beat path. The API view path arrives with a run_id its own call to
        # start_recommendation_run already created (and counted); calling the
        # chokepoint again there would double-count it and trip the in-flight
        # guard on its own pending run.
        user = User.objects.filter(pk=user_id).first()
        if user is None:
            logger.info("scheduled run skipped: user %s no longer exists", user_id)
            return
        try:
            run = start_recommendation_run(user=user, source='beat')
        except (SubscriptionRequired, QuotaExceeded, RunInProgress) as refusal:
            # A lapsed or exhausted user's scheduled run is normal, not an error.
            logger.info(
                "scheduled run skipped for user %s: %s", user_id, type(refusal).__name__
            )
            return
        # Django noise, not a Stripe params-shape issue: RecommendationRun's
        # CharField/TextField attributes read to ty as their descriptor
        # class rather than plain str (no django-stubs installed -- see
        # [tool.ty] in pyproject.toml). Every `run.<field> = <str literal>`
        # assignment in this function carries the same, individually
        # reviewed exception.
        run.status = 'running'  # ty: ignore[invalid-assignment]
        run.save(update_fields=['status'])
    else:
        # Not ignored: .objects.get(...)'s return type is unresolved-attribute
        # (Unknown) here too, but ty doesn't flag this specific pair of
        # assignments as invalid-assignment as it does on the create() path
        # a few lines up -- an inference-order quirk, not a real difference.
        run = RecommendationRun.objects.get(id=run_id)  # ty: ignore[unresolved-attribute]
        run.status = 'running'
        run.error_message = ''
        run.save(update_fields=['status', 'error_message'])
        # Wipe any picks from a previous attempt so retries start clean.
        Recommendation.objects.filter(run=run).delete()  # ty: ignore[unresolved-attribute]
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

        profile = _resolve_profile(user_id=user_id, run_id=run.id, provider=provider)  # ty: ignore[unresolved-attribute]
        results = run_discovery_searches(user_id=user_id, profile_id=profile.id)  # ty: ignore[unresolved-attribute]
        candidates = select_candidates(
            user_id=user_id,
            profile_id=profile.id,  # ty: ignore[unresolved-attribute]
            search_results=results,
            provider=provider,
        )
        picks = verify_candidates(
            user_id=user_id,
            run_id=run.id,  # ty: ignore[unresolved-attribute]
            candidates=candidates,
            profile=profile,
            provider=provider,
        )
        generate_email_content(run_id=run.id, verified_picks=picks, provider=provider)  # ty: ignore[unresolved-attribute]
        run.status = 'done'  # ty: ignore[invalid-assignment]
        run.email_status = 'pending'  # ty: ignore[invalid-assignment]
        run.save(update_fields=['status', 'email_status'])
        send_recommendation_email.delay(user_id=user_id, run_id=run.id)  # ty: ignore[unresolved-attribute]
    except SoftTimeLimitExceeded:
        # A run that overran its budget is a failure, not a transient error:
        # retrying would just burn another 20 minutes.
        run.status = 'failed'  # ty: ignore[invalid-assignment]
        run.error_message = STALE_RUN_TIMEOUT_MESSAGE  # ty: ignore[invalid-assignment]
        run.save(update_fields=['status', 'error_message'])
        logger.error("run %s for user %s exceeded its soft time limit", run.id, user_id)  # ty: ignore[unresolved-attribute]
        return
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
            run.status = 'failed'  # ty: ignore[invalid-assignment]
            run.error_message = sanitize_sdk_error_message(exc)  # ty: ignore[invalid-assignment]
            run.save(update_fields=['status', 'error_message'])
            raise
        # Retry against the run we already own: re-entering the beat path would
        # be refused by the in-flight guard and strand this run as 'running'.
        raise self.retry(
            exc=exc,
            countdown=300,
            kwargs={'user_id': user_id, 'run_id': run.id},  # ty: ignore[unresolved-attribute]
        )


@shared_task(bind=True, max_retries=3)
def process_stripe_event(self: 'process_stripe_event', stripe_event_id: str) -> None:
    """
    Does the real Stripe work views.stripe_webhook deliberately doesn't:
    fulfillment (steps 4-6 of the payment_intent.succeeded validation chain)
    is two to three outbound Stripe calls, and the webhook view must return
    200 within Checkout's ~10s redirect window regardless. celery-worker has
    public egress; celery-beat does not, which is why this task is only ever
    reached via .delay() from the webhook view, never scheduled.

    Reads the event from the already-persisted StripeEvent row rather than
    a directly-passed payload, so retries replay from committed state
    instead of a stale in-memory dict.
    """
    from .billing import (
        InvoiceNotSettledError,
        fulfill_payment_intent,
        revoke_for_dispute,
        sync_customer_email,
    )

    try:
        event_row = StripeEvent.objects.get(stripe_event_id=stripe_event_id)  # ty: ignore[unresolved-attribute]
    except StripeEvent.DoesNotExist:  # ty: ignore[unresolved-attribute]
        return

    data_object = event_row.payload.get('data', {}).get('object', {})

    try:
        if event_row.event_type == 'payment_intent.succeeded':
            fulfill_payment_intent(payment_intent_id=data_object.get('id', ''))
        elif event_row.event_type == 'charge.dispute.created':
            revoke_for_dispute(dispute=data_object)
        elif event_row.event_type == 'customer.updated':
            sync_customer_email(
                customer=data_object,
                previous_attributes=event_row.payload.get('data', {}).get('previous_attributes'),
            )
        elif event_row.event_type not in STRIPE_HANDLED_EVENT_TYPES:
            pass  # unsubscribed event type reached the endpoint anyway -- ignored, not an error
        event_row.status = 'processed'
        event_row.processed_at = timezone.now()
        event_row.error = ''
        event_row.save(update_fields=['status', 'processed_at', 'error'])
    except Exception as exc:
        event_row.status = 'failed'
        event_row.error = sanitize_sdk_error_message(exc)
        event_row.save(update_fields=['status', 'error'])
        if self.request.retries >= self.max_retries:
            raise
        if isinstance(exc, InvoiceNotSettledError):
            countdown = STRIPE_UNSETTLED_RETRY_COUNTDOWNS[
                min(self.request.retries, len(STRIPE_UNSETTLED_RETRY_COUNTDOWNS) - 1)
            ]
        else:
            countdown = 300
        raise self.retry(exc=exc, countdown=countdown)


@shared_task
def prune_stripe_event_payloads() -> int:
    """
    Empties StripeEvent.payload after 90 days but keeps the row:
    stripe_event_id is the replay guard and must outlive Stripe's 3-day retry
    and 30-day manual-resend windows. The column is a non-null JSONField, so
    "nulled" means reset to {} (which reconciliation already treats as
    unreplayable).
    """
    cutoff = timezone.now() - timedelta(days=STRIPE_EVENT_PAYLOAD_RETENTION_DAYS)
    pruned = (
        StripeEvent.objects.filter(received_at__lt=cutoff)  # ty: ignore[unresolved-attribute]
        .exclude(payload={})
        .update(payload={})
    )
    logger.info('pruned payload from %d StripeEvent rows older than %d days', pruned, STRIPE_EVENT_PAYLOAD_RETENTION_DAYS)
    return pruned


@shared_task
def reap_stale_runs() -> int:
    """
    Fails pending/running runs older than IN_FLIGHT_MAX_AGE. An early-ack
    worker that dies mid-run strands its row; a failed run does not burn
    quota, so this also releases the allowance the stranded row was holding.
    """
    cutoff = timezone.now() - IN_FLIGHT_MAX_AGE
    reaped = RecommendationRun.objects.filter(  # ty: ignore[unresolved-attribute]
        status__in=('pending', 'running'), triggered_at__lt=cutoff
    ).update(status='failed', error_message=STALE_RUN_ERROR_MESSAGE)
    if reaped:
        logger.warning("reaped %d stale in-flight run(s)", reaped)
    return reaped


@shared_task(soft_time_limit=240, time_limit=300)
def reconcile_stripe_subscriptions() -> dict:
    """
    Nightly backstop for lost Stripe webhooks. Runs in celery-worker (public
    egress); celery-beat only dispatches it.
    """
    from .billing import reconcile_subscriptions

    return reconcile_subscriptions(dry_run=False)


def render_and_send_email(user_id: int, run_id: int) -> None:
    """Pipeline step 6. Renders the email HTML and sends via the centralized sender."""
    run = (
        RecommendationRun.objects  # ty: ignore[unresolved-attribute]
        .select_related('profile')
        .prefetch_related('picks')
        .get(id=run_id)
    )
    user = User.objects.get(pk=user_id)
    config = FragranceConfig.objects.filter(user_id=user_id).first()  # ty: ignore[unresolved-attribute]
    if config is None:
        logger.warning('user %s has no FragranceConfig at send time; creating it', user_id)
        config = get_or_create_fragrance_config(user=user)
    recipient = config.recipient_email
    if not recipient:
        logger.warning('user %s has a blank recipient_email; delivering to the account email', user_id)
        recipient = user.email
    if not recipient:
        raise ValueError(f'user {user_id} has no delivery address (blank recipient_email and account email)')

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
        to=[recipient],
    )
    email.content_subtype = 'html'
    email.send()

    run.sent_at = timezone.now()
    run.email_status = 'sent'
    run.save(update_fields=['sent_at', 'email_status'])


@shared_task(bind=True, max_retries=5)
def restore_stripe_customer_email(
    self: 'restore_stripe_customer_email', user_id: int, customer_id: str, email: str
) -> None:
    """
    Pushes the address restored by billing.revert_customer_email back onto the
    Stripe Customer, so Stripe stops holding the attacker's address. Safe to
    retry or replay: it does nothing if the customer already has the address.
    Unlike the notice tasks this deliberately re-reads the DB at run time: a
    later legitimate email change must never be overwritten with a stale
    address, so it skips unless User.email still equals `email` and the
    customer is still this user's Subscription customer. Logs carry ids only.
    """
    from .billing import _normalized_email, get_stripe_client

    current = User.objects.filter(pk=user_id).values_list('email', flat=True).first()
    if current is None or _normalized_email(email=current) != _normalized_email(email=email):
        logger.info('skipping stripe email restore for user %s: local email changed since the revert', user_id)
        return
    if not Subscription.objects.filter(user_id=user_id, stripe_customer_id=customer_id).exists():  # ty: ignore[unresolved-attribute]
        logger.warning('skipping stripe email restore for user %s: customer %s is not theirs', user_id, customer_id)
        return
    try:
        client = get_stripe_client()
        customer = client.v1.customers.retrieve(customer_id)
        if _normalized_email(email=getattr(customer, 'email', None)) == _normalized_email(email=email):
            logger.info('stripe customer %s already has the restored email for user %s', customer_id, user_id)
            return
        client.v1.customers.update(customer_id, params={'email': email})
        logger.info('restored email on stripe customer %s for user %s', customer_id, user_id)
    except Exception as exc:
        logger.error(
            'could not restore email on stripe customer %s for user %s: %s',
            customer_id,
            user_id,
            sanitize_sdk_error_message(exc),
        )
        if self.request.retries >= self.max_retries:
            raise
        raise self.retry(exc=exc, countdown=300)


@shared_task(bind=True, max_retries=5)
def send_account_email_change_notice(
    self: 'send_account_email_change_notice',
    user_id: int,
    recipient: str,
    old_email: str,
    new_email: str,
    is_new_address: bool,
    revert_token: str = '',
) -> None:
    """
    One half of the notice sent after billing.sync_customer_email changes
    User.email: queued once for the old address (a warning) and once for the
    new (a confirmation), so a failed delivery to one retries without
    re-sending the other. Addresses are passed in rather than re-read: the
    task must describe the change that was made, even if the row changes
    again before the worker runs. Only the old-address warning carries a
    revert_token (rendered as a link to the frontend's revert page, which POSTs
    it); the new-address confirmation never does, and the token is never logged.
    """
    username = User.objects.filter(pk=user_id).values_list('username', flat=True).first()
    if username is None:
        return
    context = {
        'username': username,
        'old_email': old_email,
        'new_email': new_email,
        'is_new_address': is_new_address,
        'support_email': settings.SUPPORT_EMAIL,
        'revert_url': (
            f"{settings.FRONTEND_URL}/auth/revert-email?token={revert_token}"
            if revert_token and not is_new_address
            else ''
        ),
    }
    try:
        message = EmailMultiAlternatives(
            subject='Your Fragrances account email was changed',
            body=render_to_string(template_name='fragrance/email_changed_notice.txt', context=context),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
        )
        message.attach_alternative(
            render_to_string(template_name='fragrance/email_changed_notice.html', context=context),
            'text/html',
        )
        message.send(fail_silently=False)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            raise
        raise self.retry(exc=exc, countdown=300)


@shared_task(bind=True, max_retries=5)
def send_beta_invite_email(self: 'send_beta_invite_email', signup_id: int) -> None:
    """
    Sends the beta-invite email for an approved signup. Queued from the admin
    approve action via transaction.on_commit rather than called synchronously
    -- a Postmark outage must not 500 the admin approval request the way
    PasswordResetRequestView's inline, fail_silently=False send would.
    """
    signup = BetaSignup.objects.select_related('invited_user').get(id=signup_id)  # ty: ignore[unresolved-attribute]
    if signup.invited_user is None:
        return
    token = generate_beta_invite_token(signup=signup)
    accept_url = f"{settings.FRONTEND_URL}/beta/accept?token={token}"
    try:
        send_mail(
            subject="You're in — set up your Fragrances account",
            message=render_to_string(
                template_name='fragrance/beta_invite_email.txt',
                context={'accept_url': accept_url},
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[signup.email],
            fail_silently=False,
        )
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            raise
        raise self.retry(exc=exc, countdown=300)


@shared_task(bind=True, max_retries=5)
def send_email_reverted_notice(self: 'send_email_reverted_notice', user_id: int, recipient: str) -> None:
    """
    Sent to the restored address after billing.revert_customer_email: tells the
    owner the email was put back, all sessions were signed out, and to reset
    the password now. Carries no token; the only link is the plain
    forgot-password page.
    """
    username = User.objects.filter(pk=user_id).values_list('username', flat=True).first()
    if username is None:
        return
    context = {
        'username': username,
        'forgot_password_url': f"{settings.FRONTEND_URL}/auth/forgot-password",
        'support_email': settings.SUPPORT_EMAIL,
    }
    try:
        message = EmailMultiAlternatives(
            subject='Your Fragrances account email was restored',
            body=render_to_string(template_name='fragrance/email_reverted_notice.txt', context=context),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
        )
        message.attach_alternative(
            render_to_string(template_name='fragrance/email_reverted_notice.html', context=context),
            'text/html',
        )
        message.send(fail_silently=False)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            raise
        raise self.retry(exc=exc, countdown=300)


@shared_task(bind=True, max_retries=5)
def send_password_reset_email(self: 'send_password_reset_email', user_id: int) -> None:
    """
    Sends one password-reset email. Takes the user id rather than a token:
    uid and token are generated here so the reset credential is never
    serialized into the Redis broker. Queued by PasswordResetRequestView so
    that view's latency and failure modes don't depend on whether an account
    matched (an account-enumeration oracle).
    """
    user = User.objects.filter(pk=user_id).first()
    if user is None or not user.email:
        return
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user=user)
    reset_url = f"{settings.FRONTEND_URL}/auth/reset-password?uid={uid}&token={token}"
    try:
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
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            raise
        raise self.retry(exc=exc, countdown=60 * 2 ** self.request.retries)


@shared_task(bind=True, max_retries=5)
def send_recommendation_email(self: 'send_recommendation_email', user_id: int, run_id: int) -> None:
    """Sends the recommendation email for a completed run. Retried independently of generation."""
    try:
        render_and_send_email(user_id=user_id, run_id=run_id)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            RecommendationRun.objects.filter(id=run_id).update(  # ty: ignore[unresolved-attribute]
                email_status='failed',
                error_message=f"Email delivery failed after {self.max_retries} retries: {exc}",
            )
            raise
        raise self.retry(exc=exc, countdown=300)


def _resolve_profile(
    user_id: int, run_id: int, provider: StructuredLLMProvider
) -> PreferenceProfile:
    """
    Return the profile to use for this run. Generates a new one via LLM only when the
    user's fragrance collection has changed since the last profile was produced; otherwise
    reuses the latest existing profile to avoid an unnecessary LLM call.
    """
    latest_change = (
        Fragrance.objects  # ty: ignore[unresolved-attribute]
        .filter(user_id=user_id)
        .aggregate(Max('updated_at'))['updated_at__max']
    )
    latest_profile = (
        PreferenceProfile.objects  # ty: ignore[unresolved-attribute]
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
    RecommendationRun.objects.filter(id=run_id).update(profile=latest_profile)  # ty: ignore[unresolved-attribute]
    return latest_profile
