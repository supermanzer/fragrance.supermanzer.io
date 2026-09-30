"""
Defining the Celery application that will execute scheduled tasks
"""

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Version-controlled system schedules. The DatabaseScheduler syncs these into
# django_celery_beat's tables at beat start; the per-user
# fragrance-run-<user_id> tasks are managed separately by fragrance/signals.py.
# Staggered, off the top of the hour (UTC). Beat only dispatches: the tasks
# themselves run in celery-worker, which has the public egress Stripe needs.
app.conf.beat_schedule = {
    "system-reap-stale-runs": {
        "task": "fragrance.tasks.reap_stale_runs",
        "schedule": crontab(minute=37, hour="*/2"),
    },
    "system-retry-failed-runs": {
        "task": "fragrance.tasks.retry_failed_runs",
        "schedule": crontab(minute="3-59/10"),
    },
    "system-operator-failure-digest": {
        "task": "fragrance.tasks.send_operator_failure_digest",
        "schedule": crontab(minute=23, hour="*/4"),
    },
    "system-reconcile-stripe": {
        "task": "fragrance.tasks.reconcile_stripe_subscriptions",
        "schedule": crontab(minute=17, hour=3),
    },
    "system-flush-expired-jwt-tokens": {
        "task": "fragrance.tasks.flush_expired_jwt_tokens",
        "schedule": crontab(minute=43, hour=4),
    },
    "system-prune-stripe-event-payloads": {
        "task": "fragrance.tasks.prune_stripe_event_payloads",
        "schedule": crontab(minute=29, hour=5),
    },
}
