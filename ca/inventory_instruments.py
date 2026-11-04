"""Plant-scoped laboratory equipment sourced from inventory."""

from __future__ import annotations

import logging
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

UNAVAILABLE_STATUS_TERMS = (
    "retired",
    "decommissioned",
    "inactive",
    "unavailable",
    "out of service",
    "out-of-service",
    "broken",
    "fault",
)


def inventory_equipment_queryset(treatment_plant_id):
    """Return equipment registered in Inventory for one plant's sections."""
    from inventory.models import Equipment

    queryset = Equipment.objects.select_related("section", "section__treatment_plant", "category")
    if not treatment_plant_id:
        return queryset.none()
    # Prefer clearly identified laboratory assets where Inventory categories
    # support that distinction; retain all plant equipment as a compatibility
    # fallback for registers that use generic categories.
    plant_equipment = queryset.filter(section__treatment_plant_id=treatment_plant_id)
    lab_asset = Q()
    for term in ("instrument", "laboratory", "lab", "analyser", "analyzer", "meter", "spectro", "photometer", "titrator", "balance", "probe", "turbid"):
        lab_asset |= Q(category__name__icontains=term) | Q(name__icontains=term)
    if plant_equipment.filter(lab_asset).exists():
        plant_equipment = plant_equipment.filter(lab_asset)
    # Keep unavailable and overdue assets visible so analysts can document
    # what was actually used; model validation flags the result.
    return plant_equipment.order_by("name", "serial_number")


def equipment_is_unavailable(equipment):
    name = (getattr(getattr(equipment, "status", None), "name", "") or "").strip().lower()
    return any(term in name for term in UNAVAILABLE_STATUS_TERMS)


def equipment_calibration_info(equipment, *, today=None):
    today = today or timezone.localdate()
    due_date = getattr(equipment, "next_calibration_date", None)
    if due_date is None:
        return {"status": "MISSING", "due_date": None, "days_to_due": None, "is_current": False}
    days_to_due = (due_date - today).days
    return {
        "status": "EXPIRED" if days_to_due < 0 else "DUE" if days_to_due == 0 else "CURRENT",
        "due_date": due_date,
        "days_to_due": days_to_due,
        "is_current": days_to_due >= 0,
    }


def equipment_maintenance_info(equipment, *, today=None):
    today = today or timezone.localdate()
    due_date = getattr(equipment, "next_maintenance_date", None)
    if due_date is None:
        return {"status": "NOT_RECORDED", "due_date": None, "days_to_due": None}
    days_to_due = (due_date - today).days
    return {
        "status": "OVERDUE" if days_to_due < 0 else "DUE" if days_to_due == 0 else "CURRENT",
        "due_date": due_date,
        "days_to_due": days_to_due,
    }


def equipment_snapshot(equipment):
    if equipment is None:
        return {}
    calibration = equipment_calibration_info(equipment)
    maintenance = equipment_maintenance_info(equipment)
    section = getattr(equipment, "section", None)
    plant = getattr(section, "treatment_plant", None) if section else None
    status = getattr(equipment, "status", None)
    category = getattr(equipment, "category", None)
    return {
        "source": "inventory",
        "equipment_id": equipment.pk,
        "name": equipment.name,
        "serial_number": equipment.serial_number,
        "category": getattr(category, "name", "") if category else "",
        "section_id": getattr(equipment, "section_id", None),
        "section": getattr(section, "name", "") if section else "",
        "treatment_plant_id": getattr(section, "treatment_plant_id", None) if section else None,
        "treatment_plant": getattr(plant, "name", "") if plant else "",
        "operational_status": getattr(status, "name", "Unspecified") if status else "Unspecified",
        "is_available": not equipment_is_unavailable(equipment),
        "calibration_status": calibration["status"],
        "calibration_due_date": calibration["due_date"].isoformat() if calibration["due_date"] else None,
        "days_to_calibration": calibration["days_to_due"],
        "maintenance_status": maintenance["status"],
        "maintenance_due_date": maintenance["due_date"].isoformat() if maintenance["due_date"] else None,
        "days_to_maintenance": maintenance["days_to_due"],
    }


