"""
Row 14: the background Stripe client backs off (bounded exponential, jittered)
on timeouts, connection errors, 429 and 5xx, re-sending ONE Idempotency-Key per
call; the interactive client never retries. Exercised at the requests layer
through the SDK's real retry loop -- no network.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import requests
import stripe
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from fragrance import billing
from requests.structures import CaseInsensitiveDict


class _ScriptedStripe:
    """Replays `script` (an HTTP status, or an Exception) per wire call, then 200."""

    def __init__(self, *, script: list, headers: dict | None = None) -> None:
        self.script = list(script)
        self.headers = headers or {}
        self.calls: list[dict] = []

    def handle(self, session, method, url, **kwargs):
        self.calls.append(
            {"url": url, "key": kwargs["headers"].get("Idempotency-Key"), "timeout": kwargs.get("timeout")}
        )
        step = self.script.pop(0) if self.script else 200
        if isinstance(step, Exception):
            raise step
        body = (
            {"id": "cus_ok", "object": "customer"}
            if step == 200
            else {"error": {"type": "api_error", "message": "slow"}}
        )
        return SimpleNamespace(
            content=json.dumps(body), status_code=step, headers=CaseInsensitiveDict(self.headers)
        )


def _run(*, server: _ScriptedStripe, client: stripe.StripeClient):
    sleeps: list[float] = []
    with patch.object(
        requests.Session, "request", autospec=True, side_effect=server.handle
    ), patch("stripe._http_client.time.sleep", side_effect=sleeps.append):
        try:
            result = client.v1.customers.create(params={"email": "backoff@example.test"})
        except stripe.StripeError as exc:
            return exc, sleeps
    return result, sleeps


@override_settings(STRIPE_SECRET_KEY="sk_test_backoff", STRIPE_API_VERSION="2026-08-26.dahlia")
class BackgroundBackoffTests(TestCase):
    def test_429_then_success_retries_under_one_idempotency_key(self) -> None:
        server = _ScriptedStripe(script=[429, 429])
        with self.assertLogs("fragrance.billing", level="WARNING") as logs:
            result, sleeps = _run(server=server, client=billing.get_stripe_client())
        self.assertEqual(result.id, "cus_ok")
        self.assertEqual(len(server.calls), 3)
        keys = {call["key"] for call in server.calls}
        self.assertEqual(len(keys), 1)
        self.assertTrue(all(call["timeout"] == (5.0, 15.0) for call in server.calls))
        self.assertEqual(len(sleeps), 2)
        self.assertEqual(len(logs.output), 2)
        self.assertTrue(all("POST /v1/customers" in line and "http_429" in line for line in logs.output))

    def test_backoff_is_bounded_exponential_with_jitter(self) -> None:
        server = _ScriptedStripe(script=[503, 503, 503, 503])
        result, sleeps = _run(server=server, client=billing.get_stripe_client())
        self.assertIsInstance(result, stripe.APIError)
        self.assertEqual(len(server.calls), 1 + billing._BACKGROUND_MAX_NETWORK_RETRIES)
        self.assertEqual(len(sleeps), billing._BACKGROUND_MAX_NETWORK_RETRIES)
        for index, slept in enumerate(sleeps):
            ceiling = min(0.5 * 2**index, 5)
            self.assertGreaterEqual(slept, 0.5)
            self.assertLessEqual(slept, ceiling)

    def test_exhausted_429_raises_rate_limit_error(self) -> None:
        server = _ScriptedStripe(script=[429, 429, 429, 429])
        result, _ = _run(server=server, client=billing.get_stripe_client())
        self.assertIsInstance(result, stripe.RateLimitError)
        self.assertEqual(len(server.calls), 4)

    def test_stripe_should_retry_false_is_honoured_on_429(self) -> None:
        server = _ScriptedStripe(script=[429], headers={"stripe-should-retry": "false"})
        result, _ = _run(server=server, client=billing.get_stripe_client())
        self.assertIsInstance(result, stripe.RateLimitError)
        self.assertEqual(len(server.calls), 1)

    def test_timeout_is_retried_and_logged_without_ids_or_query(self) -> None:
        server = _ScriptedStripe(script=[requests.exceptions.Timeout("slow")])
        with self.assertLogs("fragrance.billing", level="WARNING") as logs:
            result, _ = _run(server=server, client=billing.get_stripe_client())
        self.assertEqual(result.id, "cus_ok")
        self.assertEqual(len(server.calls), 2)
        self.assertIn("APIConnectionError", logs.output[0])
        self.assertNotIn("backoff@example.test", logs.output[0])

    def test_a_400_is_never_retried(self) -> None:
        server = _ScriptedStripe(script=[400])
        result, sleeps = _run(server=server, client=billing.get_stripe_client())
        self.assertIsInstance(result, stripe.StripeError)
        self.assertEqual(len(server.calls), 1)
        self.assertEqual(sleeps, [])

    def test_interactive_client_never_retries_429_or_5xx(self) -> None:
        for status_code, expected in ((429, stripe.RateLimitError), (503, stripe.APIError)):
            with self.subTest(status=status_code):
                server = _ScriptedStripe(script=[status_code, status_code])
                result, sleeps = _run(server=server, client=billing.get_interactive_stripe_client())
                self.assertIsInstance(result, expected)
                self.assertEqual(len(server.calls), 1)
                self.assertEqual(sleeps, [])
                self.assertEqual(server.calls[0]["timeout"], (3.05, 10))

    def test_each_separate_call_gets_a_fresh_idempotency_key(self) -> None:
        server = _ScriptedStripe(script=[])
        with patch.object(requests.Session, "request", autospec=True, side_effect=server.handle):
            client = billing.get_stripe_client()
            client.v1.customers.create(params={"email": "a@example.test"})
            client.v1.customers.create(params={"email": "b@example.test"})
        self.assertEqual(len({call["key"] for call in server.calls}), 2)

    def test_worst_case_per_call_fits_inside_the_reconcile_budget(self) -> None:
        connect, read = billing._BACKGROUND_TIMEOUT_SECONDS
        attempts = 1 + billing._BACKGROUND_MAX_NETWORK_RETRIES
        ladder = sum(min(0.5 * 2**i, 5) for i in range(billing._BACKGROUND_MAX_NETWORK_RETRIES))
        worst_call = attempts * (connect + read) + ladder
        from fragrance.tasks import reconcile_stripe_subscriptions

        # Per-invoice work is ORM-only; the one network call that can start
        # after the deadline check is the paginated list advance.
        self.assertLessEqual(billing.RECONCILE_TIME_BUDGET_SECONDS, 100.0)
        self.assertLessEqual(
            billing.RECONCILE_TIME_BUDGET_SECONDS + worst_call,
            reconcile_stripe_subscriptions.soft_time_limit,
        )

    def test_webhook_and_reconcile_and_recovery_paths_use_the_background_accessor(self) -> None:
        import inspect

        for func in (
            billing.fulfill_payment_intent,
            billing.reconcile_subscriptions,
            billing.recover_past_due_invoice,
            billing.revoke_for_dispute,
            billing.record_subscription_lapse_signal,
        ):
            source = inspect.getsource(func)
            self.assertIn("get_stripe_client()", source, func.__name__)
            self.assertNotIn("get_interactive_stripe_client", source, func.__name__)
