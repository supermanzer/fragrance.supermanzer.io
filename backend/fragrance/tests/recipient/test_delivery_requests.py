"""POST /config/recipient/, .../resend/, DELETE .../pending/ and the config read/PATCH contract."""

import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from fragrance import recipient
from fragrance.models import FragranceConfig, PendingEmailChange, RecipientChangeAttempt
from fragrance.tasks import _delivery_address
from fragrance.tests.recipient.common import PASSWORD, RecipientTestCase
from fragrance.tests.support.factories import active_subscription_for
from kombu.exceptions import OperationalError
from rest_framework.test import APIClient


class _DeliveryBase(RecipientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("recipient-change")
        self.resend_url = reverse("recipient-resend")
        self.pending_url = reverse("recipient-pending")

    def request_change(self, email: object = "new@example.com", *, password: object = PASSWORD, client=None):
        body: dict = {"email": email}
        if password is not None:
            body["current_password"] = password
        return (client or self.api).post(self.url, body, format="json")


class RequestChangeTests(_DeliveryBase):
    def test_valid_request_is_202_and_leaves_the_verified_address_alone(self) -> None:
        response = self.request_change("New@Example.com ")

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["pending_recipient_email"], "new@example.com")
        self.assertEqual(body["pending_state"], "pending")
        self.assertIsNotNone(body["pending_expires_at"])
        self.assertIsNotNone(body["resend_available_at"])
        self.config.refresh_from_db()
        self.assertEqual(self.config.recipient_email, "alice@example.com")
        self.assertEqual(_delivery_address(user_id=self.user.pk), "alice@example.com")
        pending = self.pending(purpose="delivery")
        assert pending is not None
        self.mock_confirm_delay.assert_called_once_with(
            user_id=self.user.pk, purpose="delivery", nonce=pending.nonce
        )

    def test_unauthenticated_is_401_for_all_three_endpoints(self) -> None:
        anonymous = APIClient()
        self.assertEqual(anonymous.post(self.url, {"email": "a@b.co"}, format="json").status_code, 401)
        self.assertEqual(anonymous.post(self.resend_url).status_code, 401)
        self.assertEqual(anonymous.delete(self.pending_url).status_code, 401)

    def test_lapsed_user_is_402_on_request_and_resend_but_can_cancel(self) -> None:
        self.request_change()
        self.lapse()
        self.mock_confirm_delay.reset_mock()

        self.assertEqual(self.request_change("other@example.com").status_code, 402)
        self.assertEqual(self.request_change("other@example.com").json()["code"], "subscription_required")
        self.assertEqual(self.api.post(self.resend_url).status_code, 402)
        self.assertEqual(self.api.delete(self.pending_url).status_code, 204)
        self.assertIsNone(self.pending(purpose="delivery"))
        self.mock_confirm_delay.assert_not_called()

    def test_invalid_email_is_400(self) -> None:
        for bad in ("", "nope", "a@", "a b@example.com", None, 5, ["a@b.co"], "x@example.com\nBcc: y@z.co"):
            with self.subTest(email=bad):
                cache.clear()
                RecipientChangeAttempt.objects.all().delete()
                response = self.request_change(bad)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "invalid_email")
        self.assertIsNone(self.pending(purpose="delivery"))

    def test_wrong_or_missing_password_stores_and_sends_nothing(self) -> None:
        for password in ("wrong-password", None, ""):
            with self.subTest(password=password):
                cache.clear()
                response = self.request_change(password=password)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "invalid_password")
        self.assertIsNone(self.pending(purpose="delivery"))
        self.mock_confirm_delay.assert_not_called()

    def test_password_requirement_is_one_switch(self) -> None:
        with patch.object(recipient, "DELIVERY_CHANGE_REQUIRES_PASSWORD", False):
            response = self.request_change(password=None)
        self.assertEqual(response.status_code, 202)

    def test_same_as_current_is_case_insensitive_and_sends_nothing(self) -> None:
        response = self.request_change("  ALICE@example.com")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "same_as_current")
        self.assertIsNone(self.pending(purpose="delivery"))
        self.mock_confirm_delay.assert_not_called()

    def test_account_email_still_needs_the_link_when_it_is_not_the_current_address(self) -> None:
        FragranceConfig.objects.filter(pk=self.config.pk).update(recipient_email="elsewhere@example.com")

        response = self.request_change("alice@example.com")

        self.assertEqual(response.status_code, 202)
        self.config.refresh_from_db()
        self.assertEqual(self.config.recipient_email, "elsewhere@example.com")

    def test_blank_recipient_falls_back_to_the_account_email_for_the_comparison(self) -> None:
        FragranceConfig.objects.filter(pk=self.config.pk).update(recipient_email="")

        self.assertEqual(self.request_change("alice@example.com").json()["code"], "same_as_current")

    def test_a_new_request_replaces_the_pending_one_and_retires_the_old_link(self) -> None:
        self.request_change("first@example.com")
        first_token = self.token_for(purpose="delivery")
        first_nonce = self.pending(purpose="delivery").nonce

        self.clear_gap(purpose="delivery")
        self.request_change("second@example.com")

        pending = self.pending(purpose="delivery")
        assert pending is not None
        self.assertEqual(pending.address, "second@example.com")
        self.assertNotEqual(pending.nonce, first_nonce)
        self.assertEqual(PendingEmailChange.objects.filter(user=self.user).count(), 1)
        result = recipient.confirm_change(token=first_token)
        self.assertEqual(result.outcome, "superseded")

    def test_broker_failure_is_503_and_lifts_the_resend_gap(self) -> None:
        self.mock_confirm_delay.side_effect = OperationalError("broker down")

        with self.assertLogs("fragrance.recipient", level="ERROR"):
            response = self.request_change()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "send_unavailable")
        pending = self.pending(purpose="delivery")
        assert pending is not None
        self.assertIsNone(pending.last_sent_at)
        self.mock_confirm_delay.side_effect = None
        self.assertEqual(self.api.post(self.resend_url).status_code, 202)

    def test_sixth_request_in_an_hour_is_429_with_retry_after(self) -> None:
        for i in range(5):
            self.clear_gap(purpose="delivery")
            self.assertEqual(self.request_change(f"n{i}@example.com").status_code, 202)
        self.clear_gap(purpose="delivery")

        response = self.request_change("n6@example.com")

        self.assertEqual(response.status_code, 429)
        body = response.json()
        self.assertEqual(body["code"], "rate_limited")
        self.assertIsInstance(body["retry_after"], int)
        self.assertGreaterEqual(body["retry_after"], 1)
        self.assertTrue(response.headers.get("Retry-After"))

    def test_wrong_passwords_spend_the_change_budget(self) -> None:
        for _ in range(5):
            self.assertEqual(self.request_change(password="nope").status_code, 400)

        self.assertEqual(self.request_change().status_code, 429)

    def test_throttle_is_per_user(self) -> None:
        for _ in range(5):
            self.request_change(password="nope")
        other = self.make_user(username="carol")
        client = APIClient()
        client.force_authenticate(user=other)

        self.assertEqual(self.request_change("c@example.com", client=client).status_code, 202)

    def test_staff_account_may_change_without_a_subscription_row(self) -> None:
        staff = User.objects.create_user(username="ops", email="ops@example.com", password=PASSWORD, is_staff=True)
        client = APIClient()
        client.force_authenticate(user=staff)

        response = self.request_change("ops2@example.com", client=client)

        self.assertEqual(response.status_code, 202)

    def test_never_touches_the_account_email_or_password(self) -> None:
        before = User.objects.get(pk=self.user.pk).password
        self.request_change("new@example.com")
        user = User.objects.get(pk=self.user.pk)
        self.assertEqual(user.email, "alice@example.com")
        self.assertEqual(user.password, before)


