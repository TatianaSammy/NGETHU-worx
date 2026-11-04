
from __future__ import annotations

import logging
import re
from copy import deepcopy
from typing import Any, Dict, Iterable

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.db.models import QuerySet

from core.registry import register_model

logger = logging.getLogger(__name__)

APP_LABEL = "ca"


def get_model(name: str):
    try:
        return apps.get_model(APP_LABEL, name)
    except LookupError:
        logger.exception(
            "[ca.registry] Unable to resolve model %s.%s",
            APP_LABEL,
            name,
        )
        return None


Chemical = get_model("Chemical")
Supplier = get_model("Supplier")
ChemicalLocation = get_model("ChemicalLocation")
ChemicalSpecification = get_model("ChemicalSpecification")
ChemicalBatch = get_model("ChemicalBatch")
ChemicalAnalysisRequest = get_model("ChemicalAnalysisRequest")
ChemicalAnalysisProcess = get_model("ChemicalAnalysisProcess")
BatchTestResult = get_model("BatchTestResult")
ChemicalAnalysisInput = get_model("ChemicalAnalysisInput")
ChemicalCoA = get_model("ChemicalCoA")
PackagingStatus = get_model("PackagingStatus")
ChemicalCertApprover = get_model("ChemicalCertApprover")
ChecklistForWaterTreatmentChemicalAnalysis = get_model(
    "ChecklistForWaterTreatmentChemicalAnalysis"
)


ICONS = {
    "Chemical": "flask",
    "Supplier": "truck",
    "ChemicalLocation": "location-dot",
    "ChemicalSpecification": "vial-circle-check",
    "ChemicalBatch": "boxes-stacked",
    "ChemicalAnalysisRequest": "clipboard-list",
    "ChemicalAnalysisProcess": "diagram-project",
    "BatchTestResult": "microscope",
    "ChemicalAnalysisInput": "keyboard",
    "ChemicalCoA": "certificate",
    "PackagingStatus": "box",
    "ChemicalCertApprover": "signature",
    "ChecklistForWaterTreatmentChemicalAnalysis": "clipboard-check",
}


GROUPS = {
    "Chemical": "chemicals",
    "Supplier": "suppliers",
    "ChemicalLocation": "inventory",
    "ChemicalSpecification": "specifications",
    "ChemicalBatch": "batches",
    "ChemicalAnalysisRequest": "analysis",
    "ChemicalAnalysisProcess": "analysis",
    "BatchTestResult": "results",
    "ChemicalAnalysisInput": "analysis",
    "ChemicalCoA": "certificates",
    "PackagingStatus": "logistics",
    "ChemicalCertApprover": "compliance",
    "ChecklistForWaterTreatmentChemicalAnalysis": "compliance",
}


def humanize_model_name(model) -> str:
    return re.sub(
        r"(?<!^)(?=[A-Z])",
        " ",
        model.__name__,
    ).strip()


def build_meta(model) -> Dict[str, Any]:
    verbose = humanize_model_name(model)

    return {
        "key": f"{model._meta.app_label}.{model._meta.model_name}",
        "app_label": model._meta.app_label,
        "model_name": model._meta.model_name,
        "verbose_name": model._meta.verbose_name
        or verbose,
        "verbose_name_plural": model._meta.verbose_name_plural
        or f"{verbose}s",
    }


def chemical_qs(model) -> QuerySet:
    return (
        model.objects
        .prefetch_related(
            "specifications",
        )
        .order_by("name")
    )


def supplier_qs(model) -> QuerySet:
    return model.objects.order_by("name")


def location_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "treatment_plant",
        )
        .order_by(
            "treatment_plant__name",
            "name",
        )
    )


def specification_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "chemical",
            "method",
            "treatment_plant",
        )
        .prefetch_related(
            "remarks",
        )
        .order_by(
            "chemical__name",
            "parameter_name",
            "-version",
        )
    )


def batch_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "chemical",
            "supplier",
            "treatment_plant",
            "location",
            "received_by",
        )
        .prefetch_related(
            "test_results",
            "test_results__parameter",
            "test_results__analyst",
        )
        .order_by(
            "-received_date",
            "-created_at",
        )
    )


def request_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "batch",
            "batch__chemical",
            "batch__supplier",
            "batch__treatment_plant",
            "batch__location",
            "requested_by",
            "received_by",
        )
        .prefetch_related(
            "assigned_analysts",
        )
        .order_by(
            "-created_at",
        )
    )


def process_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "batch",
            "batch__chemical",
            "batch__supplier",
            "batch__treatment_plant",
            "analysis_request",
            "initiated_by",
            "assigned_analyst",
            "qa_officer",
            "approver",
        )
        .prefetch_related(
            "batch__test_results",
            "batch__test_results__parameter",
            "batch__test_results__analyst",
        )
        .order_by(
            "-created_at",
        )
    )


def result_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "batch",
            "batch__chemical",
            "batch__treatment_plant",
            "parameter",
            "parameter__chemical",
            "parameter__method",
            "analyst",
        )
        .order_by(
            "-tested_at",
            "parameter__parameter_name",
        )
    )


def input_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "test_result",
            "test_result__batch",
            "test_result__batch__chemical",
            "test_result__parameter",
            "test_result__parameter__method",
        )
        .order_by(
            "-id",
        )
    )


