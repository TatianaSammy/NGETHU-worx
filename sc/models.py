#sc/models.py
from __future__ import annotations
import threading
from datetime import  timedelta as py_timedelta
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from typing import Optional, List, Dict, Tuple
from django.db import models, transaction
from calendar import monthrange
from django.contrib.auth import get_user_model
from collections import defaultdict
from typing import List, Optional
from django.utils import timezone
from t.models import TreatmentPlant
from inventory.models import (Equipment, Status)
from django.conf import settings
from datetime import datetime, date, timedelta
from django.core.exceptions import ValidationError
from django.db import models
from django.contrib.postgres.fields import DateTimeRangeField

class SafeDateTimeRangeField(DateTimeRangeField):
    """DateTimeRangeField compatible with both Postgres and SQLite development."""
    def get_placeholder(self, value, compiler, connection):
        if connection.vendor == 'postgresql':
            return '%s::tstzrange'
        return '%s'

User = get_user_model()

# -------------------------------
_STATUS_CACHE: Dict[str, Status] = {}
_STATUS_CACHE_LOCK = threading.Lock()
_STATUS_CACHE_TTL = 60  # seconds
_STATUS_CACHE_TS: Dict[str, float] = {}


def get_status_by_name(name: str) -> Optional[Status]:
    """Retrieve Status by name with cache and TTL."""
    if not name:
        return None
    key = name.strip().lower()
    now_ts = timezone.now().timestamp()
    with _STATUS_CACHE_LOCK:
        if key in _STATUS_CACHE and now_ts - _STATUS_CACHE_TS.get(key, 0) < _STATUS_CACHE_TTL:
            return _STATUS_CACHE[key]
    status = Status.objects.filter(name__iexact=name).first()
    if status:
        with _STATUS_CACHE_LOCK:
            _STATUS_CACHE[key] = status
            _STATUS_CACHE_TS[key] = now_ts
    return status

def invalidate_status_cache(name: Optional[str] = None) -> None:
    """Invalidate cache optionally for a single name."""
    with _STATUS_CACHE_LOCK:
        if name:
            key = name.strip().lower()
            _STATUS_CACHE.pop(key, None)
            _STATUS_CACHE_TS.pop(key, None)
        else:
            _STATUS_CACHE.clear()
            _STATUS_CACHE_TS.clear()

# -------------------------------
# Audit Mixin
# -------------------------------
TRACKED_MODELS: set[str] = set()

class AuditMixin:
    """Add as a base for models you want audited manually."""
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        try:
            TRACKED_MODELS.add(cls._meta.model_name)
        except Exception:
            pass

    @staticmethod
    def create_audit(
        user: Optional[settings.AUTH_USER_MODEL],
        action: str,
        instance: models.Model,
        before: Optional[dict] = None,
        after: Optional[dict] = None
    ) -> None:
        ct = ContentType.objects.get_for_model(instance.__class__)
        AuditLog.objects.create(
            user=user,
            action=action,
            content_type=ct,
            object_id=str(getattr(instance, "pk", "")),
            object_repr=str(instance)[:512],
            changes={"before": before, "after": after} if (before or after) else None,
        )

# -------------------------------
# AuditLog
# -------------------------------
class AuditLog(models.Model):
    ACTION_CHOICES = [("create", "Create"), ("update", "Update"), ("delete", "Delete")]

    timestamp: datetime = models.DateTimeField(auto_now_add=True)
    user: Optional[settings.AUTH_USER_MODEL] = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="sc_audit_logs"
    )
    action: str = models.CharField(max_length=10, choices=ACTION_CHOICES)
    content_type: ContentType = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="sc_audit_logs")
    object_id: str = models.CharField(max_length=255)
    content_object: GenericForeignKey = GenericForeignKey("content_type", "object_id")
    object_repr: str = models.CharField(max_length=512)
    changes: Optional[dict] = models.JSONField(blank=True, null=True)

    class Meta:
        ordering = ["-timestamp"]
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"

    def __str__(self) -> str:
        return f"[{self.timestamp}] {self.action.upper()} {self.content_type.app_label}.{self.content_type.model}({self.object_id})"

# -------------------------------
# SoftDeleteMixin
# -------------------------------
class SoftDeleteQuerySet(models.QuerySet):
    def active(self) -> models.QuerySet:
        return self.filter(is_deleted=False)

class SoftDeleteMixin(models.Model):
    is_deleted: bool = models.BooleanField(default=False)
    deleted_at: Optional[datetime] = models.DateTimeField(null=True, blank=True)

    objects: SoftDeleteQuerySet = SoftDeleteQuerySet.as_manager()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False) -> None:
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save(update_fields=['is_deleted', 'deleted_at'])

# -------------------------------
# Shift Manager
# -------------------------------
class ShiftManager(models.Manager):
    def active(self) -> models.QuerySet:
        return self.get_queryset().filter(is_deleted=False)

    def for_treatment_plant(self, tp: TreatmentPlant) -> models.QuerySet:
        return self.active().filter(treatment_plant=tp)

# -------------------------------
# MonthlyDutyRosterManager
# -------------------------------
class MonthlyDutyRosterManager(models.Manager):
    def for_month_year(self, month: int, year: int, treatment_plant: Optional[TreatmentPlant] = None) -> models.QuerySet:
        qs = self.get_queryset().filter(month=month, year=year)
        if treatment_plant:
            qs = qs.filter(treatment_plant=treatment_plant)
        return qs

# ==========================================================
# SHIFT
# ==========================================================

def _minutes(t):
    return t.hour * 60 + t.minute


