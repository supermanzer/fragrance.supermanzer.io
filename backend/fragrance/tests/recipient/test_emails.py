"""The two mail tasks and their templates (rendered for real; no Reply-To on either)."""

import re
from unittest.mock import patch

from django.core import mail
from fragrance import recipient
from fragrance.models import PendingEmailChange
from fragrance.tasks import send_recipient_changed_notice, send_recipient_confirmation_email
from fragrance.tests.recipient.common import RecipientTestCase


class _MailBase(RecipientTestCase):
    def setUp(self) -> None:
        super().setUp()
        mail.outbox.clear()

    def send_confirmation(self, purpose: str, address: str = "new@example.com"):
        pending = recipient.start_change(user=self.user, purpose=purpose, address=address)
        return send_recipient_confirmation_email.apply(
            kwargs={"user_id": self.user.pk, "purpose": purpose, "nonce": pending.nonce}
        )


class ConfirmationEmailTests(_MailBase):
    def test_delivery_mail_goes_to_the_pending_address_with_a_working_link(self) -> None:
        self.send_confirmation("delivery")

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["new@example.com"])
        self.assertEqual(message.subject, "Confirm your delivery address for Fragrance Journey")
        self.assertEqual(message.reply_to, [])
        self.assertEqual(message.from_email, "noreply@example.test")
        self.assertIn("monthly fragrance picks and notices", message.body)
        self.assertIn("No picks will be sent here", message.body)
        self.assertIn("help@example.test", message.body)
        self.assertNotIn("&amp;", message.body)
        self.assertNotIn("alice", message.body)
        url = re.search(r"https://app\.example\.test/auth/confirm-recipient\?token=(\S+)", message.body)
        assert url is not None
        self.assertNotIn("purpose=receipt", url.group(0))
        result = recipient.confirm_change(token=url.group(1))
        self.assertEqual(result.outcome, "confirmed")

    def test_receipt_mail_wording_and_purpose_in_the_link(self) -> None:
        self.send_confirmation("receipt", "billing@example.com")

        message = mail.outbox[0]
        self.assertEqual(message.to, ["billing@example.com"])
        self.assertEqual(message.subject, "Confirm your receipt address for Fragrance Journey")
        self.assertIn("payment receipts and invoices", message.body)
        self.assertIn("No receipts will be sent here", message.body)
        match = re.search(r"token=([^&\s]+)&purpose=receipt", message.body)
        assert match is not None
        self.assertEqual(recipient.read_token(token=match.group(1)).purpose, "receipt")
        html = message.alternatives[0].content
        self.assertIn("Confirm your receipt address", html)
        self.assertIn("Confirm this email address", html)
        self.assertIn("mailto:help@example.test", html)
        self.assertIn("#4A6DB5", html)

    def test_skips_silently_when_the_change_was_replaced_or_cancelled(self) -> None:
        pending = recipient.start_change(user=self.user, purpose="delivery", address="one@example.com")
        stale_nonce = pending.nonce
        self.clear_gap(purpose="delivery")
        recipient.start_change(user=self.user, purpose="delivery", address="two@example.com")

        result = send_recipient_confirmation_email.apply(
            kwargs={"user_id": self.user.pk, "purpose": "delivery", "nonce": stale_nonce}
        )
        recipient.cancel(user=self.user, purpose="delivery")
        result_cancelled = send_recipient_confirmation_email.apply(
            kwargs={"user_id": self.user.pk, "purpose": "delivery", "nonce": stale_nonce}
        )

        self.assertTrue(result.successful())
        self.assertTrue(result_cancelled.successful())
        self.assertEqual(len(mail.outbox), 0)

    def test_send_failure_retries_and_never_logs_the_address(self) -> None:
        pending = recipient.start_change(user=self.user, purpose="delivery", address="secret-addr@example.com")
        with patch("fragrance.tasks.EmailMessage.send", side_effect=RuntimeError("smtp down")):
            with patch.object(
                send_recipient_confirmation_email, "retry", side_effect=RuntimeError("retried")
            ) as retry:
                with self.assertLogs("fragrance.tasks", level="WARNING") as logs:
                    result = send_recipient_confirmation_email.apply(
                        kwargs={"user_id": self.user.pk, "purpose": "delivery", "nonce": pending.nonce}
                    )

        self.assertTrue(result.failed())
        retry.assert_called_once()
        self.assertNotIn("secret-addr@example.com", "\n".join(logs.output))
        self.assertTrue(PendingEmailChange.objects.filter(pk=pending.pk).exists())


class ChangedNoticeTests(_MailBase):
    def send(self, purpose: str):
        return send_recipient_changed_notice.apply(
            kwargs={
                "user_id": self.user.pk,
                "purpose": purpose,
                "recipient": "old@example.com",
                "new_address": "new@example.com",
            }
        )

    def test_delivery_notice(self) -> None:
        self.send("delivery")

        message = mail.outbox[0]
        self.assertEqual(message.to, ["old@example.com"])
        self.assertEqual(message.subject, "Your Fragrance Journey delivery address was changed")
        self.assertEqual(message.reply_to, [])
        self.assertIn("Hi alice,", message.body)
        self.assertIn("Your monthly picks and notices now go to new@example.com instead of this address.", message.body)
        self.assertIn("help@example.test and reset your password", message.body)
        self.assertNotIn("http", message.body)

    def test_receipt_notice(self) -> None:
        self.send("receipt")

        message = mail.outbox[0]
        self.assertEqual(message.subject, "Your Fragrance Journey receipt address was changed")
        self.assertIn(
            "Receipts and invoices for your subscription now go to new@example.com instead of this address.",
            message.body,
        )
        self.assertIn("new@example.com", message.alternatives[0].content)

    def test_unknown_user_sends_nothing(self) -> None:
        send_recipient_changed_notice.apply(
            kwargs={"user_id": 999999, "purpose": "delivery", "recipient": "o@example.com", "new_address": "n@example.com"}
        )

        self.assertEqual(len(mail.outbox), 0)

    def test_send_failure_retries(self) -> None:
        with patch("fragrance.tasks.EmailMessage.send", side_effect=RuntimeError("smtp down")):
            with patch.object(send_recipient_changed_notice, "retry", side_effect=RuntimeError("retried")) as retry:
                with self.assertLogs("fragrance.tasks", level="WARNING"):
                    result = self.send("delivery")

        self.assertTrue(result.failed())
        retry.assert_called_once()
