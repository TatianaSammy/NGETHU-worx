from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.views.generic import (
    CreateView,
    DetailView,
    FormView,
    ListView,
    TemplateView,
    UpdateView,
    View,
)

from core.engine.super_user import SuperUserEngine

from .forms import (
    AnalystAssignmentForm,
    ChemicalBatchHoldResolutionForm,
    ChemicalBatchForm,
    ChemicalCustodyRecordForm,
    ChemicalSpecificationFilterForm,
    TestResultFilterForm,
)
from .mixins import CaWanjalaMixin
from .models import (
    BatchTestResult,
    Chemical,
    ChemicalAnalysisInput,
    ChemicalAnalysisProcess,
    ChemicalAnalysisRequest,
    ChemicalBatch,
    ChemicalBatchHold,
    ChemicalCustodyEvent,
    ChemicalInventoryMovement,
    ChemicalWorkflowEvent,
    BatchTestResultRevision,
    ChemicalCoA,
    ChemicalCorrectiveAction,
    ChemicalLocation,
    ChemicalSpecification,
    ChecklistForWaterTreatmentChemicalAnalysis,
    Supplier,
)
from .workflow_ui import WORKFLOW_STAGES, next_stage_key, stage_for_task_name

logger = logging.getLogger(__name__)


CHEMICAL_PERMISSIONS = {
    "view_workflow": "ca.view_chemicalanalysisprocess",
    "receive": "ca.receive_chemical_batch",
    "request": "ca.create_analysis_request",
    "checklist": "ca.complete_receiving_checklist",
    "start": "ca.start_chemical_analysis",
    "result": "ca.record_chemical_result",
    "complete": "ca.complete_chemical_analysis",
    "retest": "ca.perform_chemical_retest",
    "qa": "ca.review_chemical_analysis",
    "generate_coa": "ca.approve_chemical_coa",
    "approve_coa": "ca.approve_chemical_coa",
    "verify_coa": "ca.verify_chemical_coa",
    "approve_batch": "ca.approve_chemical_batch",
    "reject_batch": "ca.reject_chemical_batch",
    "utilize": "ca.utilize_chemical_batch",
    "return": "ca.return_chemical_batch",
    "reopen_checklist": "ca.reopen_chemical_checklist",
}


PROCESS_ACTIVE_STATUSES = (
    "REQUESTED",
    "CHECKLIST",
    "ASSIGNED",
    "ANALYSIS",
    "RETEST",
    "QA",
)

PROCESS_ANALYSIS_STATUSES = (
    "ANALYSIS",
    "RETEST",
)

PROCESS_TERMINAL_STATUSES = (
    "UTILIZED",
    "RETURNED",
    "COMPLETED",
)

BATCH_TERMINAL_STATUSES = (
    "UTILIZED",
    "RETURNED",
)


def _scope_queryset(queryset, user, field="treatment_plant_id", plant_id=None):
    """Plant-scope a queryset using Wanjala/SuperUser rules."""
    if SuperUserEngine.is_superuser(user):
        if plant_id is None:
            return queryset
        return queryset.filter(**{field: plant_id})

    assigned = getattr(user, "assigned_treatment_plant_id", None)
    if assigned is None:
        raise PermissionDenied(
            "Your account is not assigned to a treatment plant."
        )
    return queryset.filter(**{field: assigned})


def _process_queryset(user, plant_id=None):
    queryset = ChemicalAnalysisProcess.objects.all()

    return _scope_queryset(
        queryset,
        user,
        "batch__treatment_plant_id",
        plant_id=plant_id,
    )


def _process_display_queryset(user, plant_id=None):
    return (
        _process_queryset(user, plant_id=plant_id)
        .select_related(
            "batch",
            "batch__chemical",
            "batch__supplier",
            "batch__treatment_plant",
            "batch__location",
            "analysis_request",
            "initiated_by",
            "assigned_analyst",
            "qa_officer",
            "approver",
        )
        .prefetch_related(
            "analysis_request__assigned_analysts",
        )
        .order_by(
            "-updated_at",
            "-pk",
        )
    )


def _batch_queryset(user, plant_id=None):
    queryset = ChemicalBatch.objects.all()

    return _scope_queryset(
        queryset,
        user,
        plant_id=plant_id,
    )


def _batch_display_queryset(user, plant_id=None):
    return (
        _batch_queryset(user, plant_id=plant_id)
        .select_related(
            "chemical",
            "supplier",
            "treatment_plant",
            "location",
            "received_by",
        )
        .order_by(
            "-received_date",
            "-pk",
        )
    )


def _request_queryset(user, plant_id=None):
    queryset = ChemicalAnalysisRequest.objects.all()

    return _scope_queryset(
        queryset,
        user,
        "batch__treatment_plant_id",
        plant_id=plant_id,
    )


def _request_display_queryset(user, plant_id=None):
    return (
        _request_queryset(user, plant_id=plant_id)
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
            "-pk",
        )
    )


def _result_queryset(user, plant_id=None):
    queryset = BatchTestResult.objects.all()

    return _scope_queryset(
        queryset,
        user,
        "batch__treatment_plant_id",
        plant_id=plant_id,
    )


def _result_display_queryset(user, plant_id=None):
    return (
        _result_queryset(user, plant_id=plant_id)
        .select_related(
            "batch",
            "batch__chemical",
            "parameter",
            "parameter__method",
            "analyst",
        )
        .order_by(
            "-tested_at",
            "-pk",
        )
    )


def _analysis_input_queryset(user, plant_id=None):
    queryset = (
        ChemicalAnalysisInput.objects
        .select_related(
            "test_result",
            "test_result__batch",
            "test_result__batch__chemical",
            "test_result__parameter",
            "test_result__parameter__method",
        )
    )

    return _scope_queryset(
        queryset,
        user,
        "test_result__batch__treatment_plant_id",
        plant_id=plant_id,
    )


def _coa_queryset(user, plant_id=None):
    queryset = ChemicalCoA.objects.all()

    return _scope_queryset(
        queryset,
        user,
        "batch__treatment_plant_id",
        plant_id=plant_id,
    )


def _coa_display_queryset(user, plant_id=None):
    return (
        _coa_queryset(user, plant_id=plant_id)
        .select_related(
            "batch",
            "batch__chemical",
            "batch__supplier",
            "batch__treatment_plant",
            "approved_by",
        )
        .order_by(
            "-pk",
        )
    )


def _specification_queryset(user, plant_id=None):
    queryset = (
        ChemicalSpecification.objects
        .select_related(
            "chemical",
            "method",
            "treatment_plant",
        )
        .order_by(
            "chemical__name",
            "parameter_name",
            "-version",
        )
    )

    if SuperUserEngine.is_superuser(user):
        if plant_id is None:
            return queryset
        return queryset.filter(
            Q(treatment_plant_id=plant_id)
            | Q(treatment_plant__isnull=True)
        )

    assigned = getattr(user, "assigned_treatment_plant_id", None)
    if assigned is None:
        raise PermissionDenied(
            "Your account is not assigned to a treatment plant."
        )

    return queryset.filter(
        Q(treatment_plant_id=assigned)
        | Q(treatment_plant__isnull=True)
    )


def _checklist_queryset(user, plant_id=None):
    queryset = (
        ChecklistForWaterTreatmentChemicalAnalysis.objects
        .select_related(
            "chemical_analysis_request",
            "chemical_analysis_request__batch",
            "chemical_analysis_request__batch__chemical",
            "chemical_analysis_request__batch__treatment_plant",
        )
        .order_by(
            "-pk",
        )
    )

    return _scope_queryset(
        queryset,
        user,
        "chemical_analysis_request__batch__treatment_plant_id",
        plant_id=plant_id,
    )


def _get_process_or_404(
    user,
    pk,
    lock=False,
    plant_id=None,
):
    queryset = _process_display_queryset(user, plant_id=plant_id)

    if lock:
        queryset = queryset.select_for_update(of=("self",))

    process = (
        queryset
        .filter(pk=pk)
        .first()
    )

    if process is None:
        raise Http404(
            "Chemical analysis process not found."
        )

    return process


def _get_batch_or_404(
    user,
    pk,
    lock=False,
    plant_id=None,
):
    queryset = _batch_display_queryset(user, plant_id=plant_id)

    if lock:
        queryset = queryset.select_for_update(of=("self",))

    batch = (
        queryset
        .filter(pk=pk)
        .first()
    )

    if batch is None:
        raise Http404(
            "Chemical batch not found."
        )

    return batch


def _get_request_or_404(
    user,
    pk,
    plant_id=None,
):
    request_obj = (
        _request_display_queryset(user, plant_id=plant_id)
        .filter(pk=pk)
        .first()
    )

    if request_obj is None:
        raise Http404(
            "Chemical analysis request not found."
        )

    return request_obj


def _get_result_or_404(
    user,
    pk,
    plant_id=None,
):
    result = (
        _result_display_queryset(user, plant_id=plant_id)
        .filter(pk=pk)
        .first()
    )

    if result is None:
        raise Http404(
            "Chemical test result not found."
        )

    return result


def _get_coa_or_404(
    user,
    pk,
    lock=False,
    plant_id=None,
):
    queryset = _coa_display_queryset(user, plant_id=plant_id)

    if lock:
        queryset = queryset.select_for_update(of=("self",))

    coa = (
        queryset
        .filter(pk=pk)
        .first()
    )

    if coa is None:
        raise Http404(
            "Certificate of Analysis not found."
        )

    return coa




def _request_assignment_data(analysis_request):
    assigned_analysts = list(
        analysis_request.assigned_analysts.all()
    )

    deadline = getattr(
        analysis_request,
        "analysis_deadline",
        None,
    )

    return {
        "assigned_analyst_ids": [
            user.pk
            for user in assigned_analysts
        ],
        "assigned_analysts": [
            {
                "id": user.pk,
                "username": getattr(
                    user,
                    "username",
                    None,
                ),
                "name": (
                    user.get_full_name()
                    if hasattr(
                        user,
                        "get_full_name",
                    )
                    else str(user)
                ),
            }
            for user in assigned_analysts
        ],
        "assigned_analyst_count": len(
            assigned_analysts
        ),
        "assignment_pending": bool(
            getattr(
                analysis_request,
                "assignment_pending",
                False,
            )
        ),
        "assignment_overdue": bool(
            getattr(
                analysis_request,
                "assignment_overdue",
                False,
            )
        ),
        "manual_assigned_at": (
            analysis_request.manual_assigned_at.isoformat()
            if getattr(
                analysis_request,
                "manual_assigned_at",
                None,
            )
            else None
        ),
        "auto_assigned_at": (
            analysis_request.auto_assigned_at.isoformat()
            if getattr(
                analysis_request,
                "auto_assigned_at",
                None,
            )
            else None
        ),
        "analysis_deadline": (
            deadline.isoformat()
            if deadline
            else None
        ),
    }


