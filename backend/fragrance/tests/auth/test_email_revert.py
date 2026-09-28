import logging
from unittest.mock import patch

import stripe
from celery.exceptions import Retry
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache.backends.locmem import LocMemCache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from fragrance.models import StripeEvent, Subscription
from fragrance.services import (
    EMAIL_REVERT_MAX_AGE,
    generate_email_revert_token,
    verify_email_revert_token,
)
from fragrance.tasks import (
    process_stripe_event,
    restore_stripe_customer_email,
    send_account_email_change_notice,
    send_email_reverted_notice,
)
from fragrance.tests.support.bases import NEW, OLD, _RevertBase
from fragrance.tests.support.stripe_fakes import _customer
from fragrance.views import FailOpenScopedRateThrottle
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)
from rest_framework_simplejwt.tokens import RefreshToken


class TokenTests(TestCase):
    def test_round_trip(self) -> None:
        token = generate_email_revert_token(user_id=7, old_email=OLD, new_email=NEW)
        self.assertEqual(verify_email_revert_token(token=token), {"uid": 7, "old": OLD, "new": NEW})

    def test_max_age_is_seven_days(self) -> None:
        self.assertEqual(EMAIL_REVERT_MAX_AGE, 7 * 24 * 3600)

    def test_token_is_bound_to_its_own_salt(self) -> None:
        from django.core import signing

        foreign = signing.dumps({"uid": 1, "old": OLD, "new": NEW}, salt="beta-invite")
        self.assertIsNone(verify_email_revert_token(token=foreign))

    def test_non_strings_and_garbage_are_rejected(self) -> None:
        for bad in (None, "", 5, ["x"], {"a": 1}, "not-a-token", "a" * 5000):
            with self.subTest(bad=bad):
                self.assertIsNone(verify_email_revert_token(token=bad))


class RevertEndpointTests(_RevertBase):
    def test_success_restores_email_and_returns_200(self) -> None:
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertIn("detail", response.json())
        self.assertEqual(self._email(), OLD)

    def test_get_is_405_and_changes_nothing(self) -> None:
        response = self.client.get(self.url, {"token": self.token})
        self.assertEqual(response.status_code, 405)
        self.assertEqual(self._email(), NEW)

    def test_every_failure_mode_returns_the_identical_400(self) -> None:
        tampered = self.token[:-3] + ("aaa" if not self.token.endswith("aaa") else "bbb")
        gone = generate_email_revert_token(user_id=999999, old_email=OLD, new_email=NEW)
        not_current = generate_email_revert_token(user_id=self.user.pk, old_email=OLD, new_email="someone@else.example")
        cases = {
            "missing": None,
            "not_a_string": 12345,
            "malformed": "not-a-token",
            "bad_signature": tampered,
            "user_gone": gone,
            "email_not_new_address": not_current,
        }
        for name, token in cases.items():
            with self.subTest(case=name):
                if name == "missing":
                    response = self.client.post(self.url, {}, format="json")
                else:
                    response = self._post(token=token)
                self._assert_invalid(response)
        self.assertEqual(self._email(), NEW)

    def test_unparseable_body_gets_the_same_400(self) -> None:
        response = self.client.post(self.url, "{not json", content_type="application/json")
        self._assert_invalid(response)
        response = self.client.post(self.url, "[1, 2]", content_type="application/json")
        self._assert_invalid(response)

    def test_expired_token_is_rejected(self) -> None:
        with patch("fragrance.services.EMAIL_REVERT_MAX_AGE", -1):
            self._assert_invalid(self._post())
        self.assertEqual(self._email(), NEW)

    def test_single_use(self) -> None:
        self.assertEqual(self._post().status_code, 200)
        self._assert_invalid(self._post())
        self.assertEqual(self._email(), OLD)

    def test_stale_token_after_a_second_email_change(self) -> None:
        self.user.email = "second@change.example"
        self.user.save()
        self._assert_invalid(self._post())
        self.assertEqual(self._email(), "second@change.example")

    def test_case_insensitive_match_of_the_new_address(self) -> None:
        self.user.email = NEW.upper()
        self.user.save()
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(self._email(), OLD)

    def test_staff_and_superuser_cannot_be_reverted(self) -> None:
        for flag in ("is_staff", "is_superuser"):
            with self.subTest(flag=flag):
                setattr(self.user, flag, True)
                self.user.save()
                token = RefreshToken.for_user(self.user)
                self._assert_invalid(self._post())
                self.assertEqual(self._email(), NEW)
                self.assertFalse(BlacklistedToken.objects.filter(token__jti=token["jti"]).exists())
                setattr(self.user, flag, False)
                self.user.save()

    def test_anonymous_caller_and_junk_auth_header_are_fine(self) -> None:
        response = self._post(HTTP_AUTHORIZATION="Bearer garbage.token.value")
        self.assertEqual(response.status_code, 200)

    def test_password_is_not_touched(self) -> None:
        before = User.objects.get(pk=self.user.pk).password
        self._post()
        self.assertEqual(User.objects.get(pk=self.user.pk).password, before)
        self.assertTrue(User.objects.get(pk=self.user.pk).check_password("pw12345!"))

    def test_throttled_after_the_scope_limit(self) -> None:
        with patch.object(FailOpenScopedRateThrottle, "cache", LocMemCache("revert-throttle", {})), patch.object(
            FailOpenScopedRateThrottle, "THROTTLE_RATES", {"revert-email": "2/min"}
        ):
            statuses = [self._post(token="junk").status_code for _ in range(3)]
        self.assertEqual(statuses, [400, 400, 429])


