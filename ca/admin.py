from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import SimpleListFilter
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import (
    BatchTestResult,
    BatchTestResultRevision,
    Chemical,
    ChemicalAnalysisInput,
    ChemicalAnalysisProcess,
    ChemicalAnalysisRequest,
    ChemicalBatch,
    ChemicalBatchHold,
    ChemicalCorrectiveAction,
    ChemicalCustodyEvent,
    ChemicalInventoryMovement,
    ChemicalCertApprover,
    ChemicalCoA,
    ChemicalCoAAuditEvent,
    ChemicalLabComparisonResult,
    ChemicalLabReferral,
    ChemicalMethodQualityPlan,
    ChemicalMethodQCControl,
    ChemicalQCCheckResult,
    ChemicalOOSInvestigation,
    ChemicalKEBSReferral,
    ChemicalOutboxEvent,
    ChemicalSuperuserPurgeAudit,
    ChemicalLocation,
    ChemicalSpecification,
    ChemicalSpecificationRemark,
    ChemicalWorkflowAlert,
    ChemicalWorkflowEvent,
    ChecklistForWaterTreatmentChemicalAnalysis,
    PackagingStatus,
    Supplier,
)


class CaSuperuserDeletionAdminMixin:
    """Keep CA records protected while giving superusers explicit delete access."""

    def has_delete_permission(self, request, obj=None):
        if (
            getattr(request.user, "is_active", False)
            and getattr(request.user, "is_superuser", False)
        ):
            return True
        # CA records and their Viewflow-linked audit history are destructive
        # operations reserved exclusively for active superusers.
        return False

    def get_deleted_objects(self, objs, request):
        if not getattr(request.user, "is_superuser", False):
            return super().get_deleted_objects(objs, request)

        objects = list(objs)
        deleted_objects = [
            "The selected CA records and their dependent workflow, result, movement, and audit records will be permanently deleted.",
            [str(obj) for obj in objects],
        ]
        model_count = {str(self.opts.verbose_name_plural): len(objects)}
        return deleted_objects, model_count, set(), []

    def delete_model(self, request, obj):
        if getattr(request.user, "is_superuser", False):
            from .services.superuser_deletion import delete_ca_objects

            delete_ca_objects([obj], actor=request.user)
            return
        return super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        if getattr(request.user, "is_superuser", False):
            from .services.superuser_deletion import delete_ca_objects

            delete_ca_objects(queryset, actor=request.user)
            return
        return super().delete_queryset(request, queryset)


class PlantScopedAdminMixin:
    plant_field = "treatment_plant"

    def get_queryset(self, request):
        qs = super().get_queryset(request)

        user = request.user

        if getattr(user, "is_superuser", False):
            return qs

        plant_id = getattr(user, "assigned_treatment_plant_id", None)

        if not plant_id:
            return qs.none()

        field = self.plant_field

        if field == "treatment_plant":
            return qs.filter(treatment_plant_id=plant_id)

        if field == "batch__treatment_plant":
            return qs.filter(batch__treatment_plant_id=plant_id)

        if field == "chemical_analysis_request__batch__treatment_plant":
            return qs.filter(
                chemical_analysis_request__batch__treatment_plant_id=plant_id
            )

        if field == "coa__batch__treatment_plant":
            return qs.filter(coa__batch__treatment_plant_id=plant_id)

        return qs


class PlantFilter(SimpleListFilter):
    title = "Treatment Plant"
    parameter_name = "treatment_plant"

    def lookups(self, request, model_admin):
        field = getattr(model_admin, "plant_filter_field", "treatment_plant")

        model = model_admin.model
        qs = model_admin.get_queryset(request)

        try:
            values = (
                qs.exclude(**{f"{field}__isnull": True})
                .values_list(
                    f"{field}__pk",
                    f"{field}__name",
                )
                .distinct()
                .order_by(f"{field}__name")
            )
        except Exception:
            return []

        return [(pk, name) for pk, name in values]

    def queryset(self, request, queryset):
        if not self.value():
            return queryset

        field = getattr(
            self.model_admin,
            "plant_filter_field",
            "treatment_plant",
        )

        return queryset.filter(
            **{f"{field}_id": self.value()}
        )


class CompletionFilter(SimpleListFilter):
    title = "Completion"
    parameter_name = "completion"

    def lookups(self, request, model_admin):
        return (
            ("complete", "Complete"),
            ("pending", "Pending"),
        )

    def queryset(self, request, queryset):
        if self.value() == "complete":
            return queryset.filter(
                Q(status=BatchTestResult.Status.PASS)
                | Q(status=BatchTestResult.Status.FAIL)
                | Q(status=BatchTestResult.Status.NA)
            )

        if self.value() == "pending":
            return queryset.filter(
                Q(status__isnull=True)
                | Q(status="")
            )

        return queryset


class AssignmentFilter(SimpleListFilter):
    title = "Analyst Assignment"
    parameter_name = "assignment"

    def lookups(self, request, model_admin):
        return (
            ("assigned", "Assigned"),
            ("unassigned", "Unassigned"),
            ("auto", "Automatically Assigned"),
            ("manual", "Manually Assigned"),
            ("overdue", "Needs Assignment"),
        )

    def queryset(self, request, queryset):
        if self.value() == "assigned":
            return queryset.filter(
                assigned_analysts__isnull=False
            ).distinct()

        if self.value() == "unassigned":
            return queryset.filter(
                assigned_analysts__isnull=True
            )

        if self.value() == "auto":
            return queryset.filter(
                auto_assigned_at__isnull=False
            )

        if self.value() == "manual":
            return queryset.filter(
                manual_assigned_at__isnull=False
            )

        if self.value() == "overdue":
            return queryset.filter(
                assigned_analysts__isnull=True,
                status__in=(
                    ChemicalAnalysisRequest.Status.PENDING,
                    ChemicalAnalysisRequest.Status.IN_PROGRESS,
                ),
            )

        return queryset


class RequestDeadlineFilter(SimpleListFilter):
    title = "Analysis Deadline"
    parameter_name = "deadline"

    def lookups(self, request, model_admin):
        return (
            ("overdue", "Overdue"),
            ("due_today", "Due Today"),
            ("future", "Future"),
        )

    def queryset(self, request, queryset):
        now = timezone.now()

        if self.value() == "overdue":
            return queryset.filter(
                analysis_deadline__lt=now,
                status=ChemicalAnalysisRequest.Status.IN_PROGRESS,
            )

        if self.value() == "due_today":
            today = timezone.localdate()
            return queryset.filter(
                analysis_deadline__date=today
            )

        if self.value() == "future":
            return queryset.filter(
                analysis_deadline__gt=now
            )

        return queryset


class BatchQualityFilter(SimpleListFilter):
    title = "Quality"
    parameter_name = "quality"

    def lookups(self, request, model_admin):
        return (
            ("failed", "Has Failed Results"),
            ("pending", "Has Pending Results"),
            ("passed", "All Results Passed"),
            ("na", "Has Not Applicable Results"),
        )

    def queryset(self, request, queryset):
        if self.value() == "failed":
            return queryset.filter(
                test_results__status=BatchTestResult.Status.FAIL
            ).distinct()

        if self.value() == "pending":
            return queryset.filter(
                Q(test_results__status__isnull=True)
                | Q(test_results__status="")
            ).distinct()

        if self.value() == "passed":
            return queryset.exclude(
                test_results__status=BatchTestResult.Status.FAIL
            ).exclude(
                Q(test_results__status__isnull=True)
                | Q(test_results__status="")
            ).distinct()

        if self.value() == "na":
            return queryset.filter(
                test_results__status=BatchTestResult.Status.NA
            ).distinct()

        return queryset


class ApprovalFilter(SimpleListFilter):
    title = "Approval"
    parameter_name = "approval"

    def lookups(self, request, model_admin):
        return (
            ("approved", "Approved"),
            ("unapproved", "Not Approved"),
            ("verified", "Integrity Verified"),
            ("invalid", "Integrity Invalid"),
        )

    def queryset(self, request, queryset):
        if self.value() == "approved":
            return queryset.filter(
                approved_by__isnull=False,
                approved_on__isnull=False,
            )

        if self.value() == "unapproved":
            return queryset.filter(
                Q(approved_by__isnull=True)
                | Q(approved_on__isnull=True)
            )

        if self.value() == "verified":
            return queryset.exclude(
                verified_hash=""
            ).filter(
                approved_by__isnull=False,
                approved_on__isnull=False,
            )

        if self.value() == "invalid":
            candidates = queryset.exclude(
                verified_hash=""
            ).filter(
                approved_by__isnull=False,
                approved_on__isnull=False,
            )

            invalid_ids = []

            for coa in candidates.iterator():
                if not coa.verify_integrity():
                    invalid_ids.append(coa.pk)

            return queryset.filter(pk__in=invalid_ids)

        return queryset


