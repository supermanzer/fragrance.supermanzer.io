from django.contrib.auth.models import User
from django.db import IntegrityError, models, transaction
from django.utils import timezone

# Constants
AI_PROVIDER_FAMILIES = [
    ("anthropic", "Anthropic"),
    ("openai", "OpenAI"),
    ("qwen", "Qwen"),
]
FREQUENCIES = [
    ("weekly", "Weekly"),
    ("monthly", "Monthly"),
    ("yearly", "Yearly"),
]
FRAGRANCE_STATUSES = [
    ("own", "Own"),
    ("like", "Like"),
    ("dislike", "Don't Like"),
]
PICK_STATUSES = [("confirmed", "Confirmed"), ("replaced", "Replaced")]
RUN_STATUSES = [
    ("pending", "Pending"),
    ("running", "Running"),
    ("done", "Done"),
    ("failed", "Failed"),
]
EMAIL_STATUSES = [
    ("pending", "Pending"),
    ("sent", "Sent"),
    ("failed", "Failed"),
]
BETA_SIGNUP_STATUSES = [
    ("pending", "Pending"),
    ("approved", "Approved"),
    ("converted", "Converted"),
    ("rejected", "Rejected"),
]
BILLING_INTERVALS = [
    ("month", "Month"),
    ("year", "Year"),
]
SUBSCRIPTION_STATUSES = [
    ("none", "None"),
    ("checkout_started", "Checkout Started"),
    ("active", "Active"),
    ("revoked", "Revoked"),
]
STRIPE_EVENT_STATUSES = [
    ("received", "Received"),
    ("processed", "Processed"),
    ("failed", "Failed"),
]


# Create your models here.
class FragranceConfig(models.Model):
    """Per-user configuration.  One config per user"""

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="fragrance_config"
    )
    recipient_email = models.EmailField(
        help_text="The email we will deliver fragrance recommendations to"
    )
    # Fragrance Recommendation Schedule
    frequency = models.CharField(
        max_length=10, choices=FREQUENCIES, default="monthly"
    )
    run_hour = models.PositiveIntegerField(default=9)
    run_day_of_week = models.PositiveIntegerField(
        default=1
    )  # Only weekly (0=Sun, 1=Mon, etc)
    run_day_of_month = models.PositiveIntegerField(default=1)
    run_month = models.PositiveBigIntegerField(default=1)


class Fragrance(models.Model):
    """
    The user's fragrane collection, managed directly through the app.

    This is the authoritative source for preference analysis
    """

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="fragrances"
    )
    name = models.CharField(max_length=255)
    house = models.CharField(max_length=255)
    status = models.CharField(max_length=10, choices=FRAGRANCE_STATUSES)
    notes = models.TextField(
        blank=True
    )  # Users personal notes "I like it because..."
    added_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    source_recommendation = models.OneToOneField(
        "Recommendation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="promoted_fragrance",
    )  # if a user records a fragrance from a recommendation, linking the recommendation record it came from

    class Meta:
        unique_together = [["user", "name"]]


class PreferenceProfile(models.Model):
    """
    LLM-generated tase profile, stored per run so users can see how their profile has evovled over time
    """

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="preference_profiles"
    )
    generated_at = models.DateTimeField(auto_now_add=True)
    loved_notes = models.TextField()
    liked_notes = models.TextField()
    disliked_notes = models.TextField()
    owns_list = models.JSONField(default=list)
    search_angle_1 = models.CharField(max_length=500)
    search_angle_2 = models.CharField(max_length=500)


class RecommendationRun(models.Model):
    """Executed recommendation generation event."""

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="recommendation_runs"
    )
    triggered_at = models.DateTimeField(auto_now_add=True)
    profile = models.ForeignKey(
        PreferenceProfile, on_delete=models.SET_NULL, null=True
    )
    email_html = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=10, choices=RUN_STATUSES, default="pending"
    )
    celery_task_id = models.CharField(max_length=255, blank=True)
    error_message = models.TextField(blank=True)
    intro = models.TextField(blank=True)  # LLM-generated personalized intro for the email
    email_status = models.CharField(
        max_length=10, choices=EMAIL_STATUSES, null=True, blank=True, default=None
    )
    # Snapshot of the AIModelConfig resolved at run start — plain fields, not a
    # FK, so a run's provenance survives even if the AIModelConfig row it was
    # resolved from is later edited or deleted.
    provider_family = models.CharField(
        max_length=20, choices=AI_PROVIDER_FAMILIES, blank=True
    )
    provider_model_id = models.CharField(max_length=255, blank=True)


