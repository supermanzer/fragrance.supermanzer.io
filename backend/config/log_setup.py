"""
Logging building blocks referenced by settings.LOGGING and wired into Celery.

Everything here must stay importable before the Django app registry is ready:
Django applies LOGGING during django.setup(), so models, the cache framework,
the mail framework and fragrance.tasks are only ever imported inside the
methods that run at log time.

Pieces:
  * request/task context (contextvars) plus the middleware and Celery signal
    handlers that populate it, and RequestContextFilter, which stamps it onto
    records;
  * RedactionFilter, which scrubs credential shapes and email addresses from messages, tracebacks
    and stack info;
  * SingleLineFormatter, the greppable container-log format;
  * CriticalEmailHandler, which mails CRITICAL records to settings.ADMIN_EMAIL.
"""

import atexit
import copy
import hashlib
import logging
import os
import queue
import re
import socket
import threading
import time
import uuid
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any, Callable

REQUEST_ID_HEADER = "X-Request-ID"
TASK_REQUEST_ID_HEADER = "request_id"
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
_request_var: ContextVar[Any] = ContextVar("http_request", default=None)
_task_var: ContextVar[dict[str, str] | None] = ContextVar("celery_task", default=None)

# Failures inside the alert handler are reported here. The handler is on the
# root logger, so this logger does reach it: it must only ever log below
# CRITICAL (the handler ignores those), and _alert_state stops a CRITICAL
# raised while an alert is being delivered from being queued again.
internal_logger = logging.getLogger("config.log_setup.internal")


def is_safe_request_id(*, value: object) -> bool:
    return isinstance(value, str) and bool(_SAFE_REQUEST_ID.fullmatch(value))


def current_request_id() -> str | None:
    return _request_id_var.get()


# ---------------------------------------------------------------------------
# Request context
# ---------------------------------------------------------------------------


class RequestIdMiddleware:
    """
    Assigns every request an id (an inbound X-Request-ID is honoured only when
    it is a short safe token), exposes it as request.request_id and in the
    response header, and keeps the HttpRequest in a contextvar so log records
    can read the authenticated user lazily. DRF authenticates inside the view,
    after middleware has run, so user_id cannot be captured here.
    """

    def __init__(self, get_response: Callable) -> None:
        self.get_response = get_response

    def __call__(self, request: Any) -> Any:
        inbound = request.META.get("HTTP_X_REQUEST_ID", "")
        request_id = inbound if is_safe_request_id(value=inbound) else uuid.uuid4().hex
        request.request_id = request_id
        id_token = _request_id_var.set(request_id)
        request_token = _request_var.set(request)
        try:
            response = self.get_response(request)
        finally:
            _request_var.reset(request_token)
            _request_id_var.reset(id_token)
        response[REQUEST_ID_HEADER] = request_id
        return response


def _current_user_id() -> str | None:
    """
    The authenticated user's numeric id, or None. Never evaluates a lazy user:
    that could run a DB query from inside a logging call.
    """
    from django.utils.functional import empty

    request = _request_var.get()
    if request is None:
        return None
    user = request.__dict__.get("user")
    if user is None:
        return None
    if getattr(user, "_wrapped", None) is empty:
        return None
    pk = getattr(user, "pk", None)
    return str(pk) if isinstance(pk, int) else None


class RequestContextFilter(logging.Filter):
    """Stamps request_id, user_id, task_name and task_id onto every record ('' when absent)."""

    def filter(self, record: logging.LogRecord) -> bool:
        task = _task_var.get() or {}
        record.request_id = _request_id_var.get() or ""
        record.user_id = _current_user_id() or ""
        record.task_name = task.get("task_name", "")
        record.task_id = task.get("task_id", "")
        return True


# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------

_task_tokens: dict[str, list[tuple[ContextVar, Token]]] = {}


def _on_before_task_publish(*, headers: dict | None = None, **kwargs: Any) -> None:
    request_id = _request_id_var.get()
    if request_id and headers is not None:
        headers[TASK_REQUEST_ID_HEADER] = request_id


