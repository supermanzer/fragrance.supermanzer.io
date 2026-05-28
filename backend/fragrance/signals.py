"""
fragrance/signals.py

Post-save signal for FragranceConfig that keeps the Celery Beat schedule
in sync whenever a user changes their run frequency, hour, or day settings.
"""

import json

from django.db.models.signals import post_save
from django.dispatch import receiver
from django_celery_beat.models import CrontabSchedule, PeriodicTask

from .models import FragranceConfig


@receiver(signal=post_save, sender=FragranceConfig)
def sync_user_schedule(
    sender: type,
    instance: FragranceConfig,
    **kwargs: object,
) -> None:
    """Upsert the PeriodicTask for this user whenever their config is saved."""
    _create_or_update_user_schedule(config=instance)


def _create_or_update_user_schedule(config: FragranceConfig) -> None:
    """
    Builds a CrontabSchedule from the user's frequency settings and upserts the
    corresponding PeriodicTask. Unused fields are silently ignored — for example,
    run_day_of_week has no effect when frequency is 'monthly'.

    This is an instance of the Strategy pattern: the schedule is data, not code.
    Changing frequency in the UI takes effect on the next Celery Beat tick without
    any redeploy.
    """
    crontab_kwargs: dict = {'minute': '0', 'hour': str(config.run_hour)}

    if config.frequency == 'weekly':
        crontab_kwargs.update({
            'day_of_week': str(config.run_day_of_week),
            'day_of_month': '*',
            'month_of_year': '*',
        })
    elif config.frequency == 'monthly':
        crontab_kwargs.update({
            'day_of_week': '*',
            'day_of_month': str(config.run_day_of_month),
            'month_of_year': '*',
        })
    elif config.frequency == 'yearly':
        crontab_kwargs.update({
            'day_of_week': '*',
            'day_of_month': str(config.run_day_of_month),
            'month_of_year': str(config.run_month),
        })

    schedule, _ = CrontabSchedule.objects.get_or_create(**crontab_kwargs)
    PeriodicTask.objects.update_or_create(
        name=f'fragrance-run-{config.user_id}',
        defaults={
            'crontab': schedule,
            'task': 'fragrance.tasks.monthly_fragrance_run',
            'args': json.dumps([config.user_id]),
        },
    )