class Shift(SoftDeleteMixin, models.Model):
    name = models.CharField(max_length=50)
    start_time = models.TimeField()
    end_time = models.TimeField()
    handover_time = models.TimeField(
        null=True,
        blank=True,)
    takeover_time = models.TimeField(
        null=True,
        blank=True,)
    treatment_plant = models.ForeignKey(
        TreatmentPlant,
        on_delete=models.CASCADE,
        related_name="shifts",
    )

    time_range = SafeDateTimeRangeField(
        null=True,
        blank=True,
        editable=False,
    )

    objects = ShiftManager()

    class Meta:
        ordering = [
            "treatment_plant",
            "start_time",
        ]

        constraints = [
            models.UniqueConstraint(
                fields=[
                    "treatment_plant",
                    "name",
                ],
                name="unique_shift_name_per_plant",
            ),
        ]

    def __str__(self):
        return (
            f"{self.name} "
            f"({self.start_time:%H:%M}"
            f" → "
            f"{self.end_time:%H:%M})"
        )

    @property
    def is_overnight(self):
        return self.end_time <= self.start_time

    @property
    def duration(self):
        start = datetime.combine(date.today(), self.start_time)
        end = datetime.combine(date.today(), self.end_time)

        if self.is_overnight:
            end += timedelta(days=1)

        return end - start

    @classmethod
    def get_shift_for_time(
            cls,
            sample_time,
            treatment_plant=None,
    ):

        qs = cls.objects.active()

        if treatment_plant:
            qs = qs.filter(
                treatment_plant=treatment_plant
            )

        if isinstance(sample_time, datetime):

            if timezone.is_aware(sample_time):
                current_datetime = timezone.localtime(sample_time)
            else:
                current_datetime = timezone.make_aware(sample_time)

            current_time = current_datetime.time()

        else:
            current_time = sample_time

        shifts = list(
            qs.order_by(
                "start_time"
            )
        )

        for shift in shifts:

            start = shift.start_time
            end = shift.end_time

            # ------------------------------
            # Normal shift
            # Example:
            # 08:00 - 16:00
            # ------------------------------
            if not shift.is_overnight:

                if start <= current_time < end:
                    return shift


            # ------------------------------
            # Overnight shift
            # Example:
            # 16:00 - 00:00
            # 22:00 - 06:00
            # ------------------------------
            else:

                if (
                        current_time >= start
                        or
                        current_time < end
                ):
                    return shift

        return None

    @classmethod
    def get_shift_map(
            cls,
            times,
            treatment_plant=None,
    ):

        qs = cls.objects.active()

        if treatment_plant:
            qs = qs.filter(
                treatment_plant=treatment_plant
            )

        shifts = list(qs)

        mapping = {}

        for item in times:

            if isinstance(item, datetime):

                if timezone.is_aware(item):
                    current_time = timezone.localtime(item).time()
                else:
                    current_time = item.time()

            else:
                current_time = item

            mapping[item] = None

            for shift in shifts:

                start = shift.start_time
                end = shift.end_time

                if not shift.is_overnight:

                    if start <= current_time < end:
                        mapping[item] = shift
                        break


                else:

                    if (
                            current_time >= start
                            or
                            current_time < end
                    ):
                        mapping[item] = shift
                        break

        return mapping


    def _minutes(t):
        return (
                t.hour * 60
                +
                t.minute
        )

    def clean(self):

        if self.start_time == self.end_time:
            raise ValidationError(
                "Start and end time cannot be identical."
            )

        start = _minutes(self.start_time)
        end = _minutes(self.end_time)

        if end <= start:
            end += 1440

        for other in (
                Shift.objects.active()
                        .filter(
                    treatment_plant=self.treatment_plant
                )
                        .exclude(pk=self.pk)
        ):

            other_start = _minutes(
                other.start_time
            )

            other_end = _minutes(
                other.end_time
            )

            if other_end <= other_start:
                other_end += 1440

            ranges = [
                (start, end),
                (other_start, other_end),
                (other_start + 1440, other_end + 1440),
            ]

            if any(
                    a < d and c < b
                    for a, b in [(start, end)]
                    for c, d in [
                        (other_start, other_end),
                        (other_start + 1440, other_end + 1440),
                    ]
            ):
                raise ValidationError(
                    f"Shift overlaps with '{other.name}'."
                )

    def save(self, *args, **kwargs):

        self.full_clean()

        start = datetime.combine(
            date.today(),
            self.start_time,
        )

        end = datetime.combine(
            date.today(),
            self.end_time,
        )
        if self.is_overnight:
            end += timedelta(days=1)

        from django.db import connection
        if connection.vendor == 'postgresql':
            self.time_range = (start, end)
        else:
            self.time_range = None

        super().save(*args, **kwargs)

    def get_requirement(self, section):

        return self.section_requirements.filter(
            section=section
        ).first()

    def get_required_staff(
        self,
        section,
        day_type,
    ):

        requirement = self.get_requirement(section)

        if requirement is None:
            return 0

        return requirement.get_required_staff(
            day_type
        )


class ShiftSectionRequirement(models.Model):
    shift = models.ForeignKey(
        Shift,
        on_delete=models.CASCADE,
        related_name="section_requirements",
    )

    section = models.ForeignKey(
        "core.Section",
        on_delete=models.CASCADE,
        related_name="shift_requirements",)
    weekday_required = models.PositiveSmallIntegerField(default=1,)
    weekend_required = models.PositiveSmallIntegerField(default=1,)
    holiday_required = models.PositiveSmallIntegerField(default=1,)
    minimum_required = models.PositiveSmallIntegerField(default=1,)
    preferred_required = models.PositiveSmallIntegerField(default=1,)
    maximum_allowed = models.PositiveSmallIntegerField(default=20,)
    class Meta:
        ordering = [ "shift","section", ]

        constraints = [
            models.UniqueConstraint(
                fields=[
                    "shift",
                    "section",
                ],
                name="unique_shift_section_requirement",
            ),
        ]

    def __str__(self):

        return (
            f"{self.shift.name} - "
            f"{self.section}"
        )

    def get_required_staff(
        self,
        day_type,
    ):

        if (
            day_type ==
            ShiftAssignment.DAYTYPE_WEEKEND
        ):
            return self.weekend_required

        if (
            day_type ==
            ShiftAssignment.DAYTYPE_HOLIDAY
        ):
            return self.holiday_required

        return self.weekday_required

    def is_understaffed(
        self,
        assigned_count,
        day_type,
    ):

        return (
            assigned_count <
            self.get_required_staff(day_type)
        )

    def is_overstaffed(
        self,
        assigned_count,
    ):

        return (
            assigned_count >
            self.maximum_allowed
        )

    def has_optimal_staff(
        self,
        assigned_count,
    ):

        return (
            self.minimum_required
            <= assigned_count
            <= self.maximum_allowed
        )

    def clean(self):
        super().clean()
        if self.shift_id and self.section_id:
            shift_plant_id = self.shift.treatment_plant_id
            sec_plant_id = self.section.treatment_plant_id
            if not sec_plant_id or shift_plant_id != sec_plant_id:
                raise ValidationError({
                    "section": "The section must belong to the same treatment plant as the shift."
                })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