def _on_task_prerun(*, task_id: str | None = None, task: Any = None, **kwargs: Any) -> None:
    if task_id is None or task is None:
        return
    tokens: list[tuple[ContextVar, Token]] = [
        (_task_var, _task_var.set({"task_name": task.name, "task_id": task_id}))
    ]
    inherited = getattr(task.request, TASK_REQUEST_ID_HEADER, None)
    if is_safe_request_id(value=inherited):
        tokens.append((_request_id_var, _request_id_var.set(inherited)))
    _task_tokens[task_id] = tokens


def _on_task_postrun(*, task_id: str | None = None, **kwargs: Any) -> None:
    for var, token in reversed(_task_tokens.pop(task_id or "", [])):
        try:
            var.reset(token)
        except ValueError:
            var.set(None)


def _on_worker_process_shutdown(**kwargs: Any) -> None:
    flush_alerts(timeout=3.0)


def _on_setup_logging(**kwargs: Any) -> None:
    from logging.config import dictConfig

    from django.conf import settings

    dictConfig(settings.LOGGING)


def connect_celery_signals() -> None:
    """
    Called from config/celery.py. Connecting a receiver to setup_logging makes
    Celery skip its own logging setup (it would otherwise hijack the root
    logger and replace our handlers/format) in both worker and beat, so
    settings.LOGGING is what they actually run with.
    """
    from celery.signals import (
        before_task_publish,
        setup_logging,
        task_postrun,
        task_prerun,
        worker_process_shutdown,
    )

    setup_logging.connect(_on_setup_logging, weak=False)
    # billiard pool children leave through os._exit, which skips atexit, so the
    # at-exit flush never runs for a CRITICAL logged inside a task.
    worker_process_shutdown.connect(_on_worker_process_shutdown, weak=False)
    before_task_publish.connect(_on_before_task_publish, weak=False)
    task_prerun.connect(_on_task_prerun, weak=False)
    task_postrun.connect(_on_task_postrun, weak=False)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

REDACTED = "[REDACTED]"
# Redaction input is capped: truncation (with a marker) rather than redacting a
# prefix and passing the tail through, which would leak.
MAX_REDACT_CHARS = 50_000
# Scheme length is bounded: an open-ended run of [A-Za-z0-9+.-] rescans per start position.
_URL_SCHEME = r"\b[A-Za-z][A-Za-z0-9+.-]{0,31}://"
_KEY_VALUE_SEP = r"""["']?\s*[:=]\s*["']?"""
_UUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
_REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), REDACTED),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {REDACTED}"),
    # URL userinfo (redis://:pw@host, postgres://user:pw@host). Runs before the
    # email pattern, which would otherwise eat part of it.
    (re.compile(rf"({_URL_SCHEME})[^/\s@?#]+@"), rf"\1{REDACTED}@"),
    # Authorization / Proxy-Authorization header or key=value, with or without a scheme word.
    (
        re.compile(
            rf"(?i)(\b(?:proxy-)?authorization\b{_KEY_VALUE_SEP})(?:(?:basic|bearer|token|digest)\s+)?[^\s,;\"']+"
        ),
        rf"\1{REDACTED}",
    ),
    # password=..., "api_key": "...", SECRET_KEY=..., refresh_token: ..., POSTMARK_SERVER_TOKEN=...
    # The keyword may carry a prefix (client_secret) and a trailing _key; `max_tokens` is not matched.
    (
        re.compile(
            rf"(?i)(\b[A-Za-z0-9_]*(?:password|passwd|pwd|secret|token|api[_-]?key)(?:_key)?\b{_KEY_VALUE_SEP})"
            r"[^\s,;\"'&]+"
        ),
        rf"\1{REDACTED}",
    ),
    # Postmark server token written without a separator ("Postmark server token <uuid>").
    (re.compile(rf"(?i)(\bpostmark[\w\s-]{{0,30}}?\b(?:token|key)\b[\s:=\"']*){_UUID}"), rf"\1{REDACTED}"),
    # Stripe-Signature header value (t=...,v1=<hex>[,v0=<hex>]) with or without the header name.
    (re.compile(r"\bt=\d{6,},v[01]=[0-9a-f]{16,}(?:,v[01]=[0-9a-f]{16,})*"), REDACTED),
    # Stripe client secrets (pi_..._secret_..., seti_..., src_...) and any other *_secret_<token>.
    (re.compile(r"\b[a-z]{2,5}_[A-Za-z0-9]+_secret_[A-Za-z0-9]+"), REDACTED),
    (re.compile(r"_secret_[A-Za-z0-9]+"), f"_secret_{REDACTED}"),
    (re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]+"), REDACTED),
    (re.compile(r"\b(?:sk|rk)_[A-Za-z0-9_]{12,}"), REDACTED),
    (re.compile(r"\bwhsec_[A-Za-z0-9]{8,}"), REDACTED),
    # Anthropic (sk-ant-...), OpenAI (sk-..., sk-proj-...) and the vendor-masked
    # form OpenAI echoes back in a 401 ("sk-****...ZXY").
    (re.compile(r"\bsk-[A-Za-z0-9_*.-]{4,}"), REDACTED),
]
# Mail-provider errors echo the recipient; user ids are logged instead. The
# lookbehind restricts attempts to the start of a local-part run and the
# possessive quantifier stops it backtracking, so a long unbroken token costs
# one linear scan rather than one per start position.
_EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]++@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)