class ChemicalBatchListView(
    CaWanjalaMixin,
    ListView,
):
    model = ChemicalBatch
    template_name = "ca/chema/batch_list.html"
    context_object_name = "batches"
    permission_required_view = "ca.view_chemicalbatch"
    paginate_by = 50

    def get_queryset(self):
        queryset = _batch_display_queryset(
            self.request.user
        )

        highlighted = self.request.GET.get("highlight")
        if highlighted and str(highlighted).isdigit():
            # The assignment notification lands on the exact scoped batch so
            # it is visible without searching or paging through the register.
            queryset = queryset.filter(pk=int(highlighted))

        status = self.request.GET.get("status")

        if status:
            queryset = queryset.filter(
                status=status
            )

        category = self.request.GET.get("category")
        if category:
            queryset = queryset.filter(
                Q(chemical__categories__category__icontains=category)
                | Q(chemical__name__icontains=category)
                | Q(chemical__description__icontains=category)
            )

        chemical = self.request.GET.get(
            "chemical"
        )

        if chemical:
            queryset = queryset.filter(
                Q(
                    chemical__name__icontains=chemical
                )
                | Q(
                    chemical__code__icontains=chemical
                )
            )

        query = self.request.GET.get("q")

        if query:
            queryset = queryset.filter(
                Q(
                    sample_id__icontains=query
                )
                | Q(
                    batch_number__icontains=query
                )
                | Q(
                    delivery_note_number__icontains=query
                )
            )

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        highlighted = self.request.GET.get("highlight")
        context["highlighted_batch_id"] = (
            int(highlighted)
            if highlighted and str(highlighted).isdigit()
            else None
        )

        page = context.get("page_obj")
        visible_batches = list(page.object_list if page is not None else context.get("batches", ()))
        batch_ids = [batch.pk for batch in visible_batches if batch.pk]
        processes = (
            ChemicalAnalysisProcess.objects
            .filter(batch_id__in=batch_ids)
            .select_related("batch", "analysis_request")
            .prefetch_related("analysis_request__checklists", "task_set")
        )
        user = self.request.user
        active_statuses = {"NEW", "ASSIGNED", "STARTED"}

        for batch in visible_batches:
            batch._state.fields_cache["analysis_process"] = None
            batch._state.fields_cache["analysis_request"] = None
            batch.workflow_action_url = ""
            batch.workflow_action_label = ""

        for process in processes:
            batch = process.batch
            batch._state.fields_cache["analysis_process"] = process
            request = process.analysis_request if process.analysis_request_id else None
            batch._state.fields_cache["analysis_request"] = request
            if (
                request is None
                or not request.is_assigned_analyst(user)
                or not process.is_authorized_analyst(user)
                or batch.has_active_holds
            ):
                continue

            checklist_complete = any(
                checklist.completed for checklist in request.checklists.all()
            )
            if not checklist_complete:
                node_name = "checklist"
                label = "Complete Checklist"
                required_permission = "ca.complete_receiving_checklist"
            elif process.status in {
                ChemicalAnalysisProcess.WorkflowStatus.ASSIGNED,
                ChemicalAnalysisProcess.WorkflowStatus.CHECKLIST,
            }:
                node_name = "start_analysis"
                label = "Start Analysis"
                required_permission = "ca.start_chemical_analysis"
            elif process.status == ChemicalAnalysisProcess.WorkflowStatus.ANALYSIS:
                node_name = "record_result"
                label = "Continue Analysis"
                required_permission = "ca.record_chemical_result"
            else:
                continue

            if not user.is_superuser and not user.has_perm(required_permission):
                continue

            for task in process.task_set.all():
                node = task.flow_task
                if not node or node.name != node_name or task.status not in active_statuses:
                    continue
                try:
                    if task.status == "NEW":
                        can_open = node.can_assign(user, task)
                    elif task.status == "ASSIGNED":
                        # The analyst pool can claim an unstarted task even
                        # when Viewflow previously assigned it to another role.
                        can_open = True
                    else:
                        can_open = task.owner_id == user.pk
                    if not can_open:
                        continue
                except Exception:
                    continue
                batch.workflow_action_url = reverse(
                    "ca:batch-workflow-action",
                    kwargs={"pk": batch.pk, "node_name": node_name},
                )
                batch.workflow_action_label = label
                break

        return context


class ChemicalBatchWorkflowActionView(CaWanjalaMixin, View):
    """Claim and open the current analyst's active batch workflow task."""

    permission_required_view = "ca.view_chemicalbatch"
    task_views = {
        "checklist": "ChecklistTaskView",
        "start_analysis": "StartAnalysisTaskView",
        "record_result": "RecordTestResultTaskView",
    }
    task_permissions = {
        "checklist": "ca.complete_receiving_checklist",
        "start_analysis": "ca.start_chemical_analysis",
        "record_result": "ca.record_chemical_result",
    }

    def get(self, request, pk, node_name, *args, **kwargs):
        return self._open_task(request, pk, node_name)

    def post(self, request, pk, node_name, *args, **kwargs):
        return self._open_task(request, pk, node_name)

    def _open_task(self, request, batch_id, node_name):
        from .chemical_flow import (
            ChemicalAnalysisFlow,
            ChecklistTaskView,
            RecordTestResultTaskView,
            StartAnalysisTaskView,
        )

        view_classes = {
            "checklist": ChecklistTaskView,
            "start_analysis": StartAnalysisTaskView,
            "record_result": RecordTestResultTaskView,
        }

        if node_name not in self.task_views:
            raise Http404("Workflow action not found.")
        user = request.user
        batch = _get_batch_or_404(
            user,
            batch_id,
            plant_id=self.get_scope_plant_id(),
        )
        process_id = (
            ChemicalAnalysisProcess.objects
            .filter(batch_id=batch.pk)
            .values_list("pk", flat=True)
            .first()
        )
        if process_id is None:
            raise Http404("Chemical analysis workflow not found.")

        with ChemicalAnalysisFlow.lock(process_id), transaction.atomic():
            process = (
                ChemicalAnalysisProcess.objects
                # Lock only the process row. ``analysis_request`` is nullable,
                # so locking every row pulled in by select_related() makes
                # PostgreSQL reject this query's outer join.
                .select_for_update(of=("self",))
                .select_related("batch", "analysis_request")
                .get(pk=process_id)
            )
            analysis_request = process.analysis_request if process.analysis_request_id else None
            if (
                analysis_request is None
                or not analysis_request.is_assigned_analyst(user)
                or not process.is_authorized_analyst(user)
            ):
                raise PermissionDenied("Only an assigned, authorized analyst may run this batch task.")
            permission = self.task_permissions[node_name]
            if not user.is_superuser and not user.has_perm(permission):
                raise PermissionDenied("You do not have permission to run this workflow task.")

            if node_name == "checklist":
                if analysis_request.checklists.filter(completed=True).exists() or process.status not in {
                    ChemicalAnalysisProcess.WorkflowStatus.RECEIVED,
                    ChemicalAnalysisProcess.WorkflowStatus.REQUESTED,
                    ChemicalAnalysisProcess.WorkflowStatus.ASSIGNED,
                    ChemicalAnalysisProcess.WorkflowStatus.CHECKLIST,
                }:
                    messages.info(request, "The receiving checklist has already been completed for this batch.")
                    stage = next_stage_key(process)
                    return redirect(f"{reverse('ca:batch-detail', kwargs={'pk': batch.pk})}?stage={stage}")
                if batch.has_active_holds:
                    raise PermissionDenied("Resolve the open intake hold before completing the checklist.")
            elif node_name == "start_analysis":
                if process.status == ChemicalAnalysisProcess.WorkflowStatus.ANALYSIS:
                    return redirect(
                        reverse(
                            "ca:batch-workflow-action",
                            kwargs={"pk": batch.pk, "node_name": "record_result"},
                        )
                    )
                if process.status not in {
                    ChemicalAnalysisProcess.WorkflowStatus.ASSIGNED,
                    ChemicalAnalysisProcess.WorkflowStatus.CHECKLIST,
                } or not process.analysis_ready or batch.has_active_holds:
                    raise PermissionDenied("The batch is not ready to start analysis.")
            elif process.status != ChemicalAnalysisProcess.WorkflowStatus.ANALYSIS:
                raise PermissionDenied("The batch is not currently accepting analysis results.")

            active_statuses = {"NEW", "ASSIGNED", "STARTED"}
            task = None
            for candidate in process.task_set.select_for_update().filter(status__in=active_statuses):
                node = candidate.flow_task
                if node and node.name == node_name:
                    task = candidate
                    break
            if task is None:
                stage = next_stage_key(process)
                messages.info(request, "That workflow task is not currently pending.")
                return redirect(f"{reverse('ca:batch-detail', kwargs={'pk': batch.pk})}?stage={stage}")

            activation = task.flow_task.activation_class(task)
            if task.status == "NEW":
                if not task.flow_task.can_assign(user, task):
                    raise PermissionDenied("You cannot claim this workflow task.")
                activation.assign(user)
            elif task.status == "ASSIGNED" and task.owner_id != user.pk:
                previous_owner_id = task.owner_id
                task.owner = user
                task.save(update_fields=["owner"])
                from .services.workflow_enhancements import record_workflow_event

                record_workflow_event(
                    process,
                    event_type="analyst_task_claimed",
                    action=node_name,
                    actor=user,
                    details={"task_id": task.pk, "previous_owner_id": previous_owner_id},
                )
            elif task.status == "STARTED" and task.owner_id != user.pk:
                raise PermissionDenied("This task is already being completed by another analyst.")

            if task.status != "STARTED":
                activation.start(request)
            request.activation = activation

            return view_classes[node_name].as_view()(
                request,
                process_pk=process.pk,
                task_pk=task.pk,
            )


class ChemicalBatchTaskClaimView(CaWanjalaMixin, View):
    """Claim or take over a batch workflow task (e.g. when previous owner is conflicted)."""

    permission_required_view = "ca.view_chemicalbatch"

    def get(self, request, pk, task_pk, *args, **kwargs):
        return self._claim_and_redirect(request, pk, task_pk)

    def post(self, request, pk, task_pk, *args, **kwargs):
        return self._claim_and_redirect(request, pk, task_pk)

    def _claim_and_redirect(self, request, batch_id, task_pk):
        from viewflow.workflow.models import Task
        from .chemical_flow import ChemicalAnalysisFlow
        from .services.workflow_enhancements import record_workflow_event

        user = request.user
        batch = _get_batch_or_404(
            user,
            batch_id,
            plant_id=self.get_scope_plant_id(),
        )
        process = (
            ChemicalAnalysisProcess.objects
            .filter(batch_id=batch.pk)
            .select_related("batch")
            .first()
        )
        if process is None:
            raise Http404("Chemical analysis workflow not found.")

        with ChemicalAnalysisFlow.lock(process.pk), transaction.atomic():
            try:
                task = process.task_set.select_for_update().get(pk=task_pk)
            except Task.DoesNotExist:
                raise Http404("Task not found.")

            node = task.flow_task
            node_name = getattr(node, "name", "")

            if not ChemicalBatchDetailView._task_permission_ok(user, task):
                raise PermissionDenied("You do not have permission to claim this task.")

            if node_name == "approval" and not process.is_independent_approver(user):
                raise PermissionDenied("Legacy batch approval must be performed by someone independent of analysis and chemist review.")

            if node_name == "qa_review" and process.is_analysis_performer(user):
                raise PermissionDenied("The analyst who performed this batch cannot claim its chemist review.")

            if node_name == "approve_coa":
                if (
                    user.pk in process.performing_analyst_ids
                    or user.pk == process.qa_officer_id
                    or user.pk == process.approver_id
                ):
                    raise PermissionDenied("CoA approval must be independent of analysis and chemist batch review.")

            if task.status not in {"NEW", "ASSIGNED", "STARTED"}:
                messages.warning(request, "This task is not available to claim.")
                return redirect("ca:batch-detail", pk=batch.pk)

            previous_owner_id = task.owner_id
            if task.status == "NEW":
                activation = task.flow_task.activation_class(task)
                activation.assign(user)
            elif task.status in {"ASSIGNED", "STARTED"}:
                if task.owner_id != user.pk:
                    task.owner = user
                    if task.status == "STARTED":
                        task.status = "ASSIGNED"
                    task.save(update_fields=["owner", "status"])
                    record_workflow_event(
                        process,
                        event_type="workflow_task_reassigned",
                        action=node_name,
                        actor=user,
                        details={"task_id": task.pk, "previous_owner_id": previous_owner_id},
                    )

            return redirect(
                f"ca:chemicalanalysis:{node_name}:execute",
                process_pk=process.pk,
                task_pk=task.pk,
            )