def coa_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "batch",
            "batch__chemical",
            "batch__supplier",
            "batch__treatment_plant",
            "approved_by",
            "packaging_status",
        )
        .order_by(
            "-date_of_report",
        )
    )


def approver_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "user",
        )
        .order_by(
            "user__username",
        )
    )


def checklist_qs(model) -> QuerySet:
    return (
        model.objects
        .select_related(
            "chemical_analysis_request",
            "chemical_analysis_request__batch",
            "chemical_analysis_request__batch__treatment_plant",
            "chemical",
            "sampled_by",
            "requested_by",
        )
        .order_by(
            "-created_at",
        )
    )


LOOKUP_BASE_CONFIG = {
    "ordering": [
        "name",
    ],
}


BATCH_BASE_CONFIG = {
    "search_fields": [
        "sample_id",
        "batch_number",
        "delivery_note_number",
        "chemical__name",
        "chemical__code",
        "supplier__name",
    ],
    "filter_fields": [
        "status",
        "chemical",
        "supplier",
        "treatment_plant",
    ],
    "ordering": [
        "-received_date",
        "-created_at",
    ],
    "select_related": [
        "chemical",
        "supplier",
        "treatment_plant",
        "received_by",
    ],
    "prefetch_related": [
        "test_results",
    ],
    "queryset_factory": batch_qs,
}


ANALYSIS_REQUEST_BASE_CONFIG = {
    "search_fields": [
        "request_id",
        "batch__sample_id",
        "batch__batch_number",
        "batch__chemical__name",
    ],
    "filter_fields": [
        "status",
        "batch",
        "batch__location",
    ],
    "ordering": [
        "-created_at",
    ],
    "select_related": [
        "batch",
        "batch__location",
        "requested_by",
        "received_by",
    ],
    "prefetch_related": [
        "assigned_analysts",
    ],
    "queryset_factory": request_qs,
}


ANALYSIS_PROCESS_BASE_CONFIG = {
    "search_fields": [
        "batch__sample_id",
        "batch__batch_number",
        "batch__chemical__name",
        "assigned_analyst__username",
    ],
    "filter_fields": [
        "status",
        "batch",
        "assigned_analyst",
        "qa_officer",
        "approver",
    ],
    "ordering": [
        "-created_at",
    ],
    "select_related": [
        "batch",
        "analysis_request",
        "initiated_by",
        "assigned_analyst",
        "qa_officer",
        "approver",
    ],
    "queryset_factory": process_qs,
}


TEST_RESULT_BASE_CONFIG = {
    "search_fields": [
        "batch__sample_id",
        "batch__batch_number",
        "parameter__parameter_name",
        "parameter__chemical__name",
        "analyst__username",
    ],
    "filter_fields": [
        "status",
        "batch",
        "parameter",
        "analyst",
    ],
    "ordering": [
        "-tested_at",
    ],
    "select_related": [
        "batch",
        "parameter",
        "parameter__chemical",
        "parameter__method",
        "analyst",
    ],
    "queryset_factory": result_qs,
}


CERTIFICATE_BASE_CONFIG = {
    "search_fields": [
        "batch__sample_id",
        "batch__batch_number",
        "chemical_name",
        "supplier_name",
        "delivery_note_number",
    ],
    "filter_fields": [
        "packaging_status",
        "batch",
        "approved_by",
    ],
    "ordering": [
        "-date_of_report",
    ],
    "select_related": [
        "batch",
        "approved_by",
        "packaging_status",
    ],
    "queryset_factory": coa_qs,
}


CHECKLIST_BASE_CONFIG = {
    "search_fields": [
        "chemical__name",
        "chemical__code",
        "chemical_analysis_request__request_id",
        "chemical_analysis_request__batch__sample_id",
        "chemical_analysis_request__batch__batch_number",
    ],
    "filter_fields": [
        "grade",
        "country_of_origin",
        "chemical",
        "chemical_analysis_request",
        "completed",
    ],
    "ordering": [
        "-created_at",
    ],
    "select_related": [
        "chemical_analysis_request",
        "chemical",
        "sampled_by",
        "requested_by",
    ],
    "queryset_factory": checklist_qs,
}


