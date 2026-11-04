"""Permission-aware, plant-scoped CA reminders and SLA escalation."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ca.models import (
    ChemicalAnalysisProcess,
    ChemicalAnalysisRequest,
    ChemicalBatch,
    ChemicalBatchHold,
    ChemicalWorkflowAlert,
)

logger = logging.getLogger(__name__)


def _eligible_users(batch, permissions):
    User = get_user_model()
    users = User.objects.filter(is_active=True)
    if batch.treatment_plant_id:
        users = users.filter(
            Q(assigned_treatment_plant_id=batch.treatment_plant_id) | Q(is_superuser=True)
        )
    recipients = []
    for user in users.iterator(chunk_size=200):
        if user.is_superuser or any(user.has_perm(permission) for permission in permissions):
            recipients.append(user)
    return recipients


@transaction.atomic
def create_workflow_alert(
    *, batch, alert_type, title, message, url, severity="medium", dedupe_key,
    permissions, request=None, exclude_user_ids=(),
):
    created = []
    excluded_ids = {int(value) for value in (exclude_user_ids or ()) if value is not None}
    for recipient in _eligible_users(batch, permissions):
        if recipient.pk in excluded_ids:
            continue
        alert, was_created = ChemicalWorkflowAlert.objects.get_or_create(
            recipient=recipient,
            dedupe_key=str(dedupe_key),
            defaults={
                "batch": batch,
                "analysis_request": request,
                "alert_type": str(alert_type),
                "title": str(title)[:180],
                "message": str(message),
                "url": str(url),
                "severity": str(severity).lower(),
            },
        )
        if was_created:
            created.append(alert)
    if created:
        try:
            from ca.signals import broadcast_dashboard_event

            transaction.on_commit(lambda: broadcast_dashboard_event(
                "chemical_workflow_alert",
                {
                    "alert_ids": [item.pk for item in created],
                    "batch_id": batch.pk,
                    "alert_type": alert_type,
                    "plant_id": batch.treatment_plant_id,
                },
            ))
        except Exception:
            logger.exception("Could not broadcast a CA workflow notification")
    return created


@transaction.atomic
def notify_unassigned_analysis_request(analysis_request):
    """Create or reopen assignment prompts until this request gets an analyst."""
    if (
        not getattr(analysis_request, "pk", None)
        or analysis_request.status != ChemicalAnalysisRequest.Status.PENDING
        or analysis_request.assigned_analysts.exists()
        or not analysis_request.batch_id
    ):
        return []
    batch = analysis_request.batch
    if batch is None:
        return []

    now = timezone.now()
    dedupe_key = f"assignment-pending:{analysis_request.pk}"
    message = (
        f"Assign an analyst to {batch.chemical.name} batch {batch.batch_number}."
        if batch.chemical_id else f"Assign an analyst to batch {batch.batch_number}."
    )
    changed = []
    reopened = []
    for recipient in _eligible_users(batch, ("ca.create_analysis_request",)):
        alert, created = ChemicalWorkflowAlert.objects.get_or_create(
            recipient=recipient,
            dedupe_key=dedupe_key,
            defaults={
                "batch": batch,
                "analysis_request": analysis_request,
                "alert_type": "ca_assignment_pending",
                "title": "Analysis request needs an analyst",
                "message": message,
                "url": f"/ca/requests/{analysis_request.pk}/assign-analyst/",
                "severity": "medium",
            },
        )
        if created:
            changed.append(alert)
        elif alert.acknowledged_at:
            ChemicalWorkflowAlert.objects.filter(pk=alert.pk).update(
                acknowledged_at=None,
                acknowledged_by=None,
                created_at=now,
                message=message,
            )
            alert.refresh_from_db()
            changed.append(alert)
            reopened.append(alert)
            try:
                from alerts.models import VoiceAlert

                VoiceAlert.objects.filter(source_key=f"ca-workflow:{alert.pk}").update(
                    repeat_due_at=None,
                    repeat_count=0,
                )
            except Exception:
                logger.exception("Could not reset CA assignment voice reminders for alert %s", alert.pk)

    if changed:
        try:
            from ca.signals import broadcast_dashboard_event

            transaction.on_commit(lambda: broadcast_dashboard_event(
                "chemical_workflow_alert",
                {
                    "alert_ids": [item.pk for item in changed],
                    "batch_id": batch.pk,
                    "alert_type": "ca_assignment_pending",
                    "plant_id": batch.treatment_plant_id,
                },
            ))
        except Exception:
            logger.exception("Could not broadcast a CA analyst assignment prompt")
    for alert in reopened:
        def queue_reopened_voice(alert_id=alert.pk):
            try:
                from alerts.models import AlertConfig
                from alerts.tasks import generate_voice_notification

                config_id = AlertConfig.objects.filter(
                    alert_type="ca_assignment_pending", active=True
                ).values_list("pk", flat=True).first()
                generate_voice_notification.delay(
                    message=alert.message,
                    title=alert.title,
                    source_key=f"ca-workflow:{alert_id}",
                    alert_config_id=config_id,
                    recipient_ids=[alert.recipient_id],
                )
            except Exception:
                logger.exception("Could not restart CA assignment voice alert %s", alert_id)

        transaction.on_commit(queue_reopened_voice)
    return changed


def clear_assignment_notifications(analysis_request):
    """Clear assignment prompts and escalations after an analyst is assigned."""
    if not getattr(analysis_request, "pk", None):
        return 0
    if not analysis_request.assigned_analysts.exists():
        return 0
    alerts = list(ChemicalWorkflowAlert.objects.filter(
        analysis_request=analysis_request,
        alert_type__in=("ca_analysis_assignment", "ca_assignment_pending", "ca_assignment_escalation"),
        acknowledged_at__isnull=True,
    ).only("pk", "recipient_id"))
    if not alerts:
        return 0
    now = timezone.now()
    ids = [item.pk for item in alerts]
    ChemicalWorkflowAlert.objects.filter(pk__in=ids).update(acknowledged_at=now)
    try:
        from alerts.models import VoiceAlert

        VoiceAlert.objects.filter(source_key__in=[f"ca-workflow:{alert_id}" for alert_id in ids]).update(
            repeat_due_at=None
        )
    except Exception:
        logger.exception("Could not stop CA assignment voice reminders")

    def broadcast_cleared():
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer

            layer = get_channel_layer()
            if not layer:
                return
            for item in alerts:
                async_to_sync(layer.group_send)(
                    f"user_{item.recipient_id}_alerts",
                    {
                        "type": "alert_acknowledged",
                        "id": f"ca_workflow_alert_{item.pk}",
                        "timestamp": now.isoformat(),
                    },
                )
        except Exception:
            logger.exception("Could not broadcast cleared CA assignment alerts")

    transaction.on_commit(broadcast_cleared)
    return len(alerts)


def escalate_due_chemical_workflows(*, now=None):
    now = now or timezone.now()
    counts = {"assignment_pending": 0, "assignment": 0, "analysis": 0, "holds": 0, "stalled": 0}

    # Backfill persistent reminders for pending requests created before this
    # notification lifecycle was installed. The helper is idempotent.
    pending_requests = ChemicalAnalysisRequest.objects.filter(
        status=ChemicalAnalysisRequest.Status.PENDING,
        assigned_analysts__isnull=True,
    ).select_related("batch", "batch__chemical").distinct()
    for request in pending_requests.iterator(chunk_size=100):
        try:
            counts["assignment_pending"] += len(notify_unassigned_analysis_request(request))
        except Exception:
            logger.exception("Could not create CA assignment prompt for request %s", request.pk)

    due_assignments = (
        ChemicalAnalysisRequest.objects.filter(
            status=ChemicalAnalysisRequest.Status.PENDING,
            assigned_analysts__isnull=True,
        )
        .select_related("batch", "batch__chemical")
        .distinct()
    )
    for request in due_assignments.iterator(chunk_size=100):
        batch = request.batch
        if not batch:
            continue
        try:
            assigned = request.auto_assign_analysts_if_due(now=now)
            if assigned.exists():
                try:
                    process = request.analysis_process
                except ChemicalAnalysisProcess.DoesNotExist:
                    process = None
                if process is not None:
                    from ca.chemical_flow import advance_analyst_assignment_workflow

                    advance_analyst_assignment_workflow(
                        process,
                        automatic=True,
                    )
                continue
            key_day = now.date().isoformat()
            created = create_workflow_alert(
                batch=batch,
                alert_type="ca_assignment_escalation",
                title="Chemical analysis has no assigned analyst",
                message=(
                    f"No eligible analyst was available for {batch.chemical.name} "
                    f"batch {batch.batch_number}. Assign an eligible analyst at this plant."
                ),
                url=f"/ca/requests/{request.pk}/assign-analyst/",
                severity="high",
                dedupe_key=f"assignment-overdue:{request.pk}:{key_day}",
                permissions=("ca.create_analysis_request",),
                request=request,
            )
            counts["assignment"] += len(created)
        except Exception:
            logger.exception("CA assignment escalation failed for request %s", request.pk)

    overdue = (
        ChemicalAnalysisRequest.objects.filter(
            status=ChemicalAnalysisRequest.Status.IN_PROGRESS,
            analysis_deadline__lt=now,
        )
        .select_related("batch", "batch__chemical")
        .distinct()
    )
    for request in overdue.iterator(chunk_size=100):
        batch = request.batch
        if not batch:
            continue
        try:
            created = create_workflow_alert(
                batch=batch,
                alert_type="ca_analysis_sla_escalation",
                title="Chemical analysis deadline missed",
                message=f"Analysis request {request.request_id} for batch {batch.batch_number} is overdue.",
                url=f"/ca/requests/{request.pk}/",
                severity="critical",
                dedupe_key=f"analysis-overdue:{request.pk}:{now.date().isoformat()}",
                permissions=("ca.start_chemical_analysis", "ca.complete_chemical_analysis", "ca.review_chemical_analysis"),
                request=request,
            )
            counts["analysis"] += len(created)
        except Exception:
            logger.exception("CA analysis SLA escalation failed for request %s", request.pk)

    open_holds = ChemicalBatchHold.objects.filter(
        status=ChemicalBatchHold.Status.OPEN,
    ).select_related("batch", "batch__chemical", "batch__analysis_process")
    for hold in open_holds.iterator(chunk_size=100):
        batch = hold.batch
        try:
            created = create_workflow_alert(
                batch=batch,
                alert_type="ca_batch_hold",
                title="Chemical batch is on intake hold",
                message=f"{hold.get_issue_display()}: {hold.details}",
                url=f"/ca/batches/{batch.pk}/",
                severity="high",
                dedupe_key=f"intake-hold:{hold.pk}",
                permissions=("ca.resolve_chemical_batch_hold", "ca.view_chemicalanalysisprocess"),
            )
            counts["holds"] += len(created)
        except Exception:
            logger.exception("CA hold reminder failed for hold %s", hold.pk)

    stalled_before = now - timedelta(hours=24)
    stalled = ChemicalAnalysisProcess.objects.filter(
        updated_at__lt=stalled_before,
        status__in=(
            ChemicalAnalysisProcess.WorkflowStatus.RECEIVED,
            ChemicalAnalysisProcess.WorkflowStatus.REQUESTED,
            ChemicalAnalysisProcess.WorkflowStatus.ASSIGNED,
            ChemicalAnalysisProcess.WorkflowStatus.CHECKLIST,
            ChemicalAnalysisProcess.WorkflowStatus.ANALYSIS,
            ChemicalAnalysisProcess.WorkflowStatus.RETEST,
            ChemicalAnalysisProcess.WorkflowStatus.QA,
            ChemicalAnalysisProcess.WorkflowStatus.APPROVED,
        ),
    ).select_related("batch", "batch__chemical")
    for process in stalled.iterator(chunk_size=100):
        batch = process.batch
        if not batch or batch.has_active_holds:
            continue
        permission_by_status = {
            "RECEIVED": ("ca.receive_chemical_batch",),
            "REQUESTED": ("ca.create_analysis_request",),
            "ASSIGNED": ("ca.start_chemical_analysis",),
            "CHECKLIST": ("ca.complete_receiving_checklist",),
            "ANALYSIS": ("ca.record_chemical_result", "ca.complete_chemical_analysis"),
            "RETEST": ("ca.perform_chemical_retest", "ca.record_chemical_result"),
            "QA": ("ca.review_chemical_analysis", "ca.approve_chemical_batch"),
            "APPROVED": ("ca.approve_chemical_coa", "ca.utilize_chemical_batch", "ca.return_chemical_batch"),
        }
        try:
            created = create_workflow_alert(
                batch=batch,
                request=process.analysis_request if process.analysis_request_id else None,
                alert_type="ca_workflow_stalled",
                title="Chemical workflow needs attention",
                message=f"Batch {batch.batch_number} has remained in {dict(process.WorkflowStatus.choices).get(process.status, process.status)} for over 24 hours.",
                url=f"/ca/processes/{process.pk}/",
                severity="medium",
                dedupe_key=f"stalled:{process.pk}:{now.date().isoformat()}",
                permissions=permission_by_status.get(process.status, ("ca.view_chemicalanalysisprocess",)),
            )
            counts["stalled"] += len(created)
        except Exception:
            logger.exception("CA stalled-workflow alert failed for process %s", process.pk)

    return counts
