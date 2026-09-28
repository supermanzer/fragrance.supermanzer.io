import importlib
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

import stripe
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from fragrance.models import (
    AIModelConfig,
    Fragrance,
    FragranceConfig,
    RecommendationRun,
    Subscription,
)
from fragrance.services import RunInProgress, start_recommendation_run
from fragrance.tasks import monthly_fragrance_run
from fragrance.tests.support.bases import StripeClientTestCase
from fragrance.tests.support.factories import (
    _make_plan,
    _staff_not_superuser,
    active_subscription_for,
)


class SubscriptionPermissionTests(TestCase):
    def setUp(self) -> None:
        self.paid = User.objects.create_user(
            username="paid", password="pw12345!"
        )
        active_subscription_for(self.paid)
        self.lapsed = User.objects.create_user(
            username="lapsed", password="pw12345!"
        )
        Subscription.objects.create(
            user=self.lapsed,
            status="active",
            access_expires_at=timezone.now() - timedelta(days=1),
        )
        self.staff = User.objects.create_user(
            username="staffer", password="pw12345!", is_staff=True
        )
        self.norow = User.objects.create_user(
            username="norow", password="pw12345!"
        )

    def _client(self, user: User) -> APIClient:
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _assert_402(self, response) -> None:
        self.assertEqual(response.status_code, 402)
        self.assertEqual(
            response.json(),
            {
                "code": "subscription_required",
                "detail": "An active subscription is required for this action.",
            },
        )

    def test_unauthenticated_is_401_not_402(self) -> None:
        for url in (
            reverse("fragrance-list"),
            reverse("run-list"),
            reverse("import-collection"),
        ):
            self.assertEqual(APIClient().get(url).status_code, 401)
        self.assertEqual(
            APIClient().post(reverse("import-collection")).status_code, 401
        )

    def test_paid_user_can_write_collection(self) -> None:
        response = self._client(self.paid).post(
            reverse("fragrance-list"),
            {"name": "Aventus", "house": "Creed", "status": "own"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)

    def test_expired_subscription_reads_but_cannot_write(self) -> None:
        client = self._client(self.lapsed)
        Fragrance.objects.create(
            user=self.lapsed, name="Old", house="H", status="own"
        )
        self.assertEqual(client.get(reverse("fragrance-list")).status_code, 200)
        self.assertEqual(client.get(reverse("run-list")).status_code, 200)
        self.assertEqual(
            client.get(reverse("fragrance-config")).status_code, 200
        )
        self.assertEqual(
            client.get(reverse("export-collection")).status_code, 200
        )
        self._assert_402(
            client.post(
                reverse("fragrance-list"),
                {"name": "New", "house": "H", "status": "own"},
                format="json",
            )
        )

    def test_lapsed_user_writes_all_402(self) -> None:
        client = self._client(self.lapsed)
        fragrance = Fragrance.objects.create(
            user=self.lapsed, name="Old", house="H", status="own"
        )
        detail = reverse("fragrance-detail", args=[fragrance.pk])
        self._assert_402(client.patch(detail, {"notes": "x"}, format="json"))
        self._assert_402(
            client.put(
                detail,
                {"name": "Old", "house": "H", "status": "like"},
                format="json",
            )
        )
        self._assert_402(client.delete(detail))
        self.assertTrue(Fragrance.objects.filter(pk=fragrance.pk).exists())
        self._assert_402(client.post(reverse("import-collection")))
        self._assert_402(
            client.patch(
                reverse("fragrance-config"), {"run_hour": 5}, format="json"
            )
        )
        with patch("fragrance.tasks.monthly_fragrance_run.delay") as delay:
            self._assert_402(client.post(reverse("run-trigger")))
        delay.assert_not_called()
        self.assertFalse(
            RecommendationRun.objects.filter(user=self.lapsed).exists()
        )

    def test_lapsed_import_is_402_even_with_no_file(self) -> None:
        self._assert_402(
            self._client(self.lapsed).post(reverse("import-collection"))
        )

    def test_staff_bypasses_without_subscription(self) -> None:
        client = self._client(self.staff)
        response = client.post(
            reverse("fragrance-list"),
            {"name": "Aventus", "house": "Creed", "status": "own"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertFalse(Subscription.objects.filter(user=self.staff).exists())

    def test_no_subscription_row_is_402_and_none_is_created(self) -> None:
        client = self._client(self.norow)
        self._assert_402(
            client.post(
                reverse("fragrance-list"),
                {"name": "X", "house": "H", "status": "own"},
                format="json",
            )
        )
        self.assertEqual(client.get(reverse("fragrance-list")).status_code, 200)
        self.assertEqual(
            client.get(reverse("export-collection")).status_code, 200
        )
        self.assertFalse(Subscription.objects.filter(user=self.norow).exists())

    def test_revoked_status_with_future_expiry_is_402(self) -> None:
        subscription = active_subscription_for(self.norow)
        subscription.status = "revoked"
        subscription.save()
        self._assert_402(
            self._client(self.norow).post(reverse("import-collection"))
        )

    def test_rate_and_profile_views_stay_open_to_lapsed_users(self) -> None:
        client = self._client(self.lapsed)
        self.assertEqual(
            client.get(reverse("recommendations")).status_code, 200
        )
        self.assertEqual(
            client.get(reverse("preference-profile-history")).status_code, 200
        )
        self.assertEqual(
            client.get(reverse("preference-profile")).status_code, 404
        )


class StaffNotSuperuserExemptionTests(TestCase):
    """The production exempt account: is_staff=True, is_superuser=False, no Subscription row."""

    def setUp(self) -> None:
        AIModelConfig.objects.all().delete()
        self.user = _staff_not_superuser()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_writes_pass_both_permission_classes_without_creating_a_row(
        self,
    ) -> None:
        self.assertFalse(self.user.is_superuser)
        response = self.client.post(
            reverse("fragrance-list"),
            {"name": "Aventus", "house": "Creed", "status": "own"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(
            self.client.post(reverse("import-collection")).status_code, 402
        )
        self.assertFalse(Subscription.objects.filter(user=self.user).exists())

    def test_quota_bypassed_but_in_flight_guard_is_not(self) -> None:
        start_recommendation_run(user=self.user, source="test")
        with self.assertRaises(RunInProgress):
            start_recommendation_run(user=self.user, source="test")
        for status_ in ("pending", "running"):
            RecommendationRun.objects.filter(user=self.user).update(
                status=status_
            )
            with self.assertRaises(RunInProgress):
                start_recommendation_run(user=self.user, source="test")
        RecommendationRun.objects.filter(user=self.user).update(status="done")
        start_recommendation_run(user=self.user, source="test")

    def test_beat_path_runs_for_them(self) -> None:
        FragranceConfig.objects.create(
            user=self.user, recipient_email="s@example.com"
        )
        with patch(
            "fragrance.tasks.registry.get_active_config",
            side_effect=RuntimeError("stop"),
        ):
            monthly_fragrance_run.push_request(
                retries=monthly_fragrance_run.max_retries
            )
            try:
                with self.assertRaises(RuntimeError):
                    monthly_fragrance_run(user_id=self.user.id)
            finally:
                monthly_fragrance_run.pop_request()
        self.assertEqual(
            RecommendationRun.objects.filter(user=self.user).count(), 1
        )

    def test_payload_reports_effective_access(self) -> None:
        body = self.client.get(reverse("billing-subscription")).json()
        self.assertTrue(body["has_active_access"])
        self.assertTrue(body["exempt"])
        self.assertIsNone(body["quota"]["runs_allowed"])
        self.assertIsNone(body["quota"]["next_eligible_at"])
        self.assertTrue(body["quota"]["can_trigger"])
        self.assertEqual(body["status"], "none")


class GrantStaffMigrationTests(TestCase):
    def test_forward_function(self) -> None:
        from django.apps import apps as real_apps

        migration = importlib.import_module(
            "fragrance.migrations.0009_grant_staff_to_existing_users"
        )
        before = datetime(2026, 6, 21, tzinfo=dt_timezone.utc)
        after = datetime(2026, 9, 20, 0, 0, tzinfo=dt_timezone.utc)
        plain = User.objects.create_user(
            username="old_plain", password="pw12345!"
        )
        staff = User.objects.create_user(
            username="old_staff", password="pw12345!", is_staff=True
        )
        late = User.objects.create_user(
            username="late_joiner", password="pw12345!"
        )
        User.objects.filter(pk__in=[plain.pk, staff.pk]).update(
            date_joined=before
        )
        User.objects.filter(pk=late.pk).update(date_joined=after)
        migration.grant_staff_to_existing_users(real_apps, None)
        for user in (plain, staff, late):
            user.refresh_from_db()
        self.assertTrue(plain.is_staff)
        self.assertTrue(staff.is_staff)
        self.assertFalse(plain.is_superuser)
        self.assertFalse(late.is_staff)
        newcomer = User.objects.create_user(
            username="new_after", password="pw12345!"
        )
        self.assertFalse(newcomer.is_staff)

    def test_already_staff_user_after_cutoff_is_untouched(self) -> None:
        from django.apps import apps as real_apps

        migration = importlib.import_module(
            "fragrance.migrations.0009_grant_staff_to_existing_users"
        )
        staff = User.objects.create_user(
            username="late_staff", password="pw12345!", is_staff=True
        )
        User.objects.filter(pk=staff.pk).update(
            date_joined=datetime(2026, 10, 1, tzinfo=dt_timezone.utc)
        )
        migration.grant_staff_to_existing_users(real_apps, None)
        staff.refresh_from_db()
        self.assertTrue(staff.is_staff)


class ExemptCanTriggerTests(TestCase):
    def test_can_trigger_false_only_while_in_flight(self) -> None:
        user = _staff_not_superuser(username="exempt_ct")
        client = APIClient()
        client.force_authenticate(user=user)
        url = reverse("billing-subscription")
        RecommendationRun.objects.create(user=user, status="done")
        self.assertIs(client.get(url).json()["quota"]["can_trigger"], True)
        RecommendationRun.objects.create(user=user, status="pending")
        quota = client.get(url).json()["quota"]
        self.assertIs(quota["can_trigger"], False)
        self.assertIsNone(quota["runs_allowed"])
        self.assertIsNone(quota["next_eligible_at"])


class RevokedAndResendTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(
            username="rev", password="pw12345!"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_revoked_user_can_start_checkout_again(self) -> None:
        plan = _make_plan(slug="rev-plan", interval="month", access_days=30)
        Subscription.objects.create(
            user=self.user,
            status="revoked",
            access_expires_at=timezone.now(),
            stripe_customer_id="cus_rev",
        )
        self.stripe.v1.checkout.sessions.create.return_value = stripe.checkout.Session.construct_from(
            {"id": "cs_1", "object": "checkout.session", "url": "https://checkout.example/x"},
            "sk_test_x",
        )
        with override_settings(
            STRIPE_SECRET_KEY="sk_test_x",
            STRIPE_API_VERSION="2025-09-30.clover",
        ):
            response = self.client.post(
                reverse("billing-checkout"), {"slug": plan.slug}, format="json"
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["url"], "https://checkout.example/x")

    def test_resend_email_is_402_for_lapsed_user(self) -> None:
        run = RecommendationRun.objects.create(user=self.user, status="done")
        response = self.client.post(reverse("run-resend-email", args=[run.pk]))
        self.assertEqual(response.status_code, 402)