MODEL_CAPABILITIES = {
    "Chemical": {
        "create": True,
        "update": True,
        "delete": True,
        "search": True,
        "filter": True,
        "export": True,
        "bulk_edit": True,
    },
    "Supplier": {
        "create": True,
        "update": True,
        "delete": True,
        "search": True,
        "filter": True,
        "export": True,
        "bulk_edit": True,
    },
    "ChemicalLocation": {
        "create": True,
        "update": True,
        "delete": True,
        "search": True,
        "filter": True,
        "export": True,
        "bulk_edit": True,
    },
    "ChemicalSpecification": {
        "create": True,
        "update": True,
        "delete": True,
        "search": True,
        "filter": True,
        "export": True,
        "bulk_edit": True,
    },
    "ChemicalBatch": {
        "create": True,
        "update": True,
        "delete": False,
        "search": True,
        "filter": True,
        "export": True,
        "bulk_edit": True,
        "workflow": True,
    },
    "ChemicalAnalysisRequest": {
        "create": True,
        "update": True,
        "delete": False,
        "search": True,
        "filter": True,
        "export": True,
        "bulk_edit": True,
        "workflow": True,
        "assignment": True,
    },
    "ChemicalAnalysisProcess": {
        "create": False,
        "update": True,
        "delete": False,
        "search": True,
        "filter": True,
        "export": True,
        "bulk_edit": False,
        "workflow": True,
        "assignment": True,
        "qa": True,
        "approval": True,
    },
    "BatchTestResult": {
        "create": True,
        "update": True,
        "delete": False,
        "search": True,
        "filter": True,
        "export": True,
        "bulk_edit": True,
    },
    "ChemicalAnalysisInput": {
        "create": True,
        "update": True,
        "delete": False,
        "search": True,
        "filter": True,
        "export": True,
        "bulk_edit": True,
        "calculation": True,
    },
    "ChemicalCoA": {
        "create": False,
        "update": False,
        "delete": False,
        "search": True,
        "filter": True,
        "export": True,
        "bulk_edit": False,
        "workflow": True,
        "document": True,
        "verification": True,
        "hash_verification": True,
    },
    "PackagingStatus": {
        "create": True,
        "update": True,
        "delete": True,
        "search": True,
        "filter": True,
        "export": True,
        "bulk_edit": True,
    },
    "ChemicalCertApprover": {
        "create": True,
        "update": True,
        "delete": True,
        "search": True,
        "filter": True,
        "export": True,
        "bulk_edit": True,
    },
    "ChecklistForWaterTreatmentChemicalAnalysis": {
        "create": True,
        "update": True,
        "delete": False,
        "search": True,
        "filter": True,
        "export": True,
        "bulk_edit": True,
        "workflow": True,
    },
}


PERMISSIONS = {
    "Chemical": {
        "view": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
            "QA Officer",
        ],
        "create": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
        ],
        "update": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
        ],
        "delete": [
            "Lab Supervisor",
            "QA Officer",
        ],
    },
    "Supplier": {
        "view": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
            "QA Officer",
        ],
        "create": [
            "Lab Supervisor",
            "QA Officer",
        ],
        "update": [
            "Lab Supervisor",
            "QA Officer",
        ],
        "delete": [
            "Lab Supervisor",
        ],
    },
    "ChemicalLocation": {
        "view": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
            "QA Officer",
        ],
        "create": [
            "Lab Supervisor",
        ],
        "update": [
            "Lab Supervisor",
        ],
        "delete": [
            "Lab Supervisor",
        ],
    },
    "ChemicalSpecification": {
        "view": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
            "QA Officer",
        ],
        "create": [
            "Lab Supervisor",
            "QA Officer",
        ],
        "update": [
            "Lab Supervisor",
            "QA Officer",
        ],
        "delete": [
            "QA Officer",
        ],
    },
    "ChemicalBatch": {
        "view": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
            "QA Officer",
        ],
        "create": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
        ],
        "update": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
        ],
        "approve": [
            "Lab Supervisor",
            "QA Officer",
        ],
        "reject": [
            "Lab Supervisor",
            "QA Officer",
        ],
        "utilize": [
            "Operations",
            "Lab Supervisor",
        ],
        "return": [
            "Lab Supervisor",
            "Operations",
        ],
    },
    "ChemicalAnalysisRequest": {
        "view": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
            "QA Officer",
        ],
        "create": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
        ],
        "update": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
        ],
        "start_analysis": [
            "Lab Technician",
            "Lab Assistant",
        ],
        "complete_analysis": [
            "Lab Supervisor",
        ],
        "assign_analyst": [
            "Lab Supervisor",
            "Lab Technician",
        ],
        "cancel": [
            "Lab Supervisor",
            "QA Officer",
        ],
    },
    "ChemicalAnalysisProcess": {
        "view": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
            "QA Officer",
        ],
        "update": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
        ],
        "start_analysis": [
            "Lab Technician",
            "Lab Assistant",
        ],
        "complete_analysis": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
        ],
        "approve": [
            "Lab Supervisor",
            "QA Officer",
        ],
        "reject": [
            "Lab Supervisor",
            "QA Officer",
        ],
        "utilize": [
            "Operations",
            "Lab Supervisor",
        ],
        "return": [
            "Operations",
            "Lab Supervisor",
        ],
        "complete": [
            "Lab Supervisor",
            "QA Officer",
        ],
    },
    "BatchTestResult": {
        "view": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
            "QA Officer",
        ],
        "create": [
            "Lab Technician",
            "Lab Assistant",
        ],
        "update": [
            "Lab Technician",
            "Lab Assistant",
        ],
        "verify": [
            "Lab Supervisor",
        ],
        "reject": [
            "Lab Supervisor",
            "QA Officer",
        ],
    },
    "ChemicalAnalysisInput": {
        "view": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
            "QA Officer",
        ],
        "create": [
            "Lab Technician",
            "Lab Assistant",
        ],
        "update": [
            "Lab Technician",
            "Lab Assistant",
        ],
        "calculate": [
            "Lab Technician",
            "Lab Assistant",
        ],
    },
    "ChemicalCoA": {
        "view": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
            "QA Officer",
        ],
        "generate": [
            "Lab Supervisor",
            "QA Officer",
        ],
        "verify": [
            "QA Officer",
        ],
        "approve": [
            "ChemicalCertApprover",
            "QA Officer",
        ],
        "download": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
            "QA Officer",
        ],
        "revoke": [
            "QA Officer",
            "ChemicalCertApprover",
        ],
    },
    "PackagingStatus": {
        "view": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
            "QA Officer",
        ],
        "create": [
            "Lab Supervisor",
        ],
        "update": [
            "Lab Supervisor",
        ],
        "delete": [
            "Lab Supervisor",
        ],
    },
    "ChemicalCertApprover": {
        "view": [
            "Lab Supervisor",
            "QA Officer",
        ],
        "create": [
            "QA Officer",
        ],
        "update": [
            "QA Officer",
        ],
        "delete": [
            "QA Officer",
        ],
    },
    "ChecklistForWaterTreatmentChemicalAnalysis": {
        "view": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
            "QA Officer",
        ],
        "create": [
            "Lab Technician",
            "Lab Assistant",
        ],
        "update": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
        ],
        "complete": [
            "Lab Technician",
            "Lab Assistant",
            "Lab Supervisor",
        ],
        "verify": [
            "Lab Supervisor",
            "QA Officer",
        ],
        "reopen": [
            "Lab Supervisor",
            "QA Officer",
        ],
    },
}


