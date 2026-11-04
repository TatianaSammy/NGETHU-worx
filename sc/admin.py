# sc/admin.py
from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.contrib import admin, messages
from django.contrib.admin import DateFieldListFilter
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.html import format_html
from django.urls import reverse
from django.utils.safestring import mark_safe

from core.models import Section
from core.plant_section_scope import object_in_scope, scoped_queryset, user_plant_section
from t.models import TreatmentPlant
from .models import (
    ShiftSectionRequirement,
    AuditLog,
    AnnualLeave,
    Shift,
    ShiftSupervisor,
    ShiftAssignment,
    MonthlyDutyRoster,
    ShiftHandover,
    Apparatus,
    ApparatusCondition,
)


# =============================================================================
# GLOBAL ADMIN SETTINGS
# =============================================================================

class ReadOnlyAdmin(admin.ModelAdmin):
    """Base readonly admin."""

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class SCScopedAdminMixin:
    scoped_filter_tokens = ("treatment_plant", "section", "shift", "roster", "handover", "apparatus")

    def get_queryset(self, request):
        return scoped_queryset(super().get_queryset(request), request.user)

    def get_autocomplete_fields(self, request):
        return super().get_autocomplete_fields(request) if request.user.is_superuser else ()

    def get_list_filter(self, request):
        configured = super().get_list_filter(request)
        if request.user.is_superuser:
            return configured
        filtered = []
        for item in configured:
            field_name = item[0] if isinstance(item, tuple) else item
            if not any(token in str(field_name) for token in self.scoped_filter_tokens):
                filtered.append(item)
        return tuple(filtered)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if formfield and hasattr(formfield, "queryset") and not request.user.is_superuser:
            formfield.queryset = scoped_queryset(formfield.queryset, request.user)
        return formfield

    def save_model(self, request, obj, form, change):
        if not object_in_scope(obj, request.user):
            raise ValidationError("This record must belong to your assigned treatment plant and section.")
        super().save_model(request, obj, form, change)


# =============================================================================
# INLINES
# =============================================================================

class ShiftAssignmentInline(admin.TabularInline):
    model = ShiftAssignment
    extra = 0
    autocomplete_fields = (
        "staff",
        "shift",
        "supervisor",
        "handover_to",
        "temp_supervisor",
    )
    fields = (
        "staff",
        "date",
        "shift",
        "attendance_status",
        "supervisor",
        "handover_to",
    )
    show_change_link = True

    def get_autocomplete_fields(self, request):
        return super().get_autocomplete_fields(request) if request.user.is_superuser else ()

    def get_queryset(self, request):
        return scoped_queryset(super().get_queryset(request), request.user)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if formfield and hasattr(formfield, "queryset") and not request.user.is_superuser:
            formfield.queryset = scoped_queryset(formfield.queryset, request.user)
        return formfield

class ShiftSectionRequirementInline(admin.TabularInline):
    model = ShiftSectionRequirement
    extra = 0

    def get_autocomplete_fields(self, request):
        return super().get_autocomplete_fields(request) if request.user.is_superuser else ()

    autocomplete_fields = (
        "section",
    )

    fields = (
        "section",
        "weekday_required",
        "weekend_required",
        "holiday_required",
        "minimum_required",
        "preferred_required",
        "maximum_allowed",
    )

    def get_queryset(self, request):
        return scoped_queryset(super().get_queryset(request), request.user)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if formfield and hasattr(formfield, "queryset") and not request.user.is_superuser:
            formfield.queryset = scoped_queryset(formfield.queryset, request.user)
        return formfield


class ApparatusConditionInline(admin.TabularInline):
    model = ApparatusCondition
    extra = 0
    autocomplete_fields = ("apparatus",)
    fields = (
        "apparatus",
        "status",
        "remarks",
    )
    show_change_link = True

    def get_autocomplete_fields(self, request):
        return super().get_autocomplete_fields(request) if request.user.is_superuser else ()

    def get_queryset(self, request):
        return scoped_queryset(super().get_queryset(request), request.user)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if formfield and hasattr(formfield, "queryset") and not request.user.is_superuser:
            formfield.queryset = scoped_queryset(formfield.queryset, request.user)
        return formfield


# =============================================================================
# AUDIT LOG
# =============================================================================

