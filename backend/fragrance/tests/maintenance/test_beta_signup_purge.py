from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from fragrance.models import BetaSignup
from fragrance.tasks import purge_rejected_beta_signups


def _signup(*, name: str, status: str, age_days: int) -> BetaSignup:
    signup = BetaSignup.objects.create(
        email=f"{name}@example.com", normalized_email=f"{name}@example.com", status=status
    )
    BetaSignup.objects.filter(pk=signup.pk).update(created_at=timezone.now() - timedelta(days=age_days))
    return signup


class PurgeRejectedBetaSignupsTests(TestCase):
    def test_deletes_only_rejected_rows_older_than_six_months(self) -> None:
        _signup(name="old_rejected", status="rejected", age_days=200)
        _signup(name="recent_rejected", status="rejected", age_days=100)
        for status in ("pending", "approved", "converted"):
            _signup(name=f"old_{status}", status=status, age_days=400)

        with self.assertLogs("fragrance.tasks", level="INFO") as logs:
            purged = purge_rejected_beta_signups()

        self.assertEqual(purged, 1)
        self.assertTrue(any("purged 1 rejected" in line for line in logs.output))
        self.assertEqual(
            set(BetaSignup.objects.values_list("normalized_email", flat=True)),
            {"recent_rejected@example.com", "old_pending@example.com",
             "old_approved@example.com", "old_converted@example.com"},
        )

    def test_second_run_deletes_nothing(self) -> None:
        _signup(name="old_rejected", status="rejected", age_days=200)
        self.assertEqual(purge_rejected_beta_signups(), 1)
        self.assertEqual(purge_rejected_beta_signups(), 0)


class PurgeScheduleTests(SimpleTestCase):
    def test_registered_in_beat_schedule(self) -> None:
        from config.celery import app

        entry = app.conf.beat_schedule["system-purge-rejected-beta-signups"]
        self.assertEqual(entry["task"], "fragrance.tasks.purge_rejected_beta_signups")
