
from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from functools import wraps
from io import BytesIO
from threading import local
from typing import Any

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models.signals import m2m_changed, post_save, pre_save
from django.dispatch import Signal, receiver
from django.utils import timezone
from PIL import Image

from audit_logs.models import AuditLogEntry

from .models import (
    BatchTestResult,
    ChemicalAnalysisRequest,
    ChemicalBatch,
    ChemicalCoA,
    ChemicalBatchHold,
    ChemicalInventoryMovement,
    ChemicalAnalysisProcess,
    ChemicalLabComparisonResult,
    ChemicalLabReferral,
    ChemicalWorkflowEvent,
    ChemicalWorkflowAlert,
    ChecklistForWaterTreatmentChemicalAnalysis,
)
from .utils.coa_pdf import generate_coa_pdf, generate_qr_code


logger = logging.getLogger(__name__)


@receiver(post_save, dispatch_uid="ca.persist_domain_invalidation")
def persist_domain_invalidation(sender, instance, created=False, raw=False, **kwargs):
    """Write important CA refresh hints to the outbox in the entity transaction."""
    if raw or sender not in {
        ChemicalBatch,
        ChemicalAnalysisRequest,
        ChemicalAnalysisProcess,
        BatchTestResult,
        ChecklistForWaterTreatmentChemicalAnalysis,
        ChemicalCoA,
        ChemicalWorkflowAlert,
        ChemicalLabReferral,
        ChemicalLabComparisonResult,
        ChemicalBatchHold,
        ChemicalInventoryMovement,
    }:
        return
    from ca.services.outbox import enqueue_ca_event

    batch = getattr(instance, "batch", None)
    if batch is None and getattr(instance, "batch_id", None):
        batch = instance.batch
    if batch is None and isinstance(instance, ChecklistForWaterTreatmentChemicalAnalysis):
        request = instance.chemical_analysis_request
        batch = request.batch if request and request.batch_id else None
    if batch is None and getattr(instance, "analysis_request_id", None):
        request = instance.analysis_request
        batch = request.batch if request and request.batch_id else None
    plant_id = getattr(batch, "treatment_plant_id", None)
    if not plant_id and isinstance(instance, ChemicalBatch):
        plant_id = instance.treatment_plant_id
    batch_id = instance.pk if isinstance(instance, ChemicalBatch) else getattr(batch, "pk", None)
    # Some audited writes deliberately omit ``updated_at`` from update_fields;
    # a timestamp-based key would then suppress later invalidations for the
    # same row. These events are idempotent invalidation hints, so assign each
    # committed model save its own durable event key.
    event_key = f"{sender._meta.model_name}:{instance.pk}:{uuid.uuid4()}"
    enqueue_ca_event(
        event_type="created" if created else "updated",
        aggregate_type=sender._meta.model_name,
        aggregate_id=instance.pk,
        idempotency_key=event_key,
        payload={
            "plant_id": plant_id,
            "batch_id": getattr(instance, "batch_id", None) or batch_id,
        },
    )


@receiver(pre_save, sender=ChemicalAnalysisProcess, dispatch_uid="ca.capture_process_status")
def capture_chemical_process_status(sender, instance, raw=False, **kwargs):
    if raw or not instance.pk:
        instance._ca_previous_status = None
        return
    instance._ca_previous_status = sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()


@receiver(post_save, sender=ChemicalAnalysisProcess, dispatch_uid="ca.journal_process_status")
def journal_chemical_process_status(sender, instance, created=False, raw=False, **kwargs):
    if raw or not instance.batch_id:
        return
    old_status = getattr(instance, "_ca_previous_status", None)
    if not created and old_status == instance.status:
        return
    request = get_audit_request()
    actor = getattr(request, "user", None) if request else None
    if not getattr(actor, "is_authenticated", False):
        actor = None
    ChemicalWorkflowEvent.objects.create(
        process=instance,
        batch=instance.batch,
        analysis_request=instance.analysis_request if instance.analysis_request_id else None,
        event_type="process_created" if created else "process_status_changed",
        action="process initialized" if created else "status transition",
        from_status=str(old_status or ""),
        to_status=str(instance.status or ""),
        actor=actor,
        details={"process_id": instance.pk},
    )
    broadcast_dashboard_event(
        "chemical_workflow_event",
        {"process_id": instance.pk, "batch_id": instance.batch_id, "status": str(instance.status)},
    )
    if not created and old_status != instance.status:
        ChemicalWorkflowAlert.objects.filter(
            batch_id=instance.batch_id,
            alert_type="ca_workflow_stalled",
            acknowledged_at__isnull=True,
        ).update(acknowledged_at=timezone.now())