class RefreshTokenRevocationTests(_RevertBase):
    def test_users_refresh_tokens_are_blacklisted_and_cannot_refresh(self) -> None:
        refreshes = [RefreshToken.for_user(self.user) for _ in range(2)]
        for refresh in refreshes:
            self.assertEqual(
                self.client.post(reverse("token_refresh"), {"refresh": str(refresh)}, format="json").status_code, 200
            )
        # Rotation minted successors; every outstanding token must now be dead.
        live = [RefreshToken.for_user(self.user) for _ in range(2)]

        self.assertEqual(self._post().status_code, 200)

        self.assertFalse(
            OutstandingToken.objects.filter(user=self.user, blacklistedtoken__isnull=True).exists()
        )
        for refresh in live:
            response = self.client.post(reverse("token_refresh"), {"refresh": str(refresh)}, format="json")
            self.assertEqual(response.status_code, 401)

    def test_other_users_tokens_are_untouched(self) -> None:
        other = User.objects.create_user(username="bob", email="bob@x.example", password="pw12345!")
        theirs = RefreshToken.for_user(other)
        self.assertEqual(self._post().status_code, 200)
        self.assertFalse(BlacklistedToken.objects.filter(token__user=other).exists())
        response = self.client.post(reverse("token_refresh"), {"refresh": str(theirs)}, format="json")
        self.assertEqual(response.status_code, 200)

    def test_already_blacklisted_tokens_do_not_break_the_revert(self) -> None:
        refresh = RefreshToken.for_user(self.user)
        refresh.blacklist()
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(BlacklistedToken.objects.filter(token__user=self.user).count(), 1)

    def test_failed_revert_leaves_sessions_alone(self) -> None:
        refresh = RefreshToken.for_user(self.user)
        self._assert_invalid(self._post(token="junk"))
        self.assertFalse(BlacklistedToken.objects.filter(token__user=self.user).exists())
        response = self.client.post(reverse("token_refresh"), {"refresh": str(refresh)}, format="json")
        self.assertEqual(response.status_code, 200)