WORKFLOW_CONFIG = {
    "ChemicalBatch": {
        "enabled": True,
        "state_field": "status",
        "states": [
            "received",
            "analysed",
            "approved",
            "rejected",
            "utilized",
            "returned",
        ],
        "transitions": {
            "received": [
                "analysed",
                "rejected",
            ],
            "analysed": [
                "approved",
                "rejected",
            ],
            "approved": [
                "utilized",
                "returned",
            ],
            "rejected": [],
            "utilized": [],
            "returned": [],
        },
        "actions": [
            "approve",
            "reject",
            "utilize",
            "return",
        ],
    },
    "ChemicalAnalysisRequest": {
        "enabled": True,
        "state_field": "status",
        "states": [
            "pending",
            "in_progress",
            "completed",
        ],
        "transitions": {
            "pending": [
                "in_progress",
            ],
            "in_progress": [
                "completed",
            ],
            "completed": [],
        },
        "actions": [
            "start_analysis",
            "complete_analysis",
        ],
        "assignment": {
            "manual": True,
            "automatic": True,
            "timeout_minutes": 0,
            "immediate": True,
            "reassignment_allowed": True,
            "field": "assigned_analysts",
            "due_field": "assignment_due_at",
            "manual_timestamp_field": "manual_assigned_at",
            "automatic_timestamp_field": "auto_assigned_at",
        },
    },
    "ChemicalAnalysisProcess": {
        "enabled": True,
        "state_field": "status",
        "states": [
            "created",
            "received",
            "requested",
            "assigned",
            "checklist",
            "analysis",
            "retest",
            "qa",
            "approved",
            "rejected",
            "utilized",
            "returned",
            "completed",
        ],
        "transitions": {
            "created": [
                "received",
            ],
            "received": [
                "requested",
            ],
            "requested": [
                "assigned",
                "checklist",
            ],
            "assigned": [
                "checklist",
            ],
            "checklist": [
                "analysis",
            ],
            "analysis": [
                "retest",
                "qa",
            ],
            "retest": [
                "analysis",
                "qa",
            ],
            "qa": [
                "approved",
                "rejected",
            ],
            "approved": [
                "utilized",
                "returned",
            ],
            "rejected": [],
            "utilized": [
                "completed",
            ],
            "returned": [
                "completed",
            ],
            "completed": [],
        },
        "actions": [
            "move_to_analysis",
            "mark_analysis_started",
            "mark_analysis_completed",
            "mark_approved",
            "mark_rejected",
            "mark_disposition_completed",
            "mark_completed",
        ],
        "assignment": {
            "field": "assigned_analyst",
            "manual": True,
            "automatic": True,
            "timeout_minutes": 0,
            "immediate": True,
            "reassignment_allowed": True,
        },
        "sampling": {
            "enabled": False,
        },
    },
    "BatchTestResult": {
        "enabled": False,
        "state_field": "status",
        "states": [
            "pass",
            "fail",
            "na",
        ],
        "transitions": {},
        "actions": [],
    },
    "ChemicalCoA": {
        "enabled": True,
        "state_field": None,
        "derived_states": [
            "not_generated",
            "generated",
            "approved",
            "revoked",
        ],
        "transitions": {
            "not_generated": [
                "generated",
            ],
            "generated": [
                "approved",
                "revoked",
            ],
            "approved": [
                "revoked",
            ],
            "revoked": [
                "generated",
            ],
        },
        "actions": [
            "generate",
            "verify",
            "approve",
            "download",
            "revoke",
        ],
    },
    "ChecklistForWaterTreatmentChemicalAnalysis": {
        "enabled": True,
        "state_field": "completed",
        "states": [
            "pending",
            "completed",
        ],
        "transitions": {
            "pending": [
                "completed",
            ],
            "completed": [
                "pending",
            ],
        },
        "actions": [
            "complete",
            "reopen",
        ],
        "sampling": {
            "enabled": False,
        },
    },
}


