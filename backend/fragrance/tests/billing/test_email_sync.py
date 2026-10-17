"""
customer.updated is log-only: Stripe is never an authority for the account (login) email.
The legacy change-notice task is kept only so notices queued before the cutover still render.
"""

import logging
from unittest.mock import patch

from celery.exceptions import Retry
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from fragrance.models import StripeEvent, Subscription
from fragrance.tasks import process_stripe_event, send_account_email_change_notice
from fragrance.tests.support.bases import _EmailSyncBase
from fragrance.tests.support.stripe_fakes import _store_customer_updated


class CustomerUpdatedIsLogOnlyTests(_EmailSyncBase):
    def _assert_untouched(self) -> None:
        self.assertEqual(self._email(), "alice@old.example")
        self.mock_delay.assert_not_called()

    def test_changed_stripe_email_does_not_touch_the_account(self) -> None:
        with self.assertLogs("fragrance.billing", level=logging.INFO) as logs:
            self._process(event_id="evt_1", email="alice@new.example")

        self._assert_untouched()
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_1").status, "processed")
        joined = "\n".join(logs.output)
        self.assertIn("cus_alice", joined)
        self.assertIn("no account changes", joined)
        self.assertNotIn("alice@new.example", joined)
        self.assertNotIn("alice@old.example", joined)

    def test_every_shape_of_event_is_inert(self) -> None:
        cases = {
            "email_changed": ("attacker@evil.example", {"email": "alice@old.example"}),
            "previous_absent": ("attacker@evil.example", None),
            "previous_empty": ("attacker@evil.example", {}),
            "name_only": ("attacker@evil.example", {"name": "Old Name"}),
            "email_cleared": (None, {"email": "alice@old.example"}),
            "invalid_email": ("not-an-email", {"email": "alice@old.example"}),
        }
        for name, (email, previous) in cases.items():
            with self.subTest(case=name):
                _store_customer_updated(
                    event_id=f"evt_{name}", customer_id="cus_alice", email=email, previous=previous
                )
                with self.assertLogs("fragrance.billing", level=logging.INFO) as logs:
                    process_stripe_event(stripe_event_id=f"evt_{name}")
                self.assertEqual(StripeEvent.objects.get(stripe_event_id=f"evt_{name}").status, "processed")
                self.assertNotIn("evil.example", "\n".join(logs.output).lower())
        self._assert_untouched()

    def test_staff_and_unknown_customers_are_equally_inert(self) -> None:
        self.user.is_staff = True
        self.user.save()
        self._process(event_id="evt_staff", email="admin@new.example")
        self._process(event_id="evt_unk", email="x@new.example", customer_id="cus_nobody")
        self._assert_untouched()
        for event_id in ("evt_staff", "evt_unk"):
            self.assertEqual(StripeEvent.objects.get(stripe_event_id=event_id).status, "processed")

    def test_user_without_an_email_is_not_given_one(self) -> None:
        self.user.email = ""
        self.user.save()
        self._process(event_id="evt_blank", email="alice@new.example", previous={"email": None})
        self.assertEqual(self._email(), "")
        self.mock_delay.assert_not_called()

    def test_replay_is_idempotent(self) -> None:
        self._process(event_id="evt_a", email="alice@new.example")
        StripeEvent.objects.filter(stripe_event_id="evt_a").update(status="failed")
        process_stripe_event(stripe_event_id="evt_a")
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_a").status, "processed")
        self._assert_untouched()

    def test_other_users_are_untouched(self) -> None:
        other = User.objects.create_user(username="dave", email="alice@new.example", password="pw12345!")
        self._process(event_id="evt_iso", email="alice@new.example")
        other.refresh_from_db()
        self.assertEqual(other.email, "alice@new.example")
        self._assert_untouched()

    def test_no_stripe_calls_are_made(self) -> None:
        with patch("fragrance.billing.get_stripe_client") as get_client, \
                patch("fragrance.billing.get_interactive_stripe_client") as get_interactive_client:
            self._process(event_id="evt_nostripe", email="alice@new.example")
        get_client.assert_not_called()
        get_interactive_client.assert_not_called()