class DuplicateAddressTests(_RevertBase):
    def test_conflict_keeps_email_but_still_revokes_sessions_and_returns_200(self) -> None:
        User.objects.create_user(username="carol", email=OLD.upper(), password="pw12345!")
        refresh = RefreshToken.for_user(self.user)

        with self.assertLogs("fragrance.billing", level=logging.WARNING) as logs:
            response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertIn("detail", response.json())
        self.assertEqual(self._email(), NEW)
        self.assertEqual(User.objects.filter(email__iexact=OLD).count(), 1)
        self.assertTrue(BlacklistedToken.objects.filter(token__jti=refresh["jti"]).exists())
        self.mock_restore.assert_not_called()
        self.mock_notice.assert_not_called()
        joined = "\n".join(logs.output).lower()
        self.assertNotIn("old.example", joined)
        self.assertIn(str(self.user.pk), joined)

    def test_conflict_response_body_matches_the_success_body(self) -> None:
        success = self._post().json()
        self.user.email = NEW
        self.user.save()
        User.objects.create_user(username="carol", email=OLD, password="pw12345!")
        self.assertEqual(self._post().json(), success)


class StripePushAndNoticeTests(_RevertBase):
    def test_stripe_restore_is_queued_with_ids_and_the_old_email(self) -> None:
        self.assertEqual(self._post().status_code, 200)
        self.mock_restore.assert_called_once_with(user_id=self.user.pk, customer_id="cus_alice", email=OLD)
        self.mock_modify.assert_not_called()

    def test_stripe_restore_queue_failure_does_not_fail_the_request(self) -> None:
        self.mock_restore.side_effect = OSError("broker down secret-detail")
        with self.assertLogs("fragrance.billing", level=logging.ERROR) as logs:
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._email(), OLD)
        self.mock_notice.assert_called_once()
        joined = "\n".join(logs.output)
        self.assertNotIn("secret-detail", joined)
        self.assertNotIn("old.example", joined)

    def test_user_without_a_stripe_customer_skips_the_push(self) -> None:
        Subscription.objects.filter(user=self.user).update(stripe_customer_id="")
        self.assertEqual(self._post().status_code, 200)
        self.mock_restore.assert_not_called()

    def test_broker_failure_does_not_fail_the_request(self) -> None:
        self.mock_notice.side_effect = OSError("broker down")
        with self.assertLogs("fragrance.billing", level=logging.ERROR):
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._email(), OLD)

    def test_confirmation_notice_is_queued_for_the_restored_address_only(self) -> None:
        self._post()
        self.mock_notice.assert_called_once_with(user_id=self.user.pk, recipient=OLD)

    def test_failed_revert_queues_and_pushes_nothing(self) -> None:
        self._post(token="junk")
        self.mock_notice.assert_not_called()
        self.mock_restore.assert_not_called()

    def test_customer_updated_after_the_push_is_a_noop(self) -> None:
        self._post()
        with patch("fragrance.tasks.send_account_email_change_notice.delay") as delay:
            StripeEvent.objects.create(
                stripe_event_id="evt_echo",
                event_type="customer.updated",
                stripe_created=timezone.now(),
                payload={
                    "id": "evt_echo",
                    "type": "customer.updated",
                    "data": {
                        "object": {"id": "cus_alice", "object": "customer", "email": OLD},
                        "previous_attributes": {"email": NEW},
                    },
                },
            )
            process_stripe_event(stripe_event_id="evt_echo")
        delay.assert_not_called()
        self.assertEqual(self._email(), OLD)
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_echo").status, "processed")

    def test_attackers_password_reset_token_dies_with_the_email_change_back(self) -> None:
        attacker_token = default_token_generator.make_token(user=self.user)
        self.assertTrue(default_token_generator.check_token(user=self.user, token=attacker_token))
        self._post()
        self.user.refresh_from_db()
        self.assertFalse(default_token_generator.check_token(user=self.user, token=attacker_token))


