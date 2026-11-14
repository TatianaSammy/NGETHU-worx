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
from core.plant_section_scope import section_belongs_to_plant
from t.models import TreatmentPlant
from inventory.models import (Equipment, Status)
from django.conf import settings
from datetime import datetime, date, time, timedelta
from django.core.exceptions import ValidationError
from django.db import models
from django.contrib.postgres.fields import DateTimeRangeField

INCHARGE_GROUPS = (
    "CHEMIST OFFICER",
    "QUALITY ASSURANCE MANAGER",
    "QUALITY ASSURANCE CO-ORDINATOR",
    "PRODUCTION MANAGER",
    "TREATMENT WORKS CO-ORDINATOR",
    "TREATMENT WORKS OFFICER",
    "MAINTENANCE SUPERVISOR",
    "STORES SUPERVISOR",
    "WSP CO-ORDINATOR",
)


def is_user_incharge(user, section_id=None, section=None) -> bool:
    """Returns True if the user is a section incharge / overall supervisor."""
    if not user:
        return False
    sec_id = section_id or (section.pk if hasattr(section, "pk") else section)
    if sec_id and getattr(user, "section_id", None) != sec_id:
        return False
    groups = set(user.groups.values_list("name", flat=True))
    return bool(groups.intersection(INCHARGE_GROUPS))


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

    def get_window(self, assignment_date: date) -> tuple[datetime, datetime]:
        """Returns the start and end datetime for this shift on a specific date."""
        start_dt = datetime.combine(assignment_date, self.start_time)
        end_dt = datetime.combine(assignment_date, self.end_time)
        if self.is_overnight:
            end_dt += timedelta(days=1)
        return start_dt, end_dt

    def overlaps_with(self, assignment_date: date, other_shift, other_date: date) -> bool:
        """Determines if this shift overlaps in time with another shift on specified dates."""
        start1, end1 = self.get_window(assignment_date)
        start2, end2 = other_shift.get_window(other_date)
        return max(start1, start2) < min(end1, end2)

    def check_staff_conflict(
        self,
        staff_id: int,
        assignment_date: date,
        exclude_assignment_id: Optional[int] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        Gates shift logic directly from models.py:
        1. Total shift hours in a single day cannot exceed 8 hours.
        2. A staff member can only work in one shift per day and cannot work across shifts on that date.
        3. If staff completed an overnight shift yesterday extending into today, they cannot work today.
        4. If this shift is overnight, it extends into tomorrow; staff cannot work another shift tomorrow.
        5. Adjacent shift window time overlap check.
        """
        shift_duration = self.duration
        if shift_duration and (shift_duration.total_seconds() / 3600.0) > 8.0:
            return True, (
                f"A user can only work for 8 hours in a single day. "
                f"{self.name} duration is {shift_duration.total_seconds() / 3600.0:.1f} hours."
            )

        # 1. Staff cannot work across shifts on the same day (must only work in one shift)
        same_day_assignments = ShiftAssignment.objects.filter(
            staff_id=staff_id,
            date=assignment_date,
            is_deleted=False,
            shift__is_deleted=False,
        )
        if exclude_assignment_id:
            same_day_assignments = same_day_assignments.exclude(pk=exclude_assignment_id)
        existing_same_day = same_day_assignments.select_related("shift").first()
        if existing_same_day:
            return True, (
                f"A staff member can appear only once on the roster per day and cannot work across shifts. "
                f"Staff is already assigned to {existing_same_day.shift.name} on {assignment_date:%Y-%m-%d}."
            )

        same_day_supervisory_duties = ShiftAssignment.objects.filter(
            date=assignment_date,
            is_deleted=False,
            shift__is_deleted=False,
        ).filter(
            models.Q(temp_supervisor_id=staff_id)
            | models.Q(handover_to_id=staff_id)
        ).exclude(shift_id=self.pk)
        if exclude_assignment_id:
            same_day_supervisory_duties = same_day_supervisory_duties.exclude(pk=exclude_assignment_id)
        existing_supervisory_duty = same_day_supervisory_duties.select_related("shift").first()
        if existing_supervisory_duty:
            return True, (
                f"A staff member can work only one shift per day. They already supervise "
                f"{existing_supervisory_duty.shift.name} on {assignment_date:%Y-%m-%d}."
            )

        weekly_supervisor = ShiftSupervisor.objects.filter(
            user_id=staff_id,
            week_start__isnull=False,
            week_start__lte=assignment_date,
            week_end__gte=assignment_date,
        ).exclude(shift_id=self.pk).select_related("shift").first()
        if weekly_supervisor:
            return True, (
                f"A weekly supervisor can work only one shift per day. "
                f"Staff is designated for {weekly_supervisor.shift.name} on {assignment_date:%Y-%m-%d}."
            )
        if self.is_overnight and self.end_time > time(0, 0):
            next_day_supervisor = ShiftSupervisor.objects.filter(
                user_id=staff_id,
                week_start__isnull=False,
                week_start__lte=assignment_date + timedelta(days=1),
                week_end__gte=assignment_date + timedelta(days=1),
            ).exclude(shift_id=self.pk).select_related("shift").first()
            if next_day_supervisor:
                return True, (
                    f"This overnight shift overlaps the user's weekly supervisor duty for "
                    f"{next_day_supervisor.shift.name} on {(assignment_date + timedelta(days=1)):%Y-%m-%d}."
                )

        # 2. Check if staff worked an overnight shift yesterday extending into today
        yesterday = assignment_date - timedelta(days=1)
        yesterday_assignments = ShiftAssignment.objects.filter(
            date=yesterday,
            is_deleted=False,
            shift__is_deleted=False,
        ).filter(
            models.Q(staff_id=staff_id)
            | models.Q(temp_supervisor_id=staff_id)
            | models.Q(handover_to_id=staff_id)
        )
        if exclude_assignment_id:
            yesterday_assignments = yesterday_assignments.exclude(pk=exclude_assignment_id)
        for prev in yesterday_assignments.select_related("shift"):
            if prev.shift.is_overnight and prev.shift.end_time > time(0, 0):
                return True, (
                    f"A user can only work for 8 hours in a single day and cannot work across shifts. "
                    f"Staff already completed an overnight shift ({prev.shift.name}) extending into {assignment_date:%Y-%m-%d}."
                )

        # 3. If current shift is overnight, it extends into tomorrow; staff cannot work another shift tomorrow
        if self.is_overnight and self.end_time > time(0, 0):
            tomorrow = assignment_date + timedelta(days=1)
            tomorrow_assignments = ShiftAssignment.objects.filter(
                date=tomorrow,
                is_deleted=False,
                shift__is_deleted=False,
            ).filter(
                models.Q(staff_id=staff_id)
                | models.Q(temp_supervisor_id=staff_id)
                | models.Q(handover_to_id=staff_id)
            )
            if exclude_assignment_id:
                tomorrow_assignments = tomorrow_assignments.exclude(pk=exclude_assignment_id)
            first_tomorrow = tomorrow_assignments.select_related("shift").first()
            if first_tomorrow:
                return True, (
                    f"A user can only work for 8 hours in a single day. "
                    f"This overnight shift extends into {tomorrow:%Y-%m-%d}, where staff is already assigned to {first_tomorrow.shift.name}."
                )

        # 4. Adjacent window overlap
        adjacent_assignments = ShiftAssignment.objects.filter(
            date__range=(assignment_date - timedelta(days=1), assignment_date + timedelta(days=1)),
            is_deleted=False,
            shift__is_deleted=False,
        ).filter(
            models.Q(staff_id=staff_id)
            | models.Q(temp_supervisor_id=staff_id)
            | models.Q(handover_to_id=staff_id)
        )
        if exclude_assignment_id:
            adjacent_assignments = adjacent_assignments.exclude(pk=exclude_assignment_id)
        for other in adjacent_assignments.select_related("shift"):
            if self.overlaps_with(assignment_date, other.shift, other.date):
                return True, f"Shift overlaps in time with {other.shift.name} on {other.date:%Y-%m-%d}."

        return False, None

    def is_staff_eligible(
        self,
        staff,
        assignment_date: date,
        section=None,
        exclude_assignment_id: Optional[int] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        Validates if staff member is eligible to be assigned to this shift on assignment_date.
        Gated by: active status, plant scope, section requirement, leave/unavailability, conflict checks.
        """
        if not staff.is_active:
            return False, f"Staff member '{staff.get_username()}' is inactive."

        if is_user_incharge(staff, section=section or staff.section_id):
            return False, f"Staff member '{staff.get_username()}' is the section incharge and is not assigned to shifts."

        if not staff.is_superuser:
            if staff.assigned_treatment_plant_id != self.treatment_plant_id:
                return False, "The staff member and shift must belong to the same treatment plant."

            target_section_id = section.pk if hasattr(section, "pk") else (section or staff.section_id)
            if not target_section_id or staff.section_id != target_section_id:
                return False, "The staff member does not belong to the target section."

            req_exists = self.section_requirements.filter(
                section_id=target_section_id,
                section__is_active=True,
            ).exists()
            if not req_exists:
                return False, "The shift must be configured for the staff member's assigned section."

        if AnnualLeave.is_unavailable(staff.pk, assignment_date):
            return False, "This staff member is marked on annual leave or off duty for the selected date."

        has_conflict, reason = self.check_staff_conflict(
            staff.pk, assignment_date, exclude_assignment_id=exclude_assignment_id
        )
        if has_conflict:
            return False, reason

        return True, None

    def get_available_staff(
        self,
        assignment_date: date,
        section=None,
        exclude_assignment_id: Optional[int] = None,
    ):
        """
        Returns a list of CustomUser instances available to be assigned to this shift
        on assignment_date within the specified section.
        """
        User = get_user_model()
        qs = User.objects.filter(
            is_active=True,
            assigned_treatment_plant_id=self.treatment_plant_id,
        )
        target_section_id = section.pk if hasattr(section, "pk") else section
        if target_section_id:
            qs = qs.filter(section_id=target_section_id)

        candidates = []
        for staff in qs.order_by("last_name", "first_name", "username"):
            is_eligible, _ = self.is_staff_eligible(
                staff, assignment_date, section=section, exclude_assignment_id=exclude_assignment_id
            )
            if is_eligible:
                candidates.append(staff)
        return candidates

    def can_accept_assignment(
        self,
        section,
        assignment_date: date,
        day_type: Optional[str] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        Checks whether this shift has capacity for another assignment in the given section.
        Gated by ShiftSectionRequirement.maximum_allowed (or minimum staffing on weekends/holidays).
        """
        section_id = section.pk if hasattr(section, "pk") else section
        requirement = self.section_requirements.filter(
            section_id=section_id,
            section__is_active=True,
        ).first()
        if not requirement:
            return False, "This shift is not configured for the selected section."

        current_count = ShiftAssignment.objects.filter(
            shift=self,
            date=assignment_date,
            staff__section_id=section_id,
            is_deleted=False,
        ).count()

        # Rule: Keep the minimum number of staff on public holidays and on 'saturday and sunday'
        effective_day_type = day_type
        if not effective_day_type:
            effective_day_type = ShiftAssignment.DAYTYPE_WEEKEND if assignment_date.weekday() >= 5 else ShiftAssignment.DAYTYPE_WEEKDAY

        if effective_day_type in (ShiftAssignment.DAYTYPE_WEEKEND, ShiftAssignment.DAYTYPE_HOLIDAY):
            max_weekend = requirement.get_required_staff(effective_day_type)
            if current_count >= max_weekend:
                return False, (
                    f"Staff limit for {effective_day_type} ({max_weekend}) reached "
                    f"for {self.name} in {requirement.section.name}. Weekends and holidays maintain minimum staffing."
                )

        if current_count >= requirement.maximum_allowed:
            return False, (
                f"Maximum allowed staff limit ({requirement.maximum_allowed}) reached "
                f"for {self.name} in {requirement.section.name}."
            )
        return True, None


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
        if day_type in (
            ShiftAssignment.DAYTYPE_WEEKEND,
            ShiftAssignment.DAYTYPE_HOLIDAY,
        ):
            # Rule: Keep the minimum number of staff on public holidays and on 'saturday and sunday'
            return self.minimum_required if self.minimum_required else (
                self.weekend_required if day_type == ShiftAssignment.DAYTYPE_WEEKEND else self.holiday_required
            )

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
        day_type=None,
    ):
        if day_type in (
            ShiftAssignment.DAYTYPE_WEEKEND,
            ShiftAssignment.DAYTYPE_HOLIDAY,
        ):
            return assigned_count > self.get_required_staff(day_type)

        return (
            assigned_count >
            self.maximum_allowed
        )

    def has_optimal_staff(
        self,
        assigned_count,
        day_type=None,
    ):
        if day_type in (
            ShiftAssignment.DAYTYPE_WEEKEND,
            ShiftAssignment.DAYTYPE_HOLIDAY,
        ):
            return assigned_count == self.get_required_staff(day_type)

        return (
            self.minimum_required
            <= assigned_count
            <= self.maximum_allowed
        )

    def get_staffing_status(self, assigned_count: int, day_type: str) -> str:
        """Returns staffing status: empty, understaffed, optimal, or overstaffed."""
        if assigned_count == 0:
            return "empty"
        if self.is_overstaffed(assigned_count, day_type=day_type):
            return "overstaffed"
        if self.is_understaffed(assigned_count, day_type):
            return "understaffed"
        return "optimal"

    def clean(self):
        super().clean()
        if self.shift_id and self.section_id:
            shift_plant_id = self.shift.treatment_plant_id
            if not section_belongs_to_plant(self.section, shift_plant_id):
                raise ValidationError({
                    "section": "The section must be available to the shift's treatment plant."
                })
        # Keep the minimum number of staff on public holidays and on 'saturday and sunday'
        if self.minimum_required:
            self.weekend_required = self.minimum_required
            self.holiday_required = self.minimum_required

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

class ShiftSupervisor(AuditMixin, models.Model):
    treatment_plant: TreatmentPlant = models.ForeignKey(
        TreatmentPlant,
        on_delete=models.CASCADE,
        related_name="supervisors",
        help_text="Treatment plant served by this shift supervisor designation."
    )
    shift = models.ForeignKey(
        "Shift",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="supervisor_designations",
        help_text="Shift covered by this supervisor designation.",
    )
    section = models.ForeignKey(
        "core.Section",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="shift_supervisors",
        help_text="Section covered on the selected shift.",
    )
    user: settings.AUTH_USER_MODEL = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="shift_supervisor",
        help_text="User designated as shift supervisor."
    )
    is_active: bool = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Only one active supervisor is allowed for each shift and section.",
    )
    week_start = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Week start for weekly roster assignment. Null for standing designations.",
    )
    week_end = models.DateField(
        null=True,
        blank=True,
        help_text="Week end for weekly roster assignment.",
    )
    designated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_shift_supervisor_weeks",
        help_text="Admin who designated this weekly roster assignment.",
    )

    class Meta:
        verbose_name = "Shift Supervisor"
        verbose_name_plural = "Shift Supervisors"
        ordering = ("week_start", "id")
        constraints = [
            # --- Standing designation constraints ---
            models.UniqueConstraint(
                fields=("shift", "section"),
                condition=models.Q(is_active=True, week_start__isnull=True, shift__isnull=False),
                name="sc_one_active_supervisor_per_shift_section",
            ),
            models.UniqueConstraint(
                fields=("user",),
                condition=models.Q(is_active=True, week_start__isnull=True),
                name="sc_active_sup_one_shift",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(week_start__isnull=False)  # weekly assignments bypass this check
                    | models.Q(is_active=False)
                    | models.Q(section__isnull=False)
                ),
                name="sc_active_supervisor_requires_shift_section",
            ),
            # --- Weekly assignment constraints ---
            models.UniqueConstraint(
                fields=("shift", "section", "week_start"),
                condition=models.Q(week_start__isnull=False),
                name="sc_shiftsup_weekly_unique_shift_section_week",
            ),
            models.UniqueConstraint(
                fields=("user", "week_start"),
                condition=models.Q(week_start__isnull=False),
                name="sc_shiftsup_weekly_one_user_per_week",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(week_start__isnull=True)
                    | models.Q(week_end__isnull=False, week_end__gte=models.F("week_start"))
                ),
                name="sc_shiftsup_weekly_valid_dates",
            ),
        ]
        indexes = [
            models.Index(fields=["treatment_plant", "is_active"], name="sc_shiftsup_plant_act_idx"),
            models.Index(fields=["treatment_plant", "section", "is_active"], name="sc_shiftsup_sec_act_idx"),
            models.Index(fields=["shift", "section", "is_active"], name="sc_shiftsup_shift_sec_act"),
            models.Index(
                fields=["treatment_plant", "week_start", "shift", "section"],
                name="sc_shiftsup_weekly_scope_idx",
            ),
            models.Index(
                fields=["user", "week_start", "week_end"],
                name="sc_shiftsup_weekly_user_idx",
            ),
        ]

    @property
    def is_weekly_assignment(self) -> bool:
        """True when this record is a weekly roster designation (not a standing designation)."""
        return self.week_start is not None

    def __str__(self) -> str:
        sec_label = f" [{self.section.name}]" if self.section else ""
        if self.is_weekly_assignment:
            shift_label = f" · {self.shift.name}" if self.shift_id else ""
            week_label = f" [{self.week_start}–{self.week_end}]"
            user_name = self.user.get_full_name() or getattr(self.user, "username", "Unknown")
            plant_name = getattr(self.treatment_plant, "name", "Unknown Plant")
            return f"{user_name}{shift_label}{sec_label}{week_label} @ {plant_name}"
        shift_label = f" · {self.shift.name}" if self.shift_id else " · legacy scope"
        user_name = self.user.get_full_name() or getattr(self.user, "username", "Unknown")
        plant_name = getattr(self.treatment_plant, "name", "Unknown Plant")
        return f"{user_name}{shift_label}{sec_label} @ {plant_name}"

    def clean(self):
        super().clean()
        errors = {}

        if self.is_weekly_assignment:
            if not self.shift_id:
                errors["shift"] = "A weekly supervisor designation requires a shift."
            if not self.section_id:
                errors["section"] = "A weekly supervisor designation requires a section."
        else:
            if self.is_active and not self.section_id:
                errors["section"] = "An active supervisor must be assigned to a section."

        if not self.user_id:
            if errors:
                raise ValidationError(errors)
            return

        if not self.user.is_active:
            errors["user"] = f"Inactive user '{self.user.username}' cannot be designated as a shift supervisor."

        is_superuser = bool(self.user.is_superuser)
        if self.section_id:
            if not section_belongs_to_plant(self.section, self.treatment_plant_id):
                errors["section"] = "The section is not available to the designated treatment plant."
            if hasattr(self.section, "is_active") and not self.section.is_active:
                errors["section"] = f"Section '{self.section.name}' is inactive and cannot have supervisors assigned."
        elif not is_superuser:
            errors["section"] = "A section is required for non-superuser shift supervisors."

        if self.shift_id:
            if self.shift.is_deleted and self.is_active:
                errors["shift"] = "An active supervisor cannot be assigned to an inactive shift."
            if self.shift.treatment_plant_id != self.treatment_plant_id:
                errors["shift"] = "The shift must belong to the selected treatment plant."
            if self.section_id and not ShiftSectionRequirement.objects.filter(
                shift_id=self.shift_id,
                section_id=self.section_id,
            ).exists():
                errors["section"] = "The selected section is not configured for this shift."

        if self.is_active and self.shift_id and self.section_id and not self.is_weekly_assignment and ShiftSupervisor.objects.filter(
            shift_id=self.shift_id,
            section_id=self.section_id,
            is_active=True,
            week_start__isnull=True,
        ).exclude(pk=self.pk).exists():
            errors["is_active"] = "This shift and section already have an active standing supervisor."

        if self.is_active and self.user_id and not self.is_weekly_assignment:
            if ShiftSupervisor.objects.filter(
                user_id=self.user_id,
                is_active=True,
                week_start__isnull=True,
            ).exclude(pk=self.pk).exists():
                errors["user"] = "A supervisor can have only one active standing designation at a time."
            if self.shift_id and ShiftSupervisor.objects.filter(
                user_id=self.user_id,
                week_end__gte=timezone.localdate(),
                week_start__isnull=False,
            ).exclude(shift_id=self.shift_id).exists():
                errors["user"] = "This user is assigned to supervise another shift during a current or future roster week."

        # --- Weekly assignment validation ---
        if self.is_weekly_assignment:
            if self.shift_id and self.treatment_plant_id != self.shift.treatment_plant_id:
                errors["treatment_plant"] = "The supervisor week, shift, and treatment plant must match."
            if self.shift_id and self.section_id:
                if self.shift.is_deleted:
                    errors["shift"] = "An inactive shift cannot have a weekly supervisor."
                if not section_belongs_to_plant(self.section, self.treatment_plant_id):
                    errors["section"] = "The section is not available to the selected treatment plant."
                if not ShiftSectionRequirement.objects.filter(
                    shift_id=self.shift_id,
                    section_id=self.section_id,
                    section__is_active=True,
                ).exists():
                    errors["section"] = "The selected section is not configured for this shift."
            if self.week_start and self.week_end:
                valid_weeks = MonthlyDutyRoster.get_weeks_in_month(self.week_start.year, self.week_start.month)
                if (self.week_start, self.week_end) not in valid_weeks:
                    errors["week_start"] = "Choose one complete roster week defined by the monthly duty roster."

            if self.user_id:
                if not self.user.is_active:
                    errors["user"] = "An inactive user cannot supervise a duty week."
                elif not self.user.is_superuser and (
                    self.user.assigned_treatment_plant_id != self.treatment_plant_id
                    or self.user.section_id != self.section_id
                ):
                    errors["user"] = "The weekly supervisor must belong to this treatment plant and section."

                if self.shift_id and self.section_id and not ShiftSupervisor.objects.filter(
                    treatment_plant_id=self.treatment_plant_id,
                    section_id=self.section_id,
                    user_id=self.user_id,
                    is_active=True,
                    week_start__isnull=True,
                    user__is_active=True,
                    section__is_active=True,
                ).filter(
                    models.Q(shift_id=self.shift_id) | models.Q(shift__isnull=True) | models.Q(shift__treatment_plant_id=self.treatment_plant_id)
                ).exists():
                    errors["user"] = "The weekly supervisor must be an active supervisor for this plant and section."

                if self.week_start and self.week_end:
                    has_valid_shift_assignment = self.shift_id and self.section_id and _has_valid_shift_assignment_for_week(
                        user_id=self.user_id,
                        shift_id=self.shift_id,
                        section_id=self.section_id,
                        week_start=self.week_start,
                        week_end=self.week_end,
                    )
                    if self.shift_id and self.section_id and not has_valid_shift_assignment:
                        errors["user"] = "The weekly supervisor must have a roster assignment on this shift during the selected week."

                    if AnnualLeave.objects.filter(
                        staff_id=self.user_id,
                        treatment_plant_id=self.treatment_plant_id,
                        status__in=AnnualLeave.UNAVAILABLE_STATUSES,
                        start_date__lte=self.week_end,
                        end_date__gte=self.week_start,
                    ).exists():
                        errors["user"] = "A user on annual leave or off duty cannot supervise this week."

                    if ShiftSupervisor.objects.filter(
                        user_id=self.user_id,
                        week_start=self.week_start,
                        week_start__isnull=False,
                    ).exclude(pk=self.pk).exists():
                        errors["user"] = "A user can hold only one weekly supervisor designation per roster week."

                    if ShiftSupervisor.objects.filter(
                        user_id=self.user_id,
                        week_start__lte=self.week_end,
                        week_end__gte=self.week_start,
                        week_start__isnull=False,
                    ).exclude(shift_id=self.shift_id).exclude(pk=self.pk).exists():
                        errors["user"] = "A supervisor can cover only one designated shift during the same week."

        if not is_superuser and not self.is_weekly_assignment:
            if self.user.assigned_treatment_plant_id != self.treatment_plant_id:
                errors["treatment_plant"] = "The supervisor and treatment plant must match the user's assigned plant."
            if not self.user.section_id or self.user.section_id != self.section_id:
                errors["section"] = "The supervisor section must match the user's assigned section."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.is_weekly_assignment:
            with transaction.atomic():
                user_ids = {self.user_id} if self.user_id else set()
                if self.pk:
                    previous_user_id = type(self).objects.filter(pk=self.pk).values_list("user_id", flat=True).first()
                    if previous_user_id:
                        user_ids.add(previous_user_id)
                list(User.objects.select_for_update().filter(pk__in=user_ids).order_by("pk"))
                if self.shift_id and self.section_id:
                    list(ShiftSectionRequirement.objects.order_by().select_for_update(of=("self",)).filter(
                        shift_id=self.shift_id,
                        section_id=self.section_id,
                        section__is_active=True,
                    ).values_list("pk", flat=True))
                    list(ShiftSupervisor.objects.order_by().select_for_update(of=("self",)).filter(
                        treatment_plant_id=self.treatment_plant_id,
                        section_id=self.section_id,
                        is_active=True,
                        week_start__isnull=True,
                    ).values_list("pk", flat=True))
                    roster_assignments = list(ShiftAssignment.objects.order_by().select_for_update(of=("self",)).filter(
                        shift_id=self.shift_id,
                        staff__section_id=self.section_id,
                        date__range=(self.week_start, self.week_end),
                        is_deleted=False,
                    ))
                else:
                    roster_assignments = []
                self.full_clean()
                super().save(*args, **kwargs)
                if self.shift_id and self.section_id:
                    ShiftAssignment.objects.filter(
                        shift_id=self.shift_id,
                        staff__section_id=self.section_id,
                        date__range=(self.week_start, self.week_end),
                        is_deleted=False,
                    ).exclude(supervisor_id=self.user_id).update(supervisor=self.user)
        else:
            self.full_clean()
            super().save(*args, **kwargs)

    @classmethod
    def get_active_supervisor(
        cls,
        treatment_plant_id: int,
        section_id: Optional[int] = None,
        shift_id: Optional[int] = None,
        assignment_date: Optional[date] = None,
    ):
        """Returns the active supervisor for a plant, shift, and section scope."""
        if assignment_date and shift_id:
            week_start, week_end = MonthlyDutyRoster.get_week_bounds(assignment_date)
            weekly_rows = cls.objects.filter(
                treatment_plant_id=treatment_plant_id,
                shift_id=shift_id,
                week_start=week_start,
                week_end=week_end,
                week_start__isnull=False,
                shift__is_deleted=False,
                section__is_active=True,
                shift__section_requirements__section_id=models.F("section_id"),
                shift__section_requirements__section__is_active=True,
            )
            if section_id:
                weekly_rows = weekly_rows.filter(section_id=section_id)
            else:
                user_ids = set(weekly_rows.values_list("user_id", flat=True))
                if len(user_ids) > 1:
                    return None
            weekly = weekly_rows.select_related("user").order_by("pk").first()
            if weekly:
                is_shift_supervisor = cls.objects.filter(
                    treatment_plant_id=treatment_plant_id,
                    section_id=weekly.section_id,
                    user_id=weekly.user_id,
                    is_active=True,
                    week_start__isnull=True,
                    user__is_active=True,
                    section__is_active=True,
                ).filter(
                    models.Q(shift_id=shift_id) | models.Q(shift__isnull=True) | models.Q(shift__treatment_plant_id=treatment_plant_id)
                ).exists()
                has_week_assignment = _has_valid_shift_assignment_for_week(
                    user_id=weekly.user_id,
                    shift_id=shift_id,
                    section_id=weekly.section_id,
                    week_start=week_start,
                    week_end=week_end,
                )
                if is_shift_supervisor and has_week_assignment and not AnnualLeave.is_unavailable(weekly.user_id, assignment_date):
                    return weekly.user
                return None

        valid_user_scope = models.Q(user__is_superuser=True)
        if section_id:
            valid_user_scope |= models.Q(
                user__assigned_treatment_plant_id=treatment_plant_id,
                user__section_id=section_id,
            )
        qs = cls.objects.filter(
            treatment_plant_id=treatment_plant_id,
            is_active=True,
            week_start__isnull=True,
            user__is_active=True,
        ).filter(
            models.Q(shift__isnull=True)
            | models.Q(
                shift__is_deleted=False,
                shift__section_requirements__section_id=models.F("section_id"),
                shift__section_requirements__section__is_active=True,
            )
        ).filter(valid_user_scope).filter(
            section__is_active=True
        ).filter(
            models.Q(section__treatment_plant_id__isnull=True)
            | models.Q(section__treatment_plant_id=models.F("treatment_plant_id"))
        )
        if section_id:
            qs = qs.filter(section_id=section_id)
        if shift_id:
            qs = qs.filter(models.Q(shift_id=shift_id) | models.Q(shift__isnull=True))
            qs = qs.order_by(models.F("shift_id").desc(nulls_last=True), "pk")
        elif qs.values("user_id").distinct().count() != 1:
            return None
        supervisor = qs.select_related("user").first()
        if supervisor and assignment_date and shift_id and not _has_valid_shift_assignment_for_week(
            user_id=supervisor.user_id,
            shift_id=shift_id,
            section_id=supervisor.section_id,
            week_start=week_start,
            week_end=week_end,
        ):
            return None
        return supervisor.user if supervisor else None

    @classmethod
    def get_supervisors_map(cls, treatment_plant_ids=None, section_ids=None, shift_ids=None):
        valid_user_scope = models.Q(user__is_superuser=True) | models.Q(
            section_id__isnull=False,
            user__assigned_treatment_plant_id=models.F("treatment_plant_id"),
            user__section_id=models.F("section_id"),
        )
        qs = cls.objects.filter(
            is_active=True,
            week_start__isnull=True,
            user__is_active=True,
            shift__is_deleted=False,
            shift__section_requirements__section_id=models.F("section_id"),
            shift__section_requirements__section__is_active=True,
        ).filter(valid_user_scope).filter(
            section__is_active=True
        ).filter(
            models.Q(section__treatment_plant_id__isnull=True)
            | models.Q(section__treatment_plant_id=models.F("treatment_plant_id"))
        ).select_related("user", "section", "treatment_plant").distinct()
        if treatment_plant_ids:
            qs = qs.filter(treatment_plant_id__in=treatment_plant_ids)
        if section_ids:
            qs = qs.filter(section_id__in=section_ids)
        if shift_ids:
            qs = qs.filter(shift_id__in=shift_ids)
        mapping = {}
        for item in qs.order_by("pk"):
            if shift_ids:
                mapping.setdefault((item.shift_id, item.section_id), item.user)
            else:
                mapping.setdefault((item.treatment_plant_id, item.section_id), item.user)
        return mapping



class AnnualLeave(AuditMixin, models.Model):
    STATUS_ANNUAL_LEAVE = "annual_leave"
    STATUS_APPROVED = "approved"
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
            if (
                assigned_plant_id != self.treatment_plant_id
                or not section_belongs_to_plant(self.staff.section, self.treatment_plant_id)
            ):
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

            if ShiftSupervisor.objects.filter(
                user_id=self.staff_id,
                week_start__isnull=False,
                week_start__lte=self.end_date,
                week_end__gte=self.start_date,
            ).exists():
                conflict_messages.append("Reassign weekly shift supervisor duties before setting this status.")

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
        ordering = ['date', 'shift']
        constraints = [
            models.UniqueConstraint(
                fields=['staff', 'date'],
                condition=models.Q(is_deleted=False),
                name='unique_active_staff_assignment_per_day',
            ),
        ]

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

        # Rule: No user should appear in multiple shifts in a single day. A user can only work for a single shift in any given day.
        if self.staff_id and self.date and period_changed:
            existing_same_day = ShiftAssignment.objects.filter(
                staff_id=self.staff_id,
                date=self.date,
                is_deleted=False,
            )
            if self.pk:
                existing_same_day = existing_same_day.exclude(pk=self.pk)
            conflict_assignment = existing_same_day.select_related("shift").first()
            if conflict_assignment:
                errors["staff"] = (
                    f"A user can only work for a single shift in any given day. "
                    f"Staff is already assigned to {conflict_assignment.shift.name} on {self.date:%Y-%m-%d}."
                )

        if self.staff_id and self.shift_id:
            if not self.staff.is_superuser and self.staff.assigned_treatment_plant_id != self.shift.treatment_plant_id:
                errors["staff"] = "The staff member and shift must belong to the same treatment plant."
            if not self.staff.is_superuser and self.staff.section_id and not section_belongs_to_plant(
                self.staff.section,
                self.shift.treatment_plant_id,
            ):
                errors["staff"] = "The staff member's section must belong to the shift's treatment plant."
            if not self.staff.section_id or not ShiftSectionRequirement.objects.filter(
                shift_id=self.shift_id,
                section_id=self.staff.section_id,
                section__is_active=True,
            ).exists():
                errors["staff"] = "The shift must have an active section requirement for the staff member's assigned section."

        if self.duty_roster_id and self.shift_id:
            if self.duty_roster.treatment_plant_id != self.shift.treatment_plant_id:
                errors["duty_roster"] = "The roster and shift must belong to the same treatment plant."
            if self.date and (self.date.month != self.duty_roster.month or self.date.year != self.duty_roster.year):
                errors["date"] = "The assignment date must fall within the roster month."

        assignment_section_id = getattr(self.staff, "section_id", None) if self.staff_id else None
        if self.pk and period_changed:
            previous = type(self).objects.filter(pk=self.pk).values(
                "staff_id",
                "staff__section_id",
                "shift_id",
                "date",
                "is_deleted",
            ).first()
            if previous and not previous["is_deleted"]:
                weekly_designations = ShiftSupervisor.objects.filter(
                    user_id=previous["staff_id"],
                    shift_id=previous["shift_id"],
                    section_id=previous["staff__section_id"],
                    week_start__isnull=False,
                    week_start__lte=previous["date"],
                    week_end__gte=previous["date"],
                )
                for designation in weekly_designations:
                    still_assigned_within_shift = (
                        not self.is_deleted
                        and self.staff_id == designation.user_id
                        and self.shift_id == designation.shift_id
                        and assignment_section_id == designation.section_id
                        and self.date
                        and designation.week_start <= self.date <= designation.week_end
                    )
                    replacement_exists = _has_valid_shift_assignment_for_week(
                        user_id=designation.user_id,
                        shift_id=designation.shift_id,
                        section_id=designation.section_id,
                        week_start=designation.week_start,
                        week_end=designation.week_end,
                        exclude_assignment_id=self.pk,
                    )
                    if not still_assigned_within_shift and not replacement_exists:
                        errors["staff"] = "Keep at least one valid roster assignment for this weekly supervisor on the designated shift and section."
        for field_name in ("supervisor", "temp_supervisor", "handover_to"):
            person = getattr(self, field_name, None)
            if not person:
                continue
            if not person.is_superuser:
                if self.shift_id and person.assigned_treatment_plant_id != self.shift.treatment_plant_id:
                    errors[field_name] = "The assigned supervisor must belong to the shift's treatment plant."
                if not assignment_section_id or person.section_id != assignment_section_id:
                    errors[field_name] = "The assigned supervisor must belong to the staff member's section."
            if self.shift_id and self.date and period_changed:
                if field_name in ("temp_supervisor", "handover_to"):
                    conflicting_duty = ShiftAssignment.objects.filter(
                        date=self.date,
                        is_deleted=False,
                    ).exclude(pk=self.pk).exclude(shift_id=self.shift_id).filter(
                        models.Q(staff_id=person.pk)
                        | models.Q(temp_supervisor_id=person.pk)
                        | models.Q(handover_to_id=person.pk)
                    ).exists()
                    if conflicting_duty:
                        errors[field_name] = "A temporary supervisor cannot work across multiple shifts on the same day."
                weekly_conflict = ShiftSupervisor.objects.filter(
                    user_id=person.pk,
                    week_start__isnull=False,
                    week_start__lte=self.date,
                    week_end__gte=self.date,
                ).exclude(shift_id=self.shift_id).exists()
                if weekly_conflict:
                    errors[field_name] = "A supervisor cannot work across multiple shifts on the same day."
            if field_name == "supervisor" and self.shift_id and self.date:
                week_start, week_end = MonthlyDutyRoster.get_week_bounds(self.date)
                static_designation = ShiftSupervisor.objects.filter(
                    treatment_plant_id=self.shift.treatment_plant_id,
                    section_id=assignment_section_id,
                    user_id=person.pk,
                    is_active=True,
                    user__is_active=True,
                    section__is_active=True,
                ).filter(
                    models.Q(shift_id=self.shift_id)
                    | models.Q(shift__isnull=True)
                    | models.Q(shift__treatment_plant_id=self.shift.treatment_plant_id)
                ).exists()
                weekly_designations = ShiftSupervisor.objects.filter(
                    shift_id=self.shift_id,
                    section_id=assignment_section_id,
                    week_start=week_start,
                    week_end=week_end,
                    week_start__isnull=False,
                )
                weekly_designation = weekly_designations.filter(user_id=person.pk).exists()
                is_anchor_assignment = (
                    not self.is_deleted
                    and self.staff_id == person.pk
                )
                has_other_week_assignment = _has_valid_shift_assignment_for_week(
                    user_id=person.pk,
                    shift_id=self.shift_id,
                    section_id=assignment_section_id,
                    week_start=week_start,
                    week_end=week_end,
                    exclude_assignment_id=self.pk,
                )
                if weekly_designations.exists():
                    if not weekly_designation or (not is_anchor_assignment and not has_other_week_assignment):
                        errors[field_name] = "The supervisor must be designated for this shift and section and have a valid ShiftAssignment on that shift during the roster week."
                else:
                    if not static_designation:
                        errors[field_name] = "The supervisor must be designated for this shift and section."

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

        # Shift logic gating: max 8 hours per day, single shift per day, overnight checks, adjacent window checks
        if self.shift_id and self.staff_id and self.date and period_changed:
            has_conflict, reason = self.shift.check_staff_conflict(
                self.staff_id, self.date, exclude_assignment_id=self.pk
            )
            if has_conflict:
                errors["shift"] = reason
        elif self.shift_id:
            shift_duration = getattr(self.shift, "duration", None)
            if shift_duration:
                shift_hours = shift_duration.total_seconds() / 3600.0
                if shift_hours > 8.0:
                    errors["shift"] = (
                        f"A user can only work for 8 hours in a single day. "
                        f"{self.shift.name} duration is {shift_hours:.1f} hours."
                    )

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
        if self.temp_supervisor:
            return self.temp_supervisor
        if self.shift_id and self.date and self.staff_id and self.staff.section_id:
            week_start, week_end = MonthlyDutyRoster.get_week_bounds(self.date)
            weekly = ShiftSupervisor.objects.filter(
                shift_id=self.shift_id,
                section_id=self.staff.section_id,
                week_start=week_start,
                week_end=week_end,
                week_start__isnull=False,
            ).select_related("user").first()
            if weekly:
                if weekly.user.is_active and not AnnualLeave.is_unavailable(weekly.user_id, self.date):
                    return weekly.user
                return None
        return self.supervisor

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
        with transaction.atomic():
            assignments = list(assignments_qs.select_for_update().select_related("staff", "handover_to"))
            audit_logs = []
            now = timezone.now()
            content_type_id = ContentType.objects.get_for_model(cls).id
            for assignment in assignments:
                old_val = assignment.handover_to_id
                assignment.handover_to = handover.incoming_supervisor
                assignment.save(update_fields=("handover_to",))
                audit_logs.append(AuditLog(
                    user=assignment.staff,
                    action='update',
                    content_type_id=content_type_id,
                    object_id=assignment.pk,
                    object_repr=str(assignment),
                    changes={'handover_to': {'before': old_val, 'after': handover.incoming_supervisor.pk}},
                    timestamp=now,
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

    @classmethod
    def determine_day_type(cls, target_date: date, holidays=None) -> str:
        """Authoritative calculation of day_type (holiday, weekend, weekday)."""
        if holidays and target_date in holidays:
            return cls.DAYTYPE_HOLIDAY
        if target_date.weekday() in (5, 6):
            return cls.DAYTYPE_WEEKEND
        return cls.DAYTYPE_WEEKDAY

    @classmethod
    def create_assignment(
        cls,
        *,
        staff,
        shift,
        date,
        duty_roster=None,
        day_type=None,
        supervisor=None,
        remarks="",
        created_by=None,
    ):
        """
        Creates a new ShiftAssignment directly gated by the sc domain model invariants.
        Enforces shift eligibility, capacity, single active supervisor, and generates audit logs.
        """
        can_accept, capacity_reason = shift.can_accept_assignment(staff.section_id, date, day_type=day_type)
        if not can_accept:
            raise ValidationError({"shift": capacity_reason})

        is_eligible, eligibility_reason = shift.is_staff_eligible(staff, date, section=staff.section_id)
        if not is_eligible:
            raise ValidationError({"staff": eligibility_reason})

        if duty_roster is None:
            duty_roster, _ = MonthlyDutyRoster.objects.get_or_create(
                treatment_plant_id=shift.treatment_plant_id,
                month=date.month,
                year=date.year,
            )
        if day_type is None:
            day_type = cls.determine_day_type(date)
        if supervisor is None:
            supervisor = ShiftSupervisor.get_active_supervisor(
                shift.treatment_plant_id,
                staff.section_id,
                shift_id=shift.pk,
                assignment_date=date,
            )
            if supervisor and AnnualLeave.is_unavailable(supervisor.pk, date):
                supervisor = None

        assignment = cls(
            staff=staff,
            shift=shift,
            date=date,
            day_type=day_type,
            duty_roster=duty_roster,
            supervisor=supervisor,
            remarks=remarks,
        )
        assignment.full_clean()
        assignment.save()

        if created_by:
            AuditMixin.create_audit(
                user=created_by,
                action="create",
                instance=assignment,
                after={
                    "staff": staff.pk,
                    "shift": shift.pk,
                    "date": date.isoformat(),
                    "day_type": day_type,
                    "supervisor": supervisor.pk if supervisor else None,
                }
            )
        return assignment

    def move_assignment(self, *, target_shift, target_date, day_type=None, moved_by=None, remarks=""):
        """
        Moves this assignment to target_shift and target_date, gated by sc domain model invariants.
        """
        old_shift = self.shift
        old_date = self.date
        old_day_type = self.day_type

        if target_shift.treatment_plant_id != self.shift.treatment_plant_id:
            raise ValidationError("Duty assignments cannot be moved between treatment plants.")

        can_accept, capacity_reason = target_shift.can_accept_assignment(
            self.staff.section_id, target_date, day_type=day_type
        )
        if not can_accept:
            raise ValidationError({"shift": capacity_reason})

        is_eligible, eligibility_reason = target_shift.is_staff_eligible(
            self.staff, target_date, section=self.staff.section_id, exclude_assignment_id=self.pk
        )
        if not is_eligible:
            raise ValidationError({"shift": eligibility_reason})

        # If this staff member was designated as weekly supervisor on old_shift,
        # and moving this assignment leaves them with no remaining assignments on old_shift during that week,
        # clean up the weekly designation so they don't block the move and don't leave an orphaned designation.
        if old_date and old_shift:
            week_start, week_end = MonthlyDutyRoster.get_week_bounds(old_date)
            other_on_old = type(self).objects.filter(
                staff_id=self.staff_id,
                shift_id=old_shift.pk,
                date__range=(week_start, week_end),
                is_deleted=False,
            ).exclude(pk=self.pk).exists()
            if not other_on_old:
                ShiftSupervisor.objects.filter(
                    user_id=self.staff_id,
                    shift_id=old_shift.pk,
                    week_start=week_start,
                    week_start__isnull=False,
                ).delete()
                fallback_sup = ShiftSupervisor.get_active_supervisor(
                    old_shift.treatment_plant_id,
                    self.staff.section_id,
                    shift_id=old_shift.pk,
                    assignment_date=old_date,
                )
                type(self).objects.filter(
                    shift=old_shift,
                    supervisor_id=self.staff_id,
                    date__range=(week_start, week_end),
                    is_deleted=False,
                ).exclude(pk=self.pk).update(supervisor=fallback_sup)

        target_roster, _ = MonthlyDutyRoster.objects.get_or_create(
            treatment_plant_id=target_shift.treatment_plant_id,
            month=target_date.month,
            year=target_date.year,
        )
        if day_type is None:
            day_type = self.determine_day_type(target_date)

        target_supervisor = ShiftSupervisor.get_active_supervisor(
            target_shift.treatment_plant_id,
            self.staff.section_id,
            shift_id=target_shift.pk,
            assignment_date=target_date,
        )
        if target_supervisor and AnnualLeave.is_unavailable(target_supervisor.pk, target_date):
            target_supervisor = None

        self.shift = target_shift
        self.date = target_date
        self.day_type = day_type
        self.duty_roster = target_roster
        self.supervisor = target_supervisor
        if remarks:
            self.remarks = f"{self.remarks}\n{remarks}".strip() if self.remarks else remarks

        self.full_clean()
        self.save()

        if moved_by:
            AuditMixin.create_audit(
                user=moved_by,
                action="update",
                instance=self,
                before={"shift": old_shift.pk, "date": old_date.isoformat(), "day_type": old_day_type},
                after={"shift": target_shift.pk, "date": target_date.isoformat(), "day_type": day_type},
            )
        return self


def _has_valid_shift_assignment_for_week(
    *,
    user_id,
    shift_id,
    section_id,
    week_start,
    week_end,
    exclude_assignment_id=None,
):
    assignments = ShiftAssignment.objects.filter(
        staff_id=user_id,
        shift_id=shift_id,
        staff__section_id=section_id,
        date__range=(week_start, week_end),
        is_deleted=False,
    ).select_related("shift")
    if exclude_assignment_id:
        assignments = assignments.exclude(pk=exclude_assignment_id)
    for assignment in assignments.order_by("date", "pk"):
        has_schedule_conflict, _reason = assignment.shift.check_staff_conflict(
            assignment.staff_id,
            assignment.date,
            exclude_assignment_id=assignment.pk,
        )
        if has_schedule_conflict:
            continue
        return True
    return False


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

    @classmethod
    def get_or_create_roster(cls, treatment_plant_id: int, year: int, month: int):
        return cls.objects.get_or_create(
            treatment_plant_id=treatment_plant_id,
            year=year,
            month=month,
        )

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

    @classmethod
    def get_week_bounds(cls, target_date: date) -> tuple[date, date]:
        for week_start, week_end in cls.get_weeks_in_month(target_date.year, target_date.month):
            if week_start <= target_date <= week_end:
                return week_start, week_end
        raise ValueError(f"No roster week found for {target_date:%Y-%m-%d}.")

    @staticmethod
    def assign_monthly_shifts_bulk(
            duty_roster: "MonthlyDutyRoster",
            section,
            holidays: Optional[list[date]] = None,
            start_date: Optional[date] = None,
    ) -> None:
        holidays = set(holidays or [])
        if not section_belongs_to_plant(section, duty_roster.treatment_plant_id):
            raise ValidationError("The requested roster section must belong to the roster's treatment plant.")
        if not section.is_active:
            raise ValidationError("An inactive section cannot receive duty assignments.")

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
            supervisors_by_shift = {
                supervisor.shift_id: supervisor.user
                for supervisor in ShiftSupervisor.objects.filter(
                    treatment_plant=duty_roster.treatment_plant,
                    shift__treatment_plant=duty_roster.treatment_plant,
                    shift__section_requirements__section=section,
                    shift__section_requirements__section__is_active=True,
                    user__is_active=True,
                    is_active=True,
                ).filter(
                    models.Q(user__is_superuser=True)
                    | models.Q(
                        user__assigned_treatment_plant=duty_roster.treatment_plant,
                        user__section=section,
                    )
                ).select_related("user", "shift").distinct()
            }
            static_supervisor_shifts_by_user = defaultdict(set)
            for assigned_shift_id, supervisor_user in supervisors_by_shift.items():
                static_supervisor_shifts_by_user[supervisor_user.pk].add(assigned_shift_id)
            roster_weeks = MonthlyDutyRoster.get_weeks_in_month(duty_roster.year, duty_roster.month)
            try:
                from sc.services.supervisor_balance_service import balance_supervisors_for_roster
                balance_supervisors_for_roster(
                    treatment_plant_id=duty_roster.treatment_plant_id,
                    section_id=section.pk,
                    year=duty_roster.year,
                    month=duty_roster.month,
                    designated_by=duty_roster.created_by,
                    overwrite_existing=False,
                )
            except Exception:
                pass

            weekly_designations = list(ShiftSupervisor.objects.filter(
                treatment_plant=duty_roster.treatment_plant,
                section=section,
                week_start__isnull=False,
                week_start__in=[week_start for week_start, _week_end in roster_weeks],
                shift__is_deleted=False,
                shift__section_requirements__section=section,
                shift__section_requirements__section__is_active=True,
            ).select_related("user", "shift"))
            weekly_supervisors_by_shift_week = {
                (designation.shift_id, designation.week_start): designation.user
                for designation in weekly_designations
            }
            weekly_supervisor_shift_by_user_date = defaultdict(set)
            for designation in weekly_designations:
                current_day = designation.week_start
                while current_day <= designation.week_end:
                    weekly_supervisor_shift_by_user_date[(designation.user_id, current_day)].add(designation.shift_id)
                    current_day += timedelta(days=1)
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
            assigned_by_date = defaultdict(set)
            supervisory_shift_by_date = defaultdict(set)

            for assignment in existing:
                existing_by_shift[
                    (assignment.date, assignment.shift_id)
                ].add(assignment.staff_id)
                assigned_by_date[assignment.date].add(assignment.staff_id)
                for supervisor_id in (assignment.supervisor_id, assignment.temp_supervisor_id, assignment.handover_to_id):
                    if supervisor_id:
                        supervisory_shift_by_date[(supervisor_id, assignment.date)].add(assignment.shift_id)
                if assignment.shift.is_overnight:
                    assigned_by_date[assignment.date + timedelta(days=1)].add(assignment.staff_id)

                staff_load[assignment.staff_id] += 1

                last_shift[assignment.staff_id] = (
                    assignment.date,
                    assignment.shift,
                )

            # Query all active assignments across all sections/plants for these staff members
            # to guarantee that no user can appear in multiple shifts in a single day
            all_active_period_assignments = ShiftAssignment.objects.filter(
                staff_id__in=staff_ids,
                date__gte=month_start - timedelta(days=1),
                date__lte=month_end + timedelta(days=1),
                is_deleted=False,
            ).select_related("shift")
            for assignment in all_active_period_assignments:
                assigned_by_date[assignment.date].add(assignment.staff_id)
                if assignment.shift.is_overnight:
                    assigned_by_date[assignment.date + timedelta(days=1)].add(assignment.staff_id)
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

                        if day_type in (ShiftAssignment.DAYTYPE_WEEKEND, ShiftAssignment.DAYTYPE_HOLIDAY):
                            # Enforce minimum staff on weekends and public holidays:
                            # if existing assignments exceed required (minimum), prune excess assignments
                            if len(assigned) > required:
                                excess = [
                                    a for a in existing
                                    if a.date == current and a.shift_id == shift.id and a.staff_id in assigned
                                ][required:]
                                for ex in excess:
                                    assigned.discard(ex.staff_id)
                                    assigned_by_date[current].discard(ex.staff_id)
                                    if shift.is_overnight:
                                        assigned_by_date[current + timedelta(days=1)].discard(ex.staff_id)
                                    staff_load[ex.staff_id] = max(0, staff_load[ex.staff_id] - 1)
                                    ex.delete()
                                    if ex in existing:
                                        existing.remove(ex)

                        if len(assigned) >= required:
                            continue

                        candidates = []
                        for staff in staff_order:
                            # Rule: a user can only work in one shift per day (max 8 hours, no working across shifts)
                            if staff.id in assigned_by_date[current] or current in unavailable_days_by_staff.get(staff.id, set()):
                                continue
                            designated_shifts = weekly_supervisor_shift_by_user_date.get((staff.id, current), set())
                            if designated_shifts and shift.id not in designated_shifts:
                                continue
                            static_designated_shifts = static_supervisor_shifts_by_user.get(staff.id, set())
                            if static_designated_shifts and any(designated_shift_id != shift.id for designated_shift_id in static_designated_shifts):
                                continue
                            current_supervisor_shifts = supervisory_shift_by_date.get((staff.id, current), set())
                            if current_supervisor_shifts and any(supervisor_shift_id != shift.id for supervisor_shift_id in current_supervisor_shifts):
                                continue
                            if shift.is_overnight:
                                next_day_designations = weekly_supervisor_shift_by_user_date.get((staff.id, current + timedelta(days=1)), set())
                                if next_day_designations and shift.id not in next_day_designations:
                                    continue
                                next_day_supervisor_shifts = supervisory_shift_by_date.get((staff.id, current + timedelta(days=1)), set())
                                if next_day_supervisor_shifts and any(supervisor_shift_id != shift.id for supervisor_shift_id in next_day_supervisor_shifts):
                                    continue
                            if shift.is_overnight and staff.id in assigned_by_date[current + timedelta(days=1)]:
                                continue
                            previous = last_shift.get(staff.id)
                            if previous:
                                prev_date, prev_shift = previous
                                if (
                                        prev_shift.is_overnight
                                        and prev_date == current - timedelta(days=1)
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
                            assigned_by_date[current].add(staff.id)
                            if shift.is_overnight:
                                assigned_by_date[current + timedelta(days=1)].add(staff.id)
                            staff_load[staff.id] += 1
                            last_shift[staff.id] = (current,shift, )

                    current += timedelta(days=1)

            if new_assignments:
                for assignment in new_assignments:
                    assignment.save()

            # -----------------------------------------------------
            # Supervisor assignment
            # -----------------------------------------------------

            if not supervisors_by_shift and not weekly_supervisors_by_shift_week:
                return

            assignments = list(
                ShiftAssignment.objects.filter(
                    duty_roster=duty_roster,
                    staff__assigned_treatment_plant=duty_roster.treatment_plant,
                    staff__section=section,
                    staff__is_active=True,
                    date__gte=assignment_start,
                    date__lte=month_end,
                    is_deleted=False,
                )
                .select_related("shift")
                .order_by(
                    "date",
                    "shift__start_time",
                )
            )

            rostered_supervisor_weeks = set()
            for roster_week_start, roster_week_end in roster_weeks:
                for shift_id in {assignment.shift_id for assignment in assignments}:
                    supervisor = weekly_supervisors_by_shift_week.get(
                        (shift_id, roster_week_start)
                    ) or supervisors_by_shift.get(shift_id)
                    if supervisor and _has_valid_shift_assignment_for_week(
                        user_id=supervisor.pk,
                        shift_id=shift_id,
                        section_id=section.pk,
                        week_start=roster_week_start,
                        week_end=roster_week_end,
                    ):
                        rostered_supervisor_weeks.add((shift_id, supervisor.pk, roster_week_start))

            for assignment in assignments:
                assignment_week_start, _assignment_week_end = MonthlyDutyRoster.get_week_bounds(assignment.date)
                supervisor = weekly_supervisors_by_shift_week.get(
                    (assignment.shift_id, assignment_week_start)
                ) or supervisors_by_shift.get(assignment.shift_id)
                if (
                    not supervisor
                    or (assignment.shift_id, supervisor.pk, assignment_week_start) not in rostered_supervisor_weeks
                    or assignment.date in unavailable_days_by_staff.get(supervisor.id, set())
                    or AnnualLeave.is_unavailable(supervisor.pk, assignment.date)
                ):
                    continue

                if assignment.supervisor_id != supervisor.id:
                    assignment.supervisor = supervisor
                    assignment.save(update_fields=("supervisor",))

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
            outgoing_supervisor = ShiftSupervisor.get_active_supervisor(
                treatment_plant_id=shift.treatment_plant_id,
                section_id=assignment_section_id,
                shift_id=shift.pk,
                assignment_date=date_,
            )

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

            if assignments:
                for assignment in assignments_qs.select_for_update().select_related("staff", "handover_to"):
                    old_handover_to_id = assignment.handover_to_id
                    assignment.handover_to = incoming_supervisor
                    assignment.save(update_fields=("handover_to",))
                    AuditMixin.create_audit(
                        user=incoming_supervisor,
                        action="update",
                        instance=assignment,
                        before={"handover_to": old_handover_to_id},
                        after={"handover_to": incoming_supervisor.pk if incoming_supervisor else None},
                    )

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