class ChemicalBatchDetailView(
    CaWanjalaMixin,
    DetailView,
):
    model = ChemicalBatch
    template_name = "ca/chema/batch_detail.html"
    context_object_name = "batch"
    permission_required_view = "ca.view_chemicalbatch"

    def get_object(self, queryset=None):
        return _get_batch_or_404(
            self.request.user,
            self.kwargs["pk"],
            plant_id=self.get_scope_plant_id(),
        )

    @staticmethod
    def _task_permission_ok(user, task):
        if user.is_superuser:
            return True
        required_permission = getattr(task, "owner_permission", None)
        if required_permission:
            permission_object = getattr(task, "owner_permission_obj", None)
            try:
                return user.has_perm(required_permission, obj=permission_object) or user.has_perm(required_permission)
            except TypeError:
                return user.has_perm(required_permission)
        if getattr(task.flow_task, "name", "") == "batch_disposition":
            return user.has_perm("ca.utilize_chemical_batch") or user.has_perm("ca.return_chemical_batch")
        return False

    def _task_action(self, process, analysis_request, stage_key, user):
        if process is None:
            return "", ""

        active_tasks = list(
            process.task_set.filter(status__in=("NEW", "ASSIGNED", "STARTED"))
        )
        active_tasks.sort(key=lambda task: (task.created, task.pk))
        stage_tasks = [
            task for task in active_tasks
            if stage_for_task_name(getattr(task.flow_task, "name", None)) == stage_key
        ]

        if (
            stage_key == "assignment"
            and analysis_request is not None
            and analysis_request.status == ChemicalAnalysisRequest.Status.PENDING
            and not analysis_request.has_assigned_analysts
            and (user.is_superuser or user.has_perm("ca.create_analysis_request"))
        ):
            return (
                reverse("ca:request-assign-analyst", kwargs={"pk": analysis_request.pk}),
                "Assign analysts",
            )

        for task in stage_tasks:
            node = task.flow_task
            node_name = getattr(node, "name", "")
            if node_name in {"checklist", "start_analysis", "record_result"}:
                if (
                    process.is_authorized_analyst(user)
                    and self._task_permission_ok(user, task)
                    and not process.batch.has_active_holds
                ):
                    return (
                        reverse(
                            "ca:batch-workflow-action",
                            kwargs={"pk": process.batch_id, "node_name": node_name},
                        ),
                        {
                            "checklist": "Complete receiving checklist",
                            "start_analysis": "Start analysis",
                            "record_result": "Enter next result",
                        }[node_name],
                    )
                continue

            if not self._task_permission_ok(user, task):
                continue

            if node_name == "qa_review" and process.is_analysis_performer(user):
                continue

            if node_name == "approval" and not process.is_independent_approver(user):
                continue
            if node_name == "approve_coa" and (
                user.pk in process.performing_analyst_ids
                or user.pk == process.qa_officer_id
                or user.pk == process.approver_id
            ):
                continue

            try:
                if task.owner_id == user.pk and node.can_execute(user, task):
                    action = "execute"
                    label = "Open current step"
                    return (
                        reverse(
                            f"ca:chemicalanalysis:{node_name}:{action}",
                            args=[process.pk, task.pk],
                        ),
                        label,
                    )
                elif task.status in {"NEW", "ASSIGNED", "STARTED"}:
                    return (
                        reverse(
                            "ca:batch-task-claim",
                            kwargs={"pk": process.batch_id, "task_pk": task.pk},
                        ),
                        "Claim this step",
                    )
                else:
                    continue
            except (AttributeError, NoReverseMatch, PermissionError, TypeError):
                continue

        return "", ""

    def _waiting_message_for_stage(self, batch, process, analysis_request, stage_key):
        """Build a status message naming the authorized person awaited at this stage."""
        actor_name = None

        active_tasks = list(
            process.task_set.filter(status__in=("NEW", "ASSIGNED", "STARTED"))
        ) if process else []
        active_tasks.sort(key=lambda t: (t.created, t.pk))
        stage_tasks = [
            t for t in active_tasks
            if stage_for_task_name(getattr(t.flow_task, "name", None)) == stage_key
        ]

        # 1. Check if an active task for this stage has an assigned owner
        for t in stage_tasks:
            if t.owner:
                if stage_key == "quality" and process and process.is_analysis_performer(t.owner):
                    continue
                actor_name = t.owner.get_full_name().strip() or t.owner.get_username()
                break

        # 2. If no task owner, resolve from process / request data
        if not actor_name:
            if stage_key in {"assignment", "checklist", "analysis"}:
                if process and process.assigned_analyst:
                    actor_name = process.assigned_analyst.get_full_name().strip() or process.assigned_analyst.get_username()
                elif analysis_request and analysis_request.assigned_analysts.exists():
                    analysts = list(analysis_request.assigned_analysts.all())
                    actor_name = ", ".join(
                        a.get_full_name().strip() or a.get_username() for a in analysts
                    )
            elif stage_key == "quality":
                if process and process.qa_officer:
                    actor_name = process.qa_officer.get_full_name().strip() or process.qa_officer.get_username()
            elif stage_key == "certificate":
                coa = getattr(batch, "coa", None) if batch else None
                if coa and coa.approved_by:
                    actor_name = coa.approved_by.get_full_name().strip() or coa.approved_by.get_username()
                elif process and process.assigned_analyst:
                    actor_name = process.assigned_analyst.get_full_name().strip() or process.assigned_analyst.get_username()

        # 3. Fallback to process assigned analyst or analysis request assigned analysts
        if not actor_name:
            if stage_key != "quality" and process and process.assigned_analyst:
                actor_name = process.assigned_analyst.get_full_name().strip() or process.assigned_analyst.get_username()
            elif stage_key != "quality" and analysis_request and analysis_request.assigned_analysts.exists():
                analysts = list(analysis_request.assigned_analysts.all())
                actor_name = ", ".join(
                    a.get_full_name().strip() or a.get_username() for a in analysts
                )

        if actor_name:
            if stage_key == "quality":
                return f"Waiting for the independent chemist reviewer ({actor_name})"
            return f"Waiting for the authorized Analyst ({actor_name})"
        if stage_key == "quality":
            return "Waiting for an independent chemist reviewer"
        return "Waiting for the authorized Analyst"

    def _workflow_stage_context(self, batch, process, analysis_request, checklist, coa):
        status = getattr(process, "status", "") if process else ""
        current_key = next_stage_key(process)
        if process is None and analysis_request is not None:
            current_key = (
                "assignment"
                if not analysis_request.has_assigned_analysts
                else "checklist"
            )
        rejected = status == ChemicalAnalysisProcess.WorkflowStatus.REJECTED or batch.status == ChemicalBatch.Status.REJECTED
        terminal = status in {
            ChemicalAnalysisProcess.WorkflowStatus.UTILIZED,
            ChemicalAnalysisProcess.WorkflowStatus.RETURNED,
            ChemicalAnalysisProcess.WorkflowStatus.COMPLETED,
        } or batch.status in {ChemicalBatch.Status.UTILIZED, ChemicalBatch.Status.RETURNED}

        completed = {
            "receipt": (
                (batch.status != ChemicalBatch.Status.RECEIVED or bool(batch.received_date))
                and not (process and status == ChemicalAnalysisProcess.WorkflowStatus.CREATED)
            ),
            "assignment": bool(analysis_request and analysis_request.has_assigned_analysts),
            "checklist": bool(process and process.analysis_ready),
            "analysis": bool(process and process.is_analysis_complete and status != ChemicalAnalysisProcess.WorkflowStatus.ANALYSIS),
            "quality": status in {
                ChemicalAnalysisProcess.WorkflowStatus.APPROVED,
                ChemicalAnalysisProcess.WorkflowStatus.REJECTED,
                ChemicalAnalysisProcess.WorkflowStatus.UTILIZED,
                ChemicalAnalysisProcess.WorkflowStatus.RETURNED,
                ChemicalAnalysisProcess.WorkflowStatus.COMPLETED,
            },
            "certificate": bool(coa and coa.is_approved),
            "disposition": terminal,
            "complete": terminal or bool(process and process.completed_at),
        }
        skipped = {
            "certificate": rejected,
        }
        stage_indexes = {stage["key"]: index for index, stage in enumerate(WORKFLOW_STAGES)}
        current_index = stage_indexes.get(current_key, 0)
        stages = []

        for index, definition in enumerate(WORKFLOW_STAGES):
            key = definition["key"]
            is_skipped = skipped.get(key, False)
            is_current = key == current_key and not is_skipped and not (
                key == "complete" and completed.get(key, False)
            )
            is_completed = completed.get(key, False) or index < current_index
            if is_skipped:
                state = "skipped"
            elif is_current:
                state = "current"
            elif is_completed:
                state = "complete"
            else:
                state = "locked"

            if key == "receipt":
                summary = f"Received {batch.received_date:%d %b %Y}" if batch.received_date else "Batch receipt is not recorded yet."
            elif key == "assignment":
                count = analysis_request.assigned_analysts.count() if analysis_request else 0
                summary = f"{count} analyst{'s' if count != 1 else ''} assigned." if count else "An analyst must be assigned before the checklist can begin."
            elif key == "checklist":
                summary = "Receiving checklist is complete." if checklist and checklist.completed else "The assigned analyst must complete the receiving checklist."
            elif key == "analysis":
                progress = process.analysis_progress if process else 0
                summary = f"{progress:g}% of required analytical parameters recorded." if process else "Analysis process has not been created."
            elif key == "quality":
                summary = "Chemist review and any required corrective work are complete." if completed[key] else "Results are routed to chemist review, retest or corrective action as required."
            elif key == "certificate":
                summary = "Certificate generated and approved." if completed[key] else ("Certificate is not required for a rejected batch." if is_skipped else "Generate and approve the Certificate of Analysis.")
            elif key == "disposition":
                summary = "Batch disposition is recorded." if completed[key] else "Record utilization or return the batch to its supplier."
            else:
                summary = "The workflow is closed." if completed[key] else "The batch will appear complete after its final disposition."

            action_url, action_label = self._task_action(
                process, analysis_request, key, self.request.user
            ) if is_current else ("", "")
            waiting_message = (
                self._waiting_message_for_stage(batch, process, analysis_request, key)
                if is_current and not action_url
                else ""
            )
            stages.append({
                **definition,
                "state": state,
                "summary": summary,
                "action_url": action_url,
                "action_label": action_label,
                "waiting_message": waiting_message,
                "index": index + 1,
            })

        requested_key = self.request.GET.get("stage")
        stage_states = {stage["key"]: stage["state"] for stage in stages}
        selected_key = current_key
        if requested_key in stage_states and stage_states[requested_key] != "locked":
            selected_key = requested_key
        completed_count = sum(stage["state"] in {"complete", "skipped"} for stage in stages)
        return stages, selected_key, current_key, round(completed_count / len(stages) * 100)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(
            **kwargs
        )

        batch = self.object
        user = self.request.user

        process = (
            _process_display_queryset(user, plant_id=self.get_scope_plant_id())
            .filter(batch=batch)
            .first()
        )

        results = (
            _result_display_queryset(user, plant_id=self.get_scope_plant_id())
            .filter(batch=batch)
        )

        coa = (
            _coa_display_queryset(user, plant_id=self.get_scope_plant_id())
            .filter(batch=batch)
            .first()
        )

        analysis_request = (
            _request_display_queryset(user, plant_id=self.get_scope_plant_id())
            .filter(batch=batch)
            .first()
        )

        checklist = None

        if analysis_request:
            checklist = (
                _checklist_queryset(user, plant_id=self.get_scope_plant_id())
                .filter(
                    chemical_analysis_request=analysis_request
                )
                .first()
            )

        workflow_stages, selected_stage, current_stage, workflow_progress = self._workflow_stage_context(
            batch, process, analysis_request, checklist, coa
        )

        context.update(
            {
                "process": process,
                "analysis_request": analysis_request,
                "analyst_assignment_url": (
                    reverse("ca:request-assign-analyst", kwargs={"pk": analysis_request.pk})
                    if (
                        analysis_request
                        and analysis_request.status in {
                            ChemicalAnalysisRequest.Status.PENDING,
                            ChemicalAnalysisRequest.Status.IN_PROGRESS,
                        }
                        and (user.is_superuser or user.has_perm("ca.create_analysis_request"))
                    )
                    else ""
                ),
                "checklist": checklist,
                "results": results,
                "test_results": results,
                "coa": coa,
                "workflow_stages": workflow_stages,
                "selected_workflow_stage": selected_stage,
                "current_workflow_stage": current_stage,
                "workflow_progress": workflow_progress,
                "sampling": None,
                "holds": batch.workflow_holds.select_related("opened_by", "resolved_by").all(),
                "custody_events": batch.custody_events.select_related("actor").all(),
                "inventory_movements": batch.inventory_movements.select_related("actor").all(),
                "workflow_events": (
                    process.workflow_events.select_related("actor").all()
                    if process and user.has_perm("ca.view_chemical_workflow_event")
                    else ChemicalWorkflowEvent.objects.none()
                ),
                "result_revisions": batch.result_revisions.select_related("actor", "parameter").all(),
                "corrective_actions": (
                    process.corrective_actions.select_related("submitted_by", "reviewed_by").all()
                    if process else ChemicalCorrectiveAction.objects.none()
                ),
            }
        )

        return context


