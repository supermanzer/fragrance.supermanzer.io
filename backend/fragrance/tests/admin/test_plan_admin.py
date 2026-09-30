"""PlanAdmin: the price id is verified against Stripe on ADD and is read-only on CHANGE."""

import stripe
from django.contrib import admin
from django.contrib.auth.models import User
from django.test import RequestFactory, override_settings
from django.urls import reverse
from fragrance.admin import PlanAdmin
from fragrance.models import Plan
from fragrance.tests.billing.test_plan_price_validation import _full_price
from fragrance.tests.support.bases import StripeClientTestCase
from fragrance.tests.support.stripe_fakes import _STRIPE_TEST_SETTINGS


def _form_data(**overrides: object) -> dict:
    data: dict = {
        "name": "Beta Monthly",
        "slug": "beta-monthly",
        "stripe_price_id": "price_ok",
        "amount_cents": 400,
        "currency": "usd",
        "billing_interval": "month",
        "quota_period_days": 30,
        "max_runs_per_quota_period": 1,
        "failed_run_grace": 1,
        "is_active": "on",
    }
    data.update(overrides)
    return data


@override_settings(**_STRIPE_TEST_SETTINGS)
class PlanAdminAddTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.superuser = User.objects.create_superuser(
            username="hal", email="hal@example.com", password="hal-9000"
        )
        self.client.force_login(self.superuser)
        self.url = reverse("admin:fragrance_plan_add")

    def _errors(self, response) -> list[str]:
        return list(response.context["adminform"].form.errors.get("stripe_price_id", []))

    def test_valid_price_creates_the_plan_after_one_stripe_lookup(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price()

        response = self.client.post(self.url, _form_data())

        self.assertEqual(response.status_code, 302)
        plan = Plan.objects.get(slug="beta-monthly")
        self.assertEqual(plan.stripe_price_id, "price_ok")
        self.stripe.v1.prices.retrieve.assert_called_once()

    def test_add_form_offers_an_editable_price_id(self) -> None:
        response = self.client.get(self.url)
        self.assertContains(response, 'name="stripe_price_id"')

    def test_failed_check_refuses_the_save_with_a_form_error_on_the_price_field(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(unit_amount=500)

        response = self.client.post(self.url, _form_data())

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Plan.objects.exists())
        errors = self._errors(response)
        self.assertEqual(len(errors), 1)
        self.assertIn("500", errors[0])

    def test_every_problem_is_shown_at_once(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(active=False, interval="year")

        response = self.client.post(self.url, _form_data())

        self.assertFalse(Plan.objects.exists())
        self.assertEqual(len(self._errors(response)), 2)

    def test_unknown_price_is_refused(self) -> None:
        self.stripe.v1.prices.retrieve.side_effect = stripe.InvalidRequestError(
            "No such price", param="price", code="resource_missing"
        )
        response = self.client.post(self.url, _form_data())
        self.assertFalse(Plan.objects.exists())
        self.assertIn("no Price with id", self._errors(response)[0])

    def test_unreachable_stripe_refuses_the_save(self) -> None:
        self.stripe.v1.prices.retrieve.side_effect = stripe.APIConnectionError("down")
        with self.assertLogs("fragrance.billing", level="ERROR"):
            response = self.client.post(self.url, _form_data())
        self.assertFalse(Plan.objects.exists())
        self.assertIn("could not be verified", self._errors(response)[0])

    @override_settings(STRIPE_SECRET_KEY="")
    def test_missing_key_refuses_the_save(self) -> None:
        response = self.client.post(self.url, _form_data())
        self.assertFalse(Plan.objects.exists())
        self.assertIn("could not be verified", self._errors(response)[0])
        self.stripe.v1.prices.retrieve.assert_not_called()

    def test_wrong_mode_price_is_refused(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(livemode=True)
        response = self.client.post(self.url, _form_data())
        self.assertFalse(Plan.objects.exists())
        self.assertIn("live-mode", self._errors(response)[0])

    def test_stripe_is_not_called_when_the_form_is_already_invalid(self) -> None:
        response = self.client.post(self.url, _form_data(amount_cents=""))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Plan.objects.exists())
        self.stripe.v1.prices.retrieve.assert_not_called()

    def test_non_staff_cannot_reach_the_add_form(self) -> None:
        self.client.logout()
        user = User.objects.create_user(username="plain", password="pw12345!")
        self.client.force_login(user)
        response = self.client.post(self.url, _form_data())
        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(Plan.objects.exists())
        self.stripe.v1.prices.retrieve.assert_not_called()


@override_settings(**_STRIPE_TEST_SETTINGS)
class PlanAdminChangeTests(StripeClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.superuser = User.objects.create_superuser(
            username="hal", email="hal@example.com", password="hal-9000"
        )
        self.client.force_login(self.superuser)
        self.plan = Plan.objects.create(
            name="Beta Monthly", slug="beta-monthly", stripe_price_id="price_ok",
            amount_cents=400, billing_interval="month",
        )
        self.url = reverse("admin:fragrance_plan_change", args=[self.plan.pk])

    def test_price_id_is_read_only_on_change(self) -> None:
        model_admin = PlanAdmin(Plan, admin.site)
        request = RequestFactory().get("/")
        self.assertIn("stripe_price_id", model_admin.get_readonly_fields(request, self.plan))
        self.assertNotIn("stripe_price_id", model_admin.get_readonly_fields(request, None))

    def test_change_page_renders_the_price_id_as_text_not_an_input(self) -> None:
        response = self.client.get(self.url)
        self.assertContains(response, "price_ok")
        self.assertNotContains(response, 'name="stripe_price_id"')

    def test_a_posted_price_id_is_ignored_and_stripe_is_not_consulted(self) -> None:
        response = self.client.post(
            self.url, _form_data(stripe_price_id="price_evil", name="Renamed")
        )

        self.assertEqual(response.status_code, 302)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.stripe_price_id, "price_ok")
        self.assertEqual(self.plan.name, "Renamed")
        self.stripe.v1.prices.retrieve.assert_not_called()

    def test_other_fields_stay_editable(self) -> None:
        self.client.post(self.url, _form_data(is_active=""))
        self.plan.refresh_from_db()
        self.assertFalse(self.plan.is_active)