@admin.register(AuditLog)
class AuditLogAdmin(SCScopedAdminMixin, ReadOnlyAdmin):
    list_display = (
        "timestamp",
        "action_badge",
        "content_type",
        "object_link",
        "user",
    )

    list_filter = (
        "action",
        "content_type",
        ("timestamp", DateFieldListFilter),
    )

    search_fields = (
        "object_id",
        "object_repr",
        "user__username",
        "user__first_name",
        "user__last_name",
    )

    date_hierarchy = "timestamp"
    ordering = ("-timestamp",)

    fieldsets = (
        (
            "Audit Information",
            {
                "fields": (
                    "timestamp",
                    "user",
                    "action",
                )
            },
        ),
        (
            "Object",
            {
                "fields": (
                    "content_type",
                    "object_id",
                    "object_repr",
                    "changes",
                )
            },
        ),
    )

    readonly_fields = (
        "timestamp",
        "user",
        "action",
        "content_type",
        "object_id",
        "object_repr",
        "changes",
    )

    def action_badge(self, obj):
        colors = {
            "create": "#16a34a",
            "update": "#2563eb",
            "delete": "#dc2626",
        }
        color = colors.get(obj.action, "#64748b")

        return format_html(
            """
            <span style="
                background:{};
                color:white;
                padding:4px 10px;
                border-radius:20px;
                font-weight:600;
            ">
                {}
            </span>
            """,
            color,
            obj.action.upper(),
        )

    action_badge.short_description = "Action"

    def object_link(self, obj):
        return obj.object_repr

    object_link.short_description = "Object"


# =============================================================================
# SHIFT
# =============================================================================

@admin.register(Shift)
class ShiftAdmin(SCScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "treatment_plant",
        "start_time",
        "end_time",
        "assignment_count",
        "is_deleted",
    )

    list_filter = (
        "treatment_plant",
        "is_deleted",
    )

    search_fields = (
        "name",
        "treatment_plant__name",
    )

    autocomplete_fields = ("treatment_plant",)

    ordering = (
        "treatment_plant",
        "start_time",
    )

    readonly_fields = (
        "time_range",
        "assignment_count",
    )

    fieldsets = (
        (
            "General",
            {
                "fields": (
                    "name",
                    "treatment_plant",
                )
            },
        ),
        (
            "Schedule",
            {
                "fields": (
                    "start_time",
                    "end_time",
                    "handover_time",
                    "takeover_time",
                    "time_range",
                )
            },
        ),
        (
            "Statistics",
            {
                "fields": (
                    "assignment_count",
                )
            },
        ),
        (
            "Lifecycle",
            {
                "fields": (
                    "is_deleted",
                    "deleted_at",
                )
            },
        ),
    )
    actions = (
        "soft_delete_selected",
        "restore_selected",)
    inlines = (
        ShiftSectionRequirementInline,
    )

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser:
            plant_id, section_id = user_plant_section(request.user)
            if not plant_id or not section_id or obj.treatment_plant_id != plant_id:
                raise ValidationError("You can only manage shifts for your assigned treatment plant and section.")
            return admin.ModelAdmin.save_model(self, request, obj, form, change)
        super().save_model(request, obj, form, change)

    def has_change_permission(self, request, obj=None):
        if obj and not request.user.is_superuser:
            section_id = user_plant_section(request.user)[1]
            if obj.section_requirements.exclude(section_id=section_id).exists():
                return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            actions.pop("soft_delete_selected", None)
            actions.pop("restore_selected", None)
        return actions

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if not request.user.is_superuser:
            plant_id, section_id = user_plant_section(request.user)
            if not ShiftSectionRequirement.objects.filter(
                shift=form.instance,
                section_id=section_id,
                shift__treatment_plant_id=plant_id,
            ).exists():
                raise ValidationError("The shift must include your assigned section.")

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        assignment_filter = Q(assignments__is_deleted=False)
        if not request.user.is_superuser:
            plant_id, section_id = user_plant_section(request.user)
            assignment_filter &= Q(
                assignments__staff__assigned_treatment_plant_id=plant_id,
                assignments__staff__section_id=section_id,
            )
        return qs.annotate(assignments_total=Count("assignments", filter=assignment_filter, distinct=True))

    def assignment_count(self, obj):
        return getattr(obj, "assignments_total", 0)

    assignment_count.short_description = "Assignments"

    @admin.action(description="Soft delete selected shifts")
    def soft_delete_selected(self, request, queryset):
        if not request.user.is_superuser:
            raise PermissionDenied("Only a superuser can delete treatment plant shifts.")
        count = queryset.update(
            is_deleted=True,
            deleted_at=timezone.now(),
        )
        self.message_user(
            request,
            f"{count} shifts deleted.",
            messages.SUCCESS,
        )

    @admin.action(description="Restore selected shifts")
    def restore_selected(self, request, queryset):
        if not request.user.is_superuser:
            raise PermissionDenied("Only a superuser can restore treatment plant shifts.")
        count = queryset.update(
            is_deleted=False,
            deleted_at=None,
        )
        self.message_user(
            request,
            f"{count} shifts restored.",
            messages.SUCCESS,
        )


