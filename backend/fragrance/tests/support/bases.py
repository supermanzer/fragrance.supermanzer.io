"""Shared base TestCase classes and task-inlining helpers for fragrance tests."""


from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from fragrance.models import Subscription
from fragrance.services import generate_email_revert_token
from fragrance.tasks import process_stripe_event, send_password_reset_email
from fragrance.tests.support.stripe_fakes import (
    _KEY,
    _customer,
    _store_customer_updated,
    patch_stripe_clients,
)
from rest_framework.test import APIClient


class StripeClientTestCase(TestCase):
    """
    Base for any test that needs fragrance.billing to talk to Stripe: patches
    both get_stripe_client() and get_interactive_stripe_client() to hand back
    one shared FakeStripeClient (self.stripe), while still calling through to
    the real accessors first so a missing/misconfigured STRIPE_SECRET_KEY
    still raises ImproperlyConfigured from production code, not the fake.

    self.mock_get_stripe_client / self.mock_get_interactive_stripe_client are
    the two accessor mocks themselves -- assert_called()/assert_not_called()
    on them is how a test proves which of the two clients a code path used.
    """

    def setUp(self) -> None:
        super().setUp()
        self._stripe_clients_cm = patch_stripe_clients()
        (
            self.stripe,
            self.mock_get_stripe_client,
            self.mock_get_interactive_stripe_client,
        ) = self._stripe_clients_cm.__enter__()
        self.addCleanup(self._stripe_clients_cm.__exit__, None, None, None)


def _run_reset_task_inline():
    """Patches .delay to run the task eagerly, so tests can inspect the mail it sends."""
    return patch(
        "fragrance.tasks.send_password_reset_email.delay",
        side_effect=lambda **kwargs: send_password_reset_email.apply(kwargs=kwargs),
    )
OLD = "alice@old.example"
NEW = "attacker@evil.example"
INVALID_BODY = {"detail": "This link is invalid or has expired."}


@override_settings(
    STRIPE_SECRET_KEY=_KEY,
    STRIPE_API_VERSION="2026-08-26.dahlia",
    FRONTEND_URL="https://app.example.test",
    SUPPORT_EMAIL="help@example.test",
    DEFAULT_FROM_EMAIL="noreply@example.test",
)
class _RevertBase(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.url = reverse("revert_email")
        self.client = APIClient()
        # State right after the sync applied the attacker's change.
        self.user = User.objects.create_user(username="alice", email=NEW, password="pw12345!")
        Subscription.objects.create(user=self.user, stripe_customer_id="cus_alice")
        self.token = generate_email_revert_token(user_id=self.user.pk, old_email=OLD, new_email=NEW)

        self.mock_modify = self.stripe.v1.customers.update
        self.mock_modify.return_value = _customer(email=OLD)
        self.mock_retrieve = self.stripe.v1.customers.retrieve
        self.mock_retrieve.return_value = _customer(email=NEW)
        restore = patch("fragrance.tasks.restore_stripe_customer_email.delay")
        self.mock_restore = restore.start()
        self.addCleanup(restore.stop)
        notice = patch("fragrance.tasks.send_email_reverted_notice.delay")
        self.mock_notice = notice.start()
        self.addCleanup(notice.stop)

    def _post(self, *, token: object = None, **extra):
        return self.client.post(self.url, {"token": self.token if token is None else token}, format="json", **extra)

    def _email(self) -> str:
        self.user.refresh_from_db()
        return self.user.email

    def _assert_invalid(self, response) -> None:
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), INVALID_BODY)


class _EmailSyncBase(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="alice", email="alice@old.example", password="pw12345!"
        )
        Subscription.objects.create(user=self.user, stripe_customer_id="cus_alice")
        patcher = patch("fragrance.tasks.send_account_email_change_notice.delay")
        self.mock_delay = patcher.start()
        self.addCleanup(patcher.stop)

    def _process(self, *, event_id: str, email: str | None, previous: dict | None = None,
                 customer_id: str = "cus_alice") -> None:
        _store_customer_updated(
            event_id=event_id, customer_id=customer_id, email=email,
            previous={"email": "alice@old.example"} if previous is None else previous,
        )
        process_stripe_event(stripe_event_id=event_id)

    def _email(self) -> str:
        self.user.refresh_from_db()
        return self.user.email
