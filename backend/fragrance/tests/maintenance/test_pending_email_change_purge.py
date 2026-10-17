"""
purge_expired_pending_email_changes: only pending rows whose link expired at
least PENDING_CHANGE_PURGE_AFTER ago are deleted, across all users. Confirm
and cancel already delete their row, so a "confirmed" row does not exist; the
verified address on FragranceConfig is never touched by the purge.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from fragrance.models import FragranceConfig, PendingEmailChange
from fragrance.recipient import CONFIRMATION_LINK_MAX_AGE, PENDING_CHANGE_PURGE_AFTER
from fragrance.tasks import purge_expired_pending_email_changes


def _pending(*, user: User, purpose: str, expired_for: timedelta) -> PendingEmailChange:
    return PendingEmailChange.objects.create(
        user=user,
        purpose=purpose,
        address=f"{user.username}-{purpose}@example.com",
        nonce="n" * 32,
        expires_at=timezone.now() - expired_for,
    )


class PurgeExpiredPendingEmailChangesTests(TestCase):
    def setUp(self) -> None:
        self.alice = User.objects.create_user(username="alice", email="alice@example.com", password="pw12345!")
        self.bob = User.objects.create_user(username="bob", email="bob@example.com", password="pw12345!")

    def test_deletes_rows_expired_beyond_retention(self) -> None:
        old = _pending(user=self.alice, purpose="delivery", expired_for=PENDING_CHANGE_PURGE_AFTER + timedelta(hours=1))

        with self.assertLogs("fragrance.tasks", level="INFO") as logs:
            purged = purge_expired_pending_email_changes()

        self.assertEqual(purged, 1)
        self.assertTrue(any("purged 1 PendingEmailChange" in line for line in logs.output))
        self.assertFalse(PendingEmailChange.objects.filter(pk=old.pk).exists())

    def test_keeps_live_and_recently_expired_rows(self) -> None:
        live = _pending(user=self.alice, purpose="delivery", expired_for=-CONFIRMATION_LINK_MAX_AGE)
        just_expired = _pending(user=self.alice, purpose="receipt", expired_for=timedelta(minutes=5))
        inside_window = _pending(
            user=self.bob, purpose="delivery", expired_for=PENDING_CHANGE_PURGE_AFTER - timedelta(hours=1)
        )

        self.assertEqual(purge_expired_pending_email_changes(), 0)
        self.assertEqual(
            set(PendingEmailChange.objects.values_list("pk", flat=True)),
            {live.pk, just_expired.pk, inside_window.pk},
        )

    def test_other_users_rows_and_verified_address_untouched(self) -> None:
        config = FragranceConfig.objects.create(user=self.alice, recipient_email="verified@example.com")
        _pending(user=self.alice, purpose="delivery", expired_for=PENDING_CHANGE_PURGE_AFTER + timedelta(days=1))
        bobs = _pending(user=self.bob, purpose="delivery", expired_for=timedelta(hours=1))

        self.assertEqual(purge_expired_pending_email_changes(), 1)

        self.assertEqual(list(PendingEmailChange.objects.values_list("pk", flat=True)), [bobs.pk])
        config.refresh_from_db()
        self.assertEqual(config.recipient_email, "verified@example.com")

    def test_purges_expired_rows_of_every_user_and_is_idempotent(self) -> None:
        age = PENDING_CHANGE_PURGE_AFTER + timedelta(days=2)
        _pending(user=self.alice, purpose="delivery", expired_for=age)
        _pending(user=self.alice, purpose="receipt", expired_for=age)
        _pending(user=self.bob, purpose="receipt", expired_for=age)

        self.assertEqual(purge_expired_pending_email_changes(), 3)
        self.assertEqual(purge_expired_pending_email_changes(), 0)


class PendingEmailChangePurgeScheduleTests(SimpleTestCase):
    def test_registered_in_beat_schedule(self) -> None:
        from config.celery import app

        entry = app.conf.beat_schedule["system-purge-expired-pending-email-changes"]
        self.assertEqual(entry["task"], "fragrance.tasks.purge_expired_pending_email_changes")

    def test_retention_outlasts_link_lifetime(self) -> None:
        self.assertGreater(PENDING_CHANGE_PURGE_AFTER, CONFIRMATION_LINK_MAX_AGE)