class NoticeLinkTests(TestCase):
    """The link exists in the old-address notice only, and sync mints it."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="alice", email=NEW, password="pw12345!")

    def _send(self, *, recipient: str, is_new_address: bool, revert_token: str = "") -> None:
        kwargs = {"revert_token": revert_token} if revert_token else {}
        send_account_email_change_notice(
            user_id=self.user.pk, recipient=recipient, old_email=OLD, new_email=NEW,
            is_new_address=is_new_address, **kwargs,
        )

    @override_settings(FRONTEND_URL="https://app.example.test", SUPPORT_EMAIL="help@example.test")
    def test_old_address_notice_contains_the_link_and_guidance(self) -> None:
        self._send(recipient=OLD, is_new_address=False, revert_token="TOKEN123")
        (message,) = mail.outbox
        link = "https://app.example.test/auth/revert-email?token=TOKEN123"
        self.assertIn(link, message.body)
        self.assertIn(link, message.alternatives[0][0])
        for text in (message.body, message.alternatives[0][0]):
            self.assertIn("wasn't you", text)
            self.assertIn("help@example.test", text)
            self.assertIn("reset your password", text.lower())
        self.assertIn("Password reset emails will now go to the new address", message.body)

    @override_settings(FRONTEND_URL="https://app.example.test")
    def test_new_address_confirmation_never_contains_the_link_even_if_given_a_token(self) -> None:
        self._send(recipient=NEW, is_new_address=True, revert_token="TOKEN123")
        (message,) = mail.outbox
        for text in (message.body, message.alternatives[0][0]):
            self.assertNotIn("revert-email", text)
            self.assertNotIn("TOKEN123", text)
            self.assertNotIn("http", text)

    def test_old_address_notice_without_a_token_still_names_support(self) -> None:
        self._send(recipient=OLD, is_new_address=False)
        (message,) = mail.outbox
        self.assertNotIn("revert-email", message.body)
        self.assertIn("contact us right away", message.body)

    @override_settings(FRONTEND_URL="https://app.example.test", SUPPORT_EMAIL="help@example.test")
    def test_restored_notice_tells_owner_to_reset_password(self) -> None:
        send_email_reverted_notice(user_id=self.user.pk, recipient=OLD)
        (message,) = mail.outbox
        self.assertEqual(message.to, [OLD])
        forgot = "https://app.example.test/auth/forgot-password"
        for text in (message.body, message.alternatives[0][0]):
            self.assertIn(forgot, text)
            self.assertIn("restored", text)
            self.assertIn("signed out", text)
            self.assertNotIn("token", text.lower())

    def test_restored_notice_for_a_deleted_user_sends_nothing(self) -> None:
        send_email_reverted_notice(user_id=999999, recipient=OLD)
        self.assertEqual(mail.outbox, [])

    def test_sync_mints_a_valid_token_for_the_old_address_notice_only(self) -> None:
        user = User.objects.create_user(username="dave", email="dave@old.example", password="pw12345!")
        Subscription.objects.create(user=user, stripe_customer_id="cus_dave")
        StripeEvent.objects.create(
            stripe_event_id="evt_mint",
            event_type="customer.updated",
            stripe_created=timezone.now(),
            payload={
                "id": "evt_mint",
                "type": "customer.updated",
                "data": {
                    "object": {"id": "cus_dave", "object": "customer", "email": "dave@new.example"},
                    "previous_attributes": {"email": "dave@old.example"},
                },
            },
        )
        with patch("fragrance.tasks.send_account_email_change_notice.delay") as delay:
            process_stripe_event(stripe_event_id="evt_mint")
        by_recipient = {c.kwargs["recipient"]: c.kwargs for c in delay.call_args_list}
        self.assertNotIn("revert_token", by_recipient["dave@new.example"])
        payload = verify_email_revert_token(token=by_recipient["dave@old.example"]["revert_token"])
        self.assertEqual(payload, {"uid": user.pk, "old": "dave@old.example", "new": "dave@new.example"})


class RestoreStripeCustomerEmailTaskTests(_RevertBase):
    def setUp(self) -> None:
        super().setUp()
        # Post-revert state: the DB holds the restored address, Stripe the attacker's.
        self.user.email = OLD
        self.user.save()

    def _run(self, *, retries: int = 0, email: str = OLD, customer_id: str = "cus_alice") -> None:
        restore_stripe_customer_email.push_request(retries=retries)
        try:
            restore_stripe_customer_email(user_id=self.user.pk, customer_id=customer_id, email=email)
        finally:
            restore_stripe_customer_email.pop_request()

    def test_modifies_stripe_when_customer_still_has_the_attacker_address(self) -> None:
        self._run()
        self.mock_retrieve.assert_called_once_with("cus_alice")
        self.mock_modify.assert_called_once_with("cus_alice", params={"email": OLD})

    def test_skips_when_stripe_already_matches_case_insensitively(self) -> None:
        self.mock_retrieve.return_value = _customer(email=OLD.upper())
        self._run()
        self.mock_modify.assert_not_called()

    def test_skips_when_user_email_changed_since(self) -> None:
        self.user.email = "later@legit.example"
        self.user.save()
        with self.assertLogs("fragrance.tasks", level=logging.INFO) as logs:
            self._run()
        self.mock_retrieve.assert_not_called()
        self.mock_modify.assert_not_called()
        self.assertNotIn("legit.example", "\n".join(logs.output))

    def test_skips_for_deleted_user_and_foreign_customer(self) -> None:
        self._run(customer_id="cus_someone_else")
        self.mock_modify.assert_not_called()
        user_id = self.user.pk
        self.user.delete()
        restore_stripe_customer_email(user_id=user_id, customer_id="cus_alice", email=OLD)
        self.mock_modify.assert_not_called()

    def test_stripe_error_schedules_a_300s_retry_and_logs_sanitized_ids_only(self) -> None:
        self.mock_modify.side_effect = stripe.APIConnectionError("boom secret-detail")
        with patch.object(restore_stripe_customer_email, "retry", side_effect=Retry()) as retry:
            with self.assertLogs("fragrance.tasks", level=logging.ERROR) as logs:
                with self.assertRaises(Retry):
                    self._run()
        self.assertEqual(retry.call_args.kwargs["countdown"], 300)
        joined = "\n".join(logs.output)
        self.assertNotIn("secret-detail", joined)
        self.assertNotIn("old.example", joined)
        self.assertNotIn("evil.example", joined)
        self.assertIn("cus_alice", joined)

    def test_retrieve_error_also_retries(self) -> None:
        self.mock_retrieve.side_effect = stripe.APIConnectionError("boom")
        with patch.object(restore_stripe_customer_email, "retry", side_effect=Retry()) as retry:
            with self.assertRaises(Retry):
                self._run()
        retry.assert_called_once()

    def test_stops_retrying_at_max_retries(self) -> None:
        self.mock_modify.side_effect = stripe.APIConnectionError("boom")
        with patch.object(restore_stripe_customer_email, "retry") as retry:
            with self.assertRaises(stripe.APIConnectionError):
                self._run(retries=restore_stripe_customer_email.max_retries)
        retry.assert_not_called()
        self.assertEqual(restore_stripe_customer_email.max_retries, 5)

    def test_unconfigured_stripe_retries_without_calling_stripe(self) -> None:
        with override_settings(STRIPE_SECRET_KEY=""):
            with patch.object(restore_stripe_customer_email, "retry", side_effect=Retry()):
                with self.assertRaises(Retry):
                    self._run()
        self.mock_modify.assert_not_called()

    def test_our_own_restore_echo_is_a_noop_for_the_sync(self) -> None:
        # customer.updated produced by the restore: previous.email = attacker, email = OLD = User.email.
        with patch("fragrance.tasks.send_account_email_change_notice.delay") as delay:
            StripeEvent.objects.create(
                stripe_event_id="evt_restore_echo",
                event_type="customer.updated",
                stripe_created=timezone.now(),
                payload={
                    "id": "evt_restore_echo",
                    "type": "customer.updated",
                    "data": {
                        "object": {"id": "cus_alice", "object": "customer", "email": OLD},
                        "previous_attributes": {"email": NEW},
                    },
                },
            )
            process_stripe_event(stripe_event_id="evt_restore_echo")
        delay.assert_not_called()
        self.assertEqual(self._email(), OLD)
