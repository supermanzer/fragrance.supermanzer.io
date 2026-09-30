"""Test runner that isolates Django's cache framework from the real Redis instance.

Django's test runner creates an isolated test *database* for every run
(`setup_databases`), but has no equivalent concept for `CACHES` -- it leaves
`settings.CACHES` pointed at whatever's configured for the live app. Since
`CACHES["default"]` targets Redis DB 1 (see `config/settings.py`), any code
path that touches the cache during a test -- `cache.clear()`, for instance,
which `RedisCache.clear()` compiles to a real `FLUSHDB` -- operates on the
real, possibly-shared Redis instance rather than a fixture.

This module is deliberately not named `test_*.py` / `*_test.py`: Django's
`DiscoverRunner` default test-file pattern is `test*.py`, and a module in
that shape sitting under `manage.py test`'s discovery root would itself get
imported as a candidate test module. Naming it `testing.py` sidesteps that
ambiguity rather than relying on it being harmless (it would be -- this
module defines no `TestCase` -- but there's no reason to depend on that).

Swapping `CACHES` via a plain attribute assignment would not take effect:
Django's cache framework lazily resolves and caches connections per the
`CacheHandler` in `django.core.cache`, and that handler is only reset by the
`setting_changed` signal (see `clear_cache_handlers` in
`django/test/signals.py`, which is wired specifically to the `"CACHES"`
setting name). `override_settings(...).enable()` is what fires that signal;
a bare assignment would leave any already-resolved cache connection pointed
at the old backend.
"""
import os
from typing import Any

from django.test.runner import DiscoverRunner
from django.test.utils import override_settings

# Celery's Settings.broker_url / result_backend read these environment
# variables before any configured value, so this is the override that sticks.
_TEST_CELERY_ENV = {
    "CELERY_BROKER_URL": "memory://",
    "CELERY_RESULT_BACKEND": "cache+memory://",
}


_TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fragrance-tests",
    }
}


class RedisSafeTestRunner(DiscoverRunner):
    """DiscoverRunner that forces an isolated in-memory cache for the whole run.

    Structural, not spot-applied: no test, present or future, can reach the
    real Redis cache regardless of whether it remembers to guard itself --
    the override is enabled before any test runs and disabled after the last
    one finishes. Only active under `manage.py test`; runserver, gunicorn,
    and Celery never construct this runner, so `CACHES` is untouched for
    every other entry point.
    """

    _cache_override: override_settings | None = None
    _previous_celery_env: dict[str, str | None] | None = None

    def setup_test_environment(self, **kwargs: Any) -> None:
        super().setup_test_environment(**kwargs)
        # Same isolation for Celery: a test that reaches an unpatched
        # `.delay()` (the failure paths of monthly_fragrance_run do, via the
        # failure-notice tasks) must publish into a throwaway in-memory
        # broker, never the live Redis a running celery-worker consumes.
        self._previous_celery_env = {name: os.environ.get(name) for name in _TEST_CELERY_ENV}
        os.environ.update(_TEST_CELERY_ENV)
        self._cache_override = override_settings(CACHES=_TEST_CACHES)
        self._cache_override.enable()

    def teardown_test_environment(self, **kwargs: Any) -> None:
        if self._previous_celery_env is not None:
            for name, previous in self._previous_celery_env.items():
                if previous is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = previous
            self._previous_celery_env = None
        if self._cache_override is not None:
            self._cache_override.disable()
            self._cache_override = None
        super().teardown_test_environment(**kwargs)