AUDIT_CONFIG = {
    "Chemical": {
        "enabled": True,
        "track_create": True,
        "track_update": True,
        "track_delete": True,
    },
    "Supplier": {
        "enabled": True,
        "track_create": True,
        "track_update": True,
        "track_delete": True,
    },
    "ChemicalLocation": {
        "enabled": True,
        "track_create": True,
        "track_update": True,
        "track_delete": True,
    },
    "ChemicalSpecification": {
        "enabled": True,
        "track_create": True,
        "track_update": True,
        "track_delete": True,
    },
    "ChemicalBatch": {
        "enabled": True,
        "track_create": True,
        "track_update": True,
        "track_delete": False,
        "track_workflow": True,
    },
    "ChemicalAnalysisRequest": {
        "enabled": True,
        "track_create": True,
        "track_update": True,
        "track_delete": False,
        "track_workflow": True,
        "track_assignment": True,
    },
    "ChemicalAnalysisProcess": {
        "enabled": True,
        "track_create": True,
        "track_update": True,
        "track_delete": False,
        "track_workflow": True,
        "track_assignment": True,
        "track_qa": True,
        "track_approval": True,
    },
    "BatchTestResult": {
        "enabled": True,
        "track_create": True,
        "track_update": True,
        "track_delete": False,
    },
    "ChemicalAnalysisInput": {
        "enabled": True,
        "track_create": True,
        "track_update": True,
        "track_delete": False,
        "track_calculation": True,
    },
    "ChemicalCoA": {
        "enabled": True,
        "track_create": True,
        "track_update": False,
        "track_delete": False,
        "track_workflow": True,
        "track_download": True,
        "track_verification": True,
    },
    "PackagingStatus": {
        "enabled": True,
        "track_create": True,
        "track_update": True,
        "track_delete": True,
    },
    "ChemicalCertApprover": {
        "enabled": True,
        "track_create": True,
        "track_update": True,
        "track_delete": True,
    },
    "ChecklistForWaterTreatmentChemicalAnalysis": {
        "enabled": True,
        "track_create": True,
        "track_update": True,
        "track_delete": False,
        "track_workflow": True,
    },
}


EXPORT_CONFIG = {
    "Chemical": {
        "formats": [
            "csv",
            "xlsx",
            "pdf",
        ],
        "columns": [
            "name",
            "code",
            "description",
            "grade",
            "hydrometer_formula",
        ],
    },
    "Supplier": {
        "formats": [
            "csv",
            "xlsx",
            "pdf",
        ],
        "columns": [
            "name",
            "contract_number",
            "contact_info",
        ],
    },
    "ChemicalLocation": {
        "formats": [
            "csv",
            "xlsx",
            "pdf",
        ],
        "columns": [
            "name",
            "treatment_plant",
        ],
    },
    "ChemicalSpecification": {
        "formats": [
            "csv",
            "xlsx",
            "pdf",
        ],
        "columns": [
            "chemical",
            "parameter_name",
            "unit",
            "min_limit",
            "max_limit",
            "method",
            "is_required",
            "version",
            "treatment_plant",
        ],
    },
    "ChemicalBatch": {
        "formats": [
            "csv",
            "xlsx",
            "pdf",
        ],
        "columns": [
            "sample_id",
            "batch_number",
            "delivery_note_number",
            "chemical",
            "supplier",
            "treatment_plant",
            "location",
            "status",
            "quantity_received",
            "unit",
            "package_count",
            "received_date",
            "received_by",
        ],
    },
    "ChemicalAnalysisRequest": {
        "formats": [
            "csv",
            "xlsx",
            "pdf",
        ],
        "columns": [
            "request_id",
            "batch",
            "status",
            "delivery_date",
            "batch__location",
            "analysis_started_at",
            "analysis_deadline",
            "completed_at",
            "assignment_due_at",
            "auto_assigned_at",
            "manual_assigned_at",
        ],
    },
    "ChemicalAnalysisProcess": {
        "formats": [
            "csv",
            "xlsx",
            "pdf",
        ],
        "columns": [
            "batch",
            "analysis_request",
            "status",
            "initiated_by",
            "assigned_analyst",
            "qa_officer",
            "approver",
            "started_at",
            "checklist_completed_at",
            "analysis_started_at",
            "analysis_completed_at",
            "qa_reviewed_at",
            "approved_at",
            "rejected_at",
            "completed_at",
            "rework_count",
        ],
    },
    "BatchTestResult": {
        "formats": [
            "csv",
            "xlsx",
            "pdf",
        ],
        "columns": [
            "batch",
            "parameter",
            "value",
            "textual_result",
            "status",
            "tested_at",
            "analyst",
            "auto_remark",
        ],
    },
    "ChemicalAnalysisInput": {
        "formats": [
            "csv",
            "xlsx",
            "pdf",
        ],
        "columns": [
            "test_result",
            "inputs",
            "calculated_value",
        ],
    },
    "ChemicalCoA": {
        "formats": [
            "pdf",
        ],
        "columns": [
            "batch",
            "chemical_name",
            "supplier_name",
            "quantity",
            "delivered_to",
            "delivery_date",
            "delivery_note_number",
            "packaging_status",
            "sampling_date",
            "analysis_date",
            "date_of_report",
            "approved_by",
            "approved_on",
            "verified_hash",
        ],
    },
    "PackagingStatus": {
        "formats": [
            "csv",
            "xlsx",
            "pdf",
        ],
        "columns": [
            "name",
            "description",
        ],
    },
    "ChemicalCertApprover": {
        "formats": [
            "csv",
            "xlsx",
            "pdf",
        ],
        "columns": [
            "user",
        ],
    },
    "ChecklistForWaterTreatmentChemicalAnalysis": {
        "formats": [
            "csv",
            "xlsx",
            "pdf",
        ],
        "columns": [
            "chemical",
            "chemical_analysis_request",
            "grade",
            "manufacture_date",
            "expiry_date",
            "storage_conditions",
            "net_weight",
            "country_of_origin",
            "sampled_by",
            "requested_by",
            "completed",
            "completed_at",
            "completed_by",
            "created_at",
        ],
    },
}


