import re

from config.debug import AdminHardeningExceptionReporterFilter
from config.settings import _secure_proxy_ssl_header, _validated_admin_url
from django.conf import settings
from django.core.cache import caches
from django.core.cache.backends.locmem import LocMemCache
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import resolve, reverse
from django.urls.exceptions import Resolver404
from django.views.debug import SafeExceptionReporterFilter


class AdminURLValidationTests(TestCase):
    """
    Exercises _validated_admin_url() directly rather than reloading
    config.settings mid-suite — the validator runs once at settings-import
    time, so override_settings can't reach it, and re-executing the settings
    module against a live test run is a bad idea. See
    .claude/plans/2026-08-30-admin-hardening-plan.md, Finding 5.
    """

    def test_leading_and_trailing_slashes_are_stripped_to_one_segment(self) -> None:
        self.assertEqual(
            _validated_admin_url(raw="/my-admin-path/"), "my-admin-path"
        )

    def test_bare_valid_value_passes_unchanged(self) -> None:
        self.assertEqual(_validated_admin_url(raw="dev-admin-1"), "dev-admin-1")

    def test_too_short_rejected(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            _validated_admin_url(raw="abc123")

    def test_uppercase_rejected(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            _validated_admin_url(raw="SHORTPATH")

    def test_empty_after_stripping_slashes_rejected(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            _validated_admin_url(raw="///")

    def test_angle_brackets_rejected(self) -> None:
        # path() route strings parse "<...>" as converter syntax — a value
        # containing these must be rejected outright, not silently create an
        # unintended dynamic capture.
        with self.assertRaises(ImproperlyConfigured):
            _validated_admin_url(raw="abc<int:pk>")

    def test_leading_hyphen_rejected(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            _validated_admin_url(raw="-leadinghyphen")


class SecureProxySslHeaderTests(TestCase):
    """
    Regression test for the infra-reviewer/security-reviewer's
    2026-09-04 admin-hardening re-review, Finding A / Item 2:
    SECURE_PROXY_SSL_HEADER must be env-gated on DEBUG the same way
    SESSION_COOKIE_SECURE/CSRF_COOKIE_SECURE already are, since nginx is
    only guaranteed to overwrite X-Forwarded-Proto (rather than forward a
    client-supplied value) in prod — dev's docker-compose.override.yaml
    publishes `backend` directly, bypassing nginx entirely.

    Exercises _secure_proxy_ssl_header() directly rather than asserting
    against settings.SECURE_PROXY_SSL_HEADER/settings.DEBUG, mirroring
    AdminURLValidationTests' approach for _validated_admin_url() above —
    the settings module evaluates this once at import time using the real
    .env DEBUG value, and Django's own test runner separately forces
    settings.DEBUG=False for the duration of `manage.py test`, so a
    settings-module-level assertion here would compare two different
    timepoints and assert against the wrong one. Testing the helper
    directly avoids that trap entirely.
    """

    def test_none_when_debug_is_true(self) -> None:
        self.assertIsNone(_secure_proxy_ssl_header(debug=True))

    def test_forwarded_proto_tuple_when_debug_is_false(self) -> None:
        self.assertEqual(
            _secure_proxy_ssl_header(debug=False),
            ("HTTP_X_FORWARDED_PROTO", "https"),
        )


class AdminURLRoutingTests(TestCase):
    """
    Value-agnostic: asserts against whatever settings.DJANGO_ADMIN_URL
    actually holds in this environment, not a hardcoded literal, since the
    real value is deploy-specific and unguessable by design.
    """

    def test_admin_index_resolves_under_the_configured_admin_url(self) -> None:
        self.assertTrue(
            reverse("admin:index").startswith(f"/{settings.DJANGO_ADMIN_URL}/")
        )

    def test_legacy_admin_path_is_dead_unless_it_happens_to_be_the_configured_value(
        self,
    ) -> None:
        if settings.DJANGO_ADMIN_URL == "admin":
            self.skipTest(
                "DJANGO_ADMIN_URL is literally 'admin' in this environment; "
                "the min-length-8 regex forbids this in practice."
            )
        with self.assertRaises(Resolver404):
            resolve("/admin/")


class AdminHardeningExceptionReporterFilterTests(TestCase):
    """
    Regression test for .claude/plans/2026-08-30-admin-hardening-plan.md,
    Finding 13: Django's project-level HIDDEN_SETTINGS setting was removed
    in Django 3.1 (confirmed by reading the installed
    django.views.debug.SafeExceptionReporterFilter directly — the masking
    regex is now a hardcoded class attribute, not something settings.py can
    override by name). DEFAULT_EXCEPTION_REPORTER_FILTER must instead point
    at a SafeExceptionReporterFilter subclass, or DJANGO_ADMIN_URL renders
    unmasked in the debug settings dump on any unhandled production error.
    """

    def setUp(self) -> None:
        self.stock_filter = SafeExceptionReporterFilter()
        self.custom_filter = AdminHardeningExceptionReporterFilter()

    def test_extends_rather_than_replaces_djangos_default_pattern(self) -> None:
        self.assertTrue(
            self.custom_filter.hidden_settings.pattern.startswith(
                self.stock_filter.hidden_settings.pattern
            )
        )

    def test_django_admin_url_is_masked_by_the_custom_filter_but_not_stock(
        self,
    ) -> None:
        raw_value = "some-real-admin-path"
        self.assertEqual(
            self.stock_filter.cleanse_setting("DJANGO_ADMIN_URL", raw_value),
            raw_value,
        )
        self.assertEqual(
            self.custom_filter.cleanse_setting("DJANGO_ADMIN_URL", raw_value),
            self.custom_filter.cleansed_substitute,
        )

    def test_previously_masked_settings_remain_masked(self) -> None:
        for name in ("ANTHROPIC_API_KEY", "DB_PASSWORD", "DJANGO_SECRET_KEY"):
            self.assertEqual(
                self.custom_filter.cleanse_setting(name, "real-value"),
                self.custom_filter.cleansed_substitute,
            )

    def test_non_secret_operator_facing_settings_remain_visible(self) -> None:
        # CORS_ALLOWED_ORIGINS/FRONTEND_URL are meant to be visible/non-secret
        # — the custom pattern must not accidentally widen the match to catch
        # them just because it now also matches ADMIN_URL.
        self.assertEqual(
            self.custom_filter.cleanse_setting(
                "CORS_ALLOWED_ORIGINS", settings.CORS_ALLOWED_ORIGINS
            ),
            settings.CORS_ALLOWED_ORIGINS,
        )
        self.assertEqual(
            self.custom_filter.cleanse_setting(
                "FRONTEND_URL", settings.FRONTEND_URL
            ),
            settings.FRONTEND_URL,
        )

    def test_get_safe_settings_diff_touches_only_admin_url_settings(self) -> None:
        # The actual claim: comparing the full masked-key sets between the
        # stock and custom filters should show DJANGO_ADMIN_URL (and the
        # incidental _ADMIN_URL_PATTERN regex object — a non-secret compiled
        # pattern that happens to satisfy Django's settings-loader naming
        # rule) newly masked, and nothing newly unmasked.
        stock_masked = {
            key
            for key, value in self.stock_filter.get_safe_settings().items()
            if value == self.stock_filter.cleansed_substitute
        }
        custom_masked = {
            key
            for key, value in self.custom_filter.get_safe_settings().items()
            if value == self.custom_filter.cleansed_substitute
        }
        self.assertEqual(
            custom_masked - stock_masked,
            {"DJANGO_ADMIN_URL", "_ADMIN_URL_PATTERN"},
        )
        self.assertEqual(stock_masked - custom_masked, set())

    @override_settings(
        DEBUG=True,
        ALLOWED_HOSTS=["localhost"],
        DEFAULT_EXCEPTION_REPORTER_FILTER=(
            "config.debug.AdminHardeningExceptionReporterFilter"
        ),
    )
    def test_live_debug_page_masks_admin_url_and_still_renders(self) -> None:
        # End-to-end repro of the security-reviewer's original finding: an
        # unhandled DisallowedHost exception with DEBUG=True renders Django's
        # standard debug page. Client(raise_request_exception=False) lets the
        # response come back rendered instead of re-raising the exception,
        # matching what a real browser would receive.
        client = Client(raise_request_exception=False)
        response = client.get(
            f"/{settings.DJANGO_ADMIN_URL}/", HTTP_HOST="evil.example.com"
        )
        self.assertEqual(response.status_code, 400)
        body = response.content.decode()
        self.assertIn("DisallowedHost", body)
        row_match = re.search(
            r"<td>DJANGO_ADMIN_URL</td>\s*<td class=\"code\"><pre>(.*?)</pre></td>",
            body,
        )
        self.assertIsNotNone(row_match, "settings dump row not found on debug page")
        self.assertNotIn(settings.DJANGO_ADMIN_URL, row_match.group(1))
        self.assertIn("*", row_match.group(1))


class CacheIsolationTests(SimpleTestCase):
    """
    Positive control for config.testing.RedisSafeTestRunner: asserts the
    *resolved* cache backend object, not settings.CACHES, so this fails
    loudly (rather than silently reconnecting to real Redis) if TEST_RUNNER
    is ever dropped from settings or the runner's override stops firing.
    """

    def test_default_cache_is_locmem_under_the_test_runner(self) -> None:
        self.assertIsInstance(caches["default"], LocMemCache)