class ChemicalBatchHoldResolutionView(CaWanjalaMixin, FormView):
    template_name = "ca/chema/resolve_batch_hold.html"
    form_class = ChemicalBatchHoldResolutionForm
    permission_required_view = "ca.resolve_chemical_batch_hold"

    def get_hold(self):
        if not hasattr(self, "_hold"):
            self._hold = get_object_or_404(
                _scope_queryset(
                    ChemicalBatchHold.objects.select_related("batch", "batch__chemical", "batch__treatment_plant"),
                    self.request.user,
                    "batch__treatment_plant_id",
                    plant_id=self.get_scope_plant_id(),
                ),
                pk=self.kwargs["hold_id"],
                batch_id=self.kwargs["pk"],
            )
        return self._hold

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(user=self.request.user, hold=self.get_hold())
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["hold"] = self.get_hold()
        return context

    def form_valid(self, form):
        from .services.workflow_enhancements import resolve_batch_hold

        resolve_batch_hold(
            self.get_hold(),
            actor=self.request.user,
            resolution=form.cleaned_data["resolution"],
            reject=form.cleaned_data["reject_batch"],
        )
        messages.success(self.request, "Batch hold disposition recorded.")
        return redirect("ca:batch-detail", pk=self.get_hold().batch_id)


class ChemicalBatchCustodyRecordView(CaWanjalaMixin, FormView):
    template_name = "ca/chema/record_custody.html"
    form_class = ChemicalCustodyRecordForm
    permission_required_view = "ca.record_chemical_custody"

    def get_batch(self):
        if not hasattr(self, "_batch"):
            self._batch = _get_batch_or_404(
                self.request.user,
                self.kwargs["pk"],
                plant_id=self.get_scope_plant_id(),
            )
        return self._batch

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(user=self.request.user, batch=self.get_batch())
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["batch"] = self.get_batch()
        return context

    def form_valid(self, form):
        batch = self.get_batch()
        data = form.cleaned_data
        with transaction.atomic():
            ChemicalCustodyEvent.objects.create(
                batch=batch,
                event_type=ChemicalCustodyEvent.EventType.TRANSFERRED,
                actor=self.request.user,
                from_location=data["from_location"],
                to_location=data["to_location"],
                seal_number=data["seal_number"],
                evidence=data["evidence"],
                remarks=data["remarks"],
            )
            location = ChemicalLocation.objects.filter(
                treatment_plant_id=batch.treatment_plant_id,
                name__iexact=data["to_location"].strip(),
            ).first()
            if location:
                batch.location = location
                batch.save(update_fields=["location", "updated_at"])
            process = getattr(batch, "analysis_process", None)
            if process:
                from .services.workflow_enhancements import record_workflow_event

                record_workflow_event(
                    process,
                    event_type="custody_transfer",
                    action="batch transferred",
                    actor=self.request.user,
                    from_status=process.status,
                    to_status=process.status,
                    details={"from": data["from_location"], "to": data["to_location"], "seal": data["seal_number"]},
                )
        messages.success(self.request, "Chain-of-custody transfer recorded.")
        return redirect("ca:batch-detail", pk=batch.pk)


class ChemicalBatchCreateView(
    CaWanjalaMixin,
    CreateView,
):
    model = ChemicalBatch
    form_class = ChemicalBatchForm
    template_name = "ca/chema/batch_form.html"
    permission_required_view = CHEMICAL_PERMISSIONS["receive"]

    def get(self, request, *args, **kwargs):
        # Batch intake is presented in the shared CA modal. Keep this route as
        # a modal launcher for bookmarks and non-JavaScript navigation; AJAX
        # prerequisite lookups still receive the form markup they request.
        if self._wants_json_response():
            return super().get(request, *args, **kwargs)
        return redirect(f"{reverse('ca:dashboard')}?open_receive_batch=1")

    def post(self, request, *args, **kwargs):
        try:
            return super().post(request, *args, **kwargs)
        except PermissionDenied as exc:
            logger.warning(
                "Chemical batch intake permission denied",
                extra={"user_id": getattr(request.user, "pk", None)},
            )
            if self._wants_json_response():
                return JsonResponse(
                    {"status": "error", "message": str(exc)},
                    status=403,
                )
            raise
        except Exception as exc:
            logger.exception(
                "Chemical batch intake request failed",
                extra={
                    "user_id": getattr(request.user, "pk", None),
                    "submitted_fields": sorted(request.POST.keys()),
                },
            )
            if self._wants_json_response():
                payload = {
                    "status": "error",
                    "message": "The batch could not be saved. Check the details and try again.",
                    "error_type": type(exc).__name__,
                }
                if settings.DEBUG:
                    payload["debug_details"] = str(exc)[:2000]
                return JsonResponse(payload, status=500)
            raise

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        kwargs["treatment_plant"] = self.get_selected_plant()
        kwargs["is_superuser"] = self.is_ca_superuser()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["user_assigned_plant"] = getattr(user, "assigned_treatment_plant", None)
        context["is_ca_superuser"] = self.is_ca_superuser()
        context["chemicals"] = list(Chemical.objects.order_by("name"))
        context["suppliers"] = list(Supplier.objects.order_by("name"))
        context["chemical_locations"] = list(
            context["form"].fields["location"].queryset
        )
        return context

    def form_valid(self, form):
        user = self.request.user

        if not self.is_ca_superuser():
            form.instance.treatment_plant_id = (
                getattr(user, "assigned_treatment_plant_id", None)
            )
            if form.instance.treatment_plant_id is None:
                raise PermissionDenied(
                    "Your account is not assigned to a treatment plant."
                )
        elif not form.instance.treatment_plant_id and getattr(user, "assigned_treatment_plant_id", None):
            form.instance.treatment_plant_id = user.assigned_treatment_plant_id

        # Receipt is confirmed by this intake form, so persist the batch in
        # its received state before creating its analysis request and flow.
        form.instance.status = ChemicalBatch.Status.RECEIVED
        form.instance.received_date = form.instance.received_date or timezone.localdate()

        if not form.instance.received_by_id:
            form.instance.received_by = user

        is_ajax = self._wants_json_response()

        # Intake starts the analysis workflow. Keep all submitted batch fields
        # and its generated request in one transaction.
        try:
            with transaction.atomic():
                # WanjalaMixin.form_valid() is intended for generic CRUD
                # partials and attempts to render row_template_name for AJAX
                # requests. Batch intake has its own JSON response, so save
                # through the normal hooks and let this view return that JSON.
                self.action = "create"
                self.before_save(form)
                self.object = form.save()
                self.after_save(self.object)
                from .services.workflow_enhancements import (
                    record_custody_event,
                    record_intake_holds,
                    record_inventory_movement,
                )

                record_inventory_movement(
                    self.object,
                    movement_type="RECEIPT",
                    quantity=self.object.quantity_received,
                    actor=user,
                    idempotency_key=f"batch:{self.object.pk}:receipt",
                    notes=f"Delivery note {self.object.delivery_note_number}",
                )
                record_custody_event(
                    self.object,
                    event_type="RECEIVED",
                    actor=user,
                    from_location=str(self.object.supplier),
                    to_location=str(self.object.location),
                    remarks=self.object.remarks,
                )
                holds = record_intake_holds(
                    self.object,
                    issues=form.cleaned_data.get("intake_discrepancies"),
                    details=form.cleaned_data.get("hold_reason"),
                    actor=user,
                )
                analysis_request, _ = ChemicalAnalysisRequest.objects.get_or_create(
                    batch=self.object,
                    defaults={
                        "delivery_date": self.object.received_date or timezone.localdate(),
                        "requested_by": user,
                    },
                )
                from .chemical_flow import start_chemical_analysis_for_batch

                analysis_process = start_chemical_analysis_for_batch(
                    self.object,
                    analysis_request,
                    user,
                    self.request,
                )
                from .services.workflow_enhancements import record_workflow_event

                record_workflow_event(
                    analysis_process,
                    event_type="batch_intake",
                    action="batch_received",
                    actor=user,
                    to_status=analysis_process.status,
                    details={
                        "analysis_request_id": analysis_request.pk,
                        "hold_ids": [hold.pk for hold in holds],
                        "held": bool(holds),
                    },
                )

                # Persist a recipient-scoped batch receipt alert. The workflow
                # signal generates and links the appropriate voice clone once.
                _batch = self.object
                _hold_suffix = (
                    f" NOTE: {len(holds)} intake discrepanc{'y' if len(holds) == 1 else 'ies'} recorded. Batch placed on hold."
                    if holds else ""
                )
                _voice_msg = (
                    f"Chemical batch {_batch.batch_number} received. "
                    f"Chemical: {_batch.chemical.name}. "
                    f"Plant: {_batch.treatment_plant}. "
                    f"Quantity: {_batch.quantity_received} {getattr(_batch, 'unit', '')}."
                    f"{_hold_suffix}"
                )
                _voice_title = f"Chemical Batch Received – {_batch.batch_number}"
                from .services.workflow_notifications import create_workflow_alert
                create_workflow_alert(
                    batch=_batch,
                    request=analysis_request,
                    alert_type="ca_batch_received",
                    title=_voice_title,
                    message=_voice_msg,
                    url=f"/ca/batches/{_batch.pk}/",
                    severity="high" if holds else "medium",
                    dedupe_key=f"batch-received:{_batch.pk}",
                    permissions=("ca.receive_chemical_batch",),
                )

                response = redirect(self.get_success_url())

        except ValidationError as exc:
            logger.warning(
                "Chemical batch intake validation failed",
                extra={
                    "user_id": getattr(user, "pk", None),
                    "submitted_fields": sorted(self.request.POST.keys()),
                    "validation_errors": getattr(exc, "messages", [str(exc)]),
                },
            )
            if is_ajax:
                errors = getattr(exc, "message_dict", None)
                message = "; ".join(
                    str(error)
                    for error in getattr(exc, "messages", [str(exc)])
                )
                return JsonResponse(
                    {
                        "status": "error",
                        "message": message or "The batch could not be saved.",
                        "errors": errors or {},
                    },
                    status=400,
                )
            raise

        if is_ajax:
            return JsonResponse({
                "status": "ok",
                "message": f"Chemical batch {self.object.batch_number} received successfully!",
                "batch_id": self.object.pk,
                "sample_id": self.object.sample_id,
                "analysis_request_id": analysis_request.pk,
                "request_id": analysis_request.request_id,
                "process_id": analysis_process.pk,
                "on_hold": bool(holds),
                "hold_count": len(holds),
                "notification": ({
                    "id": f"ca_assignment_pending_{analysis_request.pk}",
                    "type": "ca_assignment_pending",
                    "title": "Analysis request needs an analyst",
                    "message": f"Assign an analyst to {self.object.chemical.name} batch {self.object.batch_number}.",
                    "url": reverse(
                        "ca:request-assign-analyst",
                        kwargs={"pk": analysis_request.pk},
                    ),
                } if (
                    self.is_ca_superuser()
                    or user.has_perm("ca.create_analysis_request")
                ) else None),
                "redirect_url": self.get_success_url(),
            })

        return response

    def form_invalid(self, form):
        logger.warning(
            "Chemical batch intake form validation failed",
            extra={
                "user_id": getattr(self.request.user, "pk", None),
                "submitted_fields": sorted(self.request.POST.keys()),
                "form_errors": form.errors.get_json_data(),
            },
        )
        if self._wants_json_response():
            non_field_errors = form.non_field_errors()
            message = (
                str(non_field_errors[0])
                if non_field_errors
                else "Please correct the highlighted errors."
            )
            return JsonResponse({
                "status": "error",
                "message": message,
                "errors": form.errors.get_json_data(),
            }, status=400)

        return super().form_invalid(form)

    def _wants_json_response(self):
        return (
            self.request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in self.request.headers.get("Accept", "")
            or self.request.POST.get("is_ajax") == "1"
        )

    def get_success_url(self):
        return reverse(
            "ca:batch-detail",
            kwargs={
                "pk": self.object.pk
            },
        )


class ChemicalBatchDeleteView(CaWanjalaMixin, TemplateView):
    """Confirm and audit a superuser hard-delete of a chemical batch."""

    template_name = "ca/chema/batch_delete_confirm.html"
    permission_required_view = "ca.view_chemicalbatch"

    def _require_superuser(self):
        if not self.is_ca_superuser():
            raise PermissionDenied("Only a superuser can delete chemical batches.")

    def _get_batch(self):
        return _get_batch_or_404(
            self.request.user,
            self.kwargs["pk"],
            plant_id=self.get_scope_plant_id(),
        )

    def get(self, request, *args, **kwargs):
        self._require_superuser()
        self.object = self._get_batch()
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["batch"] = getattr(self, "object", None) or self._get_batch()
        return context

    def post(self, request, *args, **kwargs):
        self._require_superuser()
        batch = self._get_batch()
        batch_label = batch.sample_id or batch.batch_number
        from .services.superuser_deletion import delete_ca_objects

        delete_ca_objects([batch], actor=request.user)
        messages.success(request, f"Chemical batch {batch_label} and its dependent records were permanently deleted.")
        return redirect("ca:batch-list")


