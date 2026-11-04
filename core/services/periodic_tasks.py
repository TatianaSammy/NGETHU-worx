from django.apps import apps
from django.db import connection


def register_minute_task(sender, app_label, task_name):
    if getattr(sender, "label", None) != app_label:
        return
    try:
        PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
        IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
        if PeriodicTask._meta.db_table not in connection.introspection.table_names():
            return
        interval, _ = IntervalSchedule.objects.get_or_create(every=1, period="minutes")
        PeriodicTask.objects.update_or_create(
            name=task_name,
            defaults={"task": task_name, "interval": interval, "enabled": True},
        )
    except Exception:
        return
