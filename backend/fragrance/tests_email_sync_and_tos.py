"""
Tests for the customer.updated email sync (billing.sync_customer_email plus its
two notice emails) and for Terms of Service consent on Checkout.

Stripe objects are real SDK objects via `construct_from`. The sync never calls
Stripe, so those tests need no Stripe patching at all.
"""

import logging
from datetime import timedelta
from unittest.mock import patch

import stripe
from celery.exceptions import Retry
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .billing import fulfill_checkout_session
from .models import Plan, StripeEvent, Subscription
from .tasks import process_stripe_event, send_account_email_change_notice

_KEY = "sk_test_dummy"
_SETTINGS = {
    "STRIPE_SECRET_KEY": _KEY,
    "STRIPE_API_VERSION": "2026-08-26.dahlia",
    "FRONTEND_URL": "https://app.example.test",
}


def _store_customer_updated(
    *, event_id: str, customer_id: str, email: str | None, previous: dict | None
) -> None:
    data: dict = {"object": {"id": customer_id, "object": "customer", "email": email}}
    if previous is not None:
        data["previous_attributes"] = previous
    StripeEvent.objects.create(
        stripe_event_id=event_id,
        event_type="customer.updated",
        stripe_created=timezone.now(),
        payload={"id": event_id, "type": "customer.updated", "data": data},
    )


class _EmailSyncBase(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="alice", email="alice@old.example", password="pw12345!"
        )
        Subscription.objects.create(user=self.user, stripe_customer_id="cus_alice")
        patcher = patch("fragrance.tasks.send_account_email_change_notice.delay")
        self.mock_delay = patcher.start()
        self.addCleanup(patcher.stop)

    def _process(self, *, event_id: str, email: str | None, previous: dict | None = None,
                 customer_id: str = "cus_alice") -> None:
        _store_customer_updated(
            event_id=event_id, customer_id=customer_id, email=email,
            previous={"email": "alice@old.example"} if previous is None else previous,
        )
        process_stripe_event(stripe_event_id=event_id)

    def _email(self) -> str:
        self.user.refresh_from_db()
        return self.user.email


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
        with patch("fragrance.billing._configure_stripe") as configure, \
                patch("fragrance.billing.stripe.Customer.modify") as modify:
            self._process(event_id="evt_nostripe", email="alice@new.example")
        configure.assert_not_called()
        modify.assert_not_called()

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


