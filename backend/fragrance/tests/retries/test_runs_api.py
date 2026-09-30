"""GET /api/v1/runs/: attempt, max_attempts, retry{...}, and error_message privacy."""

from datetime import timedelta

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from fragrance.tests.retries.helpers import FrozenClockTestCase, make_run
from fragrance.tests.support.factories import active_subscription_for
from rest_framework.test import APIClient

INTERNAL_FIELDS = (
    "user",
    "attempt_started_at",
    "retry_state",
    "next_retry_at",
    "last_failed_at",
    "retry_notice_sent_at",
    "exhausted_notice_sent_at",
    "operator_notified_at",
)


class RunsApiRetryPayloadTests(FrozenClockTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="api", password="pw12345!")
        active_subscription_for(self.user)

    def _list(self, *, user: User) -> list[dict]:
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.get(reverse("run-list"))
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_a_scheduled_chain(self) -> None:
        when = timezone.now() + timedelta(hours=20)
        make_run(user=self.user, status="failed", attempt=2, retry_state="scheduled",
                 next_retry_at=when, last_failed_at=timezone.now(), error_message="boom")

        (run,) = self._list(user=self.user)

        self.assertEqual((run["attempt"], run["max_attempts"]), (2, 5))
        self.assertEqual(run["retry"]["state"], "scheduled")
        self.assertEqual(run["retry"]["retries_remaining"], 3)
        self.assertIsNotNone(run["retry"]["next_retry_at"])
        self.assertIsNotNone(run["retry"]["last_failed_at"])

    def test_exhausted_and_stopped_chains(self) -> None:
        make_run(user=self.user, status="failed", attempt=5, retry_state="exhausted", last_failed_at=timezone.now())
        make_run(user=self.user, status="failed", attempt=2, retry_state="stopped", last_failed_at=timezone.now())

        runs = {run["retry"]["state"]: run for run in self._list(user=self.user)}

        self.assertEqual(runs["exhausted"]["retry"]["retries_remaining"], 0)
        self.assertIsNone(runs["exhausted"]["retry"]["next_retry_at"])
        self.assertEqual(runs["stopped"]["retry"]["retries_remaining"], 3)
        self.assertIsNone(runs["stopped"]["retry"]["next_retry_at"])

    def test_retry_is_null_unless_the_run_is_a_failed_chain(self) -> None:
        for status in ("pending", "running", "done"):
            make_run(user=self.user, status=status, attempt=3)
        make_run(user=self.user, status="failed")

        for run in self._list(user=self.user):
            self.assertIsNone(run["retry"], run["status"])

    def test_a_recovered_run_carries_its_attempt_and_no_retry(self) -> None:
        make_run(user=self.user, status="done", attempt=3, last_failed_at=timezone.now())

        (run,) = self._list(user=self.user)

        self.assertEqual((run["status"], run["attempt"], run["retry"]), ("done", 3, None))

    def test_internal_bookkeeping_is_not_exposed(self) -> None:
        make_run(user=self.user, status="failed", retry_state="scheduled", last_failed_at=timezone.now(),
                 next_retry_at=timezone.now())

        (run,) = self._list(user=self.user)

        for field in INTERNAL_FIELDS:
            self.assertNotIn(field, run)

    def test_error_message_is_blank_for_non_staff_and_raw_for_staff(self) -> None:
        make_run(user=self.user, status="failed", error_message="raw provider text")
        staff = User.objects.create_user(username="api_staff", password="pw12345!", is_staff=True)
        make_run(user=staff, status="failed", error_message="staff raw text")

        self.assertEqual(self._list(user=self.user)[0]["error_message"], "")
        self.assertEqual(self._list(user=staff)[0]["error_message"], "staff raw text")

    def test_a_superuser_who_is_not_staff_still_gets_a_blank_error_message(self) -> None:
        su = User.objects.create_user(username="api_su", password="pw12345!", is_superuser=True, is_staff=False)
        make_run(user=su, status="failed", error_message="raw")

        self.assertEqual(self._list(user=su)[0]["error_message"], "")

    def test_the_detail_endpoint_blanks_the_error_too(self) -> None:
        run = make_run(user=self.user, status="failed", error_message="raw provider text")
        client = APIClient()
        client.force_authenticate(user=self.user)

        body = client.get(reverse("run-detail", args=[run.id])).json()

        self.assertEqual(body["error_message"], "")

    def test_another_users_runs_are_never_listed(self) -> None:
        other = User.objects.create_user(username="api_other", password="pw12345!")
        make_run(user=other, status="failed", retry_state="scheduled")

        self.assertEqual(self._list(user=self.user), [])
