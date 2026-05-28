from django.contrib.auth.models import User
from django.db import models
from encrypted_model_fields.fields import EncryptedTextField

# Constants
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


# Create your models here.
class FragranceConfig(models.Model):
    """Per-user configuration.  One config per user"""

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="fragrance_config"
    )
    recipient_email = models.EmailField(
        help_text="The email we will deliver fragrance recommendations to"
    )
    gmail_user = models.EmailField()
    gmail_app_password_enc = EncryptedTextField()  # encrypted at rest
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
