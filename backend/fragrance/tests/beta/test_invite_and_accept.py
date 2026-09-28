from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from fragrance.models import BetaSignup
from fragrance.services import (
    BETA_INVITE_MAX_AGE,
    derive_unique_username,
    generate_beta_invite_token,
    verify_beta_invite_token,
)


class DeriveUniqueUsernameTests(TestCase):
    def test_simple_email_produces_local_part_username(self) -> None:
        self.assertEqual(derive_unique_username(email="ryan@example.com"), "ryan")

    def test_collision_appends_numeric_suffix(self) -> None:
        User.objects.create_user(username="ryan", email="ryan@gmail.com", password="pw12345!")
        self.assertEqual(derive_unique_username(email="ryan@example.com"), "ryan_2")

    def test_multiple_collisions_increment_suffix(self) -> None:
        User.objects.create_user(username="ryan", email="a@gmail.com", password="pw12345!")
        User.objects.create_user(username="ryan_2", email="b@example.com", password="pw12345!")
        self.assertEqual(derive_unique_username(email="ryan@another.com"), "ryan_3")

    def test_symbols_only_local_part_falls_back_to_user(self) -> None:
        self.assertEqual(derive_unique_username(email="+++@example.com"), "user")

    def test_two_distinct_addresses_reducing_to_the_same_local_part_get_distinct_usernames(
        self,
    ) -> None:
        first = derive_unique_username(email="ryan@gmail.com")
        User.objects.create_user(username=first, email="ryan@gmail.com", password="pw12345!")
        second = derive_unique_username(email="ryan@example.com")
        self.assertNotEqual(first, second)


class BetaInviteTokenTests(TestCase):
    def _signup_with_user(self) -> tuple[BetaSignup, User]:
        user = User.objects.create_user(username="invitee", email="invitee@example.com")
        signup = BetaSignup.objects.create(
            email="invitee@example.com",
            normalized_email="invitee@example.com",
            status="approved",
            invited_user=user,
        )
        return signup, user

    def test_round_trips(self) -> None:
        signup, user = self._signup_with_user()
        token = generate_beta_invite_token(signup=signup)
        payload = verify_beta_invite_token(token=token)
        self.assertEqual(payload["sid"], signup.id)
        self.assertEqual(payload["uid"], user.id)

    def test_tampered_token_rejected(self) -> None:
        signup, _ = self._signup_with_user()
        token = generate_beta_invite_token(signup=signup)
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        self.assertIsNone(verify_beta_invite_token(token=tampered))

    def test_expired_token_rejected(self) -> None:
        signup, _ = self._signup_with_user()
        token = generate_beta_invite_token(signup=signup)
        with patch("fragrance.services.BETA_INVITE_MAX_AGE", -1):
            self.assertIsNone(verify_beta_invite_token(token=token))

    def test_malformed_token_rejected(self) -> None:
        self.assertIsNone(verify_beta_invite_token(token="not-a-real-token"))

    def test_empty_token_rejected(self) -> None:
        self.assertIsNone(verify_beta_invite_token(token=""))

    def test_max_age_is_fourteen_days(self) -> None:
        self.assertEqual(BETA_INVITE_MAX_AGE, 60 * 60 * 24 * 14)


