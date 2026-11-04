from __future__ import annotations

import datetime
import math
from decimal import Decimal, ROUND_HALF_UP
import json
import logging
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View as DjangoView

from viewflow import this
from viewflow.workflow import flow, lock
from viewflow.workflow.flow import views
from viewflow.workflow.flow.views import mixins

from .signals import _generate_coa_assets
from .forms import (
    AnalystAssignmentForm,
    AnalystAutoAssignmentForm,
    ApprovalForm,
    BatchTestResultForm,
    CAPAForm,
    ChemicalAnalysisInputForm,
    ChemicalAnalysisRequestForm,
    ChemicalBatchForm,
    ChecklistForm,
    CoAApprovalForm,
    CoAGenerationForm,
    CompleteAnalysisForm,
    CompleteChecklistForm,
    ConfirmActionForm,
    ChemicalInterlaboratorySubmissionForm,
    ChemicalMethodQCForm,
    DynamicChemicalAnalysisInputForm,
    FormulaDrivenChemicalAnalysisForm,
    InterlaboratoryReviewForm,
    KEBSReferralForm,
    QAReviewForm,
    ReceiveChemicalBatchForm,
    RejectAnalysisForm,
    RetestForm,
    ReturnToSupplierForm,
    StartAnalysisForm,
    UtilizationForm,
)
from .models import (
    BatchTestResult,
    ChemicalAnalysisInput,
    ChemicalAnalysisProcess,
    ChemicalAnalysisRequest,
    ChemicalBatch,
    ChemicalCoA,
    ChemicalCorrectiveAction,
    ChemicalLabComparisonResult,
    ChemicalLabReferral,
    ChemicalMethodQualityPlan,
    ChemicalMethodQCControl,
    ChemicalQCCheckResult,
    ChemicalOOSInvestigation,
    ChemicalKEBSReferral,
    ChemicalCustodyEvent,
    ChemicalWorkflowAlert,
    ChemicalWorkflowEvent,
    ChecklistForWaterTreatmentChemicalAnalysis,
)
from .workflow_ui import next_stage_key


User = get_user_model()
logger = logging.getLogger(__name__)
INTERLAB_RESULT_QUANTUM = Decimal("0.000000000001")


class ChemicalWorkflowTaskView(
    LoginRequiredMixin,
    mixins.TaskSuccessUrlMixin,
    DjangoView,
):
    form_class = None
    template_name = "ca/workflow/task.html"
    post_methods = {"POST", "PUT", "PATCH"}

    def get_activation(self):
        activation = getattr(self.request, "activation", None)
        if activation is None:
            raise PermissionDenied("This view must be executed by Viewflow.")
        return activation

    def get_process(self):
        process = self.get_activation().process
        if process is None:
            raise PermissionDenied("No workflow process is attached to this task.")
        if not isinstance(process, ChemicalAnalysisProcess):
            process = (
                ChemicalAnalysisProcess.objects
                .select_related("batch", "analysis_request")
                .get(pk=process.pk)
            )
        return process

    def get_batch(self):
        batch = self.get_process().batch
        if batch is None:
            raise ValidationError("This workflow has no chemical batch.")
        return batch

    def get_analysis_request(self):
        request = self.get_process().analysis_request
        if request is None:
            raise ValidationError("This workflow has no analysis request.")
        return request

    def get_task_data(self) -> dict[str, Any]:
        data = self.get_activation().task.data
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _json_safe(val: Any) -> Any:
        if isinstance(val, Decimal):
            return str(val)
        if isinstance(val, (datetime.date, datetime.datetime)):
            return val.isoformat()
        if isinstance(val, UUID):
            return str(val)
        if isinstance(val, dict):
            return {k: ChemicalWorkflowTaskView._json_safe(v) for k, v in val.items()}
        if isinstance(val, (list, tuple)):
            return [ChemicalWorkflowTaskView._json_safe(v) for v in val]
        return val

    def set_task_data(self, **values: Any):
        task = self.get_activation().task
        data = task.data if isinstance(task.data, dict) else {}
        for k, v in values.items():
            data[k] = self._json_safe(v)
        task.data = data
        task.save(update_fields=["data"])
        return data

    def get_form_kwargs(self):
        if self.request.method in self.post_methods:
            return {
                "data": self.request.POST,
                "files": self.request.FILES,
            }
        return {}

    def get_form(self):
        if self.form_class is None:
            raise ImproperlyConfigured("form_class must be defined.")
        kwargs = {
            "user": self.request.user,
            "process": self.get_process(),
            "batch": self.get_batch(),
        }
        kwargs.update(self.get_form_kwargs())
        while True:
            try:
                return self.form_class(**kwargs)
            except TypeError as exc:
                msg = str(exc)
                popped = False
                for key in ("batch", "process", "user"):
                    if key in kwargs and f"unexpected keyword argument '{key}'" in msg:
                        kwargs.pop(key, None)
                        popped = True
                        break
                if not popped:
                    raise

    def get_context_data(self, *, form, **extra):
        activation = self.get_activation()
        process = self.get_process()
        flow_task = activation.flow_task
        task_name = getattr(flow_task, "name", "")
        task_prompts = {
            "receive": "Check the delivery note, batch identity, quantity, packaging and physical condition before confirming receipt.",
            "create_request": "Confirm the analysis request details. The request will be linked to this batch and routed for analyst assignment.",
            "assign_analysts": "Select or reassign the permitted analyst who will carry out this chemical analysis.",
            "checklist": "Complete each receiving checklist item and attach a clear photo of the container label showing the expiry date.",
            "complete_checklist": "Review the saved checklist and confirm it is complete before laboratory work begins.",
            "start_analysis": "Only an assigned analyst can start. The system will present each required specification in turn.",
            "record_result": "Enter the measured analytical values. The system calculates the result, checks the specification and records the appropriate pass/fail remark.",
            "record_input": "Enter the method-specific measurements and instrument inputs requested for this result.",
            "complete_analysis": "Finalize analysis after every required parameter has a result and all method inputs are recorded.",
            "qa_review": "Review all results against the originating plant specifications. A failed parameter must be repeated by another local analyst; a persistent failure is referred to other plant laboratories for independent comparison.",
        "record_method_qc": "Record every configured method quality-control check. Failed controls flag affected results and open an out-of-specification investigation before the chemist can review the batch.",
            "retest": "The chemist assigned this failed parameter to a different originating-plant analyst. Confirm the parameter and document the repeat-analysis reason before it is recorded.",
            "retest_result": "Record the requested retest result and any method inputs, then submit it for another chemist review.",
            "complete_retest": "Confirm the retest is complete. The process will return for a fresh chemist decision.",
            "capa": "Record corrective and preventive actions and evidence before resubmitting the process for chemist review.",
            "approval": "Archived workflow compatibility task for batches started before approval moved into chemist review.",
            "interlaboratory_review": "Compare each participating laboratory's results. Numeric readings are averaged by laboratory, then across the participating laboratories. The final certificate is issued by the plant that first received the chemical.",
            "generate_coa": "Generate the Certificate of Analysis from the originating treatment plant with the final results and documented comparison remarks. The report is generated even if the final result remains out of specification.",
        "refer_kebs": "Forward the persistently failed chemical and its report-only Certificate of Analysis to KEBS. Record the dispatch details and custody evidence at the originating plant.",
            "approve_coa": "Verify the certificate details and approve its release before recording batch disposition.",
            "batch_disposition": "Choose whether the approved batch will be utilized or returned to the supplier.",
            "utilize": "Record the quantity put into use and any utilization remarks to close this batch disposition.",
            "return_to_supplier": "Record the quantity returned and the reason. A rejected batch must be returned to close the process.",
            "reject": "Record the rejection reason. The workflow will then require a supplier return.",
        }
        if task_name == "generate_coa" and process.failed_tests.exists():
            task_prompts["generate_coa"] = (
                "Generate the originating plant's report-only Certificate of Analysis with the final failed results, interlaboratory averages, and documented remarks. "
                "The certificate records the outcome and does not authorize chemical use."
            )
        annotation = getattr(flow_task, "annotation", None)
        task_title = (
            getattr(annotation, "title", None)
            or getattr(flow_task, "title", None)
            or task_name.replace("_", " ").capitalize()
            or "Chemical batch workflow"
        )
        context = {
            "form": form,
            "process": process,
            "batch": process.batch,
            "analysis_request": process.analysis_request,
            "activation": activation,
            "task": activation.task,
            "flow_task": activation.flow_task,
            "workflow_status": process.status,
            "task_data": self.get_task_data(),
            "task_title": task_title,
            "task_prompt": task_prompts.get(
                task_name,
                "Complete the requested workflow step. The next eligible task will open automatically after submission.",
            ),
        }
        if task_name == "checklist":
            context["form_groups"] = [
                {
                    "title": "Product identity",
                    "description": "Confirm the chemical and supplier label details.",
                    "icon": "fa-flask",
                    "fields": [
                        form[name]
                        for name in (
                            "chemical",
                            "chemical_name_on_label",
                            "manufacturer_name_indicated",
                            "grade",
                            "country_of_origin",
                        )
                    ],
                },
                {
                    "title": "Batch condition",
                    "description": "Verify quantity, dates, storage and handling information.",
                    "icon": "fa-box-open",
                    "fields": [
                        form[name]
                        for name in (
                            "min_mass_indicated",
                            "net_weight",
                            "manufacture_date",
                            "expiry_date",
                            "chemical_label_photo",
                            "storage_conditions",
                            "handling_instructions",
                        )
                    ],
                },
                {
                    "title": "Safety verification",
                    "description": "Record the hazards shown on the package and required response guidance.",
                    "icon": "fa-shield-halved",
                    "fields": [
                        form[name]
                        for name in (
                            "is_oxidizing",
                            "is_corrosive",
                            "may_cause_fire",
                            "contact_with_acids_liberates_toxic_gas",
                            "causes_burns",
                            "keep_out_of_reach_of_children",
                            "contact_with_eyes_action",
                            "fire_action_requires_respiratory_equipment",
                        )
                    ],
                },
            ]
        context.update(extra)
        return context

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, self.get_context_data(form=self.get_form()))

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if not form.is_valid():
            return render(request, self.template_name, self.get_context_data(form=form))

        try:
            with transaction.atomic():
                process = (
                    ChemicalAnalysisProcess.objects
                    .select_for_update(of=("self",))
                    .select_related("batch", "analysis_request")
                    .get(pk=self.get_process().pk)
                )
                self.assert_process_scope(process)
                status_before = process.status
                task_name = getattr(self.get_activation().flow_task, "name", "workflow_task")
                result = self.save_form(form)
                self.after_save(form=form, result=result)
                self.get_activation().execute()
                from .services.workflow_enhancements import record_workflow_event

                process.refresh_from_db()
                record_workflow_event(
                    process,
                    event_type="task_completed",
                    action=task_name,
                    actor=request.user,
                    from_status=status_before,
                    to_status=process.status,
                    details={
                        "task_name": task_name,
                        "result_type": result.__class__.__name__ if result is not None else "",
                        "result_id": getattr(result, "pk", None),
                    },
                )
        except ValidationError as exc:
            # Business-rule failures are expected workflow feedback. Return
            # them on the task form instead of crashing the task page with a
            # 500; atomic() has already rolled back any partial changes.
            #
            # Model-level ValidationErrors may carry field-keyed messages
            # (e.g. "analyst") that don't exist on the current form class.
            # Django's add_error() would raise ValueError in that case, so
            # we flatten any unknown-field messages into non-field errors.
            if hasattr(exc, "message_dict"):
                for field, msgs in exc.message_dict.items():
                    target = None if field not in form.fields else field
                    for msg in msgs:
                        form.add_error(target, msg)
            else:
                form.add_error(None, exc)
            return render(request, self.template_name, self.get_context_data(form=form))

        return redirect(self.get_success_url())

    def get_success_url(self):
        """Return to the batch workbench focused on the next active stage."""
        process = self.get_process()
        process.refresh_from_db()
        stage = next_stage_key(process)
        return f"{reverse('ca:batch-detail', kwargs={'pk': process.batch_id})}?stage={stage}"

    def assert_process_scope(self, process):
        user = self.request.user
        if user.is_superuser:
            return
        plant_id = getattr(user, "assigned_treatment_plant_id", None)
        if plant_id is None:
            raise PermissionDenied("Your account is not assigned to a treatment plant.")
        if process.batch is None or process.batch.treatment_plant_id != plant_id:
            raise PermissionDenied("This workflow belongs to another treatment plant.")

    def require_permission(self, permission):
        if self.request.user.is_superuser:
            return
        if not self.request.user.has_perm(permission):
            raise PermissionDenied("You do not have permission to perform this action.")

    def require_active_user(self):
        if not self.request.user.is_active:
            raise PermissionDenied("Your account is inactive.")

    def require_status(self, *statuses):
        if self.get_process().status not in statuses:
            raise ValidationError("This workflow task is not available at the current stage.")

    def save_form(self, form):
        return form.save()

    def after_save(self, *, form, result):
        return None


class StartChemicalBatchView(LoginRequiredMixin, views.CreateArtifactView):
    model = ChemicalBatch
    form_class = ChemicalBatchForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        activation = getattr(self.request, "activation", None)
        if activation is None:
            raise PermissionDenied("This view must be executed by Viewflow.")

        with transaction.atomic():
            self.object = form.save()
            process = activation.process
            batch = self.object

            if batch.treatment_plant_id is None:
                raise ValidationError("A treatment plant is required for a chemical batch.")
            if (
                not self.request.user.is_superuser
                and batch.treatment_plant_id != getattr(
                    self.request.user, "assigned_treatment_plant_id", None
                )
            ):
                raise PermissionDenied("The chemical batch belongs to another treatment plant.")

            process.batch = batch
            process.initiated_by = self.request.user
            process.status = ChemicalAnalysisProcess.WorkflowStatus.CREATED
            process.artifact = batch
            activation.execute()

        return redirect(self.get_success_url())

    def get_success_url(self):
        if getattr(self, "object", None) is not None:
            return f"{reverse('ca:batch-detail', kwargs={'pk': self.object.pk})}?stage=receipt"
        return super().get_success_url()


@transaction.atomic
def start_chemical_analysis_for_batch(batch, analysis_request, user, http_request):
    """Create the Viewflow process for a batch received through the CA dashboard.

    Dashboard intake already confirms receipt and records its custody and
    inventory events. Mark that receive step complete in Viewflow as part of
    process bootstrap, then route through analyst assignment before checklist.
    """
    existing = (
        ChemicalAnalysisProcess.objects.select_for_update()
        .filter(batch=batch)
        .first()
    )
    if existing is not None:
        if analysis_request is not None and existing.analysis_request_id != analysis_request.pk:
            if existing.analysis_request_id:
                raise ValidationError(
                    "The chemical batch workflow is linked to a different analysis request."
                )
            existing.analysis_request = analysis_request
            existing.save(update_fields=["analysis_request"])
            _complete_unclaimed_view_task(existing, "create_request")
        return existing
    if batch.status != ChemicalBatch.Status.RECEIVED:
        raise ValidationError("The chemical batch must be received before its workflow can start.")

    start_task = ChemicalAnalysisFlow.start
    activation = start_task.activation_class.create(start_task, None, None)
    # Viewflow's start activation reads ``request.user`` even for a process
    # created by Celery or the inbox reconciler. Give background starts an
    # anonymous request context so the initial workflow task remains unowned.
    activation.start(
        http_request if http_request is not None else SimpleNamespace(user=None)
    )
    activation.process.batch = batch
    activation.process.analysis_request = analysis_request
    activation.process.initiated_by = user
    activation.process.status = ChemicalAnalysisProcess.WorkflowStatus.CREATED
    activation.process.artifact = batch
    activation.execute()
    process = activation.process
    process.transition_to(
        ChemicalAnalysisProcess.WorkflowStatus.RECEIVED,
        performed_by=user,
    )

    # Receipt is already persisted on the batch, so acknowledge this gate as
    # a system action even when a background reconciler created the process.
    _complete_unclaimed_view_task(process, "receive")

    return process


