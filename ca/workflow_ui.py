"""Shared stage mapping for the chemical batch workflow workbench."""

from __future__ import annotations


WORKFLOW_STAGES = (
    {"key": "receipt", "title": "Batch receipt", "short_title": "Receipt"},
    {"key": "assignment", "title": "Analyst assignment", "short_title": "Analysts"},
    {"key": "checklist", "title": "Receiving checklist", "short_title": "Checklist"},
    {"key": "analysis", "title": "Laboratory analysis", "short_title": "Analysis"},
    {"key": "quality", "title": "Chemist review", "short_title": "Chemist review"},
    {"key": "certificate", "title": "Certificate of Analysis", "short_title": "Certificate"},
    {"key": "disposition", "title": "Batch disposition", "short_title": "Disposition"},
    {"key": "complete", "title": "Workflow complete", "short_title": "Complete"},
)

TASK_STAGE_MAP = {
    "receive": "receipt",
    "create_request": "receipt",
    "assign_analysts": "assignment",
    "checklist": "checklist",
    "complete_checklist": "checklist",
    "start_analysis": "analysis",
    "record_result": "analysis",
    "record_input": "analysis",
    "complete_analysis": "analysis",
    "quality_control_gate": "quality",
    "record_method_qc": "quality",
    "qa_review": "quality",
    "qa_decision": "quality",
    "qa_interlaboratory_decision": "quality",
    "interlaboratory_review": "quality",
    "retest": "quality",
    "retest_result": "quality",
    "retest_input": "quality",
    "complete_retest": "quality",
    "capa": "quality",
    # Display compatibility for batches already at the old approval task.
    "approval": "certificate",
    "reject": "quality",
    "generate_coa": "certificate",
    "refer_kebs": "certificate",
    "approve_coa": "certificate",
    "batch_disposition": "disposition",
    "utilize": "disposition",
    "return_to_supplier": "disposition",
    "return_rejected": "disposition",
}


def stage_for_task_name(task_name: str | None) -> str | None:
    return TASK_STAGE_MAP.get(task_name or "")


def next_stage_key(process) -> str:
    """Return the stage represented by the next unfinished Viewflow task."""
    if process is None:
        return "receipt"

    active_tasks = list(
        process.task_set.filter(status__in=("NEW", "ASSIGNED", "STARTED"))
    )
    active_tasks.sort(key=lambda task: (task.created, task.pk))
    for task in active_tasks:
        node = task.flow_task
        stage = stage_for_task_name(getattr(node, "name", None))
        if stage:
            return stage

    status = getattr(process, "status", "")
    request = getattr(process, "analysis_request", None)
    if status in {"CREATED"}:
        return "receipt"
    if status in {"RECEIVED", "REQUESTED"}:
        if request is None or not request.has_assigned_analysts:
            return "assignment"
        return "checklist"
    if status == "ASSIGNED":
        return "analysis" if process.analysis_ready else "checklist"
    if status == "CHECKLIST":
        return "analysis"
    if status == "ANALYSIS":
        return "quality" if process.is_analysis_complete else "analysis"
    if status in {"RETEST", "QA"}:
        return "quality"
    if status == "APPROVED":
        return "certificate"
    if status == "REJECTED":
        return "disposition"
    if status in {"UTILIZED", "RETURNED", "COMPLETED"}:
        return "complete"
    return "receipt"
