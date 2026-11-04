"""Scheduled tasks for the chemical-analysis workflow."""

import logging
from datetime import timedelta

from celery import shared_task
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models import Q
from django.db import transaction
from django.utils import timezone

from ca.models import ChemicalAnalysisProcess, ChemicalAnalysisRequest, ChemicalBatch


logger = logging.getLogger(__name__)


@shared_task(name="ca.auto_assign_due_chemical_analysts")
def auto_assign_due_chemical_analysts():
    """Assign an eligible analyst to any unassigned active request immediately.

    The model method takes a row lock and is idempotent, so this task is safe
    to run repeatedly and alongside workflow requests that check assignment.
    """
    now = timezone.now()
    due_requests = (
        ChemicalAnalysisRequest.objects
        .filter(
            status=ChemicalAnalysisRequest.Status.PENDING,
            assigned_analysts__isnull=True,
        )
        .select_related("batch", "batch__treatment_plant")
        .order_by("created_at", "pk")
        .distinct()
    )

    assigned_count = 0
    synchronized_count = 0
    checked_count = 0
    for request in due_requests.iterator(chunk_size=100):
        checked_count += 1
        try:
            assigned = request.auto_assign_analysts_if_due(now=now)
            assigned_count += assigned.count()
            if assigned.exists():
                try:
                    process = request.analysis_process
                except ChemicalAnalysisProcess.DoesNotExist:
                    process = None
                if process is not None:
                    from ca.chemical_flow import advance_analyst_assignment_workflow

                    synchronized_count += advance_analyst_assignment_workflow(
                        process,
                        automatic=True,
                    )
        except Exception:
            logger.exception(
                "Automatic analyst assignment failed for request %s",
                request.pk,
            )

    # Retry workflow synchronization for prior assignments, and bootstrap
    # workflows for received requests that were assigned outside the flow.
    requests_to_sync = (
        ChemicalAnalysisRequest.objects
        .filter(
            status__in={
                ChemicalAnalysisRequest.Status.PENDING,
                ChemicalAnalysisRequest.Status.IN_PROGRESS,
            },
            assigned_analysts__isnull=False,
        )
        .filter(
            Q(analysis_process__status__in={
                "CREATED",
                "RECEIVED",
                "REQUESTED",
                "CHECKLIST",
                "ASSIGNED",
            })
            | Q(
                analysis_process__isnull=True,
                batch__status=ChemicalBatch.Status.RECEIVED,
            )
        )
        .select_related("analysis_process", "batch", "batch__received_by", "requested_by")
        .distinct()
        .order_by("pk")
    )
    for request in requests_to_sync.iterator(chunk_size=100):
        try:
            from ca.chemical_flow import (
                advance_analyst_assignment_workflow,
                start_chemical_analysis_for_batch,
            )

            try:
                process = request.analysis_process
            except ChemicalAnalysisProcess.DoesNotExist:
                process = start_chemical_analysis_for_batch(
                    request.batch,
                    request,
                    request.batch.received_by or request.requested_by,
                    None,
                )

            if advance_analyst_assignment_workflow(
                process,
                automatic=True,
            ):
                synchronized_count += 1
        except Exception:
            logger.exception(
                "Automatic workflow synchronization failed for request %s",
                request.pk,
            )

    logger.info(
        "Automatic chemical analyst assignment checked %s requests; assigned %s analysts and synchronized %s workflows.",
        checked_count,
        assigned_count,
        synchronized_count,
    )
    return {
        "checked": checked_count,
        "assigned": assigned_count,
        "synchronized": synchronized_count,
    }


@shared_task(name="ca.escalate_due_chemical_workflows")
def escalate_due_chemical_workflows():
    """Create scoped, deduplicated alerts for overdue and stalled CA work."""
    from ca.services.workflow_notifications import escalate_due_chemical_workflows as escalate

    try:
        result = escalate()
    except Exception:
        logger.exception("Chemical workflow SLA escalation failed")
        raise
    logger.info("Chemical workflow escalation completed: %s", result)
    return result


@shared_task(name="ca.publish_ca_outbox_events")
def publish_ca_outbox_events(batch_size=100):
    """Publish pending dashboard invalidations and retry failures with backoff."""
    from ca.models import ChemicalOutboxEvent

    now = timezone.now()
    claimed = []
    with transaction.atomic():
        events = list(
            ChemicalOutboxEvent.objects.select_for_update(skip_locked=True)
            .filter(delivered_at__isnull=True, available_at__lte=now)
            .order_by("created_at", "event_id")[: max(1, min(int(batch_size), 500))]
        )
        for event in events:
            event.attempt_count += 1
            event.available_at = now + timedelta(minutes=1)
            event.save(update_fields=["attempt_count", "available_at"])
            claimed.append((str(event.event_id), event.event_type, event.aggregate_type, event.aggregate_id, event.payload))

    layer = get_channel_layer()
    delivered = 0
    for event_id, event_type, aggregate_type, aggregate_id, payload in claimed:
        try:
            if layer is None:
                raise RuntimeError("No channel layer is configured.")
            event = {
                "type": "data_changed",
                "app_label": "ca",
                "model": aggregate_type,
                "action": event_type,
                "plant_id": payload.get("plant_id"),
                "timestamp": timezone.now().isoformat(),
                "event_id": event_id,
                "aggregate_id": aggregate_id,
            }
            groups = ["live-app-ca"]
            if payload.get("plant_id"):
                groups.append(f"live-plant-{payload['plant_id']}")
            for group in groups:
                async_to_sync(layer.group_send)(group, event)
            ChemicalOutboxEvent.objects.filter(event_id=event_id, delivered_at__isnull=True).update(
                delivered_at=timezone.now(), last_error=""
            )
            delivered += 1
        except Exception as exc:
            # Query the attempt count once on failure to calculate bounded exponential backoff.
            attempt = ChemicalOutboxEvent.objects.filter(event_id=event_id).values_list("attempt_count", flat=True).first() or 1
            delay = min(60 * (2 ** min(8, attempt - 1)), 21600)
            ChemicalOutboxEvent.objects.filter(event_id=event_id, delivered_at__isnull=True).update(
                available_at=timezone.now() + timedelta(seconds=delay), last_error=str(exc)[:4000]
            )
            logger.warning("CA outbox event %s delivery failed; retrying in %ss", event_id, delay, exc_info=True)
    return {"claimed": len(claimed), "delivered": delivered}