def _complete_unclaimed_view_task(process, node_name):
    """Advance a Viewflow node already completed in the CA domain records."""
    for task in process.task_set.select_for_update().all():
        node = task.flow_task
        if not node or node.name != node_name or task.status not in {"NEW", "ASSIGNED"}:
            continue
        # A claimed task belongs to its current owner. Only reconcile unclaimed
        # tasks whose domain work has already been saved and verified.
        if task.owner_id is not None:
            return False
        activation = node.activation_class(task)
        if task.status == "NEW":
            task.status = "ASSIGNED"
            task.save(update_fields=["status"])
        activation.start(None)
        activation.execute()
        return True
    return False


def _complete_persisted_receive_task(process):
    """Close a receive node when the batch already records confirmed receipt.

    Older processes can retain an assigned receive node after the batch was
    marked received through the dashboard. The persisted batch state is the
    source of truth for that gate, so it is safe to reconcile the stale node
    even when Viewflow had assigned it to a user.
    """
    for task in process.task_set.select_for_update().all():
        node = task.flow_task
        if (
            not node
            or node.name != "receive"
            or task.status not in {"NEW", "ASSIGNED", "STARTED"}
        ):
            continue

        activation = node.activation_class(task)
        if task.status in {"NEW", "ASSIGNED"}:
            if task.status == "NEW":
                task.status = "ASSIGNED"
                task.save(update_fields=["status"])
            activation.start(None)
        activation.execute()
        return True
    return False


@transaction.atomic
def advance_analyst_assignment_workflow(
    process,
    *,
    assigned_by=None,
    http_request=None,
    automatic=False,
):
    """Sync a direct assignment with an already waiting Viewflow assignment task.

    Analyst assignment can also be submitted from the request page or an
    assignment notification. When the flow is waiting at its assignment node,
    complete that node here so the assigned analyst immediately receives the
    receiving checklist. Automatic assignment can complete an unclaimed
    assignment node as a system action. Intake and checklist nodes are never
    skipped by this helper.
    """
    if process is None or not process.pk:
        return False

    process = (
        ChemicalAnalysisProcess.objects.select_for_update(of=("self",))
        .select_related("analysis_request", "batch")
        .get(pk=process.pk)
    )
    if not process.analysis_request_id or not process.has_assigned_analysts:
        return False

    # Reconcile processes created before dashboard intake began auto-closing
    # its Viewflow receive node. Receipt itself is already persisted on the
    # batch; this only aligns the process and its task graph with that fact.
    if (
        process.batch_id
        and process.batch.status == ChemicalBatch.Status.RECEIVED
    ):
        if process.status == ChemicalAnalysisProcess.WorkflowStatus.CREATED:
            process.transition_to(
                ChemicalAnalysisProcess.WorkflowStatus.RECEIVED,
                auto=True,
            )
        completed_receive_task = _complete_persisted_receive_task(process)
        if not completed_receive_task and any(
            task.flow_task
            and task.flow_task.name == "receive"
            and task.status in {"NEW", "ASSIGNED", "STARTED"}
            for task in process.task_set.all()
        ):
            return False

    has_completed_checklist = process.analysis_request.checklists.filter(completed=True).exists()
    if (
        has_completed_checklist
        and process.status in {
            ChemicalAnalysisProcess.WorkflowStatus.RECEIVED,
            ChemicalAnalysisProcess.WorkflowStatus.REQUESTED,
            ChemicalAnalysisProcess.WorkflowStatus.ASSIGNED,
        }
        and not (process.batch_id and process.batch.has_active_holds)
    ):
        checklist = (
            process.analysis_request.checklists.filter(completed=True)
            .order_by("-completed_at", "-pk")
            .first()
        )
        process.transition_to(
            ChemicalAnalysisProcess.WorkflowStatus.CHECKLIST,
            performed_by=None,
            extra_fields={
                "checklist_completed_at": process.checklist_completed_at
                or (checklist.completed_at if checklist else None)
                or timezone.now(),
            },
            auto=True,
        )
        _complete_unclaimed_view_task(process, "checklist")
        _complete_unclaimed_view_task(process, "complete_checklist")

    if process.status in {
        ChemicalAnalysisProcess.WorkflowStatus.RECEIVED,
        ChemicalAnalysisProcess.WorkflowStatus.REQUESTED,
        ChemicalAnalysisProcess.WorkflowStatus.CHECKLIST,
    }:
        process.transition_to(
            ChemicalAnalysisProcess.WorkflowStatus.ASSIGNED,
            performed_by=assigned_by,
            auto=automatic,
        )

    active_statuses = {"NEW", "ASSIGNED"}
    pending_assignment = None
    for task in process.task_set.select_for_update().all():
        node = task.flow_task
        if (
            node
            and node.name == "assign_analysts"
            and task.status in active_statuses
        ):
            pending_assignment = (task, node)
            break

    if pending_assignment is None:
        return False

    task, node = pending_assignment
    activation = node.activation_class(task)
    if automatic:
        if task.status == "NEW":
            # The system has already validated and recorded the eligible
            # analyst assignment. Move only this unclaimed assignment node
            # into Viewflow's executable state before completing it.
            task.status = "ASSIGNED"
            task.save(update_fields=["status"])
        elif task.owner_id is not None:
            # Respect a manager who has already claimed the assignment task.
            return False
        activation.start(None)
        assignment_mode = "AUTOMATIC"
    else:
        if assigned_by is None:
            return False
        if task.status == "NEW":
            if not node.can_assign(assigned_by, task):
                return False
            activation.assign(assigned_by)
        elif task.status == "ASSIGNED" and task.owner_id != assigned_by.pk:
            # Do not take over a task another staff member has already claimed.
            return False
        if not node.can_execute(assigned_by, task):
            return False
        activation.start(http_request)
        assignment_mode = "MANUAL"

    data = task.data if isinstance(task.data, dict) else {}
    data.update(
        assignment_mode=assignment_mode,
        assigned_analyst_ids=list(
            process.assigned_analysts.values_list("pk", flat=True)
        ),
        assigned_at=timezone.now().isoformat(),
        assignment_completed_by_id=assigned_by.pk if assigned_by else None,
    )
    task.data = data
    task.save(update_fields=["data"])
    activation.execute()
    return True


class ReceiveChemicalBatchTaskView(ChemicalWorkflowTaskView):
    form_class = ReceiveChemicalBatchForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(batch=self.get_batch(), process=self.get_process(), user=self.request.user)
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.receive_chemical_batch")
        self.require_status(
            ChemicalAnalysisProcess.WorkflowStatus.CREATED,
            ChemicalAnalysisProcess.WorkflowStatus.RECEIVED,
        )
        batch = self.get_batch()
        if batch.status != ChemicalBatch.Status.RECEIVED:
            raise ValidationError("The chemical batch must be in RECEIVED state.")
        remarks = (form.cleaned_data.get("remarks") or "").strip()
        if remarks:
            batch.remarks = remarks
            batch.save(update_fields=["remarks", "updated_at"])
        process = self.get_process()
        process.transition_to(
            ChemicalAnalysisProcess.WorkflowStatus.RECEIVED,
            performed_by=self.request.user,
        )
        return batch


class CreateChemicalAnalysisRequestTaskView(ChemicalWorkflowTaskView):
    form_class = ChemicalAnalysisRequestForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        batch = self.get_batch()
        kwargs.update(
            user=self.request.user,
            batch=batch,
            process=self.get_process(),
            treatment_plant=batch.treatment_plant,
        )
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.create_analysis_request")
        process = self.get_process()
        batch = self.get_batch()
        if batch.status != ChemicalBatch.Status.RECEIVED:
            raise ValidationError("The chemical batch must be received before creating the analysis request.")
        if process.analysis_request_id:
            raise ValidationError("An analysis request already exists for this process.")

        form.instance.batch = batch
        form.instance.requested_by = self.request.user
        request = form.save()

        process.transition_to(
            ChemicalAnalysisProcess.WorkflowStatus.REQUESTED,
            performed_by=self.request.user,
            extra_fields={"analysis_request": request},
        )

        request.auto_assign_analysts_if_due()
        return request


class AnalystAssignmentTaskView(ChemicalWorkflowTaskView):
    form_class = AnalystAssignmentForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(process=self.get_process(), user=self.request.user)
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.create_analysis_request")
        process = self.get_process()
        request = self.get_analysis_request()
        analysts = form.cleaned_data.get("analysts")
        if request.status not in {
            ChemicalAnalysisRequest.Status.PENDING,
            ChemicalAnalysisRequest.Status.IN_PROGRESS,
        }:
            raise ValidationError("Analyst assignment is only available while the analysis request is active.")
        request.assign_analysts(analysts, assigned_by=self.request.user, manual=True)
        assignment_mode = "MANUAL"
        if process.status != ChemicalAnalysisProcess.WorkflowStatus.ASSIGNED:
            process.transition_to(
                ChemicalAnalysisProcess.WorkflowStatus.ASSIGNED,
                performed_by=self.request.user,
            )
        self.set_task_data(
            assignment_mode=assignment_mode,
            assigned_analyst_ids=list(request.assigned_analysts.values_list("pk", flat=True)),
            assigned_at=timezone.now().isoformat(),
        )
        return request


class AnalystAutoAssignmentTaskView(ChemicalWorkflowTaskView):
    form_class = AnalystAutoAssignmentForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(process=self.get_process(), user=self.request.user)
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.create_analysis_request")
        request = self.get_analysis_request()
        if not request.assignment_overdue and not request.has_assigned_analysts:
            raise ValidationError("An analyst is already assigned to this request.")
        assigned = request.auto_assign_analysts_if_due()
        if not assigned.exists():
            raise ValidationError("No eligible analyst is available at this plant for automatic assignment.")
        process = self.get_process()
        if process.status != ChemicalAnalysisProcess.WorkflowStatus.ASSIGNED:
            process.transition_to(
                ChemicalAnalysisProcess.WorkflowStatus.ASSIGNED,
                performed_by=self.request.user,
            )
        self.set_task_data(
            assignment_mode="AUTOMATIC",
            assigned_analyst_ids=list(assigned.values_list("pk", flat=True)),
            assigned_at=timezone.now().isoformat(),
        )
        return request


class ChecklistTaskView(ChemicalWorkflowTaskView):
    form_class = ChecklistForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        analysis_request = self.get_analysis_request()
        kwargs.update(
            request=analysis_request,
            user=self.request.user,
            process=self.get_process(),
            batch=self.get_batch(),
            instance=(
                analysis_request.checklists
                .filter(completed=False)
                .order_by("-pk")
                .first()
            ),
        )
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.complete_receiving_checklist")
        self.require_active_user()
        process = self.get_process()
        if not self.request.user.is_superuser and not process.is_authorized_analyst(self.request.user):
            raise PermissionDenied("Only an analyst assigned to this batch may complete its receiving checklist.")
        if self.get_batch().has_active_holds:
            raise ValidationError("Resolve the open intake holds before completing the receiving checklist.")
        if self.get_analysis_request().checklists.filter(completed=True).exists():
            raise ValidationError("The receiving checklist is already complete.")
        self.require_status(
            ChemicalAnalysisProcess.WorkflowStatus.RECEIVED,
            ChemicalAnalysisProcess.WorkflowStatus.REQUESTED,
            ChemicalAnalysisProcess.WorkflowStatus.ASSIGNED,
            ChemicalAnalysisProcess.WorkflowStatus.CHECKLIST,
        )
        action = self.request.POST.get("checklist_action", "save_close")
        if action not in {"start_analysis", "save_close"}:
            raise ValidationError("Choose whether to start analysis now or save the completed checklist for later.")

        checklist = form.save()
        checklist.mark_completed(performed_by=self.request.user)
        if process.status != ChemicalAnalysisProcess.WorkflowStatus.CHECKLIST:
            process.transition_to(
                ChemicalAnalysisProcess.WorkflowStatus.CHECKLIST,
                performed_by=self.request.user,
                extra_fields={
                    "checklist_completed_at": checklist.completed_at or timezone.now(),
                },
            )
        elif not process.checklist_completed_at:
            process.checklist_completed_at = checklist.completed_at or timezone.now()
            process.save(update_fields=["checklist_completed_at", "updated_at"])

        from .services.workflow_enhancements import record_custody_event

        batch = self.get_batch()
        record_custody_event(
            batch,
            event_type="SAMPLED",
            actor=self.request.user,
            from_location=str(batch.location),
            to_location="Chemical analysis sample",
            remarks=f"Receiving checklist {checklist.pk} completed.",
        )
        self.set_task_data(
            checklist_id=checklist.pk,
            completed_by_id=self.request.user.pk,
            completed_at=checklist.completed_at.isoformat() if checklist.completed_at else timezone.now().isoformat(),
            next_action=action,
        )

        if action == "start_analysis":
            # Keep checklist completion durable even when a later analysis
            # prerequisite (for example, the batch storage location) prevents
            # analysis from starting in this same submission. Roll back only
            # the attempted analysis transition, then let Viewflow route the
            # analyst to its explicit Start Analysis task.
            try:
                with transaction.atomic():
                    process.move_to_analysis(performed_by=self.request.user)
                    process.mark_analysis_started(performed_by=self.request.user)
                    process.refresh_from_db()
                    record_custody_event(
                        batch,
                        event_type="ANALYSIS_STARTED",
                        actor=self.request.user,
                        from_location=str(batch.location),
                        to_location="Chemical analysis laboratory",
                        remarks="Assigned analyst completed the receiving checklist and started analysis.",
                    )
            except ValidationError as exc:
                process.refresh_from_db()
                messages.warning(
                    self.request,
                    f"The receiving checklist was completed and saved. Analysis could not start yet: {exc}",
                )
                self.set_task_data(
                    next_action="start_analysis_pending",
                    analysis_start_blocked_reason=str(exc),
                )

        return checklist

    def get_success_url(self):
        process = self.get_process()
        process.refresh_from_db()
        stage = next_stage_key(process)
        return f"{reverse('ca:batch-detail', kwargs={'pk': process.batch_id})}?stage={stage}"


class CompleteChecklistTaskView(ChemicalWorkflowTaskView):
    form_class = CompleteChecklistForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(
            user=self.request.user,
            process=self.get_process(),
            batch=self.get_batch(),
        )
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.complete_receiving_checklist")
        process = self.get_process()
        if self.get_batch().has_active_holds:
            raise ValidationError("Resolve the open intake holds before completing the receiving checklist.")
        request = self.get_analysis_request()
        checklist = (
            ChecklistForWaterTreatmentChemicalAnalysis.objects
            .select_for_update()
            .filter(chemical_analysis_request=request)
            .order_by("-pk")
            .first()
        )
        if checklist is None:
            raise ValidationError("The receiving checklist must exist before completion.")
        if not checklist.completed:
            checklist.completed = True
            checklist.completed_at = timezone.now()
            checklist.completed_by = self.request.user
            checklist.save(update_fields=["completed", "completed_at", "completed_by", "updated_at"])
        process.transition_to(
            ChemicalAnalysisProcess.WorkflowStatus.CHECKLIST,
            performed_by=self.request.user,
            extra_fields={
                "checklist_completed_at": process.checklist_completed_at or checklist.completed_at or timezone.now(),
            },
        )
        from .services.workflow_enhancements import record_custody_event

        record_custody_event(
            self.get_batch(),
            event_type="SAMPLED",
            actor=self.request.user,
            from_location=str(self.get_batch().location),
            to_location="Chemical analysis sample",
            remarks=f"Receiving checklist {checklist.pk} completed.",
        )
        return process


