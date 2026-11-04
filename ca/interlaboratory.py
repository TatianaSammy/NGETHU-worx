from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.generic import DetailView, ListView

from .forms import ChemicalInterlaboratorySubmissionForm
from .mixins import CaWanjalaMixin
from .models import (
    ChemicalLabComparisonResult,
    ChemicalLabReferral,
    ChemicalWorkflowAlert,
    ChemicalWorkflowEvent,
)


class InterlaboratoryReferralScopeMixin(CaWanjalaMixin):
    permission_required_view = "ca.record_chemical_result"

    def get_referral_queryset(self):
        queryset = ChemicalLabReferral.objects.select_related(
            "batch",
            "batch__chemical",
            "batch__treatment_plant",
            "process",
            "treatment_plant",
            "requested_by",
            "submitted_by",
        ).prefetch_related("results", "results__parameter")
        if self.is_ca_superuser():
            selected_plant = self.get_selected_plant()
            if selected_plant is not None:
                queryset = queryset.filter(treatment_plant_id=selected_plant.pk)
            return queryset
        return queryset.filter(treatment_plant_id=self.request.user.assigned_treatment_plant_id)


class InterlaboratoryReferralListView(InterlaboratoryReferralScopeMixin, ListView):
    template_name = "ca/interlaboratory/referral_list.html"
    context_object_name = "referrals"
    paginate_by = 30

    def get_queryset(self):
        return self.get_referral_queryset().order_by("status", "requested_at", "pk")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["pending_count"] = self.get_queryset().filter(status=ChemicalLabReferral.Status.PENDING).count()
        return context


class InterlaboratoryReferralDetailView(InterlaboratoryReferralScopeMixin, DetailView):
    template_name = "ca/interlaboratory/referral_detail.html"
    context_object_name = "referral"
    pk_url_kwarg = "pk"

    def get_queryset(self):
        return self.get_referral_queryset()

    def get_submission_form(self, *, data=None):
        return ChemicalInterlaboratorySubmissionForm(
            data=data,
            referral=self.object,
            user=self.request.user,
        )

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return self.render_to_response(self.get_context_data(form=self.get_submission_form()))

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if self.object.status != ChemicalLabReferral.Status.PENDING:
            raise PermissionDenied("Submitted laboratory referral results are read-only.")
        form = self.get_submission_form(data=request.POST)
        if form.is_valid():
            with transaction.atomic():
                self.object = ChemicalLabReferral.objects.select_for_update().get(pk=self.object.pk)
                if self.object.status != ChemicalLabReferral.Status.PENDING:
                    raise PermissionDenied("Another analyst has already submitted this referral.")
                for entry in form.result_entries():
                    ChemicalLabComparisonResult.objects.create(
                        batch=self.object.batch,
                        process=self.object.process,
                        treatment_plant=self.object.treatment_plant,
                        referral=self.object,
                        parameter=entry["parameter"],
                        source=ChemicalLabComparisonResult.Source.INTERLAB,
                        run_number=self.object.pk,
                        value=entry["value"],
                        textual_result=entry["textual_result"],
                        status=entry["status"] or "FAIL",
                        analyst=request.user,
                        inventory_equipment=entry["inventory_equipment"],
                        reference_standard_name=entry["reference_standard_name"],
                        reference_standard_lot=entry["reference_standard_lot"],
                        reagent_lot_numbers=entry["reagent_lot_numbers"],
                        expanded_uncertainty=entry["expanded_uncertainty"],
                        uncertainty_coverage_factor=entry["uncertainty_coverage_factor"],
                        remarks=(form.cleaned_data.get("submission_remarks") or "").strip(),
                    )
                self.object.mark_completed(
                    actor=request.user,
                    remarks=form.cleaned_data.get("submission_remarks"),
                )
                ChemicalWorkflowEvent.objects.create(
                    process=self.object.process,
                    batch=self.object.batch,
                    analysis_request=(
                        self.object.process.analysis_request
                        if self.object.process.analysis_request_id else None
                    ),
                    event_type="interlaboratory_results_submitted",
                    action="independent laboratory submitted comparison results",
                    actor=request.user,
                    from_status=self.object.process.status,
                    to_status=self.object.process.status,
                    details={
                        "referral_id": self.object.pk,
                        "laboratory_plant_id": self.object.treatment_plant_id,
                        "result_ids": list(self.object.results.values_list("pk", flat=True)),
                        "remarks": self.object.submission_remarks,
                    },
                )
                ChemicalWorkflowAlert.objects.filter(
                    dedupe_key=f"ca-interlab-referral:{self.object.pk}",
                    acknowledged_at__isnull=True,
                ).update(acknowledged_at=self.object.submitted_at, acknowledged_by=request.user)
                if not self.object.process.interlaboratory_referrals.exclude(
                    status=ChemicalLabReferral.Status.COMPLETED
                ).exists():
                    from .inventory_instruments import trigger_configured_alert

                    completed_referrals = list(
                        self.object.process.interlaboratory_referrals.select_related("treatment_plant")
                        .filter(status=ChemicalLabReferral.Status.COMPLETED)
                    )
                    trigger_configured_alert(
                        instance=self.object.process,
                        alert_type="ca_interlab_results_ready",
                        context_data={
                            "source": "chemical_interlaboratory_results_ready",
                            "plant_id": self.object.batch.treatment_plant_id,
                            "batch_id": self.object.batch_id,
                            "batch": self.object.batch.sample_id or self.object.batch.batch_number,
                            "chemical": self.object.batch.chemical.name,
                            "laboratory_count": len(completed_referrals) + 1,
                            "laboratories": ", ".join(item.treatment_plant.name for item in completed_referrals),
                            "result_count": self.object.results.count(),
                            "referral_ids": ", ".join(str(item.pk) for item in completed_referrals),
                        },
                        batch=self.object.batch,
                        request=self.object.process.analysis_request if self.object.process.analysis_request_id else None,
                        title="Interlaboratory results are ready for comparison",
                        message=(
                            f"All referred laboratories submitted results for {self.object.batch.chemical.name} "
                            f"batch {self.object.batch.sample_id or self.object.batch.batch_number}. "
                            "Review the readings, between-laboratory spread, and configured consensus rule."
                        ),
                        url=f"/ca/batches/{self.object.batch_id}/?stage=quality",
                        severity="high",
                        dedupe_key=f"ca-interlab-results-ready:{self.object.process_id}",
                        permissions=("ca.review_chemical_analysis",),
                    )
            messages.success(request, "Independent laboratory results were submitted and locked for chemist comparison.")
            return redirect(reverse("ca:interlaboratory-referral-list"))
        return self.render_to_response(self.get_context_data(form=form))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = kwargs.get("form")
        if form is None:
            form = self.get_submission_form()
        if self.object.status != ChemicalLabReferral.Status.PENDING:
            form = None
        context["form"] = form
        context["comparison_results"] = self.object.results.select_related(
            "parameter", "analyst", "instrument", "inventory_equipment"
        ).order_by("parameter__parameter_name")
        context["source_readings"] = ChemicalLabComparisonResult.objects.filter(
            process=self.object.process,
            parameter_id__in=self.object.parameter_ids,
        ).filter(
            treatment_plant_id=self.object.batch.treatment_plant_id,
            source__in=(
                ChemicalLabComparisonResult.Source.ORIGINAL,
                ChemicalLabComparisonResult.Source.REASSIGNMENT,
            ),
        ).select_related("parameter", "analyst").order_by("parameter__parameter_name", "recorded_at")
        return context