class ShiftSupervisor(AuditMixin, models.Model):
    treatment_plant: TreatmentPlant = models.ForeignKey(
        TreatmentPlant,
        on_delete=models.CASCADE,
        related_name="supervisors",
        help_text="Designated treatment plant for supervisory authority."
    )
    section = models.ForeignKey(
        "core.Section",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="shift_supervisors",
        help_text="Operational section supervised (must match user's section if assigned)."
    )
    user: settings.AUTH_USER_MODEL = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="shift_supervisor",
        help_text="User designated as shift supervisor."
    )
    is_active: bool = models.BooleanField(default=True, db_index=True)

    class Meta:
        verbose_name = "Shift Supervisor"
        verbose_name_plural = "Shift Supervisors"
        unique_together = ("treatment_plant", "user")
        indexes = [
            models.Index(fields=["treatment_plant", "is_active"], name="sc_shiftsup_plant_act_idx"),
            models.Index(fields=["treatment_plant", "section", "is_active"], name="sc_shiftsup_sec_act_idx"),
        ]

    def __str__(self) -> str:
        sec_label = f" [{self.section.name}]" if self.section else ""
        user_name = self.user.get_full_name() or getattr(self.user, "username", "Unknown")
        plant_name = getattr(self.treatment_plant, "name", "Unknown Plant")
        return f"{user_name}{sec_label} @ {plant_name}"

    def clean(self):
        super().clean()
        errors = {}

        if not self.user_id:
            return

        if not self.user.is_active:
            errors["user"] = f"Inactive user '{self.user.username}' cannot be designated as a shift supervisor."

        is_superuser = bool(self.user.is_superuser)
        if self.section_id:
            sec_plant_id = getattr(self.section, "treatment_plant_id", None)
            if not sec_plant_id or sec_plant_id != self.treatment_plant_id:
                errors["section"] = "The section must belong to the designated treatment plant."
            if hasattr(self.section, "is_active") and not self.section.is_active:
                errors["section"] = f"Section '{self.section.name}' is inactive and cannot have supervisors assigned."
        elif not is_superuser:
            errors["section"] = "A section is required for non-superuser shift supervisors."

        if not is_superuser:
            if self.user.assigned_treatment_plant_id != self.treatment_plant_id:
                errors["treatment_plant"] = "The supervisor and treatment plant must match the user's assigned plant."
            if not self.user.section_id or self.user.section_id != self.section_id:
                errors["section"] = "The supervisor section must match the user's assigned section."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class AnnualLeave(AuditMixin, models.Model):
    STATUS_ANNUAL_LEAVE = "annual_leave"
    STATUS_OFF_DUTY = "off_duty"
    STATUS_CHOICES = [
        (STATUS_ANNUAL_LEAVE, "Annual leave"),
        (STATUS_OFF_DUTY, "Off duty"),
    ]
    UNAVAILABLE_STATUSES = (STATUS_ANNUAL_LEAVE, STATUS_OFF_DUTY)

    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="annual_leave_records",
    )
    treatment_plant = models.ForeignKey(
        TreatmentPlant,
        on_delete=models.CASCADE,
        related_name="annual_leave_records",
    )
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(db_index=True)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_ANNUAL_LEAVE,
        db_index=True,
    )
    reason = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_sc_annual_leaves",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_sc_annual_leaves",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Staff availability"
        verbose_name_plural = "Staff availability"
        ordering = ["start_date", "staff__last_name", "staff__first_name"]
        permissions = [
            ("manage_staff_availability", "Can set staff annual leave and off-duty periods"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F("start_date")),
                name="sc_leave_end_after_start",
            ),
        ]
        indexes = [
            models.Index(
                fields=["treatment_plant", "status", "start_date"],
                name="sc_leave_plant_status_idx",
            ),
            models.Index(
                fields=["staff", "status", "start_date", "end_date"],
                name="sc_leave_staff_period_idx",
            ),
        ]

    def __str__(self):
        staff_name = self.staff.get_full_name() or self.staff.get_username()
        return f"{staff_name}: {self.start_date}–{self.end_date} ({self.get_status_display()})"

    @property
    def duration_days(self):
        if not self.start_date or not self.end_date:
            return 0
        return (self.end_date - self.start_date).days + 1

    @classmethod
    def is_unavailable(cls, staff_id, target_date):
        if not staff_id or not target_date:
            return False
        return cls.objects.filter(
            staff_id=staff_id,
            status__in=cls.UNAVAILABLE_STATUSES,
            start_date__lte=target_date,
            end_date__gte=target_date,
        ).exists()

    def clean(self):
        super().clean()
        errors = {}

        if self.status not in self.UNAVAILABLE_STATUSES:
            errors["status"] = "Choose annual leave or off duty."

        if self.start_date and self.end_date and self.end_date < self.start_date:
            errors["end_date"] = "The end date cannot be earlier than the start date."

        if self.staff_id and self.treatment_plant_id and not self.staff.is_superuser:
            assigned_plant_id = self.staff.assigned_treatment_plant_id
            section_plant_id = getattr(getattr(self.staff, "section", None), "treatment_plant_id", None)
            if assigned_plant_id != self.treatment_plant_id or section_plant_id != self.treatment_plant_id:
                errors["staff"] = "The staff member, section, and annual leave must belong to the same treatment plant."
            if not self.staff.section_id:
                errors["staff"] = "A section assignment is required before staff availability can be recorded."

        if self.staff_id and self.start_date and self.end_date:
            overlapping_leave = AnnualLeave.objects.filter(
                staff_id=self.staff_id,
                start_date__lte=self.end_date,
                end_date__gte=self.start_date,
            )
            if self.pk:
                overlapping_leave = overlapping_leave.exclude(pk=self.pk)
            if overlapping_leave.exists():
                errors["start_date"] = "This staff member already has an overlapping availability period."

            existing_duties = ShiftAssignment.objects.filter(
                is_deleted=False,
                date__range=(self.start_date, self.end_date),
            ).filter(
                models.Q(staff_id=self.staff_id)
                | models.Q(supervisor_id=self.staff_id)
                | models.Q(temp_supervisor_id=self.staff_id)
                | models.Q(handover_to_id=self.staff_id)
            )
            conflict_messages = []
            if existing_duties.exists():
                conflict_messages.append("Reassign or remove overlapping shift duties before setting this status.")

            from django.apps import apps

            daily_task_assignment = apps.get_model("weeklytasks", "DailyTaskAssignment")
            existing_tasks = daily_task_assignment.objects.filter(
                user_id=self.staff_id,
                date__range=(self.start_date, self.end_date),
            )
            if existing_tasks.exists():
                conflict_messages.append("Reassign or remove overlapping task duties before setting this status.")

            existing_handovers = ShiftHandover.objects.filter(
                date__range=(self.start_date, self.end_date),
            ).filter(
                models.Q(outgoing_supervisor_id=self.staff_id)
                | models.Q(incoming_supervisor_id=self.staff_id)
            )
            if existing_handovers.exists():
                conflict_messages.append("Reassign or remove overlapping shift handovers before setting this status.")
            if conflict_messages:
                errors["start_date"] = " ".join(conflict_messages)

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            previous = None
            if self.pk:
                previous = AnnualLeave.objects.filter(pk=self.pk).values(
                    "staff_id", "treatment_plant_id", "start_date", "end_date", "status"
                ).first()
                if previous:
                    previous["start_date"] = previous["start_date"].isoformat()
                    previous["end_date"] = previous["end_date"].isoformat()
            staff_ids = {self.staff_id} if self.staff_id else set()
            if previous and previous["staff_id"]:
                staff_ids.add(previous["staff_id"])
            list(User.objects.select_for_update().filter(pk__in=sorted(staff_ids)).order_by("pk"))
            self.full_clean()
            super().save(*args, **kwargs)
            AuditMixin.create_audit(
                user=self.updated_by or self.created_by,
                action="update" if previous else "create",
                instance=self,
                before=previous,
                after={
                    "staff_id": self.staff_id,
                    "treatment_plant_id": self.treatment_plant_id,
                    "start_date": self.start_date.isoformat() if self.start_date else None,
                    "end_date": self.end_date.isoformat() if self.end_date else None,
                    "status": self.status,
                },
            )


