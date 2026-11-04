from django.apps import apps
from django.db import connection
from django.db.models.signals import post_migrate
from django.dispatch import receiver


@receiver(post_migrate)
def register_workflow_refresh_schedule(sender, **kwargs):
    if getattr(sender, "label", None) != "destw":
        return
    try:
        PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
        IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
        if PeriodicTask._meta.db_table not in connection.introspection.table_names():
            return
        interval, _ = IntervalSchedule.objects.get_or_create(every=1, period="minutes")
        PeriodicTask.objects.update_or_create(
            name="destw.refresh_analysis_workflow",
            defaults={
                "task": "destw.refresh_analysis_workflow",
                "interval": interval,
                "enabled": True,
            },
        )
    except Exception:
        return
