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


class SyncCustomerEmailTests(_EmailSyncBase):
    def test_changed_email_updates_user_and_queues_both_notices(self) -> None:
        self._process(event_id="evt_1", email="alice@new.example")

        self.assertEqual(self._email(), "alice@new.example")
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_1").status, "processed")
        self.assertEqual(self.mock_delay.call_count, 2)
        by_recipient = {c.kwargs["recipient"]: c.kwargs for c in self.mock_delay.call_args_list}
        self.assertEqual(set(by_recipient), {"alice@old.example", "alice@new.example"})
        self.assertFalse(by_recipient["alice@old.example"]["is_new_address"])
        self.assertTrue(by_recipient["alice@new.example"]["is_new_address"])
        for kwargs in by_recipient.values():
            self.assertEqual(kwargs["old_email"], "alice@old.example")
            self.assertEqual(kwargs["new_email"], "alice@new.example")
            self.assertEqual(kwargs["user_id"], self.user.pk)

    def test_whitespace_is_stripped_from_the_new_email(self) -> None:
        self._process(event_id="evt_ws", email="  alice@new.example \n")
        self.assertEqual(self._email(), "alice@new.example")

    def test_case_only_difference_is_a_noop(self) -> None:
        self._process(event_id="evt_case", email="  ALICE@Old.Example ")
        self.assertEqual(self._email(), "alice@old.example")
        self.mock_delay.assert_not_called()

    def test_replay_is_inert_and_sends_one_pair_only(self) -> None:
        self._process(event_id="evt_a", email="alice@new.example")
        # Same change delivered again (a manual resend / reconcile requeue).
        StripeEvent.objects.filter(stripe_event_id="evt_a").update(status="failed")
        process_stripe_event(stripe_event_id="evt_a")
        self.assertEqual(self._email(), "alice@new.example")
        self.assertEqual(self.mock_delay.call_count, 2)

    def test_unknown_customer_is_a_noop(self) -> None:
        self._process(event_id="evt_unk", email="x@new.example", customer_id="cus_nobody")
        self.assertEqual(self._email(), "alice@old.example")
        self.mock_delay.assert_not_called()
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_unk").status, "processed")

    def test_user_is_never_resolved_by_email(self) -> None:
        # A customer whose email matches a local user but whose id matches no Subscription.
        other = User.objects.create_user(username="bob", email="bob@old.example", password="pw12345!")
        self._process(event_id="evt_byemail", email="bob@old.example",
                      previous={"email": "x@y.example"}, customer_id="cus_stranger")
        other.refresh_from_db()
        self.assertEqual(other.email, "bob@old.example")
        self.mock_delay.assert_not_called()

    def test_duplicate_email_is_skipped_case_insensitively(self) -> None:
        User.objects.create_user(username="carol", email="Carol@Taken.example", password="pw12345!")
        with self.assertLogs("fragrance.billing", level=logging.WARNING) as logs:
            self._process(event_id="evt_dup", email="carol@taken.EXAMPLE")
        self.assertEqual(self._email(), "alice@old.example")
        self.mock_delay.assert_not_called()
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_dup").status, "processed")
        self.assertNotIn("taken.example", "\n".join(logs.output).lower())

    def test_staff_and_superuser_are_untouched(self) -> None:
        for flag in ("is_staff", "is_superuser"):
            with self.subTest(flag=flag):
                setattr(self.user, flag, True)
                self.user.save()
                self._process(event_id=f"evt_{flag}", email="admin@new.example")
                self.assertEqual(self._email(), "alice@old.example")
                self.mock_delay.assert_not_called()

    def test_empty_and_invalid_emails_are_skipped(self) -> None:
        for index, bad in enumerate([None, "", "   ", "not-an-email", "a@b@c", "x" * 250 + "@e.example"]):
            with self.subTest(email=bad):
                self._process(event_id=f"evt_bad{index}", email=bad)
                self.assertEqual(self._email(), "alice@old.example")
        self.mock_delay.assert_not_called()

    def test_event_that_did_not_change_email_is_ignored(self) -> None:
        # Stripe's email differs from ours, but only `name` changed on this event.
        self._process(event_id="evt_name", email="alice@new.example", previous={"name": "Old Name"})
        self.assertEqual(self._email(), "alice@old.example")
        self.mock_delay.assert_not_called()

    def test_absent_empty_or_emailless_previous_attributes_are_noops(self) -> None:
        # Stripe's email differs from User.email in every case; still nothing may change.
        cases = {"absent": None, "empty": {}, "no_email_key": {"name": "Old Name"}}
        for name, previous in cases.items():
            with self.subTest(case=name):
                _store_customer_updated(
                    event_id=f"evt_{name}", customer_id="cus_alice", email="attacker@evil.example",
                    previous=previous,
                )
                with self.assertLogs("fragrance.billing", level=logging.INFO) as logs:
                    process_stripe_event(stripe_event_id=f"evt_{name}")
                self.assertEqual(self._email(), "alice@old.example")
                self.assertEqual(StripeEvent.objects.get(stripe_event_id=f"evt_{name}").status, "processed")
                self.assertNotIn("evil.example", "\n".join(logs.output).lower())
        self.mock_delay.assert_not_called()

    def test_post_revert_stripe_still_holding_attacker_address_is_not_resynced(self) -> None:
        # Revert restored User.email, but the push to Stripe failed: Stripe still
        # has the attacker's address. A later unrelated update must not re-sync it.
        _store_customer_updated(
            event_id="evt_postrevert", customer_id="cus_alice", email="attacker@evil.example",
            previous={"default_source": None},
        )
        process_stripe_event(stripe_event_id="evt_postrevert")
        _store_customer_updated(
            event_id="evt_postrevert2", customer_id="cus_alice", email="attacker@evil.example", previous={},
        )
        process_stripe_event(stripe_event_id="evt_postrevert2")
        self.assertEqual(self._email(), "alice@old.example")
        self.mock_delay.assert_not_called()

    def test_real_email_change_with_previous_email_still_syncs(self) -> None:
        self._process(event_id="evt_real", email="alice@new.example", previous={"email": "alice@old.example"})
        self.assertEqual(self._email(), "alice@new.example")
        self.assertEqual(self.mock_delay.call_count, 2)

    def test_user_without_a_previous_email_gets_only_the_confirmation(self) -> None:
        self.user.email = ""
        self.user.save()
        self._process(event_id="evt_blank", email="alice@new.example", previous={"email": None})
        self.assertEqual(self._email(), "alice@new.example")
        self.assertEqual(self.mock_delay.call_count, 1)
        self.assertEqual(self.mock_delay.call_args.kwargs["recipient"], "alice@new.example")

    def test_other_users_and_dependent_rows_are_untouched(self) -> None:
        other = User.objects.create_user(username="dave", email="dave@x.example", password="pw12345!")
        self._process(event_id="evt_iso", email="alice@new.example")
        other.refresh_from_db()
        self.assertEqual(other.email, "dave@x.example")

    def test_no_stripe_calls_are_made(self) -> None:
        with patch("fragrance.billing.get_stripe_client") as get_client, \
                patch("fragrance.billing.get_interactive_stripe_client") as get_interactive_client:
            self._process(event_id="evt_nostripe", email="alice@new.example")
        get_client.assert_not_called()
        get_interactive_client.assert_not_called()

    def test_broker_failure_rolls_back_the_change_and_retries_the_event(self) -> None:
        self.mock_delay.side_effect = OSError("broker down")
        _store_customer_updated(
            event_id="evt_broker", customer_id="cus_alice", email="alice@new.example",
            previous={"email": "alice@old.example"},
        )
        with patch.object(process_stripe_event, "retry", side_effect=Retry()) as retry:
            with self.assertRaises(Retry):
                process_stripe_event(stripe_event_id="evt_broker")
        self.assertEqual(retry.call_args.kwargs["countdown"], 300)
        self.assertEqual(self._email(), "alice@old.example")
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_broker").status, "failed")

        # The retry, with the broker back, completes the change and sends the pair.
        self.mock_delay.side_effect = None
        process_stripe_event(stripe_event_id="evt_broker")
        self.assertEqual(self._email(), "alice@new.example")
        self.assertEqual(self.mock_delay.call_count, 3)  # 1 failed attempt + the pair


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
        # Real (unpatched-broker) path: run the queued tasks eagerly through locmem mail.
        Subscription.objects.create(user=self.user, stripe_customer_id="cus_flow")
        self.user.email = "alice@old.example"
        self.user.save()
        with patch(
            "fragrance.tasks.send_account_email_change_notice.delay",
            side_effect=lambda **kw: send_account_email_change_notice.apply(kwargs=kw),
        ):
            _store_customer_updated(
                event_id="evt_flow", customer_id="cus_flow", email="alice@new.example",
                previous={"email": "alice@old.example"},
            )
            process_stripe_event(stripe_event_id="evt_flow")
            process_stripe_event(stripe_event_id="evt_flow")  # replay
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ["alice@new.example", "alice@old.example"])
        self.assertEqual(len(mail.outbox), 2)

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