# =============================================================================
# SHIFT SUPERVISOR
# =============================================================================

class ShiftSupervisorAdminForm(forms.ModelForm):
    class Meta:
        model = ShiftSupervisor
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        User = get_user_model()
        plant_id = None
        if self.is_bound:
            plant_id = self.data.get("treatment_plant")
        elif self.instance and getattr(self.instance, "treatment_plant_id", None):
            plant_id = self.instance.treatment_plant_id

        if plant_id:
            self.fields["section"].queryset = self.fields["section"].queryset.filter(
                treatment_plant_id=plant_id,
                is_active=True,
            ).order_by("name")
        else:
            self.fields["section"].queryset = self.fields["section"].queryset.filter(is_active=True).order_by("name")

        section_id = None
        if self.is_bound:
            section_id = self.data.get("section")
        elif self.instance and getattr(self.instance, "section_id", None):
            section_id = self.instance.section_id

        user_qs = self.fields["user"].queryset.filter(is_active=True)
        if section_id:
            user_qs = user_qs.filter(section_id=section_id)
        if plant_id:
            user_qs = user_qs.filter(assigned_treatment_plant_id=plant_id)
        if section_id:
            user_qs = user_qs.filter(section_id=section_id)
        self.fields["user"].queryset = user_qs.order_by("username")


@admin.register(ShiftSupervisor)
class ShiftSupervisorAdmin(SCScopedAdminMixin, admin.ModelAdmin):
    form = ShiftSupervisorAdminForm

    list_display = (
        "user",
        "treatment_plant",
        "section",
        "is_active",
    )

    list_filter = (
        "is_active",
        "treatment_plant",
        "section",
    )

    search_fields = (
        "user__username",
        "user__first_name",
        "user__last_name",
        "treatment_plant__name",
        "section__name",
        "section__code",
    )

    list_editable = ("is_active",)
    list_select_related = (
        "user",
        "treatment_plant",
        "section",
    )

    class Media:
        js = ("core/js/plant_section_user_cascade.js",)


class AnnualLeaveAdminForm(forms.ModelForm):
    class Meta:
        model = AnnualLeave
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        User = get_user_model()
        plant_id = self.data.get("treatment_plant") if self.is_bound else self.instance.treatment_plant_id
        if plant_id:
            self.fields["staff"].queryset = self.fields["staff"].queryset.filter(
                assigned_treatment_plant_id=plant_id,
                is_active=True,
            ).distinct().order_by("last_name", "first_name", "username")


@admin.register(AnnualLeave)
class AnnualLeaveAdmin(SCScopedAdminMixin, admin.ModelAdmin):
    form = AnnualLeaveAdminForm
    list_display = (
        "staff",
        "treatment_plant",
        "start_date",
        "end_date",
        "duration_days",
        "status",
        "created_by",
        "updated_by",
    )
    list_filter = (
        "status",
        "treatment_plant",
        ("start_date", DateFieldListFilter),
        ("end_date", DateFieldListFilter),
    )
    search_fields = (
        "staff__username",
        "staff__first_name",
        "staff__last_name",
        "treatment_plant__name",
        "reason",
        "notes",
    )
    autocomplete_fields = (
        "staff",
        "treatment_plant",
    )
    readonly_fields = (
        "created_by",
        "updated_by",
        "created_at",
        "updated_at",
        "duration_days",
    )
    date_hierarchy = "start_date"
    list_select_related = (
        "staff",
        "treatment_plant",
        "created_by",
        "updated_by",
    )
    fieldsets = (
        ("Availability", {"fields": (("staff", "treatment_plant"), ("start_date", "end_date"), "status", "reason", "notes")}),
        ("Access record", {"fields": ("created_by", "updated_by", "created_at", "updated_at", "duration_days")}),
    )

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser or request.user.has_perm("sc.manage_staff_availability") or super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        return request.user.is_superuser or request.user.has_perm("sc.manage_staff_availability")

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser or request.user.has_perm("sc.manage_staff_availability")

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser or request.user.has_perm("sc.manage_staff_availability")

    def get_queryset(self, request):
        return super().get_queryset(request)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser:
            plant_id = getattr(request.user, "assigned_treatment_plant_id", None)
            if not plant_id:
                plant_id = getattr(getattr(request.user, "section", None), "treatment_plant_id", None)
            if obj.treatment_plant_id != plant_id:
                raise ValidationError("You cannot manage annual leave for another treatment plant.")
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user

        super().save_model(request, obj, form, change)