class ShiftAssignment(AuditMixin, SoftDeleteMixin, models.Model):
    # -----------------------------
    # Attendance
    # -----------------------------
    ATTENDANCE_PRESENT = 'present'
    ATTENDANCE_ABSENT = 'absent'
    ATTENDANCE_LATE = 'late'
    ATTENDANCE_EXCUSED = 'excused'
    ATTENDANCE_CHOICES = [
        (ATTENDANCE_PRESENT, 'Present'),
        (ATTENDANCE_ABSENT, 'Absent'),
        (ATTENDANCE_LATE, 'Late'),
        (ATTENDANCE_EXCUSED, 'Excused')
    ]

    # -----------------------------
    # Day type
    # -----------------------------
    DAYTYPE_WEEKDAY = 'weekday'
    DAYTYPE_WEEKEND = 'weekend'
    DAYTYPE_HOLIDAY = 'holiday'
    DAYTYPE_CHOICES = [
        (DAYTYPE_WEEKDAY, 'Weekday'),
        (DAYTYPE_WEEKEND, 'Weekend'),
        (DAYTYPE_HOLIDAY, 'Holiday')
    ]

    # -----------------------------
    # Fields
    # -----------------------------
    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='shift_assignments'
    )
    shift = models.ForeignKey(
        'Shift',
        on_delete=models.CASCADE,
        related_name='assignments'
    )
    date = models.DateField()
    day_type = models.CharField(
        max_length=10,
        choices=DAYTYPE_CHOICES,
        default=DAYTYPE_WEEKDAY,
        help_text="Automatically populated: weekday, weekend, or holiday"
    )
    attendance_status = models.CharField(max_length=10, choices=ATTENDANCE_CHOICES, default=ATTENDANCE_PRESENT)
    remarks = models.TextField(blank=True, null=True)
    duty_roster = models.ForeignKey(
        'MonthlyDutyRoster',
        on_delete=models.CASCADE,
        related_name='assignments'
    )
    supervisor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='supervised_assignments'
    )
    reassigned_from = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reassignments'
    )
    handover_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+'
    )
    reassigned_at = models.DateTimeField(null=True, blank=True)
    temp_supervisor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='temp_supervised_assignments'
    )
    temp_supervisor_start = models.DateTimeField(null=True, blank=True)
    temp_supervisor_end = models.DateTimeField(null=True, blank=True)

    # -----------------------------
    # Meta
    # -----------------------------
    class Meta:
        unique_together = ('staff', 'date', 'shift')
        ordering = ['date', 'shift']

    def _assignment_period_changed(self):
        if self._state.adding:
            return True
        previous = type(self).objects.filter(pk=self.pk).values(
            "staff_id", "shift_id", "date", "duty_roster_id", "supervisor_id", "handover_to_id",
            "temp_supervisor_id", "reassigned_from_id", "is_deleted"
        ).first()
        return previous is None or any(
            previous[field] != value
            for field, value in (
                ("staff_id", self.staff_id),
                ("shift_id", self.shift_id),
                ("date", self.date),
                ("duty_roster_id", self.duty_roster_id),
                ("supervisor_id", self.supervisor_id),
                ("handover_to_id", self.handover_to_id),
                ("temp_supervisor_id", self.temp_supervisor_id),
                ("reassigned_from_id", self.reassigned_from_id),
                ("is_deleted", self.is_deleted),
            )
        )

    def clean(self):
        super().clean()
        errors = {}
        period_changed = self._assignment_period_changed()

        if self.staff_id and self.shift_id and not self.staff.is_superuser:
            if self.staff.assigned_treatment_plant_id != self.shift.treatment_plant_id:
                errors["staff"] = "The staff member and shift must belong to the same treatment plant."
            if not self.staff.section_id or not ShiftSectionRequirement.objects.filter(
                shift_id=self.shift_id,
                section_id=self.staff.section_id,
            ).exists():
                errors["staff"] = "The shift must be configured for the staff member's assigned section."

        if self.duty_roster_id and self.shift_id:
            if self.duty_roster.treatment_plant_id != self.shift.treatment_plant_id:
                errors["duty_roster"] = "The roster and shift must belong to the same treatment plant."
            if self.date and (self.date.month != self.duty_roster.month or self.date.year != self.duty_roster.year):
                errors["date"] = "The assignment date must fall within the roster month."

        assignment_section_id = getattr(self.staff, "section_id", None) if self.staff_id else None
        for field_name in ("supervisor", "temp_supervisor", "handover_to"):
            person = getattr(self, field_name, None)
            if not person or person.is_superuser:
                continue
            if self.shift_id and person.assigned_treatment_plant_id != self.shift.treatment_plant_id:
                errors[field_name] = "The assigned supervisor must belong to the shift's treatment plant."
            if not assignment_section_id or person.section_id != assignment_section_id:
                errors[field_name] = "The assigned supervisor must belong to the staff member's section."

        if self.reassigned_from_id and self.shift_id:
            source = self.reassigned_from
            if source.shift.treatment_plant_id != self.shift.treatment_plant_id:
                errors["reassigned_from"] = "The source assignment must belong to the same treatment plant."
            if source.staff.section_id != assignment_section_id:
                errors["reassigned_from"] = "The source assignment must belong to the same section."

        if (
            self.staff_id
            and self.date
            and period_changed
            and AnnualLeave.is_unavailable(self.staff_id, self.date)
        ):
            errors["staff"] = "This staff member is marked on annual leave or off duty for the selected date."
        if self.date and period_changed:
            for field_name in ("supervisor", "temp_supervisor"):
                user_id = getattr(self, f"{field_name}_id")
                if user_id and AnnualLeave.is_unavailable(user_id, self.date):
                    errors[field_name] = "A staff member marked on annual leave or off duty cannot supervise a duty on this date."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        assignment_fields = {
            "staff", "staff_id", "shift", "shift_id", "date", "duty_roster", "duty_roster_id",
            "supervisor", "supervisor_id", "handover_to", "handover_to_id", "temp_supervisor",
            "temp_supervisor_id", "reassigned_from", "reassigned_from_id", "is_deleted",
        }
        validate_assignment = not update_fields or assignment_fields.intersection(update_fields)
        with transaction.atomic():
            period_changed = validate_assignment and self._assignment_period_changed()
            if validate_assignment:
                previous = None
                if self.pk:
                    previous = type(self).objects.filter(pk=self.pk).values(
                        "staff_id", "supervisor_id", "temp_supervisor_id"
                    ).first()
                user_ids = sorted({
                    user_id
                    for user_id in (
                        previous.get("staff_id") if previous else None,
                        previous.get("supervisor_id") if previous else None,
                        previous.get("temp_supervisor_id") if previous else None,
                        previous.get("handover_to_id") if previous else None,
                        self.staff_id,
                        self.supervisor_id,
                        self.handover_to_id,
                        self.temp_supervisor_id,
                    )
                    if user_id
                })
                list(User.objects.select_for_update().filter(pk__in=user_ids).order_by("pk"))
            if (
                period_changed
                and not self.is_deleted
                and self.staff_id
                and self.date
                and AnnualLeave.is_unavailable(self.staff_id, self.date)
            ):
                raise ValidationError({
                    "staff": "This staff member is marked on annual leave or off duty for the selected date."
                })
            if period_changed and not self.is_deleted and self.date:
                for field_name in ("supervisor", "temp_supervisor"):
                    user_id = getattr(self, f"{field_name}_id")
                    if user_id and AnnualLeave.is_unavailable(user_id, self.date):
                        raise ValidationError({
                            field_name: "A staff member marked on annual leave or off duty cannot supervise a duty on this date."
                        })
            if validate_assignment:
                self.full_clean()
            super().save(*args, **kwargs)

    # -----------------------------
    # Properties
    # -----------------------------
    @property
    def week(self):
        return self.date.isocalendar()[1] if self.date else None

    @property
    def section(self):
        return self.staff.section if self.staff else None

    @property
    def is_supervised(self):
        return self.supervisor is not None

    @property
    def duration(self):
        if self.shift and self.shift.start_time and self.shift.end_time:
            start_dt = datetime.combine(date.min, self.shift.start_time)
            end_dt = datetime.combine(date.min, self.shift.end_time)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
            return (end_dt - start_dt).total_seconds() / 3600
        return None

    @property
    def effective_supervisor(self):
        self.check_revert_temp_supervisor()
        return self.temp_supervisor if self.temp_supervisor else self.supervisor

    # -----------------------------
    # Methods
    # -----------------------------
    def assign_temp_supervisor(self, new_supervisor, duration_hours=3):
        if self.attendance_status != self.ATTENDANCE_PRESENT:
            raise ValueError(f"{self.staff} is not present and cannot supervise.")

        now = timezone.now()
        end_time = now + timedelta(hours=duration_hours)
        old_temp = self.temp_supervisor

        self.attendance_status = self.ATTENDANCE_EXCUSED
        self.temp_supervisor = new_supervisor
        self.temp_supervisor_start = now
        self.temp_supervisor_end = end_time
        self.save(update_fields=['attendance_status', 'temp_supervisor', 'temp_supervisor_start', 'temp_supervisor_end'])

        AuditMixin.create_audit(
            user=new_supervisor,
            action="update",
            instance=self,
            before={"temp_supervisor": old_temp.id if old_temp else None, "supervisor_status": self.ATTENDANCE_PRESENT},
            after={"temp_supervisor": new_supervisor.id, "duration_hours": duration_hours, "supervisor_status": self.ATTENDANCE_EXCUSED}
        )

    def check_revert_temp_supervisor(self):
        if self.temp_supervisor and self.temp_supervisor_end and timezone.now() >= self.temp_supervisor_end:
            old_temp = self.temp_supervisor
            self.temp_supervisor = None
            self.temp_supervisor_start = None
            self.temp_supervisor_end = None
            self.attendance_status = self.ATTENDANCE_PRESENT
            self.save(update_fields=['temp_supervisor', 'temp_supervisor_start', 'temp_supervisor_end', 'attendance_status'])

            AuditMixin.create_audit(
                user=self.staff,
                action="update",
                instance=self,
                before={"temp_supervisor": old_temp.id},
                after={"temp_supervisor": None, "attendance_status": self.ATTENDANCE_PRESENT}
            )

    def mark_attendance(self, status, remarks=None):
        old_status = self.attendance_status
        old_remarks = self.remarks
        self.attendance_status = status
        if remarks is not None:
            self.remarks = remarks
        self.save(update_fields=['attendance_status', 'remarks'])

        AuditMixin.create_audit(
            user=self.staff,
            action='update',
            instance=self,
            before={'attendance_status': old_status, 'remarks': old_remarks},
            after={'attendance_status': status, 'remarks': self.remarks}
        )

    def record_handover(self, handover=None, taken_over_by=None, notes="", merge_pending=True):
        from sc.models import ShiftHandover

        if handover is None:
            pending_summary = f"Remarks: {self.remarks or 'No remarks'}" if merge_pending else ""
            handover = ShiftHandover.objects.create(
                shift=self.shift,
                date=self.date,
                outgoing_supervisor=self.staff,
                incoming_supervisor=taken_over_by,
                handover_notes=notes,
                pending_work=pending_summary,
                is_taken_over=True,
                taken_over_at=timezone.now()
            )
            AuditMixin.create_audit(
                user=self.staff,
                action="create",
                instance=handover,
                after={
                    "incoming_supervisor": taken_over_by.id if taken_over_by else None,
                    "handover_notes": notes,
                    "is_taken_over": True,
                    "pending_work": pending_summary
                }
            )

        old_handover_to = self.handover_to
        self.handover_to = handover.incoming_supervisor
        self.save(update_fields=['handover_to'])

        AuditMixin.create_audit(
            user=self.staff,
            action="update",
            instance=self,
            before={"handover_to": old_handover_to.id if old_handover_to else None},
            after={"handover_to": handover.incoming_supervisor.id if handover.incoming_supervisor else None}
        )

        return handover

    @classmethod
    def bulk_record_handover(cls, assignments, handover):
        if not handover.incoming_supervisor:
            return

        assignment_ids = [a.id for a in assignments]
        assignments_qs = cls.objects.filter(id__in=assignment_ids)
        if not handover.incoming_supervisor.is_superuser:
            assignments_qs = assignments_qs.filter(
                shift__treatment_plant_id=handover.shift.treatment_plant_id,
                staff__assigned_treatment_plant_id=handover.shift.treatment_plant_id,
                staff__section_id=handover.incoming_supervisor.section_id,
                shift__section_requirements__section_id=handover.incoming_supervisor.section_id,
            ).distinct()
        assignments = list(assignments_qs.select_related("staff", "handover_to"))
        assignment_ids = [assignment.pk for assignment in assignments]
        cls.objects.filter(pk__in=assignment_ids).update(handover_to=handover.incoming_supervisor)

        # Batch audit logs
        audit_logs = []
        now = timezone.now()
        content_type_id = ContentType.objects.get_for_model(cls).id
        for assignment in assignments:
            old_val = assignment.handover_to.id if assignment.handover_to else None
            audit_logs.append(AuditLog(
                user=assignment.staff,
                action='update',
                content_type_id=content_type_id,
                object_id=assignment.id,
                object_repr=str(assignment),
                changes={'handover_to': {'before': old_val, 'after': handover.incoming_supervisor.id}},
                timestamp=now
            ))
        AuditLog.objects.bulk_create(audit_logs)

    def reassign_to_shift(self, new_shift):
        if self.shift == new_shift:
            return self

        if AnnualLeave.is_unavailable(self.staff_id, self.date):
            raise ValidationError("A staff member marked on annual leave or off duty cannot be assigned to a duty shift.")

        old_shift = self.shift
        old_handover_to = self.handover_to

        self.delete()
        self.reassigned_at = timezone.now()
        AuditMixin.create_audit(
            user=self.staff,
            action="update",
            instance=self,
            before={
                "shift": old_shift.id,
                "handover_to": old_handover_to.id if old_handover_to else None
            },
            after={
                "shift": new_shift.id,
                "handover_to": None,
                "is_deleted": True,
                "reassigned_at": self.reassigned_at.isoformat()
            }
        )

        new_assignment = ShiftAssignment.objects.create(
            staff=self.staff,
            shift=new_shift,
            date=self.date,
            duty_roster=self.duty_roster,
            reassigned_from=self
        )
        AuditMixin.create_audit(
            user=self.staff,
            action="create",
            instance=new_assignment
        )

        return new_assignment


