import json
from unittest.mock import patch

import stripe
from celery.exceptions import Retry
from django.test import TestCase, override_settings
from fragrance.billing import InvoiceNotSettledError
from fragrance.models import StripeEvent
from fragrance.tasks import process_stripe_event
from fragrance.tests.support.stripe_fakes import _BILLING_CHANGES_SETTINGS as _SETTINGS
from fragrance.tests.support.stripe_fakes import _STRIPE_TEST_SETTINGS


@override_settings(**_STRIPE_TEST_SETTINGS)
class StripeWebhookViewTests(TestCase):
    slash_url = "/api/v1/billing/webhook/stripe/"
    noslash_url = "/api/v1/billing/webhook/stripe"

    def _signed_post(self, url: str, body: bytes, secret: str = "whsec_test_dummy"):
        sig = stripe.WebhookSignature.generate_signature_header(payload=body.decode(), secret=secret)
        return self.client.post(url, data=body, content_type="application/json", HTTP_STRIPE_SIGNATURE=sig)

    @patch("fragrance.tasks.process_stripe_event.delay")
    def test_valid_signature_creates_event_and_queues_processing(self, mock_delay) -> None:
        body = json.dumps({
            "id": "evt_test_1", "object": "event", "type": "payment_intent.succeeded",
            "created": 1758300000, "data": {"object": {"id": "pi_1", "status": "succeeded"}},
        }).encode()

        response = self._signed_post(self.slash_url, body)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(StripeEvent.objects.filter(stripe_event_id="evt_test_1").exists())
        mock_delay.assert_called_once_with(stripe_event_id="evt_test_1")

    @patch("fragrance.tasks.process_stripe_event.delay")
    def test_event_with_decimal_typed_field_is_stored_not_500(self, mock_delay) -> None:
        # Regression, found by the sandbox walkthrough: the stripe SDK parses Stripe's `*_decimal`
        # string fields (Price.unit_amount_decimal, invoice line amounts, ...) into Python Decimal,
        # and `event.to_dict()` keeps them. StripeEvent.payload is a JSONField, so storing
        # `event.to_dict()` raised "Decimal is not JSON serializable" and returned a 500 for any
        # real event carrying such a field, which Stripe then retries for three days. The mocks
        # never caught it because no fake event had a decimal-typed field. We now store the
        # signature-verified request body as JSON, which is lossless and needs no SDK conversion.
        body = json.dumps({
            "id": "evt_decimal_1", "object": "event", "type": "price.created",
            "created": 1758300000,
            "data": {"object": {
                "id": "price_dec_1", "object": "price", "currency": "usd",
                "unit_amount": 400, "unit_amount_decimal": "400",
                "recurring": {"interval": "month"},
            }},
        }).encode()

        response = self._signed_post(self.slash_url, body)

        self.assertEqual(response.status_code, 200)
        stored = StripeEvent.objects.get(stripe_event_id="evt_decimal_1")
        self.assertEqual(stored.payload["data"]["object"]["unit_amount_decimal"], "400")
        mock_delay.assert_called_once_with(stripe_event_id="evt_decimal_1")

    def test_tampered_body_returns_400(self) -> None:
        body = json.dumps({
            "id": "evt_bad", "object": "event", "type": "payment_intent.succeeded",
            "created": 1758300000, "data": {"object": {}},
        }).encode()
        sig = stripe.WebhookSignature.generate_signature_header(payload=body.decode(), secret="whsec_test_dummy")
        tampered_body = body.replace(b"evt_bad", b"evt_evil!")

        response = self.client.post(
            self.slash_url, data=tampered_body, content_type="application/json", HTTP_STRIPE_SIGNATURE=sig
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(StripeEvent.objects.filter(stripe_event_id="evt_evil").exists())

    def test_wrong_secret_returns_400(self) -> None:
        body = json.dumps({
            "id": "evt_wrong_secret", "object": "event", "type": "payment_intent.succeeded",
            "created": 1758300000, "data": {"object": {}},
        }).encode()
        response = self._signed_post(self.slash_url, body, secret="whsec_totally_different")
        self.assertEqual(response.status_code, 400)

    @patch("fragrance.tasks.process_stripe_event.delay")
    def test_replayed_event_id_returns_200_without_second_queue(self, mock_delay) -> None:
        body = json.dumps({
            "id": "evt_replay", "object": "event", "type": "payment_intent.succeeded",
            "created": 1758300000, "data": {"object": {"id": "pi_1"}},
        }).encode()

        first = self._signed_post(self.slash_url, body)
        second = self._signed_post(self.slash_url, body)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(StripeEvent.objects.filter(stripe_event_id="evt_replay").count(), 1)
        mock_delay.assert_called_once_with(stripe_event_id="evt_replay")

    @patch("fragrance.tasks.process_stripe_event.delay")
    def test_unsubscribed_event_type_returns_200_and_is_stored(self, mock_delay) -> None:
        body = json.dumps({
            "id": "evt_unsub", "object": "event", "type": "customer.created",
            "created": 1758300000, "data": {"object": {"id": "cus_1"}},
        }).encode()

        response = self._signed_post(self.slash_url, body)

        self.assertEqual(response.status_code, 200)
        event_row = StripeEvent.objects.get(stripe_event_id="evt_unsub")
        self.assertEqual(event_row.event_type, "customer.created")
        mock_delay.assert_called_once()

    def test_both_url_spellings_reach_view_without_redirect(self) -> None:
        body_slash = json.dumps({
            "id": "evt_slash", "object": "event", "type": "customer.created",
            "created": 1758300000, "data": {"object": {}},
        }).encode()
        body_noslash = json.dumps({
            "id": "evt_noslash", "object": "event", "type": "customer.created",
            "created": 1758300000, "data": {"object": {}},
        }).encode()

        with patch("fragrance.tasks.process_stripe_event.delay"):
            response_slash = self._signed_post(self.slash_url, body_slash)
            response_noslash = self._signed_post(self.noslash_url, body_noslash)

        self.assertEqual(response_slash.status_code, 200)
        self.assertEqual(response_noslash.status_code, 200)

    def test_get_not_allowed(self) -> None:
        response = self.client.get(self.slash_url)
        self.assertEqual(response.status_code, 405)


@override_settings(**_STRIPE_TEST_SETTINGS)
class ProcessStripeEventTaskTests(TestCase):
    def test_missing_event_row_is_a_noop(self) -> None:
        process_stripe_event(stripe_event_id="evt_missing")  # must not raise

    @patch("fragrance.billing.fulfill_payment_intent")
    def test_payment_intent_succeeded_dispatches_to_fulfillment(self, mock_fulfill) -> None:
        StripeEvent.objects.create(
            stripe_event_id="evt_pi", event_type="payment_intent.succeeded",
            payload={"data": {"object": {"id": "pi_dispatch"}}},
        )
        process_stripe_event(stripe_event_id="evt_pi")
        mock_fulfill.assert_called_once_with(payment_intent_id="pi_dispatch")
        row = StripeEvent.objects.get(stripe_event_id="evt_pi")
        self.assertEqual(row.status, "processed")
        self.assertIsNotNone(row.processed_at)

    @patch("fragrance.billing.revoke_for_dispute")
    def test_charge_dispute_created_dispatches_to_revocation(self, mock_revoke) -> None:
        StripeEvent.objects.create(
            stripe_event_id="evt_dispute", event_type="charge.dispute.created",
            payload={"data": {"object": {"payment_intent": "pi_d"}}},
        )
        process_stripe_event(stripe_event_id="evt_dispute")
        mock_revoke.assert_called_once_with(dispute={"payment_intent": "pi_d"})
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_dispute").status, "processed")

    def test_unhandled_event_type_is_a_noop_and_marked_processed(self) -> None:
        StripeEvent.objects.create(
            stripe_event_id="evt_other", event_type="customer.created",
            payload={"data": {"object": {}}},
        )
        process_stripe_event(stripe_event_id="evt_other")
        self.assertEqual(StripeEvent.objects.get(stripe_event_id="evt_other").status, "processed")


@override_settings(**_SETTINGS)
class ProcessStripeEventUnsettledRetryTests(TestCase):
    def setUp(self) -> None:
        self.event = StripeEvent.objects.create(
            stripe_event_id="evt_race", event_type="payment_intent.succeeded",
            payload={"data": {"object": {"id": "pi_1"}}},
        )

    def _run(self, *, retries: int) -> None:
        process_stripe_event.push_request(retries=retries)
        try:
            process_stripe_event(stripe_event_id="evt_race")
        finally:
            process_stripe_event.pop_request()

    @patch("fragrance.billing.fulfill_payment_intent", side_effect=InvoiceNotSettledError("in_x"))
    def test_unsettled_retries_on_a_short_ladder(self, _mock_fulfill) -> None:
        for retries, expected_countdown in ((0, 30), (1, 60), (2, 120)):
            with self.subTest(retries=retries):
                with patch.object(process_stripe_event, "retry", side_effect=Retry()) as mock_retry:
                    with self.assertRaises(Retry):
                        self._run(retries=retries)
                self.assertEqual(mock_retry.call_args.kwargs["countdown"], expected_countdown)
                self.event.refresh_from_db()
                self.assertEqual(self.event.status, "failed")

    @patch("fragrance.billing.fulfill_payment_intent", side_effect=RuntimeError("boom"))
    def test_other_errors_keep_the_300_second_countdown(self, _mock_fulfill) -> None:
        with patch.object(process_stripe_event, "retry", side_effect=Retry()) as mock_retry:
            with self.assertRaises(Retry):
                self._run(retries=0)
        self.assertEqual(mock_retry.call_args.kwargs["countdown"], 300)

    @patch("fragrance.billing.fulfill_payment_intent", side_effect=InvoiceNotSettledError("in_x"))
    def test_exhausted_retries_raise_and_leave_the_event_failed(self, _mock_fulfill) -> None:
        with patch.object(process_stripe_event, "retry") as mock_retry:
            with self.assertRaises(InvoiceNotSettledError):
                self._run(retries=process_stripe_event.max_retries)
        mock_retry.assert_not_called()
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, "failed")

    def test_event_ends_processed_after_a_successful_retry(self) -> None:
        with patch(
            "fragrance.billing.fulfill_payment_intent",
            side_effect=[InvoiceNotSettledError("in_x"), 1],
        ):
            with patch.object(process_stripe_event, "retry", side_effect=Retry()):
                with self.assertRaises(Retry):
                    self._run(retries=0)
            self.event.refresh_from_db()
            self.assertEqual(self.event.status, "failed")
            self._run(retries=1)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, "processed")
        self.assertIsNotNone(self.event.processed_at)
        self.assertEqual(self.event.error, "")