class Recommendation(models.Model):
    """
    A single frangrance pick from a single run.
    Serves as a permanent degdupilication guard and can be used to generate Fragrance records if the user tries a recommendation and provides feedback.
    """

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="recommendations"
    )
    run = models.ForeignKey(
        RecommendationRun, on_delete=models.CASCADE, related_name="picks"
    )
    name = models.CharField(max_length=255)
    house = models.CharField(max_length=255)
    status = models.CharField(
        max_length=10, choices=PICK_STATUSES, default="confirmed"
    )
    rationale = models.TextField()  # LLM generated: why was this recommended?
    search_source_url = models.URLField(blank=True)


class BetaSignup(models.Model):
    """
    A waitlist row, created before any User exists. This is triage, not
    authorization -- manual admin approval (a later slice) is the real gate.
    `normalized_email` (not `email`) carries the uniqueness constraint, since
    a raw-email unique index does nothing against `+tag` or Gmail-dot abuse;
    see services.normalize_email for the normalization rule it depends on.
    """

    email = models.EmailField()
    normalized_email = models.EmailField(unique=True)
    status = models.CharField(
        max_length=10, choices=BETA_SIGNUP_STATUSES, default="pending"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_beta_signups",
    )
    invited_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="beta_signups",
    )
    # HMAC of the submitting client's IP, salted with SECRET_KEY -- never the
    # raw address. Enough to correlate abuse across signups without storing PII.
    signup_ip_hash = models.CharField(max_length=64, blank=True)
    converted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.email} ({self.status})"


class Plan(models.Model):
    """
    Billing terms and usage terms, deliberately separate. `billing_interval`
    and `access_days_granted` describe how the customer is charged;
    `quota_period_days` and `max_runs_per_quota_period` describe how often
    the pipeline may run. They must stay separate fields: a single period
    reused for both would let an annual subscriber take twelve runs on the
    first of January. The quota chokepoint (a later slice) reads
    `quota_period_days` on every plan and must never look at
    `billing_interval`.
    """

    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    stripe_price_id = models.CharField(max_length=255, unique=True)
    amount_cents = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="usd")
    billing_interval = models.CharField(max_length=5, choices=BILLING_INTERVALS)
    # Fallback only for computing access_expires_at when Stripe's own
    # period_end is unavailable -- prefer the value Stripe supplies.
    access_days_granted = models.PositiveIntegerField()
    quota_period_days = models.PositiveIntegerField(default=30)
    max_runs_per_quota_period = models.PositiveIntegerField(default=1)
    failed_run_grace = models.PositiveIntegerField(default=1)
    # "Offered at checkout / plan-change target" -- never "honoured at
    # renewal". billing._match_plan_line matches ANY Plan row regardless of
    # this flag, on purpose: a retired row (see seed_plans --rotate-price)
    # must keep granting for subscribers still billing on its Price even
    # after it stops being offered here.
    is_active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"{self.name} ({self.slug})"


class Subscription(models.Model):
    """
    The authorization record -- one per User. Access is granted only by
    `has_active_access`, a timestamp check, never a boolean: nothing needs to
    handle cancellation, because a cancelled subscription simply stops
    producing payments and the window expires on its own.

    The status set is deliberately small: there is no `past_due`,
    `trialing`, or `canceled` to mirror -- Stripe owns those. `revoked`
    exists solely for disputes, the one case where access must end before
    `access_expires_at`.
    """

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="subscription"
    )
    plan = models.ForeignKey(
        Plan, on_delete=models.SET_NULL, null=True, blank=True, related_name="subscriptions"
    )
    stripe_customer_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    stripe_subscription_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    status = models.CharField(max_length=20, choices=SUBSCRIPTION_STATUSES, default="none")
    access_expires_at = models.DateTimeField(null=True, blank=True)
    last_paid_at = models.DateTimeField(null=True, blank=True)
    last_checkout_session_id = models.CharField(max_length=255, blank=True)

    def __str__(self) -> str:
        return f"{self.user} ({self.status})"

    @property
    def has_active_access(self) -> bool:
        if self.access_expires_at is None:
            return False
        return self.status == "active" and self.access_expires_at > timezone.now()


