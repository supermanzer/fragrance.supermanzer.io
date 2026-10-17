from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from fragrance.models import FragranceConfig
from fragrance.services import default_schedule_for
from fragrance.tests.support.factories import active_subscription_for


class DefaultScheduleForTests(SimpleTestCase):
    def test_deterministic(self) -> None:
        self.assertEqual(
            default_schedule_for(user_id=42), default_schedule_for(user_id=42)
        )

    def test_formula(self) -> None:
        self.assertEqual(default_schedule_for(user_id=1), (2, 0))
        self.assertEqual(default_schedule_for(user_id=28), (1, 1))

    def test_day_always_1_to_28_and_hour_0_to_23_across_wide_sweep(
        self,
    ) -> None:
        for user_id in range(1, 20001):
            day, hour = default_schedule_for(user_id=user_id)
            self.assertTrue(1 <= day <= 28, user_id)
            self.assertTrue(0 <= hour <= 23, user_id)

    def test_672_slots_spread_evenly(self) -> None:
        from collections import Counter

        slots = Counter(
            default_schedule_for(user_id=i) for i in range(1, 672 * 3 + 1)
        )
        self.assertEqual(len(slots), 672)
        self.assertEqual(set(slots.values()), {3})
        days = Counter(day for day, _ in slots.elements())
        self.assertEqual(len(days), 28)
        self.assertEqual(len(set(days.values())), 1)


class FragranceConfigCreationJitterTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="jit", password="pw12345!"
        )
        active_subscription_for(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_first_config_get_applies_jittered_default(self) -> None:
        response = self.client.get(reverse("fragrance-config"))
        day, hour = default_schedule_for(user_id=self.user.id)
        self.assertEqual(response.json()["run_day_of_month"], day)
        self.assertEqual(response.json()["run_hour"], hour)

    def test_existing_choice_is_never_overwritten(self) -> None:
        FragranceConfig.objects.create(
            user=self.user,
            recipient_email="j@example.com",
            run_day_of_month=15,
            run_hour=3,
        )
        response = self.client.get(reverse("fragrance-config"))
        self.assertEqual(response.json()["run_day_of_month"], 15)
        self.assertEqual(response.json()["run_hour"], 3)

    def test_explicit_day_of_1_chosen_via_api_after_creation_is_kept(
        self,
    ) -> None:
        self.client.get(reverse("fragrance-config"))
        self.client.patch(
            reverse("fragrance-config"),
            {"run_day_of_month": 1, "run_hour": 9},
            format="json",
        )
        response = self.client.get(reverse("fragrance-config"))
        self.assertEqual(response.json()["run_day_of_month"], 1)
        self.assertEqual(response.json()["run_hour"], 9)


class FragranceConfigDeliveryAddressTests(TestCase):
    def test_lazy_creation_defaults_the_recipient_to_the_account_email(self) -> None:
        from fragrance.services import get_or_create_fragrance_config

        user = User.objects.create_user(username="addr", email="addr@example.com", password="pw12345!")

        config = get_or_create_fragrance_config(user=user)

        self.assertEqual(config.recipient_email, "addr@example.com")
        self.assertEqual(
            (config.run_day_of_month, config.run_hour), default_schedule_for(user_id=user.id)
        )

    def test_an_existing_row_is_never_overwritten(self) -> None:
        from fragrance.services import get_or_create_fragrance_config

        user = User.objects.create_user(username="keep", email="keep@example.com", password="pw12345!")
        FragranceConfig.objects.create(
            user=user, recipient_email="chosen@example.com", run_day_of_month=15, run_hour=3
        )

        config = get_or_create_fragrance_config(user=user)

        self.assertEqual(config.recipient_email, "chosen@example.com")
        self.assertEqual((config.run_day_of_month, config.run_hour), (15, 3))

    def test_first_config_get_returns_the_account_email_as_recipient(self) -> None:
        user = User.objects.create_user(username="addr2", email="addr2@example.com", password="pw12345!")
        active_subscription_for(user)
        client = APIClient()
        client.force_authenticate(user=user)

        response = client.get(reverse("fragrance-config"))

        self.assertEqual(response.json()["recipient_email"], "addr2@example.com")


class RebalanceRunSchedulesCommandTests(TestCase):
    def setUp(self) -> None:
        self.untouched = User.objects.create_user(
            username="untouched", password="pw12345!"
        )
        self.custom = User.objects.create_user(
            username="custom", password="pw12345!"
        )
        self.weekly = User.objects.create_user(
            username="weekly", password="pw12345!"
        )
        FragranceConfig.objects.create(
            user=self.untouched, recipient_email="a@example.com"
        )
        FragranceConfig.objects.create(
            user=self.custom,
            recipient_email="b@example.com",
            run_day_of_month=15,
            run_hour=9,
        )
        FragranceConfig.objects.create(
            user=self.weekly,
            recipient_email="c@example.com",
            frequency="weekly",
        )

    def _run(self, **kwargs: object) -> str:
        out = StringIO()
        call_command("rebalance_run_schedules", stdout=out, **kwargs)
        return out.getvalue()

    def test_dry_run_by_default_writes_nothing_but_reports(self) -> None:
        output = self._run()
        config = FragranceConfig.objects.get(user=self.untouched)
        self.assertEqual((config.run_day_of_month, config.run_hour), (1, 9))
        self.assertIn("untouched", output)
        self.assertIn("Dry run", output)

    def test_apply_moves_untouched_row_and_skips_customised_and_weekly(
        self,
    ) -> None:
        self._run(apply=True)
        moved = FragranceConfig.objects.get(user=self.untouched)
        self.assertEqual(
            (moved.run_day_of_month, moved.run_hour),
            default_schedule_for(user_id=self.untouched.id),
        )
        custom = FragranceConfig.objects.get(user=self.custom)
        self.assertEqual((custom.run_day_of_month, custom.run_hour), (15, 9))
        weekly = FragranceConfig.objects.get(user=self.weekly)
        self.assertEqual((weekly.run_day_of_month, weekly.run_hour), (1, 9))

    def test_apply_syncs_the_periodic_task(self) -> None:
        from django_celery_beat.models import PeriodicTask

        self._run(apply=True)
        day, hour = default_schedule_for(user_id=self.untouched.id)
        task = PeriodicTask.objects.get(
            name=f"fragrance-run-{self.untouched.id}"
        )
        self.assertEqual(task.crontab.day_of_month, str(day))
        self.assertEqual(task.crontab.hour, str(hour))


class FragranceConfigScheduleValidationTests(TestCase):
    url = "/api/v1/config/"

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="sched", password="pw12345!"
        )
        active_subscription_for(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _put(self, **fields: int) -> object:
        return self.client.patch(self.url, fields, format="json")

    def test_out_of_range_values_are_rejected(self) -> None:
        for field, bad in (
            ("run_hour", 24),
            ("run_hour", 99),
            ("run_hour", -1),
            ("run_day_of_week", 7),
            ("run_day_of_week", -1),
            ("run_day_of_month", 0),
            ("run_day_of_month", 29),
            ("run_day_of_month", 31),
            ("run_day_of_month", 32),
            ("run_month", 0),
            ("run_month", 13),
        ):
            with self.subTest(field=field, value=bad):
                response = self._put(**{field: bad})
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.json())

    def test_boundary_values_are_accepted(self) -> None:
        for field, good in (
            ("run_hour", 0),
            ("run_hour", 23),
            ("run_day_of_week", 0),
            ("run_day_of_week", 6),
            ("run_day_of_month", 1),
            ("run_day_of_month", 28),
            ("run_month", 1),
            ("run_month", 12),
        ):
            with self.subTest(field=field, value=good):
                response = self._put(**{field: good})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()[field], good)

    def test_rejected_value_is_not_persisted(self) -> None:
        self._put(run_hour=99)
        self.assertNotEqual(
            FragranceConfig.objects.get(user=self.user).run_hour, 99
        )

    def test_existing_default_config_still_saves(self) -> None:
        # recipient_email is read-only now (verified-address flow); a schedule
        # save never carries it. See tests/recipient/ for the 400 contract.
        response = self._put(
            run_hour=9,
            run_day_of_week=1,
            run_day_of_month=1,
            run_month=1,
        )
        self.assertEqual(response.status_code, 200)