class StatusBadgeMixin:
    status_colors = {}

    def status_badge(self, obj):
        value = getattr(obj, "status", None) or "UNKNOWN"
        label = getattr(obj, "get_status_display", lambda: value)()

        return format_html(
            '<span style="display:inline-block;padding:3px 9px;'
            'border-radius:999px;font-weight:600;'
            'background:#f1f5f9;color:#0f172a;">{}</span>',
            label,
        )

    status_badge.short_description = "Status"
class ChemicalAdmin(CaSuperuserDeletionAdminMixin, admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "grade",
        "specification_count",
        "batch_count",
        "hydrometer_enabled",
    )
    search_fields = (
        "name",
        "code",
        "description",
        "grade",
    )
    list_filter = (
        "grade",
    )
    ordering = (
        "code",
        "name",
    )
    list_per_page = 50
    save_on_top = True
    list_select_related = False
    readonly_fields = (
        "hydrometer_enabled",
        "specification_count",
        "batch_count",
    )
    fieldsets = (
        (
            "Chemical Identity",
            {
                "fields": (
                    "name",
                    "code",
                    "grade",
                    "description",
                )
            },
        ),
        (
            "Hydrometer Calculation",
            {
                "fields": (
                    "hydrometer_formula",
                    "hydrometer_enabled",
                ),
                "description": (
                    "The hydrometer formula is evaluated using the "
                    "validated reading variable and approved math "
                    "functions only."
                ),
            },
        ),
        (
            "Operational Statistics",
            {
                "fields": (
                    "specification_count",
                    "batch_count",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(boolean=True, description="Hydrometer Formula")
    def hydrometer_enabled(self, obj):
        return bool(
            obj.hydrometer_formula
            and obj.hydrometer_formula.strip()
        )

    @admin.display(description="Specifications")
    def specification_count(self, obj):
        return getattr(obj, "_specification_count", 0)

    @admin.display(description="Batches")
    def batch_count(self, obj):
        return getattr(obj, "_batch_count", 0)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(
                _specification_count=Count(
                    "specifications",
                    distinct=True,
                ),
                _batch_count=Count(
                    "chemicalbatch",
                    distinct=True,
                ),
            )
        )

    def save_model(self, request, obj, form, change):
        obj.full_clean()
        super().save_model(request, obj, form, change)


class SupplierAdmin(CaSuperuserDeletionAdminMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "contract_number",
        "contact_info",
        "contract_document_status",
        "batch_count",
    )
    search_fields = (
        "name",
        "contract_number",
        "contact_info",
    )
    list_filter = ()
    ordering = ("name",)
    list_per_page = 50
    save_on_top = True
    readonly_fields = (
        "batch_count",
        "contract_document_status",
    )

    fieldsets = (
        (
            "Supplier",
            {
                "fields": (
                    "name",
                    "contract_number",
                    "contact_info",
                )
            },
        ),
        (
            "Contract Documentation",
            {
                "fields": (
                    "contract_document",
                    "contract_document_status",
                )
            },
        ),
        (
            "Operational Usage",
            {
                "fields": (
                    "batch_count",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description="Contract Document")
    def contract_document_status(self, obj):
        if not obj.contract_document:
            return format_html(
                '<span style="color:#b91c1c;font-weight:600;">Missing</span>'
            )

        return format_html(
            '<span style="color:#15803d;font-weight:600;">Available</span>'
        )

    @admin.display(description="Chemical Batches")
    def batch_count(self, obj):
        return getattr(obj, "_batch_count", 0)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(
                _batch_count=Count(
                    "chemical_batches",
                    distinct=True,
                )
            )
        )


class ChemicalLocationAdmin(CaSuperuserDeletionAdminMixin, PlantScopedAdminMixin, admin.ModelAdmin):
    plant_field = "treatment_plant"
    plant_filter_field = "treatment_plant"

    list_display = (
        "name",
        "treatment_plant",
        "batch_count",
    )
    search_fields = (
        "name",
        "treatment_plant__name",
    )
    list_filter = (
        PlantFilter,
    )
    autocomplete_fields = (
        "treatment_plant",
    )
    ordering = (
        "treatment_plant__name",
        "name",
    )
    list_per_page = 50

    @admin.display(description="Chemical Batches")
    def batch_count(self, obj):
        return getattr(obj, "_batch_count", 0)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(
                _batch_count=Count(
                    "chemical_batches",
                    distinct=True,
                )
            )
        )

class ChemicalSpecificationRemarkInline(admin.StackedInline):
    model = ChemicalSpecificationRemark
    extra = 0
    fields = ("on_pass", "on_fail")


class ChemicalSpecificationAdmin(CaSuperuserDeletionAdminMixin, PlantScopedAdminMixin, admin.ModelAdmin):
    plant_field = "treatment_plant"
    plant_filter_field = "treatment_plant"

    list_display = (
        "chemical",
        "parameter_name",
        "unit",
        "limit_range",
        "is_required",
        "is_active",
        "version",
        "method",
        "treatment_plant",
    )
    search_fields = (
        "chemical__name",
        "chemical__code",
        "parameter_name",
        "unit",
        "method__method",
    )
    list_filter = (
        PlantFilter,
        "is_required",
        "is_active",
        "version",
        "unit",
    )
    autocomplete_fields = (
        "chemical",
        "method",
        "treatment_plant",
    )
    ordering = (
        "chemical__name",
        "parameter_name",
        "-version",
    )
    readonly_fields = (
        "version",
        "created_at",
    )
    fieldsets = (
        (None, {
            "fields": (
                "treatment_plant",
                "chemical",
                "parameter_name",
                "unit",
                "method",
            )
        }),
        ("Limits & Requirements", {
            "fields": (
                ("min_limit", "max_limit"),
                ("is_required", "requires_instrument", "is_active"),
            )
        }),
        ("Audit & Versioning", {
            "fields": (
                "version",
                "created_at",
            )
        }),
    )
    inlines = [ChemicalSpecificationRemarkInline]
    list_per_page = 50
    save_on_top = True

    def has_delete_permission(self, request, obj=None):
        return bool(
            getattr(request.user, "is_active", False)
            and getattr(request.user, "is_superuser", False)
        )

    @admin.display(description="Specification Range")
    def limit_range(self, obj):
        minimum = obj.min_limit
        maximum = obj.max_limit

        if minimum is None and maximum is None:
            return "No limit"

        if minimum is None:
            return format_html("≤ {}", maximum)

        if maximum is None:
            return format_html("≥ {}", minimum)

        return format_html("{} – {}", minimum, maximum)


class ChemicalSpecificationRemarkAdmin(CaSuperuserDeletionAdminMixin, admin.ModelAdmin):
    list_display = (
        "specification",
        "on_pass_preview",
        "on_fail_preview",
    )
    search_fields = (
        "specification__parameter_name",
        "specification__chemical__name",
        "on_pass",
        "on_fail",
    )
    autocomplete_fields = (
        "specification",
    )
    list_per_page = 50

    @admin.display(description="Pass Remark")
    def on_pass_preview(self, obj):
        return (obj.on_pass or "")[:100]

    @admin.display(description="Fail Remark")
    def on_fail_preview(self, obj):
        return (obj.on_fail or "")[:100]


class PackagingStatusAdmin(CaSuperuserDeletionAdminMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "description",
        "coa_count",
    )
    search_fields = (
        "name",
        "description",
    )
    ordering = ("name",)
    list_per_page = 50

    @admin.display(description="CoAs")
    def coa_count(self, obj):
        return getattr(obj, "_coa_count", 0)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(
                _coa_count=Count(
                    "chemical_coas",
                    distinct=True,
                )
            )
        )


class ChemicalCertApproverAdmin(CaSuperuserDeletionAdminMixin, admin.ModelAdmin):
    list_display = (
        "user",
        "signature_status",
        "stamp_status",
    )
    search_fields = (
        "user__username",
        "user__first_name",
        "user__last_name",
        "user__email",
    )
    autocomplete_fields = (
        "user",
    )
    list_per_page = 50

    @admin.display(description="Signature")
    def signature_status(self, obj):
        return bool(obj.signature)

    @admin.display(description="Rubber Stamp")
    def stamp_status(self, obj):
        return bool(obj.rubber_stamp)


class ChemicalBatchAdmin(
    CaSuperuserDeletionAdminMixin,
    PlantScopedAdminMixin,
    StatusBadgeMixin,
    admin.ModelAdmin,
):
    plant_field = "treatment_plant"
    plant_filter_field = "treatment_plant"

    list_display = (
        "sample_identifier",
        "batch_number",
        "chemical",
        "supplier",
        "treatment_plant",
        "location",
        "received_date",
        "quantity_received",
        "package_count",
        "status_badge",
        "quality_indicator",
        "analysis_indicator",
        "coa_indicator",
    )
    search_fields = (
        "sample_id",
        "batch_number",
        "delivery_note_number",
        "chemical__name",
        "chemical__code",
        "supplier__name",
        "treatment_plant__name",
        "location__name",
    )
    list_filter = (
        PlantFilter,
        "status",
        BatchQualityFilter,
        "received_date",
    )
    date_hierarchy = "received_date"
    autocomplete_fields = (
        "chemical",
        "supplier",
        "treatment_plant",
        "location",
        "received_by",
    )
    readonly_fields = (
        "sample_id",
        "received_date",
        "created_at",
        "updated_at",
        "analysis_request_link",
        "analysis_process_link",
        "coa_link",
        "test_result_count",
        "quality_summary",
    )
    ordering = (
        "-received_date",
        "-created_at",
    )
    list_per_page = 50
    save_on_top = True
    actions = (
        # Explicit action lists replace Django's defaults. Keep the native
        # confirmation flow available; delete permission and the installed
        # superuser handler restrict execution to active superusers and route
        # it through the cascading CA hard-delete service.
        "delete_selected",
        "transition_to_rejected",
        "transition_to_approved",
        "transition_to_utilized",
        "transition_to_returned",
    )

    fieldsets = (
        (
            "Batch Identification",
            {
                "fields": (
                    "sample_id",
                    "batch_number",
                    "delivery_note_number",
                )
            },
        ),
        (
            "Chemical & Supplier",
            {
                "fields": (
                    "chemical",
                    "supplier",
                    "treatment_plant",
                    "location",
                )
            },
        ),
        (
            "Receipt",
            {
                "fields": (
                    "received_date",
                    "quantity_received",
                    "unit",
                    "package_count",
                    "received_by",
                    "status",
                    "remarks",
                )
            },
        ),
        (
            "Workflow",
            {
                "fields": (
                    "analysis_request_link",
                    "analysis_process_link",
                    "coa_link",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Quality Overview",
            {
                "fields": (
                    "test_result_count",
                    "quality_summary",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Audit",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description="Sample ID")
    def sample_identifier(self, obj):
        return format_html(
            '<strong>{}</strong>',
            obj.sample_id,
        )

    @admin.display(description="Quality")
    def quality_indicator(self, obj):
        failed = obj.test_results.filter(
            status=BatchTestResult.Status.FAIL
        ).exists()

        pending = obj.test_results.filter(
            Q(status__isnull=True)
            | Q(status="")
        ).exists()

        if failed:
            return format_html(
                '<span style="color:#b91c1c;font-weight:700;">FAILED</span>'
            )

        if pending:
            return format_html(
                '<span style="color:#b45309;font-weight:700;">PENDING</span>'
            )

        if obj.test_results.exists():
            return format_html(
                '<span style="color:#15803d;font-weight:700;">PASSED</span>'
            )

        return "No results"

    @admin.display(description="Analysis")
    def analysis_indicator(self, obj):
        request = getattr(obj, "analysis_request", None)

        if request is None:
            return "Not requested"

        return request.get_status_display()

    @admin.display(description="CoA")
    def coa_indicator(self, obj):
        coa = getattr(obj, "coa", None)

        if coa is None:
            return "Not generated"

        if coa.is_approved:
            return format_html(
                '<span style="color:#15803d;font-weight:700;">APPROVED</span>'
            )

        if coa.can_generate():
            return format_html(
                '<span style="color:#b45309;font-weight:700;">READY</span>'
            )

        return "Not ready"

    @admin.display(description="Test Results")
    def test_result_count(self, obj):
        return obj.test_results.count()

    @admin.display(description="Quality Summary")
    def quality_summary(self, obj):
        total = obj.test_results.count()
        passed = obj.test_results.filter(
            status=BatchTestResult.Status.PASS
        ).count()
        failed = obj.test_results.filter(
            status=BatchTestResult.Status.FAIL
        ).count()
        na = obj.test_results.filter(
            status=BatchTestResult.Status.NA
        ).count()
        pending = obj.test_results.filter(
            Q(status__isnull=True)
            | Q(status="")
        ).count()

        return (
            f"Total: {total} | "
            f"Pass: {passed} | "
            f"Fail: {failed} | "
            f"N/A: {na} | "
            f"Pending: {pending}"
        )

    @admin.display(description="Analysis Request")
    def analysis_request_link(self, obj):
        request = getattr(obj, "analysis_request", None)

        if request is None:
            return "—"

        url = reverse(
            "admin:ca_chemicalanalysisrequest_change",
            args=[request.pk],
        )

        return format_html(
            '<a href="{}">{}</a>',
            url,
            request.request_id,
        )

    @admin.display(description="Analysis Process")
    def analysis_process_link(self, obj):
        process = getattr(obj, "analysis_process", None)

        if process is None:
            return "—"

        url = reverse(
            "admin:ca_chemicalanalysisprocess_change",
            args=[process.pk],
        )

        return format_html(
            '<a href="{}">Process #{}</a>',
            url,
            process.pk,
        )

    @admin.display(description="Certificate of Analysis")
    def coa_link(self, obj):
        coa = getattr(obj, "coa", None)

        if coa is None:
            return "—"

        url = reverse(
            "admin:ca_chemicalcoa_change",
            args=[coa.pk],
        )

        return format_html(
            '<a href="{}">CoA #{}</a>',
            url,
            coa.pk,
        )

    def _transition_selected(self, request, queryset, target):
        success = 0
        failures = []

        for batch in queryset:
            try:
                batch.transition_to(
                    target,
                    performed_by=request.user,
                )
                success += 1
            except ValidationError as exc:
                failures.append(
                    f"{batch.sample_id}: {exc}"
                )

        if success:
            self.message_user(
                request,
                f"{success} batch(es) transitioned to "
                f"{target}.",
                messages.SUCCESS,
            )

        for failure in failures[:10]:
            self.message_user(
                request,
                failure,
                messages.ERROR,
            )

    @admin.action(description="Transition selected batches to Rejected")
    def transition_to_rejected(self, request, queryset):
        self._transition_selected(
            request,
            queryset,
            ChemicalBatch.Status.REJECTED,
        )

    @admin.action(description="Transition selected batches to Approved")
    def transition_to_approved(self, request, queryset):
        self._transition_selected(
            request,
            queryset,
            ChemicalBatch.Status.APPROVED,
        )

    @admin.action(description="Transition selected batches to Utilized")
    def transition_to_utilized(self, request, queryset):
        self._transition_selected(
            request,
            queryset,
            ChemicalBatch.Status.UTILIZED,
        )

    @admin.action(description="Transition selected batches to Returned")
    def transition_to_returned(self, request, queryset):
        self._transition_selected(
            request,
            queryset,
            ChemicalBatch.Status.RETURNED,
        )


class ChemicalAnalysisRequestAdmin(
    CaSuperuserDeletionAdminMixin,
    PlantScopedAdminMixin,
    StatusBadgeMixin,
    admin.ModelAdmin,
):
    plant_field = "batch__treatment_plant"
    plant_filter_field = "batch__treatment_plant"

    list_display = (
        "request_identifier",
        "batch_sample",
        "chemical",
        "treatment_plant",
        "status_badge",
        "assignment_status",
        "analyst_count",
        "deadline_status",
        "analysis_started_at",
        "completed_at",
    )
    search_fields = (
        "request_id",
        "batch__sample_id",
        "batch__batch_number",
        "batch__chemical__name",
        "batch__chemical__code",
        "batch__location__name",
        "requested_by__username",
        "requested_by__first_name",
        "requested_by__last_name",
        "received_by__username",
    )
    list_filter = (
        PlantFilter,
        "status",
        AssignmentFilter,
        RequestDeadlineFilter,
        "created_at",
    )
    date_hierarchy = "created_at"
    autocomplete_fields = (
        "batch",
        "requested_by",
        "received_by",
        "assigned_analysts",
        "completed_by",
    )
    filter_horizontal = (
        "assigned_analysts",
    )
    readonly_fields = (
        "request_id",
        "created_at",
        "updated_at",
        "assignment_due_at",
        "auto_assigned_at",
        "manual_assigned_at",
        "analysis_started_at",
        "analysis_deadline",
        "completed_at",
        "assigned_analyst_count_display",
        "assignment_status_display",
        "workflow_links",
        "checklist_link",
        "result_summary",
    )
    ordering = (
        "status",
        "assignment_due_at",
        "-created_at",
    )
    list_per_page = 50
    save_on_top = True
    actions = (
        "run_due_auto_assignment",
        "start_selected_requests",
        "complete_selected_requests",
    )

    fieldsets = (
        (
            "Request Identity",
            {
                "fields": (
                    "request_id",
                    "batch",
                    "delivery_date",
                )
            },
        ),
        (
            "Request Origin",
            {
                "fields": (
                    "requested_by",
                    "received_by",
                )
            },
        ),
        (
            "Assignment",
            {
                "fields": (
                    "assigned_analysts",
                    "assigned_analyst_count_display",
                    "assignment_status_display",
                    "assignment_due_at",
                    "manual_assigned_at",
                    "auto_assigned_at",
                )
            },
        ),
        (
            "Analysis Lifecycle",
            {
                "fields": (
                    "status",
                    "analysis_started_at",
                    "analysis_deadline",
                    "completed_at",
                    "completed_by",
                )
            },
        ),
        (
            "Workflow Navigation",
            {
                "fields": (
                    "workflow_links",
                    "checklist_link",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Results",
            {
                "fields": (
                    "result_summary",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Audit",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description="Request")
    def request_identifier(self, obj):
        return format_html(
            '<strong>{}</strong>',
            obj.request_id,
        )

    @admin.display(description="Sample")
    def batch_sample(self, obj):
        return obj.sample_id or "—"

    @admin.display(description="Chemical")
    def chemical(self, obj):
        return getattr(
            obj.chemical,
            "name",
            "—",
        )

    @admin.display(description="Treatment Plant")
    def treatment_plant(self, obj):
        return getattr(
            obj.treatment_plant,
            "name",
            "—",
        )

    @admin.display(description="Analysts")
    def analyst_count(self, obj):
        return obj.assigned_analysts.count()

    @admin.display(description="Assignment")
    def assignment_status(self, obj):
        if obj.has_assigned_analysts:
            if obj.auto_assigned_at:
                return format_html(
                    '<span style="color:#0369a1;font-weight:700;">AUTO</span>'
                )

            if obj.manual_assigned_at:
                return format_html(
                    '<span style="color:#15803d;font-weight:700;">MANUAL</span>'
                )

            return "ASSIGNED"

        if obj.assignment_overdue:
            return format_html(
                '<span style="color:#b91c1c;font-weight:700;">NEEDS ASSIGNMENT</span>'
            )

        if obj.assignment_pending:
            return format_html(
                '<span style="color:#b45309;font-weight:700;">PENDING</span>'
            )

        return "—"

    @admin.display(description="Deadline")
    def deadline_status(self, obj):
        if obj.is_overdue:
            return format_html(
                '<span style="color:#b91c1c;font-weight:700;">OVERDUE</span>'
            )

        remaining = obj.time_remaining

        if remaining is None:
            return "—"

        total_seconds = int(
            remaining.total_seconds()
        )

        hours, remainder = divmod(
            total_seconds,
            3600,
        )
        minutes = remainder // 60

        return format_html(
            '<span style="font-weight:600;">{}h {}m</span>',
            hours,
            minutes,
        )

    @admin.display(description="Assigned Analysts")
    def assigned_analyst_count_display(self, obj):
        return obj.assigned_analyst_count

    @admin.display(description="Assignment State")
    def assignment_status_display(self, obj):
        return self.assignment_status(obj)

    @admin.display(description="Workflow")
    def workflow_links(self, obj):
        links = []

        process = getattr(
            obj.batch,
            "analysis_process",
            None,
        )

        if process:
            url = reverse(
                "admin:ca_chemicalanalysisprocess_change",
                args=[process.pk],
            )
            links.append(
                format_html(
                    '<a href="{}">Analysis Process</a>',
                    url,
                )
            )

        coa = getattr(
            obj.batch,
            "coa",
            None,
        )

        if coa:
            url = reverse(
                "admin:ca_chemicalcoa_change",
                args=[coa.pk],
            )
            links.append(
                format_html(
                    '<a href="{}">Certificate of Analysis</a>',
                    url,
                )
            )

        return format_html(" | ".join(str(link) for link in links)) or "—"

    @admin.display(description="Checklist")
    def checklist_link(self, obj):
        checklist = (
            obj.checklists
            .order_by("-created_at")
            .first()
        )

        if not checklist:
            return "No checklist"

        url = reverse(
            "admin:ca_checklistforwater treatmentchemicalanalysis_change".replace(
                " ",
                "",
            ),
            args=[checklist.pk],
        )

        return format_html(
            '<a href="{}">Checklist #{}</a>',
            url,
            checklist.pk,
        )

    @admin.display(description="Results")
    def result_summary(self, obj):
        if not obj.batch_id:
            return "No batch"

        results = obj.batch.test_results.all()

        total = results.count()
        passed = results.filter(
            status=BatchTestResult.Status.PASS
        ).count()
        failed = results.filter(
            status=BatchTestResult.Status.FAIL
        ).count()
        na = results.filter(
            status=BatchTestResult.Status.NA
        ).count()
        pending = results.filter(
            Q(status__isnull=True)
            | Q(status="")
        ).count()

        return (
            f"Total {total} | Pass {passed} | "
            f"Fail {failed} | N/A {na} | Pending {pending}"
        )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "batch",
                "batch__chemical",
                "batch__supplier",
                "batch__treatment_plant",
                "batch__location",
                "requested_by",
                "received_by",
                "completed_by",
            )
            .prefetch_related(
                "assigned_analysts",
            )
        )

    @admin.action(description="Assign analysts to unassigned requests now")
    def run_due_auto_assignment(self, request, queryset):
        assigned = 0
        skipped = 0

        for analysis_request in queryset:
            try:
                users = analysis_request.auto_assign_analysts_if_due()

                if users:
                    assigned += 1
                else:
                    skipped += 1
            except ValidationError as exc:
                self.message_user(
                    request,
                    f"{analysis_request.request_id}: {exc}",
                    messages.ERROR,
                )

        self.message_user(
            request,
            f"Automatic assignment completed. "
            f"Assigned: {assigned}; skipped: {skipped}.",
            messages.SUCCESS,
        )

    @admin.action(description="Start selected analysis requests")
    def start_selected_requests(self, request, queryset):
        started = 0

        for analysis_request in queryset:
            try:
                analysis_request.start_analysis(
                    performed_by=request.user
                )
                started += 1
            except ValidationError as exc:
                self.message_user(
                    request,
                    f"{analysis_request.request_id}: {exc}",
                    messages.ERROR,
                )

        self.message_user(
            request,
            f"{started} request(s) started.",
            messages.SUCCESS,
        )

    @admin.action(description="Complete selected analysis requests")
    def complete_selected_requests(self, request, queryset):
        completed = 0

        for analysis_request in queryset:
            try:
                analysis_request.mark_completed(
                    performed_by=request.user
                )
                completed += 1
            except ValidationError as exc:
                self.message_user(
                    request,
                    f"{analysis_request.request_id}: {exc}",
                    messages.ERROR,
                )

        self.message_user(
            request,
            f"{completed} request(s) completed.",
            messages.SUCCESS,
        )


class ChecklistAdmin(
    CaSuperuserDeletionAdminMixin,
    PlantScopedAdminMixin,
    admin.ModelAdmin,
):
    plant_field = "chemical_analysis_request__batch__treatment_plant"
    plant_filter_field = (
        "chemical_analysis_request__batch__treatment_plant"
    )

    list_display = (
        "checklist_identifier",
        "chemical",
        "batch_number",
        "treatment_plant",
        "grade",
        "completed_badge",
        "expiry_status_display",
        "sampled_by",
        "completed_by",
        "created_at",
    )
    search_fields = (
        "chemical__name",
        "chemical__code",
        "chemical_analysis_request__request_id",
        "chemical_analysis_request__batch__sample_id",
        "chemical_analysis_request__batch__batch_number",
        "chemical_analysis_request__batch__supplier__name",
    )
    list_filter = (
        PlantFilter,
        "completed",
        "grade",
        "created_at",
    )
    date_hierarchy = "created_at"
    autocomplete_fields = (
        "chemical_analysis_request",
        "chemical",
        "sampled_by",
        "requested_by",
        "completed_by",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "completed_at",
        "completion_status",
        "expiry_status_display",
        "request_context",
    )
    ordering = (
        "-created_at",
    )
    list_per_page = 50
    save_on_top = True
    actions = (
        "complete_checklists",
    )

    fieldsets = (
        (
            "Request",
            {
                "fields": (
                    "chemical_analysis_request",
                    "chemical",
                    "request_context",
                )
            },
        ),
        (
            "Product Identification",
            {
                "fields": (
                    "label",
                    "manufacturer",
                    "grade",
                    "net_weight",
                    "country_of_origin",
                )
            },
        ),
        (
            "Dates",
            {
                "fields": (
                    "manufacture_date",
                    "expiry_date",
                    "expiry_status_display",
                )
            },
        ),
        (
            "Container label evidence",
            {
                "fields": ("chemical_label_photo",),
            },
        ),
        (
            "Storage & Handling",
            {
                "fields": (
                    "storage_conditions",
                    "handling_instructions",
                    "minimum_mass_requirement_met",
                )
            },
        ),
        (
            "Safety",
            {
                "fields": (
                    "hazardous",
                    "flammable",
                    "corrosive",
                    "toxic",
                )
            },
        ),
        (
            "Workflow",
            {
                "fields": (
                    "sampled_by",
                    "requested_by",
                    "completed",
                    "completed_by",
                    "completed_at",
                    "completion_status",
                )
            },
        ),
        (
            "Audit",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description="Checklist")
    def checklist_identifier(self, obj):
        return format_html(
            "<strong>Checklist #{}</strong>",
            obj.pk,
        )

    @admin.display(description="Chemical")
    def chemical(self, obj):
        return getattr(
            obj.chemical,
            "name",
            "—",
        )

    @admin.display(description="Batch")
    def batch_number(self, obj):
        return getattr(
            obj.batch,
            "batch_number",
            "—",
        )

    @admin.display(description="Treatment Plant")
    def treatment_plant(self, obj):
        return getattr(
            obj.treatment_plant,
            "name",
            "—",
        )

    @admin.display(description="Completed")
    def completed_badge(self, obj):
        if obj.completed:
            return format_html(
                '<span style="color:#15803d;font-weight:700;">COMPLETE</span>'
            )

        return format_html(
            '<span style="color:#b45309;font-weight:700;">PENDING</span>'
        )

    @admin.display(description="Expiry")
    def expiry_status_display(self, obj):
        return obj.expiry_status

    @admin.display(description="Request Context")
    def request_context(self, obj):
        request = obj.chemical_analysis_request

        return format_html(
            "Request: {}<br>Sample: {}<br>Batch: {}",
            request.request_id,
            request.sample_id or "—",
            request.batch_number or "—",
        )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "chemical",
                "chemical_analysis_request",
                "chemical_analysis_request__batch",
                "chemical_analysis_request__batch__treatment_plant",
                "chemical_analysis_request__batch__chemical",
                "sampled_by",
                "requested_by",
                "completed_by",
            )
        )

    @admin.action(description="Complete selected checklists")
    def complete_checklists(self, request, queryset):
        completed = 0

        for checklist in queryset:
            try:
                checklist.mark_completed(
                    performed_by=request.user
                )
                completed += 1
            except ValidationError as exc:
                self.message_user(
                    request,
                    f"Checklist #{checklist.pk}: {exc}",
                    messages.ERROR,
                )

        self.message_user(
            request,
            f"{completed} checklist(s) completed.",
            messages.SUCCESS,
        )


class BatchTestResultAdmin(
    CaSuperuserDeletionAdminMixin,
    PlantScopedAdminMixin,
    admin.ModelAdmin,
):
    plant_field = "batch__treatment_plant"
    plant_filter_field = "batch__treatment_plant"

    list_display = (
        "batch_sample",
        "chemical",
        "parameter",
        "value_display",
        "textual_result",
        "inventory_equipment",
        "status_badge",
        "analyst",
        "tested_at",
        "input_status",
    )
    search_fields = (
        "batch__sample_id",
        "batch__batch_number",
        "batch__chemical__name",
        "batch__chemical__code",
        "parameter__parameter_name",
        "analyst__username",
        "analyst__first_name",
        "analyst__last_name",
        "textual_result",
        "auto_remark",
    )
    list_filter = (
        PlantFilter,
        "status",
        CompletionFilter,
        "tested_at",
    )
    date_hierarchy = "tested_at"
    autocomplete_fields = (
        "batch",
        "parameter",
        "analyst",
    )
    readonly_fields = (
        "tested_at",
        "auto_remark",
        "input_link",
        "batch_context",
        "inventory_equipment",
        "instrument",
        "instrument_calibration",
        "instrument_snapshot",
        "specification_snapshot",
        "method_snapshot",
        "quality_flags",
        "revision_number",
    )
    ordering = (
        "batch__sample_id",
        "parameter__parameter_name",
        "id",
    )
    list_per_page = 100
    save_on_top = True

    def has_delete_permission(self, request, obj=None):
        return bool(
            getattr(request.user, "is_active", False)
            and getattr(request.user, "is_superuser", False)
        )

    fieldsets = (
        (
            "Test",
            {
                "fields": (
                    "batch",
                    "parameter",
                    "batch_context",
                )
            },
        ),
        (
            "Result",
            {
                "fields": (
                    "value",
                    "textual_result",
                    "status",
                    "revision_reason",
                    "auto_remark",
                )
            },
        ),
        (
            "Execution",
            {
                "fields": (
                    "analyst",
                    "tested_at",
                    "inventory_equipment",
                    "instrument",
                    "instrument_calibration",
                    "instrument_snapshot",
                    "input_link",
                )
            },
        ),
    )

    @admin.display(description="Sample")
    def batch_sample(self, obj):
        return obj.batch.sample_id

    @admin.display(description="Chemical")
    def chemical(self, obj):
        return obj.batch.chemical.name

    @admin.display(description="Value")
    def value_display(self, obj):
        if obj.value is not None:
            return obj.value

        return obj.textual_result or "—"

    @admin.display(description="Status")
    def status_badge(self, obj):
        status = obj.status

        if status == BatchTestResult.Status.PASS:
            return format_html(
                '<span style="color:#15803d;font-weight:700;">PASS</span>'
            )

        if status == BatchTestResult.Status.FAIL:
            return format_html(
                '<span style="color:#b91c1c;font-weight:700;">FAIL</span>'
            )

        if status == BatchTestResult.Status.NA:
            return format_html(
                '<span style="color:#475569;font-weight:700;">N/A</span>'
            )

        return format_html(
            '<span style="color:#b45309;font-weight:700;">PENDING</span>'
        )

    @admin.display(description="Analysis Inputs")
    def input_status(self, obj):
        try:
            inputs = obj.inputs
        except ChemicalAnalysisInput.DoesNotExist:
            return format_html(
                '<span style="color:#b45309;font-weight:700;">MISSING</span>'
            )

        if inputs.calculated_value is not None:
            return format_html(
                '<span style="color:#15803d;font-weight:700;">CALCULATED</span>'
            )

        return "Recorded"

    @admin.display(description="Input Record")
    def input_link(self, obj):
        try:
            input_record = obj.inputs
        except ChemicalAnalysisInput.DoesNotExist:
            return "No input record"

        url = reverse(
            "admin:ca_chemicalanalysisinput_change",
            args=[input_record.pk],
        )

        return format_html(
            '<a href="{}">Open analysis inputs</a>',
            url,
        )

    @admin.display(description="Batch Context")
    def batch_context(self, obj):
        return format_html(
            "Sample: {}<br>Batch: {}<br>Chemical: {}<br>Plant: {}",
            obj.batch.sample_id,
            obj.batch.batch_number,
            obj.batch.chemical.name,
            getattr(
                obj.batch.treatment_plant,
                "name",
                "—",
            ),
        )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "batch",
                "batch__chemical",
                "batch__treatment_plant",
                "parameter",
                "parameter__chemical",
                "parameter__method",
                "analyst",
            )
            .prefetch_related(
                "parameter__remarks",
            )
        )


class ChemicalAnalysisInputAdmin(
    CaSuperuserDeletionAdminMixin,
    PlantScopedAdminMixin,
    admin.ModelAdmin,
):
    plant_field = "test_result__batch__treatment_plant"
    plant_filter_field = "test_result__batch__treatment_plant"

    list_display = (
        "test_result_identifier",
        "chemical",
        "parameter",
        "method",
        "calculated_value",
        "input_completeness",
    )
    search_fields = (
        "test_result__batch__sample_id",
        "test_result__batch__batch_number",
        "test_result__batch__chemical__name",
        "test_result__parameter__parameter_name",
        "test_result__analyst__username",
    )
    list_filter = (
        PlantFilter,
    )
    autocomplete_fields = (
        "test_result",
    )
    readonly_fields = (
        "test_parameter",
        "analysis_method",
        "required_input_fields",
        "formula",
        "rounding_precision",
        "calculated_value",
    )
    ordering = (
        "-pk",
    )
    list_per_page = 100

    def get_readonly_fields(self, request, obj=None):
        readonly = tuple(super().get_readonly_fields(request, obj))
        if obj is not None:
            return readonly + ("inputs", "method_snapshot")
        return readonly

    fieldsets = (
        (
            "Test Result",
            {
                "fields": (
                    "test_result",
                    "test_parameter",
                    "analysis_method",
                )
            },
        ),
        (
            "Required Inputs",
            {
                "fields": (
                    "required_input_fields",
                    "inputs",
                )
            },
        ),
        (
            "Calculation",
            {
                "fields": (
                    "formula",
                    "rounding_precision",
                    "calculated_value",
                )
            },
        ),
    )

    @admin.display(description="Test Result")
    def test_result_identifier(self, obj):
        return f"Result #{obj.test_result_id}"

    @admin.display(description="Chemical")
    def chemical(self, obj):
        parameter = obj.test_parameter

        if parameter is None:
            return "—"

        return getattr(
            parameter.chemical,
            "name",
            "—",
        )

    @admin.display(description="Parameter")
    def parameter(self, obj):
        parameter = obj.test_parameter

        if parameter is None:
            return "—"

        return parameter.parameter_name

    @admin.display(description="Method")
    def method(self, obj):
        method = obj.analysis_method

        if method is None:
            return "—"

        return str(method)

    @admin.display(description="Input Completeness")
    def input_completeness(self, obj):
        try:
            required = obj.required_input_fields
            supplied = obj.inputs or {}
        except ValidationError:
            return format_html(
                '<span style="color:#b91c1c;font-weight:700;">INVALID</span>'
            )

        missing = [
            field
            for field in required
            if field not in supplied
        ]

        if missing:
            return format_html(
                '<span style="color:#b45309;font-weight:700;">{} MISSING</span>',
                len(missing),
            )

        return format_html(
            '<span style="color:#15803d;font-weight:700;">COMPLETE</span>'
        )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "test_result",
                "test_result__batch",
                "test_result__batch__chemical",
                "test_result__batch__treatment_plant",
                "test_result__parameter",
                "test_result__parameter__chemical",
                "test_result__parameter__method",
            )
        )

    def save_model(self, request, obj, form, change):
        obj.recalculate()
        super().save_model(request, obj, form, change)


class ChemicalAnalysisProcessAdmin(
    CaSuperuserDeletionAdminMixin,
    PlantScopedAdminMixin,
    StatusBadgeMixin,
    admin.ModelAdmin,
):
    plant_field = "batch__treatment_plant"
    plant_filter_field = "batch__treatment_plant"

    list_display = (
        "process_identifier",
        "sample_id_display",
        "chemical",
        "treatment_plant",
        "status_badge",
        "assigned_analyst_display",
        "progress_display",
        "analysis_state",
        "qa_state",
        "approval_state",
        "started_at",
    )
    search_fields = (
        "batch__sample_id",
        "batch__batch_number",
        "batch__chemical__name",
        "batch__chemical__code",
        "initiated_by__username",
        "assigned_analyst__username",
        "qa_officer__username",
        "approver__username",
    )
    list_filter = (
        PlantFilter,
        "status",
        "started_at",
        "qa_reviewed_at",
        "approved_at",
    )
    date_hierarchy = "started_at"
    autocomplete_fields = (
        "batch",
        "analysis_request",
        "initiated_by",
        "assigned_analyst",
        "qa_officer",
        "approver",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "started_at",
        "checklist_completed_at",
        "analysis_started_at",
        "analysis_completed_at",
        "qa_reviewed_at",
        "approved_at",
        "rejected_at",
        "completed_at",
        "progress_display",
        "test_statistics",
        "workflow_summary",
    )
    ordering = (
        "status",
        "-started_at",
        "-created_at",
    )
    list_per_page = 50
    save_on_top = True

    def has_delete_permission(self, request, obj=None):
        return bool(
            getattr(request.user, "is_active", False)
            and getattr(request.user, "is_superuser", False)
        )

    fieldsets = (
        (
            "Process",
            {
                "fields": (
                    "batch",
                    "analysis_request",
                    "status",
                    "rework_count",
                )
            },
        ),
        (
            "Personnel",
            {
                "fields": (
                    "initiated_by",
                    "assigned_analyst",
                    "qa_officer",
                    "approver",
                )
            },
        ),
        (
            "Workflow Timeline",
            {
                "fields": (
                    "started_at",
                    "checklist_completed_at",
                    "analysis_started_at",
                    "analysis_completed_at",
                    "qa_reviewed_at",
                    "approved_at",
                    "rejected_at",
                    "completed_at",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Quality Control",
            {
                "fields": (
                    "progress_display",
                    "test_statistics",
                    "workflow_summary",
                ),
            },
        ),
        (
            "Audit",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description="Process")
    def process_identifier(self, obj):
        return format_html(
            "<strong>Process #{}</strong>",
            obj.pk,
        )

    @admin.display(description="Sample")
    def sample_id_display(self, obj):
        return obj.sample_id or "—"

    @admin.display(description="Chemical")
    def chemical(self, obj):
        return getattr(
            obj.chemical,
            "name",
            "—",
        )

    @admin.display(description="Treatment Plant")
    def treatment_plant(self, obj):
        return getattr(
            obj.treatment_plant,
            "name",
            "—",
        )

    @admin.display(description="Analyst")
    def assigned_analyst_display(self, obj):
        analysts = obj.assigned_analysts

        if not analysts:
            return "Unassigned"

        return ", ".join(
            str(user)
            for user in analysts[:5]
        )

    @admin.display(description="Progress")
    def progress_display(self, obj):
        try:
            return f"{obj.analysis_progress}%"
        except Exception:
            return "—"

    @admin.display(description="Analysis")
    def analysis_state(self, obj):
        if obj.is_analysis_complete:
            return format_html(
                '<span style="color:#15803d;font-weight:700;">COMPLETE</span>'
            )

        if obj.analysis_ready:
            return format_html(
                '<span style="color:#b45309;font-weight:700;">READY</span>'
            )

        return "Pending"

    @admin.display(description="QA")
    def qa_state(self, obj):
        if obj.status == obj.WorkflowStatus.QA:
            return format_html(
                '<span style="color:#b45309;font-weight:700;">REVIEW REQUIRED</span>'
            )

        if obj.qa_reviewed_at:
            return "Reviewed"

        return "—"

    @admin.display(description="Approval")
    def approval_state(self, obj):
        if obj.status == obj.WorkflowStatus.APPROVED:
            return format_html(
                '<span style="color:#15803d;font-weight:700;">APPROVED</span>'
            )

        if obj.status == obj.WorkflowStatus.REJECTED:
            return format_html(
                '<span style="color:#b91c1c;font-weight:700;">REJECTED</span>'
            )

        return "Pending"

    @admin.display(description="Test Statistics")
    def test_statistics(self, obj):
        total = obj.test_results.count()
        passed = obj.test_results.filter(
            status=BatchTestResult.Status.PASS
        ).count()
        failed = obj.test_results.filter(
            status=BatchTestResult.Status.FAIL
        ).count()
        na = obj.test_results.filter(
            status=BatchTestResult.Status.NA
        ).count()
        pending = obj.test_results.filter(
            Q(status__isnull=True)
            | Q(status="")
        ).count()

        return (
            f"Total: {total} | Pass: {passed} | "
            f"Fail: {failed} | N/A: {na} | Pending: {pending}"
        )

    @admin.display(description="Workflow Summary")
    def workflow_summary(self, obj):
        stages = [
            ("Checklist", obj.checklist_completed_at),
            ("Analysis", obj.analysis_started_at),
            ("Analysis Complete", obj.analysis_completed_at),
            ("Chemist Review", obj.qa_reviewed_at),
            ("Approval", obj.approved_at),
            ("Completion", obj.completed_at),
        ]

        completed = sum(
            bool(timestamp)
            for _, timestamp in stages
        )

        return format_html(
            "{} of {} workflow milestones recorded.",
            completed,
            len(stages),
        )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
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
        )


class ChemicalCoAAdmin(
    CaSuperuserDeletionAdminMixin,
    PlantScopedAdminMixin,
    admin.ModelAdmin,
):
    plant_field = "batch__treatment_plant"
    plant_filter_field = "batch__treatment_plant"

    list_display = (
        "coa_identifier",
        "sample_id_display",
        "chemical_name",
        "supplier_name",
        "treatment_plant",
        "analysis_date",
        "approval_badge",
        "generation_status_display",
        "integrity_badge",
        "date_of_report",
    )
    search_fields = (
        "chemical_name",
        "supplier_name",
        "delivery_note_number",
        "batch__sample_id",
        "batch__batch_number",
        "batch__chemical__name",
        "batch__supplier__name",
        "approved_by__username",
        "approved_by__first_name",
        "approved_by__last_name",
        "verified_hash",
    )
    list_filter = (
        PlantFilter,
        ApprovalFilter,
        "analysis_date",
        "date_of_report",
    )
    date_hierarchy = "date_of_report"
    autocomplete_fields = (
        "batch",
        "packaging_status",
        "approved_by",
    )
    readonly_fields = (
        "chemical_name",
        "supplier_name",
        "quantity",
        "delivered_to",
        "delivery_date",
        "delivery_note_number",
        "sampling_date",
        "analysis_date",
        "date_of_report",
        "updated_at",
        "verified_hash",
        "sample_id",
        "treatment_plant",
        "generation_status_display",
        "integrity_badge",
        "result_statistics",
        "missing_assets_display",
        "analysis_request_link",
    )
    ordering = (
        "-date_of_report",
    )
    list_per_page = 50
    save_on_top = True
    actions = (
        "prepare_selected_coas",
        "approve_selected_coas",
        "verify_selected_coas",
        "revoke_selected_approvals",
    )

    fieldsets = (
        (
            "Certificate Identity",
            {
                "fields": (
                    "batch",
                    "sample_id",
                    "title",
                )
            },
        ),
        (
            "Certificate Header",
            {
                "fields": (
                    "coa_header",
                    "coa_footer",
                )
            },
        ),
        (
            "Delivery",
            {
                "fields": (
                    "chemical_name",
                    "supplier_name",
                    "quantity",
                    "delivered_to",
                    "delivery_date",
                    "delivery_note_number",
                    "packaging_status",
                )
            },
        ),
        (
            "Analysis",
            {
                "fields": (
                    "sampling_date",
                    "analysis_date",
                    "analysis_request_link",
                    "result_statistics",
                    "results_summary",
                    "remarks",
                )
            },
        ),
        (
            "Approval & Integrity",
            {
                "fields": (
                    "approved_by",
                    "approved_on",
                    "verified_hash",
                    "integrity_badge",
                )
            },
        ),
        (
            "Generated Assets",
            {
                "fields": (
                    "footer_image",
                    "watermark",
                    "qr_code",
                    "pdf_file",
                    "missing_assets_display",
                    "generation_status_display",
                )
            },
        ),
        (
            "Audit",
            {
                "fields": (
                    "date_of_report",
                    "updated_at",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description="CoA")
    def coa_identifier(self, obj):
        return format_html(
            "<strong>CoA #{}</strong>",
            obj.pk,
        )

    @admin.display(description="Sample")
    def sample_id_display(self, obj):
        return obj.sample_id or "—"

    @admin.display(description="Treatment Plant")
    def treatment_plant(self, obj):
        return getattr(
            obj.treatment_plant,
            "name",
            "—",
        )

    @admin.display(description="Approval")
    def approval_badge(self, obj):
        if obj.is_approved:
            return format_html(
                '<span style="color:#15803d;font-weight:700;">APPROVED</span>'
            )

        return format_html(
            '<span style="color:#b45309;font-weight:700;">UNAPPROVED</span>'
        )

    @admin.display(description="Generation")
    def generation_status_display(self, obj):
        return obj.generation_status

    @admin.display(description="Integrity")
    def integrity_badge(self, obj):
        status = obj.integrity_status

        if status == "VALID":
            return format_html(
                '<span style="color:#15803d;font-weight:700;">VALID</span>'
            )

        if status == "INVALID":
            return format_html(
                '<span style="color:#b91c1c;font-weight:700;">INVALID</span>'
            )

        return format_html(
            '<span style="color:#b45309;font-weight:700;">{}</span>',
            status,
        )

    @admin.display(description="Result Statistics")
    def result_statistics(self, obj):
        statistics = obj.get_result_statistics()

        return (
            f"Total: {statistics['total']} | "
            f"Pass: {statistics['pass']} | "
            f"Fail: {statistics['fail']} | "
            f"N/A: {statistics['na']} | "
            f"Pending: {statistics['pending']} | "
            f"Pass Rate: {statistics['pass_rate']}%"
        )

    @admin.display(description="Missing Assets")
    def missing_assets_display(self, obj):
        missing = obj.missing_assets

        if not missing:
            return format_html(
                '<span style="color:#15803d;font-weight:700;">COMPLETE</span>'
            )

        return ", ".join(missing)

    @admin.display(description="Analysis Request")
    def analysis_request_link(self, obj):
        request = obj.analysis_request

        if request is None:
            return "—"

        url = reverse(
            "admin:ca_chemicalanalysisrequest_change",
            args=[request.pk],
        )

        return format_html(
            '<a href="{}">{}</a>',
            url,
            request.request_id,
        )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "batch",
                "batch__chemical",
                "batch__supplier",
                "batch__treatment_plant",
                "packaging_status",
                "approved_by",
            )
        )

    @admin.action(description="Prepare selected CoAs for generation")
    def prepare_selected_coas(self, request, queryset):
        prepared = 0

        for coa in queryset:
            try:
                coa.prepare_for_generation()
                coa.save(
                    populate=False,
                    regenerate_watermark=False,
                    regenerate_hash=False,
                )
                prepared += 1
            except ValidationError as exc:
                self.message_user(
                    request,
                    f"CoA #{coa.pk}: {exc}",
                    messages.ERROR,
                )

        self.message_user(
            request,
            f"{prepared} CoA(s) prepared.",
            messages.SUCCESS,
        )

    @admin.action(description="Approve selected CoAs")
    def approve_selected_coas(self, request, queryset):
        approved = 0

        for coa in queryset:
            try:
                coa.approve(
                    approved_by=request.user
                )
                approved += 1
            except ValidationError as exc:
                self.message_user(
                    request,
                    f"CoA #{coa.pk}: {exc}",
                    messages.ERROR,
                )

        self.message_user(
            request,
            f"{approved} CoA(s) approved.",
            messages.SUCCESS,
        )

    @admin.action(description="Verify selected CoA integrity")
    def verify_selected_coas(self, request, queryset):
        valid = 0
        invalid = 0

        for coa in queryset:
            if coa.verify_integrity():
                valid += 1
            else:
                invalid += 1

        self.message_user(
            request,
            f"Integrity verification completed. "
            f"Valid: {valid}; Invalid/Unverified: {invalid}.",
            messages.SUCCESS,
        )

    @admin.action(description="Revoke selected CoA approvals")
    def revoke_selected_approvals(self, request, queryset):
        revoked = 0

        for coa in queryset:
            try:
                coa.revoke_approval(
                    revoked_by=request.user,
                    reason="Approval revoked by Django administrator action.",
                )
                revoked += 1
            except ValidationError as exc:
                self.message_user(
                    request,
                    f"CoA #{coa.pk}: {exc}",
                    messages.ERROR,
                )

        self.message_user(
            request,
            f"{revoked} CoA approval(s) revoked.",
            messages.SUCCESS,
        )

class ChemicalAdminForm(forms.ModelForm):
    class Meta:
        model = Chemical
        fields = "__all__"

    def clean_hydrometer_formula(self):
        value = self.cleaned_data.get(
            "hydrometer_formula"
        )

        if value:
            value = value.strip()

        return value


class ChemicalAnalysisRequestAdminForm(forms.ModelForm):
    class Meta:
        model = ChemicalAnalysisRequest
        fields = "__all__"

    def clean_assigned_analysts(self):
        users = self.cleaned_data.get(
            "assigned_analysts"
        )

        batch = self.instance.batch

        invalid = [
            user
            for user in (users or [])
            if not user.has_perm("ca.start_chemical_analysis")
            or not user.has_perm("ca.record_chemical_result")
            or (
                batch
                and batch.treatment_plant_id
                and not user.is_superuser
                and user.assigned_treatment_plant_id
                != batch.treatment_plant_id
            )
        ]

        if batch and users:
            eligible_ids = set(
                self.instance.eligible_analysts(users).values_list("pk", flat=True)
            )
            invalid.extend(user for user in users if user.pk not in eligible_ids and user not in invalid)

        if invalid:
            names = ", ".join(str(user) for user in invalid[:10])

            raise ValidationError(
                f"The following users lack chemical-analysis permission "
                f"or plant access: {names}"
            )

        return users


ChemicalAdmin.form = ChemicalAdminForm
ChemicalAnalysisRequestAdmin.form = ChemicalAnalysisRequestAdminForm


class ChemicalCoAAdminForm(forms.ModelForm):
    class Meta:
        model = ChemicalCoA
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()

        if self.instance.pk and self.instance.is_approved:
            protected = {
                "title",
                "coa_header",
                "coa_footer",
                "footer_image",
                "watermark",
                "qr_code",
                "pdf_file",
                "chemical_name",
                "supplier_name",
                "quantity",
                "delivered_to",
                "delivery_date",
                "delivery_note_number",
                "packaging_status",
                "sampling_date",
                "analysis_date",
                "results_summary",
                "remarks",
            }

            changed = {
                field
                for field in protected
                if field in self.changed_data
            }

            if changed:
                raise ValidationError(
                    "Approved CoA content is immutable. "
                    "Revoke approval before modifying certificate content."
                )

        return cleaned_data


ChemicalCoAAdmin.form = ChemicalCoAAdminForm


class PlantScopedAppendOnlyAdmin(CaSuperuserDeletionAdminMixin, PlantScopedAdminMixin, admin.ModelAdmin):
    """Read-only admin views for audit, custody and stock ledgers."""
    plant_field = "batch__treatment_plant"
    plant_filter_field = "batch__treatment_plant"
    list_per_page = 100

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return bool(
            getattr(request.user, "is_active", False)
            and getattr(request.user, "is_superuser", False)
        )


@admin.register(ChemicalBatchHold)
class ChemicalBatchHoldAdmin(PlantScopedAppendOnlyAdmin):
    list_display = ("batch", "issue", "status", "opened_at", "opened_by", "resolved_at", "resolved_by")
    list_filter = (PlantFilter, "status", "issue", "opened_at")
    search_fields = ("batch__sample_id", "batch__batch_number", "details", "resolution")


@admin.register(ChemicalWorkflowEvent)
class ChemicalWorkflowEventAdmin(PlantScopedAppendOnlyAdmin):
    list_display = ("occurred_at", "batch", "event_type", "action", "from_status", "to_status", "actor")
    list_filter = (PlantFilter, "event_type", "occurred_at")
    search_fields = ("batch__sample_id", "batch__batch_number", "action", "details")


@admin.register(ChemicalCustodyEvent)
class ChemicalCustodyEventAdmin(PlantScopedAppendOnlyAdmin):
    list_display = ("occurred_at", "batch", "event_type", "from_location", "to_location", "seal_number", "actor")
    list_filter = (PlantFilter, "event_type", "occurred_at")
    search_fields = ("batch__sample_id", "batch__batch_number", "seal_number", "remarks")


@admin.register(ChemicalInventoryMovement)
class ChemicalInventoryMovementAdmin(PlantScopedAppendOnlyAdmin):
    list_display = ("created_at", "batch", "movement_type", "quantity_delta", "quantity_after", "unit", "actor")
    list_filter = (PlantFilter, "movement_type", "created_at")
    search_fields = ("batch__sample_id", "batch__batch_number", "idempotency_key", "notes")


@admin.register(BatchTestResultRevision)
class BatchTestResultRevisionAdmin(PlantScopedAppendOnlyAdmin):
    list_display = ("created_at", "batch", "parameter", "revision_number", "actor", "revision_reason")
    list_filter = (PlantFilter, "created_at")
    search_fields = ("batch__sample_id", "batch__batch_number", "parameter__parameter_name", "revision_reason")


@admin.register(ChemicalWorkflowAlert)
class ChemicalWorkflowAlertAdmin(PlantScopedAppendOnlyAdmin):
    plant_field = "batch__treatment_plant"
    plant_filter_field = "batch__treatment_plant"
    list_display = ("created_at", "recipient", "batch", "alert_type", "severity", "acknowledged_at")
    list_filter = (PlantFilter, "alert_type", "severity", "acknowledged_at", "created_at")
    search_fields = ("recipient__username", "title", "message", "batch__sample_id")


@admin.register(ChemicalLabReferral)
class ChemicalLabReferralAdmin(PlantScopedAppendOnlyAdmin):
    plant_field = "treatment_plant"
    plant_filter_field = "treatment_plant"
    list_display = ("requested_at", "batch", "treatment_plant", "status", "requested_by", "submitted_by", "submitted_at")
    list_filter = (PlantFilter, "status", "requested_at", "submitted_at")
    search_fields = ("batch__sample_id", "batch__batch_number", "batch__chemical__name", "treatment_plant__name")


@admin.register(ChemicalLabComparisonResult)
class ChemicalLabComparisonResultAdmin(PlantScopedAppendOnlyAdmin):
    plant_field = "treatment_plant"
    plant_filter_field = "treatment_plant"
    list_display = ("recorded_at", "batch", "treatment_plant", "parameter", "source", "run_number", "value", "status", "inventory_equipment", "analyst")
    list_filter = (PlantFilter, "source", "status", "recorded_at")
    search_fields = ("batch__sample_id", "batch__batch_number", "parameter__parameter_name", "remarks")


@admin.register(ChemicalCorrectiveAction)
class ChemicalCorrectiveActionAdmin(PlantScopedAppendOnlyAdmin):
    list_display = ("submitted_at", "batch", "status", "submitted_by", "reviewed_by", "reviewed_at")
    list_filter = (PlantFilter, "status", "submitted_at", "reviewed_at")
    search_fields = (
        "batch__sample_id", "batch__batch_number", "qa_observation",
        "corrective_action", "preventive_action", "review_notes",
    )


@admin.register(ChemicalCoAAuditEvent)
class ChemicalCoAAuditEventAdmin(PlantScopedAppendOnlyAdmin):
    plant_field = "coa__batch__treatment_plant"
    plant_filter_field = "coa__batch__treatment_plant"
    list_display = ("occurred_at", "coa", "action", "actor", "content_hash")
    list_filter = ("action", "occurred_at")
    search_fields = ("coa__batch__sample_id", "content_hash", "details")


class SuperuserOnlyAuditAdmin(CaSuperuserDeletionAdminMixin, admin.ModelAdmin):
    def has_view_permission(self, request, obj=None):
        return bool(request.user.is_active and request.user.is_superuser)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)


class ChemicalMethodQCControlInline(admin.TabularInline):
    model = ChemicalMethodQCControl
    extra = 1
    fields = (
        "code", "label", "check_type", "evaluation", "required", "target_value",
        "spike_amount", "acceptance_min", "acceptance_max", "warning_sigma",
        "corrective_instruction", "is_active",
    )


@admin.register(ChemicalMethodQualityPlan)
class ChemicalMethodQualityPlanAdmin(CaSuperuserDeletionAdminMixin, admin.ModelAdmin):
    list_display = (
        "method", "version", "treatment_plant", "consensus_rule", "interlab_tolerance_absolute",
        "interlab_tolerance_percent", "require_measurement_uncertainty", "is_active", "created_at",
    )
    list_filter = ("is_active", "consensus_rule", "treatment_plant", "require_measurement_uncertainty")
    search_fields = ("method__method", "method__kebs_reference", "treatment_plant__name", "change_reason")
    readonly_fields = ("created_at",)
    inlines = (ChemicalMethodQCControlInline,)
    fieldsets = (
        ("Method and scope", {"fields": ("method", "treatment_plant", "version", "is_active", "change_reason", "created_by", "created_at")}),
        ("Interlaboratory decision", {"fields": ("consensus_rule", "interlab_tolerance_absolute", "interlab_tolerance_percent", "uncertainty_coverage_factor")}),
        ("Measurement traceability requirements", {"fields": ("require_measurement_uncertainty", "require_reference_standard_lot", "require_reagent_lot_numbers")}),
    )

    def get_queryset(self, request):
        queryset = super().get_queryset(request).select_related("method", "treatment_plant")
        if request.user.is_superuser:
            return queryset
        plant_id = getattr(request.user, "assigned_treatment_plant_id", None)
        return queryset.filter(Q(treatment_plant_id=plant_id) | Q(treatment_plant__isnull=True)) if plant_id else queryset.none()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ChemicalQCCheckResult)
class ChemicalQCCheckResultAdmin(PlantScopedAppendOnlyAdmin):
    list_display = ("recorded_at", "batch", "control", "parameter", "run_number", "observed_value", "evaluated_value", "status", "inventory_equipment", "analyst")
    list_filter = (PlantFilter, "status", "control__check_type", "control__plan__method", "recorded_at")
    search_fields = ("batch__sample_id", "control__label", "parameter__parameter_name", "remarks")


@admin.register(ChemicalOOSInvestigation)
class ChemicalOOSInvestigationAdmin(PlantScopedAppendOnlyAdmin):
    list_display = ("opened_at", "batch", "parameter", "status", "opened_by", "closed_by", "closed_at")
    list_filter = (PlantFilter, "status", "opened_at", "closed_at")
    search_fields = ("batch__sample_id", "parameter__parameter_name", "assignable_cause", "corrective_action", "final_disposition")


@admin.register(ChemicalKEBSReferral)
class ChemicalKEBSReferralAdmin(PlantScopedAppendOnlyAdmin):
    list_display = ("dispatched_at", "batch", "destination", "kebs_reference", "dispatch_method", "shipment_reference", "dispatched_by")
    list_filter = (PlantFilter, "status", "dispatch_method", "dispatched_at")
    search_fields = ("batch__sample_id", "batch__batch_number", "kebs_reference", "shipment_reference", "remarks")


@admin.register(ChemicalOutboxEvent)
class ChemicalOutboxEventAdmin(SuperuserOnlyAuditAdmin):
    list_display = ("created_at", "event_type", "aggregate_type", "aggregate_id", "attempt_count", "available_at", "delivered_at")
    list_filter = ("event_type", "delivered_at", "created_at")
    search_fields = ("idempotency_key", "aggregate_id", "last_error")


@admin.register(ChemicalSuperuserPurgeAudit)
class ChemicalSuperuserPurgeAuditAdmin(SuperuserOnlyAuditAdmin):
    list_display = ("occurred_at", "actor", "root_model", "deleted_counts")
    list_filter = ("root_model", "occurred_at")
    search_fields = ("root_model", "root_ids", "root_labels")




@admin.register(Chemical)
class RegisteredChemicalAdmin(ChemicalAdmin):
    pass


@admin.register(Supplier)
class RegisteredSupplierAdmin(SupplierAdmin):
    pass


@admin.register(ChemicalLocation)
class RegisteredChemicalLocationAdmin(ChemicalLocationAdmin):
    pass


@admin.register(ChemicalSpecification)
class RegisteredChemicalSpecificationAdmin(
    ChemicalSpecificationAdmin
):
    pass


@admin.register(ChemicalSpecificationRemark)
class RegisteredChemicalSpecificationRemarkAdmin(
    ChemicalSpecificationRemarkAdmin
):
    pass


@admin.register(PackagingStatus)
class RegisteredPackagingStatusAdmin(PackagingStatusAdmin):
    pass


@admin.register(ChemicalCertApprover)
class RegisteredChemicalCertApproverAdmin(
    ChemicalCertApproverAdmin
):
    pass


@admin.register(ChemicalBatch)
class RegisteredChemicalBatchAdmin(ChemicalBatchAdmin):
    pass


@admin.register(ChemicalAnalysisRequest)
class RegisteredChemicalAnalysisRequestAdmin(
    ChemicalAnalysisRequestAdmin
):
    pass


@admin.register(ChecklistForWaterTreatmentChemicalAnalysis)
class RegisteredChecklistAdmin(ChecklistAdmin):
    pass


@admin.register(BatchTestResult)
class RegisteredBatchTestResultAdmin(BatchTestResultAdmin):
    pass


@admin.register(ChemicalAnalysisInput)
class RegisteredChemicalAnalysisInputAdmin(
    ChemicalAnalysisInputAdmin
):
    pass


@admin.register(ChemicalAnalysisProcess)
class RegisteredChemicalAnalysisProcessAdmin(
    ChemicalAnalysisProcessAdmin
):
    pass


@admin.register(ChemicalCoA)
class RegisteredChemicalCoAAdmin(ChemicalCoAAdmin):
    pass