class SanitizeStripeErrorMessageTests(TestCase):
    def test_stripe_error_is_sanitized(self) -> None:
        from fragrance.ai_providers.base import sanitize_sdk_error_message

        exc = stripe.InvalidRequestError("super secret internal detail sk_live_abc123", None)
        message = sanitize_sdk_error_message(exc)
        self.assertNotIn("sk_live_abc123", message)

    def test_connection_error_has_its_own_fixed_message_without_leaking(self) -> None:
        from fragrance.ai_providers.base import sanitize_sdk_error_message

        exc = stripe.APIConnectionError("could not connect to sk_live_abc123 at api.stripe.com")
        message = sanitize_sdk_error_message(exc)
        self.assertEqual(message, "Could not reach Stripe. Try again shortly.")
        self.assertNotIn("sk_live_abc123", message)
        self.assertNotIn("Check the Stripe configuration", message)

    def test_non_connection_stripe_error_keeps_config_message(self) -> None:
        from fragrance.ai_providers.base import sanitize_sdk_error_message

        message = sanitize_sdk_error_message(stripe.AuthenticationError("bad key sk_live_zzz"))
        self.assertIn("Check the Stripe configuration", message)
        self.assertNotIn("sk_live_zzz", message)

    def test_non_sdk_exception_passes_through_unchanged(self) -> None:
        from fragrance.ai_providers.base import sanitize_sdk_error_message

        exc = ValueError("plain application error")
        self.assertEqual(sanitize_sdk_error_message(exc), "plain application error")
