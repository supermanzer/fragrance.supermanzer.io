"""Credential and address scrubbing, plus the single-line console format."""

import logging
import sys
import time

import stripe
from config import log_setup
from config.log_setup import REDACTED, RedactionFilter, RequestContextFilter, SingleLineFormatter, redact
from django.test import SimpleTestCase
from fragrance.ai_providers.base import sanitized_exc_info

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjo3fQ.c2lnbmF0dXJlLXZhbHVl"


def _record(*, msg: str, args: tuple = (), exc_info: object = None, level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord(
        name="fragrance.test",
        level=level,
        pathname=__file__,
        lineno=10,
        msg=msg,
        args=args,
        exc_info=exc_info,  # type: ignore[arg-type]
        func="case",
    )


class RedactTests(SimpleTestCase):
    def test_credential_shapes_are_removed(self) -> None:
        cases = {
            "jwt": JWT,
            "bearer": "Bearer abc.def-ghi_jkl",
            "stripe secret": "sk_live_51HabcdefGHIJKL",
            "stripe test restricted": "rk_test_abcdefghijklmnop",
            "stripe publishable": "pk_live_abcdef123456",
            "webhook secret": "whsec_abcdefgh12345678",
            "anthropic": "sk-ant-api03-abcDEF_123-xyz",
            "openai": "sk-proj-AbCdEf1234567890",
            "openai masked": "sk-****************ZXY",
        }
        for label, secret in cases.items():
            with self.subTest(label):
                out = redact(text=f"call failed with credential {secret} while saving")
                self.assertNotIn(secret, out)
                self.assertTrue(out.startswith("call failed with credential "))
                self.assertTrue(out.endswith(" while saving"))

    def test_email_addresses_are_replaced(self) -> None:
        out = redact(text="Postmark rejected recipient dave.bowman+tag@mail.example.co.uk (406)")

        self.assertEqual(out, "Postmark rejected recipient [email] (406)")

    def test_ids_and_ordinary_text_are_left_alone(self) -> None:
        text = "invoice in_1Nabc for cus_Oxyz pi_3Qabc user 7 dispute dp_1Zzz run 12 sk (skipped)"

        self.assertEqual(redact(text=text), text)


class RedactionFilterTests(SimpleTestCase):
    def test_interpolated_message_is_redacted_and_template_is_kept(self) -> None:
        record = _record(msg="user %s sent %s", args=("dave@example.test", f"Bearer {JWT}"))

        RedactionFilter().filter(record)

        self.assertEqual(record.getMessage(), f"user [email] sent Bearer {REDACTED}")
        self.assertEqual(record.log_template, "user %s sent %s")  # type: ignore[attr-defined]

    def test_exception_text_and_stack_info_are_redacted(self) -> None:
        try:
            raise RuntimeError("provider said: key sk-ant-api03-SECRETSECRET for ops@example.test")
        except RuntimeError:
            record = _record(msg="boom", exc_info=sys.exc_info())
        record.stack_info = "Stack: token " + JWT

        RedactionFilter().filter(record)

        self.assertIsNotNone(record.exc_text)
        self.assertNotIn("SECRETSECRET", record.exc_text or "")
        self.assertNotIn("ops@example.test", record.exc_text or "")
        self.assertNotIn(JWT, record.stack_info or "")

    def test_filter_is_idempotent(self) -> None:
        record = _record(msg="token %s", args=(JWT,))
        redaction = RedactionFilter()

        redaction.filter(record)
        once = (record.getMessage(), record.log_template)  # type: ignore[attr-defined]
        redaction.filter(record)

        self.assertEqual((record.getMessage(), record.log_template), once)  # type: ignore[attr-defined]

    def test_unrenderable_record_does_not_raise(self) -> None:
        record = _record(msg="needs two args %s %s", args=("only-one",))

        self.assertTrue(RedactionFilter().filter(record))
        self.assertEqual(record.getMessage(), "[log record could not be rendered or redacted]")


class SanitizedExcInfoTests(SimpleTestCase):
    def test_traceback_keeps_frames_but_not_the_raw_sdk_message(self) -> None:
        # Built at runtime: a traceback also echoes the raising source line.
        raw_message = "boom " + "secret" + "-detail"
        try:
            try:
                raise ValueError("inner cause leaking sk_live_innerinnerinner")
            except ValueError as inner:
                raise stripe.APIConnectionError(raw_message) from inner
        except stripe.APIConnectionError as exc:
            info = sanitized_exc_info(exc=exc)

        text = "".join(logging.Formatter().formatException(info))  # type: ignore[arg-type]

        self.assertNotIn("secret-detail", text)
        self.assertNotIn("innerinnerinner", text)
        self.assertIn("APIConnectionError", text)
        self.assertIn("Could not reach Stripe", text)
        self.assertIn("test_redaction.py", text)


class SingleLineFormatterTests(SimpleTestCase):
    def _format(self, record: logging.LogRecord) -> str:
        RequestContextFilter().filter(record)
        return SingleLineFormatter().format(record)

    def test_message_newlines_are_escaped_onto_one_line(self) -> None:
        record = _record(msg="first\nsecond\r\nthird")

        line = self._format(record)

        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("first\\nsecond\\nthird", line)
        self.assertIn(" ERROR fragrance.test ", line)

    def test_traceback_lines_repeat_the_prefix_and_marker(self) -> None:
        try:
            raise RuntimeError("kaput")
        except RuntimeError:
            record = _record(msg="failed", exc_info=sys.exc_info())
        record.request_id = "req-12345678"  # type: ignore[attr-defined]
        prefix_fields = ("ERROR", "fragrance.test")

        lines = SingleLineFormatter().format(record).splitlines()

        self.assertGreater(len(lines), 2)
        for line in lines[1:]:
            self.assertIn(" | ", line)
            for field in prefix_fields:
                self.assertIn(field, line)
        self.assertTrue(lines[-1].endswith("RuntimeError: kaput"))


class DropMessageTests(SimpleTestCase):
    def test_drop_message_keeps_frames_and_class_but_not_the_text(self) -> None:
        raw_message = "connect failed redis://:" + "hunter2" + "@redis:6379/0"
        try:
            raise ConnectionError(raw_message)
        except ConnectionError as exc:
            info = sanitized_exc_info(exc=exc, drop_message=True)

        text = logging.Formatter().formatException(info)  # type: ignore[arg-type]

        self.assertNotIn("hunter2", text)
        self.assertIn("ConnectionError", text)
        self.assertIn("test_redaction.py", text)


class ControlCharacterEscapingTests(SimpleTestCase):
    def _format(self, record: logging.LogRecord) -> str:
        RequestContextFilter().filter(record)
        return SingleLineFormatter().format(record)

    def test_every_c0_c1_and_unicode_line_separator_is_escaped_in_the_message(self) -> None:
        for codepoint in [*range(0x00, 0x20), *range(0x7F, 0xA0), 0x2028, 0x2029]:
            char = chr(codepoint)
            if char in "\t\r":
                continue
            with self.subTest(codepoint=hex(codepoint)):
                line = self._format(_record(msg=f"a{char}b"))

                self.assertNotIn(char, line)
                self.assertEqual(len(line.split("\n")), 1)
                self.assertEqual(len(line.splitlines()), 1)

    def test_escapes_are_readable(self) -> None:
        line = self._format(_record(msg="x\x1b[31mred\x00 \x85\ny"))

        self.assertIn("x\\x1b[31mred\\x00\\u2028\\x85\\ny", line)

    def test_tab_is_kept_and_cr_is_dropped(self) -> None:
        line = self._format(_record(msg="a\tb\rc"))

        self.assertTrue(line.endswith("a\tbc"))

    def test_args_rendered_text_is_escaped(self) -> None:
        line = self._format(_record(msg="path %s", args=("/billing/\x1b]52;c;x\x07",)))

        self.assertNotIn("\x1b", line)
        self.assertNotIn("\x07", line)

    def test_traceback_continuation_lines_are_escaped_and_prefixed(self) -> None:
        try:
            raise RuntimeError("bad\x1b[2Jline\x0bone two\x85three\nfour")
        except RuntimeError:
            record = _record(msg="failed", exc_info=sys.exc_info())

        text = self._format(record)

        for char in ("\x1b", "\x0b", " ", "\x85"):
            self.assertNotIn(char, text)
        self.assertEqual(len(text.splitlines()), len(text.split("\n")))
        self.assertTrue(all(" | " in line for line in text.split("\n")[1:]))
        self.assertIn("\\x1b[2Jline\\x0bone\\u2028two\\x85three", text)

    def test_stack_info_is_escaped(self) -> None:
        record = _record(msg="stack")
        record.stack_info = "Stack (most recent call last):\n  frame \x1b[0m end"

        text = self._format(record)

        self.assertNotIn("\x1b", text)
        self.assertNotIn(" ", text)


class ExtendedRedactionTests(SimpleTestCase):
    def test_url_userinfo_is_redacted_but_host_survives(self) -> None:
        cases = {
            "redis://:hunter2secret@redis:6379/0": "redis://[REDACTED]@redis:6379/0",
            "postgres://app:s3cr3tpw@db:5432/fragrance": "postgres://[REDACTED]@db:5432/fragrance",
            "amqp://guest:guest@rabbit.internal.example.com/vhost": "amqp://[REDACTED]@rabbit.internal.example.com/vhost",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                out = redact(text=f"Error connecting to {raw}: refused")
                self.assertEqual(out, f"Error connecting to {expected}: refused")
                self.assertNotIn("hunter2secret", out)

    def test_authorization_headers_are_redacted(self) -> None:
        for raw in (
            "Authorization: Basic dXNlcjpodW50ZXIy",
            "authorization=Basic dXNlcjpodW50ZXIy",
            "Proxy-Authorization: Basic dXNlcjpodW50ZXIy",
            "'Authorization': 'Token abcdef0123456789'",
            "Authorization: Bearer abc.def.ghi",
        ):
            with self.subTest(raw=raw):
                out = redact(text=f"request headers {raw} end")
                self.assertNotIn("dXNlcjpodW50ZXIy", out)
                self.assertNotIn("abcdef0123456789", out)
                self.assertNotIn("abc.def.ghi", out)
                self.assertIn(REDACTED, out)

    def test_key_value_secrets_are_redacted(self) -> None:
        values = {
            "password=hunter2secret": "hunter2secret",
            "passwd: hunter2secret": "hunter2secret",
            "SECRET_KEY=django-insecure-abc123xyz": "django-insecure-abc123xyz",
            "STRIPE_SECRET_KEY=whatever123": "whatever123",
            "refresh_token=abcDEF123456": "abcDEF123456",
            "api_key: abcDEF123456": "abcDEF123456",
            "API-KEY=abcDEF123456": "abcDEF123456",
            "client_secret=abcDEF123456": "abcDEF123456",
            "{'password': 'hunter2secret', 'user': 'dave'}": "hunter2secret",
            '{"token": "hunter2secret"}': "hunter2secret",
            "POSTMARK_SERVER_TOKEN=0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0": "0f1e2d3c",
            "X-Postmark-Server-Token: 0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0": "0f1e2d3c",
        }
        for raw, secret in values.items():
            with self.subTest(raw=raw):
                out = redact(text=f"settings dump {raw} done")
                self.assertNotIn(secret, out)
                self.assertTrue(out.startswith("settings dump "))
                self.assertTrue(out.endswith(" done"))

    def test_postmark_token_without_separator_is_redacted(self) -> None:
        out = redact(text="Postmark server token 0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0 was rejected")

        self.assertNotIn("0f1e2d3c", out)

    def test_stripe_client_secrets_and_signature_are_redacted(self) -> None:
        cases = {
            "client_secret pi_3QabcDEF123_secret_Xyz987abcDEF": "Xyz987abcDEF",
            "seti_1Nabc_secret_ZZtop123456": "ZZtop123456",
            "value _secret_abcDEF123456 here": "abcDEF123456",
            "Stripe-Signature: t=1700000000,v1=" + "a1" * 32: "a1" * 32,
            "sig header t=1700000000,v1=" + "b2" * 32 + ",v0=" + "c3" * 32: "b2" * 32,
        }
        for raw, secret in cases.items():
            with self.subTest(raw=raw):
                self.assertNotIn(secret, redact(text=raw))

    def test_ordinary_ids_and_urls_are_untouched(self) -> None:
        texts = [
            "invoice in_1Nabc cus_Oxyz pi_3Qabc evt_1Zzz cs_live_a1b2c3 dp_1Zzz sub_1Abc",
            "Task fragrance.tasks.send_password_reset_email[3f2504e0-4f89-41d3-9a0c-0305e82c3301] retry: ok",
            "fetched https://example.com/path/to/page?x=1&y=2 and http://searxng:8080/search?q=abc",
            "GET https://example.com/a@b/c status 200",
            "max_tokens=1024 input_tokens: 55 password reset email queued for user 7",
            "uuid 3f2504e0-4f89-41d3-9a0c-0305e82c3301 run 12 token expired",
            "retry in 300s: attempt 2/6 at 2026-10-10T12:00:00+00:00",
        ]
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(redact(text=text), text)

    def test_emails_still_redacted_and_url_userinfo_handled_first(self) -> None:
        out = redact(text="to dave@example.com via redis://:pw12345@redis.internal.example.com:6379/0")

        self.assertEqual(out, f"to [email] via redis://{REDACTED}@redis.internal.example.com:6379/0")

    def test_redaction_is_idempotent(self) -> None:
        text = "Authorization: Basic abc password=xyz redis://:pw@h:1/0 dave@example.com pi_1_secret_ab"
        once = redact(text=text)

        self.assertEqual(redact(text=once), once)


class RedactionPerformanceTests(SimpleTestCase):
    def _assert_fast(self, text: str, label: str) -> None:
        started = time.perf_counter()
        redact(text=text)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.1, msg=f"{label} took {elapsed:.3f}s")

    def test_long_unbroken_tokens_do_not_cost_quadratic_time(self) -> None:
        size = 100_000
        shapes = {
            "alnum run": "a" * size,
            "run then @": "a" * size + "@",
            "@ then run": "@" + "a" * size,
            "a@ repeated": "a@" * (size // 2),
            "run with dots": "a." * (size // 2) + "@",
            "email-like local runs": ("a" * 500 + "@") * 200,
            "hyphen run": "a-" * (size // 2),
            "underscore run": "a_" * (size // 2),
            "secret-ish run": "token" * (size // 5),
            "sk run": "sk-" + "a" * size,
            "eyJ run": "eyJ" + "a" * size,
            "key= run": "password=" + "a" * size,
            "scheme run": "a" * size + "://",
            "url userinfo run": "redis://" + "a" * size,
        }
        for label, text in shapes.items():
            with self.subTest(label=label):
                self._assert_fast(text, label)

    def test_oversized_input_is_truncated_with_a_marker_not_passed_through(self) -> None:
        secret = "sk_live_" + "A" * 20
        text = "x" * (log_setup.MAX_REDACT_CHARS + 10) + secret

        out = redact(text=text)

        self.assertNotIn(secret, out)
        self.assertIn("truncated", out)
        self.assertLessEqual(len(out), log_setup.MAX_REDACT_CHARS + 60)
