import threading

from django.contrib.auth.models import User
from django.test import TransactionTestCase
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken


class ConcurrentTokenRefreshTests(TransactionTestCase):
    """
    Regression test for the refresh-token race: two requests presenting the
    same refresh token, fired as close to simultaneously as two real threads
    (each with its own DB connection) allow, must resolve to exactly one 200
    and one 401 — never two 200s minting independent token chains.

    Requires TransactionTestCase (real commits across threads) rather than
    TestCase (single wrapped, uncommitted transaction per test) because the
    fix's select_for_update() lock is only meaningful across real,
    independently-committing transactions.
    """

    REFRESH_URL = "/api/v1/auth/token/refresh/"

    def test_same_refresh_token_used_twice_concurrently(self) -> None:
        user = User.objects.create_user(username="dave", password="hal-9000")
        refresh_str = str(RefreshToken.for_user(user))

        results: list[int] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def _refresh() -> None:
            from django.db import connection

            try:
                barrier.wait()
                response = APIClient().post(
                    self.REFRESH_URL, {"refresh": refresh_str}, format="json"
                )
                with results_lock:
                    results.append(response.status_code)
            finally:
                # Each thread opens its own DB connection; Django only
                # auto-closes the main thread's connection after a test, so
                # leaving this open would dangle a session against the test
                # database and break its teardown (DROP DATABASE).
                connection.close()

        threads = [threading.Thread(target=_refresh) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(
            sorted(results), [status.HTTP_200_OK, status.HTTP_401_UNAUTHORIZED]
        )
