"""
The notice task carries `recipient` / `new_address` kwargs. Celery logs them
verbatim on two paths this project does not control: the worker's
unregistered-task ERROR (body, headers, truncated at 1024 chars) and the
task-failure line. Neither may reach the console handler with an address.
"""

import io
import json
import logging
import sys
from types import SimpleNamespace

from celery.worker.consumer.consumer import UNKNOWN_TASK_ERROR, dump_body
from config.log_setup import (
    ExceptionScrubFilter,
    RedactionFilter,
    RequestContextFilter,
    SingleLineFormatter,
    redact,
)
from django.test import SimpleTestCase

RECIPIENT = "previous.owner@old-mailbox.example"
NEW_ADDRESS = "attacker-chosen@new-mailbox.example"


class _ConsoleCapture:
    """Attaches a handler wired like settings.LOGGING's console handler to a real logger."""

    def __init__(self, *, name: str) -> None:
        self.logger = logging.getLogger(name)
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(SingleLineFormatter())
        self.handler.addFilter(RequestContextFilter())
        self.handler.addFilter(RedactionFilter())
        self._propagate = self.logger.propagate

    def __enter__(self) -> "_ConsoleCapture":
        self.logger.addHandler(self.handler)
        self.logger.propagate = False
        return self

    def __exit__(self, *exc: object) -> None:
        self.logger.removeHandler(self.handler)
        self.logger.propagate = self._propagate

    @property
    def output(self) -> str:
        return self.stream.getvalue()


def _message(*, padding: int = 0) -> SimpleNamespace:
    body = json.dumps(
        [
            [],
            {
                "pad": "x" * padding,
                "user_id": 7,
                "purpose": "delivery",
                "recipient": RECIPIENT,
                "new_address": NEW_ADDRESS,
            },
            {"callbacks": None},
        ]
    ).encode()
    return SimpleNamespace(
        body=body,
        headers={
            "id": "abc",
            "task": "fragrance.tasks.send_recipient_changed_notice",
            "kwargsrepr": repr({"recipient": RECIPIENT, "new_address": NEW_ADDRESS}),
        },
        delivery_info={"exchange": "", "routing_key": "celery"},
    )


class UnknownTaskLineTests(SimpleTestCase):
    def _emit(self, *, message: SimpleNamespace) -> str:
        with _ConsoleCapture(name="celery.worker.consumer.consumer") as capture:
            try:
                raise KeyError("fragrance.tasks.send_recipient_changed_notice")
            except KeyError as exc:
                logging.getLogger("celery.worker.consumer.consumer").error(
                    UNKNOWN_TASK_ERROR,
                    exc,
                    dump_body(message, None),
                    message.headers,
                    message.delivery_info,
                    exc_info=True,
                )
        return capture.output

    def test_addresses_in_body_and_headers_are_scrubbed(self) -> None:
        output = self._emit(message=_message())

        self.assertIn("unregistered task", output)
        for address in (RECIPIENT, NEW_ADDRESS):
            self.assertNotIn(address, output)
        self.assertNotIn("old-mailbox", output)
        self.assertNotIn("new-mailbox", output)

    def test_no_padding_length_lets_a_truncated_body_leak_an_address(self) -> None:
        # Celery truncates the body at a word boundary, so an address is
        # dropped whole or kept whole; sweep the boundary to prove it either way.
        for padding in range(850, 1100, 2):
            with self.subTest(padding=padding):
                output = self._emit(message=_message(padding=padding))
                self.assertNotIn("previous.owner@", output)
                self.assertNotIn("old-mailbox", output)
                self.assertNotIn("attacker-chosen@", output)

    def test_truncated_address_patterns(self) -> None:
        for text in ("...,\"recipient\": \"previous.owner@old-mai...", "x previous.owner@..."):
            with self.subTest(text=text):
                self.assertNotIn("previous.owner@", redact(text=text))
        self.assertEqual(redact(text="see docs at user@host for details"), "see docs at user@host for details")


class TaskFailureLineTests(SimpleTestCase):
    def test_failure_line_and_context_carry_no_address(self) -> None:
        try:
            raise ConnectionRefusedError(f"smtp refused {RECIPIENT}")
        except ConnectionRefusedError:
            info = sys.exc_info()
        context = {
            "name": "fragrance.tasks.send_recipient_changed_notice",
            "id": "abc",
            "description": "raised unexpected",
            "exc": repr(info[1]),
            "traceback": f"Traceback ... {RECIPIENT}",
            "args": "()",
            "kwargs": repr({"recipient": RECIPIENT, "new_address": NEW_ADDRESS}),
        }
        logger = logging.getLogger("celery.app.trace")
        self.assertTrue(any(isinstance(f, ExceptionScrubFilter) for f in logger.filters))
        with _ConsoleCapture(name="celery.app.trace") as capture:
            with self.assertLogs("celery.app.trace", level="ERROR") as captured:
                logger.error(
                    "Task %(name)s[%(id)s] %(description)s: %(exc)s",
                    context,
                    exc_info=info,
                    extra={"data": context},
                )
        record = captured.records[0]

        self.assertNotIn(RECIPIENT, capture.output)
        self.assertNotIn(NEW_ADDRESS, capture.output)
        self.assertNotIn(RECIPIENT, str(record.data))  # type: ignore[attr-defined]
        self.assertNotIn(NEW_ADDRESS, str(record.data))  # type: ignore[attr-defined]