# =============================================================================
# MONTHLY DUTY ROSTER
# =============================================================================

@admin.register(MonthlyDutyRoster)
class MonthlyDutyRosterAdmin(SCScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "month",
        "year",
        "treatment_plant",
        "created_by",
        "assignment_total",
        "created_at",
    )

    list_filter = (
        "month",
        "year",
        "treatment_plant",
    )

    search_fields = (
        "treatment_plant__name",
        "notes",
    )

    autocomplete_fields = (
        "treatment_plant",
        "created_by",
    )

    readonly_fields = (
        "created_at",
        "assignment_total",
    )

    inlines = (ShiftAssignmentInline,)

    fieldsets = (
        (
            "Roster",
            {
                "fields": (
                    ("month", "year"),
                    "treatment_plant",
                    "created_by",
                    "notes",
                )
            },
        ),
        (
            "Statistics",
            {
                "fields": (
                    "assignment_total",
                    "created_at",
                )
            },
        ),
    )

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        assignment_filter = Q(assignments__is_deleted=False)
        if not request.user.is_superuser:
            plant_id, section_id = user_plant_section(request.user)
            assignment_filter &= Q(
                assignments__staff__assigned_treatment_plant_id=plant_id,
                assignments__staff__section_id=section_id,
            )
        return queryset.annotate(
            assignments_total=Count("assignments", filter=assignment_filter, distinct=True),
            _assigned_user_count=Count("assignments__staff_id", filter=assignment_filter, distinct=True),
        )

    def assignment_total(self, obj):
        return getattr(obj, "assignments_total", 0)

    def assigned_user_count(self, obj):
        return getattr(obj, "_assigned_user_count", 0)

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    assignment_total.short_description = "Assignments"


# =============================================================================
# SHIFT ASSIGNMENT
# =============================================================================

@admin.register(ShiftAssignment)
class ShiftAssignmentAdmin(SCScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "staff",
        "date",
        "shift",
        "day_type",
        "attendance_badge",
        "supervisor",
        "handover_to",
        "is_deleted",
    )

    list_filter = (
        "attendance_status",
        "day_type",
        "shift",
        "shift__treatment_plant",
        "is_deleted",
        ("date", DateFieldListFilter),
    )

    search_fields = (
        "staff__username",
        "staff__first_name",
        "staff__last_name",
        "remarks",
        "shift__name",
    )

    autocomplete_fields = (
        "staff",
        "shift",
        "duty_roster",
        "supervisor",
        "handover_to",
        "temp_supervisor",
        "reassigned_from",
    )

    date_hierarchy = "date"

    readonly_fields = (
        "week",
        "duration",
        "effective_supervisor",
    )

    list_select_related = (
        "staff",
        "shift",
        "supervisor",
        "handover_to",
    )

    actions = (
        "mark_present",
        "mark_absent",
        "mark_excused",
        "soft_delete_assignments",
    )

    fieldsets = (
        (
            "Assignment",
            {
                "fields": (
                    "staff",
                    "shift",
                    "date",
                    "duty_roster",
                    "day_type",
                )
            },
        ),
        (
            "Attendance",
            {
                "fields": (
                    "attendance_status",
                    "remarks",
                )
            },
        ),
        (
            "Supervision",
            {
                "fields": (
                    "supervisor",
                    "effective_supervisor",
                    "temp_supervisor",
                    "temp_supervisor_start",
                    "temp_supervisor_end",
                )
            },
        ),
        (
            "Handover",
            {
                "fields": (
                    "handover_to",
                    "reassigned_from",
                    "reassigned_at",
                )
            },
        ),
        (
            "Statistics",
            {
                "fields": (
                    "week",
                    "duration",
                )
            },
        ),
    )

    def attendance_badge(self, obj):
        colors = {
            "present": "#16a34a",
            "absent": "#dc2626",
            "late": "#f59e0b",
            "excused": "#2563eb",
        }

        return format_html(
            """
            <span style="
                background:{};
                color:white;
                padding:4px 12px;
                border-radius:20px;
                font-weight:600;
            ">
                {}
            </span>
            """,
            colors.get(obj.attendance_status, "#64748b"),
            obj.get_attendance_status_display(),
        )

    attendance_badge.short_description = "Attendance"

    @admin.action(description="Mark selected as PRESENT")
    def mark_present(self, request, queryset):
        queryset.update(
            attendance_status=ShiftAssignment.ATTENDANCE_PRESENT
        )

    @admin.action(description="Mark selected as ABSENT")
    def mark_absent(self, request, queryset):
        queryset.update(
            attendance_status=ShiftAssignment.ATTENDANCE_ABSENT
        )

    @admin.action(description="Mark selected as EXCUSED")
    def mark_excused(self, request, queryset):
        queryset.update(
            attendance_status=ShiftAssignment.ATTENDANCE_EXCUSED
        )

    @admin.action(description="Soft delete assignments")
    def soft_delete_assignments(self, request, queryset):
        queryset.update(
            is_deleted=True,
            deleted_at=timezone.now(),
        )