# An address cut by a truncating logger (Celery caps the unknown-task body at
# 1024 chars and appends "...") has no TLD left for _EMAIL_PATTERN: scrub
# local@partial-domain when it runs into that marker.
_TRUNCATED_EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]++@[A-Za-z0-9-]*+(?:\.[A-Za-z0-9-]++)*+(?=\.\.\.)"
)


def redact(*, text: str) -> str:
    if len(text) > MAX_REDACT_CHARS:
        text = f"{text[:MAX_REDACT_CHARS]}...[truncated {len(text) - MAX_REDACT_CHARS} chars]"
    for pattern, replacement in _REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    if "@" in text:
        text = _EMAIL_PATTERN.sub("[email]", text)
        text = _TRUNCATED_EMAIL_PATTERN.sub("[email]", text)
    return text


class RedactionFilter(logging.Filter):
    """
    Rewrites the record in place: the interpolated message, the formatted
    traceback (record.exc_text, which Formatter reuses rather than
    re-rendering) and stack_info. Idempotent, so it is safe on several handlers.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if not hasattr(record, "log_template"):
                record.log_template = str(record.msg)
            record.msg = redact(text=record.getMessage())
            record.args = ()
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact(text=record.exc_text)
            if record.stack_info:
                record.stack_info = redact(text=record.stack_info)
        except Exception:
            record.msg = "[log record could not be rendered or redacted]"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


class ExceptionScrubFilter(logging.Filter):
    """
    Defence in depth for loggers whose records we do not build: celery.app.trace
    (failure lines carry safe_repr(raw exception) and exc_info) and
    django.request (exc_info of the view's raw exception). Replaces exc_info
    with the sanitized stand-in from fragrance.ai_providers.base and rewrites
    Celery's `exc`/`traceback` context entries. Retry lines have no exc_info;
    those are fixed at the source by sanitizing the exception before
    self.retry (fragrance/tasks.py). Never raises.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            data = getattr(record, "data", None)
            if isinstance(data, dict):
                # Celery's context carries the task's args/kwargs reprs
                # (recipient addresses for the notice task).
                scrubbed = {
                    key: redact(text=value) if key in ("args", "kwargs") and isinstance(value, str) else value
                    for key, value in data.items()
                }
                record.data = scrubbed
            info = record.exc_info
            if not (isinstance(info, tuple) and len(info) == 3 and isinstance(info[1], Exception)):
                return True
            from fragrance.ai_providers.base import (
                sanitize_sdk_error_message,
                sanitized_exc_info,
            )

            exc = info[1]
            safe = sanitized_exc_info(exc=exc)
            record.exc_info, record.exc_text = safe, None
            description = redact(
                text=f"{type(exc).__name__}({sanitize_sdk_error_message(exc=exc)!r})"
            )
            if isinstance(record.args, dict) and "exc" in record.args:
                record.args = {**record.args, "exc": description, "traceback": ""}
            data = getattr(record, "data", None)
            if isinstance(data, dict):
                record.data = {**data, "exc": description, "traceback": ""}
        except Exception:
            record.exc_info = None
            record.exc_text = None
            record.args = ()
            record.msg = "[log record could not be scrubbed]"
        return True


# ---------------------------------------------------------------------------
# Console format
# ---------------------------------------------------------------------------


def _context_tag(*, record: logging.LogRecord) -> str:
    parts = []
    for label, attr in (
        ("request_id", "request_id"),
        ("user_id", "user_id"),
        ("task", "task_name"),
        ("task_id", "task_id"),
    ):
        value = getattr(record, attr, "")
        if value:
            parts.append(f"{label}={value}")
    return f" [{' '.join(parts)}]" if parts else ""


_CONTROL_CHARS = re.compile("[\x00-\x08\x0a-\x1f\x7f-\x9f\u2028\u2029]")


def _escape_match(match: re.Match[str]) -> str:
    char = match.group()
    if char == "\n":
        return "\\n"
    if char == "\r":
        return ""
    return char.encode("unicode_escape").decode("ascii")


def escape_control_chars(*, text: str) -> str:
    """
    Makes text safe to put on one terminal line: a newline becomes the two
    characters backslash-n, a carriage return is dropped, and every other
    C0/C1 control character (ESC, NUL, NEL, ...) and U+2028/U+2029 becomes its
    \\xNN / \\uNNNN escape. Tab is kept.
    """
    return _CONTROL_CHARS.sub(_escape_match, text)


class SingleLineFormatter(logging.Formatter):
    """
    One record, one greppable line:
      2026-10-10T12:00:00.123+00:00 ERROR fragrance.billing billing:grant [request_id=... user_id=7] message
    Control characters inside the message are escaped (see escape_control_chars).
    A traceback follows as continuation lines, each repeating the
    timestamp/level/logger/context prefix and a `| ` marker, so
    `grep request_id=...` returns the whole story.
    """

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds")
        tag = escape_control_chars(text=_context_tag(record=record))
        prefix = f"{stamp} {record.levelname} {record.name}"
        message = escape_control_chars(text=record.getMessage())
        lines = [f"{prefix} {record.module}:{record.funcName}{tag} {message}"]
        extra_text = []
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            extra_text.append(record.exc_text)
        if record.stack_info:
            extra_text.append(self.formatStack(record.stack_info))
        for block in extra_text:
            # Split on \n only: str.splitlines() would also consume VT, FF, FS,
            # NEL and U+2028 before they could be escaped.
            for line in block.replace("\r\n", "\n").rstrip("\n").split("\n"):
                lines.append(f"{prefix}{tag} | {escape_control_chars(text=line)}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# CRITICAL -> operator email
# ---------------------------------------------------------------------------

ALERT_WINDOW_SECONDS = 15 * 60
# Outlives the gate key so the suppressed count is still there for the next send.
ALERT_COUNT_TTL_SECONDS = 4 * ALERT_WINDOW_SECONDS
ALERT_GLOBAL_CAP_PER_WINDOW = 20
ALERT_DIRECT_SEND_TIMEOUT_SECONDS = 10
_FALLBACK_MAX_KEYS = 256

_fallback_lock = threading.Lock()
_fallback_keys: dict[str, list[float | int]] = {}
_fallback_global: list[float | int] = [0.0, 0]


def _fallback_allow(*, digest: str, now: float) -> tuple[bool, int]:
    """In-process limiter used only when the cache is unreachable: fails open, but capped."""
    with _fallback_lock:
        window_start, sent = _fallback_global
        if now - window_start >= ALERT_WINDOW_SECONDS:
            _fallback_global[:] = [now, 0]
            _fallback_keys.clear()
            sent = 0
        if sent >= ALERT_GLOBAL_CAP_PER_WINDOW:
            return False, 0
        entry = _fallback_keys.get(digest)
        if entry is not None and now - entry[0] < ALERT_WINDOW_SECONDS:
            entry[1] = int(entry[1]) + 1
            return False, 0
        if len(_fallback_keys) >= _FALLBACK_MAX_KEYS and digest not in _fallback_keys:
            return False, 0
        suppressed = int(entry[1]) if entry is not None else 0
        _fallback_keys[digest] = [now, 0]
        _fallback_global[1] = sent + 1
        return True, suppressed


def _alert_digest(*, record: logging.LogRecord) -> str:
    """
    Keyed on logger + call site + message *template* (never the formatted
    text, whose ids vary per event). A call site that handles many distinct
    subjects can pass extra={"alert_key": <id>} to keep them separate.
    """
    raw = "|".join(
        [
            record.name,
            record.pathname,
            str(record.lineno),
            str(getattr(record, "log_template", record.msg)),
            str(getattr(record, "alert_key", "")),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:20]


def _rate_limit(*, digest: str) -> tuple[bool, int]:
    """Returns (send, suppressed_since_last_send). Fails open to an in-process cap."""
    now = time.time()
    try:
        from django.core.cache import cache

        gate_key = f"critalert:gate:{digest}"
        count_key = f"critalert:count:{digest}"
        if not cache.add(gate_key, 1, timeout=ALERT_WINDOW_SECONDS):
            cache.add(count_key, 0, timeout=ALERT_COUNT_TTL_SECONDS)
            cache.incr(count_key)
            return False, 0
        suppressed = int(cache.get(count_key, 0) or 0)
        cache.delete(count_key)
        global_key = "critalert:global"
        cache.add(global_key, 0, timeout=ALERT_WINDOW_SECONDS)
        if cache.incr(global_key) > ALERT_GLOBAL_CAP_PER_WINDOW:
            cache.add(count_key, 0, timeout=ALERT_COUNT_TTL_SECONDS)
            cache.incr(count_key, delta=suppressed + 1)
            return False, 0
        return True, suppressed
    except Exception:
        return _fallback_allow(digest=digest, now=now)


def build_alert(*, record: logging.LogRecord, suppressed: int) -> tuple[str, str]:
    """(subject, body) for a CRITICAL record. Expects the record to be redacted."""
    from django.conf import settings

    message = record.getMessage()
    first_line = ""
    for line in message.splitlines():
        first_line = " ".join(_CONTROL_CHARS.sub(" ", line).split())
        if first_line:
            break
    subject = f"[Fragrance Journey] CRITICAL: {record.name}: {first_line or '(no message)'}"[:200]
    stamp = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"Time:        {stamp}",
        f"Logger:      {record.name}",
        f"Location:    {record.pathname}:{record.lineno} ({record.funcName})",
        f"Host:        {socket.gethostname()}",
        f"Environment: {getattr(settings, 'ENVIRONMENT', '') or '(unset)'}",
    ]
    for label, attr in (
        ("Request id:", "request_id"),
        ("User id:", "user_id"),
        ("Task:", "task_name"),
        ("Task id:", "task_id"),
    ):
        value = getattr(record, attr, "")
        if value:
            lines.append(f"{label:<12} {value}")
    if suppressed:
        lines.append(
            f"Suppressed:  {suppressed} identical alert(s) were withheld since the last one "
            f"(rate limit: one per {ALERT_WINDOW_SECONDS // 60} minutes)."
        )
    lines += ["", message]
    if record.exc_text:
        lines += ["", record.exc_text]
    if record.stack_info:
        lines += ["", record.stack_info]
    return subject, "\n".join(lines) + "\n"


def deliver_alert_email(*, subject: str, body: str, timeout: float = ALERT_DIRECT_SEND_TIMEOUT_SECONDS) -> int:
    """
    Sends an operator alert to ADMIN_EMAIL; 0 when it is blank. No reply_to:
    operator mail is not a conversation. requests_timeout bounds the Postmark
    call (anymail's default is 30s). Raises on failure; callers decide.
    """
    from django.conf import settings
    from django.core.mail import EmailMessage, get_connection

    recipient = settings.ADMIN_EMAIL
    if not recipient:
        return 0
    connection = get_connection(fail_silently=False, requests_timeout=timeout)
    return EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[recipient],
        connection=connection,
    ).send(fail_silently=False)


# ---------------------------------------------------------------------------
# Alert worker: all alert I/O (rate-limit cache, broker publish, direct send)
# happens on one daemon thread per process, never on the logging caller.
# ---------------------------------------------------------------------------

ALERT_QUEUE_MAX = 50
_STOP = object()

_worker_lock = threading.Lock()
_worker_queue: "queue.Queue[Any] | None" = None
_worker_thread: threading.Thread | None = None
_worker_pid: int | None = None
_dropped_alerts = 0


def _reset_worker_state_in_child() -> None:
    # A fork copies the queue but not the thread (and may copy a held lock):
    # the child starts clean and lazily spawns its own worker on first use.
    global _fallback_lock, _worker_lock, _worker_queue, _worker_thread, _worker_pid, _dropped_alerts
    _fallback_lock = threading.Lock()
    _worker_lock = threading.Lock()
    _worker_queue = None
    _worker_thread = None
    _worker_pid = None
    _dropped_alerts = 0


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_worker_state_in_child)