class CustomerUpdatedMirrorsReceiptEmailTests(_EmailSyncBase):
    def setUp(self) -> None:
        super().setUp()
        Subscription.objects.filter(user=self.user).update(receipt_email="old-receipt@example.com")
        self.other = User.objects.create_user(username="dave", email="dave@example.com", password="pw12345!")
        Subscription.objects.create(
            user=self.other, stripe_customer_id="cus_dave", receipt_email="dave-receipt@example.com"
        )

    def _receipt(self, user: User) -> str:
        return Subscription.objects.get(user=user).receipt_email

    def test_changed_email_is_mirrored_normalised_for_that_customer_only(self) -> None:
        self._process(event_id="evt_m1", email="  New-Receipt@Example.com ")

        self.assertEqual(self._receipt(self.user), "new-receipt@example.com")
        self.assertEqual(self._receipt(self.other), "dave-receipt@example.com")
        self.assertEqual(self._email(), "alice@old.example")
        self.mock_delay.assert_not_called()

    def test_removed_email_blanks_the_cache(self) -> None:
        self._process(event_id="evt_m2", email=None)

        self.assertEqual(self._receipt(self.user), "")

    def test_event_without_an_email_change_leaves_the_cache(self) -> None:
        self._process(event_id="evt_m3", email="x@example.com", previous={"name": "Old"})
        self._process(event_id="evt_m4", email="x@example.com", previous={})

        self.assertEqual(self._receipt(self.user), "old-receipt@example.com")

    def test_invalid_email_is_not_cached(self) -> None:
        self._process(event_id="evt_m5", email="not-an-email")
        self._process(event_id="evt_m6", email="a" * 250 + "@example.com")

        self.assertEqual(self._receipt(self.user), "old-receipt@example.com")

    def test_unknown_customer_changes_nothing_and_replay_is_stable(self) -> None:
        self._process(event_id="evt_m7", email="x@example.com", customer_id="cus_nobody")
        self.assertEqual(self._receipt(self.user), "old-receipt@example.com")
        self.assertEqual(self._receipt(self.other), "dave-receipt@example.com")

        self._process(event_id="evt_m8", email="new@example.com")
        StripeEvent.objects.filter(stripe_event_id="evt_m8").update(status="failed")
        process_stripe_event(stripe_event_id="evt_m8")
        self.assertEqual(self._receipt(self.user), "new@example.com")

    def test_no_stripe_calls_are_made(self) -> None:
        with patch("fragrance.billing.get_stripe_client") as get_client, \
                patch("fragrance.billing.get_interactive_stripe_client") as get_interactive_client:
            self._process(event_id="evt_m9", email="new@example.com")
        get_client.assert_not_called()
        get_interactive_client.assert_not_called()


@override_settings(SUPPORT_EMAIL="help@example.test", DEFAULT_FROM_EMAIL="noreply@example.test")
class EmailChangeNoticeTaskTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="alice", email="alice@new.example", password="pw12345!"
        )

    def _send(self, *, is_new_address: bool, recipient: str, retries: int = 0) -> None:
        send_account_email_change_notice.push_request(retries=retries)
        try:
            send_account_email_change_notice(
                user_id=self.user.pk, recipient=recipient, old_email="alice@old.example",
                new_email="alice@new.example", is_new_address=is_new_address,
            )
        finally:
            send_account_email_change_notice.pop_request()

    def test_full_flow_sends_one_message_to_each_address(self) -> None:
        # Retired path's task still renders and sends when invoked directly (messages
        # already queued at deploy time must not fail).
        self._send(is_new_address=False, recipient="alice@old.example")
        self._send(is_new_address=True, recipient="alice@new.example")
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ["alice@new.example", "alice@old.example"])

    def test_old_address_notice_warns_and_names_support(self) -> None:
        self._send(is_new_address=False, recipient="alice@old.example")
        (message,) = mail.outbox
        self.assertEqual(message.to, ["alice@old.example"])
        self.assertEqual(message.from_email, "noreply@example.test")
        self.assertIn("alice@new.example", message.body)
        self.assertIn("help@example.test", message.body)
        self.assertIn("wasn't you", message.body)
        html, mimetype = message.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("help@example.test", html)

    def test_new_address_notice_confirms_and_has_no_links_or_tokens(self) -> None:
        self._send(is_new_address=True, recipient="alice@new.example")
        (message,) = mail.outbox
        self.assertEqual(message.to, ["alice@new.example"])
        self.assertIn("alice@old.example", message.body)
        self.assertNotIn("http", message.body)
        self.assertNotIn("http", message.alternatives[0][0])
        self.assertNotIn("token", message.body.lower())

    def test_html_escapes_addresses(self) -> None:
        send_account_email_change_notice(
            user_id=self.user.pk, recipient="a@b.example", old_email="o'<i>@x.example",
            new_email="n@x.example", is_new_address=True,
        )
        self.assertNotIn("<i>", mail.outbox[0].alternatives[0][0])

    def test_send_failure_schedules_a_300s_retry(self) -> None:
        with patch("fragrance.tasks.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            with patch.object(send_account_email_change_notice, "retry", side_effect=Retry()) as retry:
                with self.assertRaises(Retry):
                    self._send(is_new_address=True, recipient="alice@new.example")
        self.assertEqual(retry.call_args.kwargs["countdown"], 300)

    def test_send_failure_reraises_once_retries_are_exhausted(self) -> None:
        with patch("fragrance.tasks.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            with patch.object(send_account_email_change_notice, "retry") as retry:
                with self.assertRaises(OSError):
                    self._send(
                        is_new_address=True, recipient="alice@new.example",
                        retries=send_account_email_change_notice.max_retries,
                    )
        retry.assert_not_called()

    def test_deleted_user_sends_nothing(self) -> None:
        user_id = self.user.pk
        self.user.delete()
        send_account_email_change_notice(
            user_id=user_id, recipient="a@b.example", old_email="o@x.example",
            new_email="n@x.example", is_new_address=True,
        )
        self.assertEqual(mail.outbox, [])