@admin.register(ShiftSectionRequirement)
class ShiftSectionRequirementAdmin(SCScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "shift",
        "section",
        "weekday_required",
        "weekend_required",
        "holiday_required",
        "minimum_required",
        "preferred_required",
        "maximum_allowed",
    )

    list_filter = (
        "shift",
        "section",
    )

    search_fields = (
        "shift__name",
        "section__name",
    )

    autocomplete_fields = (
        "shift",
        "section",
    )

    list_select_related = (
        "shift",
        "section",
    )

@admin.register(ShiftHandover)
class ShiftHandoverAdmin(SCScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "date",
        "shift",
        "outgoing_supervisor",
        "incoming_supervisor",
        "is_taken_over",
        "taken_over_at",
    )

    list_filter = (
        "is_taken_over",
        "shift",
        ("date", DateFieldListFilter),
    )

    search_fields = (
        "shift__name",
        "handover_notes",
        "pending_work",
        "outgoing_supervisor__username",
        "incoming_supervisor__username",
    )

    autocomplete_fields = (
        "shift",
        "outgoing_supervisor",
        "incoming_supervisor",
    )

    date_hierarchy = "date"

    inlines = (ApparatusConditionInline,)

    readonly_fields = (
        "handed_over_at",
    )
    list_select_related = (
        "shift",
        "incoming_supervisor",
        "outgoing_supervisor",
    )


# =============================================================================
# APPARATUS
# =============================================================================

@admin.register(Apparatus)
class ApparatusAdmin(SCScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "equipment",
        "equipment_status",
    )

    search_fields = (
        "name",
        "equipment__name",
    )

    autocomplete_fields = ("equipment",)

    def equipment_status(self, obj):
        if not obj.equipment or not obj.equipment.status:
            return "-"

        return obj.equipment.status.name

    equipment_status.short_description = "Equipment Status"


# =============================================================================
# APPARATUS CONDITION
# =============================================================================

@admin.register(ApparatusCondition)
class ApparatusConditionAdmin(SCScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "apparatus",
        "handover",
        "status_badge",
    )

    list_filter = (
        "status",
        "handover__shift",
    )

    search_fields = (
        "apparatus__name",
        "remarks",
    )

    autocomplete_fields = (
        "handover",
        "apparatus",
    )

    def status_badge(self, obj):
        colors = {
            "working": "#16a34a",
            "needs_repair": "#f59e0b",
            "out_of_service": "#dc2626",
        }

        return format_html(
            """
            <span style="
                background:{};
                color:white;
                padding:4px 12px;
                border-radius:20px;
                font-weight:600;
            ">
                {}
            </span>
            """,
            colors.get(obj.status, "#64748b"),
            obj.get_status_display(),
        )

    status_badge.short_description = "Status"