class MonthlyOnlyFrequencyTests(TestCase):
    """Only monthly is offered; the weekly/yearly columns and code stay for a later reintroduction."""

    url = "/api/v1/config/"

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="freq", password="pw12345!")
        active_subscription_for(self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_only_monthly_is_a_valid_choice(self) -> None:
        from fragrance.models import FREQUENCIES

        self.assertEqual(FREQUENCIES, [("monthly", "Monthly")])

    def test_weekly_and_yearly_are_rejected_with_a_field_error(self) -> None:
        for value in ("weekly", "yearly", "daily", "", "MONTHLY"):
            with self.subTest(frequency=value):
                response = self.client.patch(self.url, {"frequency": value}, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn("frequency", response.json())

    def test_rejected_frequency_is_not_persisted(self) -> None:
        self.client.get(self.url)
        self.client.patch(self.url, {"frequency": "weekly"}, format="json")
        self.assertEqual(FragranceConfig.objects.get(user=self.user).frequency, "monthly")

    def test_monthly_is_accepted(self) -> None:
        response = self.client.patch(self.url, {"frequency": "monthly"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["frequency"], "monthly")

    def test_omitting_frequency_keeps_monthly(self) -> None:
        response = self.client.patch(self.url, {"run_hour": 4}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["frequency"], "monthly")

    def test_a_legacy_weekly_row_can_still_be_read_and_edited_without_touching_frequency(self) -> None:
        FragranceConfig.objects.create(
            user=self.user, recipient_email="legacy@example.com", frequency="weekly"
        )
        self.assertEqual(self.client.get(self.url).json()["frequency"], "weekly")
        response = self.client.patch(self.url, {"run_hour": 5}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["frequency"], "weekly")


class ScheduleSignalFrequencyTests(TestCase):
    """What signals.sync_user_schedule builds for monthly, for legacy non-monthly rows, and for junk."""

    def _crontab(self, *, frequency: str, suffix: str = "", **fields: object):
        from django_celery_beat.models import PeriodicTask

        user = User.objects.create_user(username=f"sig_{frequency or 'blank'}{suffix}", password="pw12345!")
        FragranceConfig.objects.create(
            user=user, recipient_email="s@example.com", frequency=frequency, **fields
        )
        return PeriodicTask.objects.get(name=f"fragrance-run-{user.id}").crontab

    def test_monthly_runs_on_its_day_of_month(self) -> None:
        crontab = self._crontab(frequency="monthly", run_day_of_month=17, run_hour=6)
        self.assertEqual(
            (crontab.day_of_month, crontab.day_of_week, crontab.month_of_year, crontab.hour),
            ("17", "*", "*", "6"),
        )

    def test_legacy_weekly_row_keeps_its_weekly_schedule(self) -> None:
        crontab = self._crontab(frequency="weekly", run_day_of_week=3, run_hour=6)
        self.assertEqual(
            (crontab.day_of_week, crontab.day_of_month, crontab.month_of_year),
            ("3", "*", "*"),
        )

    def test_legacy_yearly_row_keeps_its_yearly_schedule(self) -> None:
        crontab = self._crontab(frequency="yearly", run_day_of_month=9, run_month=4)
        self.assertEqual(
            (crontab.day_of_month, crontab.month_of_year, crontab.day_of_week),
            ("9", "4", "*"),
        )

    def test_a_stored_day_above_28_is_clamped_to_28_in_the_crontab(self) -> None:
        for day in (29, 30, 31):
            with self.subTest(day=day):
                crontab = self._crontab(frequency="monthly", run_day_of_month=day, run_hour=6, suffix=str(day))
                self.assertEqual(crontab.day_of_month, "28")

    def test_the_clamp_never_touches_a_day_at_or_below_28(self) -> None:
        for day in (1, 27, 28):
            with self.subTest(day=day):
                crontab = self._crontab(frequency="monthly", run_day_of_month=day, run_hour=6, suffix=str(day))
                self.assertEqual(crontab.day_of_month, str(day))

    def test_a_legacy_row_resaved_through_the_signal_is_clamped(self) -> None:
        from django_celery_beat.models import PeriodicTask

        user = User.objects.create_user(username="legacy31", password="pw12345!")
        config = FragranceConfig.objects.create(user=user, recipient_email="l@example.com")
        FragranceConfig.objects.filter(pk=config.pk).update(run_day_of_month=31)

        config.refresh_from_db()
        config.save()

        crontab = PeriodicTask.objects.get(name=f"fragrance-run-{user.id}").crontab
        self.assertEqual(crontab.day_of_month, "28")
        config.refresh_from_db()
        self.assertEqual(config.run_day_of_month, 31)

    def test_clamp_applies_to_legacy_yearly_rows_too(self) -> None:
        crontab = self._crontab(frequency="yearly", run_day_of_month=30, run_month=2, suffix="y30")
        self.assertEqual((crontab.day_of_month, crontab.month_of_year), ("28", "2"))

    def test_an_unrecognised_frequency_falls_back_to_monthly_never_daily(self) -> None:
        crontab = self._crontab(frequency="daily", run_day_of_month=12, run_hour=7)
        self.assertEqual(
            (crontab.day_of_month, crontab.day_of_week, crontab.month_of_year, crontab.hour),
            ("12", "*", "*", "7"),
        )