class StripeEvent(models.Model):
    """
    Delivery-level idempotency: stops Stripe's duplicate *deliveries* of the
    same event id. This is distinct from ProcessedPayment, which stops a
    *period being granted twice* for the same money -- the webhook and the
    Checkout success page reach the same fulfillment function by different
    routes, and only one of them has an event id.
    """

    stripe_event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=255)
    stripe_created = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STRIPE_EVENT_STATUSES, default="received")
    error = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"{self.stripe_event_id} ({self.event_type}, {self.status})"


class ProcessedPayment(models.Model):
    """
    Grant-level idempotency, keyed on the Stripe **invoice** id, not the
    PaymentIntent id. One invoice is one billing period is one grant; a
    single PaymentIntent can settle several invoices, so keying on it would
    under-grant. The insert happens inside the same transaction that extends
    Subscription.access_expires_at, so a concurrent double-call loses the
    race on this unique constraint rather than granting two periods.
    """

    stripe_invoice_id = models.CharField(max_length=255, unique=True)
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True)
    stripe_subscription_id = models.CharField(max_length=255, blank=True)
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processed_payments",
    )
    amount_paid_cents = models.PositiveIntegerField()
    applied_at = models.DateTimeField(auto_now_add=True)
    period_end = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return self.stripe_invoice_id


class ActiveAIModelConfigConflictError(Exception):
    """
    Raised when two concurrent saves both try to make a different
    AIModelConfig row the active one. The DB-level partial unique index
    (`one_active_ai_model_config`) is the actual source of truth — this
    exists to translate the resulting IntegrityError into a message an admin
    can act on, since the raw constraint-violation text names an index, not
    a row.
    """


class AIModelConfig(models.Model):
    """
    Selectable (family, model_id) pair for the LLM pipeline, plus its proven
    structured-output capabilities. Holds selection only — never credentials;
    API keys stay in environment variables per family (see fragrance/ai_providers).

    Global configuration, not per-user: unlike every other model in this file,
    there is deliberately no `user` FK here. Which LLM the whole pipeline runs
    against is an operator/admin decision, not a per-tenant one.
    """

    family = models.CharField(max_length=20, choices=AI_PROVIDER_FAMILIES)
    model_id = models.CharField(max_length=255)
    max_tokens_default = models.PositiveIntegerField(default=1024)
    supports_strict_schema = models.BooleanField(default=False)
    supports_enum_enforcement = models.BooleanField(default=False)
    is_active = models.BooleanField(default=False)
    verified_at = models.DateTimeField(
        null=True, blank=True
    )  # set by the verify_ai_model conformance harness
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["family", "model_id"], name="unique_ai_model_config"
            ),
            # "At most one active row" enforced at the DB level, independent
            # of any transaction's stale snapshot. save() below only reduces
            # how often two admins actually collide with this constraint; it
            # cannot substitute for it — two transactions activating two
            # *different*, already-existing rows near-simultaneously never
            # contend for the same row lock, so app-level checks alone can't
            # serialize them (traced in .claude/reports/
            # 2026-08-30-ai-provider-abstraction-review.md, finding 5).
            models.UniqueConstraint(
                fields=["is_active"],
                condition=models.Q(is_active=True),
                name="one_active_ai_model_config",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.family}:{self.model_id}" + (" (active)" if self.is_active else "")

    def save(self, *args: object, **kwargs: object) -> None:
        # Deactivate other rows *before* activating self — the reverse of
        # this method's original order — so this transaction never holds two
        # is_active=True rows at once, even momentarily: the partial unique
        # index above is checked per-statement, not just at commit.
        if not self.is_active:
            super().save(*args, **kwargs)
            return
        with transaction.atomic():
            AIModelConfig.objects.filter(is_active=True).exclude(
                pk=self.pk
            ).update(is_active=False)
            try:
                super().save(*args, **kwargs)
            except IntegrityError as exc:
                raise ActiveAIModelConfigConflictError(
                    "Another admin just changed the active AI model at the "
                    "same time. Reload and try again."
                ) from exc