MODEL_CONFIGS = {
    "Chemical": {
        "search_fields": [
            "name",
            "code",
            "description",
            "grade",
        ],
        "filter_fields": [
            "grade",
        ],
        "ordering": [
            "name",
        ],
        "prefetch_related": [
            "specifications",
        ],
        "queryset_factory": chemical_qs,
        "inline_edit_fields": [
            "name",
            "code",
            "description",
            "grade",
            "hydrometer_formula",
        ],
    },
    "Supplier": {
        "search_fields": [
            "name",
            "contract_number",
            "contact_info",
        ],
        "ordering": [
            "name",
        ],
        "queryset_factory": supplier_qs,
        "inline_edit_fields": [
            "name",
            "contract_number",
            "contact_info",
        ],
    },
    "ChemicalLocation": {
        "search_fields": [
            "name",
            "treatment_plant__name",
        ],
        "filter_fields": [
            "treatment_plant",
        ],
        "ordering": [
            "name",
        ],
        "select_related": [
            "treatment_plant",
        ],
        "queryset_factory": location_qs,
        "inline_edit_fields": [
            "name",
        ],
    },
    "ChemicalSpecification": {
        "search_fields": [
            "chemical__name",
            "chemical__code",
            "parameter_name",
            "unit",
            "method__method",
        ],
        "filter_fields": [
            "chemical",
            "method",
            "treatment_plant",
            "is_required",
            "version",
        ],
        "ordering": [
            "chemical__name",
            "parameter_name",
            "-version",
        ],
        "select_related": [
            "chemical",
            "method",
            "treatment_plant",
        ],
        "prefetch_related": [
            "remarks",
        ],
        "queryset_factory": specification_qs,
        "inline_edit_fields": [
            "parameter_name",
            "unit",
            "min_limit",
            "max_limit",
            "is_required",
            "version",
            "method",
        ],
    },
    "ChemicalBatch": {
        **BATCH_BASE_CONFIG,
        "inline_edit_fields": [
            "status",
            "remarks",
            "quantity_received",
            "package_count",
            "unit",
        ],
        "bulk_actions": {
            "approve": True,
            "reject": True,
            "utilize": True,
            "return": True,
        },
    },
    "ChemicalAnalysisRequest": {
        **ANALYSIS_REQUEST_BASE_CONFIG,
        "inline_edit_fields": [
            "status",
            "analysis_deadline",
        ],
        "bulk_actions": {
            "start_analysis": True,
            "complete_analysis": True,
        },
    },
    "ChemicalAnalysisProcess": {
        **ANALYSIS_PROCESS_BASE_CONFIG,
        "bulk_actions": {
            "move_to_analysis": True,
            "mark_analysis_started": True,
            "mark_analysis_completed": True,
            "mark_approved": True,
            "mark_rejected": True,
            "mark_disposition_completed": True,
            "mark_completed": True,
        },
    },
    "BatchTestResult": {
        **TEST_RESULT_BASE_CONFIG,
        "inline_edit_fields": [
            "value",
            "textual_result",
            "status",
            "auto_remark",
        ],
        "bulk_actions": {
            "verify": True,
            "reject": True,
        },
    },
    "ChemicalAnalysisInput": {
        "search_fields": [
            "test_result__id",
            "test_result__batch__sample_id",
            "test_result__batch__batch_number",
            "test_result__parameter__parameter_name",
        ],
        "filter_fields": [
            "test_result",
        ],
        "ordering": [
            "-id",
        ],
        "select_related": [
            "test_result",
            "test_result__batch",
            "test_result__parameter",
        ],
        "queryset_factory": input_qs,
        "inline_edit_fields": [
            "inputs",
        ],
        "calculation": {
            "property": "calculated_value",
            "formula_property": "formula",
            "required_fields_property": "required_input_fields",
            "precision_property": "rounding_precision",
        },
    },
    "ChemicalCoA": {
        **CERTIFICATE_BASE_CONFIG,
        "bulk_actions": {
            "generate": True,
            "verify": True,
            "approve": True,
            "download": True,
            "revoke": True,
        },
        "meta": {
            "document_type": "certificate_of_analysis",
            "supports_pdf": True,
            "supports_hash_verification": True,
            "immutable_after_approval": True,
        },
    },
    "PackagingStatus": {
        "search_fields": [
            "name",
            "description",
        ],
        "ordering": [
            "name",
        ],
        "inline_edit_fields": [
            "name",
            "description",
        ],
    },
    "ChemicalCertApprover": {
        "search_fields": [
            "user__username",
            "user__first_name",
            "user__last_name",
            "user__email",
        ],
        "filter_fields": [
            "user",
        ],
        "ordering": [
            "user__username",
        ],
        "select_related": [
            "user",
        ],
        "queryset_factory": approver_qs,
    },
    "ChecklistForWaterTreatmentChemicalAnalysis": {
        **CHECKLIST_BASE_CONFIG,
        "inline_edit_fields": [
            "grade",
            "country_of_origin",
            "manufacture_date",
            "expiry_date",
            "storage_conditions",
            "net_weight",
        ],
        "bulk_actions": {
            "complete": True,
            "reopen": True,
        },
    },
}


