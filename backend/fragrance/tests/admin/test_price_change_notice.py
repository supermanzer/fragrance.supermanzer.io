from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.contrib.admin.models import LogEntry
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from fragrance.models import Plan, PriceChangeNotice, Subscription
from fragrance.services import (
    PriceChangeNoticeError,
    price_change_recipients,
    send_price_change_notice,
)
from fragrance.tasks import send_price_change_notice_email
from fragrance.tests.support.factories import active_subscription_for


def _plan(*, slug: str = "monthly", amount_cents: int = 400) -> Plan:
    return Plan.objects.create(
        name="Monthly", slug=slug, stripe_price_id=f"price_{slug}",
        amount_cents=amount_cents, billing_interval="month",
    )


def _subscriber(*, username: str, plan: Plan, **user_fields: object) -> User:
    user = User.objects.create_user(
        username=username, email=f"{username}@example.com", password="pw12345!", **user_fields
    )
    active_subscription_for(user, plan=plan)
    return user


class PriceChangeNoticeTests(TestCase):
    def setUp(self) -> None:
        self.superuser = User.objects.create_superuser(
            username="hal", email="hal@example.com", password="hal-9000"
        )
        self.client.force_login(self.superuser)
        self.plan = _plan()
        self.effective = timezone.localdate() + timedelta(days=31)

    def _post_add(self, *, effective_date: object, new_amount_cents: int = 500):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                reverse("admin:fragrance_pricechangenotice_add"),
                {
                    "plan": self.plan.pk,
                    "new_amount_cents": new_amount_cents,
                    "effective_date": str(effective_date),
                },
            )

    def test_recipients_are_active_unrevoked_non_staff_subscribers_of_the_plan(self) -> None:
        other_plan = _plan(slug="other")
        included = _subscriber(username="ok", plan=self.plan)
        _subscriber(username="staff", plan=self.plan, is_staff=True)
        _subscriber(username="other", plan=other_plan)
        _subscriber(username="inactive", plan=self.plan, is_active=False)
        _subscriber(username="noemail", plan=self.plan)
        User.objects.filter(username="noemail").update(email="")
        revoked = _subscriber(username="revoked", plan=self.plan)
        Subscription.objects.filter(user=revoked).update(status="revoked")
        expired = _subscriber(username="expired", plan=self.plan)
        Subscription.objects.filter(user=expired).update(
            access_expires_at=timezone.now() - timedelta(days=1)
        )

        self.assertEqual(list(price_change_recipients(plan=self.plan)), [included])

    def test_admin_add_records_notice_and_queues_one_email_per_recipient(self) -> None:
        first = _subscriber(username="a", plan=self.plan)
        second = _subscriber(username="b", plan=self.plan)
        with patch.object(send_price_change_notice_email, "delay") as delay:
            response = self._post_add(effective_date=self.effective)

        self.assertEqual(response.status_code, 302)
        notice = PriceChangeNotice.objects.get()
        self.assertEqual(notice.plan, self.plan)
        self.assertEqual(notice.current_amount_cents, 400)
        self.assertEqual(notice.new_amount_cents, 500)
        self.assertEqual(notice.recipient_count, 2)
        self.assertIsNotNone(notice.sent_at)
        self.assertEqual(notice.created_by, self.superuser)
        queued = {call.kwargs["user_id"] for call in delay.call_args_list}
        self.assertEqual(queued, {first.pk, second.pk})
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.amount_cents, 400)

    def test_effective_date_under_30_days_is_refused(self) -> None:
        _subscriber(username="a", plan=self.plan)
        with patch.object(send_price_change_notice_email, "delay") as delay:
            response = self._post_add(effective_date=timezone.localdate() + timedelta(days=29))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "at least 30 days")
        self.assertEqual(PriceChangeNotice.objects.count(), 0)
        delay.assert_not_called()

    def test_exactly_30_days_is_accepted(self) -> None:
        with patch.object(send_price_change_notice_email, "delay"):
            self._post_add(effective_date=timezone.localdate() + timedelta(days=30))
        self.assertEqual(PriceChangeNotice.objects.count(), 1)

    def test_same_price_is_refused(self) -> None:
        with patch.object(send_price_change_notice_email, "delay"):
            response = self._post_add(effective_date=self.effective, new_amount_cents=400)
        self.assertContains(response, "same as the plan")
        self.assertEqual(PriceChangeNotice.objects.count(), 0)

    def test_same_notice_cannot_be_sent_twice(self) -> None:
        _subscriber(username="a", plan=self.plan)
        with patch.object(send_price_change_notice_email, "delay") as delay:
            self._post_add(effective_date=self.effective)
            response = self._post_add(effective_date=self.effective)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(PriceChangeNotice.objects.count(), 1)
        self.assertEqual(delay.call_count, 1)

    def test_a_queue_failure_deletes_the_record_so_the_notice_can_be_sent_again(self) -> None:
        _subscriber(username="a", plan=self.plan)
        notice = PriceChangeNotice(plan=self.plan, new_amount_cents=500, effective_date=self.effective)

        with patch.object(send_price_change_notice_email, "delay", side_effect=OSError("broker down")):
            with self.assertLogs("fragrance.services", level="ERROR"):
                with self.assertRaises(PriceChangeNoticeError) as raised:
                    with self.captureOnCommitCallbacks(execute=True):
                        send_price_change_notice(notice=notice, created_by=self.superuser)

        self.assertIn("could not be queued", raised.exception.problems[0])
        self.assertEqual(PriceChangeNotice.objects.count(), 0)
        with patch.object(send_price_change_notice_email, "delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                send_price_change_notice(
                    notice=PriceChangeNotice(
                        plan=self.plan, new_amount_cents=500, effective_date=self.effective
                    ),
                    created_by=self.superuser,
                )
        self.assertEqual(PriceChangeNotice.objects.count(), 1)
        self.assertEqual(delay.call_count, 1)

    def test_service_refuses_duplicate_and_short_notice_directly(self) -> None:
        with patch.object(send_price_change_notice_email, "delay"):
            send_price_change_notice(
                notice=PriceChangeNotice(plan=self.plan, new_amount_cents=500, effective_date=self.effective),
                created_by=self.superuser,
            )
            with self.assertRaises(PriceChangeNoticeError):
                send_price_change_notice(
                    notice=PriceChangeNotice(plan=self.plan, new_amount_cents=500, effective_date=self.effective),
                    created_by=self.superuser,
                )
            with self.assertRaises(PriceChangeNoticeError):
                send_price_change_notice(
                    notice=PriceChangeNotice(
                        plan=self.plan, new_amount_cents=600,
                        effective_date=timezone.localdate() + timedelta(days=5),
                    ),
                    created_by=self.superuser,
                )
        self.assertEqual(PriceChangeNotice.objects.count(), 1)

    def test_non_staff_cannot_reach_the_add_form(self) -> None:
        self.client.logout()
        user = User.objects.create_user(username="plain", password="pw12345!")
        self.client.force_login(user)
        response = self.client.get(reverse("admin:fragrance_pricechangenotice_add"))
        self.assertIn(response.status_code, (302, 403))

    def test_plan_action_redirects_to_prefilled_form_and_sends_nothing(self) -> None:
        with patch.object(send_price_change_notice_email, "delay") as delay:
            response = self.client.post(
                reverse("admin:fragrance_plan_changelist"),
                {"action": "send_price_change_notice_action", "_selected_action": [str(self.plan.pk)]},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url, f"{reverse('admin:fragrance_pricechangenotice_add')}?plan={self.plan.pk}"
        )
        delay.assert_not_called()
        self.assertEqual(PriceChangeNotice.objects.count(), 0)

    def test_plan_action_needs_exactly_one_plan(self) -> None:
        other = _plan(slug="other")
        response = self.client.post(
            reverse("admin:fragrance_plan_changelist"),
            {"action": "send_price_change_notice_action", "_selected_action": [str(self.plan.pk), str(other.pk)]},
            follow=True,
        )
        self.assertContains(response, "Select exactly one plan.")

    def test_sent_notices_cannot_be_deleted_or_edited(self) -> None:
        notice = PriceChangeNotice.objects.create(
            plan=self.plan, current_amount_cents=400, new_amount_cents=500, effective_date=self.effective
        )
        delete = self.client.post(reverse("admin:fragrance_pricechangenotice_delete", args=[notice.pk]))
        self.assertEqual(delete.status_code, 403)
        self.assertEqual(PriceChangeNotice.objects.count(), 1)