@override_settings(**_SETTINGS)
class ToSConsentCheckoutTests(TestCase):
    url = "/api/v1/billing/checkout/"

    def setUp(self) -> None:
        patcher = patch(
            "fragrance.billing.stripe.Subscription.list",
            return_value=stripe.ListObject.construct_from(
                {"object": "list", "data": [], "has_more": False, "url": "/v1/subscriptions"}, _KEY
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
            amount_cents=400, currency="usd", billing_interval="month", access_days_granted=30,
        )
        self.user = User.objects.create_user(username="u", email="u@example.com", password="pw12345!")
        Subscription.objects.create(
            user=self.user, stripe_customer_id="cus_1", last_checkout_session_id="cs_old"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _session(self, *, consent: dict | None, session_id: str = "cs_old") -> stripe.checkout.Session:
        data = {
            "id": session_id, "object": "checkout.session", "status": "open", "mode": "subscription",
            "url": f"https://checkout.stripe.com/{session_id}",
            "metadata": {"plan_slug": "beta-monthly", "price_id": "price_mo"},
            "consent_collection": consent,
        }
        return stripe.checkout.Session.construct_from(data, _KEY)

    def _new_session(self) -> stripe.checkout.Session:
        return self._session(consent=None, session_id="cs_new")

    @override_settings(STRIPE_REQUIRE_TOS_CONSENT=True)
    @patch("fragrance.billing.stripe.checkout.Session.create")
    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_param_present_when_flag_on(self, retrieve, create) -> None:
        retrieve.side_effect = stripe.InvalidRequestError("gone", "id")
        create.return_value = self._new_session()
        self.assertEqual(self.client.post(self.url, {"slug": "beta-monthly"}).status_code, 200)
        self.assertEqual(create.call_args.kwargs["consent_collection"], {"terms_of_service": "required"})

    @override_settings(STRIPE_REQUIRE_TOS_CONSENT=False)
    @patch("fragrance.billing.stripe.checkout.Session.create")
    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_param_absent_when_flag_off(self, retrieve, create) -> None:
        retrieve.side_effect = stripe.InvalidRequestError("gone", "id")
        create.return_value = self._new_session()
        self.assertEqual(self.client.post(self.url, {"slug": "beta-monthly"}).status_code, 200)
        self.assertNotIn("consent_collection", create.call_args.kwargs)

    @override_settings(STRIPE_REQUIRE_TOS_CONSENT=True)
    @patch("fragrance.billing.stripe.checkout.Session.expire")
    @patch("fragrance.billing.stripe.checkout.Session.create")
    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_open_session_without_consent_is_expired_when_flag_on(self, retrieve, create, expire) -> None:
        retrieve.return_value = self._session(consent=None)
        create.return_value = self._new_session()
        response = self.client.post(self.url, {"slug": "beta-monthly"})
        self.assertEqual(response.json()["url"], "https://checkout.stripe.com/cs_new")
        expire.assert_called_once_with("cs_old")

    @override_settings(STRIPE_REQUIRE_TOS_CONSENT=True)
    @patch("fragrance.billing.stripe.checkout.Session.expire")
    @patch("fragrance.billing.stripe.checkout.Session.create")
    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_open_session_with_consent_is_reused_when_flag_on(self, retrieve, create, expire) -> None:
        retrieve.return_value = self._session(consent={"terms_of_service": "required"})
        response = self.client.post(self.url, {"slug": "beta-monthly"})
        self.assertEqual(response.json()["url"], "https://checkout.stripe.com/cs_old")
        create.assert_not_called()
        expire.assert_not_called()

    @override_settings(STRIPE_REQUIRE_TOS_CONSENT=False)
    @patch("fragrance.billing.stripe.checkout.Session.expire")
    @patch("fragrance.billing.stripe.checkout.Session.create")
    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_open_session_with_consent_is_expired_when_flag_off(self, retrieve, create, expire) -> None:
        retrieve.return_value = self._session(consent={"terms_of_service": "required"})
        create.return_value = self._new_session()
        self.client.post(self.url, {"slug": "beta-monthly"})
        expire.assert_called_once_with("cs_old")
        create.assert_called_once()

    @override_settings(STRIPE_REQUIRE_TOS_CONSENT=False)
    @patch("fragrance.billing.stripe.checkout.Session.expire")
    @patch("fragrance.billing.stripe.checkout.Session.create")
    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_open_session_without_consent_is_reused_when_flag_off(self, retrieve, create, expire) -> None:
        retrieve.return_value = self._session(consent=None)
        response = self.client.post(self.url, {"slug": "beta-monthly"})
        self.assertEqual(response.json()["url"], "https://checkout.stripe.com/cs_old")
        create.assert_not_called()

    @override_settings(STRIPE_REQUIRE_TOS_CONSENT=True)
    @patch("fragrance.billing.stripe.checkout.Session.create")
    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_missing_terms_url_maps_to_502_with_fixed_message_and_actionable_log(self, retrieve, create) -> None:
        retrieve.side_effect = stripe.InvalidRequestError("gone", "id")
        create.side_effect = stripe.InvalidRequestError(
            "You cannot collect consent to a terms of service agreement until a URL is set in "
            "the Stripe Dashboard at https://dashboard.stripe.com/settings/public.",
            "consent_collection",
        )
        with self.assertLogs("fragrance.billing", level=logging.ERROR) as logs:
            response = self.client.post(self.url, {"slug": "beta-monthly"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"detail": "Could not start checkout. Please try again shortly."})
        self.assertIn("STRIPE_REQUIRE_TOS_CONSENT=False", logs.output[0])
        self.assertIn("Public details", logs.output[0])
        # State is not advanced by a failed create.
        self.assertEqual(Subscription.objects.get(user=self.user).last_checkout_session_id, "cs_old")

    @override_settings(STRIPE_REQUIRE_TOS_CONSENT=True)
    @patch("fragrance.billing.stripe.checkout.Session.create")
    @patch("fragrance.billing.stripe.checkout.Session.retrieve")
    def test_other_invalid_request_errors_still_502_via_generic_path(self, retrieve, create) -> None:
        retrieve.side_effect = stripe.InvalidRequestError("gone", "id")
        create.side_effect = stripe.InvalidRequestError("No such price: 'price_mo'", "price")
        with self.assertLogs("fragrance.views", level=logging.ERROR):
            response = self.client.post(self.url, {"slug": "beta-monthly"})
        self.assertEqual(response.status_code, 502)


@override_settings(**_SETTINGS)
class ToSAcceptanceLogTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="u2", email="u2@example.com", password="pw12345!")

    def _fulfill(self, *, consent: dict | None) -> None:
        session = stripe.checkout.Session.construct_from({
            "id": "cs_done", "object": "checkout.session", "client_reference_id": str(self.user.id),
            "payment_status": "unpaid", "consent": consent, "invoice": None,
        }, _KEY)
        with patch("fragrance.billing.stripe.checkout.Session.retrieve", return_value=session):
            fulfill_checkout_session(session_id="cs_done", user=self.user)

    def test_accepted_terms_are_logged_with_the_session_id(self) -> None:
        with self.assertLogs("fragrance.billing", level=logging.INFO) as logs:
            self._fulfill(consent={"terms_of_service": "accepted", "promotions": None})
        self.assertTrue(any("cs_done" in line and "terms of service" in line for line in logs.output))

    def test_no_consent_logs_no_acceptance(self) -> None:
        with self.assertLogs("fragrance.billing", level=logging.INFO) as logs:
            self._fulfill(consent=None)
        self.assertFalse(any("accepted the terms" in line for line in logs.output))
