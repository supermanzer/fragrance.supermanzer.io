"""Request-id middleware, the lazy user_id filter, and Celery context propagation."""

import logging
import weakref
from types import SimpleNamespace
from unittest.mock import MagicMock

from config import log_setup
from config.log_setup import REQUEST_ID_HEADER, RequestContextFilter, RequestIdMiddleware
from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase
from django.utils.functional import SimpleLazyObject


def _stamp() -> logging.LogRecord:
    record = logging.LogRecord(
        name="fragrance.test", level=logging.INFO, pathname=__file__, lineno=1, msg="m", args=(), exc_info=None
    )
    RequestContextFilter().filter(record)
    return record


class RequestIdMiddlewareTests(SimpleTestCase):
    def _run(self, *, inbound: str | None) -> tuple[HttpResponse, dict]:
        seen: dict = {}

        def view(request: object) -> HttpResponse:
            seen["during"] = log_setup.current_request_id()
            seen["attr"] = getattr(request, "request_id", None)
            seen["logged"] = _stamp().request_id  # type: ignore[attr-defined]
            return HttpResponse("ok")

        extra = {} if inbound is None else {"HTTP_X_REQUEST_ID": inbound}
        response = RequestIdMiddleware(view)(RequestFactory().get("/x/", **extra))
        return response, seen

    def test_safe_inbound_id_is_honoured_everywhere(self) -> None:
        response, seen = self._run(inbound="abc-12345.def_6")

        self.assertEqual(response[REQUEST_ID_HEADER], "abc-12345.def_6")
        self.assertEqual(seen, {"during": "abc-12345.def_6", "attr": "abc-12345.def_6", "logged": "abc-12345.def_6"})

    def test_unsafe_inbound_ids_are_replaced(self) -> None:
        for bad in ("short", "x" * 65, "has space in it", "inject\r\nSet-Cookie: a=b", "<script>alert(1)</script>", "abcdefgh\n", "abcdefgh\n2026 CRITICAL forged"):
            with self.subTest(bad=bad):
                response, seen = self._run(inbound=bad)

                self.assertNotEqual(response[REQUEST_ID_HEADER], bad)
                self.assertRegex(response[REQUEST_ID_HEADER], r"^[0-9a-f]{32}$")
                self.assertEqual(seen["during"], response[REQUEST_ID_HEADER])

    def test_missing_inbound_id_is_generated_and_unique(self) -> None:
        first, _ = self._run(inbound=None)
        second, _ = self._run(inbound=None)

        self.assertNotEqual(first[REQUEST_ID_HEADER], second[REQUEST_ID_HEADER])

    def test_context_is_cleared_after_the_request(self) -> None:
        self._run(inbound="abc-12345.def_6")

        self.assertIsNone(log_setup.current_request_id())
        self.assertEqual(_stamp().request_id, "")  # type: ignore[attr-defined]

    def test_full_stack_sets_the_response_header(self) -> None:
        response = Client().get("/definitely/not/a/route/", HTTP_X_REQUEST_ID="client-req-0001")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response[REQUEST_ID_HEADER], "client-req-0001")


class UserIdFilterTests(SimpleTestCase):
    def _with_request(self, *, user: object) -> str:
        request = SimpleNamespace(user=user)
        token = log_setup._request_var.set(request)
        try:
            return _stamp().user_id  # type: ignore[attr-defined]
        finally:
            log_setup._request_var.reset(token)

    def test_no_request_means_no_user_id(self) -> None:
        self.assertEqual(_stamp().user_id, "")  # type: ignore[attr-defined]

    def test_authenticated_user_pk_is_stamped(self) -> None:
        self.assertEqual(self._with_request(user=SimpleNamespace(pk=7)), "7")

    def test_unevaluated_lazy_user_is_never_evaluated(self) -> None:
        loader = MagicMock(side_effect=AssertionError("lazy user was evaluated by a log call"))

        self.assertEqual(self._with_request(user=SimpleLazyObject(loader)), "")
        loader.assert_not_called()

    def test_evaluated_lazy_user_is_used(self) -> None:
        lazy = SimpleLazyObject(lambda: SimpleNamespace(pk=42))
        _ = lazy.pk

        self.assertEqual(self._with_request(user=lazy), "42")

    def test_non_numeric_pk_is_not_logged(self) -> None:
        self.assertEqual(self._with_request(user=SimpleNamespace(pk=None)), "")
        self.assertEqual(self._with_request(user=SimpleNamespace(pk="dave@example.test")), "")


class CeleryContextTests(SimpleTestCase):
    def _task(self, *, name: str = "fragrance.tasks.demo", request_id: object = None) -> SimpleNamespace:
        request = SimpleNamespace(**({} if request_id is None else {"request_id": request_id}))
        return SimpleNamespace(name=name, request=request)

    def test_publish_copies_the_active_request_id_into_headers(self) -> None:
        token = log_setup._request_id_var.set("req-abcdef12")
        try:
            headers: dict = {}
            log_setup._on_before_task_publish(headers=headers, body=None)
        finally:
            log_setup._request_id_var.reset(token)

        self.assertEqual(headers, {"request_id": "req-abcdef12"})

    def test_publish_outside_a_request_adds_nothing(self) -> None:
        headers: dict = {}

        log_setup._on_before_task_publish(headers=headers)

        self.assertEqual(headers, {})

    def test_signal_receivers_are_connected(self) -> None:
        from celery.signals import before_task_publish, setup_logging, task_postrun, task_prerun

        for signal, receiver in (
            (before_task_publish, log_setup._on_before_task_publish),
            (task_prerun, log_setup._on_task_prerun),
            (task_postrun, log_setup._on_task_postrun),
            (setup_logging, log_setup._on_setup_logging),
        ):
            with self.subTest(signal=signal.name if hasattr(signal, "name") else str(signal)):
                registered = [r() if isinstance(r, weakref.ref) else r for _, r in signal.receivers]
                self.assertIn(receiver, registered)

    def test_prerun_stamps_task_context_and_inherited_request_id_then_postrun_clears(self) -> None:
        log_setup._on_task_prerun(task_id="task-0001", task=self._task(request_id="req-abcdef12"))

        record = _stamp()
        self.assertEqual(record.request_id, "req-abcdef12")  # type: ignore[attr-defined]
        self.assertEqual(record.task_name, "fragrance.tasks.demo")  # type: ignore[attr-defined]
        self.assertEqual(record.task_id, "task-0001")  # type: ignore[attr-defined]

        log_setup._on_task_postrun(task_id="task-0001")

        cleared = _stamp()
        self.assertEqual(cleared.request_id, "")  # type: ignore[attr-defined]
        self.assertEqual(cleared.task_name, "")  # type: ignore[attr-defined]

    def test_unsafe_inherited_request_id_is_ignored(self) -> None:
        log_setup._on_task_prerun(task_id="task-0002", task=self._task(request_id="bad id\n"))
        self.addCleanup(log_setup._on_task_postrun, task_id="task-0002")

        self.assertEqual(_stamp().request_id, "")  # type: ignore[attr-defined]


class RequestIdPredicateTests(SimpleTestCase):
    def test_trailing_newline_is_rejected(self) -> None:
        self.assertFalse(log_setup.is_safe_request_id(value="abcdefgh\n"))
        self.assertFalse(log_setup.is_safe_request_id(value="abcdefgh\r\n"))
        self.assertTrue(log_setup.is_safe_request_id(value="abcdefgh"))