class BetaAcceptViewTests(TestCase):
    """
    Covers POST /api/v1/beta/accept/: token round-trip, single-use
    enforcement (an already-converted token must be rejected, distinctly
    from a never-valid one -- 409 "already_used" vs. 400
    "invalid_or_expired", per the operator-approved 2026-09-20 contract),
    expired/tampered tokens, and that validate_password() actually runs --
    slice 1's removed UserRegistrationSerializer skipped validators, and
    this path must not repeat that.
    """

    url = "/api/v1/beta/accept/"

    def _approved_signup(self, email: str = "invitee@example.com") -> tuple[BetaSignup, User]:
        user = User.objects.create_user(username=email.split("@")[0], email=email)
        signup = BetaSignup.objects.create(
            email=email, normalized_email=email, status="approved", invited_user=user
        )
        return signup, user

    def test_valid_token_sets_password_marks_converted_and_returns_jwt_pair(self) -> None:
        signup, user = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)

        response = self.client.post(self.url, {"token": token, "password": "Str0ng-Passw0rd!"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("access", body)
        self.assertIn("refresh", body)

        signup.refresh_from_db()
        self.assertEqual(signup.status, "converted")
        self.assertIsNotNone(signup.converted_at)
        user.refresh_from_db()
        self.assertTrue(user.check_password("Str0ng-Passw0rd!"))

    def test_already_converted_token_returns_409_already_used(self) -> None:
        signup, _ = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)
        first = self.client.post(self.url, {"token": token, "password": "Str0ng-Passw0rd!"})
        self.assertEqual(first.status_code, 200)

        second = self.client.post(self.url, {"token": token, "password": "An0ther-Passw0rd!"})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json(), {"code": "already_used"})

    def test_expired_token_returns_400_invalid_or_expired(self) -> None:
        signup, _ = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)
        with patch("fragrance.services.BETA_INVITE_MAX_AGE", -1):
            response = self.client.post(self.url, {"token": token, "password": "Str0ng-Passw0rd!"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"code": "invalid_or_expired"})

    def test_tampered_token_returns_400_invalid_or_expired(self) -> None:
        signup, _ = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        response = self.client.post(self.url, {"token": tampered, "password": "Str0ng-Passw0rd!"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"code": "invalid_or_expired"})

    def test_weak_password_is_rejected_by_validators(self) -> None:
        signup, user = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)
        response = self.client.post(self.url, {"token": token, "password": "12345678"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.json())
        signup.refresh_from_db()
        self.assertEqual(signup.status, "approved")
        user.refresh_from_db()
        self.assertFalse(user.has_usable_password())

    def test_pending_signup_token_returns_400_invalid_or_expired(self) -> None:
        user = User.objects.create_user(username="pending_invitee", email="pending@example.com")
        signup = BetaSignup.objects.create(
            email="pending@example.com",
            normalized_email="pending@example.com",
            status="pending",
            invited_user=user,
        )
        token = generate_beta_invite_token(signup=signup)
        response = self.client.post(self.url, {"token": token, "password": "Str0ng-Passw0rd!"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"code": "invalid_or_expired"})

    def test_missing_token_key_returns_400_with_field_required_error(self) -> None:
        # A wholly absent "token" key hits DRF's own required-field check
        # before resolve_beta_invite_signup runs, so this is the one 400
        # shape that is *not* {"code": "invalid_or_expired"} -- see
        # BetaAcceptSerializer.token's docstring. Not reachable from the
        # real frontend form, which always submits a string.
        response = self.client.post(self.url, {"password": "Str0ng-Passw0rd!"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("token", response.json())

    def test_blank_token_returns_400_invalid_or_expired(self) -> None:
        response = self.client.post(
            self.url, {"token": "", "password": "Str0ng-Passw0rd!"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"code": "invalid_or_expired"})

    def test_unauthenticated_access_is_allowed(self) -> None:
        signup, _ = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)
        response = self.client.post(self.url, {"token": token, "password": "Str0ng-Passw0rd!"})
        self.assertNotEqual(response.status_code, 401)
        self.assertFalse(User.objects.filter(username="sneaky").exists())


class BetaAcceptPreCheckViewTests(TestCase):
    """Covers GET /api/v1/beta/accept/?token=... -- the pre-submit validity check."""

    url = "/api/v1/beta/accept/"

    def _approved_signup(self, email: str = "precheck@example.com") -> tuple[BetaSignup, User]:
        user = User.objects.create_user(username=email.split("@")[0], email=email)
        signup = BetaSignup.objects.create(
            email=email, normalized_email=email, status="approved", invited_user=user
        )
        return signup, user

    def test_valid_token_returns_200_valid_true(self) -> None:
        signup, _ = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)
        response = self.client.get(self.url, {"token": token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"valid": True})

    def test_valid_token_is_not_consumed_by_the_precheck(self) -> None:
        signup, _ = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)
        self.client.get(self.url, {"token": token})
        signup.refresh_from_db()
        self.assertEqual(signup.status, "approved")

        response = self.client.post(self.url, {"token": token, "password": "Str0ng-Passw0rd!"})
        self.assertEqual(response.status_code, 200)

    def test_converted_token_returns_409_already_used(self) -> None:
        signup, _ = self._approved_signup()
        token = generate_beta_invite_token(signup=signup)
        self.client.post(self.url, {"token": token, "password": "Str0ng-Passw0rd!"})

        response = self.client.get(self.url, {"token": token})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"code": "already_used"})

    def test_bogus_token_returns_400_invalid_or_expired(self) -> None:
        response = self.client.get(self.url, {"token": "not-a-real-token"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"code": "invalid_or_expired"})

    def test_missing_token_returns_400_invalid_or_expired(self) -> None:
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"code": "invalid_or_expired"})

    def test_unauthenticated_access_is_allowed(self) -> None:
        response = self.client.get(self.url, {"token": "not-a-real-token"})
        self.assertNotEqual(response.status_code, 401)
