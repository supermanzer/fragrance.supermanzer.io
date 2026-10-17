"""Shared fixtures for the recipient-address verification tests (delivery and receipt)."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from fragrance import recipient
from fragrance.models import PendingEmailChange, Subscription
from fragrance.services import get_or_create_fragrance_config
from fragrance.tests.support.bases import StripeClientTestCase
from fragrance.tests.support.factories import active_subscription_for
from fragrance.tests.support.stripe_fakes import _KEY
from rest_framework.test import APIClient

PASSWORD = "pw12345!"


@override_settings(
    STRIPE_SECRET_KEY=_KEY,
    STRIPE_API_VERSION="2026-08-26.dahlia",
    FRONTEND_URL="https://app.example.test",
    SUPPORT_EMAIL="help@example.test",
    DEFAULT_FROM_EMAIL="noreply@example.test",
)
class RecipientTestCase(StripeClientTestCase):
    """
    alice has an active subscription, a config (delivery = account email) and
    a Stripe customer whose cached receipt address is the account email.
    Both mail tasks are patched at `.delay`; call the task functions directly
    to exercise the rendered mail.
    """

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(
            username="alice", email="alice@example.com", password=PASSWORD
        )
        self.subscription = active_subscription_for(self.user)
        Subscription.objects.filter(pk=self.subscription.pk).update(
            stripe_customer_id="cus_alice", receipt_email="alice@example.com"
        )
        self.subscription.refresh_from_db()
        self.config = get_or_create_fragrance_config(user=self.user)
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)
        confirm = patch("fragrance.tasks.send_recipient_confirmation_email.delay")
        self.mock_confirm_delay = confirm.start()
        self.addCleanup(confirm.stop)
        notice = patch("fragrance.tasks.send_recipient_changed_notice.delay")
        self.mock_notice_delay = notice.start()
        self.addCleanup(notice.stop)

    def make_user(self, *, username: str = "bob", staff: bool = False) -> User:
        user = User.objects.create_user(
            username=username, email=f"{username}@example.com", password=PASSWORD, is_staff=staff
        )
        sub = active_subscription_for(user)
        Subscription.objects.filter(pk=sub.pk).update(
            stripe_customer_id=f"cus_{username}", receipt_email=user.email
        )
        get_or_create_fragrance_config(user=user)
        return user

    def pending(self, *, purpose: str, user: User | None = None) -> PendingEmailChange | None:
        return PendingEmailChange.objects.filter(
            user=user or self.user, purpose=purpose
        ).first()

    def token_for(self, *, purpose: str, user: User | None = None) -> str:
        pending = self.pending(purpose=purpose, user=user)
        assert pending is not None
        return recipient.make_token(pending=pending)

    def age_pending(self, *, purpose: str, hours: float = 25, user: User | None = None) -> None:
        """Pushes the row (not the token) into the past: last send and expiry both."""
        PendingEmailChange.objects.filter(user=user or self.user, purpose=purpose).update(
            last_sent_at=timezone.now() - timedelta(hours=hours),
            expires_at=timezone.now() - timedelta(hours=hours - 24),
        )

    def clear_gap(self, *, purpose: str, user: User | None = None) -> None:
        """Moves the last send back past RESEND_MIN_GAP so a replace/resend is accepted."""
        PendingEmailChange.objects.filter(user=user or self.user, purpose=purpose).update(
            last_sent_at=timezone.now() - timedelta(minutes=2)
        )

    def lapse(self, *, user: User | None = None) -> None:
        Subscription.objects.filter(user=user or self.user).update(
            access_expires_at=timezone.now() - timedelta(days=1)
        )