class StartAnalysisTaskView(ChemicalWorkflowTaskView):
    form_class = StartAnalysisForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(user=self.request.user, process=self.get_process(), batch=self.get_batch())
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.start_chemical_analysis")
        process = self.get_process()
        if process.status not in {
            ChemicalAnalysisProcess.WorkflowStatus.CHECKLIST,
            ChemicalAnalysisProcess.WorkflowStatus.ASSIGNED,
        }:
            raise ValidationError("Chemical analysis can only start after the receiving checklist is complete.")
        if not process.analysis_ready:
            if self.get_batch().has_active_holds:
                raise ValidationError("Resolve the intake hold before starting chemical analysis.")
            raise ValidationError("A completed receiving checklist is required before analysis.")
        request = self.get_analysis_request()
        if not request.has_assigned_analysts:
            request.auto_assign_analysts_if_due()
            request.refresh_from_db()
        if not request.has_assigned_analysts:
            raise ValidationError("No eligible analyst could be assigned automatically. Assign an analyst before starting the analysis.")
        if not process.is_authorized_analyst(self.request.user):
            raise PermissionDenied("You are not assigned to perform this chemical analysis.")
        process.move_to_analysis(performed_by=self.request.user)
        process.mark_analysis_started(performed_by=self.request.user)
        process.refresh_from_db()
        from .services.workflow_enhancements import record_custody_event

        record_custody_event(
            self.get_batch(),
            event_type="ANALYSIS_STARTED",
            actor=self.request.user,
            from_location=str(self.get_batch().location),
            to_location="Chemical analysis laboratory",
            remarks="Assigned analyst started the analysis.",
        )
        return process


class RecordTestResultTaskView(ChemicalWorkflowTaskView):
    form_class = BatchTestResultForm

    @staticmethod
    def _validate_formula_specification(parameter):
        method = getattr(parameter, "method", None)
        if method is None:
            raise ValidationError(
                "No analytical method is assigned to this specification. "
                "Configure its method before recording results."
            )
        if not (method.formula or "").strip():
            raise ValidationError(
                "This analysis method has input measurements but no calculation formula. "
                "Configure the approved formula before recording results."
            )
        if not method.get_input_fields():
            raise ValidationError(
                "This analysis method has a formula but no input measurements. "
                "Configure its input fields before continuing."
            )
        if parameter.min_limit is None and parameter.max_limit is None:
            raise ValidationError(
                "Automated pass/fail needs a minimum or maximum specification limit. "
                "Configure the parameter limits before continuing."
            )
        method_unit = (method.unit or "").strip().casefold()
        specification_unit = (parameter.unit or "").strip().casefold()
        if method_unit and specification_unit and method_unit != specification_unit:
            raise ValidationError(
                "The method output unit does not match the chemical specification unit. "
                "Correct the method or specification setup before recording results."
            )

    def get_retest_parameter_id(self):
        value = self.get_task_data().get("retest_parameter_id")
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError("The retest parameter reference is invalid.") from exc

    def get_next_parameter(self):
        process = self.get_process()
        retest_parameter_id = self.get_retest_parameter_id()
        required = process.required_specifications()
        if retest_parameter_id is not None:
            parameter = required.filter(pk=retest_parameter_id).first()
            if parameter is None:
                raise ValidationError("The selected retest parameter is invalid.")
            return parameter

        completed_ids = set(
            BatchTestResult.objects
            .filter(batch=process.batch)
            .exclude(status__isnull=True)
            .exclude(status="")
            .values_list("parameter_id", flat=True)
        )
        return required.exclude(pk__in=completed_ids).order_by("pk").first()

    def get_form_kwargs(self):
        process = self.get_process()
        process.refresh_assigned_analysts()
        if not process.has_assigned_analysts:
            raise ValidationError("No active analyst is assigned to this analysis.")
        if not process.is_authorized_analyst(self.request.user):
            raise PermissionDenied("You are not assigned to perform this chemical analysis.")
        kwargs = super().get_form_kwargs()
        kwargs.update(
            process=None if process.status == ChemicalAnalysisProcess.WorkflowStatus.RETEST else process,
            batch=self.get_batch(),
            analyst=self.request.user,
            user=self.request.user,
        )
        return kwargs

    def get_form(self):
        parameter = self.get_next_parameter()
        if parameter is None:
            raise ValidationError("All required analytical parameters have been completed.")
        kwargs = self.get_form_kwargs()
        kwargs.pop("user", None)
        kwargs.update(
            parameter=parameter,
            batch=self.get_batch(),
            process=self.get_process(),
            analyst=self.request.user,
        )
        return FormulaDrivenChemicalAnalysisForm(**kwargs)

    def save_form(self, form):
        self.require_permission("ca.record_chemical_result")
        process = self.get_process()
        process.refresh_assigned_analysts()
        if not process.is_authorized_analyst(self.request.user):
            raise PermissionDenied("You are not assigned to perform this chemical analysis.")
        expected = self.get_next_parameter()
        if isinstance(form, FormulaDrivenChemicalAnalysisForm):
            if expected is None or form.parameter.pk != expected.pk:
                raise ValidationError("The calculated result does not match the required parameter.")
            return self._save_formula_result(form, process, expected)
        parameter = form.cleaned_data.get("parameter")
        if parameter is None or expected is None or parameter.pk != expected.pk:
            raise ValidationError("The selected parameter is not currently required.")
        result = form.save()
        if result.batch_id != process.batch_id or result.parameter_id != parameter.pk:
            raise ValidationError("The recorded result does not match this analysis.")
        if process.status == ChemicalAnalysisProcess.WorkflowStatus.RETEST:
            self._record_reassignment_result(process, result)
        self.set_task_data(
            result_id=result.pk,
            parameter_id=result.parameter_id,
            result_status=result.status,
            result_recorded_at=timezone.now().isoformat(),
        )
        return result

    def _save_formula_result(self, form, process, parameter):
        self._validate_formula_specification(parameter)
        if process.status not in {
            ChemicalAnalysisProcess.WorkflowStatus.ANALYSIS,
            ChemicalAnalysisProcess.WorkflowStatus.RETEST,
        }:
            raise ValidationError("Formula results can only be recorded during analysis or an assigned retest.")

        is_retest = process.status == ChemicalAnalysisProcess.WorkflowStatus.RETEST
        if is_retest and process.retest_parameter_id != parameter.pk:
            raise ValidationError("The result does not match the parameter selected for retest.")

        existing = (
            BatchTestResult.objects
            .filter(batch=process.batch, parameter=parameter)
            .first()
        )
        if existing and existing.status and not is_retest:
            raise ValidationError("This parameter already has a finalized result. Use the QA retest workflow to change it.")

        inputs = form.as_input_dictionary()
        result = existing or BatchTestResult(batch=process.batch, parameter=parameter)
        result.batch = process.batch
        result.parameter = parameter
        result.analyst = self.request.user
        result.inventory_equipment = form.cleaned_data.get("inventory_equipment")
        result.reference_standard_name = (form.cleaned_data.get("reference_standard_name") or "").strip()
        result.reference_standard_lot = (form.cleaned_data.get("reference_standard_lot") or "").strip()
        result.reagent_lot_numbers = (form.cleaned_data.get("reagent_lot_numbers") or "").strip()
        result.expanded_uncertainty = form.cleaned_data.get("expanded_uncertainty")
        result.uncertainty_coverage_factor = form.cleaned_data.get("uncertainty_coverage_factor")
        result.tested_at = timezone.now()
        result.textual_result = ""
        if is_retest:
            reason = (process.retest_reason or "").strip()
            if not reason:
                raise ValidationError("A documented retest reason is required before recording this result.")
            result.revision_reason = reason[:500]
        elif existing:
            result.revision_reason = "Finalized pending formula result from method measurements."

        calculated_input = ChemicalAnalysisInput(test_result=result, inputs=inputs)
        value = calculated_input.calculate()
        minimum = parameter.min_limit
        maximum = parameter.max_limit
        passed = (minimum is None or value >= minimum) and (maximum is None or value <= maximum)
        result.value = value
        result.status = BatchTestResult.Status.PASS if passed else BatchTestResult.Status.FAIL
        result.save()

        if is_retest:
            self._record_reassignment_result(process, result)

        previous_input = ChemicalAnalysisInput.objects.filter(test_result=result).first()
        if previous_input is not None:
            calculated_input.pk = previous_input.pk
            calculated_input.method_snapshot = previous_input.method_snapshot
            calculated_input._allow_retest_update = is_retest
        calculated_input.test_result = result
        calculated_input.save()
        if calculated_input.calculated_value != result.value:
            raise ValidationError(
                "The saved method inputs do not reproduce the recorded analytical value."
            )
        transaction.on_commit(
            lambda: messages.success(
                self.request,
                f"{parameter.parameter_name}: {result.value:g} {parameter.unit} — "
                f"{result.get_status_display()}. {result.auto_remark}",
            )
        )

        self.set_task_data(
            result_id=result.pk,
            parameter_id=result.parameter_id,
            result_status=result.status,
            result_recorded_at=timezone.now().isoformat(),
            input_record_id=calculated_input.pk,
            input_recorded=True,
            calculated_value=str(result.value) if result.value is not None else None,
        )
        return result

    def _record_reassignment_result(self, process, result):
        source_plant_id = process.batch.treatment_plant_id
        next_run = (
            ChemicalLabComparisonResult.objects.filter(
                process=process,
                treatment_plant_id=source_plant_id,
                parameter_id=result.parameter_id,
                source=ChemicalLabComparisonResult.Source.REASSIGNMENT,
            ).order_by("-run_number").values_list("run_number", flat=True).first()
            or 0
        ) + 1
        ChemicalLabComparisonResult.objects.create(
            batch=process.batch,
            process=process,
            treatment_plant_id=source_plant_id,
            parameter=result.parameter,
            source=ChemicalLabComparisonResult.Source.REASSIGNMENT,
            run_number=next_run,
            value=result.value,
            textual_result=result.textual_result,
            status=result.status or BatchTestResult.Status.FAIL,
            analyst=self.request.user,
            instrument=result.instrument,
            inventory_equipment=result.inventory_equipment,
            instrument_calibration=result.instrument_calibration,
            instrument_snapshot=result.instrument_snapshot,
            reference_standard_name=result.reference_standard_name,
            reference_standard_lot=result.reference_standard_lot,
            reagent_lot_numbers=result.reagent_lot_numbers,
            expanded_uncertainty=result.expanded_uncertainty,
            uncertainty_coverage_factor=result.uncertainty_coverage_factor,
            remarks=result.auto_remark,
            recorded_at=result.tested_at or timezone.now(),
        )
        investigation = ChemicalOOSInvestigation.objects.filter(
            process=process,
            parameter_id=result.parameter_id,
            status=ChemicalOOSInvestigation.Status.OPEN,
        ).first()
        if investigation:
            investigation.append_repeat(result)

    def get_context_data(self, *, form, **extra):
        context = super().get_context_data(form=form, **extra)
        parameter = self.get_next_parameter()
        if parameter is not None and isinstance(form, FormulaDrivenChemicalAnalysisForm):
            method = parameter.method
            setup_error = ""
            try:
                self._validate_formula_specification(parameter)
            except ValidationError as exc:
                setup_error = " ".join(str(message) for message in exc.messages)
            except Exception:
                setup_error = "The configured analysis method inputs are invalid. Ask an administrator to review the formula setup."
            field_setup_errors = getattr(form, "setup_errors", [])
            if field_setup_errors:
                setup_error = " ".join(field_setup_errors)
            context.update(
                formula_result=True,
                task_title=f"Enter measurements · {parameter.parameter_name}",
                task_prompt=(
                    "Enter the measured volumes and reagent values below. The system will calculate "
                    "the analytical result, compare it with the approved limits, set pass or fail, "
                    "and apply the configured remark."
                ),
                result_parameter=parameter,
                result_method=method,
                result_unit=parameter.unit,
                result_minimum=parameter.min_limit,
                result_maximum=parameter.max_limit,
                result_has_minimum=parameter.min_limit is not None,
                result_has_maximum=parameter.max_limit is not None,
                formula_setup_error=setup_error,
            )
        else:
            context["formula_result"] = False
        return context


class RecordDynamicAnalysisInputTaskView(ChemicalWorkflowTaskView):
    form_class = DynamicChemicalAnalysisInputForm

    def get_result_id(self):
        value = self.get_task_data().get("result_id")
        if value in (None, ""):
            raise ValidationError("No analytical result is attached to this task.")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError("The analytical result reference is invalid.") from exc

    def get_test_result(self):
        process = self.get_process()
        process.refresh_assigned_analysts()
        if not process.is_authorized_analyst(self.request.user):
            raise PermissionDenied("You are not assigned to perform this chemical analysis.")
        result = (
            BatchTestResult.objects
            .select_related("parameter", "parameter__method")
            .filter(pk=self.get_result_id(), batch=process.batch)
            .first()
        )
        if result is None:
            raise ValidationError("The analytical test result could not be found.")
        if result.analyst_id != self.request.user.pk:
            raise PermissionDenied("You did not create this analytical result.")
        if result.parameter is None or result.parameter.method is None:
            raise ValidationError("The analytical result must have an analysis parameter and method.")
        fields = result.parameter.method.get_input_fields()
        if isinstance(fields, str):
            fields = [value.strip() for value in fields.split(",") if value.strip()]
        if not fields:
            raise ValidationError("This analytical method does not require method inputs.")
        return result

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(
            test_result=self.get_test_result(),
            user=self.request.user,
            process=self.get_process(),
            batch=self.get_batch(),
        )
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.record_chemical_result")
        test_result = self.get_test_result()
        inputs = form.as_input_dictionary()
        model_form = ChemicalAnalysisInputForm(
            data={"test_result": test_result.pk, "inputs": json.dumps(inputs)},
            test_result=test_result,
            process=self.get_process(),
            user=self.request.user,
        )
        if not model_form.is_valid():
            raise ValidationError(model_form.errors.as_json())
        result = model_form.save()
        self.set_task_data(input_record_id=result.pk, input_recorded=True, input_recorded_at=timezone.now().isoformat())
        return result


