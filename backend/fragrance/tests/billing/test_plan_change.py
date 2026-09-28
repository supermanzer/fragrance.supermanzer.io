from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

import stripe
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.cache.backends.locmem import LocMemCache
from django.test import TestCase, override_settings
from django.utils import timezone
from fragrance import billing
from fragrance.models import Plan, Subscription
from fragrance.tests.support.bases import StripeClientTestCase
from fragrance.tests.support.factories import _active_user, _make_plans
from fragrance.tests.support.stripe_fakes import _BILLING_CHANGES_SETTINGS as _SETTINGS
from fragrance.tests.support.stripe_fakes import (
    _PERIOD_END,
    _PERIOD_START,
    _bare_schedule,
    _pending_schedule_dict,
    _phase_dict,
    _portal_session,
    _price,
    _price_for,
    _real_schedule,
    _schedule_dict,
    _spent_schedule_dict,
    _stripe_subscription,
    patch_stripe_clients,
)
from fragrance.views import FailOpenScopedRateThrottle
from rest_framework.test import APIClient


@override_settings(**_SETTINGS)
class BillingChangePlanViewTests(StripeClientTestCase):
    url = "/api/v1/billing/change-plan/"

    def setUp(self) -> None:
        super().setUp()
        self.monthly, self.annual = _make_plans()
        self.user = _active_user(username="changer")
        Subscription.objects.filter(user=self.user).update(plan=self.monthly)
        self.other = _active_user(username="bystander", customer_id="cus_b", sub_id="sub_bystander")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.mock_price = self.stripe.v1.prices.retrieve
        self.mock_price.side_effect = _price_for
        self.addCleanup(cache.clear)

    # -- auth / eligibility ------------------------------------------------

    def test_unauthenticated_is_401_on_every_method(self) -> None:
        anon = APIClient()
        self.assertEqual(anon.get(self.url).status_code, 401)
        self.assertEqual(anon.post(self.url, {"plan": "beta-annual"}, format="json").status_code, 401)
        self.assertEqual(anon.delete(self.url).status_code, 401)

    def test_staff_is_409_exempt_on_every_method(self) -> None:
        User.objects.filter(pk=self.user.pk).update(is_staff=True)
        self.user.refresh_from_db()
        self.client.force_authenticate(user=self.user)
        for response in (
            self.client.get(self.url),
            self.client.post(self.url, {"plan": "beta-annual"}, format="json"),
            self.client.delete(self.url),
        ):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["code"], "exempt")
        self.stripe.v1.subscriptions.retrieve.assert_not_called()

    def test_lapsed_or_missing_subscription_is_409_no_active_subscription(self) -> None:
        Subscription.objects.filter(user=self.user).update(access_expires_at=timezone.now() - timedelta(days=1))
        for response in (
            self.client.get(self.url),
            self.client.post(self.url, {"plan": "beta-annual"}, format="json"),
        ):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["code"], "no_active_subscription")
        self.stripe.v1.subscriptions.retrieve.assert_not_called()

    def test_no_stripe_subscription_id_is_409_no_active_subscription(self) -> None:
        Subscription.objects.filter(user=self.user).update(stripe_subscription_id=None)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "no_active_subscription")
        self.stripe.v1.subscriptions.retrieve.assert_not_called()

    def test_stripe_side_canceled_subscription_is_409(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(status="canceled")
        self.assertEqual(self.client.get(self.url).json()["code"], "no_active_subscription")
        self.assertEqual(
            self.client.post(self.url, {"plan": "beta-annual"}, format="json").json()["code"],
            "no_active_subscription",
        )

    # -- GET ---------------------------------------------------------------

    def test_get_without_pending_lists_other_plans_and_period_end(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body["pending"])
        self.assertEqual([p["slug"] for p in body["available"]], ["beta-annual"])
        self.assertEqual(
            set(body["available"][0]),
            {"slug", "name", "amount_cents", "currency", "billing_interval"},
        )
        self.assertEqual(
            datetime.fromisoformat(body["current_period_end"].replace("Z", "+00:00")),
            datetime.fromtimestamp(_PERIOD_END, tz=dt_timezone.utc),
        )
        self.assertIs(body["cancel_at_period_end"], False)
        self.stripe.v1.subscriptions.retrieve.assert_called_once()
        self.assertEqual(self.stripe.v1.subscriptions.retrieve.call_args.args[0], "sub_local")

    def test_get_with_pending_returns_flat_pending_and_no_available(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(schedule=_pending_schedule_dict())
        body = self.client.get(self.url).json()
        self.assertEqual(body["available"], [])
        self.assertEqual(set(body["pending"]), {"slug", "name", "effective_at"})
        self.assertEqual(body["pending"]["slug"], "beta-annual")
        self.assertEqual(body["pending"]["name"], "Beta Annual")
        self.assertEqual(
            datetime.fromisoformat(body["pending"]["effective_at"].replace("Z", "+00:00")),
            datetime.fromtimestamp(_PERIOD_END, tz=dt_timezone.utc),
        )

    def test_get_treats_a_spent_schedule_shell_as_no_pending_change(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(
            price_id="price_yr", schedule=_spent_schedule_dict()
        )
        body = self.client.get(self.url).json()
        self.assertIsNone(body["pending"])
        self.assertEqual([p["slug"] for p in body["available"]], ["beta-monthly"])

    def test_get_ignores_a_canceled_schedule_left_on_the_subscription(self) -> None:
        canceled = _pending_schedule_dict()
        canceled["status"] = "canceled"
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(schedule=canceled)
        self.assertIsNone(self.client.get(self.url).json()["pending"])

    def test_get_offers_nothing_when_the_subscription_is_set_to_cancel(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(cancel_at_period_end=True)
        body = self.client.get(self.url).json()
        self.assertEqual(body["available"], [])
        self.assertIs(body["cancel_at_period_end"], True)

    def test_get_excludes_inactive_plans(self) -> None:
        Plan.objects.filter(pk=self.annual.pk).update(is_active=False)
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        self.assertEqual(self.client.get(self.url).json()["available"], [])

    def test_get_stripe_error_is_502_without_leak(self) -> None:
        self.stripe.v1.subscriptions.retrieve.side_effect = stripe.APIConnectionError("boom sk_test_leak")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("sk_test_leak", response.content.decode())

    @override_settings(STRIPE_SECRET_KEY="")
    def test_get_unconfigured_is_503(self) -> None:
        self.assertEqual(self.client.get(self.url).status_code, 503)

    # -- POST --------------------------------------------------------------

    def _post(self, plan: str = "beta-annual", **extra: object):
        return self.client.post(self.url, {"plan": plan, **extra}, format="json")

    def test_post_builds_the_verified_two_phase_no_proration_schedule(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        self.stripe.v1.subscription_schedules.create.return_value = _real_schedule(
            schedule=_schedule_dict(
                phases=[_phase_dict(start=_PERIOD_START, end=_PERIOD_END, price_id="price_mo")],
                current_start=_PERIOD_START,
            )
        )
        self.stripe.v1.subscription_schedules.update.return_value = _real_schedule(schedule=_pending_schedule_dict())

        response = self._post(price_id="price_evil")

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["pending"]["slug"], "beta-annual")
        self.assertEqual(body["pending"]["name"], "Beta Annual")
        self.stripe.v1.subscription_schedules.create.assert_called_once_with(
            params={"from_subscription": "sub_local"}
        )
        update_call = self.stripe.v1.subscription_schedules.update.call_args
        self.assertEqual(update_call.args[0], "sub_sched_1")
        params = update_call.kwargs["params"]
        self.assertEqual(params["end_behavior"], "release")
        self.assertEqual(params["proration_behavior"], "none")
        first, second = params["phases"]
        self.assertEqual(
            first,
            {
                "items": [{"price": "price_mo", "quantity": 1}],
                "start_date": _PERIOD_START,
                "end_date": _PERIOD_END,
            },
        )
        self.assertEqual(
            second,
            {
                "items": [{"price": "price_yr", "quantity": 1}],
                "duration": {"interval": "year", "interval_count": 1},
                "billing_cycle_anchor": "phase_start",
            },
        )
        self.assertNotIn("price_evil", str(params))

    def test_annual_to_monthly_uses_a_month_duration(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(price_id="price_yr")
        self.stripe.v1.subscription_schedules.create.return_value = _real_schedule(
            schedule=_schedule_dict(
                phases=[_phase_dict(start=_PERIOD_START, end=_PERIOD_END, price_id="price_yr")],
                current_start=_PERIOD_START,
            )
        )
        self.stripe.v1.subscription_schedules.update.return_value = _real_schedule(
            schedule=_pending_schedule_dict(current_price="price_yr", target_price="price_mo")
        )

        response = self._post("beta-monthly")

        self.assertEqual(response.status_code, 201)
        second = self.stripe.v1.subscription_schedules.update.call_args.kwargs["params"]["phases"][1]
        self.assertEqual(second["duration"], {"interval": "month", "interval_count": 1})
        self.assertEqual(second["billing_cycle_anchor"], "phase_start")
        self.assertEqual(second["items"], [{"price": "price_mo", "quantity": 1}])

    def test_post_missing_plan_is_400(self) -> None:
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 400)

    def test_post_price_id_alone_is_400(self) -> None:
        self.assertEqual(self.client.post(self.url, {"price_id": "price_yr"}, format="json").status_code, 400)

    def test_post_unknown_or_inactive_slug_is_400(self) -> None:
        self.assertEqual(self._post("nope").status_code, 400)
        Plan.objects.filter(pk=self.annual.pk).update(is_active=False)
        self.assertEqual(self._post("beta-annual").status_code, 400)
        self.stripe.v1.subscription_schedules.create.assert_not_called()

    def test_post_same_plan_is_409(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        response = self._post("beta-monthly")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "same_plan")
        self.stripe.v1.subscription_schedules.create.assert_not_called()

    def test_post_with_existing_pending_change_is_409_already_scheduled(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(schedule=_pending_schedule_dict())
        response = self._post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "already_scheduled")
        self.stripe.v1.subscription_schedules.create.assert_not_called()

    def test_post_when_set_to_cancel_is_409_cancellation_scheduled(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(cancel_at_period_end=True)
        response = self._post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "cancellation_scheduled")
        self.stripe.v1.subscription_schedules.create.assert_not_called()

    def test_post_releases_a_spent_shell_then_schedules(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(
            price_id="price_yr", schedule=_spent_schedule_dict()
        )
        self.stripe.v1.subscription_schedules.release.return_value = None
        self.stripe.v1.subscription_schedules.create.return_value = _real_schedule(
            schedule=_schedule_dict(
                schedule_id="sub_sched_2",
                phases=[_phase_dict(start=_PERIOD_START, end=_PERIOD_END, price_id="price_yr")],
                current_start=_PERIOD_START,
            )
        )
        self.stripe.v1.subscription_schedules.update.return_value = _real_schedule(
            schedule=_pending_schedule_dict(current_price="price_yr", target_price="price_mo")
        )
        response = self._post("beta-monthly")
        self.assertEqual(response.status_code, 201)
        self.stripe.v1.subscription_schedules.release.assert_called_once_with("sub_sched_1")

    def test_post_losing_a_double_submit_race_is_409_already_scheduled(self) -> None:
        self.stripe.v1.subscriptions.retrieve.side_effect = [
            _stripe_subscription(),
            _stripe_subscription(schedule=_pending_schedule_dict()),
        ]
        self.stripe.v1.subscription_schedules.create.side_effect = stripe.InvalidRequestError(
            "already has a schedule", None
        )
        response = self._post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "already_scheduled")

    def test_post_releases_the_half_built_schedule_when_modify_fails(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        self.stripe.v1.subscription_schedules.create.return_value = _real_schedule(
            schedule=_schedule_dict(
                phases=[_phase_dict(start=_PERIOD_START, end=_PERIOD_END, price_id="price_mo")],
                current_start=_PERIOD_START,
            )
        )
        self.stripe.v1.subscription_schedules.update.side_effect = stripe.InvalidRequestError(
            "nope sk_test_leak", None
        )
        self.stripe.v1.subscription_schedules.release.return_value = None
        response = self._post()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("sk_test_leak", response.content.decode())
        self.stripe.v1.subscription_schedules.release.assert_called_once_with("sub_sched_1")

    def test_post_only_ever_touches_the_callers_own_subscription(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(schedule=_pending_schedule_dict())
        self._post(subscription="sub_bystander", customer="cus_b")
        self.assertEqual(self.stripe.v1.subscriptions.retrieve.call_args.args[0], "sub_local")

    @override_settings(STRIPE_SECRET_KEY="")
    def test_post_unconfigured_is_503(self) -> None:
        self.assertEqual(self._post().status_code, 503)

    # -- DELETE ------------------------------------------------------------

    def test_delete_releases_the_pending_schedule_with_204(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(schedule=_pending_schedule_dict())
        self.stripe.v1.subscription_schedules.release.return_value = None
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.stripe.v1.subscription_schedules.release.assert_called_once_with("sub_sched_1")

    def test_delete_with_nothing_pending_is_404(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "no_pending_change")
        self.stripe.v1.subscription_schedules.release.assert_not_called()

    def test_delete_does_not_treat_a_spent_shell_as_a_pending_change(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(
            price_id="price_yr", schedule=_spent_schedule_dict()
        )
        self.assertEqual(self.client.delete(self.url).status_code, 404)
        self.stripe.v1.subscription_schedules.release.assert_not_called()

    def test_delete_release_error_is_502(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(schedule=_pending_schedule_dict())
        self.stripe.v1.subscription_schedules.release.side_effect = stripe.APIConnectionError("down")
        self.assertEqual(self.client.delete(self.url).status_code, 502)

    def test_delete_without_a_stripe_subscription_is_404(self) -> None:
        Subscription.objects.filter(user=self.user).update(stripe_subscription_id=None)
        self.assertEqual(self.client.delete(self.url).status_code, 404)
        self.stripe.v1.subscriptions.retrieve.assert_not_called()

    # -- throttle ----------------------------------------------------------

    def test_throttled_after_the_scope_limit(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        with patch.object(FailOpenScopedRateThrottle, "cache", LocMemCache("plan-throttle", {})), patch.object(
            FailOpenScopedRateThrottle, "THROTTLE_RATES", {"billing-change-plan": "2/min"}
        ):
            statuses = [self.client.get(self.url).status_code for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 429])


@override_settings(**_SETTINGS)
class PlanChangeHardeningTests(StripeClientTestCase):
    url = "/api/v1/billing/change-plan/"
    portal_url = "/api/v1/billing/portal/"

    def setUp(self) -> None:
        super().setUp()
        self.monthly, self.annual = _make_plans()
        self.user = _active_user(username="hardened")
        Subscription.objects.filter(user=self.user).update(plan=self.monthly)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.lock_key = f"plan-change-lock:{self.user.pk}"
        self.mock_price = self.stripe.v1.prices.retrieve
        self.mock_price.side_effect = _price_for
        self.addCleanup(cache.clear)

    def _post(self, plan: str = "beta-annual"):
        return self.client.post(self.url, {"plan": plan}, format="json")

    # -- lock: change-plan ----------------------------------------------------

    def test_post_while_locked_is_409_change_in_progress_and_touches_no_stripe(self) -> None:
        cache.add(self.lock_key, "1", timeout=60)
        response = self._post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "change_in_progress")
        self.stripe.v1.subscriptions.retrieve.assert_not_called()
        self.assertIsNotNone(cache.get(self.lock_key), "a contended request must not release someone else's lock")

    def test_delete_while_locked_is_409_change_in_progress(self) -> None:
        cache.add(self.lock_key, "1", timeout=60)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "change_in_progress")
        self.stripe.v1.subscriptions.retrieve.assert_not_called()

    def test_lock_is_per_user(self) -> None:
        other = _active_user(username="neighbour", customer_id="cus_n", sub_id="sub_neighbour")
        cache.add(f"plan-change-lock:{other.pk}", "1", timeout=60)
        # subscription_schedules.create is deliberately left unconfigured: the
        # point of this test is that the *other* user's lock does not block
        # this request, which instead reaches (and fails on) a real Stripe
        # call -- a 502, not the 409 a lock collision would produce.
        self.stripe.allow_unconfigured_stripe_calls = True
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(price_id="price_yr")
        self.assertEqual(self._post("beta-monthly").status_code, 502)
        self.stripe.v1.subscriptions.retrieve.assert_called()

    def test_lock_is_held_during_the_stripe_calls_and_released_after_success(self) -> None:
        held: list[bool] = []
        self.stripe.v1.subscriptions.retrieve.side_effect = lambda *a, **k: (
            held.append(cache.get(self.lock_key) is not None) or _stripe_subscription()
        )
        self.stripe.v1.subscription_schedules.create.return_value = _bare_schedule()
        self.stripe.v1.subscription_schedules.update.return_value = _real_schedule(schedule=_pending_schedule_dict())
        self.assertEqual(self._post().status_code, 201)
        self.assertEqual(held, [True])
        self.assertIsNone(cache.get(self.lock_key))

    def test_lock_is_released_after_a_stripe_failure(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        self.stripe.v1.subscription_schedules.create.return_value = _bare_schedule()
        self.stripe.v1.subscription_schedules.update.side_effect = stripe.APIConnectionError("down")
        self.stripe.v1.subscription_schedules.release.return_value = None
        self.assertEqual(self._post().status_code, 502)
        self.assertIsNone(cache.get(self.lock_key))

    def test_lock_is_released_after_a_domain_refusal(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(schedule=_pending_schedule_dict())
        self.assertEqual(self._post().status_code, 409)
        self.assertIsNone(cache.get(self.lock_key))

    def test_delete_releases_the_lock(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(schedule=_pending_schedule_dict())
        self.stripe.v1.subscription_schedules.release.return_value = None
        self.assertEqual(self.client.delete(self.url).status_code, 204)
        self.assertIsNone(cache.get(self.lock_key))

    def test_cache_failure_fails_closed_with_503_on_post_and_delete(self) -> None:
        with patch("fragrance.billing.cache.add", side_effect=ConnectionError("redis down")):
            post = self._post()
            delete = self.client.delete(self.url)
        self.assertEqual(post.status_code, 503)
        self.assertEqual(delete.status_code, 503)
        self.assertNotIn("redis", post.content.decode().lower())
        self.stripe.v1.subscriptions.retrieve.assert_not_called()

    # -- lock: portal ---------------------------------------------------------

    def test_portal_while_locked_skips_the_release_but_still_opens(self) -> None:
        cache.add(self.lock_key, "1", timeout=60)
        self.stripe.v1.billing_portal.sessions.create.return_value = _portal_session()
        response = self.client.post(self.portal_url)
        self.assertEqual(response.status_code, 200)
        self.stripe.v1.subscriptions.retrieve.assert_not_called()
        self.stripe.v1.subscription_schedules.release.assert_not_called()
        self.assertIsNotNone(cache.get(self.lock_key))

    def test_portal_cache_failure_skips_the_release_but_still_opens(self) -> None:
        self.stripe.v1.billing_portal.sessions.create.return_value = _portal_session()
        with patch("fragrance.billing.cache.add", side_effect=ConnectionError("redis down")):
            response = self.client.post(self.portal_url)
        self.assertEqual(response.status_code, 200)
        self.stripe.v1.subscription_schedules.release.assert_not_called()

    def test_portal_releases_the_lock_after_its_release(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(
            price_id="price_yr", schedule=_spent_schedule_dict()
        )
        self.stripe.v1.subscription_schedules.release.return_value = None
        self.stripe.v1.billing_portal.sessions.create.return_value = _portal_session()
        self.assertEqual(self.client.post(self.portal_url).status_code, 200)
        self.stripe.v1.subscription_schedules.release.assert_called_once_with("sub_sched_1")
        self.assertIsNone(cache.get(self.lock_key))

    # -- portal fails closed without a configuration id ----------------------

    @override_settings(STRIPE_PORTAL_CONFIGURATION_ID="", DEBUG=False)
    def test_portal_without_configuration_id_is_503_outside_debug_and_mutates_nothing(self) -> None:
        response = self.client.post(self.portal_url)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Billing management is temporarily unavailable."})
        self.stripe.v1.billing_portal.sessions.create.assert_not_called()
        self.stripe.v1.subscriptions.retrieve.assert_not_called()
        self.stripe.v1.subscription_schedules.release.assert_not_called()

    # -- phase duration derived from the Stripe price --------------------------

    def test_duration_comes_from_the_stripe_price(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        self.stripe.v1.subscription_schedules.create.return_value = _bare_schedule()
        self.stripe.v1.subscription_schedules.update.return_value = _real_schedule(schedule=_pending_schedule_dict())
        self.mock_price.side_effect = lambda price_id, **k: _price(price_id=price_id, interval="year", interval_count=2)
        self.assertEqual(self._post().status_code, 201)
        self.mock_price.assert_called_once_with("price_yr")
        self.assertEqual(
            self.stripe.v1.subscription_schedules.update.call_args.kwargs["params"]["phases"][1]["duration"],
            {"interval": "year", "interval_count": 2},
        )

    def test_interval_mismatch_is_502_before_anything_is_created(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        self.mock_price.side_effect = lambda price_id, **k: _price(price_id=price_id, interval="month")
        response = self._post()
        self.assertEqual(response.status_code, 502)
        self.stripe.v1.subscription_schedules.create.assert_not_called()
        self.assertIsNone(cache.get(self.lock_key))
        self.assertNotIn("month", response.content.decode())

    def test_price_without_a_recurring_interval_is_502(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        self.mock_price.side_effect = lambda price_id, **k: _price(price_id=price_id, interval=None)
        self.assertEqual(self._post().status_code, 502)
        self.stripe.v1.subscription_schedules.create.assert_not_called()

    def test_price_lookup_failure_is_502(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        self.mock_price.side_effect = stripe.APIConnectionError("down")
        self.assertEqual(self._post().status_code, 502)
        self.stripe.v1.subscription_schedules.create.assert_not_called()

    # -- empty subscription items ----------------------------------------------

    def test_get_with_no_subscription_items_is_a_clean_409(self) -> None:
        sub = _stripe_subscription()
        sub["items"]["data"] = []
        self.stripe.v1.subscriptions.retrieve.return_value = sub
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "no_active_subscription")


class StripeInteractiveBudgetTests(TestCase):
    """
    Same test names as before the migration; new bodies. There is no longer a
    ContextVar-toggled budget to inspect (see billing.py's accessor
    docstrings for why that was replaced outright, not reworked) -- these now
    confirm the two module-level http_clients carry the right timeout/retry
    configuration, and that a real call through each accessor reaches the
    http layer with the right max_network_retries.
    """

    @staticmethod
    def _record_max_network_retries() -> list[int | None]:
        """Patches the http layer and makes one real call through each accessor, recording what each reached it with."""
        seen: list[int | None] = []

        def _fake(self, method, url, headers, post_data=None, max_network_retries=None, *, _usage=None):
            seen.append(max_network_retries)
            return "{}", 200, {}

        with patch("stripe.RequestsClient.request_with_retries", autospec=True) as mock_request:
            mock_request.side_effect = _fake
            billing.get_stripe_client().v1.customers.create(params={"email": "a@example.test"})
            billing.get_interactive_stripe_client().v1.customers.create(params={"email": "b@example.test"})
        return seen

    def test_interactive_timeout_is_a_connect_read_tuple_only_inside_the_budget(self) -> None:
        self.assertEqual(billing._HTTP_CLIENT._timeout, 20)
        self.assertEqual(billing._INTERACTIVE_HTTP_CLIENT._timeout, billing._INTERACTIVE_TIMEOUT_SECONDS)
        self.assertEqual(billing._INTERACTIVE_TIMEOUT_SECONDS, (3.05, 10))

    @override_settings(STRIPE_SECRET_KEY="sk_test_budget", STRIPE_API_VERSION="2026-08-26.dahlia")
    def test_timeout_and_retries_are_capped_only_inside_the_budget(self) -> None:
        seen_retries = self._record_max_network_retries()
        self.assertEqual(seen_retries, [2, 0])

    @override_settings(STRIPE_SECRET_KEY="sk_test_budget", STRIPE_API_VERSION="2026-08-26.dahlia")
    def test_zero_retries_forced_inside_the_budget(self) -> None:
        seen_retries = self._record_max_network_retries()
        self.assertEqual(seen_retries[1], 0)
        self.assertEqual(billing._INTERACTIVE_MAX_NETWORK_RETRIES, 0)

    def test_worst_case_interactive_sequence_stays_well_under_gunicorn_timeout(self) -> None:
        worst_calls = 6  # subscription, price, release, create, modify, cleanup release
        connect, read = billing._INTERACTIVE_TIMEOUT_SECONDS
        worst_case = worst_calls * (connect + read) * (1 + billing._INTERACTIVE_MAX_NETWORK_RETRIES)
        self.assertLessEqual(worst_case, 120 * 0.75)
        self.assertGreaterEqual(billing._PLAN_CHANGE_LOCK_TTL_SECONDS, 130)


class PlanChangeLockTokenTests(TestCase):
    def setUp(self) -> None:
        cache.clear()
        self.user = User.objects.create_user(username="locktok", password="pw")

    def test_release_does_not_delete_a_lock_acquired_by_another_owner(self) -> None:
        key = f"plan-change-lock:{self.user.pk}"
        with billing._plan_change_lock(user=self.user, required=True) as a_locked:
            self.assertTrue(a_locked)
            cache.delete(key)  # simulated TTL expiry
            self.assertTrue(cache.add(key, "b-token", timeout=60))  # B acquires
        self.assertEqual(cache.get(key), "b-token")

    def test_release_deletes_own_lock(self) -> None:
        key = f"plan-change-lock:{self.user.pk}"
        with billing._plan_change_lock(user=self.user, required=True):
            self.assertIsNotNone(cache.get(key))
        self.assertIsNone(cache.get(key))

    @override_settings(**_SETTINGS)
    def test_portal_runs_inside_the_budget_and_leaves_it_after(self) -> None:
        """
        Same name as before the migration; new body. There is no budget
        ContextVar to check "left after" anymore -- this now confirms the
        interactive accessor was used (and the default was not).
        """
        with patch_stripe_clients() as (fake, mock_default, mock_interactive):
            fake.v1.subscriptions.retrieve.return_value = _stripe_subscription()
            fake.v1.billing_portal.sessions.create.return_value = _portal_session()
            user = _active_user(username="budgeted")
            billing.create_portal_session(user=user)
        mock_interactive.assert_called()
        mock_default.assert_not_called()