def _worker_loop(work: "queue.Queue[Any]") -> None:
    while True:
        item = work.get()
        try:
            if item is _STOP:
                return
            _process_alert(record=item)
        except Exception:
            try:
                internal_logger.exception("could not send CRITICAL alert email")
            except Exception:
                pass
        finally:
            work.task_done()


def _ensure_worker() -> "queue.Queue[Any]":
    global _worker_queue, _worker_thread, _worker_pid
    with _worker_lock:
        pid = os.getpid()
        if _worker_queue is None or _worker_pid != pid:
            _worker_queue = queue.Queue(maxsize=ALERT_QUEUE_MAX)
            _worker_thread = None
            _worker_pid = pid
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(
                target=_worker_loop, args=(_worker_queue,), name="critical-alert-mailer", daemon=True
            )
            _worker_thread.start()
        return _worker_queue


def _enqueue(*, record: logging.LogRecord) -> None:
    global _dropped_alerts
    try:
        _ensure_worker().put_nowait(record)
    except queue.Full:
        _dropped_alerts += 1
        if _dropped_alerts == 1 or _dropped_alerts % 100 == 0:
            internal_logger.warning(
                "CRITICAL alert queue full; %d alert(s) dropped in this process", _dropped_alerts
            )


def dropped_alert_count() -> int:
    return _dropped_alerts