class ChemicalBatchUpdateView(
    CaWanjalaMixin,
    UpdateView,
):
    model = ChemicalBatch
    form_class = ChemicalBatchForm
    template_name = "ca/chema/batch_form.html"
    permission_required_view = "ca.change_chemicalbatch"

    def get_object(self, queryset=None):
        return _get_batch_or_404(
            self.request.user,
            self.kwargs["pk"],
            plant_id=self.get_scope_plant_id(),
        )

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        kwargs["is_superuser"] = self.is_ca_superuser()
        return kwargs

    def form_valid(self, form):
        user = self.request.user

        if not self.is_ca_superuser():
            plant_id = getattr(user, "assigned_treatment_plant_id", None)

            if plant_id is None:
                raise PermissionDenied(
                    "Your account is not assigned to a treatment plant."
                )

            if form.instance.treatment_plant_id != plant_id:
                raise PermissionDenied(
                    "The chemical batch belongs to another treatment plant."
                )

        return super().form_valid(form)

    def get_success_url(self):
        return reverse(
            "ca:batch-detail",
            kwargs={
                "pk": self.object.pk
            },
        )


class ChemicalAnalysisRequestListView(
    CaWanjalaMixin,
    ListView,
):
    model = ChemicalAnalysisRequest
    template_name = "ca/chema/request_list.html"
    context_object_name = "requests"
    permission_required_view = "ca.view_chemicalanalysisrequest"
    paginate_by = 50

    def get_queryset(self):
        queryset = _request_display_queryset(
            self.request.user
        )

        status = self.request.GET.get(
            "status"
        )

        if status:
            queryset = queryset.filter(
                status=status
            )

        assignment = self.request.GET.get(
            "assignment"
        )

        if assignment == "pending":
            queryset = queryset.filter(
                assigned_analysts__isnull=True
            )

        elif assignment == "assigned":
            queryset = queryset.filter(
                assigned_analysts__isnull=False
            )

        elif assignment == "overdue":
            queryset = queryset.filter(
                status__in=(
                    ChemicalAnalysisRequest.Status.PENDING,
                    ChemicalAnalysisRequest.Status.IN_PROGRESS,
                ),
                assigned_analysts__isnull=True,
            )

        query = self.request.GET.get("q")

        if query:
            queryset = queryset.filter(
                Q(
                    batch__sample_id__icontains=query
                )
                | Q(
                    batch__batch_number__icontains=query
                )
                | Q(
                    batch__chemical__name__icontains=query
                )
            )

        return queryset.distinct()