class CompleteAnalysisTaskView(ChemicalWorkflowTaskView):
    form_class = CompleteAnalysisForm

    def get(self, request, *args, **kwargs):
        process = self.get_process()
        process.refresh_from_db()
        stage = next_stage_key(process)
        return redirect(f"{reverse('ca:batch-detail', kwargs={'pk': process.batch_id})}?stage={stage}")

    def post(self, request, *args, **kwargs):
        return self.get(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(user=self.request.user, process=self.get_process(), batch=self.get_batch())
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.complete_chemical_analysis")
        process = self.get_process()
        process.refresh_assigned_analysts()
        if not process.is_authorized_analyst(self.request.user):
            raise PermissionDenied("You are not assigned to perform this chemical analysis.")
        if process.has_pending_tests or not process.is_analysis_complete:
            raise ValidationError("All required analytical results must be finalized before completing the analysis.")
        process.mark_analysis_completed(performed_by=self.request.user)
        process.refresh_from_db()
        if process.status != ChemicalAnalysisProcess.WorkflowStatus.QA:
            raise ValidationError("Chemical analysis did not advance to QA.")
        from .services.workflow_enhancements import record_custody_event

        record_custody_event(
            self.get_batch(),
            event_type="ANALYSIS_COMPLETED",
            actor=self.request.user,
            from_location="Chemical analysis laboratory",
            to_location=str(self.get_batch().location),
            remarks="Analysis completed and sent for chemist review.",
        )
        return process


class RecordMethodQCTaskView(ChemicalWorkflowTaskView):
    form_class = ChemicalMethodQCForm

    def get_form_kwargs(self):
        process = self.get_process()
        process.refresh_assigned_analysts()
        if not process.is_authorized_analyst(self.request.user):
            raise PermissionDenied("Only an analyst assigned to this batch may record its method QC checks.")
        kwargs = super().get_form_kwargs()
        kwargs.update(process=process, user=self.request.user)
        return kwargs

    def get_context_data(self, *, form, **extra):
        context = super().get_context_data(form=form, **extra)
        context.update(
            task_title="Method quality-control checks",
            task_prompt=(
                "Record the configured method blanks, reference standards, duplicates, matrix spikes, "
                "and instrument verification checks before the chemist reviews the batch. "
                "Failed checks flag their related result and require an investigation."
            ),
            qc_controls=getattr(form, "controls", []),
        )
        return context

    def save_form(self, form):
        self.require_permission("ca.record_chemical_result")
        process = self.get_process()
        batch = self.get_batch()
        if process.status != ChemicalAnalysisProcess.WorkflowStatus.QA or not process.is_analysis_complete:
            raise ValidationError("Complete the analysis before recording method QC checks.")
        created = []
        for entry in getattr(form, "qc_entries", []):
            control = entry["control"]
            parameter = entry["parameter"]
            result = BatchTestResult.objects.select_for_update().filter(batch=batch, parameter=parameter).first()
            if result is None:
                raise ValidationError(f"Record {parameter.parameter_name} before evaluating its QC control.")
            run_number = (
                ChemicalQCCheckResult.objects.filter(batch=batch, control=control, parameter=parameter)
                .order_by("-run_number").values_list("run_number", flat=True).first() or 0
            ) + 1
            snapshot = {
                "plan_id": control.plan_id,
                "plan_version": control.plan.version,
                "method_id": str(control.plan.method_id),
                "method": control.plan.method.method,
                "method_version": control.plan.method.version,
                "control_code": control.code,
                "check_type": control.check_type,
                "evaluation": control.evaluation,
                "target_value": str(control.target_value) if control.target_value is not None else None,
                "spike_amount": str(control.spike_amount) if control.spike_amount is not None else None,
                "acceptance_min": str(control.acceptance_min) if control.acceptance_min is not None else None,
                "acceptance_max": str(control.acceptance_max) if control.acceptance_max is not None else None,
                "warning_sigma": str(control.warning_sigma),
                "corrective_instruction": control.corrective_instruction,
            }
            qc = ChemicalQCCheckResult.objects.create(
                batch=batch,
                process=process,
                control=control,
                parameter=parameter,
                run_number=run_number,
                observed_value=entry["observed_value"],
                evaluated_value=entry["evaluated_value"],
                comparison_value=entry["comparison_value"],
                status=entry["status"],
                limits_snapshot=snapshot,
                remarks=entry["remarks"],
                inventory_equipment=entry["inventory_equipment"],
                analyst=self.request.user,
            )
            created.append(qc)
            flag = f"QC_FAIL_{control.pk}"
            flags = set(result.quality_flags or [])
            if qc.status == ChemicalQCCheckResult.Status.FAIL:
                flags.add(flag)
                result.quality_flags = sorted(flags)
                result.status = BatchTestResult.Status.FAIL
                instruction = control.corrective_instruction or "Investigate the control failure and repeat affected analyses as required."
                result.revision_reason = f"QC control failed: {control.label}. {instruction}"[:500]
                result.save()
                from .inventory_instruments import trigger_qc_alert

                trigger_qc_alert(
                    qc,
                    alert_type="ca_qc_failure",
                    title="Chemical method QC check failed",
                    message=(
                        f"{control.label} failed for {parameter.parameter_name} on batch "
                        f"{batch.sample_id or batch.batch_number}. Evaluated value {qc.evaluated_value}; "
                        f"acceptance {control.acceptance_min if control.acceptance_min is not None else '—'} to "
                        f"{control.acceptance_max if control.acceptance_max is not None else '—'}. "
                        f"{control.corrective_instruction or 'Investigate affected results and repeat analysis where required.'}"
                    ),
                    severity="high",
                    context_data={
                        "qc_result_id": qc.pk,
                        "qc_run_number": qc.run_number,
                        "corrective_instruction": control.corrective_instruction,
                        "equipment_name": (qc.instrument_snapshot or {}).get("name", ""),
                        "equipment_serial_number": (qc.instrument_snapshot or {}).get("serial_number", ""),
                    },
                )
            else:
                flags.discard(flag)
                result.quality_flags = sorted(flags)
                if not any(str(value).startswith("QC_FAIL_") for value in flags):
                    if any(value in flags for value in ("OUT_OF_SPEC_LOW", "OUT_OF_SPEC_HIGH", "INSTRUMENT_UNCALIBRATED", "INSTRUMENT_UNAVAILABLE")):
                        result.status = BatchTestResult.Status.FAIL
                    elif result.value is not None and (
                        (parameter.min_limit is not None and result.value < parameter.min_limit)
                        or (parameter.max_limit is not None and result.value > parameter.max_limit)
                    ):
                        result.status = BatchTestResult.Status.FAIL
                    else:
                        result.status = BatchTestResult.Status.PASS
                result.revision_reason = f"QC control passed on run {run_number}: {control.label}."[:500]
                result.save()
            try:
                from .services.quality_control import evaluate_control_chart_and_alert
                evaluate_control_chart_and_alert(qc)
            except Exception:
                logger.exception("Unable to evaluate QC control-chart alert for record %s", qc.pk)

        ChemicalWorkflowEvent.objects.create(
            process=process,
            batch=batch,
            analysis_request=process.analysis_request if process.analysis_request_id else None,
            event_type="method_quality_controls_recorded",
            action="method QC checks evaluated",
            actor=self.request.user,
            from_status=process.status,
            to_status=process.status,
            details={
                "qc_result_ids": [item.pk for item in created],
                "failed_qc_ids": [item.pk for item in created if item.status == ChemicalQCCheckResult.Status.FAIL],
                "method_ids": sorted({str(item.control.plan.method_id) for item in created}),
            },
        )
        self.set_task_data(
            qc_result_ids=[item.pk for item in created],
            qc_fail_count=sum(item.status == ChemicalQCCheckResult.Status.FAIL for item in created),
            qc_recorded_at=timezone.now().isoformat(),
        )
        if any(item.status == ChemicalQCCheckResult.Status.FAIL for item in created):
            messages.warning(self.request, "At least one QC control is outside its method acceptance limits. Affected results were flagged and chemist investigation is required.")
        return created


class RetestTaskView(ChemicalWorkflowTaskView):
    form_class = RetestForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(process=self.get_process(), batch=self.get_batch(), user=self.request.user)
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.perform_chemical_retest")
        process = self.get_process()
        if process.status != ChemicalAnalysisProcess.WorkflowStatus.QA:
            raise ValidationError("Retesting can only be requested during chemist review.")
        process.refresh_assigned_analysts()
        if not process.is_authorized_analyst(self.request.user):
            raise PermissionDenied("You are not assigned to this chemical analysis.")
        parameter = form.cleaned_data.get("parameter")
        reason = (form.cleaned_data.get("reason") or "").strip()
        if parameter is None or not reason:
            raise ValidationError("A retest parameter and reason are required.")
        if not process.required_specifications().filter(pk=parameter.pk).exists():
            raise ValidationError("The selected parameter is not required for this analysis.")
        original_result = process.failed_tests.filter(parameter_id=parameter.pk).first()
        if original_result is None:
            raise ValidationError("Only a currently failed parameter can be reassigned for a repeat analysis.")
        if original_result.analyst_id == self.request.user.pk:
            raise PermissionDenied("The analyst who recorded this failed result cannot perform its independent repeat.")
        process.request_retest(
            parameter=parameter,
            reason=reason,
            performed_by=self.request.user,
        )
        self.set_task_data(
            retest_parameter_id=parameter.pk,
            retest_reason=reason,
            retest_requested_by_id=self.request.user.pk,
            replacement_analyst_id=self.request.user.pk,
            retest_requested_at=timezone.now().isoformat(),
        )
        return parameter


class CompleteRetestTaskView(ChemicalWorkflowTaskView):
    form_class = ConfirmActionForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(user=self.request.user, process=self.get_process(), batch=self.get_batch())
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.perform_chemical_retest")
        process = self.get_process()
        if process.status != ChemicalAnalysisProcess.WorkflowStatus.RETEST:
            raise ValidationError("The analysis is not currently in the retest stage.")
        parameter_id = self.get_task_data().get("retest_parameter_id")
        if not parameter_id:
            raise ValidationError("No retest parameter is attached to this retest.")
        try:
            parameter_id = int(parameter_id)
        except (TypeError, ValueError) as exc:
            raise ValidationError("The retest parameter reference is invalid.") from exc
        if process.retest_parameter_id != parameter_id:
            raise ValidationError("The workflow retest parameter does not match the saved process record.")
        result = (
            BatchTestResult.objects
            .filter(batch=process.batch, parameter_id=parameter_id)
            .first()
        )
        if result is None or not result.status:
            raise ValidationError("The required retest result has not been finalized.")
        process.mark_retest_completed(performed_by=self.request.user, result=result)
        self.set_task_data(retest_completed=True, retest_result_id=result.pk, retest_completed_at=timezone.now().isoformat())
        return process


class QAReviewTaskView(ChemicalWorkflowTaskView):
    form_class = QAReviewForm

    def _redirect_analysis_performer(self, request):
        process = self.get_process()
        if not process.is_analysis_performer(request.user):
            return None
        messages.info(request, "Chemist review for this batch must be completed by an independent reviewer.")
        return redirect(
            f"{reverse('ca:batch-detail', kwargs={'pk': process.batch_id})}?stage=quality"
        )

    def get(self, request, *args, **kwargs):
        response = self._redirect_analysis_performer(request)
        return response or super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        response = self._redirect_analysis_performer(request)
        return response or super().post(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(user=self.request.user, process=self.get_process(), batch=self.get_batch())
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.review_chemical_analysis")
        process = self.get_process()
        if process.status != ChemicalAnalysisProcess.WorkflowStatus.QA:
            raise ValidationError("The analysis must be awaiting chemist review before review can be recorded.")
        if process.has_pending_tests or not process.is_analysis_complete:
            raise ValidationError("All required analytical results must be finalized before chemist review.")
        if process.is_analysis_performer(self.request.user):
            raise PermissionDenied("The analyst who performed this analysis cannot independently review it.")
        decision = str(form.cleaned_data.get("decision", "")).upper()
        remarks = (form.cleaned_data.get("remarks") or "").strip()
        if decision not in {"PASS", "REWORK", "CAPA", "REJECT", "REASSIGN", "REFER_INTERLAB"}:
            raise ValidationError("Invalid chemist review decision.")
        if decision == "PASS" and process.failed_tests.exists():
            raise ValidationError("A failed analysis cannot pass QA.")
        if decision in {"REASSIGN", "REFER_INTERLAB"} and not process.failed_tests.exists():
            raise ValidationError("A failed result is required for analyst reassignment or interlaboratory referral.")
        if process.failed_tests.exists():
            assignable_cause = (form.cleaned_data.get("assignable_cause") or "").strip()
            corrective_action = (form.cleaned_data.get("corrective_action") or "").strip()
            for failed_result in process.failed_tests.exclude(parameter__isnull=True).select_related("parameter"):
                ChemicalOOSInvestigation.record_decision(
                    process=process,
                    result=failed_result,
                    actor=self.request.user,
                    cause=assignable_cause,
                    decision=decision,
                    remarks=remarks,
                    corrective_action=corrective_action,
                )
        elif decision in {"PASS", "REJECT"}:
            for investigation in process.oos_investigations.filter(
                status=ChemicalOOSInvestigation.Status.OPEN
            ).select_related("parameter"):
                final_result = BatchTestResult.objects.filter(
                    batch=process.batch,
                    parameter=investigation.parameter,
                ).first()
                if (
                    final_result is None
                    or final_result.status != BatchTestResult.Status.PASS
                    or any(
                        str(flag).startswith("QC_FAIL_") or flag in {"INSTRUMENT_UNCALIBRATED", "INSTRUMENT_UNAVAILABLE"}
                        for flag in (final_result.quality_flags or [])
                    )
                ):
                    continue
                final_snapshot = ChemicalOOSInvestigation.result_snapshot(final_result)
                investigation.close(
                    actor=self.request.user,
                    disposition=(
                        f"Chemist finalized the investigation with a {decision.lower()} decision after reviewing "
                        f"the passing final result. Review remarks: {remarks or 'none'}"
                    ),
                    comparison={
                        "final_result": final_snapshot,
                        "chemist_decision": decision,
                        "remarks": remarks,
                        "corrective_action": investigation.corrective_action,
                    },
                )
        if decision == "REASSIGN":
            for failed in process.failed_tests.exclude(parameter_id__isnull=True):
                ChemicalLabComparisonResult.objects.get_or_create(
                    batch=process.batch,
                    process=process,
                    treatment_plant_id=process.batch.treatment_plant_id,
                    parameter=failed.parameter,
                    source=ChemicalLabComparisonResult.Source.ORIGINAL,
                    run_number=1,
                    defaults={
                        "value": failed.value,
                        "textual_result": failed.textual_result,
                        "status": failed.status or BatchTestResult.Status.FAIL,
                        "analyst": failed.analyst,
                        "instrument": failed.instrument,
                        "inventory_equipment": failed.inventory_equipment,
                        "instrument_snapshot": failed.instrument_snapshot,
                        "remarks": failed.auto_remark,
                        "recorded_at": failed.tested_at,
                    },
                )
            replacement = form.cleaned_data.get("replacement_analyst")
            if replacement is None:
                raise ValidationError("Select a qualified replacement analyst before routing the repeat test.")
            request = process.analysis_request
            request._ca_assignment_actor = self.request.user
            try:
                request.assigned_analysts.set([replacement.pk])
            finally:
                request.__dict__.pop("_ca_assignment_actor", None)
            process.assigned_analyst = replacement
            process.save(update_fields=["assigned_analyst", "updated_at"])
        selected_labs = list(form.cleaned_data.get("laboratories", ()))
        created_referrals = []
        if decision == "REFER_INTERLAB":
            failed_results = list(process.failed_tests.exclude(parameter_id__isnull=True))
            failed_ids = sorted({result.parameter_id for result in failed_results})
            if not failed_ids:
                raise ValidationError("There are no valid failed parameters to refer to other laboratories.")
            comparison_rules = {}
            for failed in failed_results:
                if not ChemicalLabComparisonResult.objects.filter(
                    process=process,
                    parameter_id=failed.parameter_id,
                    treatment_plant_id=process.batch.treatment_plant_id,
                    source=ChemicalLabComparisonResult.Source.ORIGINAL,
                ).exists() or not ChemicalLabComparisonResult.objects.filter(
                    process=process,
                    parameter_id=failed.parameter_id,
                    treatment_plant_id=process.batch.treatment_plant_id,
                    source=ChemicalLabComparisonResult.Source.REASSIGNMENT,
                ).exists():
                    raise ValidationError(
                        f"{failed.parameter.parameter_name} must be repeated by another originating-plant analyst before referral."
                    )
                parameter = failed.parameter
                plan = ChemicalMethodQualityPlan.current_for(parameter.method, process.batch.treatment_plant) if parameter.method_id else None
                comparison_rules[str(parameter.pk)] = {
                    "plan_id": plan.pk if plan else None,
                    "plan_version": plan.version if plan else None,
                    "consensus_rule": plan.consensus_rule if plan else ChemicalMethodQualityPlan.ConsensusRule.EQUAL_LAB_MEAN,
                    "interlab_tolerance_absolute": str(plan.interlab_tolerance_absolute) if plan and plan.interlab_tolerance_absolute is not None else None,
                    "interlab_tolerance_percent": str(plan.interlab_tolerance_percent) if plan and plan.interlab_tolerance_percent is not None else None,
                    "uncertainty_coverage_factor": str(plan.uncertainty_coverage_factor) if plan else "2",
                    "require_measurement_uncertainty": bool(plan and plan.require_measurement_uncertainty),
                    "require_reference_standard_lot": bool(plan and plan.require_reference_standard_lot),
                    "require_reagent_lot_numbers": bool(plan and plan.require_reagent_lot_numbers),
                }
            for treatment_plant in selected_labs:
                referral, created = ChemicalLabReferral.objects.get_or_create(
                    process=process,
                    treatment_plant=treatment_plant,
                    defaults={
                        "batch": process.batch,
                        "parameter_ids": failed_ids,
                        "comparison_rules_snapshot": comparison_rules,
                        "remarks": remarks,
                        "requested_by": self.request.user,
                    },
                )
                if not created:
                    raise ValidationError(f"A referral to {treatment_plant} already exists for this analysis.")
                created_referrals.append(referral)
                recipients = User.objects.filter(
                    is_active=True,
                    assigned_treatment_plant_id=treatment_plant.pk,
                    is_superuser=False,
                )
                for recipient in recipients.iterator(chunk_size=100):
                    if recipient.has_perm("ca.record_chemical_result"):
                        ChemicalWorkflowAlert.objects.get_or_create(
                            recipient=recipient,
                            dedupe_key=f"ca-interlab-referral:{referral.pk}",
                            defaults={
                                "batch": process.batch,
                                "analysis_request": process.analysis_request if process.analysis_request_id else None,
                                "alert_type": "ca_interlab_referral",
                                "title": "Independent chemical analysis requested",
                                "message": (
                                    f"The {treatment_plant.name} laboratory is requested to verify "
                                    f"{process.batch.chemical.name} batch {process.batch.sample_id}."
                                ),
                                "url": f"/ca/interlaboratory/referrals/{referral.pk}/",
                                "severity": "high",
                            },
                        )

        open_capa = list(
            ChemicalCorrectiveAction.objects.select_for_update()
            .filter(process=process, status=ChemicalCorrectiveAction.Status.OPEN)
        )
        if not self.request.user.is_superuser and any(item.submitted_by_id == self.request.user.pk for item in open_capa):
            raise PermissionDenied("The CAPA submitter cannot independently verify their own corrective action.")
        if open_capa and decision in {"PASS", "REJECT"} and not remarks:
            raise ValidationError("QA remarks are required to verify or reject the submitted corrective actions.")

        process.qa_officer = self.request.user
        process.qa_reviewed_at = timezone.now()
        process.save(update_fields=["qa_officer", "qa_reviewed_at", "updated_at"])

        if decision in {"PASS", "REJECT"}:
            for action in open_capa:
                action.review(
                    actor=self.request.user,
                    accepted=(decision == "PASS"),
                    notes=remarks,
                )
                from .models import ChemicalWorkflowAlert

                ChemicalWorkflowAlert.objects.filter(
                    alert_type="ca_capa_review",
                    dedupe_key=f"capa-review:{action.pk}",
                    acknowledged_at__isnull=True,
                ).update(acknowledged_at=timezone.now(), acknowledged_by=self.request.user)

        self.set_task_data(
            decision=decision,
            remarks=remarks,
            reviewed_by_id=self.request.user.pk,
            reviewed_at=timezone.now().isoformat(),
        )
        from .services.workflow_enhancements import record_workflow_event

        if decision == "PASS":
            # The chemist review is the batch release decision. The flow goes
            # straight to CoA generation after this transition.
            process.mark_approved(approved_by=self.request.user)
        record_workflow_event(
            process,
            event_type="qa_review_recorded",
            action=f"Chemist decision: {decision.lower()}",
            actor=self.request.user,
            from_status=ChemicalAnalysisProcess.WorkflowStatus.QA,
            to_status=process.status,
            details={
                "decision": decision,
                "remarks": remarks,
                "assignable_cause": (form.cleaned_data.get("assignable_cause") or "").strip(),
                "corrective_action": (form.cleaned_data.get("corrective_action") or "").strip(),
                "replacement_analyst_id": (
                    form.cleaned_data.get("replacement_analyst").pk
                    if decision == "REASSIGN" and form.cleaned_data.get("replacement_analyst")
                    else None
                ),
                "referral_ids": [item.pk for item in created_referrals],
                "referred_parameter_ids": sorted({int(value) for item in created_referrals for value in item.parameter_ids}),
                "reviewed_capa_ids": [item.pk for item in open_capa] if decision in {"PASS", "REJECT"} else [],
            },
        )
        return process


def build_interlaboratory_comparison(process):
    referrals = list(
        process.interlaboratory_referrals.filter(status=ChemicalLabReferral.Status.COMPLETED)
        .select_related("treatment_plant", "submitted_by")
        .order_by("treatment_plant__name", "pk")
    )
    referral_ids = [item.pk for item in referrals]
    parameter_ids = sorted({
        int(parameter_id)
        for referral in referrals
        for parameter_id in (referral.parameter_ids or [])
    })
    summaries = []
    for parameter in process.required_specifications().filter(pk__in=parameter_ids).select_related("method").order_by("parameter_name"):
        plan = ChemicalMethodQualityPlan.current_for(parameter.method, process.batch.treatment_plant) if parameter.method_id else None
        rule_snapshot = next((item.comparison_rules_snapshot.get(str(parameter.pk)) for item in referrals if item.comparison_rules_snapshot.get(str(parameter.pk))), {})
        consensus_rule = rule_snapshot.get("consensus_rule") or (plan.consensus_rule if plan else ChemicalMethodQualityPlan.ConsensusRule.EQUAL_LAB_MEAN)
        coverage_factor = Decimal(str(rule_snapshot.get("uncertainty_coverage_factor") or (plan.uncertainty_coverage_factor if plan else "2")))
        rows = ChemicalLabComparisonResult.objects.filter(process=process, parameter=parameter).filter(
            Q(
                source__in=(
                    ChemicalLabComparisonResult.Source.ORIGINAL,
                    ChemicalLabComparisonResult.Source.REASSIGNMENT,
                ),
                treatment_plant_id=process.batch.treatment_plant_id,
            )
            | Q(referral_id__in=referral_ids)
        ).select_related("treatment_plant", "analyst").order_by("treatment_plant__name", "run_number", "pk")
        plant_summaries = []
        for treatment_plant in process.batch.treatment_plant.__class__.objects.filter(
            pk__in=rows.values_list("treatment_plant_id", flat=True).distinct()
        ).order_by("name"):
            lab_rows = list(rows.filter(treatment_plant=treatment_plant))
            numeric_rows = [item for item in lab_rows if item.value is not None]
            all_numeric = bool(lab_rows) and len(numeric_rows) == len(lab_rows)
            lab_mean = (
                (sum((item.value for item in numeric_rows), Decimal("0")) / Decimal(len(numeric_rows))).quantize(
                    INTERLAB_RESULT_QUANTUM,
                    rounding=ROUND_HALF_UP,
                )
                if all_numeric else None
            )
            if lab_mean is not None:
                lab_status = (
                    BatchTestResult.Status.PASS
                    if (parameter.min_limit is None or lab_mean >= parameter.min_limit)
                    and (parameter.max_limit is None or lab_mean <= parameter.max_limit)
                    else BatchTestResult.Status.FAIL
                )
            else:
                if numeric_rows:
                    lab_status = BatchTestResult.Status.FAIL
                else:
                    outcomes = [item.status for item in lab_rows if item.status in BatchTestResult.Status.values]
                    counts = {value: outcomes.count(value) for value in BatchTestResult.Status.values}
                    best = max(counts.values(), default=0)
                    winners = [value for value, count in counts.items() if count == best and count]
                    lab_status = winners[0] if len(winners) == 1 else BatchTestResult.Status.FAIL
            standard_errors = []
            for reading in numeric_rows:
                if reading.expanded_uncertainty is not None:
                    k = reading.uncertainty_coverage_factor or coverage_factor
                    if k > 0:
                        standard_errors.append(reading.expanded_uncertainty / k)
            if len(numeric_rows) > 1:
                read_mean = sum((item.value for item in numeric_rows), Decimal("0")) / Decimal(len(numeric_rows))
                variance = sum(((item.value - read_mean) ** 2 for item in numeric_rows), Decimal("0")) / Decimal(len(numeric_rows) - 1)
                repeatability_u = variance.sqrt() / Decimal(len(numeric_rows)).sqrt()
                standard_errors.append(repeatability_u)
            within_lab_u = (
                (sum((item ** 2 for item in standard_errors), Decimal("0")) / Decimal(len(standard_errors))).sqrt()
                if standard_errors else None
            )
            plant_summaries.append({
                "plant": treatment_plant,
                "readings": lab_rows,
                "mean": lab_mean,
                "status": lab_status,
                "all_numeric": all_numeric,
                "standard_uncertainty": within_lab_u,
                "reading_status_disagreement": len(
                    {item.status for item in lab_rows if item.status in BatchTestResult.Status.values}
                ) > 1 or any(
                    item.status in BatchTestResult.Status.values and item.status != lab_status
                    for item in lab_rows
                ),
            })
        numeric_plants = [item for item in plant_summaries if item["mean"] is not None]
        all_plants_numeric = bool(plant_summaries) and len(numeric_plants) == len(plant_summaries)
        mean_fallback = False
        if all_plants_numeric and consensus_rule == ChemicalMethodQualityPlan.ConsensusRule.POOLED_READINGS:
            all_values = [row.value for lab in plant_summaries for row in lab["readings"] if row.value is not None]
            consensus_candidate = sum(all_values, Decimal("0")) / Decimal(len(all_values)) if all_values else None
        elif all_plants_numeric and consensus_rule == ChemicalMethodQualityPlan.ConsensusRule.UNCERTAINTY_WEIGHTED:
            weighted = [item for item in numeric_plants if item["standard_uncertainty"] is not None and item["standard_uncertainty"] > 0]
            if len(weighted) == len(numeric_plants) and weighted:
                weights = [(item, Decimal("1") / (item["standard_uncertainty"] ** 2)) for item in weighted]
                consensus_candidate = sum((item["mean"] * weight for item, weight in weights), Decimal("0")) / sum((weight for _, weight in weights), Decimal("0"))
            else:
                mean_fallback = True
                consensus_candidate = sum((item["mean"] for item in numeric_plants), Decimal("0")) / Decimal(len(numeric_plants)) if numeric_plants else None
        else:
            consensus_candidate = sum((item["mean"] for item in numeric_plants), Decimal("0")) / Decimal(len(numeric_plants)) if numeric_plants else None
        consensus_value = (
            consensus_candidate.quantize(
                INTERLAB_RESULT_QUANTUM,
                rounding=ROUND_HALF_UP,
            )
            if all_plants_numeric and consensus_candidate is not None else None
        )
        if consensus_value is not None:
            consensus_status = (
                BatchTestResult.Status.PASS
                if (parameter.min_limit is None or consensus_value >= parameter.min_limit)
                and (parameter.max_limit is None or consensus_value <= parameter.max_limit)
                else BatchTestResult.Status.FAIL
            )
        else:
            if numeric_plants:
                consensus_status = BatchTestResult.Status.FAIL
            else:
                outcomes = [item["status"] for item in plant_summaries]
                counts = {value: outcomes.count(value) for value in BatchTestResult.Status.values}
                best = max(counts.values(), default=0)
                winners = [value for value, count in counts.items() if count == best and count]
                consensus_status = winners[0] if len(winners) == 1 else BatchTestResult.Status.FAIL
        lab_means = [item["mean"] for item in numeric_plants if item["mean"] is not None]
        spread = max(lab_means) - min(lab_means) if len(lab_means) > 1 else Decimal("0") if lab_means else None
        spread_percent = (
            spread / abs(consensus_value) * Decimal("100")
            if spread is not None and consensus_value not in (None, Decimal("0")) else None
        )
        abs_tolerance = (
            Decimal(str(rule_snapshot["interlab_tolerance_absolute"]))
            if rule_snapshot.get("interlab_tolerance_absolute") is not None
            else None if "interlab_tolerance_absolute" in rule_snapshot
            else plan.interlab_tolerance_absolute if plan else None
        )
        pct_tolerance = (
            Decimal(str(rule_snapshot["interlab_tolerance_percent"]))
            if rule_snapshot.get("interlab_tolerance_percent") is not None
            else None if "interlab_tolerance_percent" in rule_snapshot
            else plan.interlab_tolerance_percent if plan else None
        )
        tolerance_breached = bool(
            (abs_tolerance is not None and spread is not None and spread > abs_tolerance)
            or (pct_tolerance is not None and spread_percent is not None and spread_percent > pct_tolerance)
        )
        uncertainty_complete = bool(numeric_plants) and all(item["standard_uncertainty"] is not None for item in numeric_plants)
        if all_plants_numeric and numeric_plants:
            within_u = (
                (sum((item["standard_uncertainty"] ** 2 for item in numeric_plants), Decimal("0")).sqrt() / Decimal(len(numeric_plants)))
                if uncertainty_complete else None
            )
            between_u = None
            if len(numeric_plants) > 1:
                mean_lab_value = sum((item["mean"] for item in numeric_plants), Decimal("0")) / Decimal(len(numeric_plants))
                between_variance = sum(((item["mean"] - mean_lab_value) ** 2 for item in numeric_plants), Decimal("0")) / Decimal(len(numeric_plants) - 1)
                between_u = between_variance.sqrt() / Decimal(len(numeric_plants)).sqrt()
            if consensus_rule == ChemicalMethodQualityPlan.ConsensusRule.UNCERTAINTY_WEIGHTED and not mean_fallback and uncertainty_complete:
                weights = [Decimal("1") / (item["standard_uncertainty"] ** 2) for item in numeric_plants]
                combined_u = (Decimal("1") / sum(weights, Decimal("0"))).sqrt()
            elif consensus_rule == ChemicalMethodQualityPlan.ConsensusRule.POOLED_READINGS:
                values = [row.value for item in plant_summaries for row in item["readings"] if row.value is not None]
                if len(values) > 1:
                    pooled_mean = sum(values, Decimal("0")) / Decimal(len(values))
                    variance = sum(((value - pooled_mean) ** 2 for value in values), Decimal("0")) / Decimal(len(values) - 1)
                    combined_u = (variance / Decimal(len(values))).sqrt()
                else:
                    combined_u = within_u
            else:
                components = [value for value in (within_u, between_u) if value is not None]
                combined_u = sum((value ** 2 for value in components), Decimal("0")).sqrt() if components else None
            expanded_u = combined_u * coverage_factor if combined_u is not None else None
        else:
            expanded_u = None
        require_uncertainty = bool(rule_snapshot["require_measurement_uncertainty"]) if "require_measurement_uncertainty" in rule_snapshot else bool(plan and plan.require_measurement_uncertainty)
        status_disagreement = (
            len({item["status"] for item in plant_summaries}) > 1
            or any(item["reading_status_disagreement"] for item in plant_summaries)
        )
        requires_chemist_attention = bool(
            tolerance_breached
            or status_disagreement
            or mean_fallback
            or (require_uncertainty and not uncertainty_complete)
        )
        summaries.append({
            "parameter": parameter,
            "laboratories": plant_summaries,
            "mean": consensus_value,
            "status": consensus_status,
            "numeric_average": all_plants_numeric,
            "incompatible_types": bool(numeric_plants and not all_plants_numeric),
            "concordant": len({item["status"] for item in plant_summaries}) <= 1,
            "status_disagreement": status_disagreement,
            "consensus_rule": consensus_rule,
            "spread": spread,
            "spread_percent": spread_percent,
            "absolute_tolerance": abs_tolerance,
            "percent_tolerance": pct_tolerance,
            "tolerance_breached": tolerance_breached,
            "uncertainty_complete": uncertainty_complete,
            "expanded_uncertainty": expanded_u,
            "coverage_factor": coverage_factor,
            "requires_chemist_attention": requires_chemist_attention,
            "mean_fallback": mean_fallback,
        })
    return referrals, summaries


class InterlaboratoryReviewTaskView(ChemicalWorkflowTaskView):
    form_class = InterlaboratoryReviewForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        process = self.get_process()
        _, comparison = build_interlaboratory_comparison(process)
        kwargs.update(
            user=self.request.user,
            process=process,
            requires_disagreement_ack=any(item["requires_chemist_attention"] for item in comparison),
        )
        return kwargs

    def get_context_data(self, *, form, **extra):
        context = super().get_context_data(form=form, **extra)
        process = self.get_process()
        _, comparison = build_interlaboratory_comparison(process)
        referrals = list(
            process.interlaboratory_referrals.select_related("treatment_plant", "submitted_by")
            .order_by("treatment_plant__name", "pk")
        )
        context.update(
            task_title="Interlaboratory result comparison",
            task_prompt=(
                "Review the originating-plant repeat and every peer laboratory result, the configurable consensus rule, "
                "each individual run outcome, between-laboratory spread, acceptance tolerance and combined measurement uncertainty. "
                "A tolerance breach, result-status disagreement, fallback calculation or missing required uncertainty "
                "must be acknowledged and explained before finalization."
            ),
            interlab_referrals=referrals,
            interlab_comparison=comparison,
            interlab_pending=self.get_process().interlaboratory_referrals.exclude(
                status=ChemicalLabReferral.Status.COMPLETED
            ).exists(),
            interlab_attention=any(item["requires_chemist_attention"] for item in comparison),
        )
        return context

    def save_form(self, form):
        self.require_permission("ca.review_chemical_analysis")
        process = self.get_process()
        batch = self.get_batch()
        referrals, comparison = build_interlaboratory_comparison(process)
        if not referrals or process.interlaboratory_referrals.exclude(status=ChemicalLabReferral.Status.COMPLETED).exists():
            raise ValidationError("Every participating laboratory must submit all requested results before comparison.")
        if not comparison:
            raise ValidationError("No comparable analytical parameters were submitted by the participating laboratories.")

        finalized = []
        for summary in comparison:
            parameter = summary["parameter"]
            result = BatchTestResult.objects.filter(batch=batch, parameter=parameter).first()
            if result is None:
                raise ValidationError(f"The originating batch has no recorded result for {parameter.parameter_name}.")
            value = summary["mean"]
            lab_names = ", ".join(item["plant"].name for item in summary["laboratories"])
            if summary["numeric_average"]:
                text = (
                    f"Interlaboratory {summary['consensus_rule'].replace('_', ' ').lower()} result {value} {parameter.unit}; "
                    f"range {summary['spread']} {parameter.unit} across {len(summary['laboratories'])} laboratories."
                )
                if summary["tolerance_breached"]:
                    text += " Configured between-laboratory tolerance exceeded; chemist review required."
                if summary["mean_fallback"]:
                    text += " Uncertainty-weighted rule unavailable; equal-laboratory mean used and flagged for review."
                if summary["status_disagreement"]:
                    text += " Individual run or laboratory result statuses disagree; chemist review required."
                if summary["expanded_uncertainty"] is not None:
                    text += f" Expanded uncertainty (k={summary['coverage_factor']}) {summary['expanded_uncertainty']}."
                elif not summary["uncertainty_complete"]:
                    text += " Combined uncertainty incomplete because one or more laboratory readings lack uncertainty estimates."
                status = summary["status"]
            else:
                counts = {}
                for item in summary["laboratories"]:
                    counts[item["status"]] = counts.get(item["status"], 0) + 1
                outcome_text = ", ".join(f"{key}: {count}" for key, count in sorted(counts.items()))
                if summary["incompatible_types"]:
                    text = (
                        f"Incompatible numeric and qualitative result types were submitted across {lab_names}; "
                        "no numerical mean is valid. The comparison is conservatively marked failed."
                    )
                    status = BatchTestResult.Status.FAIL
                else:
                    text = f"Qualitative interlaboratory comparison across {lab_names}: {outcome_text}."
                status = summary["status"]
            result.value = value
            result.textual_result = text[:255]
            result.status = status
            result.analyst = self.request.user
            result.expanded_uncertainty = summary["expanded_uncertainty"]
            result.uncertainty_coverage_factor = summary["coverage_factor"] if summary["expanded_uncertainty"] is not None else None
            result.quality_flags = [
                flag for flag in (result.quality_flags or [])
                if str(flag).startswith("QC_FAIL_")
                or flag in {"INSTRUMENT_UNCALIBRATED", "INSTRUMENT_UNAVAILABLE", "INSTRUMENT_MAINTENANCE_DUE"}
            ]
            if any(
                str(flag).startswith("QC_FAIL_")
                or flag in {"INSTRUMENT_UNCALIBRATED", "INSTRUMENT_UNAVAILABLE"}
                for flag in result.quality_flags
            ):
                result.status = BatchTestResult.Status.FAIL
            result.revision_reason = (
                f"Chemist finalized interlaboratory comparison using {summary['consensus_rule']}; "
                "the configured decision rule, spread, tolerance and uncertainty assessment are recorded in the comparison event."
            )[:500]
            result.save()
            if summary["requires_chemist_attention"]:
                from .inventory_instruments import trigger_configured_alert

                labs_context = [
                    {
                        "treatment_plant_id": item["plant"].pk,
                        "treatment_plant": item["plant"].name,
                        "mean": str(item["mean"]) if item["mean"] is not None else None,
                        "status": item["status"],
                    }
                    for item in summary["laboratories"]
                ]
                trigger_configured_alert(
                    instance=result,
                    alert_type="ca_interlab_disagreement",
                    context_data={
                        "source": "chemical_interlaboratory_consensus",
                        "plant_id": batch.treatment_plant_id,
                        "batch_id": batch.pk,
                        "batch": batch.sample_id or batch.batch_number,
                        "chemical": batch.chemical.name,
                        "parameter": parameter.parameter_name,
                        "consensus_mean": str(value) if value is not None else "",
                        "consensus_status": status,
                        "consensus_rule": summary["consensus_rule"],
                        "between_laboratory_spread": str(summary["spread"]) if summary["spread"] is not None else "",
                        "between_laboratory_spread_percent": str(summary["spread_percent"]) if summary["spread_percent"] is not None else "",
                        "absolute_tolerance": str(summary["absolute_tolerance"]) if summary["absolute_tolerance"] is not None else "",
                        "percent_tolerance": str(summary["percent_tolerance"]) if summary["percent_tolerance"] is not None else "",
                        "tolerance_breached": summary["tolerance_breached"],
                        "status_disagreement": summary["status_disagreement"],
                        "laboratory_count": len(summary["laboratories"]),
                        "laboratories": labs_context,
                        "expanded_uncertainty": str(summary["expanded_uncertainty"]) if summary["expanded_uncertainty"] is not None else "",
                    },
                    batch=batch,
                    request=process.analysis_request if process.analysis_request_id else None,
                    title="Interlaboratory results require chemist attention",
                    message=(
                        f"{parameter.parameter_name} on batch {batch.sample_id or batch.batch_number}: "
                        f"between-laboratory spread {summary['spread']} {parameter.unit}; "
                        f"tolerance breached={summary['tolerance_breached']}; "
                        f"status disagreement={summary['status_disagreement']}. Review the recorded readings and consensus rule."
                    ),
                    url=f"/ca/workflow/{process.pk}/",
                    severity="high",
                    dedupe_key=f"ca-interlab-disagreement:{process.pk}:{parameter.pk}",
                    permissions=("ca.review_chemical_analysis",),
                )
            finalized.append({
                "parameter_id": parameter.pk,
                "parameter": parameter.parameter_name,
                "laboratories": [
                    {
                        "plant_id": item["plant"].pk,
                        "plant": item["plant"].name,
                        "readings": [str(reading.value) if reading.value is not None else reading.textual_result for reading in item["readings"]],
                        "laboratory_mean": str(item["mean"]) if item["mean"] is not None else None,
                        "standard_uncertainty": str(item["standard_uncertainty"]) if item["standard_uncertainty"] is not None else None,
                        "status": item["status"],
                    }
                    for item in summary["laboratories"]
                ],
                "average": str(value) if value is not None else None,
                "consensus_rule": summary["consensus_rule"],
                "between_laboratory_spread": str(summary["spread"]) if summary["spread"] is not None else None,
                "between_laboratory_spread_percent": str(summary["spread_percent"]) if summary["spread_percent"] is not None else None,
                "absolute_tolerance": str(summary["absolute_tolerance"]) if summary["absolute_tolerance"] is not None else None,
                "percent_tolerance": str(summary["percent_tolerance"]) if summary["percent_tolerance"] is not None else None,
                "tolerance_breached": summary["tolerance_breached"],
                "status_disagreement": summary["status_disagreement"],
                "expanded_uncertainty": str(summary["expanded_uncertainty"]) if summary["expanded_uncertainty"] is not None else None,
                "uncertainty_complete": summary["uncertainty_complete"],
                "requires_chemist_attention": summary["requires_chemist_attention"],
                "status": status,
                "remarks": text,
            })

        if process.failed_tests.exists():
            final_remarks = (form.cleaned_data.get("remarks") or "").strip()
        else:
            final_remarks = (form.cleaned_data.get("remarks") or "").strip()
            process.mark_approved(approved_by=self.request.user)
            process.refresh_from_db()
        failed_after_average = process.failed_tests.exists()
        ChemicalWorkflowEvent.objects.create(
            process=process,
            batch=batch,
            analysis_request=process.analysis_request if process.analysis_request_id else None,
            event_type="interlaboratory_consensus_recorded",
            action="laboratory comparison average finalized",
            actor=self.request.user,
            from_status=ChemicalAnalysisProcess.WorkflowStatus.QA,
            to_status=process.status,
            details={
                "originating_treatment_plant_id": batch.treatment_plant_id,
                "referral_ids": [item.pk for item in referrals],
                "laboratory_count": len({item.treatment_plant_id for item in referrals} | {batch.treatment_plant_id}),
                "remarks": final_remarks,
                "laboratory_remarks": [
                    {
                        "treatment_plant": item.treatment_plant.name,
                        "remarks": item.submission_remarks,
                    }
                    for item in referrals
                    if item.submission_remarks
                ],
                "comparison": finalized,
                "failed_after_average": failed_after_average,
            },
        )
        for investigation in process.oos_investigations.filter(status=ChemicalOOSInvestigation.Status.OPEN):
            if failed_after_average:
                ChemicalOOSInvestigation.objects.filter(pk=investigation.pk).update(
                    comparison_snapshot={"parameters": finalized, "chemist_remarks": final_remarks}
                )
            else:
                investigation.close(
                    actor=self.request.user,
                    disposition="Consolidated interlaboratory result passed the originating specification and was approved.",
                    comparison={"parameters": finalized, "chemist_remarks": final_remarks},
                )
        ChemicalWorkflowAlert.objects.filter(
            alert_type="ca_interlab_results_ready",
            dedupe_key__in=[f"ca-interlab-results-ready:{item.pk}" for item in referrals],
            acknowledged_at__isnull=True,
        ).update(acknowledged_at=timezone.now(), acknowledged_by=self.request.user)
        messages.success(
            self.request,
            "Interlaboratory comparison finalized. The certificate will be generated from the originating treatment plant with the consolidated results.",
        )
        self.set_task_data(
            interlaboratory_consensus_recorded=True,
            referral_ids=[item.pk for item in referrals],
            laboratory_count=len({item.treatment_plant_id for item in referrals} | {batch.treatment_plant_id}),
            failed_after_average=failed_after_average,
        )
        return process


class CAPATaskView(ChemicalWorkflowTaskView):
    form_class = CAPAForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(user=self.request.user, process=self.get_process())
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.review_chemical_analysis")
        process = self.get_process()
        if process.status != ChemicalAnalysisProcess.WorkflowStatus.QA:
            raise ValidationError("CAPA can only be initiated during chemist review.")
        decision_data = self.get_task_data()
        action = ChemicalCorrectiveAction.submit(
            process=process,
            actor=self.request.user,
            qa_observation=decision_data.get("remarks"),
            corrective_action=form.cleaned_data.get("corrective_action"),
            preventive_action=form.cleaned_data.get("preventive_action"),
        )
        from .services.workflow_notifications import create_workflow_alert

        create_workflow_alert(
            batch=process.batch,
            request=process.analysis_request if process.analysis_request_id else None,
            alert_type="ca_capa_review",
            title="Corrective action awaits independent QA verification",
            message=f"CAPA for batch {process.batch.sample_id} was submitted and requires an independent chemist review.",
            url="/ca/tasks/",
            severity="high",
            dedupe_key=f"capa-review:{action.pk}",
            permissions=("ca.review_chemical_analysis",),
            exclude_user_ids=[
                *process.assigned_analysts.values_list("pk", flat=True),
                self.request.user.pk,
            ],
        )
        self.set_task_data(
            capa_submitted=True,
            corrective_action=(form.cleaned_data.get("corrective_action") or "").strip(),
            preventive_action=(form.cleaned_data.get("preventive_action") or "").strip(),
            corrective_action_id=action.pk,
            capa_submitted_by_id=self.request.user.pk,
            capa_submitted_at=timezone.now().isoformat(),
        )
        from .services.workflow_enhancements import record_workflow_event

        record_workflow_event(
            process,
            event_type="capa_submitted",
            action="corrective and preventive actions submitted",
            actor=self.request.user,
            from_status=process.status,
            to_status=process.status,
            details={"capa_id": action.pk, "qa_observation": action.qa_observation},
        )
        return process


class ApprovalTaskView(ChemicalWorkflowTaskView):
    form_class = ApprovalForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(user=self.request.user, process=self.get_process(), batch=self.get_batch())
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.approve_chemical_batch")
        process = self.get_process()
        if process.status != ChemicalAnalysisProcess.WorkflowStatus.QA:
            raise ValidationError("Chemical analysis approval is only available after chemist review.")
        if process.has_pending_tests or process.failed_tests.exists() or not process.is_analysis_complete:
            raise ValidationError("The analysis must be complete with no failed results before approval.")
        if not process.is_independent_approver(self.request.user):
            raise PermissionDenied("Batch approval must be performed by someone independent of analysis and chemist review.")
        if str(form.cleaned_data.get("decision", "APPROVE")).upper() != "APPROVE":
            raise ValidationError("Only APPROVE is valid at this stage.")
        process.mark_approved(approved_by=self.request.user)
        process.refresh_from_db()
        return process


class ReferChemicalToKEBSTaskView(ChemicalWorkflowTaskView):
    form_class = KEBSReferralForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(user=self.request.user, process=self.get_process(), batch=self.get_batch())
        return kwargs

    def get_context_data(self, *, form, **extra):
        context = super().get_context_data(form=form, **extra)
        context.update(
            task_title="Forward failed chemical to KEBS",
            task_prompt=(
                "The report-only Certificate of Analysis has been generated. Record the KEBS handover or shipment "
                "details and dispatch evidence. This closes the originating plant's investigation and custody record."
            ),
        )
        return context

    def save_form(self, form):
        self.require_permission("ca.reject_chemical_batch")
        process = self.get_process()
        batch = self.get_batch()
        if process.status != ChemicalAnalysisProcess.WorkflowStatus.QA or not process.failed_tests.exists():
            raise ValidationError("Only a persistently failed batch awaiting its failed-result certificate can be forwarded to KEBS.")
        coa = ChemicalCoA.objects.select_for_update().filter(batch=batch, pdf_file__isnull=False).first()
        if coa is None:
            raise ValidationError("Generate the Certificate of Analysis before forwarding the chemical to KEBS.")
        if ChemicalKEBSReferral.objects.filter(batch=batch).exists():
            raise ValidationError("This chemical batch has already been referred to KEBS.")
        details = form.cleaned_data
        referral = ChemicalKEBSReferral.objects.create(
            batch=batch,
            process=process,
            coa=coa,
            kebs_reference=(details.get("kebs_reference") or "").strip(),
            dispatch_method=(details.get("dispatch_method") or "").strip(),
            shipment_reference=(details.get("shipment_reference") or "").strip(),
            assignable_cause=(details.get("assignable_cause") or "").strip(),
            comparison_summary=(
                process.workflow_events.filter(event_type="interlaboratory_consensus_recorded")
                .order_by("-occurred_at", "-pk").values_list("details", flat=True).first() or {}
            ),
            remarks=(details.get("remarks") or "").strip(),
            evidence=details.get("evidence"),
            dispatched_by=self.request.user,
        )
        process.mark_rejected(
            performed_by=self.request.user,
            reason="Persistently non-conforming chemical forwarded to KEBS for independent assessment.",
        )
        from .services.workflow_enhancements import record_custody_event
        record_custody_event(
            batch,
            event_type=ChemicalCustodyEvent.EventType.TRANSFERRED,
            actor=self.request.user,
            from_location=str(batch.location or batch.treatment_plant),
            to_location="Kenya Bureau of Standards (KEBS)",
            seal_number=referral.shipment_reference,
            remarks=(
                f"Failed chemical and report-only CoA forwarded by {referral.dispatch_method}. "
                f"KEBS reference: {referral.kebs_reference or 'pending'}; "
                f"cause: {referral.assignable_cause}"
            ),
            evidence=referral.evidence,
        )
        for investigation in process.oos_investigations.filter(status=ChemicalOOSInvestigation.Status.OPEN):
            investigation.close(
                actor=self.request.user,
                disposition="Non-conforming chemical and failed-result CoA forwarded to KEBS for independent assessment.",
                comparison=referral.comparison_summary,
            )
        ChemicalWorkflowEvent.objects.create(
            process=process,
            batch=batch,
            analysis_request=process.analysis_request if process.analysis_request_id else None,
            event_type="chemical_forwarded_to_kebs",
            action="non-conforming chemical and certificate forwarded to KEBS",
            actor=self.request.user,
            from_status=ChemicalAnalysisProcess.WorkflowStatus.QA,
            to_status=ChemicalAnalysisProcess.WorkflowStatus.REJECTED,
            details={
                "referral_id": referral.pk,
                "coa_id": coa.pk,
                "kebs_reference": referral.kebs_reference,
                "dispatch_method": referral.dispatch_method,
                "shipment_reference": referral.shipment_reference,
                "assignable_cause": referral.assignable_cause,
                "remarks": referral.remarks,
            },
        )
        self.set_task_data(
            kebs_referral_id=referral.pk,
            kebs_dispatched=True,
            kebs_dispatched_at=referral.dispatched_at.isoformat(),
        )
        messages.success(self.request, "Failed chemical and report-only Certificate of Analysis were recorded as forwarded to KEBS.")
        return referral


class GenerateChemicalCoATaskView(ChemicalWorkflowTaskView):
    form_class = CoAGenerationForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(user=self.request.user, process=self.get_process(), batch=self.get_batch())
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.approve_chemical_coa")
        process = self.get_process()
        batch = self.get_batch()
        has_failed_results = process.failed_tests.exists()
        interlaboratory_consensus_completed = bool(
            process.workflow_events.filter(event_type="interlaboratory_consensus_recorded").exists()
            and process.interlaboratory_referrals.exists()
            and not process.interlaboratory_referrals.exclude(
                status=ChemicalLabReferral.Status.COMPLETED
            ).exists()
        )
        failed_certificate_requested = bool(
            has_failed_results
            and process.status == ChemicalAnalysisProcess.WorkflowStatus.QA
            and batch.status == ChemicalBatch.Status.ANALYSED
            and process.qa_officer_id
            and process.qa_reviewed_at
            and (
                interlaboratory_consensus_completed
                or (
                    not process.interlaboratory_referrals.filter(status=ChemicalLabReferral.Status.PENDING).exists()
                    and process.workflow_events.filter(
                        event_type="qa_review_recorded",
                        details__decision__in=["REJECT", "ISSUE_FAILED_COA"],
                    ).exists()
                )
            )
        )
        standard_generation = bool(
            not has_failed_results
            and process.status == ChemicalAnalysisProcess.WorkflowStatus.APPROVED
            and batch.status == ChemicalBatch.Status.APPROVED
        )
        if not (standard_generation or failed_certificate_requested):
            raise ValidationError(
                "A passing certificate follows approval. A failed result requires a chemist disposition and, when laboratories were referred, their completed comparison before the originating plant can issue the report-only certificate."
            )
        try:
            # Isolate CoA and file metadata writes from Viewflow's wider task
            # transaction. A swallowed database error in a post_save signal
            # must not leave the whole workflow transaction unusable.
            with transaction.atomic():
                coa, _ = ChemicalCoA.objects.select_for_update().get_or_create(batch=batch)
                if failed_certificate_requested and coa.is_approved:
                    raise ValidationError(
                        "An approved certificate is immutable and cannot be replaced with failed results."
                    )
                if failed_certificate_requested or not (coa.pdf_file and coa.qr_code):
                    coa.prepare_for_generation()
                    coa.save(generate_assets=True, regenerate_watermark=True)
                    # Generate QR code and PDF synchronously. Its post_save
                    # callback runs at commit, after this task needs the assets.
                    _generate_coa_assets(coa, force=failed_certificate_requested)
                    coa.refresh_from_db()
                if not coa.pdf_file or not coa.qr_code:
                    raise ValidationError("The CoA assets could not be generated completely.")
                batch.coa_generated = True
                batch.save(update_fields=["coa_generated", "updated_at"])
                self.set_task_data(
                    coa_id=coa.pk,
                    coa_generated=True,
                    coa_approved=coa.is_approved,
                    nonconforming_certificate=failed_certificate_requested,
                )
        except DatabaseError as exc:
            logger.exception("Database error while generating CoA for batch %s", batch.pk)
            raise ValidationError(
                "The certificate could not be generated because a database write failed. "
                "The error has been logged; correct it and retry certificate generation."
            ) from exc
        return coa


class ApproveChemicalCoATaskView(ChemicalWorkflowTaskView):
    form_class = CoAApprovalForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(user=self.request.user, process=self.get_process(), batch=self.get_batch())
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.approve_chemical_coa")
        process = self.get_process()
        batch = self.get_batch()
        if process.status != ChemicalAnalysisProcess.WorkflowStatus.APPROVED:
            raise ValidationError("CoA approval is only available after chemical analysis approval.")
        coa = ChemicalCoA.objects.select_for_update().filter(batch=batch).first()
        if coa is None:
            raise ValidationError("The Certificate of Analysis has not been generated.")
        if not coa.pdf_file or not coa.qr_code:
            raise ValidationError("The Certificate of Analysis is incomplete.")
        if not coa.is_approved:
            performing_ids = process.performing_analyst_ids
            if (
                self.request.user.pk in performing_ids
                or process.qa_officer_id == self.request.user.pk
                or process.approver_id == self.request.user.pk
            ):
                raise PermissionDenied("CoA approval must be independent of analysis, chemist review, and batch approval.")
            coa.approve(approved_by=self.request.user)
            coa.refresh_from_db()
        if not coa.is_approved:
            raise ValidationError("The Certificate of Analysis was not approved.")
        self.set_task_data(coa_id=coa.pk, coa_approved=True, coa_approved_by_id=coa.approved_by_id)
        return coa


class RejectAnalysisTaskView(ChemicalWorkflowTaskView):
    form_class = RejectAnalysisForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(user=self.request.user, process=self.get_process(), batch=self.get_batch())
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.reject_chemical_batch")
        process = self.get_process()
        if process.status not in {
            ChemicalAnalysisProcess.WorkflowStatus.ANALYSIS,
            ChemicalAnalysisProcess.WorkflowStatus.RETEST,
            ChemicalAnalysisProcess.WorkflowStatus.QA,
        }:
            raise ValidationError("The analysis cannot be rejected at this workflow stage.")
        reason = (form.cleaned_data.get("reason") or form.cleaned_data.get("remarks") or "").strip()
        if not reason:
            raise ValidationError("A rejection reason is required.")
        prior_status = process.status
        process.mark_rejected(performed_by=self.request.user, reason=reason)
        self.set_task_data(decision="REJECT", rejection_reason=reason, rejected_by_id=self.request.user.pk, rejected_at=timezone.now().isoformat())
        from .services.workflow_enhancements import record_workflow_event

        record_workflow_event(
            process,
            event_type="batch_rejected",
            action="chemical batch rejected",
            actor=self.request.user,
            from_status=prior_status,
            to_status=ChemicalAnalysisProcess.WorkflowStatus.REJECTED,
            details={"reason": reason},
        )
        return process


class ConfirmWorkflowActionTaskView(ChemicalWorkflowTaskView):
    form_class = ConfirmActionForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(user=self.request.user, batch=self.get_batch(), process=self.get_process())
        return kwargs

    def save_form(self, form):
        process = self.get_process()
        batch = self.get_batch()
        if process.status != ChemicalAnalysisProcess.WorkflowStatus.APPROVED:
            raise ValidationError("Batch disposition requires an approved analysis.")
        coa = ChemicalCoA.objects.filter(batch=batch).first()
        if coa is None or not coa.is_approved:
            raise ValidationError("An approved Certificate of Analysis is required before disposition.")
        action = str(form.cleaned_data.get("action", "")).upper()
        if action not in {"UTILIZE", "RETURN"}:
            raise ValidationError("Invalid batch disposition.")
        self.require_permission({"UTILIZE": "ca.utilize_chemical_batch", "RETURN": "ca.return_chemical_batch"}[action])
        self.set_task_data(action=action, disposition_selected_by_id=self.request.user.pk, disposition_selected_at=timezone.now().isoformat())
        return action


class UtilizeChemicalBatchTaskView(ChemicalWorkflowTaskView):
    form_class = UtilizationForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(batch=self.get_batch(), process=self.get_process(), user=self.request.user)
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.utilize_chemical_batch")
        process = self.get_process()
        batch = self.get_batch()
        if process.status != ChemicalAnalysisProcess.WorkflowStatus.APPROVED or batch.status != ChemicalBatch.Status.APPROVED:
            raise ValidationError("Only an approved chemical batch can be utilized.")
        coa = ChemicalCoA.objects.filter(batch=batch).first()
        if coa is None or not coa.is_approved:
            raise ValidationError("An approved Certificate of Analysis is required.")
        quantity = form.cleaned_data.get("quantity_used")
        batch.quantity_utilized = quantity if quantity is not None else batch.quantity_received
        batch.utilization_remarks = (form.cleaned_data.get("remarks") or "").strip()
        batch.utilized_by = self.request.user
        batch.utilized_at = timezone.now()
        from .services.workflow_enhancements import record_custody_event, record_inventory_movement

        quantity = batch.quantity_utilized
        record_inventory_movement(
            batch,
            movement_type="UTILIZATION",
            quantity=quantity,
            actor=self.request.user,
            idempotency_key=f"batch:{batch.pk}:utilization",
            notes=batch.utilization_remarks,
        )
        batch.transition_to(ChemicalBatch.Status.UTILIZED, performed_by=self.request.user)
        batch.save(update_fields=[
            "quantity_utilized", "utilization_remarks", "utilized_by",
            "utilized_at", "updated_at",
        ])
        process.mark_disposition_completed(ChemicalAnalysisProcess.WorkflowStatus.UTILIZED, performed_by=self.request.user)
        process.mark_completed(performed_by=self.request.user)
        record_custody_event(
            batch,
            event_type="UTILIZED",
            actor=self.request.user,
            from_location=str(batch.location),
            to_location="Treatment process",
            remarks=batch.utilization_remarks,
        )
        self.set_task_data(disposition="UTILIZED", completed_by_id=self.request.user.pk, completed_at=timezone.now().isoformat())
        return batch


class ReturnChemicalBatchTaskView(ChemicalWorkflowTaskView):
    form_class = ReturnToSupplierForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(batch=self.get_batch(), process=self.get_process(), user=self.request.user)
        return kwargs

    def save_form(self, form):
        self.require_permission("ca.return_chemical_batch")
        process = self.get_process()
        batch = self.get_batch()
        if batch.status not in {ChemicalBatch.Status.APPROVED, ChemicalBatch.Status.REJECTED}:
            raise ValidationError("The chemical batch cannot currently be returned.")
        quantity = form.cleaned_data.get("quantity_returned")
        batch.quantity_returned = quantity if quantity is not None else batch.quantity_received
        batch.return_reason = (form.cleaned_data.get("reason") or "").strip()
        batch.returned_by = self.request.user
        batch.returned_at = timezone.now()
        from .services.workflow_enhancements import record_custody_event, record_inventory_movement

        quantity = batch.quantity_returned
        record_inventory_movement(
            batch,
            movement_type="RETURN",
            quantity=quantity,
            actor=self.request.user,
            idempotency_key=f"batch:{batch.pk}:return",
            notes=batch.return_reason,
        )
        batch.transition_to(ChemicalBatch.Status.RETURNED, performed_by=self.request.user)
        batch.save(update_fields=[
            "quantity_returned", "return_reason", "returned_by",
            "returned_at", "updated_at",
        ])
        process.mark_disposition_completed(ChemicalAnalysisProcess.WorkflowStatus.RETURNED, performed_by=self.request.user)
        process.mark_completed(performed_by=self.request.user)
        record_custody_event(
            batch,
            event_type="RETURNED",
            actor=self.request.user,
            from_location=str(batch.location),
            to_location=str(batch.supplier),
            remarks=batch.return_reason,
        )
        self.set_task_data(disposition="RETURNED", completed_by_id=self.request.user.pk, completed_at=timezone.now().isoformat())
        return batch


class ChemicalAnalysisFlow(flow.Flow):
    process_class = ChemicalAnalysisProcess
    lock_impl = lock.select_for_update_lock

    start = (
        flow.Start(StartChemicalBatchView.as_view())
        .Annotation(title="Register Chemical Batch")
        .Permission("ca.receive_chemical_batch")
        .Next(this.receive)
    )

    receive = (
        flow.View(ReceiveChemicalBatchTaskView.as_view())
        .Annotation(title="Confirm Chemical Receipt")
        .Permission("ca.receive_chemical_batch")
        .Next(this.request_check)
    )

    request_check = (
        flow.If(lambda activation: ChemicalAnalysisFlow.has_analysis_request(activation))
        .Then(this.request_assignment_check)
        .Else(this.create_request)
    )

    create_request = (
        flow.View(CreateChemicalAnalysisRequestTaskView.as_view())
        .Annotation(title="Create Chemical Analysis Request")
        .Permission("ca.create_analysis_request")
        .Next(this.request_assignment_check)
    )

    request_assignment_check = (
        flow.If(lambda activation: ChemicalAnalysisFlow.has_assigned_analysts(activation))
        .Then(this.checklist)
        .Else(this.assign_analysts)
    )

    assign_analysts = (
        flow.View(AnalystAssignmentTaskView.as_view())
        .Annotation(title="Assign Chemical Analysis Analysts")
        .Permission("ca.create_analysis_request")
        .Next(this.checklist)
    )

    checklist = (
        flow.View(ChecklistTaskView.as_view())
        .Annotation(title="Receiving Checklist")
        .Permission("ca.complete_receiving_checklist")
        .Next(this.checklist_decision)
    )

    checklist_decision = (
        flow.If(lambda activation: ChemicalAnalysisFlow.checklist_starts_analysis(activation))
        .Then(this.record_result)
        .Else(this.start_analysis)
    )

    complete_checklist = (
        flow.View(CompleteChecklistTaskView.as_view())
        .Annotation(title="Complete Receiving Checklist")
        .Permission("ca.complete_receiving_checklist")
        .Next(this.assignment_check)
    )

    assignment_check = (
        flow.If(lambda activation: ChemicalAnalysisFlow.has_assigned_analysts(activation))
        .Then(this.start_analysis)
        .Else(this.assign_analysts)
    )

    start_analysis = (
        flow.View(StartAnalysisTaskView.as_view())
        .Annotation(title="Start Chemical Analysis")
        .Permission("ca.start_chemical_analysis")
        .Next(this.record_result)
    )

    record_result = (
        flow.View(RecordTestResultTaskView.as_view())
        .Annotation(title="Record Chemical Test Result")
        .Permission("ca.record_chemical_result")
        .Next(this.check_result_input, task_data=lambda activation: ChemicalAnalysisFlow.result_task_data(activation))
    )

    check_result_input = (
        flow.If(lambda activation: ChemicalAnalysisFlow.result_requires_input(activation))
        .Then(this.record_input)
        .Else(this.check_analysis_progress)
    )

    record_input = (
        flow.View(RecordDynamicAnalysisInputTaskView.as_view())
        .Annotation(title="Record Analytical Method Inputs")
        .Permission("ca.record_chemical_result")
        .Next(this.check_analysis_progress)
    )

    check_analysis_progress = (
        flow.If(lambda activation: ChemicalAnalysisFlow.analysis_complete(activation))
        .Then(this.complete_analysis)
        .Else(this.record_result)
    )

    complete_analysis = (
        flow.Function(
            this._auto_complete_analysis
        )
        .Annotation(title="Complete Chemical Analysis")
        .Next(this.quality_control_gate)
    )

    quality_control_gate = (
        flow.If(lambda activation: ChemicalAnalysisFlow.has_method_quality_controls(activation))
        .Annotation(title="Check method quality-control requirements")
        .Then(this.record_method_qc)
        .Else(this.qa_review)
    )

    record_method_qc = (
        flow.View(RecordMethodQCTaskView.as_view())
        .Annotation(title="Record Method Quality Controls")
        .Permission("ca.record_chemical_result")
        .Next(this.qa_review)
    )

    qa_review = (
        flow.View(QAReviewTaskView.as_view())
        .Annotation(title="Chemist Review")
        .Permission("ca.review_chemical_analysis")
        .Next(this.qa_decision, task_data=lambda activation: ChemicalAnalysisFlow.qa_task_data(activation))
    )

    qa_decision = (
        flow.If(lambda activation: ChemicalAnalysisFlow.qa_is_retest(activation))
        .Annotation(title="Chemist routing: retest")
        .Then(this.retest)
        .Else(this.qa_interlaboratory_decision)
    )

    qa_interlaboratory_decision = (
        flow.If(lambda activation: ChemicalAnalysisFlow.qa_is_interlaboratory_referral(activation))
        .Annotation(title="Chemist routing: interlaboratory verification")
        .Then(this.interlaboratory_review)
        .Else(this.qa_capa_decision)
    )

    qa_capa_decision = (
        flow.If(lambda activation: ChemicalAnalysisFlow.qa_is_capa(activation))
        .Annotation(title="Chemist routing: corrective action")
        .Then(this.capa)
        .Else(this.qa_reject_decision)
    )

    qa_reject_decision = (
        flow.If(lambda activation: ChemicalAnalysisFlow.qa_is_rejected(activation))
        .Annotation(title="Chemist routing: rejection")
        .Then(this.reject)
        .Else(this.generate_coa)
    )

    retest = (
        flow.View(RetestTaskView.as_view())
        .Annotation(title="Request Chemical Retest")
        .Permission("ca.perform_chemical_retest")
        .Next(this.retest_result)
    )

    retest_result = (
        flow.View(RecordTestResultTaskView.as_view())
        .Annotation(title="Record Retest Result")
        .Permission("ca.record_chemical_result")
        .Next(this.check_retest_input, task_data=lambda activation: ChemicalAnalysisFlow.result_task_data(activation))
    )

    check_retest_input = (
        flow.If(lambda activation: ChemicalAnalysisFlow.result_requires_input(activation))
        .Then(this.retest_input)
        .Else(this.complete_retest)
    )

    retest_input = (
        flow.View(RecordDynamicAnalysisInputTaskView.as_view())
        .Annotation(title="Record Retest Method Inputs")
        .Permission("ca.record_chemical_result")
        .Next(this.complete_retest)
    )

    complete_retest = (
        flow.View(CompleteRetestTaskView.as_view())
        .Annotation(title="Complete Chemical Retest")
        .Permission("ca.perform_chemical_retest")
        .Next(this.quality_control_gate)
    )

    interlaboratory_review = (
        flow.View(InterlaboratoryReviewTaskView.as_view())
        .Annotation(title="Compare Independent Laboratory Results")
        .Permission("ca.review_chemical_analysis")
        .Next(this.generate_coa)
    )

    capa = (
        flow.View(CAPATaskView.as_view())
        .Annotation(title="Corrective and Preventive Action")
        .Permission("ca.review_chemical_analysis")
        .Next(this.qa_review)
    )

    approval = (
        flow.View(ApprovalTaskView.as_view())
        .Annotation(title="Archived Workflow Compatibility")
        .Permission("ca.approve_chemical_batch")
        .Next(this.generate_coa)
    )

    generate_coa = (
        flow.View(GenerateChemicalCoATaskView.as_view())
        .Annotation(title="Generate Chemical CoA")
        .Permission("ca.approve_chemical_coa")
        .Next(
            this.coa_generation_decision,
            task_data=lambda activation: {
                "nonconforming_certificate": ChemicalAnalysisFlow.task_value(
                    activation,
                    "nonconforming_certificate",
                    False,
                ),
            },
        )
    )

    coa_generation_decision = (
        flow.If(
            lambda activation: ChemicalAnalysisFlow.task_value(
                activation,
                "nonconforming_certificate",
                False,
            )
        )
        .Annotation(title="Route non-conforming certificate")
        .Then(this.refer_kebs)
        .Else(this.approve_coa)
    )

    approve_coa = (
        flow.View(ApproveChemicalCoATaskView.as_view())
        .Annotation(title="Approve Chemical CoA")
        .Permission("ca.approve_chemical_coa")
        .Next(this.batch_disposition)
    )

    refer_kebs = (
        flow.View(ReferChemicalToKEBSTaskView.as_view())
        .Annotation(title="Forward Failed Chemical and CoA to KEBS")
        .Permission("ca.reject_chemical_batch")
        .Next(this.completed)
    )

    batch_disposition = (
        flow.View(ConfirmWorkflowActionTaskView.as_view())
        .Annotation(title="Select Chemical Batch Disposition")
        .Next(this.disposition_decision, task_data=lambda activation: {"action": ChemicalAnalysisFlow.task_value(activation, "action")})
    )

    disposition_decision = (
        flow.If(lambda activation: ChemicalAnalysisFlow.disposition_is_utilize(activation))
        .Then(this.utilize)
        .Else(this.return_to_supplier)
    )

    utilize = (
        flow.View(UtilizeChemicalBatchTaskView.as_view())
        .Annotation(title="Utilize Chemical Batch")
        .Permission("ca.utilize_chemical_batch")
        .Next(this.completed)
    )

    return_to_supplier = (
        flow.View(ReturnChemicalBatchTaskView.as_view())
        .Annotation(title="Return Chemical Batch")
        .Permission("ca.return_chemical_batch")
        .Next(this.completed)
    )

    reject = (
        flow.View(RejectAnalysisTaskView.as_view())
        .Annotation(title="Reject Chemical Batch")
        .Permission("ca.reject_chemical_batch")
        .Next(this.return_rejected)
    )

    return_rejected = (
        flow.View(ReturnChemicalBatchTaskView.as_view())
        .Annotation(title="Return Rejected Chemical Batch")
        .Permission("ca.return_chemical_batch")
        .Next(this.completed)
    )

    completed = flow.End().Annotation(title="Chemical Analysis Completed")

    @staticmethod
    def task_value(activation, key, default=None):
        data = activation.task.data
        return data.get(key, default) if isinstance(data, dict) else default

    @staticmethod
    def has_assigned_analysts(activation):
        process = activation.process
        process.refresh_from_db()
        return process.has_assigned_analysts

    @staticmethod
    def has_analysis_request(activation):
        process = activation.process
        process.refresh_from_db()
        return bool(process.analysis_request_id)

    @staticmethod
    def has_method_quality_controls(activation):
        process = activation.process
        for parameter in process.required_specifications().select_related("method"):
            if not parameter.method_id:
                continue
            plan = ChemicalMethodQualityPlan.current_for(parameter.method, process.batch.treatment_plant)
            if plan and plan.controls.filter(is_active=True).exists():
                return True
        return False

    @staticmethod
    def checklist_starts_analysis(activation):
        return ChemicalAnalysisProcess.objects.filter(
            pk=activation.process.pk,
            status=ChemicalAnalysisProcess.WorkflowStatus.ANALYSIS,
        ).exists()

    @staticmethod
    def result_task_data(activation):
        data = activation.task.data
        if not isinstance(data, dict):
            return {}
        return {
            "result_id": data.get("result_id"),
            "parameter_id": data.get("parameter_id"),
            "result_status": data.get("result_status"),
            "input_recorded": data.get("input_recorded", False),
            "retest_parameter_id": data.get("retest_parameter_id"),
        }

    @staticmethod
    def qa_task_data(activation):
        data = activation.task.data
        if not isinstance(data, dict):
            return {}
        return {
            "decision": data.get("decision"),
            "remarks": data.get("remarks"),
            "reviewed_by_id": data.get("reviewed_by_id"),
            "reviewed_at": data.get("reviewed_at"),
        }

    @staticmethod
    def result_requires_input(activation):
        if ChemicalAnalysisFlow.task_value(activation, "input_recorded"):
            return False
        result_id = ChemicalAnalysisFlow.task_value(activation, "result_id")
        if result_id in (None, ""):
            return False
        try:
            result_id = int(result_id)
        except (TypeError, ValueError):
            return False
        result = (
            BatchTestResult.objects
            .select_related("parameter", "parameter__method")
            .filter(pk=result_id, batch=activation.process.batch)
            .first()
        )
        if result is None or result.parameter is None or result.parameter.method is None:
            return False
        fields = result.parameter.method.get_input_fields()
        if isinstance(fields, str):
            fields = [value.strip() for value in fields.split(",") if value.strip()]
        return bool(fields)

    @staticmethod
    def analysis_complete(activation):
        process = activation.process
        process.refresh_from_db()
        return bool(process.is_analysis_complete and not process.has_pending_tests)

    @staticmethod
    def _auto_complete_analysis(activation):
        process = activation.process
        process.refresh_from_db()
        batch = process.batch
        analyst = process.assigned_analyst
        if analyst is None and process.analysis_request_id:
            analyst = process.analysis_request.assigned_analysts.first()
        if analyst is None and batch is not None:
            last_res = batch.test_results.filter(analyst__isnull=False).order_by("-tested_at").first()
            if last_res is not None:
                analyst = last_res.analyst
        if analyst is None and getattr(activation, "user", None):
            analyst = activation.user

        if process.status == ChemicalAnalysisProcess.WorkflowStatus.ANALYSIS:
            process.mark_analysis_completed(performed_by=analyst)

        process.refresh_from_db()

        if batch is not None:
            from .services.workflow_enhancements import record_custody_event, record_workflow_event
            record_custody_event(
                batch,
                event_type="ANALYSIS_COMPLETED",
                actor=analyst,
                from_location="Chemical analysis laboratory",
                to_location=str(batch.location),
                remarks="Analysis automatically completed and forwarded to chemist review.",
            )
            record_workflow_event(
                process,
                event_type="analysis_completed",
                action="complete_analysis",
                actor=analyst,
                from_status=ChemicalAnalysisProcess.WorkflowStatus.ANALYSIS,
                to_status=process.status,
                details={"automated": True},
            )

    @staticmethod
    def qa_is_retest(activation):
        return str(ChemicalAnalysisFlow.task_value(activation, "decision", "")).upper() in {"RETEST", "REWORK", "REASSIGN"}

    @staticmethod
    def qa_is_interlaboratory_referral(activation):
        return str(ChemicalAnalysisFlow.task_value(activation, "decision", "")).upper() == "REFER_INTERLAB"

    @staticmethod
    def qa_is_capa(activation):
        return str(ChemicalAnalysisFlow.task_value(activation, "decision", "")).upper() == "CAPA"

    @staticmethod
    def qa_is_rejected(activation):
        if str(ChemicalAnalysisFlow.task_value(activation, "decision", "")).upper() != "REJECT":
            return False
        process = getattr(activation, "process", None)
        # A failed batch must still receive its report-only CoA and KEBS handoff.
        # Preserve the reject route for decisions on a batch with no failed result.
        return not (process and process.failed_tests.exists())

    @staticmethod
    def disposition_is_utilize(activation):
        return str(ChemicalAnalysisFlow.task_value(activation, "action", "")).upper() == "UTILIZE"