def _alert_context(equipment, *, batch=None, process=None, parameter=None, source="chemical_analysis"):
    snapshot = equipment_snapshot(equipment)
    plant_id = snapshot.get("treatment_plant_id") or getattr(batch, "treatment_plant_id", None)
    context = {
        **snapshot,
        "source": source,
        "plant_id": plant_id,
        "batch_id": getattr(batch, "pk", None),
        "batch": getattr(batch, "sample_id", "") or getattr(batch, "batch_number", ""),
        "chemical": getattr(getattr(batch, "chemical", None), "name", ""),
        "process_id": getattr(process, "pk", None),
        "parameter": getattr(parameter, "parameter_name", ""),
    }
    return context


def trigger_configured_alert(
    *,
    instance,
    alert_type,
    context_data=None,
    batch=None,
    request=None,
    title,
    message,
    url="/ca/quality-control/",
    severity="medium",
    dedupe_key,
    permissions=("ca.review_chemical_analysis",),
):
    """Use AlertConfig/AlertTemplate channels and escalation; retain a CA inbox fallback if unconfigured."""
    from alerts.engine import AlertEngine
    from alerts.models import AlertConfig

    configured = AlertConfig.objects.filter(active=True, alert_type=alert_type).exists()
    engine_failed = False
    try:
        states = AlertEngine.trigger_alert(
            instance,
            alert_type=alert_type,
            audit_notes=f"Chemical analysis event: {alert_type}",
            context_data=context_data or {},
        )
    except Exception:
        logger.exception("Configured alert engine failed for CA event %s", alert_type)
        engine_failed = True
        states = []
    if states or (configured and not engine_failed):
        # A configured rule owns its filter, audience, template, channels,
        # repeat voice policy, and escalation matrix, including an intentional
        # no-match result.
        return states

    if batch is None:
        return []
    from .services.workflow_notifications import create_workflow_alert

    return create_workflow_alert(
        batch=batch,
        request=request,
        alert_type=alert_type,
        title=title,
        message=message,
        url=url,
        severity=severity,
        dedupe_key=dedupe_key,
        permissions=permissions,
    )


def trigger_equipment_alerts(equipment, *, batch, process, parameter, source="chemical_analysis"):
    if equipment is None:
        return []
    context = _alert_context(
        equipment,
        batch=batch,
        process=process,
        parameter=parameter,
        source=source,
    )
    fired = []
    common = {
        "instance": equipment,
        "context_data": context,
        "batch": batch,
        "request": process.analysis_request if process and process.analysis_request_id else None,
        "permissions": ("ca.record_chemical_result", "ca.review_chemical_analysis"),
    }
    if not context["is_available"]:
        fired.extend(trigger_configured_alert(
            **common,
            alert_type="ca_instrument_unavailable",
            title="Laboratory instrument is unavailable",
            message=f"Inventory equipment {equipment.name} ({equipment.serial_number}) is marked {context['operational_status']}. Do not use it for chemical analysis.",
            severity="high",
            dedupe_key=f"ca-instrument-unavailable:{equipment.pk}",
        ))
    calibration = equipment_calibration_info(equipment)
    if calibration["status"] != "CURRENT":
        fired.extend(trigger_configured_alert(
            **common,
            alert_type="verification_pending",
            title="Laboratory instrument calibration needs attention",
            message=(
                f"Inventory equipment {equipment.name} ({equipment.serial_number}) has calibration status "
                f"{calibration['status'].lower()}; due date {calibration['due_date'] or 'not recorded'}. "
                f"It was selected for {parameter.parameter_name if parameter else 'chemical analysis'} at {batch.treatment_plant}."
            ),
            severity="high",
            dedupe_key=f"ca-instrument-calibration:{equipment.pk}:{calibration['status']}",
        ))
    maintenance = equipment_maintenance_info(equipment)
    if maintenance["status"] in {"DUE", "OVERDUE"}:
        fired.extend(trigger_configured_alert(
            **common,
            alert_type="maintenance_due",
            title="Laboratory equipment maintenance is due",
            message=(
                f"Inventory equipment {equipment.name} ({equipment.serial_number}) has maintenance status "
                f"{maintenance['status'].lower()} (due {maintenance['due_date']}). Review its readiness before further use."
            ),
            severity="high" if maintenance["status"] == "OVERDUE" else "medium",
            dedupe_key=f"ca-instrument-maintenance:{equipment.pk}:{maintenance['status']}",
        ))
    return fired


