"""Shared user/plan/subscription/run factories for fragrance tests."""


from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone
from fragrance.models import (
    BetaSignup,
    Plan,
    Recommendation,
    RecommendationRun,
    Subscription,
)


def active_subscription_for(
    user: User, *, plan: Plan | None = None, days: int = 30
) -> Subscription:
    """Give a test user an active, unexpired subscription (the reusable factory)."""
    subscription, _ = Subscription.objects.get_or_create(user=user)
    subscription.plan = plan
    subscription.status = "active"
    subscription.access_expires_at = timezone.now() + timedelta(days=days)
    subscription.save()
    return subscription


def _make_run_with_pick(
    user: User, name: str = "Aventus", house: str = "Creed", pick_status: str = "confirmed"
) -> Recommendation:
    run = RecommendationRun.objects.create(user=user, status="done")
    return Recommendation.objects.create(
        user=user, run=run, name=name, house=house, status=pick_status, rationale="Smoky and bold."
    )


def _make_plan(*, slug: str, interval: str, access_days: int) -> Plan:
    return Plan.objects.create(
        name=slug, slug=slug, stripe_price_id=f"price_{slug}", amount_cents=400,
        billing_interval=interval, access_days_granted=access_days,
    )


def _backdate_runs(*, user: User, days_ago: float) -> None:
    RecommendationRun.objects.filter(user=user).update(
        triggered_at=timezone.now() - timedelta(days=days_ago)
    )


def _staff_not_superuser(*, username: str = "prod_user_2") -> User:
    return User.objects.create_user(
        username=username, password="pw12345!", is_staff=True, is_superuser=False
    )


def _make_user(*, username: str, customer_id: str | None, approved: bool = True) -> User:
    user = User.objects.create_user(username=username, email=f"{username}@example.com", password="pw12345!")
    if approved:
        BetaSignup.objects.create(
            email=user.email, normalized_email=user.email, status="converted", invited_user=user
        )
    Subscription.objects.create(user=user, stripe_customer_id=customer_id)
    return user


def _make_plans() -> tuple[Plan, Plan]:
    monthly = Plan.objects.create(
        name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_mo",
        amount_cents=400, billing_interval="month", access_days_granted=30,
    )
    annual = Plan.objects.create(
        name="Beta Annual", slug="beta-annual", stripe_price_id="price_yr",
        amount_cents=4400, billing_interval="year", access_days_granted=365,
    )
    return monthly, annual


def _active_user(*, username: str, customer_id: str = "cus_a", sub_id: str | None = "sub_local") -> User:
    user = User.objects.create_user(username=username, email=f"{username}@example.com", password="pw12345!")
    Subscription.objects.create(
        user=user,
        stripe_customer_id=customer_id,
        stripe_subscription_id=sub_id,
        status="active",
        access_expires_at=timezone.now() + timedelta(days=20),
    )
    return user
