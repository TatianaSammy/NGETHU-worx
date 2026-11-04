from __future__ import annotations

import logging
import re
from typing import Any, Dict

from django.apps import apps
from django.db.models import QuerySet

from core.registry import register_model

logger = logging.getLogger(__name__)

APP_LABEL = "sc"


# ==========================================================
# SAFE MODEL LOADER
# ==========================================================

def get_model(name: str):
    return apps.get_model(APP_LABEL, name)


# ==========================================================
# MODEL REFERENCES
# ==========================================================

AuditLog = get_model("AuditLog")

Shift = get_model("Shift")
ShiftSectionRequirement = get_model("ShiftSectionRequirement")
ShiftSupervisor = get_model("ShiftSupervisor")
ShiftAssignment = get_model("ShiftAssignment")
MonthlyDutyRoster = get_model("MonthlyDutyRoster")
ShiftHandover = get_model("ShiftHandover")

Apparatus = get_model("Apparatus")
ApparatusCondition = get_model("ApparatusCondition")


# ==========================================================
# ICONS
# ==========================================================

ICONS = {
    "AuditLog": "clipboard-list",

    "Shift": "clock",
    "ShiftSectionRequirement": "users",

    "ShiftSupervisor": "user-shield",
    "ShiftAssignment": "calendar-check",

    "MonthlyDutyRoster": "calendar-days",
    "ShiftHandover": "handshake",

    "Apparatus": "gears",
    "ApparatusCondition": "screwdriver-wrench",
}


# ==========================================================
# GROUPS
# ==========================================================

GROUPS = {
    "AuditLog": "audit",

    "Shift": "shifts",
    "ShiftSectionRequirement": "shifts",
    "ShiftSupervisor": "shifts",
    "ShiftAssignment": "shifts",

    "MonthlyDutyRoster": "rosters",

    "ShiftHandover": "handovers",

    "Apparatus": "equipment",
    "ApparatusCondition": "equipment",
}


# ==========================================================
# META
# ==========================================================

def humanize(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", name)


def build_meta(model) -> Dict[str, Any]:
    verbose = humanize(model.__name__)

    return {
        "key": f"{model._meta.app_label}.{model._meta.model_name}",
        "app_label": model._meta.app_label,
        "model_name": model._meta.model_name,
        "verbose_name": verbose,
        "verbose_name_plural": f"{verbose}s",
    }


# ==========================================================
# QUERYSET FACTORIES
# ==========================================================

def audit_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "user",
            "content_type",
        )
    )


def shift_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related("treatment_plant")
        .prefetch_related("section_requirements")
    )


def requirement_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "shift",
            "section",
        )
    )


def supervisor_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "user",
            "treatment_plant",
        )
    )


def assignment_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "staff",
            "shift",
            "duty_roster",
            "supervisor",
            "handover_to",
            "temp_supervisor",
            "reassigned_from",
        )
    )


def roster_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "treatment_plant",
            "created_by",
        )
        .prefetch_related(
            "assignments",
        )
    )


def handover_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "shift",
            "outgoing_supervisor",
            "incoming_supervisor",
        )
        .prefetch_related(
            "apparatus_conditions",
        )
    )


def apparatus_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "equipment",
        )
    )


def apparatus_condition_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "apparatus",
            "apparatus__equipment",
            "handover",
        )
    )


# ==========================================================
# REGISTRATION WRAPPER
# ==========================================================

def safe_register(model, **kwargs):

    if model is None:
        raise RuntimeError("Model resolution failed.")

    meta = kwargs.pop("meta", {})
    meta.update(build_meta(model))

    kwargs["meta"] = meta

    ui = kwargs.pop("ui", {})

    ui.setdefault(
        "icon",
        ICONS.get(model.__name__, "cube"),
    )

    ui.setdefault(
        "group",
        GROUPS.get(model.__name__, "general"),
    )

    kwargs["ui"] = ui

    kwargs.pop("templates", None)

    try:
        return register_model(**kwargs)(model)

    except Exception:
        logger.exception(
            "[sc.registry] Failed registering %s",
            model.__name__,
        )
        raise


# ==========================================================
# AUDIT
# ==========================================================

safe_register(
    AuditLog,
    search_fields=[
        "object_repr",
        "object_id",
        "action",
    ],
    filter_fields=[
        "action",
        "content_type",
    ],
    ordering=[
        "-timestamp",
    ],
    queryset_factory=audit_qs,
)


# ==========================================================
# SHIFTS
# ==========================================================

safe_register(
    Shift,
    search_fields=[
        "name",
        "treatment_plant__name",
    ],
    filter_fields=[
        "treatment_plant",
        "is_deleted",
    ],
    ordering=[
        "treatment_plant",
        "start_time",
    ],
    queryset_factory=shift_qs,
    inline_edit_fields=[
        "name",
        "start_time",
        "end_time",
        "handover_time",
        "takeover_time",
    ],
)


safe_register(
    ShiftSectionRequirement,
    search_fields=[
        "shift__name",
        "section__name",
    ],
    filter_fields=[
        "shift",
        "section",
    ],
    ordering=[
        "shift",
        "section",
    ],
    queryset_factory=requirement_qs,
)


safe_register(
    ShiftSupervisor,
    search_fields=[
        "user__username",
        "user__first_name",
        "user__last_name",
    ],
    filter_fields=[
        "treatment_plant",
        "is_active",
    ],
    ordering=[
        "treatment_plant",
    ],
    queryset_factory=supervisor_qs,
)


safe_register(
    ShiftAssignment,
    search_fields=[
        "staff__username",
        "staff__first_name",
        "staff__last_name",
        "shift__name",
    ],
    filter_fields=[
        "date",
        "attendance_status",
        "day_type",
    ],
    ordering=[
        "date",
        "shift",
    ],
    queryset_factory=assignment_qs,
    bulk_actions={
        "mark_present": True,
        "mark_absent": True,
        "mark_excused": True,
        "reassign": True,
    },
)


safe_register(
    MonthlyDutyRoster,
    search_fields=[
        "month",
        "year",
        "treatment_plant__name",
    ],
    filter_fields=[
        "month",
        "year",
        "treatment_plant",
    ],
    ordering=[
        "-year",
        "-month",
    ],
    queryset_factory=roster_qs,
    bulk_actions={
        "generate_assignments": True,
        "regenerate": True,
    },
)


safe_register(
    ShiftHandover,
    search_fields=[
        "shift__name",
        "handover_notes",
        "pending_work",
    ],
    filter_fields=[
        "date",
        "is_taken_over",
    ],
    ordering=[
        "-date",
    ],
    queryset_factory=handover_qs,
)


# ==========================================================
# EQUIPMENT
# ==========================================================

safe_register(
    Apparatus,
    search_fields=[
        "name",
        "equipment__name",
    ],
    filter_fields=[
        "equipment",
    ],
    ordering=[
        "name",
    ],
    queryset_factory=apparatus_qs,
)


safe_register(
    ApparatusCondition,
    search_fields=[
        "apparatus__name",
        "status",
        "remarks",
    ],
    filter_fields=[
        "status",
        "handover",
    ],
    ordering=[
        "-id",
    ],
    queryset_factory=apparatus_condition_qs,
)


# ==========================================================
# DEBUG
# ==========================================================

try:
    from core.registry import registry

    logger.info(
        "[sc.registry] Registered %d models for %s",
        len(registry._registry),
        APP_LABEL,
    )

except Exception:
    logger.exception(
        "[sc.registry] Registry unavailable during import"
    )
