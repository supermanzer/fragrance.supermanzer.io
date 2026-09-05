from django.contrib.auth.models import User
from django.db import IntegrityError, models, transaction

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
