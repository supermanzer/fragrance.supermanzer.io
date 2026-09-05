"""
Custom exception-reporter filter, wired via DEFAULT_EXCEPTION_REPORTER_FILTER
in settings.py.

Django's project-level `HIDDEN_SETTINGS` setting (a regex string read from
settings.py) was removed in Django 3.1 — the undocumented
`django.views.debug.ExceptionReporterFilter` class it configured no longer
exists. As of Django 6.0.5 (confirmed by reading
`django/views/debug.py:SafeExceptionReporterFilter` directly, and by a
whole-tree `grep -rn HIDDEN_SETTINGS` against the installed package returning
zero hits), the masking regex is a hardcoded class attribute,
`SafeExceptionReporterFilter.hidden_settings`, and the only supported way to
extend it is subclassing that filter and pointing
`DEFAULT_EXCEPTION_REPORTER_FILTER` at the subclass.

See .claude/plans/2026-08-30-admin-hardening-plan.md, Finding 13: with
DEBUG=True, Django's standard debug error page renders every setting in a
plain-text dump, and DJANGO_ADMIN_URL's name doesn't match any term in
Django's default regex (API|AUTH|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE),
so the exact unguessable path this feature exists to protect would leak
verbatim on any unhandled production error.
"""

import re

from django.views.debug import SafeExceptionReporterFilter


class AdminHardeningExceptionReporterFilter(SafeExceptionReporterFilter):
    """
    Extends Django's own default hidden_settings pattern (rather than
    retyping it) so a future Django upgrade that adds a new default term
    doesn't silently stop being masked here just because this file wasn't
    updated in lockstep.
    """

    hidden_settings = re.compile(
        SafeExceptionReporterFilter.hidden_settings.pattern + "|ADMIN_URL",
        flags=re.IGNORECASE,
    )
