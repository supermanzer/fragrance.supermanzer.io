from io import StringIO

import stripe
from django.contrib.auth.models import User
from django.core.cache.backends.locmem import LocMemCache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone
from unittest.mock import patch

from fragrance.models import Subscription
from fragrance.tests.support.bases import StripeClientTestCase
from fragrance.tests.support.factories import _active_user
from fragrance.tests.support.stripe_fakes import _BILLING_CHANGES_SETTINGS as _SETTINGS
from fragrance.tests.support.stripe_fakes import (
    _OURS,
    _configuration,
    _configuration_list,
    _pending_schedule_dict,
    _portal_session,
    _spent_schedule_dict,
    _stripe_subscription,
)
from fragrance.views import FailOpenScopedRateThrottle
from rest_framework.test import APIClient


@override_settings(**_SETTINGS)
class BillingPortalViewTests(StripeClientTestCase):
    url = "/api/v1/billing/portal/"

    def setUp(self) -> None:
        super().setUp()
        self.user = _active_user(username="portaluser", customer_id="cus_mine")
        self.other = _active_user(username="someoneelse", customer_id="cus_theirs", sub_id="sub_theirs")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_unauthenticated_returns_401(self) -> None:
        self.assertEqual(APIClient().post(self.url).status_code, 401)

    def test_get_is_not_allowed(self) -> None:
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_revoked_account_is_409_account_locked_and_makes_no_stripe_call(self) -> None:
        Subscription.objects.filter(user=self.user).update(
            status="revoked", access_expires_at=timezone.now()
        )

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["code"], "account_locked")
        self.assertEqual(
            body["detail"],
            "This account can no longer be managed here. "
            "You're welcome to sign up again with a new account.",
        )
        self.stripe.v1.billing_portal.sessions.create.assert_not_called()
        self.stripe.v1.subscriptions.retrieve.assert_not_called()

    def test_revoked_account_without_a_customer_is_still_account_locked(self) -> None:
        Subscription.objects.filter(user=self.user).update(
            status="revoked", stripe_customer_id=""
        )

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "account_locked")

    def test_returns_url_for_the_callers_own_customer_with_server_built_params(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        self.stripe.v1.billing_portal.sessions.create.return_value = _portal_session()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"url": "https://billing.stripe.com/p/session/test_abc"})
        self.stripe.v1.billing_portal.sessions.create.assert_called_once_with(
            params={
                "customer": "cus_mine",
                "return_url": "https://app.example.test/billing?from=portal",
                "configuration": "bpc_test_config",
            }
        )

    def test_client_supplied_fields_are_ignored(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        self.stripe.v1.billing_portal.sessions.create.return_value = _portal_session()

        self.client.post(
            self.url,
            {
                "customer": "cus_theirs",
                "configuration": "bpc_evil",
                "return_url": "https://evil.example/",
                "flow_data": {"type": "subscription_cancel"},
            },
            format="json",
        )

        params = self.stripe.v1.billing_portal.sessions.create.call_args.kwargs["params"]
        self.assertEqual(params["customer"], "cus_mine")
        self.assertEqual(params["configuration"], "bpc_test_config")
        self.assertEqual(params["return_url"], "https://app.example.test/billing?from=portal")
        self.assertNotIn("flow_data", params)

    @override_settings(STRIPE_PORTAL_CONFIGURATION_ID="", DEBUG=True)
    def test_omits_configuration_when_unset_in_debug(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        self.stripe.v1.billing_portal.sessions.create.return_value = _portal_session()
        with self.assertLogs("fragrance.billing", level="WARNING"):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
        params = self.stripe.v1.billing_portal.sessions.create.call_args.kwargs["params"]
        self.assertNotIn("configuration", params)

    def test_no_customer_id_returns_409_no_billing_account(self) -> None:
        Subscription.objects.filter(user=self.user).update(stripe_customer_id=None)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "no_billing_account")
        self.stripe.v1.billing_portal.sessions.create.assert_not_called()

    def test_no_subscription_row_returns_409_no_billing_account(self) -> None:
        fresh = User.objects.create_user(username="fresh", password="pw12345!")
        self.client.force_authenticate(user=fresh)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "no_billing_account")
        self.stripe.v1.billing_portal.sessions.create.assert_not_called()

    def test_staff_returns_409_exempt_without_calling_stripe(self) -> None:
        User.objects.filter(pk=self.user.pk).update(is_staff=True)
        self.user.refresh_from_db()
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "exempt")
        self.stripe.v1.billing_portal.sessions.create.assert_not_called()

    @override_settings(STRIPE_SECRET_KEY="")
    def test_unconfigured_stripe_returns_503(self) -> None:
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 503)

    def test_stripe_error_returns_502_without_leaking_the_message(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        self.stripe.v1.billing_portal.sessions.create.side_effect = stripe.APIConnectionError(
            "secret-detail sk_test_leak"
        )
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("secret-detail", response.content.decode())
        self.assertNotIn("sk_test_leak", response.content.decode())

    def test_spent_schedule_shell_is_released_before_the_session(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(
            price_id="price_yr", schedule=_spent_schedule_dict()
        )
        self.stripe.v1.subscription_schedules.release.return_value = None
        self.stripe.v1.billing_portal.sessions.create.return_value = _portal_session()
        self.client.post(self.url)
        self.stripe.v1.subscription_schedules.release.assert_called_once_with("sub_sched_1")

    def test_pending_schedule_is_not_released_by_the_portal(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(
            schedule=_pending_schedule_dict()
        )
        self.stripe.v1.billing_portal.sessions.create.return_value = _portal_session()
        self.client.post(self.url)
        self.stripe.v1.subscription_schedules.release.assert_not_called()

    def test_failure_to_release_a_spent_shell_does_not_block_the_portal(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription(
            price_id="price_yr", schedule=_spent_schedule_dict()
        )
        self.stripe.v1.subscription_schedules.release.side_effect = stripe.APIConnectionError("down")
        self.stripe.v1.billing_portal.sessions.create.return_value = _portal_session()
        with self.assertLogs("fragrance.billing", level="WARNING"):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)

    def test_throttled_after_the_scope_limit(self) -> None:
        self.stripe.v1.subscriptions.retrieve.return_value = _stripe_subscription()
        self.stripe.v1.billing_portal.sessions.create.return_value = _portal_session()
        with patch.object(FailOpenScopedRateThrottle, "cache", LocMemCache("portal-throttle", {})), patch.object(
            FailOpenScopedRateThrottle, "THROTTLE_RATES", {"billing-portal": "2/min"}
        ):
            statuses = [self.client.post(self.url).status_code for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 429])


@override_settings(**_SETTINGS)
class ConfigureStripePortalCommandTests(StripeClientTestCase):
    def _run(self, *args: str) -> str:
        out = StringIO()
        call_command("configure_stripe_portal", *args, stdout=out)
        return out.getvalue()

    def test_creates_when_none_is_ours_and_ignores_foreign_configurations(self) -> None:
        self.stripe.v1.billing_portal.configurations.list.return_value = _configuration_list(
            configurations=[
                _configuration(config_id="bpc_default", metadata={}),
                _configuration(config_id="bpc_other", metadata={"app": "fragrances"}),
            ]
        )
        self.stripe.v1.billing_portal.configurations.create.return_value = _configuration(
            config_id="bpc_new", metadata=_OURS
        )

        output = self._run()

        self.assertIn("bpc_new", output)
        self.stripe.v1.billing_portal.configurations.list.assert_called_once_with(
            params={"limit": 100, "active": True}
        )
        params = self.stripe.v1.billing_portal.configurations.create.call_args.kwargs["params"]
        features = params["features"]
        self.assertTrue(features["payment_method_update"]["enabled"])
        self.assertTrue(features["invoice_history"]["enabled"])
        self.assertEqual(features["customer_update"], {"enabled": True, "allowed_updates": ["email"]})
        cancel = features["subscription_cancel"]
        self.assertEqual((cancel["enabled"], cancel["mode"], cancel["proration_behavior"]), (True, "at_period_end", "none"))
        self.assertFalse(features["subscription_update"]["enabled"])
        # Not a key of Stripe's portal Configuration Features; it must not be sent.
        self.assertNotIn("subscription_pause", features)
        self.assertFalse(params["login_page"]["enabled"])
        self.assertEqual(params["business_profile"]["headline"], "Manage your Fragrances subscription")
        self.assertEqual(params["business_profile"]["terms_of_service_url"], "https://app.example.test/terms")
        self.assertEqual(params["business_profile"]["privacy_policy_url"], "https://app.example.test/privacy")
        self.assertEqual(
            params["features"]["subscription_cancel"]["cancellation_reason"],
            {
                "enabled": True,
                "options": ["too_expensive", "missing_features", "unused", "switched_service", "other"],
            },
        )
        self.assertEqual(params["metadata"], _OURS)
        self.assertNotIn("default_return_url", params.get("business_profile", {}))

    def test_rerun_modifies_the_existing_configuration_instead_of_creating_another(self) -> None:
        self.stripe.v1.billing_portal.configurations.list.return_value = _configuration_list(
            configurations=[_configuration(config_id="bpc_ours", metadata=_OURS)]
        )
        self.stripe.v1.billing_portal.configurations.update.return_value = _configuration(
            config_id="bpc_ours", metadata=_OURS
        )

        output = self._run()

        self.assertIn("Updated portal configuration bpc_ours", output)
        self.assertEqual(self.stripe.v1.billing_portal.configurations.update.call_args.args[0], "bpc_ours")
        self.stripe.v1.billing_portal.configurations.create.assert_not_called()

    def test_dry_run_writes_nothing(self) -> None:
        self.stripe.v1.billing_portal.configurations.list.return_value = _configuration_list(configurations=[])
        output = self._run("--dry-run")
        self.assertIn("would create", output)
        self.stripe.v1.billing_portal.configurations.create.assert_not_called()
        self.stripe.v1.billing_portal.configurations.update.assert_not_called()

    def test_two_tagged_configurations_is_an_error(self) -> None:
        self.stripe.v1.billing_portal.configurations.list.return_value = _configuration_list(
            configurations=[
                _configuration(config_id="bpc_1", metadata=_OURS),
                _configuration(config_id="bpc_2", metadata=_OURS),
            ]
        )
        with self.assertRaises(CommandError):
            self._run()

    @override_settings(STRIPE_SECRET_KEY="sk_live_dummy")
    def test_live_key_requires_yes(self) -> None:
        with self.assertRaises(CommandError):
            self._run()
        self.stripe.v1.billing_portal.configurations.create.assert_not_called()

    @override_settings(STRIPE_SECRET_KEY="")
    def test_missing_key_is_an_error(self) -> None:
        with self.assertRaises(CommandError):
            self._run()