@receiver(post_save, sender=ChemicalAnalysisRequest, dispatch_uid="ca.notify_unassigned_analysis_request")
def notify_unassigned_analysis_request_on_save(sender, instance, raw=False, **kwargs):
    if raw or not instance.batch_id:
        return
    if instance.status != ChemicalAnalysisRequest.Status.PENDING or instance.assigned_analysts.exists():
        return
    from ca.services.workflow_notifications import notify_unassigned_analysis_request

    notify_unassigned_analysis_request(instance)


@receiver(m2m_changed, sender=ChemicalAnalysisRequest._meta.get_field("assigned_analysts").remote_field.through, dispatch_uid="ca.journal_analyst_assignment")
def journal_chemical_analyst_assignment(sender, instance, action, reverse, model, pk_set, **kwargs):
    if action not in {"post_add", "post_remove", "post_clear"} or reverse or not instance.batch_id:
        return

    if action == "post_remove" and pk_set:
        ChemicalWorkflowAlert.objects.filter(
            analysis_request=instance,
            alert_type="ca_analysis_assignment",
            recipient_id__in=pk_set,
            acknowledged_at__isnull=True,
        ).update(acknowledged_at=timezone.now())
    elif action == "post_clear":
        ChemicalWorkflowAlert.objects.filter(
            analysis_request=instance,
            alert_type="ca_analysis_assignment",
            acknowledged_at__isnull=True,
        ).update(acknowledged_at=timezone.now())

    if instance.assigned_analysts.exists():
        from ca.services.workflow_notifications import clear_assignment_notifications

        clear_assignment_notifications(instance)
    elif action in {"post_remove", "post_clear"}:
        from ca.services.workflow_notifications import notify_unassigned_analysis_request

        notify_unassigned_analysis_request(instance)

    broadcast_dashboard_event(
        "analyst_assignment_changed",
        {
            "batch_id": instance.batch_id,
            "plant_id": ChemicalBatch.objects.filter(pk=instance.batch_id)
            .values_list("treatment_plant_id", flat=True)
            .first(),
            "analysis_request_id": instance.pk,
        },
    )

    try:
        process = instance.analysis_process
    except ChemicalAnalysisProcess.DoesNotExist:
        process = None
    if not process:
        return
    actor = getattr(instance, "_ca_assignment_actor", None)
    request = get_audit_request()
    if actor is None:
        actor = getattr(request, "user", None) if request else None
    if not getattr(actor, "is_authenticated", False):
        actor = None
    ChemicalWorkflowEvent.objects.create(
        process=process,
        batch=instance.batch,
        analysis_request=instance,
        event_type="analyst_assignment_changed",
        action=action,
        actor=actor,
        from_status=str(process.status or ""),
        to_status=str(process.status or ""),
        details={"analyst_ids": sorted(pk_set or [])},
    )


from .events import batch_status_changed, checklist_completed, request_status_changed


_request_local = local()
_sync_state = local()
_coa_generation_state = local()


def set_audit_request(request):
    _request_local.request = request


def get_audit_request():
    return getattr(_request_local, "request", None)


def clear_audit_request():
    if hasattr(_request_local, "request"):
        delattr(_request_local, "request")


@contextmanager
def audit_request_context(request):
    previous = getattr(_request_local, "request", None)

    if request is None:
        if hasattr(_request_local, "request"):
            delattr(_request_local, "request")
    else:
        _request_local.request = request

    try:
        yield
    finally:
        if previous is None:
            if hasattr(_request_local, "request"):
                delattr(_request_local, "request")
        else:
            _request_local.request = previous


def _sync_guard_active() -> bool:
    return bool(getattr(_sync_state, "active", False))


