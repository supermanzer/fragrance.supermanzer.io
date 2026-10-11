"""CRITICAL log records are mailed to ADMIN_EMAIL: gating, rate limiting, fallbacks, failure isolation."""

import logging
import threading
import time
from unittest.mock import MagicMock, patch

from config import log_setup
from config.log_setup import (
    ALERT_GLOBAL_CAP_PER_WINDOW,
    CriticalEmailHandler,
    RedactionFilter,
    RequestContextFilter,
)
from django.core import mail
from django.core.cache import cache
from django.core.cache.backends.locmem import LocMemCache
from django.core.mail import EmailMessage
from django.test import SimpleTestCase, override_settings
from fragrance.tasks import send_operator_critical_alert
from kombu.exceptions import OperationalError

ADMIN = "ops@example.test"


class _AlertTestBase(SimpleTestCase):
    """A private logger under `fragrance` wired to a fresh handler, isolated from the real tree."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        with log_setup._fallback_lock:
            log_setup._fallback_keys.clear()
            log_setup._fallback_global[:] = [0.0, 0]
        self.handler = CriticalEmailHandler()
        self.handler.addFilter(RequestContextFilter())
        self.handler.addFilter(RedactionFilter())
        self.logger = logging.getLogger("fragrance.logging_tests.alerts")
        self.logger.handlers = [self.handler]
        self.logger.propagate = False
        self.logger.setLevel(logging.DEBUG)
        self.addCleanup(self._teardown_logger)
        patcher = patch.object(send_operator_critical_alert, "apply_async")
        self.apply_async: MagicMock = patcher.start()
        self.addCleanup(patcher.stop)
        delivery = override_settings(CRITICAL_ALERT_DELIVERY="queue")
        delivery.enable()
        self.addCleanup(delivery.disable)

    def _teardown_logger(self) -> None:
        self.logger.handlers = []
        self.logger.propagate = True
        cache.clear()

    def _critical(self, message: str = "disk is on fire", **extra: object) -> None:
        self.logger.critical(message, extra=extra or None)


@override_settings(ADMIN_EMAIL=ADMIN, ENVIRONMENT="test")
class GatingTests(_AlertTestBase):
    def test_critical_is_queued_with_subject_and_body(self) -> None:
        self._critical("MONEY RECEIVED ON A LOCKED ACCOUNT: invoice in_123")

        self.assertEqual(self.apply_async.call_count, 1)
        kwargs = self.apply_async.call_args.kwargs
        self.assertIs(kwargs["retry"], False)
        self.assertIn("CRITICAL", kwargs["kwargs"]["subject"])
        self.assertIn("in_123", kwargs["kwargs"]["subject"])
        self.assertIn("MONEY RECEIVED", kwargs["kwargs"]["body"])
        self.assertIn("Environment: test", kwargs["kwargs"]["body"])

    def test_error_and_below_are_not_mailed(self) -> None:
        self.logger.error("an ordinary error")
        self.logger.warning("a warning")

        self.apply_async.assert_not_called()

    @override_settings(ADMIN_EMAIL="")
    def test_blank_admin_email_is_a_silent_no_op(self) -> None:
        self._critical()

        self.apply_async.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)

    def test_settings_wire_the_handler_onto_the_root_logger_with_redaction(self) -> None:
        handlers = [h for h in logging.getLogger().handlers if isinstance(h, CriticalEmailHandler)]

        self.assertEqual(len(handlers), 1)
        self.assertTrue(any(isinstance(f, RedactionFilter) for f in handlers[0].filters))
        self.assertIs(logging.getLogger("fragrance").propagate, True)
        self.assertFalse(
            [h for h in logging.getLogger("fragrance").handlers if isinstance(h, CriticalEmailHandler)]
        )

    def test_real_fragrance_logger_mails_a_redacted_alert(self) -> None:
        logging.getLogger("fragrance.billing").critical(
            "charge failed for %s using key %s", "dave@example.test", "sk_live_abcdef1234567890"
        )

        body = self.apply_async.call_args.kwargs["kwargs"]["body"]
        self.assertNotIn("dave@example.test", body)
        self.assertNotIn("sk_live_abcdef1234567890", body)
        self.assertIn("charge failed for [email]", body)
        self.assertEqual(self.apply_async.call_count, 1)

    def test_celery_and_django_criticals_are_mailed_once_each(self) -> None:
        logging.getLogger("celery.worker.consumer").critical("Unrecoverable error: broker is gone")
        logging.getLogger("django.request").critical("request machinery is on fire")

        self.assertEqual(self.apply_async.call_count, 2)
        subjects = [call.kwargs["kwargs"]["subject"] for call in self.apply_async.call_args_list]
        self.assertTrue(any("celery.worker.consumer" in subject for subject in subjects))
        self.assertTrue(any("django.request" in subject for subject in subjects))

    def test_error_from_the_real_tree_is_still_not_mailed(self) -> None:
        for name in ("fragrance.billing", "celery.app.trace", "django.request"):
            logging.getLogger(name).error("an ordinary error")

        self.apply_async.assert_not_called()

    def test_critical_logged_while_delivering_an_alert_is_not_queued_again(self) -> None:
        def log_critical_from_the_transport(**kwargs: object) -> None:
            logging.getLogger("kombu.connection").critical("transport exploded while publishing")

        self.apply_async.side_effect = log_critical_from_the_transport
        self._critical("original problem")

        self.assertEqual(self.apply_async.call_count, 1)


@override_settings(ADMIN_EMAIL=ADMIN)
class RateLimitTests(_AlertTestBase):
    def test_identical_alerts_are_suppressed_and_counted_in_the_next_send(self) -> None:
        with patch.object(log_setup, "_alert_digest", return_value="fixed"):
            for _ in range(3):
                self._critical("same problem")
            self.assertEqual(self.apply_async.call_count, 1)
            self.assertNotIn("Suppressed", self.apply_async.call_args.kwargs["kwargs"]["body"])

            cache.delete("critalert:gate:fixed")
            self._critical("same problem")

        self.assertEqual(self.apply_async.call_count, 2)
        self.assertIn("Suppressed:  2", self.apply_async.call_args.kwargs["kwargs"]["body"])

    def test_distinct_alert_keys_from_one_call_site_are_not_suppressed(self) -> None:
        for invoice in ("in_a", "in_b", "in_a"):
            self._critical("paid invoice with no period", alert_key=invoice)

        self.assertEqual(self.apply_async.call_count, 2)

    def test_formatted_ids_do_not_defeat_the_rate_limit(self) -> None:
        for number in range(3):
            self.logger.critical("run %s exploded", number)

        self.assertEqual(self.apply_async.call_count, 1)

    def test_global_cap_bounds_a_flood_of_distinct_alerts(self) -> None:
        for number in range(ALERT_GLOBAL_CAP_PER_WINDOW + 5):
            self._critical("flood", alert_key=f"key-{number}")

        self.assertEqual(self.apply_async.call_count, ALERT_GLOBAL_CAP_PER_WINDOW)

    def test_cache_outage_falls_back_to_an_in_process_limit(self) -> None:
        with patch.object(cache, "add", side_effect=ConnectionError("redis down")):
            for _ in range(3):
                self._critical("same problem while redis is down")

        self.assertEqual(self.apply_async.call_count, 1)

    def test_cache_outage_fallback_is_also_capped(self) -> None:
        with patch.object(cache, "add", side_effect=ConnectionError("redis down")):
            for number in range(ALERT_GLOBAL_CAP_PER_WINDOW + 5):
                self._critical("flood", alert_key=f"key-{number}")

        self.assertEqual(self.apply_async.call_count, ALERT_GLOBAL_CAP_PER_WINDOW)


@override_settings(ADMIN_EMAIL=ADMIN, DEFAULT_FROM_EMAIL="picks@example.test")
class DeliveryAndFailureIsolationTests(_AlertTestBase):
    def test_alert_task_sends_plain_operator_mail_without_reply_to(self) -> None:
        send_operator_critical_alert(subject="[Fragrance Journey] CRITICAL: x", body="details\n")

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [ADMIN])
        self.assertEqual(message.reply_to, [])
        self.assertEqual(message.from_email, "picks@example.test")

    @override_settings(ADMIN_EMAIL="")
    def test_alert_task_is_a_no_op_with_blank_admin_email(self) -> None:
        send_operator_critical_alert(subject="s", body="b")

        self.assertEqual(len(mail.outbox), 0)

    def test_publish_failure_falls_back_to_a_direct_bounded_send(self) -> None:
        self.apply_async.side_effect = OperationalError("broker down")

        with self.assertLogs("config.log_setup.internal", level=logging.ERROR):
            self._critical("broker is down and so are we")

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [ADMIN])
        self.assertIn("broker is down", mail.outbox[0].body)

    def test_direct_send_passes_a_request_timeout_to_the_mail_connection(self) -> None:
        with patch("django.core.mail.get_connection") as get_connection:
            get_connection.return_value = MagicMock()
            with patch.object(EmailMessage, "send", return_value=1):
                log_setup.deliver_alert_email(subject="s", body="b", timeout=7)

        self.assertEqual(get_connection.call_args.kwargs["requests_timeout"], 7)

    def test_handler_never_raises_when_publish_and_direct_send_both_fail(self) -> None:
        self.apply_async.side_effect = OperationalError("broker down")
        with patch.object(EmailMessage, "send", side_effect=RuntimeError("postmark 500")):
            with self.assertLogs("config.log_setup.internal", level=logging.ERROR) as logs:
                self._critical("everything is down")

        self.assertTrue(any("could not send CRITICAL alert email" in line for line in logs.output))

    def test_handler_never_raises_when_building_the_alert_fails(self) -> None:
        with patch.object(log_setup, "build_alert", side_effect=ValueError("bad record")):
            with self.assertLogs("config.log_setup.internal", level=logging.ERROR):
                self._critical("anything")

        self.apply_async.assert_not_called()

    def test_internal_failure_logger_cannot_recurse_into_the_mail_handler(self) -> None:
        # The handler is on root now, so the guarantee is that failures are
        # reported below CRITICAL, which the handler ignores.
        log_setup.internal_logger.error("handler failure report")
        log_setup.internal_logger.warning("handler failure report")
        log_setup.internal_logger.exception("handler failure report")

        self.apply_async.assert_not_called()

    def test_alert_task_failure_retries_instead_of_logging_critical(self) -> None:
        with patch.object(log_setup, "deliver_alert_email", side_effect=RuntimeError("postmark 500")):
            with patch.object(send_operator_critical_alert, "retry", side_effect=KeyError("retried")) as retry:
                with self.assertRaises(KeyError):
                    send_operator_critical_alert(subject="s", body="b")

        self.assertEqual(retry.call_args.kwargs["countdown"], 60)


@override_settings(ADMIN_EMAIL=ADMIN)
class SubjectTests(_AlertTestBase):
    def _subject(self, message: str) -> str:
        self._critical(message)
        return self.apply_async.call_args.kwargs["kwargs"]["subject"]

    def test_control_characters_are_stripped_from_the_subject(self) -> None:
        subject = self._subject("bad \x1b[31mred\x00 \x85 text\u2028more")

        self.assertFalse(any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F or c in "\u2028\u2029" for c in subject))
        self.assertIn("bad", subject)

    def test_empty_first_line_falls_through_to_the_first_real_line(self) -> None:
        subject = self._subject("\n\n   \nreal problem here\nsecond line")

        self.assertTrue(subject.endswith(": real problem here"))
        self.assertNotIn("second line", subject)

    def test_message_of_only_control_characters_gets_a_placeholder(self) -> None:
        subject = self._subject("\x1b\x00\n\x0b")

        self.assertTrue(subject.endswith("(no message)"))

    def test_subject_is_capped_and_single_line(self) -> None:
        subject = self._subject("x" * 1000)

        self.assertLessEqual(len(subject), 200)
        self.assertEqual(len(subject.splitlines()), 1)


@override_settings(ADMIN_EMAIL=ADMIN, CRITICAL_ALERT_ASYNC=True)
class BackgroundWorkerTests(_AlertTestBase):
    """CRITICAL alert I/O runs on the daemon worker, never on the logging caller."""

    def setUp(self) -> None:
        super().setUp()
        log_setup.stop_alert_worker()
        log_setup._dropped_alerts = 0
        self.release = threading.Event()
        self.addCleanup(self._stop_worker)

    def _stop_worker(self) -> None:
        self.release.set()
        log_setup.stop_alert_worker()
        leftovers = [t for t in threading.enumerate() if t.name == "critical-alert-mailer"]
        for thread in leftovers:
            thread.join(timeout=2)
        self.assertEqual([t for t in threading.enumerate() if t.name == "critical-alert-mailer"], [])

    def _block_rate_limit(self):
        def blocked(*, digest: str) -> tuple[bool, int]:
            self.release.wait(timeout=10)
            return True, 0

        return patch.object(log_setup, "_rate_limit", side_effect=blocked)

    def test_alert_is_delivered_by_the_worker_thread(self) -> None:
        threads: list[str] = []
        self.apply_async.side_effect = lambda **kwargs: threads.append(threading.current_thread().name)

        self._critical("from the request thread")
        self.assertTrue(log_setup.flush_alerts(timeout=5))

        self.assertEqual(threads, ["critical-alert-mailer"])
        self.assertEqual(self.apply_async.call_count, 1)

    def test_emit_returns_immediately_while_rate_limit_and_publish_are_stuck(self) -> None:
        with self._block_rate_limit():
            started = time.perf_counter()
            self._critical("a webhook is holding a row lock")
            elapsed = time.perf_counter() - started

            self.assertLess(elapsed, 0.5)
            self.apply_async.assert_not_called()
            self.release.set()
            self.assertTrue(log_setup.flush_alerts(timeout=5))

        self.assertEqual(self.apply_async.call_count, 1)

    def test_emit_returns_immediately_while_the_broker_publish_blocks(self) -> None:
        self.apply_async.side_effect = lambda **kwargs: self.release.wait(timeout=10)

        started = time.perf_counter()
        self._critical("broker is black-holed")
        elapsed = time.perf_counter() - started
        self.release.set()

        self.assertLess(elapsed, 0.5)
        self.assertTrue(log_setup.flush_alerts(timeout=5))

    def test_full_queue_drops_and_counts_without_blocking(self) -> None:
        with patch.object(log_setup, "ALERT_QUEUE_MAX", 2), self._block_rate_limit():
            started = time.perf_counter()
            for number in range(8):
                self._critical("flood", alert_key=f"k{number}")
                time.sleep(0.01)
            elapsed = time.perf_counter() - started

            self.assertLess(elapsed, 1.0)
            dropped = log_setup.dropped_alert_count()
            self.assertGreaterEqual(dropped, 4)
            self.assertLessEqual(dropped, 6)
            self.release.set()
            self.assertTrue(log_setup.flush_alerts(timeout=5))

        self.assertEqual(self.apply_async.call_count + dropped, 8)

    def test_publish_failure_falls_back_to_a_direct_send_on_the_worker(self) -> None:
        self.apply_async.side_effect = OperationalError("broker down")

        with self.assertLogs("config.log_setup.internal", level=logging.ERROR):
            self._critical("broker is down")
            self.assertTrue(log_setup.flush_alerts(timeout=5))

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [ADMIN])

    def test_worker_failure_is_logged_and_does_not_kill_the_worker(self) -> None:
        with patch.object(log_setup, "build_alert", side_effect=[ValueError("bad"), ("s", "b")]):
            with self.assertLogs("config.log_setup.internal", level=logging.ERROR):
                self._critical("first", alert_key="a")
                self.assertTrue(log_setup.flush_alerts(timeout=5))
            self._critical("second", alert_key="b")
            self.assertTrue(log_setup.flush_alerts(timeout=5))

        self.assertEqual(self.apply_async.call_count, 1)

    def test_queued_record_is_a_snapshot_of_the_original(self) -> None:
        record = logging.LogRecord(
            "fragrance.logging_tests.alerts", logging.CRITICAL, __file__, 7, "invoice %s locked", ("in_1",), None
        )
        with self._block_rate_limit():
            self.handler.handle(record)
            record.msg, record.args = "mutated by the console handler", ()
            self.release.set()
            self.assertTrue(log_setup.flush_alerts(timeout=5))

        self.assertIn("invoice in_1 locked", self.apply_async.call_args.kwargs["kwargs"]["body"])

    def test_cache_is_reached_from_the_worker_so_class_level_patching_applies(self) -> None:
        with patch.object(LocMemCache, "add", side_effect=ConnectionError("redis down")):
            for _ in range(3):
                self._critical("same problem while redis is down")
            self.assertTrue(log_setup.flush_alerts(timeout=5))

        self.assertEqual(self.apply_async.call_count, 1)

    def test_a_forked_child_starts_its_own_worker(self) -> None:
        self._critical("parent alert", alert_key="p")
        self.assertTrue(log_setup.flush_alerts(timeout=5))
        parent_queue, parent_thread = log_setup._worker_queue, log_setup._worker_thread
        self.addCleanup(lambda: parent_queue.put(log_setup._STOP))

        log_setup._worker_pid = -1  # what a child that inherited the module state would see
        self._critical("child alert", alert_key="c")
        self.assertTrue(log_setup.flush_alerts(timeout=5))

        self.assertIsNot(log_setup._worker_queue, parent_queue)
        self.assertIsNot(log_setup._worker_thread, parent_thread)
        self.assertEqual(self.apply_async.call_count, 2)

    def test_at_fork_reset_clears_inherited_state(self) -> None:
        self._critical("alert", alert_key="x")
        self.assertTrue(log_setup.flush_alerts(timeout=5))
        queue_before, thread_before = log_setup._worker_queue, log_setup._worker_thread
        self.addCleanup(lambda: queue_before.put(log_setup._STOP))

        log_setup._reset_worker_state_in_child()

        self.assertIsNone(log_setup._worker_queue)
        self.assertIsNone(log_setup._worker_thread)
        self.assertTrue(thread_before.is_alive())

    def test_flush_without_a_worker_is_trivially_true(self) -> None:
        self.assertTrue(log_setup.flush_alerts(timeout=0.1))

    def test_test_runner_runs_alerts_inline_so_no_thread_is_spawned(self) -> None:
        with override_settings(CRITICAL_ALERT_ASYNC=False):
            self._critical("inline")

        self.assertEqual(self.apply_async.call_count, 1)
        self.assertIsNone(log_setup._worker_queue)


@override_settings(ADMIN_EMAIL=ADMIN, DEFAULT_FROM_EMAIL="picks@example.test")
class DirectDeliveryTests(_AlertTestBase):
    """Processes with mail egress (backend, celery-worker) send from the alert thread itself."""

    def setUp(self) -> None:
        super().setUp()
        direct = override_settings(CRITICAL_ALERT_DELIVERY="direct")
        direct.enable()
        self.addCleanup(direct.disable)

    def test_direct_mode_mails_without_touching_the_broker(self) -> None:
        self._critical("worker is wedged")

        self.apply_async.assert_not_called()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [ADMIN])
        self.assertIn("worker is wedged", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].reply_to, [])

    def test_direct_failure_falls_back_to_the_queue(self) -> None:
        with patch.object(EmailMessage, "send", side_effect=RuntimeError("postmark 500")):
            with self.assertLogs("config.log_setup.internal", level=logging.ERROR):
                self._critical("postmark is down")

        self.assertEqual(self.apply_async.call_count, 1)
        self.assertIn("postmark is down", self.apply_async.call_args.kwargs["kwargs"]["body"])

    def test_handler_never_raises_when_direct_and_queue_both_fail(self) -> None:
        self.apply_async.side_effect = OperationalError("broker down")
        with patch.object(EmailMessage, "send", side_effect=RuntimeError("postmark 500")):
            with self.assertLogs("config.log_setup.internal", level=logging.ERROR) as logs:
                self._critical("everything is down")

        self.assertTrue(any("could not send CRITICAL alert email" in line for line in logs.output))

    def test_direct_mode_keeps_rate_limiting(self) -> None:
        for _ in range(3):
            self._critical("same problem")

        self.assertEqual(len(mail.outbox), 1)

    def test_queue_mode_still_publishes_the_task_first(self) -> None:
        with override_settings(CRITICAL_ALERT_DELIVERY="queue"):
            self._critical("beat cannot reach the mail provider")

        self.assertEqual(self.apply_async.call_count, 1)
        self.assertEqual(len(mail.outbox), 0)


class DefaultDeliverySelectionTests(SimpleTestCase):
    def test_beat_command_line_selects_queue(self) -> None:
        argv = ["/app/.venv/lib/python3.13/site-packages/celery/__main__.py", "-A", "config", "beat", "-l", "info"]
        self.assertEqual(log_setup.default_alert_delivery(argv=argv), "queue")

    def test_worker_gunicorn_manage_and_test_runs_select_direct(self) -> None:
        for argv in (
            ["celery", "-A", "config", "worker", "-l", "info", "--concurrency", "1"],
            ["/app/.venv/bin/gunicorn", "config.wsgi:application", "--workers", "3"],
            ["manage.py", "runserver", "0.0.0.0:8000"],
            ["manage.py", "test"],
            [],
        ):
            with self.subTest(argv=argv[:3]):
                self.assertEqual(log_setup.default_alert_delivery(argv=argv), "direct")


class ProcessLifecycleTests(SimpleTestCase):
    def test_worker_process_shutdown_flushes_pending_alerts(self) -> None:
        from celery.signals import worker_process_shutdown

        with patch.object(log_setup, "flush_alerts") as flush:
            worker_process_shutdown.send(sender=None, pid=1234, exitcode=0)

        flush.assert_called_once_with(timeout=3.0)

    def test_at_fork_reset_replaces_the_fallback_lock(self) -> None:
        before = log_setup._fallback_lock
        log_setup._reset_worker_state_in_child()

        self.assertIsNot(log_setup._fallback_lock, before)
        self.assertTrue(log_setup._fallback_lock.acquire(blocking=False))
        log_setup._fallback_lock.release()

    def test_a_held_fallback_lock_does_not_survive_the_fork_reset(self) -> None:
        self.assertTrue(log_setup._fallback_lock.acquire(blocking=False))
        try:
            log_setup._reset_worker_state_in_child()
            allowed, _ = log_setup._fallback_allow(digest="fork-test", now=time.time())
            self.assertTrue(allowed)
        finally:
            with log_setup._fallback_lock:
                log_setup._fallback_keys.clear()
                log_setup._fallback_global[:] = [0.0, 0]


class AlertDeliveryResolutionTests(SimpleTestCase):
    def test_blank_setting_falls_back_to_the_argv_default(self) -> None:
        from config.log_setup import resolve_alert_delivery

        self.assertEqual(resolve_alert_delivery(configured="", argv=["celery", "-A", "config", "beat"]), "queue")
        self.assertEqual(resolve_alert_delivery(configured="  ", argv=["gunicorn", "config.wsgi"]), "direct")

    def test_explicit_value_wins_and_is_case_insensitive(self) -> None:
        from config.log_setup import resolve_alert_delivery

        self.assertEqual(resolve_alert_delivery(configured="QUEUE", argv=["gunicorn"]), "queue")
        self.assertEqual(resolve_alert_delivery(configured="direct", argv=["celery", "beat"]), "direct")

    def test_a_typo_still_fails_fast(self) -> None:
        from config.log_setup import resolve_alert_delivery
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            resolve_alert_delivery(configured="qeue", argv=["gunicorn"])