def trigger_result_quality_alert(result):
    from .models import ChemicalAnalysisProcess

    batch = result.batch
    process = ChemicalAnalysisProcess.objects.filter(batch=batch).order_by("-pk").first()
    request = process.analysis_request if process and process.analysis_request_id else None
    context = {
        "source": "chemical_analysis_result",
        "batch_id": batch.pk,
        "batch": batch.sample_id or batch.batch_number,
        "chemical": batch.chemical.name if batch.chemical_id else "",
        "plant_id": batch.treatment_plant_id,
        "parameter": result.parameter.parameter_name if result.parameter_id else "",
        "result": str(result.value) if result.value is not None else result.textual_result,
        "status": result.status,
        "quality_flags": ",".join(result.quality_flags or []),
        "analyst": str(result.analyst) if result.analyst_id else "",
        "inventory_equipment": (result.instrument_snapshot or {}).get("name", ""),
        "equipment_serial_number": (result.instrument_snapshot or {}).get("serial_number", ""),
        "calibration_status": (result.instrument_snapshot or {}).get("calibration_status", ""),
        "operational_status": (result.instrument_snapshot or {}).get("operational_status", ""),
    }
    events = []
    flags = set(result.quality_flags or [])
    equipment_or_qc_failure = any(
        flag in {"INSTRUMENT_UNCALIBRATED", "INSTRUMENT_UNAVAILABLE"}
        or str(flag).startswith("QC_FAIL_")
        for flag in flags
    )
    is_oos = result.status == "FAIL" and (
        bool(flags & {"OUT_OF_SPEC_LOW", "OUT_OF_SPEC_HIGH"})
        or not equipment_or_qc_failure
    )
    if is_oos:
        events.extend(trigger_configured_alert(
            instance=result,
            alert_type="ca_oos_result",
            context_data=context,
            batch=batch,
            request=request,
            title="Chemical analysis result is out of specification",
            message=f"{context['chemical']} batch {context['batch']}: {context['parameter']} failed with result {context['result']}. Review the OOS investigation and disposition.",
            severity="high",
            dedupe_key=f"ca-oos-result:{result.pk}",
            permissions=("ca.review_chemical_analysis",),
        ))
    if flags & {"INSTRUMENT_UNCALIBRATED", "INSTRUMENT_UNAVAILABLE", "INSTRUMENT_MAINTENANCE_DUE"}:
        equipment = getattr(result, "inventory_equipment", None)
        if equipment:
            events.extend(trigger_equipment_alerts(
                equipment,
                batch=batch,
                process=process,
                parameter=result.parameter,
                source="chemical_analysis_result",
            ))
    return events


def trigger_qc_alert(qc_result, *, alert_type, title, message, severity="medium", context_data=None):
    batch = qc_result.batch
    process = qc_result.process
    context = {
        "source": "chemical_method_qc",
        "plant_id": batch.treatment_plant_id,
        "batch_id": batch.pk,
        "batch": batch.sample_id or batch.batch_number,
        "chemical": batch.chemical.name if batch.chemical_id else "",
        "parameter": qc_result.parameter.parameter_name,
        "control": qc_result.control.code,
        "control_name": qc_result.control.label,
        "method": qc_result.control.plan.method.method,
        "method_version": qc_result.control.plan.method.version,
        "qc_status": qc_result.status,
        "observed_value": str(qc_result.observed_value),
        "evaluated_value": str(qc_result.evaluated_value),
        "analyst": str(qc_result.analyst) if qc_result.analyst_id else "",
        **(context_data or {}),
    }
    if qc_result.inventory_equipment_id:
        snapshot = equipment_snapshot(qc_result.inventory_equipment)
        context.update({
            "inventory_equipment": snapshot.get("name", ""),
            "equipment_serial_number": snapshot.get("serial_number", ""),
            "calibration_status": snapshot.get("calibration_status", ""),
        })
    if qc_result.inventory_equipment_id:
        trigger_equipment_alerts(
            qc_result.inventory_equipment,
            batch=batch,
            process=process,
            parameter=qc_result.parameter,
            source="chemical_method_qc",
        )
    return trigger_configured_alert(
        instance=qc_result,
        alert_type=alert_type,
        context_data=context,
        batch=batch,
        request=process.analysis_request if process.analysis_request_id else None,
        title=title,
        message=message,
        severity=severity,
        dedupe_key=f"{alert_type}:{qc_result.pk}",
        permissions=("ca.review_chemical_analysis",),
    )
