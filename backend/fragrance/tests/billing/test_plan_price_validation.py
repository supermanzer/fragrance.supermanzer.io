"""billing.validate_plan_price: the Stripe-side check PlanAdmin runs before a Plan may be created."""

import stripe
from django.test import override_settings
from fragrance import billing
from fragrance.tests.support.bases import StripeClientTestCase
from fragrance.tests.support.stripe_fakes import _KEY, _STRIPE_TEST_SETTINGS


def _full_price(
    *,
    price_id: str = "price_ok",
    active: bool = True,
    unit_amount: int | None = 400,
    currency: str = "usd",
    interval: str | None = "month",
    interval_count: int = 1,
    livemode: bool = False,
    product: object = "prod_ok",
    usage_type: str = "licensed",
    trial_period_days: int | None = None,
    transform_quantity: dict | None = None,
) -> stripe.Price:
    """A genuine stripe.Price (construct_from). `product` may be an id string or an expanded dict."""
    recurring = (
        {
            "interval": interval,
            "interval_count": interval_count,
            "usage_type": usage_type,
            "trial_period_days": trial_period_days,
        }
        if interval
        else None
    )
    return stripe.Price.construct_from(
        {
            "id": price_id,
            "object": "price",
            "active": active,
            "unit_amount": unit_amount,
            "currency": currency,
            "recurring": recurring,
            "livemode": livemode,
            "product": product,
            "transform_quantity": transform_quantity,
        },
        _KEY,
    )


def _validate(**overrides: object) -> list[str]:
    kwargs: dict = {
        "price_id": "price_ok",
        "amount_cents": 400,
        "currency": "usd",
        "billing_interval": "month",
    }
    kwargs.update(overrides)
    return billing.validate_plan_price(**kwargs)


