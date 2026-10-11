"""Exceptions are sanitized before Celery sees them: retry lines, result backend, final failures."""

import logging
import sys
import traceback
from unittest.mock import patch

import stripe
from celery.exceptions import Retry
from config.log_setup import ExceptionScrubFilter, RedactionFilter, SingleLineFormatter
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from fragrance.ai_providers.base import sanitized_exception
from fragrance.models import AIModelConfig, FragranceConfig, RecommendationRun
from fragrance.tests.support.factories import active_subscription_for
from fragrance.tasks import (
    _raise_sanitized,
    _retry_sanitized,
    monthly_fragrance_run,
    send_operator_critical_alert,
    send_recommendation_email,
)

# Assembled at runtime so the secret is not in this file's source line (tracebacks echo source).
SECRET = "hunter" + "2secret"
RAW = f"cannot reach redis://:{SECRET}@redis:6379/0 for dave@example.test"


class NeedsKeywordArgs(OSError):
    """Stands in for an SDK error whose constructor needs request/response objects."""

    def __init__(self, message: str, *, response: object, body: object) -> None:
        super().__init__(message)
        self.response = response
        self.body = body

    def __str__(self) -> str:
        return f"{self.response.status} {self.args[0]}"  # type: ignore[attr-defined]


class SanitizedExceptionTests(SimpleTestCase):
    def _raise(self, factory):
        try:
            raise factory()
        except Exception as exc:
            return exc

    def test_message_is_redacted_and_type_is_preserved(self) -> None:
        original = self._raise(lambda: ConnectionError(RAW))

        safe = sanitized_exception(exc=original)

        self.assertIsInstance(safe, ConnectionError)
        self.assertIs(type(safe).__name__ == "ConnectionError", True)
        for text in (str(safe), repr(safe)):
            self.assertNotIn(SECRET, text)
            self.assertNotIn("dave@example.test", text)
        self.assertIn("redis://[REDACTED]@redis:6379/0", str(safe))
        self.assertIsNot(safe, original)
        self.assertIs(safe.__traceback__.tb_frame.f_code, original.__traceback__.tb_frame.f_code)

    def test_sdk_errors_get_the_fixed_message(self) -> None:
        original = self._raise(lambda: stripe.APIConnectionError("boom " + SECRET))

        safe = sanitized_exception(exc=original)

        self.assertIsInstance(safe, stripe.APIConnectionError)
        self.assertEqual(str(safe), "Could not reach Stripe. Try again shortly.")

    def test_constructor_with_required_kwargs_and_custom_str_is_handled(self) -> None:
        original = self._raise(lambda: NeedsKeywordArgs("bad " + SECRET, response=type("R", (), {"status": 502})(), body={}))

        safe = sanitized_exception(exc=original)

        self.assertIsInstance(safe, NeedsKeywordArgs)
        self.assertEqual(str(safe), str(original))  # the message is taken once, from the original
        self.assertEqual(repr(safe), "NeedsKeywordArgs('502 bad " + SECRET + "')")
        self.assertFalse(hasattr(safe, "response"))

    def test_drop_message_keeps_only_the_class(self) -> None:
        original = self._raise(lambda: ConnectionError(RAW))

        safe = sanitized_exception(exc=original, drop_message=True)

        self.assertEqual(str(safe), "")
        self.assertIsInstance(safe, ConnectionError)

    def test_retry_string_carries_no_secret(self) -> None:
        original = self._raise(lambda: ConnectionError(RAW))

        retry = Retry(exc=sanitized_exception(exc=original), when=300)

        self.assertNotIn(SECRET, str(retry))
        self.assertNotIn("dave@example.test", str(retry))
        self.assertIn("ConnectionError", str(retry))

    def test_raise_sanitized_traceback_has_no_raw_text_cause_or_context(self) -> None:
        try:
            try:
                raise ValueError("inner " + SECRET)
            except ValueError as inner:
                raise ConnectionError(RAW) from inner
        except ConnectionError as exc:
            try:
                _raise_sanitized(exc=exc)
            except ConnectionError as raised:
                rendered = "".join(traceback.format_exception(raised))
                self.assertIsInstance(raised, ConnectionError)

        self.assertNotIn(SECRET, rendered)
        self.assertNotIn("dave@example.test", rendered)
        self.assertIn("ConnectionError", rendered)
        self.assertIn("test_exception_sanitizing.py", rendered)

    def test_never_raises_for_odd_exception_types(self) -> None:
        class Odd(Exception):
            def __new__(cls, *args: object) -> "Odd":
                if not args:
                    raise TypeError("needs args")
                return super().__new__(cls, *args)

        for original in (KeyError("k" + SECRET), Odd("x"), OSError(2, "No such file " + SECRET), SystemExit):
            with self.subTest(original=repr(original)[:30]):
                if isinstance(original, type):
                    original = original()
                safe = sanitized_exception(exc=original)
                self.assertIsInstance(safe, BaseException)


