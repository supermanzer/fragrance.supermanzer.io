"""First-run policy: manual runs are staff-only except one first run, plus the payload fields that describe it."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from fragrance.models import FragranceConfig, RecommendationRun, Subscription
from fragrance.services import has_completed_run, start_recommendation_run
from fragrance.tests.support.factories import active_subscription_for
from rest_framework.test import APIClient

_FROZEN_NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def _client_for(*, user: User) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _trigger(*, user: User) -> tuple:
    with patch("fragrance.tasks.monthly_fragrance_run.delay") as delay:
        delay.return_value = SimpleNamespace(id="task-first")
        response = _client_for(user=user).post(reverse("run-trigger"))
    return response, delay


def _payload(*, user: User) -> dict:
    return _client_for(user=user).get(reverse("billing-subscription")).json()


class HasCompletedRunTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="hcr", password="pw12345!")

    def test_false_with_no_runs(self) -> None:
        self.assertFalse(has_completed_run(user=self.user))

    def test_only_done_counts(self) -> None:
        for run_status in ("pending", "running", "failed"):
            RecommendationRun.objects.create(user=self.user, status=run_status)
        self.assertFalse(has_completed_run(user=self.user))
        RecommendationRun.objects.create(user=self.user, status="done")
        self.assertTrue(has_completed_run(user=self.user))

    def test_another_users_done_run_does_not_count(self) -> None:
        other = User.objects.create_user(username="hcr_other", password="pw12345!")
        RecommendationRun.objects.create(user=other, status="done")
        self.assertFalse(has_completed_run(user=self.user))


class FirstRunTriggerTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="first", password="pw12345!")
        active_subscription_for(self.user)

    def test_non_staff_with_no_run_may_start_their_first_run(self) -> None:
        response, delay = _trigger(user=self.user)

        self.assertEqual(response.status_code, 202)
        run = RecommendationRun.objects.get(user=self.user)
        delay.assert_called_once_with(user_id=self.user.id, run_id=run.id)

    def test_non_staff_after_a_done_run_gets_403_manual_runs_staff_only(self) -> None:
        RecommendationRun.objects.create(user=self.user, status="done")

        response, delay = _trigger(user=self.user)

        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body["code"], "manual_runs_staff_only")
        self.assertTrue(body["detail"])
        delay.assert_not_called()
        self.assertEqual(RecommendationRun.objects.filter(user=self.user).count(), 1)

    def test_the_403_holds_even_when_the_quota_window_has_long_passed(self) -> None:
        run = RecommendationRun.objects.create(user=self.user, status="done")
        RecommendationRun.objects.filter(pk=run.pk).update(
            triggered_at=datetime.now(tz=timezone.utc) - timedelta(days=200)
        )

        response, delay = _trigger(user=self.user)

        self.assertEqual(response.status_code, 403)
        delay.assert_not_called()

    def test_a_failed_run_does_not_count_as_done(self) -> None:
        RecommendationRun.objects.create(user=self.user, status="failed")

        response, _ = _trigger(user=self.user)

        self.assertEqual(response.status_code, 202)

    def test_another_users_done_run_does_not_use_up_my_first_run(self) -> None:
        other = User.objects.create_user(username="first_other", password="pw12345!")
        RecommendationRun.objects.create(user=other, status="done")

        response, _ = _trigger(user=self.user)

        self.assertEqual(response.status_code, 202)

    def test_staff_are_unaffected_by_a_done_run(self) -> None:
        staff = User.objects.create_user(username="first_staff", password="pw12345!", is_staff=True)
        RecommendationRun.objects.create(user=staff, status="done")

        response, delay = _trigger(user=staff)

        self.assertEqual(response.status_code, 202)
        delay.assert_called_once()

    def test_a_superuser_who_is_not_staff_gets_the_non_staff_rule(self) -> None:
        superuser = User.objects.create_user(
            username="first_su", password="pw12345!", is_staff=False, is_superuser=True
        )
        first, _ = _trigger(user=superuser)
        self.assertEqual(first.status_code, 202)
        RecommendationRun.objects.filter(user=superuser).update(status="done")

        again, delay = _trigger(user=superuser)

        self.assertEqual(again.status_code, 403)
        self.assertEqual(again.json()["code"], "manual_runs_staff_only")
        delay.assert_not_called()

    def test_the_first_run_still_needs_a_subscription(self) -> None:
        lapsed = User.objects.create_user(username="first_lapsed", password="pw12345!")

        response, delay = _trigger(user=lapsed)

        self.assertEqual(response.status_code, 402)
        delay.assert_not_called()

    def test_the_in_flight_guard_still_applies_to_the_first_run(self) -> None:
        RecommendationRun.objects.create(user=self.user, status="running")

        response, delay = _trigger(user=self.user)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "run_in_progress")
        delay.assert_not_called()

    def test_the_quota_still_applies_to_the_first_run(self) -> None:
        # Two failed dispatches exhaust max_runs + failed_run_grace with no done run.
        RecommendationRun.objects.create(user=self.user, status="failed")
        RecommendationRun.objects.create(user=self.user, status="failed")

        response, delay = _trigger(user=self.user)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "quota_exceeded")
        delay.assert_not_called()

    def test_the_service_chokepoint_is_not_gated_so_scheduled_runs_keep_working(self) -> None:
        run = RecommendationRun.objects.create(user=self.user, status="done")
        RecommendationRun.objects.filter(pk=run.pk).update(
            triggered_at=datetime.now(tz=timezone.utc) - timedelta(days=40)
        )

        for source in ("api", "beat"):
            with self.subTest(source=source):
                started = start_recommendation_run(user=self.user, source=source)
                RecommendationRun.objects.filter(pk=started.pk).update(status="failed")

        self.assertEqual(RecommendationRun.objects.filter(user=self.user).count(), 3)


class SubscriptionPayloadFirstRunTests(TestCase):
    def _frozen_payload(self, *, user: User) -> dict:
        with patch("fragrance.services.timezone.now", return_value=_FROZEN_NOW):
            return _payload(user=user)

    def _user_with_config(self, *, username: str, day: int = 15, hour: int = 9, **flags: bool) -> User:
        user = User.objects.create_user(username=username, password="pw12345!", **flags)
        FragranceConfig.objects.create(
            user=user, recipient_email=f"{username}@example.com", run_day_of_month=day, run_hour=hour
        )
        return user

    def _done_run_at(self, *, user: User, at: datetime) -> None:
        run = RecommendationRun.objects.create(user=user, status="done")
        RecommendationRun.objects.filter(pk=run.pk).update(triggered_at=at)

    def test_staff_payload(self) -> None:
        staff = self._user_with_config(username="p_staff", is_staff=True)

        body = self._frozen_payload(user=staff)

        self.assertTrue(body["is_staff"])
        self.assertTrue(body["exempt"])
        self.assertFalse(body["quota"]["first_run_available"])
        self.assertEqual(body["quota"]["next_run_at"], "2026-10-15T09:00:00Z")

    def test_superuser_who_is_not_staff_is_exempt_but_not_staff_and_gets_the_first_run(self) -> None:
        superuser = self._user_with_config(username="p_su", is_superuser=True)

        body = self._frozen_payload(user=superuser)

        self.assertFalse(body["is_staff"])
        self.assertTrue(body["exempt"])
        self.assertTrue(body["quota"]["first_run_available"])

    def test_first_run_user_payload(self) -> None:
        user = self._user_with_config(username="p_first")
        active_subscription_for(user, days=90)

        body = self._frozen_payload(user=user)

        self.assertFalse(body["is_staff"])
        self.assertTrue(body["quota"]["first_run_available"])
        self.assertTrue(body["quota"]["can_trigger"])
        self.assertEqual(body["quota"]["next_run_at"], "2026-10-15T09:00:00Z")

    def test_a_failed_run_keeps_the_first_run_available(self) -> None:
        user = self._user_with_config(username="p_failed")
        active_subscription_for(user, days=90)
        RecommendationRun.objects.create(user=user, status="failed")

        self.assertTrue(self._frozen_payload(user=user)["quota"]["first_run_available"])

    def test_an_in_flight_run_hides_the_first_run(self) -> None:
        user = self._user_with_config(username="p_flight")
        active_subscription_for(user)
        RecommendationRun.objects.create(user=user, status="running")

        self.assertFalse(_payload(user=user)["quota"]["first_run_available"])

    def test_established_user_payload_has_no_first_run(self) -> None:
        user = self._user_with_config(username="p_estab")
        active_subscription_for(user, days=90)
        self._done_run_at(user=user, at=_FROZEN_NOW - timedelta(days=29))

        body = self._frozen_payload(user=user)

        self.assertFalse(body["quota"]["first_run_available"])
        self.assertFalse(body["quota"]["can_trigger"])
        self.assertEqual(body["quota"]["next_run_at"], "2026-10-15T09:00:00Z")

    def test_next_run_is_the_first_slot_at_or_after_beat_eligibility_not_eligibility_itself(self) -> None:
        # Manual first run today; beat eligibility (30d minus the 4d tolerance) is
        # 2026-11-05T10:00Z, which is not a slot: beat fires on the 15th.
        user = self._user_with_config(username="p_later", day=15)
        active_subscription_for(user, days=90)
        self._done_run_at(user=user, at=_FROZEN_NOW - timedelta(hours=2))

        body = self._frozen_payload(user=user)

        self.assertEqual(body["quota"]["next_run_at"], "2026-11-15T09:00:00Z")

    def test_a_slot_that_beat_would_refuse_is_skipped_to_the_following_month(self) -> None:
        # Eligibility is 2026-11-05T10:00Z, an hour after the November 5th 09:00 slot.
        user = self._user_with_config(username="p_skip", day=5, hour=9)
        active_subscription_for(user, days=90)
        self._done_run_at(user=user, at=_FROZEN_NOW - timedelta(hours=2))

        body = self._frozen_payload(user=user)

        self.assertEqual(body["quota"]["next_run_at"], "2026-12-05T09:00:00Z")

    def test_a_slot_earlier_this_month_rolls_to_next_month(self) -> None:
        user = self._user_with_config(username="p_roll", day=5, hour=9)
        active_subscription_for(user, days=90)

        self.assertEqual(self._frozen_payload(user=user)["quota"]["next_run_at"], "2026-11-05T09:00:00Z")

    def test_a_legacy_day_above_28_is_treated_as_28(self) -> None:
        user = self._user_with_config(username="p_31")
        active_subscription_for(user, days=90)
        FragranceConfig.objects.filter(user=user).update(run_day_of_month=31)

        self.assertEqual(self._frozen_payload(user=user)["quota"]["next_run_at"], "2026-10-28T09:00:00Z")

    def test_next_run_is_null_without_a_config(self) -> None:
        user = User.objects.create_user(username="p_noconfig", password="pw12345!")
        active_subscription_for(user, days=90)

        self.assertIsNone(self._frozen_payload(user=user)["quota"]["next_run_at"])
        self.assertFalse(FragranceConfig.objects.filter(user=user).exists())

    def test_next_run_is_null_for_a_legacy_weekly_row(self) -> None:
        user = self._user_with_config(username="p_weekly")
        active_subscription_for(user, days=90)
        FragranceConfig.objects.filter(user=user).update(frequency="weekly")

        self.assertIsNone(self._frozen_payload(user=user)["quota"]["next_run_at"])

    def test_lapsed_user_payload(self) -> None:
        user = self._user_with_config(username="p_lapsed")
        Subscription.objects.create(
            user=user, status="active", access_expires_at=_FROZEN_NOW - timedelta(days=1)
        )

        body = self._frozen_payload(user=user)

        self.assertFalse(body["is_staff"])
        self.assertFalse(body["quota"]["first_run_available"])
        self.assertIsNone(body["quota"]["next_run_at"])

    def test_revoked_user_payload(self) -> None:
        user = self._user_with_config(username="p_revoked")
        Subscription.objects.create(user=user, status="revoked", access_expires_at=_FROZEN_NOW + timedelta(days=9))

        body = self._frozen_payload(user=user)

        self.assertFalse(body["quota"]["first_run_available"])
        self.assertIsNone(body["quota"]["next_run_at"])

    def test_payload_is_read_only(self) -> None:
        user = User.objects.create_user(username="p_readonly", password="pw12345!")
        active_subscription_for(user, days=90)

        _payload(user=user)

        self.assertFalse(FragranceConfig.objects.filter(user=user).exists())
