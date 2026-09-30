"""Shared builders for the failed-run retry chain tests."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from fragrance.models import RecommendationRun

FROZEN_NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


class FrozenClockTestCase(TestCase):
    """
    TestCase whose clock is FROZEN_NOW for the whole test, so auto_now_add
    columns, has_active_access, the quota window and every `now=None` default
    agree with the back-dated fixtures instead of mixing with the real date.
    """

    def setUp(self) -> None:
        super().setUp()
        clock = patch("django.utils.timezone.now", return_value=FROZEN_NOW)
        clock.start()
        self.addCleanup(clock.stop)


def make_run(
    *, user: User, status: str, started_days_ago: float = 0, **fields: object
) -> RecommendationRun:
    """A run whose chain began `started_days_ago` days before FROZEN_NOW (triggered_at is auto_now_add, so it is back-dated)."""
    run = RecommendationRun.objects.create(user=user, status=status, **fields)
    RecommendationRun.objects.filter(pk=run.pk).update(
        triggered_at=FROZEN_NOW - timedelta(days=started_days_ago)
    )
    run.refresh_from_db()
    return run


def make_scheduled_run(
    *, user: User, attempt: int = 1, due_in: timedelta = timedelta(hours=-1), started_days_ago: float = 1
) -> RecommendationRun:
    """A failed run with its automatic retry scheduled `due_in` from FROZEN_NOW (negative = already due)."""
    return make_run(
        user=user,
        status="failed",
        started_days_ago=started_days_ago,
        attempt=attempt,
        retry_state="scheduled",
        next_retry_at=FROZEN_NOW + due_in,
        last_failed_at=FROZEN_NOW - timedelta(days=1),
        error_message="boom",
    )
