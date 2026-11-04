from django.db.models.signals import post_migrate
from django.dispatch import receiver

from core.services.periodic_tasks import register_minute_task


@receiver(post_migrate, dispatch_uid="microbial.register_workflow_refresh_schedule")
def register_workflow_refresh_schedule(sender, **kwargs):
    register_minute_task(sender, "microbial", "microbial.refresh_analysis_workflow")