def get_model_field_names(model) -> set[str]:
    if model is None:
        return set()

    names: set[str] = set()

    for field in model._meta.get_fields():
        if getattr(field, "concrete", False):
            names.add(field.name)

    return names


def resolve_field_path(model, path: str):
    current_model = model

    for component in path.lstrip("-").split("__"):
        try:
            field = current_model._meta.get_field(component)
        except FieldDoesNotExist:
            return None

        remote_model = getattr(field, "remote_field", None)

        if remote_model is not None and remote_model.model is not None:
            current_model = remote_model.model

    return field


def valid_fields(model, fields: Iterable[str] | None) -> list[str]:
    result: list[str] = []

    for field_name in fields or []:
        if resolve_field_path(model, field_name) is not None:
            result.append(field_name)

    return result


def validate_registry_fields(model, config):
    field_configs = (
        "search_fields",
        "filter_fields",
        "ordering",
        "inline_edit_fields",
        "select_related",
        "prefetch_related",
    )

    for key in field_configs:
        values = config.get(key, [])

        if not values:
            continue

        invalid = []

        for value in values:
            if resolve_field_path(model, value) is None:
                invalid.append(value)

        if invalid:
            logger.warning(
                "[ca.registry] %s contains invalid %s: %s",
                model.__name__,
                key,
                invalid,
            )


def build_model_config(model, **kwargs):
    name = model.__name__

    config = deepcopy(
        MODEL_CONFIGS.get(
            name,
            {},
        )
    )

    config.update(
        deepcopy(kwargs)
    )

    config.setdefault(
        "capabilities",
        deepcopy(
            MODEL_CAPABILITIES.get(
                name,
                {},
            )
        ),
    )

    config.setdefault(
        "permissions",
        deepcopy(
            PERMISSIONS.get(
                name,
                {},
            )
        ),
    )

    config.setdefault(
        "workflow",
        deepcopy(
            WORKFLOW_CONFIG.get(
                name,
                {},
            )
        ),
    )

    config.setdefault(
        "audit",
        deepcopy(
            AUDIT_CONFIG.get(
                name,
                {},
            )
        ),
    )

    config.setdefault(
        "export",
        deepcopy(
            EXPORT_CONFIG.get(
                name,
                {},
            )
        ),
    )

    validate_registry_fields(
        model,
        config,
    )

    return config


def safe_register(model, **kwargs):
    if model is None:
        raise RuntimeError(
            "[ca.registry] Model resolution failed."
        )

    config = build_model_config(
        model,
        **kwargs,
    )

    meta = config.get(
        "meta",
        {},
    ).copy()

    meta.update(
        build_meta(model)
    )

    config["meta"] = meta

    ui = config.get(
        "ui",
        {},
    ).copy()

    ui.setdefault(
        "icon",
        ICONS.get(
            model.__name__,
            "flask",
        ),
    )

    ui.setdefault(
        "group",
        GROUPS.get(
            model.__name__,
            "general",
        ),
    )

    config["ui"] = ui

    config.pop(
        "templates",
        None,
    )

    try:
        return register_model(
            **config
        )(model)

    except Exception:
        logger.exception(
            "[ca.registry] Failed registering %s",
            model,
        )
        raise


REGISTERED_MODELS = (
    Chemical,
    Supplier,
    ChemicalLocation,
    ChemicalSpecification,
    ChemicalBatch,
    ChemicalAnalysisRequest,
    ChemicalAnalysisProcess,
    BatchTestResult,
    ChemicalAnalysisInput,
    ChemicalCoA,
    PackagingStatus,
    ChemicalCertApprover,
    ChecklistForWaterTreatmentChemicalAnalysis,
)


for _model in REGISTERED_MODELS:
    if _model is not None:
        safe_register(_model)


try:
    from core.registry import registry

    logger.info(
        "[ca.registry] loaded app=%s models=%s",
        APP_LABEL,
        len(registry._registry),
    )

except Exception:
    logger.exception(
        "[ca.registry] registry unavailable during import"
    )


def get_capabilities(model_name: str):
    return deepcopy(
        MODEL_CAPABILITIES.get(
            model_name,
            {},
        )
    )


def get_permissions(model_name: str):
    return deepcopy(
        PERMISSIONS.get(
            model_name,
            {},
        )
    )


def get_workflow(model_name: str):
    return deepcopy(
        WORKFLOW_CONFIG.get(
            model_name,
            {},
        )
    )


def get_audit_config(model_name: str):
    return deepcopy(
        AUDIT_CONFIG.get(
            model_name,
            {},
        )
    )


def get_export_config(model_name: str):
    return deepcopy(
        EXPORT_CONFIG.get(
            model_name,
            {},
        )
    )


def get_model_config(model_name: str):
    return deepcopy(
        MODEL_CONFIGS.get(
            model_name,
            {},
        )
    )


def get_registered_models():
    try:
        from core.registry import registry

        return dict(
            registry._registry
        )

    except Exception:
        logger.exception(
            "[ca.registry] Unable to read registry"
        )
        return {}


def is_registered(model_name: str) -> bool:
    try:
        from core.registry import registry

        return model_name in registry._registry

    except Exception:
        return False


def get_registered_model(model_name: str):
    try:
        from core.registry import registry

        return registry._registry.get(
            model_name
        )

    except Exception:
        return None


def list_registered_models():
    return sorted(
        get_registered_models().keys()
    )


