"""Password change/reset end every session atomically; a blacklist failure fails closed (503), never a silent 200."""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.db import DatabaseError
from django.test import TestCase
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from fragrance import billing
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken

CHANGE_URL = "/api/v1/auth/password/change/"
RESET_URL = "/api/v1/auth/password/reset/confirm/"
REFRESH_URL = "/api/v1/auth/token/refresh/"
OLD_PASSWORD = "old-Pa55word-xyz"
NEW_PASSWORD = "n3w-Pa55word-qrs-91"


def _refresh_status(*, refresh: str) -> int:
    return APIClient().post(REFRESH_URL, {"refresh": refresh}, format="json").status_code


class ChangePasswordSessionTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", email="dave@example.test", password=OLD_PASSWORD)
        self.other = User.objects.create_user(username="frank", email="frank@example.test", password="fr4nk-Pa55word")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.payload = {
            "current_password": OLD_PASSWORD,
            "new_password": NEW_PASSWORD,
            "new_password_confirm": NEW_PASSWORD,
        }

    def test_unauthenticated_is_401(self) -> None:
        self.assertEqual(APIClient().post(CHANGE_URL, self.payload, format="json").status_code, 401)

    def test_success_ends_every_other_session_and_returns_a_fresh_pair(self) -> None:
        laptop = str(RefreshToken.for_user(self.user))
        phone = str(RefreshToken.for_user(self.user))
        franks = str(RefreshToken.for_user(self.other))

        response = self.client.post(CHANGE_URL, self.payload, format="json")

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(set(body), {"detail", "access", "refresh"})
        self.assertEqual(body["detail"], "Password changed successfully.")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertFalse(self.user.check_password(OLD_PASSWORD))
        self.assertEqual(_refresh_status(refresh=laptop), 401)
        self.assertEqual(_refresh_status(refresh=phone), 401)
        self.assertEqual(_refresh_status(refresh=franks), 200)
        self.assertEqual(_refresh_status(refresh=body["refresh"]), 200)
        authed = APIClient()
        authed.credentials(HTTP_AUTHORIZATION=f"Bearer {body['access']}")
        self.assertEqual(authed.get("/api/v1/billing/subscription/").status_code, 200)

    def test_the_legacy_refresh_field_is_accepted_and_changes_nothing_about_the_outcome(self) -> None:
        sent = str(RefreshToken.for_user(self.user))
        elsewhere = str(RefreshToken.for_user(self.user))

        response = self.client.post(CHANGE_URL, {**self.payload, "refresh": sent}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(_refresh_status(refresh=sent), 401)
        self.assertEqual(_refresh_status(refresh=elsewhere), 401)

    def test_wrong_current_password_changes_nothing(self) -> None:
        token = str(RefreshToken.for_user(self.user))

        response = self.client.post(CHANGE_URL, {**self.payload, "current_password": "nope"}, format="json")

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(OLD_PASSWORD))
        self.assertEqual(_refresh_status(refresh=token), 200)

    def test_blacklist_failure_fails_closed_and_rolls_the_password_back(self) -> None:
        token = str(RefreshToken.for_user(self.user))

        with patch.object(billing, "blacklist_all_refresh_tokens", side_effect=DatabaseError("boom")):
            with self.assertLogs("fragrance.views", level="ERROR") as logs:
                response = self.client.post(CHANGE_URL, self.payload, format="json")

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("access", response.json())
        self.assertNotIn("refresh", response.json())
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(OLD_PASSWORD))
        self.assertFalse(self.user.check_password(NEW_PASSWORD))
        self.assertEqual(_refresh_status(refresh=token), 200)
        self.assertIn(str(self.user.pk), "\n".join(logs.output))
        self.assertNotIn(NEW_PASSWORD, "\n".join(logs.output))

    def test_token_mint_failure_rolls_everything_back(self) -> None:
        token = str(RefreshToken.for_user(self.user))

        with patch("fragrance.views.RefreshToken.for_user", side_effect=RuntimeError("mint failed")):
            response = self.client.post(CHANGE_URL, self.payload, format="json")

        self.assertEqual(response.status_code, 503)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(OLD_PASSWORD))
        self.assertFalse(BlacklistedToken.objects.filter(token__user=self.user).exists())
        self.assertEqual(_refresh_status(refresh=token), 200)


class PasswordResetConfirmSessionTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="dave", email="dave@example.test", password=OLD_PASSWORD)
        self.other = User.objects.create_user(username="frank", email="frank@example.test", password="fr4nk-Pa55word")
        self.client = APIClient()
        self._payload()

    def _payload(self) -> None:
        self.payload = {
            "uid": urlsafe_base64_encode(force_bytes(self.user.pk)),
            "token": default_token_generator.make_token(user=self.user),
            "new_password": NEW_PASSWORD,
            "new_password_confirm": NEW_PASSWORD,
        }

    def test_success_ends_every_session_and_returns_no_tokens(self) -> None:
        laptop = str(RefreshToken.for_user(self.user))
        phone = str(RefreshToken.for_user(self.user))
        franks = str(RefreshToken.for_user(self.other))

        response = self.client.post(RESET_URL, self.payload, format="json")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(set(response.json()), {"detail"})
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        self.assertEqual(_refresh_status(refresh=laptop), 401)
        self.assertEqual(_refresh_status(refresh=phone), 401)
        self.assertEqual(_refresh_status(refresh=franks), 200)

    def test_link_is_single_use(self) -> None:
        self.assertEqual(self.client.post(RESET_URL, self.payload, format="json").status_code, 200)
        self.assertEqual(self.client.post(RESET_URL, self.payload, format="json").status_code, 400)

    def test_blacklist_failure_fails_closed_and_keeps_the_link_usable(self) -> None:
        token = str(RefreshToken.for_user(self.user))

        with patch.object(billing, "blacklist_all_refresh_tokens", side_effect=DatabaseError("boom")):
            with self.assertLogs("fragrance.views", level="ERROR") as logs:
                response = self.client.post(RESET_URL, self.payload, format="json")

        self.assertEqual(response.status_code, 503)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(OLD_PASSWORD))
        self.assertEqual(_refresh_status(refresh=token), 200)
        self.assertIn(str(self.user.pk), "\n".join(logs.output))
        # The reset link is still valid, so the user can simply retry.
        self.assertEqual(self.client.post(RESET_URL, self.payload, format="json").status_code, 200)

    def test_account_with_an_unusable_password_can_reset(self) -> None:
        self.user.set_unusable_password()
        self.user.save()
        self._payload()

        response = self.client.post(RESET_URL, self.payload, format="json")

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