class ResendTests(_DeliveryBase):
    def test_no_pending_is_409(self) -> None:
        response = self.api.post(self.resend_url)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "no_pending")

    def test_second_resend_inside_the_gap_is_429_resend_too_soon(self) -> None:
        self.request_change()

        response = self.api.post(self.resend_url)

        self.assertEqual(response.status_code, 429)
        body = response.json()
        self.assertEqual(body["code"], "resend_too_soon")
        self.assertTrue(1 <= body["retry_after"] <= 60)

    def test_resend_after_the_gap_keeps_the_nonce_and_extends_expiry(self) -> None:
        self.request_change()
        pending = self.pending(purpose="delivery")
        assert pending is not None
        nonce = pending.nonce
        PendingEmailChange.objects.filter(pk=pending.pk).update(
            last_sent_at=timezone.now() - timedelta(minutes=2),
            expires_at=timezone.now() + timedelta(hours=2),
        )
        self.mock_confirm_delay.reset_mock()

        response = self.api.post(self.resend_url)

        self.assertEqual(response.status_code, 202)
        refreshed = self.pending(purpose="delivery")
        assert refreshed is not None
        self.assertEqual(refreshed.nonce, nonce)
        self.assertGreater(refreshed.expires_at, timezone.now() + timedelta(hours=23))
        self.mock_confirm_delay.assert_called_once_with(
            user_id=self.user.pk, purpose="delivery", nonce=nonce
        )

    def test_resend_works_on_an_expired_pending_change(self) -> None:
        self.request_change()
        self.age_pending(purpose="delivery")
        before = self.api.get(reverse("fragrance-config")).json()
        self.assertEqual(before["pending_state"], "expired")

        response = self.api.post(self.resend_url)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["pending_state"], "pending")

    def test_hourly_resend_cap(self) -> None:
        self.request_change()
        for _ in range(5):
            self.age_pending(purpose="delivery", hours=1)
            self.assertEqual(self.api.post(self.resend_url).status_code, 202)
        self.age_pending(purpose="delivery", hours=1)

        response = self.api.post(self.resend_url)

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "rate_limited")

    def test_cannot_resend_another_users_pending_change(self) -> None:
        other = self.make_user(username="carol")
        client = APIClient()
        client.force_authenticate(user=other)
        self.request_change("mine@example.com")
        self.mock_confirm_delay.reset_mock()

        response = client.post(self.resend_url)

        self.assertEqual(response.status_code, 409)
        self.mock_confirm_delay.assert_not_called()