def get_registered_model_config(model_name: str) -> Dict[str, Any]:
    entry = get_registered_model(model_name)

    if entry is None:
        return {}

    if hasattr(entry, "config"):
        config = getattr(
            entry,
            "config",
            {},
        )

        if isinstance(config, dict):
            return deepcopy(config)

    if hasattr(entry, "meta"):
        meta = getattr(
            entry,
            "meta",
            {},
        )

        if isinstance(meta, dict):
            return {
                "meta": deepcopy(meta),
            }

    if isinstance(entry, dict):
        return deepcopy(entry)

    return {}


def registry_health() -> Dict[str, Any]:
    registered = get_registered_models()

    expected_models = {
        "Chemical": Chemical,
        "Supplier": Supplier,
        "ChemicalLocation": ChemicalLocation,
        "ChemicalSpecification": ChemicalSpecification,
        "ChemicalBatch": ChemicalBatch,
        "ChemicalAnalysisRequest": ChemicalAnalysisRequest,
        "ChemicalAnalysisProcess": ChemicalAnalysisProcess,
        "BatchTestResult": BatchTestResult,
        "ChemicalAnalysisInput": ChemicalAnalysisInput,
        "ChemicalCoA": ChemicalCoA,
        "PackagingStatus": PackagingStatus,
        "ChemicalCertApprover": ChemicalCertApprover,
        "ChecklistForWaterTreatmentChemicalAnalysis": (
            ChecklistForWaterTreatmentChemicalAnalysis
        ),
    }

    expected = {
        name
        for name, model in expected_models.items()
        if model is not None
    }

    actual = set(
        registered.keys()
    )

    missing = sorted(
        expected - actual
    )

    unexpected = sorted(
        actual - expected
    )

    model_health = {}

    for name, model in expected_models.items():
        if model is None:
            model_health[name] = {
                "resolved": False,
                "registered": False,
            }
            continue

        model_health[name] = {
            "resolved": True,
            "registered": name in actual,
        }

    return {
        "app_label": APP_LABEL,
        "expected_models": len(expected),
        "registered_models": len(actual),
        "missing": missing,
        "unexpected": unexpected,
        "models": model_health,
        "healthy": not missing and not unexpected,
    }


def registry_summary() -> Dict[str, Any]:
    registered = get_registered_models()

    summary = {
        "app_label": APP_LABEL,
        "models": [],
        "groups": {},
        "workflow_models": [],
        "document_models": [],
        "exportable_models": [],
        "assignment_models": [],
        "qa_models": [],
        "approval_models": [],
    }

    for model_name in sorted(registered):
        capabilities = get_capabilities(
            model_name
        )

        workflow = get_workflow(
            model_name
        )

        config = get_model_config(
            model_name
        )

        meta = {}

        registered_entry = registered.get(
            model_name
        )

        if hasattr(
            registered_entry,
            "meta",
        ):
            meta = getattr(
                registered_entry,
                "meta",
                {},
            )

        elif hasattr(
            registered_entry,
            "config",
        ):
            entry_config = getattr(
                registered_entry,
                "config",
                {},
            )

            if isinstance(
                entry_config,
                dict,
            ):
                meta = entry_config.get(
                    "meta",
                    {},
                )

        elif isinstance(
            registered_entry,
            dict,
        ):
            meta = registered_entry.get(
                "meta",
                {},
            )

        group = GROUPS.get(
            model_name,
            "general",
        )

        model_item = {
            "name": model_name,
            "key": meta.get(
                "key",
                f"{APP_LABEL}.{model_name.lower()}",
            ),
            "verbose_name": meta.get(
                "verbose_name",
                humanize_model_name(
                    registered_entry
                    if hasattr(
                        registered_entry,
                        "__name__",
                    )
                    else type(
                        registered_entry
                    ),
                ),
            ),
            "group": group,
            "capabilities": deepcopy(
                capabilities
            ),
            "workflow": deepcopy(
                workflow
            ),
            "export": deepcopy(
                config.get(
                    "export",
                    get_export_config(
                        model_name
                    ),
                )
            ),
        }

        summary["models"].append(
            model_item
        )

        summary["groups"].setdefault(
            group,
            [],
        ).append(
            model_name
        )

        if (
            workflow.get(
                "enabled",
                False,
            )
            or capabilities.get(
                "workflow",
                False,
            )
        ):
            summary["workflow_models"].append(
                model_name
            )

        if capabilities.get(
            "document",
            False,
        ):
            summary["document_models"].append(
                model_name
            )

        if capabilities.get(
            "export",
            False,
        ):
            summary["exportable_models"].append(
                model_name
            )

        if capabilities.get(
            "assignment",
            False,
        ):
            summary["assignment_models"].append(
                model_name
            )

        if capabilities.get(
            "qa",
            False,
        ):
            summary["qa_models"].append(
                model_name
            )

        if capabilities.get(
            "approval",
            False,
        ):
            summary["approval_models"].append(
                model_name
            )

    summary["model_count"] = len(
        summary["models"]
    )

    summary["workflow_count"] = len(
        summary["workflow_models"]
    )

    summary["document_count"] = len(
        summary["document_models"]
    )

    summary["exportable_count"] = len(
        summary["exportable_models"]
    )

    summary["assignment_count"] = len(
        summary["assignment_models"]
    )

    summary["qa_count"] = len(
        summary["qa_models"]
    )

    summary["approval_count"] = len(
        summary["approval_models"]
    )

    summary["health"] = registry_health()

    return summary