class MonthlyDutyRoster(AuditMixin, models.Model):
    month = models.PositiveIntegerField(choices=[(i, i) for i in range(1, 13)])
    year = models.PositiveIntegerField()
    treatment_plant = models.ForeignKey(
        TreatmentPlant,
        on_delete=models.CASCADE,
        related_name='monthly_rosters'
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_rosters"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True, null=True)

    objects = MonthlyDutyRosterManager()

    class Meta:
        unique_together = ('month', 'year', 'treatment_plant')
        ordering = ['-year', '-month']

    def clean(self) -> None:
        """Ensure month/year is valid."""
        try:
            date(self.year, self.month, 1)
        except ValueError:
            raise ValidationError("Invalid month/year combination")

    def save(self, *args, **kwargs) -> None:
        self.full_clean()
        with transaction.atomic():
            super().save(*args, **kwargs)

    @staticmethod
    def get_weeks_in_month(year: int, month: int) -> list[tuple[date, date]]:
        """Return list of (week_start, week_end) tuples for the month (Monday → Sunday)."""
        weeks = []
        first_day = date(year, month, 1)
        last_day = date(year, month, monthrange(year, month)[1])
        current_monday = first_day - timedelta(days=first_day.weekday())

        while current_monday <= last_day:
            week_start = max(current_monday, first_day)
            week_end = min(current_monday + timedelta(days=6), last_day)
            weeks.append((week_start, week_end))
            current_monday += timedelta(days=7)
        return weeks

    @staticmethod
    def assign_monthly_shifts_bulk(
            duty_roster: "MonthlyDutyRoster",
            section,
            holidays: Optional[list[date]] = None,
            start_date: Optional[date] = None,
    ) -> None:
        holidays = set(holidays or [])
        if not section or section.treatment_plant_id != duty_roster.treatment_plant_id:
            raise ValidationError("The requested roster section must belong to the roster's treatment plant.")

        with transaction.atomic():

            MonthlyDutyRoster.objects.select_for_update().get(pk=duty_roster.pk )
            shifts = list(
                Shift.objects.filter(
                    treatment_plant=duty_roster.treatment_plant,
                    is_deleted=False,
                )
                .prefetch_related("section_requirements")
                .order_by("start_time")
            )

            if not shifts:
                return

            requirement_map = {
                (req.shift_id, req.section_id): req
                for shift in shifts
                for req in shift.section_requirements.all()}
            staff_list = list(
                User.objects.filter(
                    assigned_treatment_plant=duty_roster.treatment_plant,
                    section=section,
                    is_active=True,
                ).select_for_update().order_by("id"))

            if not staff_list:
                return

            staff_ids = [staff.id for staff in staff_list]
            unavailable_days_by_staff = defaultdict(set)
            month_start = date(duty_roster.year, duty_roster.month, 1)
            month_end = date(duty_roster.year, duty_roster.month, monthrange(duty_roster.year, duty_roster.month)[1])
            unavailable_periods = AnnualLeave.objects.filter(
                staff_id__in=staff_ids,
                treatment_plant=duty_roster.treatment_plant,
                status__in=AnnualLeave.UNAVAILABLE_STATUSES,
                start_date__lte=month_end,
                end_date__gte=month_start,
            ).values_list("staff_id", "start_date", "end_date")
            for staff_id, unavailable_start, unavailable_end in unavailable_periods:
                current_unavailable_day = max(unavailable_start, month_start)
                unavailable_end = min(unavailable_end, month_end)
                while current_unavailable_day <= unavailable_end:
                    unavailable_days_by_staff[staff_id].add(current_unavailable_day)
                    current_unavailable_day += timedelta(days=1)

            staff_lookup = {s.id: s for s in staff_list}
            supervisors = list(
                ShiftSupervisor.objects.filter(
                    treatment_plant=duty_roster.treatment_plant,
                    user__assigned_treatment_plant=duty_roster.treatment_plant,
                    user__section=section,
                    user__is_active=True,
                    is_active=True,
                ).select_related("user")
            )

            supervisor_cycle = [s.user for s in supervisors]
            existing = list(
                ShiftAssignment.objects.filter(
                    duty_roster=duty_roster,
                    staff__assigned_treatment_plant=duty_roster.treatment_plant,
                    staff__section=section,
                    staff__is_active=True,
                    is_deleted=False,
                ).select_related("staff", "shift")
            )

            existing_by_shift = defaultdict(set)
            staff_load = defaultdict(int)
            last_shift = {}

            for assignment in existing:
                existing_by_shift[
                    (assignment.date, assignment.shift_id)
                ].add(assignment.staff_id)

                staff_load[assignment.staff_id] += 1

                last_shift[assignment.staff_id] = (
                    assignment.date,
                    assignment.shift,
                )
            rotation_seed = duty_roster.month + duty_roster.year
            staff_order = sorted(
                staff_list,
                key=lambda s: (
                    staff_load[s.id],
                    (s.id + rotation_seed) % len(staff_list),),)
            new_assignments = []

            assignment_start = max(month_start, start_date) if start_date else month_start

            for week_start, week_end in MonthlyDutyRoster.get_weeks_in_month(duty_roster.year,duty_roster.month,):

                current = max(week_start, assignment_start)

                while current <= week_end:

                    if current in holidays:
                        day_type = ShiftAssignment.DAYTYPE_HOLIDAY

                    elif current.weekday() >= 5:
                        day_type = ShiftAssignment.DAYTYPE_WEEKEND

                    else:
                        day_type = ShiftAssignment.DAYTYPE_WEEKDAY

                    for shift in shifts:

                        req = requirement_map.get(
                            (shift.id, section.id)
                        )

                        if req is None:
                            continue

                        required = req.get_required_staff(day_type)

                        assigned = existing_by_shift.setdefault(
                            (current, shift.id),
                            set(),
                        )

                        if len(assigned) >= required:
                            continue

                        candidates = []
                        for staff in staff_order:
                            if staff.id in assigned or current in unavailable_days_by_staff.get(staff.id, set()):
                                continue
                            previous = last_shift.get(staff.id)
                            if previous:
                                prev_date, prev_shift = previous
                                if (
                                        prev_shift.is_overnight
                                        and prev_date == current - timedelta(days=1)
                                        and shift.start_time < prev_shift.end_time
                                ):
                                    continue
                            candidates.append(staff)
                        candidates.sort(
                            key=lambda s: (
                                staff_load[s.id],
                                (s.id + current.day) % len(staff_list),))
                        needed = required - len(assigned)
                        for staff in candidates[:needed]:
                            new_assignments.append(
                                ShiftAssignment(
                                    duty_roster=duty_roster,
                                    shift=shift,
                                    staff=staff,
                                    date=current,
                                    day_type=day_type,
                                )
                            )
                            assigned.add(staff.id)
                            staff_load[staff.id] += 1
                            last_shift[staff.id] = (current,shift, )

                    current += timedelta(days=1)

            if new_assignments:
                ShiftAssignment.objects.bulk_create(
                    new_assignments,
                    batch_size=1000,
                    ignore_conflicts=True,
                )

            # -----------------------------------------------------
            # Supervisor assignment
            # -----------------------------------------------------

            if not supervisor_cycle:
                return

            assignments = list(
                ShiftAssignment.objects.filter(
                    duty_roster=duty_roster,
                    staff__assigned_treatment_plant=duty_roster.treatment_plant,
                    staff__section=section,
                    staff__is_active=True,
                    date__gte=assignment_start,
                    is_deleted=False,
                )
                .select_related("shift")
                .order_by(
                    "date",
                    "shift__start_time",
                )
            )

            updates = []

            supervisor_index = 0

            for assignment in assignments:

                available_supervisors = [
                    supervisor
                    for supervisor in supervisor_cycle
                    if assignment.date not in unavailable_days_by_staff.get(supervisor.id, set())
                ]
                if not available_supervisors:
                    continue

                supervisor = available_supervisors[supervisor_index % len(available_supervisors)]

                supervisor_index += 1

                if assignment.supervisor_id != supervisor.id:
                    assignment.supervisor = supervisor
                    updates.append(assignment)

            if updates:
                ShiftAssignment.objects.bulk_update(
                    updates,
                    ["supervisor"],
                    batch_size=1000,
                )

            # -----------------------------------------------------
            # Existing handovers
            # -----------------------------------------------------

            existing_handovers = {
                (h.shift_id, h.date)
                for h in ShiftHandover.objects.filter(
                    shift__treatment_plant=duty_roster.treatment_plant,
                    date__year=duty_roster.year,
                    date__month=duty_roster.month,
                )
            }

            handovers = []

            for assignment in assignments:

                key = (
                    assignment.shift_id,
                    assignment.date,
                )

                if key in existing_handovers:
                    continue

                existing_handovers.add(key)

                handovers.append(
                    ShiftHandover(
                        shift=assignment.shift,
                        date=assignment.date,
                        incoming_supervisor=assignment.supervisor,
                        handover_notes=(
                            f"Automatic handover for "
                            f"{assignment.shift.name}"
                        ),
                    )
                )

            if handovers:
                ShiftHandover.objects.bulk_create(
                    handovers,
                    batch_size=1000,
                    ignore_conflicts=True,
                )
class ShiftHandover(AuditMixin, models.Model):
    shift = models.ForeignKey(
        'Shift',
        on_delete=models.CASCADE,
        related_name='handovers'
    )
    date = models.DateField()

    outgoing_supervisor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='handovers_given'
    )
    incoming_supervisor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='handovers_taken'
    )

    handover_notes = models.TextField(blank=True, null=True)
    pending_work = models.TextField(blank=True, null=True)

    handed_over_at = models.DateTimeField(auto_now_add=True)
    takeover_notes = models.TextField(blank=True, null=True)
    taken_over_at = models.DateTimeField(blank=True, null=True)

    is_taken_over = models.BooleanField(default=False)

    class Meta:
        unique_together = ('shift', 'date')
        ordering = ['-date', 'shift']

    def __str__(self):
        return f"Handover ({self.date}) - {self.shift.name} [{self.outgoing_supervisor} ➔ {self.incoming_supervisor}]"

    @property
    def expected_handover_time(self):
        return self.shift.handover_time

    @property
    def expected_takeover_time(self):
        return self.shift.takeover_time

    def clean(self):
        """Ensure handover/takeover occur within ±15 minutes of expected shift times."""
        self.validate_scope_integrity()
        for dt_field, expected_time, label in [
            ('handed_over_at', self.expected_handover_time, "Handover"),
            ('taken_over_at', self.expected_takeover_time, "Takeover")
        ]:
            actual = getattr(self, dt_field)
            if actual and expected_time:
                expected_dt = datetime.combine(self.date, expected_time)
                if timezone.is_aware(actual):
                    expected_dt = timezone.make_aware(expected_dt, timezone.get_current_timezone())
                window_start = expected_dt - timedelta(minutes=15)
                window_end = expected_dt + timedelta(minutes=15)
                if not (window_start <= actual <= window_end):
                    raise ValidationError(
                        f"{label} must occur within ±15 minutes of {expected_time.strftime('%H:%M')}."
                    )

    def validate_scope_integrity(self):
        errors = {}
        if self.shift_id:
            ordinary_sections = set()
            for field_name in ("outgoing_supervisor", "incoming_supervisor"):
                person = getattr(self, field_name, None)
                if not person or person.is_superuser:
                    continue
                if person.assigned_treatment_plant_id != self.shift.treatment_plant_id:
                    errors[field_name] = "The supervisor must belong to the shift's treatment plant."
                if not person.section_id or not ShiftSectionRequirement.objects.filter(
                    shift_id=self.shift_id,
                    section_id=person.section_id,
                ).exists():
                    errors[field_name] = "The supervisor must belong to a section configured for this shift."
                else:
                    ordinary_sections.add(person.section_id)
            if len(ordinary_sections) > 1:
                errors["incoming_supervisor"] = "Both supervisors must belong to the same treatment plant section."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        user_ids = sorted({
            user_id
            for user_id in (self.outgoing_supervisor_id, self.incoming_supervisor_id)
            if user_id
        })
        with transaction.atomic():
            list(User.objects.select_for_update().filter(pk__in=user_ids).order_by("pk"))
            self.validate_scope_integrity()
            if self.date:
                for user_id in user_ids:
                    if AnnualLeave.is_unavailable(user_id, self.date):
                        raise ValidationError("A staff member marked on annual leave or off duty cannot be assigned a shift handover.")
            super().save(*args, **kwargs)

    @classmethod
    def create_for_shift_bulk(cls, shift, date_, outgoing_supervisor=None, incoming_supervisor=None, notes=""):
        """
        Fully bulked handover creation: autopick supervisors and bulk-update all assignments.
        """
        assignments_qs = ShiftAssignment.objects.filter(
            shift=shift,
            date=date_,
            is_deleted=False
        ).select_related('staff', 'supervisor')

        assignments = list(assignments_qs)
        assignment_sections = {assignment.staff.section_id for assignment in assignments}
        if len(assignment_sections) > 1:
            raise ValidationError("A shift handover cannot combine assignments from multiple sections.")
        assignment_section_id = next(iter(assignment_sections), None)

        # Compile pending work
        pending_summary = "\n".join(
            f"{a.staff.get_full_name()}: {getattr(a, 'remarks', 'No remarks')}"
            for a in assignments
        ) or "No pending work."

        # Auto-pick outgoing supervisor if missing
        if not outgoing_supervisor and assignments:
            section_name = assignments[0].staff.section
            eligible = ShiftSupervisor.objects.filter(
                treatment_plant=shift.treatment_plant,
                user__section=section_name,
                is_active=True,
            ).order_by('user__id')
            eligible = eligible.exclude(user_id__in=AnnualLeave.objects.filter(
                status__in=AnnualLeave.UNAVAILABLE_STATUSES,
                start_date__lte=date_,
                end_date__gte=date_,
            ).values("staff_id"))
            outgoing_supervisor = eligible.first().user if eligible.exists() else None

        # Default incoming supervisor
        if not incoming_supervisor:
            incoming_supervisor = outgoing_supervisor

        supervisor_sections = {
            person.section_id
            for person in (outgoing_supervisor, incoming_supervisor)
            if person and not person.is_superuser and person.section_id
        }
        if assignment_section_id and any(section_id != assignment_section_id for section_id in supervisor_sections):
            raise ValidationError("The handover supervisors must belong to the assigned staff section.")
        if len(supervisor_sections) > 1:
            raise ValidationError("Both handover supervisors must belong to the same section.")

        with transaction.atomic():
            # Create handover
            handover = cls.objects.create(
                shift=shift,
                date=date_,
                outgoing_supervisor=outgoing_supervisor,
                incoming_supervisor=incoming_supervisor,
                handover_notes=notes,
                pending_work=pending_summary,
                is_taken_over=bool(incoming_supervisor)
            )

            # Bulk update all assignments at once
            if assignments:
                assignments_qs.update(handover_to=incoming_supervisor)

                # Bulk audit creation
                audit_entries = [
                    AuditMixin(
                        user=a.staff,
                        action="update",
                        instance=a,
                        before={"handover_to": getattr(a, 'supervisor_id', None)},
                        after={"handover_to": incoming_supervisor.id if incoming_supervisor else None}
                    )
                    for a in assignments
                ]
                AuditMixin.objects.bulk_create(audit_entries)

            # Audit the handover creation
            AuditMixin.create_audit(
                user=outgoing_supervisor,
                action="create",
                instance=handover,
                after={"pending_work": pending_summary}
            )

        return handover