class RetryHelperTests(SimpleTestCase):
    class _Task:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def retry(self, **kwargs: object) -> None:
            self.calls.append(kwargs)
            raise Retry(exc=kwargs["exc"], when=kwargs["countdown"])  # type: ignore[arg-type]

    def test_retry_receives_a_sanitized_stand_in_and_the_same_countdown(self) -> None:
        task = self._Task()
        try:
            raise ConnectionError(RAW)
        except ConnectionError as exc:
            with self.assertRaises(Retry) as raised:
                _retry_sanitized(task=task, exc=exc, countdown=120)

        self.assertEqual(task.calls[0]["countdown"], 120)
        passed = task.calls[0]["exc"]
        self.assertIsInstance(passed, ConnectionError)
        self.assertNotIn(SECRET, str(passed))
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertNotIn(SECRET, "".join(traceback.format_exception(raised.exception)))


class TaskSitesTests(TestCase):
    def _run(self, *, task, retries: int, **kwargs: object) -> None:
        task.push_request(retries=retries)
        try:
            task(**kwargs)
        finally:
            task.pop_request()

    def _raising_retry(self, **kwargs: object) -> None:
        raise Retry(exc=kwargs["exc"], when=kwargs["countdown"])  # type: ignore[arg-type]

    def test_email_task_retry_passes_a_sanitized_exception_and_logs_none_of_the_raw_text(self) -> None:
        user = User.objects.create_user(username="s1", email="s1@example.test", password="pw12345!")
        run = RecommendationRun.objects.create(user=user, status="done", email_status="pending")
        with patch("fragrance.tasks.render_and_send_email", side_effect=ConnectionError(RAW)):
            with patch.object(send_recommendation_email, "retry", side_effect=self._raising_retry) as retry:
                with self.assertRaises(Retry) as raised:
                    self._run(task=send_recommendation_email, retries=0, user_id=user.id, run_id=run.id)

        self.assertEqual(retry.call_args.kwargs["countdown"], 300)
        self.assertIsInstance(retry.call_args.kwargs["exc"], ConnectionError)
        self.assertNotIn(SECRET, str(retry.call_args.kwargs["exc"]))
        self.assertNotIn(SECRET, str(raised.exception))

    def test_email_task_final_failure_raises_the_sanitized_type_and_keeps_error_message_clean(self) -> None:
        user = User.objects.create_user(username="s2", email="s2@example.test", password="pw12345!")
        run = RecommendationRun.objects.create(user=user, status="done", email_status="pending")
        with patch("fragrance.tasks.render_and_send_email", side_effect=ConnectionError(RAW)):
            with self.assertRaises(ConnectionError) as raised:
                self._run(
                    task=send_recommendation_email,
                    retries=send_recommendation_email.max_retries,
                    user_id=user.id,
                    run_id=run.id,
                )

        self.assertNotIn(SECRET, str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)
        run.refresh_from_db()
        self.assertEqual(run.email_status, "failed")
        self.assertNotIn(SECRET, run.error_message)
        self.assertIn("Email delivery failed after 5 retries", run.error_message)

    def test_alert_task_retry_and_final_failure_are_sanitized(self) -> None:
        with patch("config.log_setup.deliver_alert_email", side_effect=ConnectionError(RAW)):
            with patch.object(send_operator_critical_alert, "retry", side_effect=self._raising_retry) as retry:
                with self.assertRaises(Retry):
                    self._run(task=send_operator_critical_alert, retries=0, subject="s", body="b")
            self.assertEqual(retry.call_args.kwargs["countdown"], 60)
            self.assertNotIn(SECRET, str(retry.call_args.kwargs["exc"]))
            with self.assertRaises(ConnectionError) as raised:
                self._run(
                    task=send_operator_critical_alert,
                    retries=send_operator_critical_alert.max_retries,
                    subject="s",
                    body="b",
                )
        self.assertNotIn(SECRET, str(raised.exception))


class ScrubFilterTests(SimpleTestCase):
    def _trace_record(self) -> logging.LogRecord:
        try:
            raise ConnectionError(RAW)
        except ConnectionError:
            info = sys.exc_info()
        context = {"name": "t", "id": "i", "description": "raised unexpected", "exc": repr(info[1]), "traceback": RAW}
        record = logging.LogRecord(
            "celery.app.trace", logging.ERROR, __file__, 1,
            "Task %(name)s[%(id)s] %(description)s: %(exc)s", context, info,  # type: ignore[arg-type]
        )
        record.data = context  # type: ignore[attr-defined]
        return record

    def test_celery_failure_record_is_scrubbed(self) -> None:
        record = self._trace_record()

        ExceptionScrubFilter().filter(record)
        RedactionFilter().filter(record)
        line = SingleLineFormatter().format(record)

        self.assertNotIn(SECRET, line)
        self.assertNotIn("dave@example.test", line)
        self.assertNotIn(SECRET, str(record.data))  # type: ignore[attr-defined]
        self.assertIn("ConnectionError", line)

    def test_record_without_exception_is_untouched_and_filter_never_raises(self) -> None:
        record = logging.LogRecord("django.request", logging.ERROR, __file__, 1, "plain %s", ("x",), None)

        self.assertTrue(ExceptionScrubFilter().filter(record))
        self.assertEqual(record.getMessage(), "plain x")

    def test_settings_wire_the_scrub_filter_on_celery_trace_and_django_request(self) -> None:
        for name in ("celery.app.trace", "django.request"):
            with self.subTest(logger=name):
                self.assertTrue(
                    any(isinstance(f, ExceptionScrubFilter) for f in logging.getLogger(name).filters)
                )