class PriceChangeNoticeEmailTests(TestCase):
    def setUp(self) -> None:
        self.plan = _plan()
        self.user = _subscriber(username="sub", plan=self.plan)
        self.notice = PriceChangeNotice.objects.create(
            plan=self.plan, current_amount_cents=400, new_amount_cents=500,
            effective_date=timezone.localdate() + timedelta(days=40),
        )

    def test_email_states_prices_date_and_cancellation_path(self) -> None:
        send_price_change_notice_email.apply(kwargs={"notice_id": self.notice.pk, "user_id": self.user.pk})

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["sub@example.com"])
        self.assertIn("$4.00 per month", message.body)
        self.assertIn("$5.00 per month", message.body)
        self.assertIn(self.notice.effective_date.strftime("%B"), message.body)
        self.assertIn("/billing", message.body)
        self.assertIn("cancel", message.body.lower())
        self.assertIn("Fragrance Journey", message.body)
        self.assertIn("$5.00 per month", message.alternatives[0].content)

    def test_no_email_when_subscriber_was_revoked_or_moved_plan(self) -> None:
        Subscription.objects.filter(user=self.user).update(status="revoked")
        send_price_change_notice_email.apply(kwargs={"notice_id": self.notice.pk, "user_id": self.user.pk})
        Subscription.objects.filter(user=self.user).update(status="active", plan=_plan(slug="x"))
        send_price_change_notice_email.apply(kwargs={"notice_id": self.notice.pk, "user_id": self.user.pk})
        self.assertEqual(len(mail.outbox), 0)

    def test_send_failure_retries(self) -> None:
        with patch("fragrance.tasks.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            with patch.object(send_price_change_notice_email, "retry", side_effect=RuntimeError("retried")) as retry:
                result = send_price_change_notice_email.apply(
                    kwargs={"notice_id": self.notice.pk, "user_id": self.user.pk}
                )
        retry.assert_called_once()
        self.assertTrue(result.failed())


class PriceChangeNoticeAdminQueueFailureTests(TransactionTestCase):
    def test_the_admin_reports_a_queue_failure_as_an_error_not_a_success(self) -> None:
        superuser = User.objects.create_superuser(username="hal", email="hal@example.com", password="hal-9000")
        self.client.force_login(superuser)
        plan = _plan()
        _subscriber(username="a", plan=plan)

        with patch.object(send_price_change_notice_email, "delay", side_effect=OSError("broker down")):
            with self.assertLogs("fragrance.services", level="ERROR"):
                response = self.client.post(
                    reverse("admin:fragrance_pricechangenotice_add"),
                    {
                        "plan": plan.pk,
                        "new_amount_cents": 500,
                        "effective_date": str(timezone.localdate() + timedelta(days=31)),
                    },
                    follow=True,
                )

        texts = [str(m) for m in response.context["messages"]]
        self.assertEqual(len(texts), 1)
        self.assertTrue(texts[0].startswith("Notice NOT sent: The emails could not be queued"))
        self.assertIn("may receive a duplicate", texts[0])
        self.assertEqual(response.redirect_chain[-1][0], reverse("admin:fragrance_pricechangenotice_changelist"))
        self.assertEqual(PriceChangeNotice.objects.count(), 0)
        self.assertEqual(LogEntry.objects.count(), 0)
