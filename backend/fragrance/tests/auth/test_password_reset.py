import re
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from fragrance.tasks import send_password_reset_email
from fragrance.tests.support.bases import _run_reset_task_inline
from rest_framework.test import APIClient


class PasswordResetUnusablePasswordTests(TestCase):
    """
    Regression coverage for the load-bearing behaviour documented on
    PasswordResetRequestView: approved-but-not-yet-converted beta users are
    created via set_unusable_password() (BetaSignupAdmin.approve_signups),
    and Django's own PasswordResetForm.get_users() filters such accounts
    out. This project's PasswordResetRequestView deliberately does its own
    User.objects.get(email=...) lookup instead, which is the only reason
    "forgot password" can recover one of these accounts -- and the new
    /beta/accept copy (see BetaAcceptViewTests) now actively points users
    down this path when their invite link is dead or expired.
    """

    def test_django_password_reset_form_excludes_unusable_password_users(self) -> None:
        # Empirical proof of the gap this project's custom view exists to
        # close -- if a future Django release changes get_users()'s
        # filtering, this is the test that should catch it.
        from django.contrib.auth.forms import PasswordResetForm

        User.objects.create_user(username="nopass", email="nopass@example.com", password=None)
        user = User.objects.get(username="nopass")
        self.assertFalse(user.has_usable_password())

        form = PasswordResetForm(data={"email": "nopass@example.com"})
        self.assertTrue(form.is_valid())
        self.assertEqual(list(form.get_users(email="nopass@example.com")), [])

    def test_reset_request_reaches_unusable_password_user_and_token_validates(self) -> None:
        user = User.objects.create_user(
            username="approved_not_converted", email="approved@example.com", password=None
        )
        self.assertFalse(user.has_usable_password())

        with _run_reset_task_inline():
            response = self.client.post(
                "/api/v1/auth/password/reset/", {"email": "approved@example.com"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

        # Asserts a literal "&", not "&amp;": render_to_string HTML-escapes
        # by default (Django's autoescaping applies to the template engine,
        # not the .txt extension), which silently breaks this link's second
        # query param for every real mail client -- password_reset_email.txt
        # wraps its body in {% autoescape off %} specifically to prevent
        # that. This assertion is what stops that fix from being "tidied"
        # away without anyone noticing the link breaks again.
        self.assertNotIn("&amp;", mail.outbox[0].body)
        match = re.search(r"uid=([^&\s]+)&token=([^&\s]+)", mail.outbox[0].body)
        self.assertIsNotNone(match)
        uid, token = match.group(1), match.group(2)

        confirm_response = self.client.post(
            "/api/v1/auth/password/reset/confirm/",
            {
                "uid": uid,
                "token": token,
                "new_password": "Str0ng-Passw0rd!",
                "new_password_confirm": "Str0ng-Passw0rd!",
            },
        )
        self.assertEqual(confirm_response.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password("Str0ng-Passw0rd!"))


class PasswordResetEmailLookupTests(TestCase):
    url = "/api/v1/auth/password/reset/"

    def test_case_insensitive_email_match_sends_mail(self) -> None:
        User.objects.create_user(username="dave", email="Dave@example.com")
        mail.outbox.clear()
        with _run_reset_task_inline():
            response = APIClient().post(self.url, {"email": "dave@example.com"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

    def test_duplicate_emails_do_not_500(self) -> None:
        User.objects.create_user(username="dup1", email="dup@example.com")
        User.objects.create_user(username="dup2", email="dup@example.com")
        mail.outbox.clear()
        with _run_reset_task_inline():
            response = APIClient().post(self.url, {"email": "dup@example.com"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 2)


class PasswordResetQueuedTests(TestCase):
    url = "/api/v1/auth/password/reset/"

    def _post(self, email: str):
        return APIClient().post(self.url, {"email": email}, format="json")

    def test_zero_one_and_many_matches_return_identical_response(self) -> None:
        User.objects.create_user(username="one", email="one@example.com")
        User.objects.create_user(username="m1", email="many@example.com")
        User.objects.create_user(username="m2", email="Many@example.com")
        with patch("fragrance.tasks.send_password_reset_email.delay"):
            none = self._post("nobody@example.com")
            one = self._post("one@example.com")
            many = self._post("many@example.com")
        self.assertEqual(none.status_code, 200)
        self.assertEqual(one.status_code, 200)
        self.assertEqual(many.status_code, 200)
        self.assertEqual(none.content, one.content)
        self.assertEqual(none.content, many.content)

    def test_mail_is_queued_per_account_not_sent_inline(self) -> None:
        u1 = User.objects.create_user(username="q1", email="q@example.com")
        u2 = User.objects.create_user(username="q2", email="Q@example.com")
        mail.outbox.clear()
        with patch("fragrance.tasks.send_password_reset_email.delay") as mock_delay:
            response = self._post("q@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(mock_delay.call_count, 2)
        self.assertEqual(
            [c.kwargs for c in mock_delay.call_args_list],
            [{"user_id": u1.pk}, {"user_id": u2.pk}],
        )

    def test_no_match_enqueues_nothing(self) -> None:
        with patch("fragrance.tasks.send_password_reset_email.delay") as mock_delay:
            self._post("ghost@example.com")
        mock_delay.assert_not_called()

    def test_broker_failure_still_returns_uniform_response(self) -> None:
        from kombu.exceptions import OperationalError

        User.objects.create_user(username="b1", email="b@example.com")
        with patch("fragrance.tasks.send_password_reset_email.delay") as mock_delay:
            mock_delay.side_effect = OperationalError("broker down")
            with self.assertLogs("fragrance.views", level="ERROR"):
                response = self._post("b@example.com")
            control = self._post("ghost@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, control.content)

    def test_task_sends_mail_with_uid_and_token_in_query_string(self) -> None:
        from urllib.parse import parse_qs, urlparse

        user = User.objects.create_user(username="t1", email="t@example.com", password=None)
        self.assertFalse(user.has_usable_password())
        mail.outbox.clear()
        send_password_reset_email.apply(kwargs={"user_id": user.pk})
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["t@example.com"])
        url = re.search(r"https?://\S+", mail.outbox[0].body).group(0)
        query = parse_qs(urlparse(url).query)
        self.assertIn("uid", query)
        self.assertIn("token", query)

    def test_task_with_missing_user_sends_nothing(self) -> None:
        mail.outbox.clear()
        send_password_reset_email.apply(kwargs={"user_id": 999999})
        self.assertEqual(len(mail.outbox), 0)

    def test_task_retries_on_send_failure(self) -> None:
        user = User.objects.create_user(username="r1", email="r@example.com")
        with patch("fragrance.tasks.EmailMessage.send", side_effect=RuntimeError("smtp down")):
            with patch.object(send_password_reset_email, "retry", side_effect=RuntimeError("retried")) as mock_retry:
                result = send_password_reset_email.apply(kwargs={"user_id": user.pk})
        self.assertTrue(result.failed())
        mock_retry.assert_called_once()

    def test_unknown_email_returns_same_response_and_sends_nothing(self) -> None:
        mail.outbox.clear()
        response = APIClient().post(self.url, {"email": "nobody@example.com"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