class Apparatus(AuditMixin, models.Model):
    name = models.CharField(max_length=100, unique=True)
    equipment = models.OneToOneField(Equipment, on_delete=models.CASCADE, related_name='apparatus')
    description = models.TextField(blank=True, null=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.equipment_id and (
            not self.equipment.section_id
            or not self.equipment.section.treatment_plant_id
        ):
            raise ValidationError({"equipment": "Apparatus equipment must belong to a section and treatment plant."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

APPARATUS_STATUS_CHOICES = [
    ('working', 'Working'),
    ('needs_repair', 'Needs Repair'),
    ('out_of_service', 'Out of Service'),
]
class ApparatusCondition(AuditMixin, models.Model):
    handover = models.ForeignKey(
        ShiftHandover,
        on_delete=models.CASCADE,
        related_name='apparatus_conditions'
    )
    apparatus = models.ForeignKey(
        Apparatus,
        on_delete=models.CASCADE,
        related_name='conditions'
    )
    status = models.CharField(
        max_length=20,
        choices=APPARATUS_STATUS_CHOICES,
        default='working'
    )
    remarks = models.TextField(blank=True, null=True)

    class Meta:
        unique_together = ('handover', 'apparatus')
        ordering = ['handover', 'apparatus']

    def __str__(self):
        return f"{self.apparatus.name}: {self.status}"

    def clean(self):
        super().clean()
        if not self.handover_id or not self.apparatus_id:
            return
        equipment = self.apparatus.equipment
        section = equipment.section
        if (
            not section.treatment_plant_id
            or section.treatment_plant_id != self.handover.shift.treatment_plant_id
            or not ShiftSectionRequirement.objects.filter(
                shift_id=self.handover.shift_id,
                section_id=section.pk,
            ).exists()
        ):
            raise ValidationError({"apparatus": "The apparatus and handover must belong to the same treatment plant and section."})
        supervisor_sections = {
            person.section_id
            for person in (self.handover.outgoing_supervisor, self.handover.incoming_supervisor)
            if person and not person.is_superuser and person.section_id
        }
        if supervisor_sections and section.pk not in supervisor_sections:
            raise ValidationError({"apparatus": "The apparatus must belong to the handover supervisors' section."})

    def save(self, *args, **kwargs):
        self.full_clean()
        with transaction.atomic():
            # Capture previous status for audit
            previous_status = None
            if self.pk:
                prev = ApparatusCondition.objects.filter(pk=self.pk).first()
                previous_status = prev.status if prev else None

            super().save(*args, **kwargs)

            # Update linked equipment status if it changed
            if self.apparatus and getattr(self.apparatus, 'equipment', None):
                equipment = self.apparatus.equipment
                new_status = get_status_by_name(self.status)
                old_equipment_status = equipment.status

                if new_status and (not old_equipment_status or old_equipment_status.name.lower() != new_status.name.lower()):
                    equipment.status = new_status
                    equipment.save(update_fields=['status'])

                    # Audit the equipment status change
                    AuditMixin.create_audit(
                        user=None,
                        action='update',
                        instance=equipment,
                        before={'status': old_equipment_status.name if old_equipment_status else None},
                        after={'status': new_status.name}
                    )

            AuditMixin.create_audit(
                user=None,
                action='update' if previous_status else 'create',
                instance=self,
                before={'status': previous_status} if previous_status else None,
                after={'status': self.status}
            )
