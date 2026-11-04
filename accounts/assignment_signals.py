import logging

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from accounts.models import CustomUser


logger = logging.getLogger(__name__)


@receiver(pre_save, sender=CustomUser)
def capture_assignment_scope(sender, instance, raw=False, **kwargs):
    if raw or not instance.pk:
        instance._assignment_scope_changed = True
        return

    previous = sender.objects.filter(pk=instance.pk).values(
        "assigned_treatment_plant_id",
        "section_id",
        "is_active",
    ).first()
    instance._assignment_scope_changed = bool(
        previous
        and (
            previous["assigned_treatment_plant_id"] != instance.assigned_treatment_plant_id
            or previous["section_id"] != instance.section_id
            or previous["is_active"] != instance.is_active
        )
    )


@receiver(post_save, sender=CustomUser)
def queue_scope_assignments(sender, instance, created, raw=False, **kwargs):
    if raw or (not created and not getattr(instance, "_assignment_scope_changed", False)):
        return
    if not instance.is_active or not instance.assigned_treatment_plant_id or not instance.section_id:
        return

    user_id = instance.pk

    def assign():
        from sc.services.auto_assignment import assign_for_user as assign_shifts
        from weeklytasks.services.auto_assignment import assign_for_user as assign_tasks

        for service in (assign_shifts, assign_tasks):
            try:
                service(user_id)
            except Exception:
                logger.exception("Automatic duty assignment failed after user %s was saved", user_id)

    transaction.on_commit(assign)