class CancelTests(_DeliveryBase):
    def test_cancel_clears_pending_and_is_idempotent(self) -> None:
        self.request_change()
        token = self.token_for(purpose="delivery")

        self.assertEqual(self.api.delete(self.pending_url).status_code, 204)
        self.assertEqual(self.api.delete(self.pending_url).status_code, 204)

        self.assertIsNone(self.pending(purpose="delivery"))
        self.assertEqual(recipient.confirm_change(token=token).outcome, "superseded")

    def test_cancel_leaves_other_users_and_the_receipt_change_alone(self) -> None:
        other = self.make_user(username="carol")
        other_client = APIClient()
        other_client.force_authenticate(user=other)
        self.request_change("mine@example.com", client=other_client)
        recipient.start_change(user=self.user, purpose="receipt", address="r@example.com")
        self.request_change("mine2@example.com")

        self.api.delete(self.pending_url)

        self.assertIsNotNone(self.pending(purpose="delivery", user=other))
        self.assertIsNotNone(self.pending(purpose="receipt"))
        self.assertIsNone(self.pending(purpose="delivery"))


class ConfigContractTests(_DeliveryBase):
    def setUp(self) -> None:
        super().setUp()
        self.config_url = reverse("fragrance-config")

    def test_get_without_pending(self) -> None:
        body = self.api.get(self.config_url).json()

        self.assertEqual(body["recipient_email"], "alice@example.com")
        self.assertIsNone(body["pending_recipient_email"])
        self.assertEqual(body["pending_state"], "none")
        self.assertIsNone(body["pending_expires_at"])
        self.assertIsNone(body["resend_available_at"])

    def test_get_with_pending_and_expired(self) -> None:
        self.request_change("new@example.com")
        body = self.api.get(self.config_url).json()
        self.assertEqual(body["pending_recipient_email"], "new@example.com")
        self.assertEqual(body["pending_state"], "pending")
        self.assertEqual(body["recipient_email"], "alice@example.com")

        self.age_pending(purpose="delivery")
        body = self.api.get(self.config_url).json()
        self.assertEqual(body["pending_state"], "expired")
        self.assertIsNone(body["resend_available_at"])

    def test_get_never_shows_another_users_pending_change(self) -> None:
        other = self.make_user(username="carol")
        recipient.start_change(user=other, purpose="delivery", address="secret@example.com")

        body = self.api.get(self.config_url).json()

        self.assertIsNone(body["pending_recipient_email"])
        self.assertNotIn("secret@example.com", str(body))

    def test_blank_recipient_is_reported_as_the_account_email(self) -> None:
        FragranceConfig.objects.filter(pk=self.config.pk).update(recipient_email="")

        self.assertEqual(self.api.get(self.config_url).json()["recipient_email"], "alice@example.com")

    def test_patch_with_a_different_recipient_is_400_and_changes_nothing(self) -> None:
        for method in (self.api.patch, self.api.put):
            with self.subTest(method=method.__name__):
                response = method(
                    self.config_url, {"recipient_email": "evil@example.com", "run_hour": 7}, format="json"
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "recipient_email_read_only")
        self.config.refresh_from_db()
        self.assertEqual(self.config.recipient_email, "alice@example.com")
        self.assertNotEqual(self.config.run_hour, 7)

    def test_patch_with_the_equal_recipient_is_tolerated(self) -> None:
        response = self.api.patch(
            self.config_url, {"recipient_email": "ALICE@example.com", "run_hour": 7}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.config.refresh_from_db()
        self.assertEqual(self.config.run_hour, 7)
        self.assertEqual(self.config.recipient_email, "alice@example.com")

    def test_patch_schedule_only_still_works(self) -> None:
        response = self.api.patch(self.config_url, {"run_hour": 5}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["run_hour"], 5)


class AdminEditTests(_DeliveryBase):
    def _save(self, *, changed: list[str]) -> None:
        from django.contrib import admin
        from fragrance.admin import FragranceConfigAdmin

        model_admin = FragranceConfigAdmin(FragranceConfig, admin.site)
        self.config.recipient_email = "ops-set@example.com"
        model_admin.save_model(
            request=None,
            obj=self.config,
            form=SimpleNamespace(changed_data=changed),
            change=True,
        )

    def test_admin_edit_of_the_address_is_accepted_and_clears_pending(self) -> None:
        self.request_change("pending@example.com")

        self._save(changed=["recipient_email"])

        self.config.refresh_from_db()
        self.assertEqual(self.config.recipient_email, "ops-set@example.com")
        self.assertIsNone(self.pending(purpose="delivery"))

    def test_admin_edit_of_something_else_keeps_pending(self) -> None:
        self.request_change("pending@example.com")

        self._save(changed=["run_hour"])

        self.assertIsNotNone(self.pending(purpose="delivery"))

    def test_admin_edit_does_not_clear_the_receipt_change(self) -> None:
        recipient.start_change(user=self.user, purpose="receipt", address="r@example.com")

        self._save(changed=["recipient_email"])

        self.assertIsNotNone(self.pending(purpose="receipt"))

    def test_admin_save_deletes_pending_before_updating_the_config(self) -> None:
        self.request_change("pending@example.com")
        order: list[str] = []
        from django.contrib.admin import ModelAdmin

        def _record_save(*args, **kwargs) -> None:
            order.append("config_save")
            self.assertIsNone(self.pending(purpose="delivery"))

        with patch.object(ModelAdmin, "save_model", side_effect=_record_save):
            self._save(changed=["recipient_email"])

        self.assertEqual(order, ["config_save"])


class ConcurrentRequestTests(TransactionTestCase):
    def test_two_concurrent_first_requests_leave_one_consistent_row(self) -> None:
        user = User.objects.create_user(username="racer", email="racer@example.com", password=PASSWORD)
        active_subscription_for(user)
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def _request(address: str) -> None:
            from django.db import connection

            try:
                barrier.wait()
                recipient.start_change(user=user, purpose="delivery", address=address)
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(exc)
            finally:
                connection.close()

        threads = [
            threading.Thread(target=_request, args=(addr,))
            for addr in ("one@example.com", "two@example.com")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # The loser finds the winner's row inside the replace gap.
        self.assertEqual([type(error) for error in errors], [recipient.ResendTooSoonError])
        rows = PendingEmailChange.objects.filter(user=user, purpose="delivery")
        self.assertEqual(rows.count(), 1)
        self.assertIn(rows.get().address, {"one@example.com", "two@example.com"})
