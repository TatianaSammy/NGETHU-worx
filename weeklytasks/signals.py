import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from weeklytasks.models import MonthlyDutySchedule, WeeklyDutyTask


logger = logging.getLogger(__name__)


@receiver(post_save, sender=MonthlyDutySchedule)
def populate_monthly_duty_weeks(sender, instance, raw=False, **kwargs):
    if raw:
        return
    from weeklytasks.services.duty_week_sync import ensure_duty_weeks

    try:
        ensure_duty_weeks(instance.pk)
    except Exception:
        logger.exception("Could not populate duty weeks for weekly schedule %s", instance.pk)


@receiver(post_save, sender="sc.MonthlyDutyRoster")
def queue_shift_control_schedule_sync(sender, instance, raw=False, **kwargs):
    if raw:
        return
    roster_id = instance.pk

    def sync():
        from weeklytasks.services.duty_week_sync import sync_shift_control_roster

        try:
            sync_shift_control_roster(roster_id)
        except Exception:
            logger.exception("Could not synchronize weekly duty weeks from Shift Control roster %s", roster_id)

    transaction.on_commit(sync)


@receiver(post_save, sender=WeeklyDutyTask)
def queue_weekly_task_assignment(sender, instance, raw=False, **kwargs):
    if raw:
        return
    task_id = instance.pk

    def assign():
        from weeklytasks.services.auto_assignment import assign_task

        try:
            assign_task(task_id)
        except Exception:
            logger.exception("Could not queue automatic assignment for weekly task %s", task_id)

    transaction.on_commit(assign)