def flush_alerts(*, timeout: float = 5.0) -> bool:
    """Waits until every queued alert has been processed. False on timeout."""
    work = _worker_queue
    if work is None or _worker_pid != os.getpid():
        return True
    deadline = time.monotonic() + timeout
    with work.all_tasks_done:
        while work.unfinished_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            work.all_tasks_done.wait(remaining)
    return True


def stop_alert_worker(*, timeout: float = 5.0) -> None:
    """Drains and joins this process's worker (tests, orderly shutdown). A later alert starts a new one."""
    global _worker_queue, _worker_thread, _worker_pid
    with _worker_lock:
        work, thread, owner = _worker_queue, _worker_thread, _worker_pid
        _worker_queue, _worker_thread, _worker_pid = None, None, None
    if work is None or thread is None or owner != os.getpid():
        return
    try:
        work.put(_STOP, timeout=timeout)
    except queue.Full:
        return
    thread.join(timeout=timeout)


def _flush_at_exit() -> None:
    flush_alerts(timeout=3.0)


atexit.register(_flush_at_exit)


# Set while this thread is inside _process_alert. The handler now sits on the
# root logger, so a CRITICAL raised by a library the alert path itself calls
# (kombu, redis, the mail backend) would otherwise be queued as a new alert.
_alert_state = threading.local()


