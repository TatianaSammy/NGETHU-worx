from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Count, Q
from django.utils import timezone

from core.models import Section
from core.plant_section_scope import object_in_scope, scoped_queryset, user_plant_section
from sc.models import Shift, ShiftAssignment, ShiftSectionRequirement
from t.models import TreatmentPlant

from .models import (
    DailyTaskAssignment,
    DutyWeek,
    MonthlyDutySchedule,
    StandardWeeklyTask,
    WeeklyDutyTask,
)


User = get_user_model()


class PlantScopedAdminMixin:
    scoped_filter_tokens = ("treatment_plant", "section", "shift", "schedule", "week", "task")

    def get_queryset(self, request):
        return scoped_queryset(super().get_queryset(request), request.user)

    def get_list_filter(self, request):
        configured = super().get_list_filter(request)
        if request.user.is_superuser:
            return configured
        return tuple(
            item for item in configured
            if not any(
                token in str(item[0] if isinstance(item, tuple) else item)
                for token in self.scoped_filter_tokens
            )
        )

    def get_autocomplete_fields(self, request):
        return super().get_autocomplete_fields(request) if request.user.is_superuser else ()

    def save_model(self, request, obj, form, change):
        if not object_in_scope(obj, request.user):
            raise ValidationError("This record must belong to your assigned treatment plant and section.")

        super().save_model(
            request,
            obj,
            form,
            change,
        )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if formfield and hasattr(formfield, "queryset") and not request.user.is_superuser:
            formfield.queryset = scoped_queryset(formfield.queryset, request.user)
        return formfield


class SectionScopedFormMixin:
    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.request = request

        if not request or request.user.is_superuser:
            return

        section_id = getattr(
            request.user,
            "section_id",
            None,
        )

        if not section_id:
            return

        for field_name in (
            "section",
            "section_id",
        ):
            if field_name not in self.fields:
                continue

            field = self.fields[field_name]

            if hasattr(field, "queryset"):
                field.queryset = field.queryset.filter(
                    pk=section_id,
                )


class StandardWeeklyTaskAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "is_active",
        "weekly_task_count",
        "created_at",
        "updated_at",
    )
    list_filter = (
        "is_active",
        "created_at",
        "updated_at",
    )
    search_fields = (
        "code",
        "name",
        "description",
    )
    ordering = (
        "code",
        "name",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "weekly_task_count",
    )
    fieldsets = (
        (
            "Task",
            {
                "fields": (
                    "code",
                    "name",
                    "description",
                    "is_active",
                )
            },
        ),
        (
            "Usage",
            {
                "fields": (
                    "weekly_task_count",
                )
            },
        ),
        (
            "Audit",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    @admin.display(
        description="Weekly tasks",
        ordering="_weekly_task_count",
    )
    def weekly_task_count(self, obj):
        return getattr(obj, "_weekly_task_count", 0)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if request.user.is_superuser:
            return queryset.annotate(_weekly_task_count=Count("weekly_duty_tasks", distinct=True))
        plant_id, section_id = user_plant_section(request.user)
        return queryset.annotate(
            _weekly_task_count=Count(
                "weekly_duty_tasks",
                filter=Q(
                    weekly_duty_tasks__treatment_plant_id=plant_id,
                    weekly_duty_tasks__section_id=section_id,
                ),
                distinct=True,
            )
        )

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(MonthlyDutySchedule)
class MonthlyDutyScheduleAdmin(PlantScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "period",
        "treatment_plant",
        "section",
        "week_count",
        "assigned_user_count",
        "created_by",
        "created_at",
    )
    list_filter = (
        "year",
        "month",
        "treatment_plant",
        "section",
        "created_at",
    )
    search_fields = (
        "treatment_plant__name",
        "section__name",
        "created_by__username",
        "created_by__first_name",
        "created_by__last_name",
        "notes",
    )
    autocomplete_fields = (
        "treatment_plant",
        "section",
        "created_by",
    )
    readonly_fields = (
        "month_start_display",
        "month_end_display",
        "week_count",
        "assigned_user_count",
        "created_at",
        "updated_at",
    )
    ordering = (
        "-year",
        "-month",
        "treatment_plant",
        "section",
    )
    date_hierarchy = "created_at"
    fieldsets = (
        (
            "Schedule",
            {
                "fields": (
                    "month",
                    "year",
                    "treatment_plant",
                    "section",
                    "notes",
                )
            },
        ),
        (
            "Schedule Period",
            {
                "fields": (
                    "month_start_display",
                    "month_end_display",
                    "week_count",
                    "assigned_user_count",
                )
            },
        ),
        (
            "Audit",
            {
                "fields": (
                    "created_by",
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    @admin.display(description="Period")
    def period(self, obj):
        return f"{obj.month:02d}/{obj.year}"

    @admin.display(description="Month start")
    def month_start_display(self, obj):
        return obj.month_start

    @admin.display(description="Month end")
    def month_end_display(self, obj):
        return obj.month_end

    @admin.display(
        description="Weeks",
        ordering="week_count",
    )
    def week_count(self, obj):
        return obj.weeks.count()

    @admin.display(
        description="Assigned users",
        ordering="assigned_user_count",
    )
    def assigned_user_count(self, obj):
        return obj.get_assigned_users().count()

    def save_model(self, request, obj, form, change):
        if not obj.created_by_id:
            obj.created_by = request.user

        super().save_model(
            request,
            obj,
            form,
            change,
        )


@admin.register(DutyWeek)
class DutyWeekAdmin(PlantScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "date_range",
        "schedule",
        "treatment_plant_display",
        "section_display",
        "weekly_task_count",
        "created_at",
    )
    list_filter = (
        "schedule__treatment_plant",
        "schedule__section",
        "start_date",
        "end_date",
    )
    search_fields = (
        "schedule__treatment_plant__name",
        "schedule__section__name",
    )
    autocomplete_fields = (
        "schedule",
    )
    readonly_fields = (
        "treatment_plant_display",
        "section_display",
        "weekly_task_count",
        "created_at",
        "updated_at",
    )
    ordering = (
        "start_date",
        "end_date",
    )
    date_hierarchy = "start_date"
    fieldsets = (
        (
            "Duty Week",
            {
                "fields": (
                    "schedule",
                    "start_date",
                    "end_date",
                )
            },
        ),
        (
            "Derived Context",
            {
                "fields": (
                    "treatment_plant_display",
                    "section_display",
                    "weekly_task_count",
                )
            },
        ),
        (
            "Audit",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    @admin.display(description="Date range")
    def date_range(self, obj):
        return f"{obj.start_date} → {obj.end_date}"

    @admin.display(
        description="Treatment plant",
        ordering="schedule__treatment_plant__name",
    )
    def treatment_plant_display(self, obj):
        return obj.treatment_plant

    @admin.display(
        description="Section",
        ordering="schedule__section__name",
    )
    def section_display(self, obj):
        return obj.section

    @admin.display(
        description="Weekly tasks",
        ordering="weekly_task_count",
    )
    def weekly_task_count(self, obj):
        return obj.weeks.count() if hasattr(obj, "weeks") else obj.weekly_tasks.count()

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "schedule",
                "schedule__treatment_plant",
                "schedule__section",
            )
            .annotate(
                _weekly_task_count=Count(
                    "weekly_tasks",
                    distinct=True,
                )
            )
        )

    @admin.display(
        description="Weekly tasks",
        ordering="_weekly_task_count",
    )
    def weekly_task_count(self, obj):
        return obj._weekly_task_count


@admin.register(WeeklyDutyTask)
class WeeklyDutyTaskAdmin(PlantScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "task_template",
        "week",
        "treatment_plant",
        "section",
        "date_range",
        "assigned_user_count",
        "daily_assignment_count",
        "created_at",
    )
    list_filter = (
        "task_template",
        "treatment_plant",
        "section",
        "week__start_date",
        "week__end_date",
    )
    search_fields = (
        "task_template__code",
        "task_template__name",
        "treatment_plant__name",
        "section__name",
    )
    autocomplete_fields = (
        "week",
        "task_template",
        "treatment_plant",
        "section",
    )
    readonly_fields = (
        "start_date_display",
        "end_date_display",
        "assigned_user_count",
        "daily_assignment_count",
        "assigned_users_display",
        "assigned_sections_display",
        "available_shifts_display",
        "created_at",
        "updated_at",
    )
    ordering = (
        "week__start_date",
        "section",
        "task_template__code",
    )
    fieldsets = (
        (
            "Weekly Duty Task",
            {
                "fields": (
                    "week",
                    "task_template",
                    "treatment_plant",
                    "section",
                )
            },
        ),
        (
            "Task Period",
            {
                "fields": (
                    "start_date_display",
                    "end_date_display",
                )
            },
        ),
        (
            "Assignments",
            {
                "fields": (
                    "assigned_users_display",
                    "assigned_sections_display",
                    "available_shifts_display",
                    "assigned_user_count",
                    "daily_assignment_count",
                )
            },
        ),
        (
            "Audit",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    @admin.display(description="Date range")
    def date_range(self, obj):
        return f"{obj.start_date} → {obj.end_date}"

    @admin.display(description="Start date")
    def start_date_display(self, obj):
        return obj.start_date

    @admin.display(description="End date")
    def end_date_display(self, obj):
        return obj.end_date

    @admin.display(
        description="Assigned users",
        ordering="assigned_user_count",
    )
    def assigned_user_count(self, obj):
        return obj.get_assigned_users().count()

    @admin.display(description="Assigned users")
    def assigned_users_display(self, obj):
        users = obj.get_assigned_users()

        if not users.exists():
            return "No users assigned"

        return ", ".join(
            user.get_full_name() or user.username
            for user in users
        )

    @admin.display(description="Assigned sections")
    def assigned_sections_display(self, obj):
        names = obj.get_assigned_sections_names()

        if not names:
            return "No section assigned"

        return ", ".join(names)

    @admin.display(description="Available shifts")
    def available_shifts_display(self, obj):
        shifts = obj.get_available_shifts()

        if not shifts.exists():
            return "No configured shifts"

        return ", ".join(
            shift.name
            for shift in shifts
        )

    @admin.display(
        description="Daily assignments",
        ordering="daily_assignment_count",
    )
    def daily_assignment_count(self, obj):
        return obj.daily_assignments.count()

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "week",
                "week__schedule",
                "week__schedule__treatment_plant",
                "week__schedule__section",
                "task_template",
                "treatment_plant",
                "section",
            )
            .prefetch_related(
                "daily_assignments__user",
            )
            .annotate(
                _assigned_user_count=Count(
                    "daily_assignments__user",
                    filter=Q(
                        daily_assignments__user__is_active=True,
                        daily_assignments__user__assigned_treatment_plant_id=models.F("treatment_plant_id"),
                        daily_assignments__user__section_id=models.F("section_id"),
                    ),
                    distinct=True,
                ),
                _daily_assignment_count=Count(
                    "daily_assignments",
                    filter=Q(
                        daily_assignments__user__assigned_treatment_plant_id=models.F("treatment_plant_id"),
                        daily_assignments__user__section_id=models.F("section_id"),
                    ),
                    distinct=True,
                ),
            )
        )

    @admin.display(
        description="Assigned users",
        ordering="_assigned_user_count",
    )
    def assigned_user_count(self, obj):
        return obj._assigned_user_count

    @admin.display(
        description="Daily assignments",
        ordering="_daily_assignment_count",
    )
    def daily_assignment_count(self, obj):
        return obj._daily_assignment_count


@admin.register(DailyTaskAssignment)
class DailyTaskAssignmentAdmin(PlantScopedAdminMixin, admin.ModelAdmin):
    list_display = (
        "user",
        "task",
        "date",
        "shift",
        "treatment_plant_display",
        "section_display",
        "shift_assignment_status",
        "created_at",
    )
    list_filter = (
        "date",
        "shift",
        "task__task_template",
        "task__treatment_plant",
        "task__section",
        "created_at",
    )
    search_fields = (
        "user__username",
        "user__first_name",
        "user__last_name",
        "task__task_template__code",
        "task__task_template__name",
        "task__treatment_plant__name",
        "task__section__name",
        "remarks",
    )
    autocomplete_fields = (
        "task",
        "user",
        "shift",
    )
    readonly_fields = (
        "treatment_plant_display",
        "section_display",
        "valid_shifts_display",
        "shift_assignment_status",
        "created_at",
        "updated_at",
    )
    ordering = (
        "date",
        "shift__start_time",
        "user__last_name",
        "user__first_name",
    )
    date_hierarchy = "date"
    fieldsets = (
        (
            "Daily Assignment",
            {
                "fields": (
                    "task",
                    "user",
                    "date",
                    "shift",
                    "remarks",
                )
            },
        ),
        (
            "Derived Context",
            {
                "fields": (
                    "treatment_plant_display",
                    "section_display",
                    "valid_shifts_display",
                    "shift_assignment_status",
                )
            },
        ),
        (
            "Audit",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    @admin.display(
        description="Treatment plant",
        ordering="task__treatment_plant__name",
    )
    def treatment_plant_display(self, obj):
        return obj.treatment_plant

    @admin.display(
        description="Section",
        ordering="task__section__name",
    )
    def section_display(self, obj):
        return obj.section

    @admin.display(description="Valid shifts")
    def valid_shifts_display(self, obj):
        shifts = obj.get_valid_shifts()

        if not shifts.exists():
            return "No valid shifts"

        return ", ".join(
            shift.name
            for shift in shifts
        )

    @admin.display(description="Shift assignment")
    def shift_assignment_status(self, obj):
        if not obj.shift_id:
            return "No shift selected"

        exists = ShiftAssignment.objects.filter(
            staff_id=obj.user_id,
            shift_id=obj.shift_id,
            date=obj.date,
            is_deleted=False,
        ).exists()

        return "Valid" if exists else "Invalid"

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "task",
                "task__task_template",
                "task__treatment_plant",
                "task__section",
                "user",
                "user__section",
                "shift",
            )
        )

    def save_model(self, request, obj, form, change):
        if (
            not request.user.is_superuser
            and obj.treatment_plant_id
            != getattr(
                request.user,
                "assigned_treatment_plant_id",
                None,
            )
        ):
            raise ValidationError(
                "You cannot create or modify assignments "
                "for another treatment plant."
            )

        super().save_model(
            request,
            obj,
            form,
            change,
        )



admin.site.register(
    StandardWeeklyTask,
    StandardWeeklyTaskAdmin,
)