class ChemicalAnalysisRequestDetailView(
    CaWanjalaMixin,
    DetailView,
):
    model = ChemicalAnalysisRequest
    template_name = "ca/chema/request_detail.html"
    context_object_name = "analysis_request"
    permission_required_view = "ca.view_chemicalanalysisrequest"

    def get_object(self, queryset=None):
        return _get_request_or_404(
            self.request.user,
            self.kwargs["pk"],
            plant_id=self.get_scope_plant_id(),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(
            **kwargs
        )

        request_obj = self.object
        user = self.request.user

        process = (
            _process_display_queryset(user, plant_id=self.get_scope_plant_id())
            .filter(
                analysis_request=request_obj
            )
            .first()
        )

        checklist = (
            _checklist_queryset(user, plant_id=self.get_scope_plant_id())
            .filter(
                chemical_analysis_request=request_obj
            )
            .first()
        )

        context.update(
            {
                "process": process,
                "checklist": checklist,
                "assignment": _request_assignment_data(
                    request_obj
                ),
                "assigned_analysts": (
                    request_obj.assigned_analysts.all()
                ),
                "assignment_pending": bool(
                    getattr(
                        request_obj,
                        "assignment_pending",
                        False,
                    )
                ),
                "assignment_overdue": bool(
                    getattr(
                        request_obj,
                        "assignment_overdue",
                        False,
                    )
                ),
            }
        )

        return context


class ChemicalAnalysisRequestAssignAnalystView(CaWanjalaMixin, FormView):
    """Offer a permission-checked analyst assignment dialog or page fallback."""
    template_name = "ca/chema/assign_analyst.html"
    form_class = AnalystAssignmentForm
    permission_required_view = CHEMICAL_PERMISSIONS["request"]

    def is_dialog_request(self):
        return (
            self.request.GET.get("dialog") == "1"
            or self.request.headers.get("X-CA-Dialog") == "1"
        )

    def get_template_names(self):
        if self.is_dialog_request():
            return ["ca/chema/partials/assign_analyst_form.html"]
        return super().get_template_names()

    def get_analysis_request(self):
        if not hasattr(self, "_analysis_request"):
            self._analysis_request = _get_request_or_404(
                self.request.user,
                self.kwargs["pk"],
                plant_id=self.get_scope_plant_id(),
            )
        return self._analysis_request

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        # The request exposes the same batch and assignment relations used by
        # AnalystAssignmentForm; this also works when no workflow process exists.
        kwargs.update(process=self.get_analysis_request(), user=self.request.user)
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        analysis_request = self.get_analysis_request()
        context["analysis_request"] = analysis_request
        context["dialog_mode"] = self.is_dialog_request()
        if analysis_request.batch_id:
            context["assignment_cancel_url"] = (
                f"{reverse('ca:batch-detail', kwargs={'pk': analysis_request.batch_id})}"
                "?stage=assignment"
            )
        else:
            context["assignment_cancel_url"] = reverse(
                "ca:request-detail", kwargs={"pk": analysis_request.pk}
            )
        return context

    def form_invalid(self, form):
        response = super().form_invalid(form)
        if self.is_dialog_request():
            # Let the dialog client distinguish a rendered validation result
            # from a successful save while preserving Django's bound form.
            response.status_code = 422
        return response

    def form_valid(self, form):
        analysis_request = self.get_analysis_request()
        try:
            analysts = form.cleaned_data["analysts"]
            # assign_analysts acquires its own row lock and transaction;
            # keeping the assignment in a separate atomic block from the
            # downstream workflow helpers prevents the combined lock hold
            # time from triggering deadlocks with the periodic Celery task.
            analysis_request.assign_analysts(
                analysts,
                assigned_by=self.request.user,
                manual=True,
                _form_validated=True,
            )
            process = (
                ChemicalAnalysisProcess.objects
                .filter(analysis_request=analysis_request)
                .first()
            )
            selected_analyst_ids = {user.pk for user in analysts}
            if (
                process
                and process.status in {
                    ChemicalAnalysisProcess.WorkflowStatus.RECEIVED,
                    ChemicalAnalysisProcess.WorkflowStatus.REQUESTED,
                    ChemicalAnalysisProcess.WorkflowStatus.ASSIGNED,
                    ChemicalAnalysisProcess.WorkflowStatus.CHECKLIST,
                    ChemicalAnalysisProcess.WorkflowStatus.ANALYSIS,
                    ChemicalAnalysisProcess.WorkflowStatus.RETEST,
                }
                and process.assigned_analyst_id not in selected_analyst_ids
            ):
                process.assigned_analyst = next(iter(analysts), None)
                process.save(update_fields=["assigned_analyst", "updated_at"])
            if (
                process is None
                and analysis_request.batch_id
                and analysis_request.batch.status == ChemicalBatch.Status.RECEIVED
            ):
                from .chemical_flow import start_chemical_analysis_for_batch

                process = start_chemical_analysis_for_batch(
                    analysis_request.batch,
                    analysis_request,
                    self.request.user,
                    self.request,
                )
            if process:
                from .chemical_flow import advance_analyst_assignment_workflow

                advance_analyst_assignment_workflow(
                    process,
                    assigned_by=self.request.user,
                    http_request=self.request,
                )
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.form_invalid(form)
        except PermissionDenied as exc:
            form.add_error(None, str(exc) or "You no longer have permission to assign analysts.")
            return self.form_invalid(form)
        except Exception:
            logger.exception(
                "Unable to assign analysts for chemical analysis request %s",
                analysis_request.pk,
            )
            form.add_error(
                None,
                "The assignment could not be saved. Your selections are still here; please try again or contact an administrator if the problem continues.",
            )
            return self.form_invalid(form)
        messages.success(self.request, "Analyst assignment or reassignment saved successfully.")
        if analysis_request.batch_id:
            stage = next_stage_key(process) if process else "assignment"
            success_url = (
                f"{reverse('ca:batch-detail', kwargs={'pk': analysis_request.batch_id})}"
                f"?stage={stage}"
            )
        else:
            success_url = (
                f"{reverse('ca:request-detail', kwargs={'pk': analysis_request.pk})}"
                "?assignment=complete"
            )
        if self.is_dialog_request():
            return JsonResponse({"success": True, "redirect_url": success_url})
        return redirect(success_url)



class ProcessListView(
    CaWanjalaMixin,
    ListView,
):
    model = ChemicalAnalysisProcess
    template_name = "ca/process/process_list.html"
    context_object_name = "processes"
    permission_required_view = CHEMICAL_PERMISSIONS["view_workflow"]
    paginate_by = 50

    def get_queryset(self):
        queryset = _process_display_queryset(
            self.request.user
        )

        status = self.request.GET.get(
            "status"
        )

        if status:
            queryset = queryset.filter(
                status=status
            )

        query = self.request.GET.get("q")

        if query:
            queryset = queryset.filter(
                Q(
                    batch__sample_id__icontains=query
                )
                | Q(
                    batch__batch_number__icontains=query
                )
                | Q(
                    batch__chemical__name__icontains=query
                )
            )

        assignment = self.request.GET.get(
            "assignment"
        )

        if assignment == "assigned":
            queryset = queryset.filter(
                analysis_request__assigned_analysts__isnull=False
            )

        elif assignment == "unassigned":
            queryset = queryset.filter(
                analysis_request__assigned_analysts__isnull=True
            )

        return queryset.distinct()


class ProcessDetailView(
    CaWanjalaMixin,
    DetailView,
):
    model = ChemicalAnalysisProcess
    template_name = "ca/process/process_detail.html"
    context_object_name = "process"
    permission_required_view = CHEMICAL_PERMISSIONS["view_workflow"]

    def get_object(self, queryset=None):
        return _get_process_or_404(
            self.request.user,
            self.kwargs["pk"],
            plant_id=self.get_scope_plant_id(),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(
            **kwargs
        )

        process = self.object
        user = self.request.user

        results = (
            _result_display_queryset(user, plant_id=self.get_scope_plant_id())
            .filter(batch=process.batch)
        )

        inputs = (
            _analysis_input_queryset(user, plant_id=self.get_scope_plant_id())
            .filter(
                test_result__batch=process.batch
            )
            .order_by("-pk")
        )

        coa = (
            _coa_display_queryset(user, plant_id=self.get_scope_plant_id())
            .filter(batch=process.batch)
            .first()
        )

        analysis_request = (
            getattr(
                process,
                "analysis_request",
                None,
            )
        )

        assigned_analysts = []

        if analysis_request:
            assigned_analysts = list(
                analysis_request.assigned_analysts.all()
            )

        # Keep the process detail useful as a guided workflow landing page.
        # The Viewflow task queue owns execution; these prompts tell each role
        # what must happen next and send them to the queue that exposes the
        # actionable task for this process.
        workflow_prompts = {
            ChemicalAnalysisProcess.WorkflowStatus.CREATED: (
                "Confirm that the received batch and its delivery details are correct.",
                "Open workflow tasks to complete chemical receipt.",
                "ca:my-tasks",
            ),
            ChemicalAnalysisProcess.WorkflowStatus.RECEIVED: (
                "Complete the receiving checklist and confirm the analysis request and analyst assignment.",
                "Open workflow tasks to continue intake.",
                "ca:my-tasks",
            ),
            ChemicalAnalysisProcess.WorkflowStatus.REQUESTED: (
                "Complete the receiving checklist, then assign one or more permitted analysts.",
                "Open workflow tasks to continue intake.",
                "ca:my-tasks",
            ),
            ChemicalAnalysisProcess.WorkflowStatus.ASSIGNED: (
                "Finish the receiving checklist if it is still open. The assigned analyst can then start testing.",
                "Open workflow tasks to continue this batch.",
                "ca:my-tasks",
            ),
            ChemicalAnalysisProcess.WorkflowStatus.CHECKLIST: (
                "Verify the completed receiving checklist, then start analysis as an assigned analyst.",
                "Open workflow tasks to start analysis.",
                "ca:my-tasks",
            ),
            ChemicalAnalysisProcess.WorkflowStatus.ANALYSIS: (
                "Record every required specification result and its required method inputs. Complete analysis when all are finalized.",
                "Open workflow tasks to record results.",
                "ca:pending-analysis",
            ),
            ChemicalAnalysisProcess.WorkflowStatus.RETEST: (
                "Record the requested retest result and method inputs, then submit the retest for chemist review.",
                "Open workflow tasks to continue the retest.",
                "ca:pending-analysis",
            ),
            ChemicalAnalysisProcess.WorkflowStatus.QA: (
                "Review the completed results and accept, request a retest, require CAPA, or reject with the required rationale.",
                "Open workflow tasks to review this analysis.",
                "ca:pending-qa",
            ),
            ChemicalAnalysisProcess.WorkflowStatus.APPROVED: (
                "Generate and approve the Certificate of Analysis, then record whether the batch is utilized or returned.",
                "Open workflow tasks to issue the CoA and record disposition.",
                "ca:my-tasks",
            ),
            ChemicalAnalysisProcess.WorkflowStatus.REJECTED: (
                "Record the return quantity and reason, then confirm return to supplier to close the process.",
                "Open workflow tasks to complete the supplier return.",
                "ca:my-tasks",
            ),
            ChemicalAnalysisProcess.WorkflowStatus.UTILIZED: (
                "Batch utilization is recorded. Complete the final workflow task to close the process.",
                "Open workflow tasks to close this process.",
                "ca:my-tasks",
            ),
            ChemicalAnalysisProcess.WorkflowStatus.RETURNED: (
                "Supplier return is recorded. Complete the final workflow task to close the process.",
                "Open My Tasks to close this process.",
                "ca:my-tasks",
            ),
        }
        prompt = workflow_prompts.get(process.status)
        workflow_prompt = None
        if prompt:
            workflow_prompt = {
                "body": prompt[0],
                "action": prompt[1],
                "url": "/ca/workflow/",
                "complete": False,
            }
        elif process.status == ChemicalAnalysisProcess.WorkflowStatus.COMPLETED:
            workflow_prompt = {
                "body": "This chemical batch has completed the full workflow cycle.",
                "action": "View the batch record or its Certificate of Analysis.",
                "url": reverse("ca:batch-detail", kwargs={"pk": process.batch_id}),
                "complete": True,
            }

        if workflow_prompt and not workflow_prompt["complete"]:
            active_task_statuses = {"NEW", "ASSIGNED", "STARTED"}
            active_tasks = sorted(
                (
                    task
                    for task in process.task_set.all()
                    if task.status in active_task_statuses and task.flow_task
                ),
                key=lambda task: (task.created, task.pk),
            )
            for task in active_tasks:
                node = task.flow_task
                try:
                    task_is_available = (
                        node.can_execute(user, task)
                        or node.can_assign(user, task)
                        or node.can_view(user, task)
                    )
                except AttributeError:
                    task_is_available = False
                if task_is_available:
                    workflow_prompt["url"] = node.reverse(
                        "index", args=[process.pk, task.pk]
                    )
                    workflow_prompt["action"] = f"Continue: {task.title or node}"
                    break

        context.update(
            {
                "results": results,
                "inputs": inputs,
                "sampling": None,
                "coa": coa,
                "pending_tests": process.pending_tests,
                "failed_tests": process.failed_tests,
                "is_analysis_complete": bool(
                    process.is_analysis_complete
                ),
                "assigned_analysts": assigned_analysts,
                "has_assigned_analysts": bool(
                    assigned_analysts
                ),
                "assignment_pending": bool(
                    getattr(
                        analysis_request,
                        "assignment_pending",
                        False,
                    )
                )
                if analysis_request
                else False,
                "assignment_overdue": bool(
                    getattr(
                        analysis_request,
                        "assignment_overdue",
                        False,
                    )
                )
                if analysis_request
                else False,
                "workflow_prompt": workflow_prompt,
                "corrective_actions": process.corrective_actions.select_related(
                    "submitted_by", "reviewed_by"
                ).all(),
            }
        )

        return context


class ProcessJSONView(
    CaWanjalaMixin,
    View,
):
    permission_required_view = CHEMICAL_PERMISSIONS["view_workflow"]

    def get(self, request, *args, **kwargs):
        process = _get_process_or_404(
            request.user,
            kwargs["pk"],
            plant_id=self.get_scope_plant_id(),
        )

        results = list(
            _result_queryset(request.user)
            .filter(batch=process.batch)
            .values(
                "id",
                "parameter_id",
                "parameter__parameter_name",
                "status",
                "tested_at",
                "analyst_id",
                "value",
                "textual_result",
                "auto_remark",
            )
        )

        analysis_request = (
            getattr(
                process,
                "analysis_request",
                None,
            )
        )

        assignment = (
            _request_assignment_data(
                analysis_request
            )
            if analysis_request
            else {
                "assigned_analyst_ids": [],
                "assigned_analysts": [],
                "assigned_analyst_count": 0,
                "assignment_pending": False,
                "assignment_overdue": False,
                "manual_assigned_at": None,
                "auto_assigned_at": None,
                "analysis_deadline": None,
            }
        )

        coa = (
            _coa_display_queryset(request.user, plant_id=self.get_scope_plant_id())
            .filter(batch=process.batch)
            .first()
        )

        integrity_status = (
            getattr(
                coa,
                "integrity_status",
                None,
            )
            if coa
            else None
        )

        return JsonResponse(
            {
                "id": process.pk,
                "status": process.status,
                "batch_id": process.batch_id,
                "analysis_request_id": (
                    process.analysis_request_id
                ),
                "assigned_analyst_id": (
                    process.assigned_analyst_id
                ),
                "assigned_analysts": assignment[
                    "assigned_analysts"
                ],
                "assigned_analyst_ids": assignment[
                    "assigned_analyst_ids"
                ],
                "assigned_analyst_count": assignment[
                    "assigned_analyst_count"
                ],
                "assignment_pending": assignment[
                    "assignment_pending"
                ],
                "assignment_overdue": assignment[
                    "assignment_overdue"
                ],
                "manual_assigned_at": assignment[
                    "manual_assigned_at"
                ],
                "auto_assigned_at": assignment[
                    "auto_assigned_at"
                ],
                "analysis_deadline": assignment[
                    "analysis_deadline"
                ],
                "qa_officer_id": (
                    process.qa_officer_id
                ),
                "approver_id": (
                    process.approver_id
                ),
                "analysis_started_at": (
                    process.analysis_started_at.isoformat()
                    if process.analysis_started_at
                    else None
                ),
                "analysis_completed_at": (
                    process.analysis_completed_at.isoformat()
                    if process.analysis_completed_at
                    else None
                ),
                "is_analysis_complete": bool(
                    process.is_analysis_complete
                ),
                "has_pending_tests": bool(
                    process.has_pending_tests
                ),
                "pending_test_count": (
                    process.pending_tests.count()
                    if hasattr(
                        process.pending_tests,
                        "count",
                    )
                    else len(
                        process.pending_tests
                    )
                ),
                "failed_test_count": (
                    process.failed_tests.count()
                    if hasattr(
                        process.failed_tests,
                        "count",
                    )
                    else len(
                        process.failed_tests
                    )
                ),
                "coa": (
                    {
                        "id": coa.pk,
                        "is_approved": bool(
                            coa.is_approved
                        ),
                        "is_verified": (
                            integrity_status
                            == "VALID"
                        ),
                        "integrity_status": (
                            integrity_status
                        ),
                        "generation_status": getattr(
                            coa,
                            "generation_status",
                            None,
                        ),
                        "pdf_available": bool(
                            coa.pdf_file
                            and coa.pdf_file.name
                        ),
                        "qr_available": bool(
                            coa.qr_code
                            and coa.qr_code.name
                        ),
                    }
                    if coa
                    else None
                ),
                "results": results,
            }
        )


class ChemicalAnalysisInputDetailView(
    CaWanjalaMixin,
    DetailView,
):
    model = ChemicalAnalysisInput
    template_name = "ca/chema/analysis_input_detail.html"
    context_object_name = "analysis_input"
    permission_required_view = CHEMICAL_PERMISSIONS["view_workflow"]

    def get_object(self, queryset=None):
        queryset = _analysis_input_queryset(
            self.request.user
        )

        return get_object_or_404(
            queryset,
            pk=self.kwargs["pk"],
        )


class RecalculateAnalysisInputView(
    CaWanjalaMixin,
    View,
):
    permission_required_view = CHEMICAL_PERMISSIONS["result"]

    def post(self, request, *args, **kwargs):
        try:
            with transaction.atomic():
                analysis_input = get_object_or_404(
                    _analysis_input_queryset(
                        request.user,
                        plant_id=self.get_scope_plant_id()
                    )
                    .select_for_update(of=("self",)),
                    pk=kwargs["pk"],
                )

                recalculator = getattr(
                    analysis_input,
                    "recalculate",
                    None,
                )

                if not callable(recalculator):
                    calculate = getattr(
                        analysis_input,
                        "calculate",
                        None,
                    )

                    if not callable(calculate):
                        raise ValidationError(
                            "This analysis input does not support recalculation."
                        )

                    analysis_input.calculated_value = calculate()
                    analysis_input.save(
                        update_fields=["calculated_value"]
                    )
                else:
                    recalculator()

                analysis_input.refresh_from_db()
        except ValidationError as exc:
            messages.error(
                request,
                "Analysis input could not be recalculated: "
                + "; ".join(str(message) for message in exc.messages),
            )
            return redirect(
                reverse("ca:analysis-input-detail", kwargs={"pk": kwargs["pk"]})
            )

        messages.success(
            request,
            "Analysis input recalculated successfully.",
        )

        return redirect(
            reverse(
                "ca:analysis-input-detail",
                kwargs={
                    "pk": analysis_input.pk
                },
            )
        )


class ChemicalSpecificationListView(
    CaWanjalaMixin,
    ListView,
):
    model = ChemicalSpecification
    template_name = "ca/chema/specification_list.html"
    context_object_name = "specifications"
    permission_required_view = "ca.view_chemicalspecification"
    paginate_by = 50

    def get_queryset(self):
        queryset = _specification_queryset(
            self.request.user
        )

        chemical = self.request.GET.get(
            "chemical"
        )

        parameter = self.request.GET.get(
            "parameter"
        )

        if chemical:
            queryset = queryset.filter(
                chemical__name__icontains=chemical
            )

        if parameter:
            queryset = queryset.filter(
                parameter_name__icontains=parameter
            )

        return queryset


class ChemicalSpecificationFilterView(
    CaWanjalaMixin,
    View,
):
    permission_required_view = CHEMICAL_PERMISSIONS["view_workflow"]

    def get(self, request, *args, **kwargs):
        form = ChemicalSpecificationFilterForm(
            request.GET or None,
            user=request.user,
        )

        queryset = _specification_queryset(
            request.user
        )

        if form.is_valid():
            chemical = form.cleaned_data.get(
                "chemical"
            )

            treatment_plant = (
                form.cleaned_data.get(
                    "treatment_plant"
                )
            )

            parameter = (
                form.cleaned_data.get(
                    "parameter_name"
                )
            )

            if chemical:
                queryset = queryset.filter(
                    chemical=chemical
                )

            if treatment_plant:
                queryset = queryset.filter(
                    treatment_plant=treatment_plant
                )

            if parameter:
                queryset = queryset.filter(
                    parameter_name__icontains=parameter
                )

        return JsonResponse(
            {
                "results": [
                    {
                        "id": specification.pk,
                        "chemical": (
                            specification.chemical.name
                        ),
                        "parameter": (
                            specification.parameter_name
                        ),
                        "unit": specification.unit,
                        "minimum": (
                            str(
                                specification.minimum
                            )
                            if specification.minimum
                            is not None
                            else None
                        ),
                        "maximum": (
                            str(
                                specification.maximum
                            )
                            if specification.maximum
                            is not None
                            else None
                        ),
                        "version": (
                            specification.version
                        ),
                    }
                    for specification in queryset[:500]
                ]
            }
        )


class ResultListView(
    CaWanjalaMixin,
    ListView,
):
    model = BatchTestResult
    template_name = "ca/chema/result_list.html"
    context_object_name = "results"
    permission_required_view = "ca.view_batchtestresult"
    paginate_by = 50

    def get_queryset(self):
        queryset = _result_display_queryset(
            self.request.user
        )

        status = self.request.GET.get(
            "status"
        )

        if status:
            queryset = queryset.filter(
                status=status
            )

        query = self.request.GET.get("q")

        if query:
            queryset = queryset.filter(
                Q(
                    batch__sample_id__icontains=query
                )
                | Q(
                    parameter__parameter_name__icontains=query
                )
                | Q(
                    batch__chemical__name__icontains=query
                )
            )

        form = TestResultFilterForm(
            self.request.GET or None,
            user=self.request.user,
        )

        if form.is_valid():
            parameter = form.cleaned_data.get(
                "parameter"
            )

            if parameter:
                queryset = queryset.filter(
                    parameter=parameter
                )

        return queryset


class ResultDetailView(
    CaWanjalaMixin,
    DetailView,
):
    model = BatchTestResult
    template_name = "ca/chema/result_detail.html"
    context_object_name = "result"
    permission_required_view = "ca.view_batchtestresult"

    def get_object(self, queryset=None):
        return _get_result_or_404(
            self.request.user,
            self.kwargs["pk"],
            plant_id=self.get_scope_plant_id(),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(
            **kwargs
        )

        context["analysis_input"] = (
            _analysis_input_queryset(
                self.request.user,
                plant_id=self.get_scope_plant_id()
            )
            .filter(
                test_result=self.object
            )
            .first()
        )
        batch = self.object.batch
        context["process"] = (
            getattr(batch, "analysis_process", None)
            if batch is not None
            else None
        )

        return context


class ChemicalCoAListView(
    CaWanjalaMixin,
    ListView,
):
    model = ChemicalCoA
    template_name = "ca/chema/coa_list.html"
    context_object_name = "coas"
    permission_required_view = "ca.view_chemicalcoa"
    paginate_by = 50

    def get_queryset(self):
        queryset = _coa_display_queryset(
            self.request.user
        )

        query = self.request.GET.get("q")

        if query:
            queryset = queryset.filter(
                Q(
                    batch__sample_id__icontains=query
                )
                | Q(
                    batch__batch_number__icontains=query
                )
                | Q(
                    batch__chemical__name__icontains=query
                )
                | Q(
                    batch__supplier__name__icontains=query
                )
            )

        status = self.request.GET.get(
            "status"
        )

        if status == "approved":
            queryset = queryset.filter(
                is_approved=True
            )

        elif status == "pending":
            queryset = queryset.filter(
                is_approved=False
            )

        elif status == "verified":
            queryset = queryset.filter(
                verified_hash__isnull=False
            )

        elif status == "unverified":
            queryset = queryset.filter(
                Q(verified_hash__isnull=True)
                | Q(verified_hash="")
            )

        return queryset


class ChemicalCoADetailView(
    CaWanjalaMixin,
    DetailView,
):
    model = ChemicalCoA
    template_name = "ca/dashboard/pages/coa/detail.html"
    context_object_name = "coa"
    permission_required_view = "ca.view_chemicalcoa"

    def get_object(self, queryset=None):
        return _get_coa_or_404(
            self.request.user,
            self.kwargs["pk"],
            plant_id=self.get_scope_plant_id(),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(
            **kwargs
        )

        coa = self.object
        batch = getattr(
            coa,
            "batch",
            None,
        )

        process = None

        if batch is not None:
            process = (
                _process_display_queryset(
                    self.request.user,
                    plant_id=self.get_scope_plant_id()
                )
                .filter(batch=batch)
                .first()
            )

        for context_name, method_name in {
            "results": "get_results",
            "result_counts": "get_result_counts",
            "result_statistics": "get_result_statistics",
            "verification_payload": "get_verification_payload",
        }.items():
            method = getattr(
                coa,
                method_name,
                None,
            )

            if not callable(method):
                context[context_name] = None
                continue

            try:
                context[context_name] = method()
            except (
                TypeError,
                ValueError,
                ValidationError,
            ):
                context[context_name] = None

        context.update(
            {
                "process": process,
                "is_approved": bool(
                    getattr(
                        coa,
                        "is_approved",
                        False,
                    )
                ),
                "is_locked": bool(
                    getattr(
                        coa,
                        "is_locked",
                        False,
                    )
                ),
                "is_complete": bool(
                    getattr(
                        coa,
                        "is_complete",
                        False,
                    )
                ),
                "can_generate": self._safe_boolean_property(
                    coa,
                    "can_generate",
                ),
                "integrity_status": getattr(
                    coa,
                    "integrity_status",
                    None,
                ),
                "generation_status": getattr(
                    coa,
                    "generation_status",
                    None,
                ),
                "missing_assets": self._safe_collection(
                    coa,
                    "missing_assets",
                ),
                "has_failed_results": bool(
                    getattr(
                        coa,
                        "has_failed_results",
                        False,
                    )
                ),
                "has_na_results": bool(
                    getattr(
                        coa,
                        "has_na_results",
                        False,
                    )
                ),
                "has_pending_results": bool(
                    getattr(
                        coa,
                        "has_pending_results",
                        False,
                    )
                ),
                "workflow_permissions": {
                    "view": self.user_has_ca_permission(
                        CHEMICAL_PERMISSIONS["view_workflow"]
                    ),
                    "generate": self.user_has_ca_permission(
                        CHEMICAL_PERMISSIONS["generate_coa"]
                    ),
                    "approve": self.user_has_ca_permission(
                        CHEMICAL_PERMISSIONS["approve_coa"]
                    ),
                    "verify": self.user_has_ca_permission(
                        CHEMICAL_PERMISSIONS["verify_coa"]
                    ),
                },
            }
        )

        context["can_regenerate"] = (
            context["workflow_permissions"]["generate"]
            and not context["is_approved"]
            and not context["is_locked"]
        )

        context["coa_identity"] = {
            "id": coa.pk,
            "sample_id": getattr(
                coa,
                "sample_id",
                None,
            ),
            "certificate_number": (
                self._certificate_number(coa)
            ),
            "title": getattr(
                coa,
                "title",
                None,
            ),
        }

        context["traceability"] = (
            self._build_traceability(coa)
        )

        return context

    @staticmethod
    def _safe_boolean_property(
        obj,
        name,
    ):
        try:
            value = getattr(
                obj,
                name,
                False,
            )

            if callable(value):
                value = value()

            return bool(value)

        except (
            TypeError,
            ValueError,
            ValidationError,
        ):
            return False

    @staticmethod
    def _safe_collection(
        obj,
        name,
    ):
        try:
            value = getattr(
                obj,
                name,
                [],
            )

            if callable(value):
                value = value()

            if value is None:
                return []

            return list(value)

        except (
            TypeError,
            ValueError,
            ValidationError,
        ):
            return []

    @staticmethod
    def _certificate_number(coa):
        if coa.pk:
            return f"COA-{coa.pk:06d}"

        sample_id = getattr(
            coa,
            "sample_id",
            None,
        )

        if sample_id:
            return f"COA-{sample_id}"

        return "COA-DRAFT"

    @staticmethod
    def _build_traceability(coa):
        batch = getattr(
            coa,
            "batch",
            None,
        )

        chemical = getattr(
            batch,
            "chemical",
            None,
        )

        supplier = getattr(
            batch,
            "supplier",
            None,
        )

        treatment_plant = getattr(
            batch,
            "treatment_plant",
            None,
        )

        return {
            "batch_id": getattr(
                batch,
                "pk",
                None,
            ),
            "sample_id": getattr(
                batch,
                "sample_id",
                None,
            ),
            "batch_number": getattr(
                batch,
                "batch_number",
                None,
            ),
            "batch_status": getattr(
                batch,
                "status",
                None,
            ),
            "chemical": getattr(
                chemical,
                "name",
                None,
            ),
            "chemical_code": getattr(
                chemical,
                "code",
                None,
            ),
            "chemical_grade": getattr(
                chemical,
                "grade",
                None,
            ),
            "supplier": getattr(
                supplier,
                "name",
                None,
            ),
            "contract_number": getattr(
                supplier,
                "contract_number",
                None,
            ),
            "treatment_plant": treatment_plant,
        }


class RegenerateChemicalCoAView(
    CaWanjalaMixin,
    View,
):
    permission_required_view = CHEMICAL_PERMISSIONS["generate_coa"]

    def post(self, request, *args, **kwargs):

        with transaction.atomic():
            coa = _get_coa_or_404(
                request.user,
                kwargs["pk"],
                lock=True,
                plant_id=self.get_scope_plant_id(),
            )

            self._validate_regeneration(coa)

            regenerate = getattr(
                coa,
                "regenerate_content",
                None,
            )

            if not callable(regenerate):
                raise ValidationError(
                    "This Certificate of Analysis does not support regeneration."
                )

            regenerate()

            coa.save(
                populate=False,
                regenerate_watermark=False,
                regenerate_hash=False,
                generate_assets=True,
            )

            self._refresh_integrity(coa)

        if self._is_htmx(request):
            return self._htmx_success_response(
                coa
            )

        if self._is_json(request):
            return JsonResponse(
                {
                    "success": True,
                    "message": (
                        "Certificate of Analysis "
                        "regenerated successfully."
                    ),
                    "coa_id": coa.pk,
                    "redirect_url": reverse(
                        "ca:coa-detail",
                        kwargs={
                            "pk": coa.pk
                        },
                    ),
                    "integrity_status": getattr(
                        coa,
                        "integrity_status",
                        None,
                    ),
                    "generation_status": getattr(
                        coa,
                        "generation_status",
                        None,
                    ),
                }
            )

        messages.success(
            request,
            "Certificate of Analysis regenerated successfully.",
        )

        return redirect(
            reverse(
                "ca:coa-detail",
                kwargs={
                    "pk": coa.pk
                },
            )
        )

    @staticmethod
    def _validate_regeneration(coa):
        if getattr(
            coa,
            "is_approved",
            False,
        ):
            raise ValidationError(
                "An approved Certificate of Analysis cannot be regenerated."
            )

        if getattr(
            coa,
            "is_locked",
            False,
        ):
            raise ValidationError(
                "This Certificate of Analysis is locked and cannot be regenerated."
            )

        batch = getattr(
            coa,
            "batch",
            None,
        )

        if batch is None:
            raise ValidationError(
                "The Certificate of Analysis is not linked to a chemical batch."
            )

        validate = getattr(
            coa,
            "validate_generation_requirements",
            None,
        )

        if callable(validate):
            validate()

    @staticmethod
    def _refresh_integrity(coa):
        refresh_hash = getattr(
            coa,
            "refresh_hash",
            None,
        )

        if callable(refresh_hash):
            refresh_hash()

    @staticmethod
    def _is_htmx(request):
        return (
            request.headers.get(
                "HX-Request"
            )
            == "true"
        )

    @staticmethod
    def _is_json(request):
        accept = request.headers.get(
            "Accept",
            "",
        )

        return (
            "application/json" in accept
            or request.headers.get(
                "X-Requested-With"
            )
            == "XMLHttpRequest"
        )

    @staticmethod
    def _htmx_success_response(coa):
        response = JsonResponse(
            {
                "success": True,
                "message": (
                    "Certificate of Analysis "
                    "regenerated successfully."
                ),
                "coa_id": coa.pk,
            }
        )

        response["HX-Trigger"] = (
            "coa:regenerated"
        )

        return response


class VerifyChemicalCoAView(
    CaWanjalaMixin,
    View,
):
    permission_required_view = CHEMICAL_PERMISSIONS["verify_coa"]

    def post(self, request, *args, **kwargs):

        with transaction.atomic():
            coa = _get_coa_or_404(
                request.user,
                kwargs["pk"],
                lock=True,
                plant_id=self.get_scope_plant_id(),
            )

            verifier = getattr(
                coa,
                "verify_integrity",
                None,
            )

            if not callable(verifier):
                raise ValidationError(
                    "This Certificate of Analysis does not support integrity verification."
                )

            result = verifier()

            coa.refresh_from_db()

        integrity_status = getattr(
            coa,
            "integrity_status",
            None,
        )

        return JsonResponse(
            {
                "id": coa.pk,
                "verified": (
                    integrity_status
                    == "VALID"
                ),
                "integrity_status": (
                    integrity_status
                ),
                "result": result,
            }
        )


class DownloadChemicalCoAView(
    CaWanjalaMixin,
    View,
):
    permission_required_view = "ca.view_chemicalcoa"

    def get(self, request, *args, **kwargs):
        coa = _get_coa_or_404(
            request.user,
            kwargs["pk"],
            plant_id=self.get_scope_plant_id(),
        )

        pdf_file = getattr(
            coa,
            "pdf_file",
            None,
        )

        if not (
            pdf_file
            and pdf_file.name
        ):
            raise Http404(
                "The Certificate of Analysis PDF is not available."
            )

        return redirect(
            pdf_file.url
        )


class ChecklistDetailView(
    CaWanjalaMixin,
    DetailView,
):
    model = ChecklistForWaterTreatmentChemicalAnalysis
    template_name = "ca/chema/checklist_detail.html"
    context_object_name = "checklist"
    permission_required_view = "ca.view_checklistforwatertreatmentchemicalanalysis"

    def get_object(self, queryset=None):
        checklist = (
            _checklist_queryset(
                self.request.user,
                plant_id=self.get_scope_plant_id()
            )
            .filter(
                pk=self.kwargs["pk"]
            )
            .first()
        )

        if checklist is None:
            raise Http404(
                "Receiving checklist not found."
            )

        return checklist


class ReopenChecklistView(
    CaWanjalaMixin,
    View,
):
    permission_required_view = CHEMICAL_PERMISSIONS[
        "reopen_checklist"
    ]

    def post(self, request, *args, **kwargs):

        with transaction.atomic():
            checklist = get_object_or_404(
                _scope_queryset(
                    ChecklistForWaterTreatmentChemicalAnalysis.objects
                    .select_for_update(),
                    request.user,
                    "chemical_analysis_request__batch__treatment_plant_id",
                ),
                pk=kwargs["pk"],
            )

            reopen = getattr(
                checklist,
                "reopen",
                None,
            )

            if not callable(reopen):
                raise ValidationError(
                    "This checklist does not support reopening."
                )

            reopen(
                performed_by=request.user
            )

        messages.success(
            request,
            "The receiving checklist has been reopened.",
        )

        return redirect(
            reverse(
                "ca:checklist-detail",
                kwargs={
                    "pk": checklist.pk
                },
            )
        )


class ProcessStatisticsView(
    CaWanjalaMixin,
    TemplateView,
):
    template_name = (
        "ca/dashboard/pages/processes/statistics.html"
    )
    permission_required_view = CHEMICAL_PERMISSIONS[
        "view_workflow"
    ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(
            **kwargs
        )

        user = self.request.user
        plant_id = self.get_scope_plant_id()

        processes = _process_queryset(user, plant_id=plant_id)
        batches = _batch_queryset(user, plant_id=plant_id)
        results = _result_queryset(user, plant_id=plant_id)
        requests = _request_queryset(user, plant_id=plant_id)

        process_counts = (
            processes
            .values("status")
            .annotate(total=Count("pk"))
            .order_by("status")
        )

        batch_counts = (
            batches
            .values("status")
            .annotate(total=Count("pk"))
            .order_by("status")
        )

        result_counts = (
            results
            .values("status")
            .annotate(total=Count("pk"))
            .order_by("status")
        )

        request_counts = (
            requests
            .values("status")
            .annotate(total=Count("pk"))
            .order_by("status")
        )

        context.update(
            {
                "process_counts": process_counts,
                "batch_counts": batch_counts,
                "result_counts": result_counts,
                "request_counts": request_counts,
                "total_processes": processes.count(),
                "active_processes": processes.filter(
                    status__in=PROCESS_ACTIVE_STATUSES
                ).count(),
                "analysis_processes": processes.filter(
                    status__in=PROCESS_ANALYSIS_STATUSES
                ).count(),
                "completed_processes": processes.filter(
                    status="COMPLETED"
                ).count(),
                "rejected_processes": processes.filter(
                    status="REJECTED"
                ).count(),
                "pending_requests": requests.filter(
                    status="PENDING"
                ).count(),
                "in_progress_requests": requests.filter(
                    status="IN_PROGRESS"
                ).count(),
                "completed_requests": requests.filter(
                    status="COMPLETED"
                ).count(),
                "unassigned_requests": requests.filter(
                    assigned_analysts__isnull=True
                ).distinct().count(),
                "overdue_requests": requests.filter(
                    analysis_deadline__lt=timezone.now(),
                    status__in=[
                        "PENDING",
                        "IN_PROGRESS",
                    ],
                ).count(),
            }
        )

        return context


class ProcessRefreshView(
    CaWanjalaMixin,
    View,
):
    permission_required_view = CHEMICAL_PERMISSIONS[
        "view_workflow"
    ]

    def post(self, request, *args, **kwargs):
        process = _get_process_or_404(
            request.user,
            kwargs["pk"],
            plant_id=self.get_scope_plant_id(),
        )

        process.refresh_from_db()

        analysis_request = getattr(
            process,
            "analysis_request",
            None,
        )

        assignment = (
            _request_assignment_data(
                analysis_request
            )
            if analysis_request
            else None
        )

        return JsonResponse(
            {
                "id": process.pk,
                "status": process.status,
                "is_analysis_complete": bool(
                    process.is_analysis_complete
                ),
                "has_pending_tests": bool(
                    process.has_pending_tests
                ),
                "updated_at": (
                    process.updated_at.isoformat()
                    if getattr(
                        process,
                        "updated_at",
                        None,
                    )
                    else None
                ),
                "assignment": assignment,
            }
        )


class MyChemicalTasksView(
    CaWanjalaMixin,
    ListView,
):
    model = ChemicalAnalysisProcess
    template_name = "ca/chema/my_tasks.html"
    context_object_name = "processes"
    # Assigned users need only their CA task/role permissions; requiring the
    # broad process-view permission here prevented analysts from reaching work
    # explicitly assigned to them.
    permission_required_view = None
    paginate_by = None

    def get_queryset(self):
        user = self.request.user
        return (
            _process_display_queryset(
                user,
                plant_id=self.get_scope_plant_id(),
            )
            .filter(
                task__owner=user,
                task__status__in={"NEW", "ASSIGNED", "STARTED"},
            )
            .exclude(status__in=PROCESS_TERMINAL_STATUSES)
            .distinct()
            .prefetch_related("task_set__owner")
            .order_by(
                "analysis_request__analysis_deadline",
                "-updated_at",
                "-pk",
            )
        )

    def _can_execute_task(self, node, task, user):
        """Require ownership and the permission recorded on the active task."""
        try:
            if not node.can_execute(user, task):
                return False
            required_permission = task.owner_permission
            if not required_permission:
                return True
            permission_object = task.owner_permission_obj
            return (
                user.has_perm(required_permission, obj=permission_object)
                or user.has_perm(required_permission)
            )
        except (AttributeError, TypeError, PermissionError):
            return False

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        task_rows = []
        processes = list(context.get("processes", context.get("object_list", [])))
        active_statuses = {"NEW", "ASSIGNED", "STARTED"}
        now = timezone.now()

        for process in processes:
            for task in process.task_set.all():
                if (
                    task.status not in active_statuses
                    or task.owner_id != user.pk
                    or not task.flow_task
                ):
                    continue
                node = task.flow_task
                if node.name == "qa_review" and process.is_analysis_performer(user):
                    continue
                if not self._can_execute_task(node, task, user):
                    continue

                try:
                    action_url = reverse(
                        f"ca:chemicalanalysis:{node.name}:execute",
                        args=[process.pk, task.pk],
                    )
                except (NoReverseMatch, AttributeError, TypeError):
                    continue

                request_obj = process.analysis_request
                deadline = getattr(request_obj, "analysis_deadline", None)
                overdue = bool(deadline and deadline < now)
                due_soon = bool(deadline and not overdue and deadline <= now + timedelta(hours=24))
                annotation = getattr(node, "annotation", None)
                title = (
                    getattr(annotation, "title", None)
                    or task.title
                    or node.name.replace("_", " ").title()
                )
                stage_key = "analysis"
                if node.name in {"receive", "create_request", "checklist", "complete_checklist"}:
                    stage_key = "intake"
                elif node.name in {"qa_review", "capa", "retest", "retest_result", "retest_input", "complete_retest", "quality_control_gate", "record_method_qc"}:
                    stage_key = "quality"
                elif node.name in {"approval", "reject", "generate_coa", "refer_kebs", "approve_coa", "batch_disposition", "utilize", "return_to_supplier", "return_rejected"}:
                    stage_key = "release"
                batch = process.batch if process.batch_id else None
                task_rows.append({
                    "task": task,
                    "process": process,
                    "created_at": task.created,
                    "title": title,
                    "stage": stage_key,
                    "stage_label": stage_key.replace("_", " ").title(),
                    "action_url": action_url,
                    "action_label": "Execute task",
                    "deadline": deadline,
                    "overdue": overdue,
                    "due_soon": due_soon,
                    "progress": process.analysis_progress,
                    "status_label": task.get_status_display() if hasattr(task, "get_status_display") else task.status.title(),
                    "batch_label": batch.sample_id if batch else f"Process {process.pk}",
                    "chemical": str(batch.chemical) if batch and batch.chemical_id else "Chemical analysis",
                    "sample_id": batch.sample_id if batch else "—",
                    "plant": str(batch.treatment_plant) if batch and batch.treatment_plant_id else "Treatment plant not set",
                    "assignee": "You",
                    "task_name": node.name,
                })

        task_rows.sort(key=lambda row: (
            not row["overdue"],
            row["deadline"] is None,
            row["deadline"] or now,
            row["created_at"],
        ))
        context["tasks"] = task_rows
        context["now"] = now
        context["task_count"] = len(task_rows)
        context["pending_analysis"] = sum(row["stage"] == "analysis" for row in task_rows)
        context["pending_qa"] = sum(row["stage"] == "quality" for row in task_rows)
        context["pending_release"] = sum(row["stage"] == "release" for row in task_rows)
        context["overdue_count"] = sum(row["overdue"] for row in task_rows)
        context["due_soon_count"] = sum(row["due_soon"] for row in task_rows)
        context["active_task_count"] = sum(
            row["task"] is not None and row["task"].status == "STARTED"
            for row in task_rows
        )
        context["next_task"] = task_rows[0] if task_rows else None
        return context


class OverdueAnalysisView(
    CaWanjalaMixin,
    ListView,
):
    model = ChemicalAnalysisProcess
    template_name = "ca/chema/overdue_analysis.html"
    context_object_name = "processes"
    permission_required_view = CHEMICAL_PERMISSIONS[
        "view_workflow"
    ]
    paginate_by = 50

    def get_queryset(self):
        return (
            _process_display_queryset(
                self.request.user,
                plant_id=self.get_scope_plant_id()
            )
            .filter(
                analysis_request__analysis_deadline__lt=(
                    timezone.now()
                ),
                status__in=PROCESS_ACTIVE_STATUSES,
            )
            .order_by(
                "analysis_request__analysis_deadline",
                "pk",
            )
        )


class PendingAnalysisView(
    CaWanjalaMixin,
    ListView,
):
    model = ChemicalAnalysisProcess
    template_name = "ca/chema/pending_analysis.html"
    context_object_name = "processes"
    permission_required_view = CHEMICAL_PERMISSIONS["result"]
    paginate_by = 50

    def get_queryset(self):
        user = self.request.user

        return (
            _process_display_queryset(user, plant_id=self.get_scope_plant_id())
            .filter(
                status__in=PROCESS_ANALYSIS_STATUSES,
                analysis_request__assigned_analysts=user,
            )
            .distinct()
            .order_by(
                "analysis_request__analysis_deadline",
                "pk",
            )
        )


class PendingQAReviewView(
    CaWanjalaMixin,
    ListView,
):
    model = ChemicalAnalysisProcess
    template_name = "ca/chema/pending_qa.html"
    context_object_name = "processes"
    permission_required_view = CHEMICAL_PERMISSIONS["view_workflow"]
    paginate_by = 50

    def get_queryset(self):
        user = self.request.user
        return (
            _process_display_queryset(user, plant_id=self.get_scope_plant_id())
            .filter(status="QA")
            .distinct()
            .order_by("-updated_at", "-pk")
        )


class PendingCoAApprovalView(
    CaWanjalaMixin,
    ListView,
):
    model = ChemicalAnalysisProcess
    template_name = "ca/chema/pending_coa_approval.html"
    context_object_name = "processes"
    permission_required_view = CHEMICAL_PERMISSIONS["approve_coa"]
    paginate_by = 50

    def get_queryset(self):
        user = self.request.user
        return (
            _process_display_queryset(user, plant_id=self.get_scope_plant_id())
            .filter(status__in=["QA", "APPROVED", "COMPLETED"])
            .distinct()
            .order_by("-updated_at", "-pk")
        )