def _process_alert(*, record: logging.LogRecord) -> None:
    _alert_state.active = True
    try:
        send, suppressed = _rate_limit(digest=_alert_digest(record=record))
        if not send:
            return
        subject, body = build_alert(record=record, suppressed=suppressed)
        _dispatch_alert(subject=subject, body=body)
    finally:
        _alert_state.active = False


def default_alert_delivery(*, argv: list[str]) -> str:
    """"queue" for the celery beat process (no mail egress), "direct" for everything else."""
    return "queue" if "beat" in argv[1:] else "direct"


def resolve_alert_delivery(*, configured: str, argv: list[str]) -> str:
    """
    The delivery mode for this process. A blank setting (a placeholder
    `CRITICAL_ALERT_DELIVERY=` line) means "auto"; a non-blank value that is not
    a known mode fails fast, since settings import is on the path of every process.
    """
    from django.core.exceptions import ImproperlyConfigured

    mode = configured.strip().lower() or default_alert_delivery(argv=argv)
    if mode not in ("direct", "queue"):
        raise ImproperlyConfigured("CRITICAL_ALERT_DELIVERY must be 'direct' or 'queue'")
    return mode


def _queue_alert(*, subject: str, body: str) -> None:
    from fragrance.tasks import send_operator_critical_alert

    send_operator_critical_alert.apply_async(kwargs={"subject": subject, "body": body}, retry=False)