def with_sync_guard(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if _sync_guard_active():
            return None

        _sync_state.active = True

        try:
            return func(*args, **kwargs)
        finally:
            _sync_state.active = False

    return wrapper


def _coa_generation_active() -> bool:
    return bool(getattr(_coa_generation_state, "active", False))


@contextmanager
def _coa_generation_guard():
    previous = _coa_generation_active()
    _coa_generation_state.active = True

    try:
        yield
    finally:
        _coa_generation_state.active = previous


def _request_meta() -> dict[str, str]:
    request = get_audit_request()

    if request is None:
        return {
            "ip": "",
            "ua": "",
            "device": "",
            "location": "",
        }

    meta = getattr(request, "META", {}) or {}

    return {
        "ip": str(
            meta.get("HTTP_X_FORWARDED_FOR", "")
            .split(",")[0]
            .strip()
            or meta.get("REMOTE_ADDR", "")
            or ""
        ),
        "ua": str(
            meta.get("HTTP_USER_AGENT", "")
            or ""
        ),
        "device": str(
            meta.get("DEVICE", "")
            or ""
        ),
        "location": str(
            meta.get("HTTP_X_GEOLOCATION", "")
            or ""
        ),
    }


def _current_user():
    request = get_audit_request()

    if request is None:
        return None

    user = getattr(request, "user", None)

    if user is None:
        return None

    if not getattr(user, "is_authenticated", False):
        return None

    return user


def _actor_from(performed_by):
    if performed_by is None:
        return None

    try:
        from django.contrib.auth import get_user_model

        user_model = get_user_model()

        if isinstance(performed_by, user_model):
            return performed_by
    except Exception:
        logger.exception(
            "Unable to resolve configured Django user model"
        )

    return None


def _resolve_actor(performed_by=None):
    return _actor_from(performed_by) or _current_user()


def _safe_value(value: Any):
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        return {
            str(key): _safe_value(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [
            _safe_value(item)
            for item in value
        ]

    try:
        return str(value)
    except Exception:
        return repr(value)


def _audit_payload(
    instance,
    additional_data: dict[str, Any] | None = None,
):
    payload = {
        "model": instance.__class__.__name__,
        "id": instance.pk,
    }

    if additional_data:
        payload.update(
            {
                str(key): _safe_value(value)
                for key, value in additional_data.items()
            }
        )

    return payload


def log_generic_audit(
    instance,
    action: str,
    result_status: str = "success",
    description: str | None = None,
    additional_data: dict[str, Any] | None = None,
    user=None,
):
    if instance is None:
        return

    actor = user or _current_user()
    meta = _request_meta()

    if description is None:
        description = (
            f"{instance.__class__.__name__} "
            f"#{instance.pk}: {action}"
        )

    try:
        AuditLogEntry.objects.create(
            user=(
                actor
                if actor
                and getattr(actor, "is_authenticated", False)
                else None
            ),
            action=str(action),
            result_status=str(result_status),
            ip_address=meta["ip"],
            user_agent=meta["ua"],
            device=meta["device"],
            location=meta["location"],
            description=str(description),
            additional_data=_audit_payload(
                instance,
                additional_data,
            ),
        )
    except Exception:
        logger.exception(
            "Audit log creation failed",
            extra={
                "model": instance.__class__.__name__,
                "object_id": instance.pk,
                "action": action,
            },
        )


def _audit_after_commit(
    instance,
    action: str,
    result_status: str = "success",
    description: str | None = None,
    additional_data: dict[str, Any] | None = None,
    user=None,
):
    actor = user or _current_user()

    def callback():
        log_generic_audit(
            instance=instance,
            action=action,
            result_status=result_status,
            description=description,
            additional_data=additional_data,
            user=actor,
        )

    transaction.on_commit(callback)


def broadcast_dashboard_event(
    event_type: str,
    payload: dict[str, Any] | None = None,
):
    payload = payload or {}

    # Keep an at-least-once record for dashboards that may be disconnected when
    # this write commits. Existing websocket consumers still receive the fast
    # best-effort event below.
    try:
        from ca.services.outbox import enqueue_ca_event

        enqueue_ca_event(
            event_type=event_type,
            aggregate_type="chemical_workflow",
            aggregate_id=str(payload.get("batch_id") or payload.get("process_id") or ""),
            payload=payload,
        )
    except Exception:
        logger.exception("Could not persist CA dashboard invalidation")

    data = {
        "event": str(event_type),
        "payload": {
            str(key): _safe_value(value)
            for key, value in payload.items()
        },
    }

    def callback():
        try:
            channel_layer = get_channel_layer()

            if channel_layer is None:
                return

            async_to_sync(
                channel_layer.group_send
            )(
                "dashboard_updates",
                {
                    "type": "dashboard_update",
                    "data": data,
                },
            )
        except Exception:
            logger.exception(
                "Dashboard WebSocket broadcast failed",
                extra={
                    "event_type": event_type,
                },
            )

    transaction.on_commit(callback)


def _chemical_name(instance):
    chemical = getattr(instance, "chemical", None)

    if chemical is None:
        return ""

    return str(
        getattr(chemical, "name", "")
        or chemical
        or ""
    )


def _supplier_name(instance):
    supplier = getattr(instance, "supplier", None)

    if supplier is None:
        return ""

    return str(
        getattr(supplier, "name", "")
        or supplier
        or ""
    )


def _batch_sample_id(batch):
    if batch is None:
        return ""

    return str(
        getattr(batch, "sample_id", "")
        or batch.pk
        or ""
    )


def _generate_coa_assets(instance: ChemicalCoA, *, force=False):
    if not instance.pk:
        return

    if _coa_generation_active():
        return

    try:
        batch = getattr(instance, "batch", None)

        if batch is None:
            raise ValidationError(
                "Cannot generate COA assets without an associated batch."
            )

        sample_id = _batch_sample_id(batch)

        if not sample_id:
            raise ValidationError(
                "Cannot generate COA assets without a batch sample ID."
            )

        qr_exists = bool(
            getattr(instance, "qr_code", None)
            and instance.qr_code.name
        )

        pdf_exists = bool(
            getattr(instance, "pdf_file", None)
            and instance.pdf_file.name
        )

        if qr_exists and pdf_exists and not force:
            return

        with _coa_generation_guard():
            qr_io = None
            generated_pdf_sha256 = None

            if not qr_exists or force:
                qr_data = (
                    f"COA ID: {instance.pk} | "
                    f"Batch: {sample_id} | "
                    f"Verified Hash: "
                    f"{getattr(instance, 'verified_hash', '') or ''}"
                )

                qr_io = generate_qr_code(qr_data)

                if not isinstance(qr_io, BytesIO):
                    qr_io = BytesIO(
                        qr_io.getvalue()
                        if hasattr(qr_io, "getvalue")
                        else bytes(qr_io)
                    )

                qr_content = ContentFile(
                    qr_io.getvalue()
                )

                instance.qr_code.save(
                    f"{sample_id}_qr.png",
                    qr_content,
                    save=False,
                )

            if qr_io is None:
                qr_field = getattr(
                    instance,
                    "qr_code",
                    None,
                )

                if qr_field and qr_field.name:
                    try:
                        with qr_field.open("rb") as qr_file:
                            qr_io = BytesIO(
                                qr_file.read()
                            )
                    except Exception:
                        qr_io = None

            watermark_img = None

            watermark = getattr(
                instance,
                "watermark",
                None,
            )

            if watermark and getattr(watermark, "name", ""):
                try:
                    with watermark.open("rb") as wm_file:
                        with Image.open(wm_file) as wm:
                            watermark_img = wm.convert(
                                "RGBA"
                            )
                except Exception:
                    logger.warning(
                        "Unable to load COA watermark",
                        extra={
                            "coa_id": instance.pk,
                        },
                        exc_info=True,
                    )

            if not pdf_exists or force:
                pdf_io = generate_coa_pdf(
                    instance,
                    qr_image=qr_io,
                    watermark=watermark_img,
                )

                if not isinstance(pdf_io, BytesIO):
                    pdf_io = BytesIO(
                        pdf_io.getvalue()
                        if hasattr(pdf_io, "getvalue")
                        else bytes(pdf_io)
                    )

                instance.pdf_file.save(
                    f"{sample_id}_coa.pdf",
                    ContentFile(
                        pdf_io.getvalue()
                    ),
                    save=False,
                )
                import hashlib

                generated_pdf_sha256 = hashlib.sha256(pdf_io.getvalue()).hexdigest()

            update_fields = []

            if getattr(
                instance,
                "qr_code",
                None,
            ) and instance.qr_code.name:
                update_fields.append("qr_code")

            if getattr(
                instance,
                "pdf_file",
                None,
            ) and instance.pdf_file.name:
                update_fields.append("pdf_file")

            if generated_pdf_sha256:
                instance.pdf_sha256 = generated_pdf_sha256
                update_fields.append("pdf_sha256")

            if update_fields:
                instance._ca_asset_write = True
                try:
                    # CoA generation runs inside the workflow transaction. Keep
                    # this write behind a savepoint so a database constraint
                    # failure can be logged below without poisoning the whole
                    # workflow transaction (which otherwise fails at commit
                    # with PostgreSQL's misleading InFailedSqlTransaction).
                    with transaction.atomic():
                        instance.save(update_fields=update_fields)
                finally:
                    instance.__dict__.pop("_ca_asset_write", None)

        _audit_after_commit(
            instance=instance,
            action="generate_coa_assets",
            result_status="success",
            description=(
                f"COA PDF and QR generated for batch "
                f"{sample_id}"
            ),
            additional_data={
                "coa_id": instance.pk,
                "batch_id": batch.pk,
                "sample_id": sample_id,
                "chemical": getattr(
                    instance,
                    "chemical_name",
                    "",
                ),
                "qr_generated": not qr_exists,
                "pdf_generated": not pdf_exists,
            },
        )

    except Exception as exc:
        logger.exception(
            "COA asset generation failed",
            extra={
                "coa_id": instance.pk,
            },
        )

        batch = getattr(instance, "batch", None)

        _audit_after_commit(
            instance=instance,
            action="generate_coa_assets",
            result_status="failure",
            description=(
                "COA asset generation failed"
            ),
            additional_data={
                "coa_id": instance.pk,
                "batch_id": getattr(
                    batch,
                    "pk",
                    None,
                ),
                "error": str(exc),
            },
        )


@receiver(
    post_save,
    sender=ChemicalCoA,
    dispatch_uid="ca.generate_coa_assets",
)
def generate_coa_assets(
    sender,
    instance: ChemicalCoA,
    created,
    raw=False,
    **kwargs,
):
    if raw:
        return

    if not instance.pk:
        return

    qr_exists = bool(
        getattr(instance, "qr_code", None)
        and instance.qr_code.name
    )

    pdf_exists = bool(
        getattr(instance, "pdf_file", None)
        and instance.pdf_file.name
    )

    if qr_exists and pdf_exists:
        return

    transaction.on_commit(
        lambda: _generate_coa_assets(instance)
    )


@receiver(
    post_save,
    sender=ChemicalBatch,
    dispatch_uid="ca.log_chemical_batch_save",
)
def log_chemical_batch_save(
    sender,
    instance: ChemicalBatch,
    created,
    raw=False,
    **kwargs,
):
    if raw:
        return

    _audit_after_commit(
        instance=instance,
        action=(
            "Created ChemicalBatch"
            if created
            else "Updated ChemicalBatch"
        ),
        additional_data={
            "sample_id": getattr(
                instance,
                "sample_id",
                "",
            ),
            "status": getattr(
                instance,
                "status",
                "",
            ),
        },
    )


@receiver(
    post_save,
    sender=BatchTestResult,
    dispatch_uid="ca.log_batch_test_result_save",
)
def log_test_result_save(
    sender,
    instance: BatchTestResult,
    created,
    raw=False,
    **kwargs,
):
    if raw:
        return

    _audit_after_commit(
        instance=instance,
        action=(
            "Created BatchTestResult"
            if created
            else "Updated BatchTestResult"
        ),
        additional_data={
            "batch_id": getattr(
                instance,
                "batch_id",
                None,
            ),
            "parameter_id": getattr(
                instance,
                "parameter_id",
                None,
            ),
            "status": getattr(
                instance,
                "status",
                "",
            ),
            "value": getattr(
                instance,
                "value",
                None,
            ),
        },
    )

    if not instance.batch_id:
        return
    if instance.status == BatchTestResult.Status.FAIL or instance.quality_flags:
        def notify_quality_exception(result_id=instance.pk, batch_id=instance.batch_id):
            try:
                result = BatchTestResult.objects.select_related("batch", "parameter").get(pk=result_id)
                from .inventory_instruments import trigger_result_quality_alert

                trigger_result_quality_alert(result)
            except Exception:
                logger.exception("Failed to notify reviewers about chemical result %s", result_id)
        transaction.on_commit(notify_quality_exception)
    else:
        ChemicalWorkflowAlert.objects.filter(
            batch_id=instance.batch_id,
            alert_type="ca_quality_exception",
            dedupe_key__startswith=f"quality-result:{instance.pk}:",
            acknowledged_at__isnull=True,
        ).update(acknowledged_at=timezone.now())


@receiver(
    post_save,
    sender=ChemicalLabComparisonResult,
    dispatch_uid="ca.alert_inventory_equipment_for_lab_comparison",
)
def alert_lab_comparison_equipment(sender, instance, created, raw=False, **kwargs):
    if raw or not instance.inventory_equipment_id:
        return

    def notify_equipment_use(result_id=instance.pk):
        try:
            result = ChemicalLabComparisonResult.objects.select_related(
                "batch", "process", "parameter", "inventory_equipment__section__treatment_plant",
                "inventory_equipment__category",
            ).get(pk=result_id)
            from .inventory_instruments import trigger_equipment_alerts

            trigger_equipment_alerts(
                result.inventory_equipment,
                batch=result.batch,
                process=result.process,
                parameter=result.parameter,
                source="interlaboratory_comparison",
            )
        except Exception:
            logger.exception("Unable to evaluate Inventory equipment alerts for comparison result %s", result_id)

    transaction.on_commit(notify_equipment_use)


@receiver(
    post_save,
    sender=ChemicalAnalysisRequest,
    dispatch_uid="ca.log_chemical_analysis_request_save",
)
def log_request_save(
    sender,
    instance: ChemicalAnalysisRequest,
    created,
    raw=False,
    **kwargs,
):
    if raw:
        return

    _audit_after_commit(
        instance=instance,
        action=(
            "Created ChemicalAnalysisRequest"
            if created
            else "Updated ChemicalAnalysisRequest"
        ),
        additional_data={
            "request_id": getattr(
                instance,
                "request_id",
                "",
            ),
            "status": getattr(
                instance,
                "status",
                "",
            ),
            "batch_id": getattr(
                instance,
                "batch_id",
                None,
            ),
        },
    )


@receiver(
    batch_status_changed,
    dispatch_uid="ca.on_batch_status_changed",
)
def on_batch_status_changed(
    sender,
    instance: ChemicalBatch,
    old_status,
    new_status,
    performed_by=None,
    **kwargs,
):
    if old_status == new_status:
        return

    actor = _resolve_actor(performed_by)

    batch_id = instance.pk
    sample_id = getattr(
        instance,
        "sample_id",
        "",
    )

    _audit_after_commit(
        instance=instance,
        action="batch_status_change",
        result_status="success",
        description=(
            f"Batch {sample_id}: "
            f"{old_status} → {new_status}"
        ),
        additional_data={
            "batch_id": batch_id,
            "sample_id": sample_id,
            "old_status": old_status,
            "new_status": new_status,
            "chemical": _chemical_name(instance),
            "supplier": _supplier_name(instance),
        },
        user=actor,
    )

    broadcast_dashboard_event(
        "batch_status_changed",
        {
            "batch_id": batch_id,
            "sample_id": sample_id,
            "old_status": old_status,
            "new_status": new_status,
        },
    )


@receiver(
    request_status_changed,
    dispatch_uid="ca.on_request_status_changed",
)
def on_request_status_changed(
    sender,
    instance: ChemicalAnalysisRequest,
    old_status,
    new_status,
    performed_by=None,
    **kwargs,
):
    if old_status == new_status:
        return

    try:
        process = instance.analysis_process
    except ChemicalAnalysisProcess.DoesNotExist:
        process = None
    if process and instance.batch_id:
        ChemicalWorkflowEvent.objects.create(
            process=process,
            batch=instance.batch,
            analysis_request=instance,
            event_type="analysis_request_status_changed",
            action="request status transition",
            from_status=str(old_status or ""),
            to_status=str(new_status or ""),
            actor=_actor_from(performed_by),
            details={"request_id": instance.pk},
        )
    if new_status in {
        ChemicalAnalysisRequest.Status.COMPLETED,
        ChemicalAnalysisRequest.Status.CANCELLED,
    }:
        ChemicalWorkflowAlert.objects.filter(
            analysis_request=instance,
            acknowledged_at__isnull=True,
        ).update(acknowledged_at=timezone.now())

    actor = _resolve_actor(performed_by)

    batch = getattr(
        instance,
        "batch",
        None,
    )

    request_id = getattr(
        instance,
        "request_id",
        "",
    )

    _audit_after_commit(
        instance=instance,
        action="request_status_change",
        result_status="success",
        description=(
            f"Request {request_id}: "
            f"{old_status} → {new_status}"
        ),
        additional_data={
            "request_id": instance.pk,
            "external_request_id": request_id,
            "old_status": old_status,
            "new_status": new_status,
            "batch_id": getattr(
                instance,
                "batch_id",
                None,
            ),
            "batch_sample_id": (
                _batch_sample_id(batch)
            ),
            "chemical": _chemical_name(instance),
            "supplier": _supplier_name(instance),
        },
        user=actor,
    )

    broadcast_dashboard_event(
        "request_status_changed",
        {
            "request_id": instance.pk,
            "external_request_id": request_id,
            "old_status": old_status,
            "new_status": new_status,
            "batch_id": getattr(
                instance,
                "batch_id",
                None,
            ),
        },
    )


@receiver(
    post_save,
    sender=ChemicalBatch,
    dispatch_uid="ca.sync_batch_to_request",
)
@with_sync_guard
def sync_batch_to_request(
    sender,
    instance: ChemicalBatch,
    created,
    raw=False,
    **kwargs,
):
    if raw:
        return

    request = getattr(
        instance,
        "analysis_request",
        None,
    )

    if request is None:
        return

    return None


@receiver(
    post_save,
    sender=ChemicalAnalysisRequest,
    dispatch_uid="ca.sync_request_to_batch",
)
@with_sync_guard
def sync_request_to_batch(
    sender,
    instance: ChemicalAnalysisRequest,
    created=False,
    raw=False,
    **kwargs,
):
    if raw:
        return

    batch = getattr(
        instance,
        "batch",
        None,
    )

    if batch is None:
        return

    if (
        instance.status
        != ChemicalAnalysisRequest.Status.COMPLETED
    ):
        return

    if (
        batch.status
        == ChemicalBatch.Status.ANALYSED
    ):
        return

    def synchronize():
        try:
            batch.transition_to(
                ChemicalBatch.Status.ANALYSED,
                performed_by=None,
                auto=True,
            )

        except ValidationError as exc:
            logger.warning(
                "Unable to synchronize batch status "
                "from completed analysis request",
                extra={
                    "request_id": instance.pk,
                    "batch_id": batch.pk,
                    "error": str(exc),
                },
            )

        except Exception:
            logger.exception(
                "Unexpected batch synchronization failure",
                extra={
                    "request_id": instance.pk,
                    "batch_id": batch.pk,
                },
            )

    transaction.on_commit(synchronize)


def _is_checklist_complete(
    checklist: ChecklistForWaterTreatmentChemicalAnalysis,
) -> bool:
    required_flags = (
        "manufacturer_name_indicated",
        "chemical_name_on_label",
        "min_mass_indicated",
        "is_oxidizing",
        "is_corrosive",
        "may_cause_fire",
        "contact_with_acids_liberates_toxic_gas",
        "causes_burns",
        "keep_out_of_reach_of_children",
        "contact_with_eyes_action",
        "fire_action_requires_respiratory_equipment",
    )

    for field_name in required_flags:
        if not bool(
            getattr(
                checklist,
                field_name,
                False,
            )
        ):
            return False

    if (
        getattr(
            checklist,
            "grade",
            None,
        )
        == "NA"
    ):
        return False

    manufacture_date = getattr(
        checklist,
        "manufacture_date",
        None,
    )

    expiry_date = getattr(
        checklist,
        "expiry_date",
        None,
    )

    if not manufacture_date or not expiry_date:
        return False

    if expiry_date < manufacture_date:
        return False

    storage_conditions = getattr(
        checklist,
        "storage_conditions",
        None,
    )

    if not storage_conditions:
        return False

    net_weight = getattr(
        checklist,
        "net_weight",
        None,
    )

    if net_weight is None:
        return False

    try:
        if net_weight <= 0:
            return False
    except TypeError:
        return False

    return True


@receiver(
    post_save,
    sender=ChecklistForWaterTreatmentChemicalAnalysis,
    dispatch_uid="ca.auto_advance_request_on_checklist",
)
def auto_advance_request_on_checklist(
    sender,
    instance: ChecklistForWaterTreatmentChemicalAnalysis,
    created=False,
    raw=False,
    **kwargs,
):
    if raw:
        return

    request = getattr(
        instance,
        "chemical_analysis_request",
        None,
    )

    if request is None:
        return

    if (
        request.status
        != ChemicalAnalysisRequest.Status.PENDING
    ):
        return

    # Saving a draft checklist must never start laboratory work. The explicit
    # completion/start actions in ChecklistTaskView own that transition.
    if not instance.completed:
        return

    if not _is_checklist_complete(instance):
        return

    # Checklist completion only records the intake result. The assigned
    # analyst explicitly chooses whether to start analysis in the workflow.
    return


@receiver(
    post_save,
    sender=ChemicalAnalysisRequest,
    dispatch_uid="ca.auto_create_checklist_for_request",
)
def auto_create_checklist_for_request(
    sender,
    instance: ChemicalAnalysisRequest,
    created,
    raw=False,
    **kwargs,
):
    if raw or not created:
        return

    chemical = getattr(
        instance,
        "chemical",
        None,
    )

    if chemical is None:
        logger.error(
            "Cannot automatically create checklist "
            "because analysis request has no chemical",
            extra={
                "request_id": instance.pk,
            },
        )
        return

    def create_checklist():
        try:
            checklist, made = (
                ChecklistForWaterTreatmentChemicalAnalysis.objects
                .get_or_create(
                    chemical_analysis_request=instance,
                    defaults={
                        "chemical": chemical,
                        "grade": "NA",
                        "net_weight": 0,
                        "manufacturer_name_indicated": False,
                        "chemical_name_on_label": False,
                        "min_mass_indicated": False,
                        "is_oxidizing": False,
                        "is_corrosive": False,
                        "may_cause_fire": False,
                        "contact_with_acids_liberates_toxic_gas": False,
                        "causes_burns": False,
                        "keep_out_of_reach_of_children": False,
                        "contact_with_eyes_action": False,
                        "fire_action_requires_respiratory_equipment": False,
                    },
                )
            )

            if made:
                logger.info(
                    "Automatically created chemical analysis checklist",
                    extra={
                        "checklist_id": checklist.pk,
                        "request_id": getattr(
                            instance,
                            "request_id",
                            instance.pk,
                        ),
                    },
                )

        except Exception:
            logger.exception(
                "Failed to automatically create checklist",
                extra={
                    "request_id": getattr(
                        instance,
                        "request_id",
                        instance.pk,
                    ),
                },
            )

    transaction.on_commit(create_checklist)


@receiver(
    post_save,
    sender=ChecklistForWaterTreatmentChemicalAnalysis,
    dispatch_uid="ca.notify_checklist_update",
)
def notify_checklist_update(
    sender,
    instance: ChecklistForWaterTreatmentChemicalAnalysis,
    created=False,
    raw=False,
    **kwargs,
):
    if raw:
        return

    request_id = getattr(
        instance,
        "chemical_analysis_request_id",
        None,
    )

    completed = _is_checklist_complete(
        instance
    )

    broadcast_dashboard_event(
        "checklist_updated",
        {
            "checklist_id": instance.pk,
            "request_id": request_id,
            "completed": completed,
        },
    )
