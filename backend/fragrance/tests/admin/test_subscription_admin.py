"""SubscriptionAdmin: read-only except for the "Unlock disputed account" action."""

from datetime import timedelta

from django.contrib import admin, messages
from django.contrib.auth.models import Permission, User
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from fragrance.admin import SubscriptionAdmin
from fragrance.models import Subscription


class UnlockDisputedAccountActionTests(TestCase):
    def setUp(self) -> None:
        self.superuser = User.objects.create_superuser(
            username="hal", email="hal@example.com", password="hal-9000"
        )
        self.client.force_login(self.superuser)
        self.dispute_time = timezone.now() - timedelta(days=2)
        self.locked_user = User.objects.create_user(username="locked", email="locked@example.com")
        self.locked = Subscription.objects.create(
            user=self.locked_user, status="revoked", access_expires_at=self.dispute_time,
            stripe_customer_id="cus_locked", stripe_subscription_id="sub_locked",
        )
        self.url = reverse("admin:fragrance_subscription_changelist")

    def _run(self, pks: list[int], *, follow: bool = True):
        return self.client.post(
            self.url,
            {"action": "unlock_disputed_accounts", "_selected_action": [str(pk) for pk in pks]},
            follow=follow,
        )

    def _run_action_name(self, name: str, pks: list[int]):
        return self.client.post(
            self.url,
            {"action": name, "_selected_action": [str(pk) for pk in pks], "post": "yes"},
            follow=True,
        )

    def _messages(self, response) -> list[tuple[int, str]]:
        return [(m.level, m.message) for m in response.context["messages"]]

    def test_unlocks_a_revoked_row_to_none_and_keeps_access_lapsed(self) -> None:
        response = self._run([self.locked.pk])

        self.locked.refresh_from_db()
        self.assertEqual(self.locked.status, "none")
        self.assertEqual(self.locked.access_expires_at, self.dispute_time)
        self.assertFalse(self.locked.has_active_access)
        # Nothing else on the row moves.
        self.assertEqual(self.locked.stripe_customer_id, "cus_locked")
        self.assertEqual(self.locked.stripe_subscription_id, "sub_locked")
        levels = [level for level, _ in self._messages(response)]
        self.assertEqual(levels, [messages.SUCCESS])
        self.assertIn("Unlocked", self._messages(response)[0][1])

    def test_skips_and_reports_rows_that_are_not_revoked(self) -> None:
        other_user = User.objects.create_user(username="fine", email="fine@example.com")
        active = Subscription.objects.create(
            user=other_user, status="active", access_expires_at=timezone.now() + timedelta(days=9),
        )

        response = self._run([active.pk, self.locked.pk])

        active.refresh_from_db()
        self.assertEqual(active.status, "active")
        self.locked.refresh_from_db()
        self.assertEqual(self.locked.status, "none")
        by_level = {level: text for level, text in self._messages(response)}
        self.assertIn("not 'revoked'", by_level[messages.WARNING])
        self.assertIn("Unlocked", by_level[messages.SUCCESS])

    def test_each_selected_row_gets_its_own_message(self) -> None:
        second_user = User.objects.create_user(username="locked2", email="locked2@example.com")
        second = Subscription.objects.create(user=second_user, status="revoked")
        response = self._run([self.locked.pk, second.pk])
        self.assertEqual(len(self._messages(response)), 2)

    def test_none_and_checkout_started_rows_are_left_alone(self) -> None:
        for status_value in ("none", "checkout_started"):
            user = User.objects.create_user(username=f"u_{status_value}", email=f"{status_value}@example.com")
            row = Subscription.objects.create(user=user, status=status_value)
            self._run([row.pk])
            row.refresh_from_db()
            self.assertEqual(row.status, status_value)

    def _staff_with(self, *codenames: str) -> User:
        staff = User.objects.create_user(username="staffer", password="pw12345!", is_staff=True)
        staff.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
        return User.objects.get(pk=staff.pk)

    def test_staff_holding_only_view_permission_cannot_run_it(self) -> None:
        self.client.force_login(self._staff_with("view_subscription"))
        # They can open the changelist, so the refusal below is the action's own gate.
        self.assertEqual(self.client.get(self.url).status_code, 200)

        response = self._run([self.locked.pk])

        self.locked.refresh_from_db()
        self.assertEqual(self.locked.status, "revoked")
        self.assertEqual(self.locked.access_expires_at, self.dispute_time)
        self.assertNotIn("Unlocked", " ".join(text for _, text in self._messages(response)))
        page = self.client.get(self.url)
        self.assertNotContains(page, "unlock_disputed_accounts")

    def test_staff_with_change_permission_can_run_it(self) -> None:
        self.client.force_login(self._staff_with("view_subscription", "change_subscription"))
        self.assertContains(self.client.get(self.url), "unlock_disputed_accounts")

        response = self._run([self.locked.pk])

        self.locked.refresh_from_db()
        self.assertEqual(self.locked.status, "none")
        self.assertIn("Unlocked", self._messages(response)[0][1])

    def test_a_row_deleted_between_selection_and_the_lock_is_skipped_and_reported(self) -> None:
        second_user = User.objects.create_user(username="locked3", email="locked3@example.com")
        second = Subscription.objects.create(user=second_user, status="revoked")
        selected = Subscription.objects.filter(pk__in=[self.locked.pk, second.pk])
        list(selected)  # evaluate now: the action iterates this cached selection
        Subscription.objects.filter(pk=self.locked.pk).delete()
        model_admin = SubscriptionAdmin(Subscription, admin.site)
        request = self.client.get(self.url).wsgi_request

        model_admin.unlock_disputed_accounts(request, selected)

        texts = [str(m) for m in get_messages(request)]
        self.assertTrue(any("no longer exists" in text for text in texts))
        self.assertTrue(any(text.startswith("Unlocked") for text in texts))
        second.refresh_from_db()
        self.assertEqual(second.status, "none")

    def test_nobody_can_delete_a_subscription_not_even_a_superuser(self) -> None:
        model_admin = SubscriptionAdmin(Subscription, admin.site)
        request = self.client.get(self.url).wsgi_request
        self.assertTrue(request.user.is_superuser)
        self.assertFalse(model_admin.has_delete_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request, self.locked))

    def test_delete_selected_action_is_gone_and_a_bulk_delete_post_changes_nothing(self) -> None:
        self.assertNotContains(self.client.get(self.url), "delete_selected")

        self._run_action_name("delete_selected", [self.locked.pk])

        self.assertTrue(Subscription.objects.filter(pk=self.locked.pk, status="revoked").exists())

    def test_the_delete_view_is_refused(self) -> None:
        url = reverse("admin:fragrance_subscription_delete", args=[self.locked.pk])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"post": "yes"}).status_code, 403)
        self.assertTrue(Subscription.objects.filter(pk=self.locked.pk, status="revoked").exists())

    def test_model_admin_stays_read_only_and_add_is_disallowed(self) -> None:
        model_admin = SubscriptionAdmin(Subscription, admin.site)
        self.assertEqual(
            set(model_admin.readonly_fields), {f.name for f in Subscription._meta.fields}
        )
        request = self.client.get(self.url).wsgi_request
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertEqual(model_admin.actions, ["unlock_disputed_accounts"])

    def test_unlock_is_logged_with_ids_only(self) -> None:
        with self.assertLogs("fragrance.admin", level="WARNING") as logs:
            self._run([self.locked.pk])
        self.assertIn(str(self.locked.pk), logs.output[0])
        self.assertNotIn("locked@example.com", logs.output[0])