def _dispatch_alert(*, subject: str, body: str) -> None:
    """
    Delivery is chosen per process by settings.CRITICAL_ALERT_DELIVERY:
    "direct" sends from this (daemon) thread, so an alert does not depend on
    the single busy celery-worker; "queue" publishes send_operator_critical_alert
    for processes with no mail egress (celery-beat). Each mode falls back to the
    other; only when both fail does this raise (the caller logs it).
    """
    from django.conf import settings

    if getattr(settings, "CRITICAL_ALERT_DELIVERY", "direct") == "queue":
        try:
            _queue_alert(subject=subject, body=body)
            return
        except Exception:
            internal_logger.exception("could not queue CRITICAL alert email; sending directly")
        deliver_alert_email(subject=subject, body=body)
        return
    try:
        deliver_alert_email(subject=subject, body=body)
        return
    except Exception:
        internal_logger.exception("direct CRITICAL alert send failed; queueing")
    _queue_alert(subject=subject, body=body)


class CriticalEmailHandler(logging.Handler):
    """
    Mails CRITICAL records to settings.ADMIN_EMAIL.

    emit() does no I/O: it snapshots the record and puts it on a bounded
    in-process queue (dropping and counting when full). A daemon thread, started
    lazily per process so it survives gunicorn/Celery forks, does the
    rate-limit cache calls and then delivers: by default it sends directly with
    a bounded timeout (CRITICAL_ALERT_DELIVERY="direct", falling back to
    publishing a small Celery task); a process without mail egress
    (celery-beat) sets "queue" to publish the task first (retry=False, so kombu
    does not sit in its retry loop) and send directly only if that fails.
    Attached to the root logger, so a record that propagates from the
    `fragrance` tree is handled exactly once. CRITICAL is logged while
    holding row locks inside webhook transactions and on gunicorn sync
    workers, so none of that may run on the caller's thread. The broker and
    cache connections also have short connect/read timeouts (settings.py), so
    the worker thread is not parked on a dead peer for minutes.

    Alerts still queued when a process is killed are lost; an orderly exit
    waits up to 3 seconds for them. Setting CRITICAL_ALERT_ASYNC = False runs
    the same work inline (the test runner does, so no test spawns a thread).

    Never raises and never recurses: any failure is reported on
    config.log_setup.internal, which has no route into this handler.
    """

    def __init__(self, level: int = logging.CRITICAL) -> None:
        super().__init__(level=level)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.CRITICAL or getattr(_alert_state, "active", False):
            return
        try:
            from django.conf import settings

            if not getattr(settings, "ADMIN_EMAIL", ""):
                return
            snapshot = self._snapshot(record=record)
            if getattr(settings, "CRITICAL_ALERT_ASYNC", True):
                _enqueue(record=snapshot)
            else:
                _process_alert(record=snapshot)
        except Exception:
            try:
                internal_logger.exception("could not send CRITICAL alert email")
            except Exception:
                pass

    @staticmethod
    def _snapshot(*, record: logging.LogRecord) -> logging.LogRecord:
        """
        A private copy with the message already interpolated and no traceback
        object (frames would be kept alive in the queue). The console handler
        keeps mutating the original record after this one returns.
        """
        snapshot = copy.copy(record)
        snapshot.log_template = getattr(record, "log_template", str(record.msg))  # type: ignore[attr-defined]
        snapshot.msg = record.getMessage()
        snapshot.args = ()
        snapshot.exc_info = None
        return snapshot