@override_settings(**_STRIPE_TEST_SETTINGS)
class ValidatePlanPriceTests(StripeClientTestCase):
    def test_a_matching_price_has_no_problems(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price()
        self.assertEqual(_validate(), [])

    def test_uses_the_interactive_client_and_expands_the_product(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price()
        _validate()
        self.mock_get_interactive_stripe_client.assert_called()
        self.mock_get_stripe_client.assert_not_called()
        self.stripe.v1.prices.retrieve.assert_called_once_with(
            "price_ok", params={"expand": ["product"]}
        )

    def test_price_that_does_not_exist(self) -> None:
        self.stripe.v1.prices.retrieve.side_effect = stripe.InvalidRequestError(
            "No such price: 'price_ok'", param="price", code="resource_missing"
        )
        problems = _validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("no Price with id 'price_ok'", problems[0])
        self.assertIn("test-mode", problems[0])

    def test_inactive_price(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(active=False)
        self.assertEqual(_validate(), ["The Price is not active in Stripe."])

    def test_amount_mismatch(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(unit_amount=500)
        problems = _validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("500", problems[0])
        self.assertIn("400", problems[0])

    def test_price_without_a_fixed_amount(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(unit_amount=None)
        self.assertEqual(len(_validate()), 1)
        self.assertIn("no fixed unit_amount", _validate()[0])

    def test_currency_mismatch(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(currency="eur")
        problems = _validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("EUR", problems[0])
        self.assertIn("USD", problems[0])

    def test_currency_comparison_is_case_insensitive(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(currency="usd")
        self.assertEqual(_validate(currency="USD"), [])

    def test_one_time_price_is_not_recurring(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(interval=None)
        problems = _validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("not recurring", problems[0])

    def test_interval_mismatch(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(interval="year")
        problems = _validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("'year'", problems[0])
        self.assertIn("'month'", problems[0])

    def test_interval_count_other_than_one(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(interval_count=3)
        problems = _validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("interval count of 1", problems[0])

    def test_metered_price_is_refused(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(usage_type="metered")
        problems = _validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("metered", problems[0])
        self.assertIn("'metered'", problems[0])

    def test_price_with_a_transform_quantity_is_refused(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(
            transform_quantity={"divide_by": 3, "round": "up"}
        )
        problems = _validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("transform_quantity", problems[0])

    def test_price_with_a_recurring_trial_is_refused(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(trial_period_days=14)
        problems = _validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("14-day free trial", problems[0])
        self.assertIn("trial_period_days", problems[0])

    def test_zero_or_absent_trial_and_a_null_transform_quantity_are_fine(self) -> None:
        for trial in (None, 0):
            with self.subTest(trial=trial):
                self.stripe.v1.prices.retrieve.return_value = _full_price(
                    trial_period_days=trial, transform_quantity=None
                )
                self.assertEqual(_validate(), [])

    def test_test_price_with_a_live_key_is_refused(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(livemode=False)
        with override_settings(STRIPE_SECRET_KEY="sk_live_dummy"):
            problems = _validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("test-mode", problems[0])
        self.assertIn("live-mode", problems[0])

    def test_live_price_with_a_test_key_is_refused(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(livemode=True)
        problems = _validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("live-mode", problems[0])
        self.assertIn("test-mode", problems[0])

    def test_live_price_with_a_live_key_is_fine(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(livemode=True)
        with override_settings(STRIPE_SECRET_KEY="sk_live_dummy"):
            self.assertEqual(_validate(), [])

    def test_restricted_test_key_counts_as_test_mode(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(livemode=False)
        with override_settings(STRIPE_SECRET_KEY="rk_test_dummy"):
            self.assertEqual(_validate(), [])

    def test_archived_product(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(
            product={"id": "prod_old", "object": "product", "active": False}
        )
        self.assertEqual(_validate(), ["The Price's Product is archived or deleted in Stripe."])

    def test_deleted_product(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(
            product={"id": "prod_gone", "object": "product", "deleted": True}
        )
        self.assertEqual(len(_validate()), 1)

    def test_active_expanded_product_is_fine(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(
            product={"id": "prod_ok", "object": "product", "active": True}
        )
        self.assertEqual(_validate(), [])

    def test_all_problems_are_reported_together(self) -> None:
        self.stripe.v1.prices.retrieve.return_value = _full_price(
            active=False, unit_amount=999, currency="eur", interval="year", livemode=True
        )
        self.assertEqual(len(_validate()), 5)

    def test_unreachable_stripe_fails_closed_without_leaking(self) -> None:
        self.stripe.v1.prices.retrieve.side_effect = stripe.APIConnectionError("boom sk_test_dummy")
        with self.assertLogs("fragrance.billing", level="ERROR") as logs:
            problems = _validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("could not be verified", problems[0])
        self.assertNotIn("sk_test_dummy", problems[0])
        self.assertNotIn("boom", problems[0])
        self.assertNotIn("sk_test_dummy", logs.output[0])

    def test_other_stripe_error_fails_closed(self) -> None:
        self.stripe.v1.prices.retrieve.side_effect = stripe.AuthenticationError("bad key sk_test_dummy")
        with self.assertLogs("fragrance.billing", level="ERROR"):
            problems = _validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("could not be verified", problems[0])
        self.assertNotIn("sk_test_dummy", problems[0])

    def test_invalid_request_that_is_not_a_missing_resource_fails_closed(self) -> None:
        self.stripe.v1.prices.retrieve.side_effect = stripe.InvalidRequestError(
            "Invalid string: sk_test_dummy", param="price"
        )
        with self.assertLogs("fragrance.billing", level="ERROR"):
            problems = _validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("could not be verified", problems[0])
        self.assertNotIn("sk_test_dummy", problems[0])

    @override_settings(STRIPE_SECRET_KEY="")
    def test_missing_key_fails_closed(self) -> None:
        problems = _validate()
        self.assertEqual(len(problems), 1)
        self.assertIn("could not be verified", problems[0])
        self.assertIn("STRIPE_SECRET_KEY is unset", problems[0])
        self.stripe.v1.prices.retrieve.assert_not_called()
