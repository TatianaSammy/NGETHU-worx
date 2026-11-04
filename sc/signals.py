import logging
from datetime import timedelta
from django.db.models.signals import pre_save, post_save, pre_delete, post_delete
from django.dispatch import receiver
from django.utils import timezone

from .models import (
    TRACKED_MODELS,
    MonthlyDutyRoster,
    ShiftAssignment,
    ShiftHandover,
    ShiftSectionRequirement,
    ShiftSupervisor,
    Status,
    invalidate_status_cache,
)
from .utils.audit import _store_snapshot, _pop_snapshot, _log_change, create_bulk_audit

logger = logging.getLogger(__name__)


# ----------------------------
# Audit signal handlers
# ----------------------------

@receiver(pre_save)
def capture_pre_save(sender, instance, **kwargs):
    """Capture a snapshot of instance fields before save for tracked models."""
    if getattr(sender, '_meta', None) and sender._meta.model_name in TRACKED_MODELS:
        try:
            _store_snapshot(instance)
        except Exception as e:
            logger.debug(f"Failed to store pre-save snapshot for {instance}: {e}")


@receiver(post_save)
def log_post_save(sender, instance, created, **kwargs):
    """
    Log audit entry with before/after snapshots for tracked models after save.
    Soft-delete (is_deleted=True) is treated as 'delete'.
    Bulk audit optimization for ShiftHandover takeover.
    """
    if getattr(sender, '_meta', None) and sender._meta.model_name in TRACKED_MODELS:
        audit_user = getattr(instance, "_audit_user", None)
        try:
            before = _pop_snapshot(instance)
            after = {f.name: getattr(instance, f.name) for f in instance._meta.concrete_fields}

            # Detect soft-delete
            if hasattr(instance, 'is_deleted') and instance.is_deleted:
                action = 'delete'
            else:
                action = 'create' if created else 'update'

            # Bulk takeover case: skip per-assignment logs if instance is ShiftHandover
            if isinstance(instance, ShiftHandover) and instance.is_taken_over:
                assignments = ShiftAssignment.objects.filter(
                    shift=instance.shift,
                    date=instance.date,
                    is_deleted=False
                )
                if instance.incoming_supervisor and not instance.incoming_supervisor.is_superuser:
                    assignments = assignments.filter(
                        staff__assigned_treatment_plant_id=instance.shift.treatment_plant_id,
                        staff__section_id=instance.incoming_supervisor.section_id,
                    )
                assignments = assignments.values_list('id', flat=True)
                create_bulk_audit(
                    user=instance.incoming_supervisor,
                    instance=instance,
                    assignments=assignments,
                    before=before,
                    after=after,
                    action='bulk_takeover'
                )
            else:
                _log_change(instance, action, user=audit_user, before=before, after=None if action == 'delete' else after)

        except Exception as e:
            logger.debug(f"Skipping post_save audit for {instance}: {e}")
        finally:
            if hasattr(instance, "_audit_user"):
                del instance._audit_user


@receiver(pre_delete)
def capture_pre_delete(sender, instance, **kwargs):
    """Capture snapshot of instance fields before delete for tracked models."""
    if getattr(sender, '_meta', None) and sender._meta.model_name in TRACKED_MODELS:
        try:
            _store_snapshot(instance)
        except Exception as e:
            logger.debug(f"Failed to store pre-delete snapshot for {instance}: {e}")


@receiver(post_delete)
def log_post_delete(sender, instance, **kwargs):
    """Log audit entry after physical delete for tracked models."""
    if getattr(sender, '_meta', None) and sender._meta.model_name in TRACKED_MODELS:
        try:
            before = _pop_snapshot(instance)
            _log_change(instance, 'delete', before=before, after=None)
        except Exception as e:
            logger.debug(f"Skipping post_delete audit for {instance}: {e}")


# ----------------------------
# Status cache signals
# ----------------------------

@receiver(post_save, sender=Status)
@receiver(post_delete, sender=Status)
def refresh_status_cache(sender, instance, **kwargs):
    """Invalidate cached Status objects when a Status instance is saved or deleted."""
    try:
        invalidate_status_cache(getattr(instance, 'name', None))
    except Exception as e:
        logger.debug(f"Skipping Status cache refresh for {instance}: {e}")


# ----------------------------
# ShiftAssignment temp supervisor auto-revert
# ----------------------------

@receiver(post_save, sender=ShiftAssignment)
def auto_revert_temp_supervisor(sender, instance, **kwargs):
    """
    Automatically clears temp_supervisor if more than 3 hours have elapsed
    since temp_supervisor_start. Logs changes for audit.
    """
    try:
        if instance.temp_supervisor and instance.temp_supervisor_start:
            now = timezone.now()
            elapsed = now - instance.temp_supervisor_start
            if elapsed >= timedelta(hours=3):
                old_temp = instance.temp_supervisor
                instance.temp_supervisor = None
                instance.temp_supervisor_end = now
                instance.temp_supervisor_start = None
                instance.save(update_fields=['temp_supervisor', 'temp_supervisor_start', 'temp_supervisor_end'])

                # Audit log
                if hasattr(instance, 'create_audit'):
                    instance.create_audit(
                        user=None,  # system action
                        action='update',
                        instance=instance,
                        before={'temp_supervisor': old_temp.id if old_temp else None},
                        after={'temp_supervisor': None}
                    )
                logger.info(f"Auto-reverted temp_supervisor for ShiftAssignment {instance.id}")
    except Exception as e:
        logger.debug(f"Failed auto-revert temp_supervisor for {instance}: {e}")


def _queue_shift_allocation(plant_id, section_id=None, roster_id=None):
    if roster_id:
        callback = lambda: _run_roster_allocation(roster_id)
    else:
        callback = lambda: _run_scope_allocation(plant_id, section_id)
    from django.db import transaction

    transaction.on_commit(callback)


def _run_roster_allocation(roster_id):
    try:
        from .services.auto_assignment import assign_for_new_roster

        assign_for_new_roster(roster_id)
    except Exception:
        logger.exception("Automatic shift allocation failed for roster %s", roster_id)


def _run_scope_allocation(plant_id, section_id):
    try:
        from .services.auto_assignment import assign_for_shift_scope

        assign_for_shift_scope(plant_id, section_id)
    except Exception:
        logger.exception(
            "Automatic shift allocation failed for treatment plant %s and section %s",
            plant_id,
            section_id,
        )


@receiver(post_save, sender=MonthlyDutyRoster)
def assign_new_roster(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        _queue_shift_allocation(instance.treatment_plant_id, roster_id=instance.pk)


@receiver(post_save, sender=ShiftSectionRequirement)
def assign_after_shift_requirement_change(sender, instance, raw=False, **kwargs):
    if not raw:
        _queue_shift_allocation(
            instance.shift.treatment_plant_id,
            instance.section_id,
        )


@receiver(post_save, sender=ShiftSupervisor)
def refresh_supervisor_assignments(sender, instance, raw=False, **kwargs):
    if not raw and instance.section_id:
        _queue_shift_allocation(instance.treatment_plant_id, instance.section_id)
