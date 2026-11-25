#sc/models.py
from __future__ import annotations
import threading
from datetime import  timedelta as py_timedelta
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from typing import Optional, List, Dict, Tuple
from django.db import models, transaction
from django.db.models import Count
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


def is_user_temporarily_supervising(user_id, target_date, at=None) -> bool:
    """Whether the user currently has a temporary supervisor duty on this date."""
    if not user_id or not target_date:
        return False
    at = at or timezone.now()
    return ShiftAssignment.objects.filter(
        temp_supervisor_id=getattr(user_id, "pk", user_id),
        date=target_date,
        is_deleted=False,
        temp_supervisor_start__lte=at,
        temp_supervisor_end__gt=at,
    ).exists()


class SafeDateTimeRangeField(DateTimeRangeField):
    """PostgreSQL timestamp range field used by weekly shift assignments."""
    def get_placeholder(self, value, compiler, connection):
        return '%s::tstzrange'

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
class ShiftQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Update shifts individually so roster and scheduling rules can be validated.")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("Create shifts individually so scheduling rules can be validated.")

    def delete(self):
        if ShiftAssignment.objects.filter(
            shift_id__in=self.values("pk"),
            is_deleted=False,
        ).exists():
            raise ValidationError("Move or remove active roster assignments before deleting a shift.")
        return super().delete()


class ShiftManager(models.Manager.from_queryset(ShiftQuerySet)):
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

        if self.pk and self.treatment_plant_id:
            previous_plant_id = type(self).objects.filter(pk=self.pk).values_list(
                "treatment_plant_id", flat=True
            ).first()
            if previous_plant_id and previous_plant_id != self.treatment_plant_id:
                if ShiftAssignment.objects.filter(shift_id=self.pk, is_deleted=False).exists():
                    raise ValidationError({
                        "treatment_plant": "Move or remove active roster assignments before changing a shift's treatment plant."
                    })
                requirements = ShiftSectionRequirement.objects.filter(shift_id=self.pk).select_related("section")
                if any(
                    not section_belongs_to_plant(requirement.section, self.treatment_plant_id)
                    for requirement in requirements
                ):
                    raise ValidationError({
                        "treatment_plant": "Update or remove shift section requirements before changing to a plant that does not include their sections."
                    })

        if self.is_deleted and self.pk and ShiftAssignment.objects.filter(
            shift_id=self.pk,
            is_deleted=False,
        ).exists():
            raise ValidationError({
                "is_deleted": "Remove active roster assignments before deactivating this shift."
            })

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
        exclude_assignment_ids=None,
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

        excluded_assignment_ids = set(exclude_assignment_ids or ())
        if exclude_assignment_id:
            excluded_assignment_ids.add(exclude_assignment_id)

        def exclude_mutable_assignments(queryset):
            if excluded_assignment_ids:
                return queryset.exclude(pk__in=excluded_assignment_ids)
            return queryset

        # 1. Staff cannot work across shifts on the same day (must only work in one shift)
        same_day_assignments = exclude_mutable_assignments(ShiftAssignment.objects.filter(
            staff_id=staff_id,
            date=assignment_date,
            is_deleted=False,
            shift__is_deleted=False,
        ))
        existing_same_day = same_day_assignments.select_related("shift").first()
        if existing_same_day:
            return True, (
                f"A staff member can appear only once on the roster per day and cannot work across shifts. "
                f"Staff is already assigned to {existing_same_day.shift.name} on {assignment_date:%Y-%m-%d}."
            )

        same_day_supervisory_duties = exclude_mutable_assignments(ShiftAssignment.objects.filter(
            date=assignment_date,
            is_deleted=False,
            shift__is_deleted=False,
        ).filter(
            models.Q(temp_supervisor_id=staff_id)
            | models.Q(handover_to_id=staff_id)
        ).exclude(shift_id=self.pk))
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
        yesterday_assignments = exclude_mutable_assignments(ShiftAssignment.objects.filter(
            date=yesterday,
            is_deleted=False,
            shift__is_deleted=False,
        ).filter(
            models.Q(staff_id=staff_id)
            | models.Q(temp_supervisor_id=staff_id)
            | models.Q(handover_to_id=staff_id)
        ))
        for prev in yesterday_assignments.select_related("shift"):
            if prev.shift.is_overnight and prev.shift.end_time > time(0, 0):
                return True, (
                    f"A user can only work for 8 hours in a single day and cannot work across shifts. "
                    f"Staff already completed an overnight shift ({prev.shift.name}) extending into {assignment_date:%Y-%m-%d}."
                )

        # 3. If current shift is overnight, it extends into tomorrow; staff cannot work another shift tomorrow
        if self.is_overnight and self.end_time > time(0, 0):
            tomorrow = assignment_date + timedelta(days=1)
            tomorrow_assignments = exclude_mutable_assignments(ShiftAssignment.objects.filter(
                date=tomorrow,
                is_deleted=False,
                shift__is_deleted=False,
            ).filter(
                models.Q(staff_id=staff_id)
                | models.Q(temp_supervisor_id=staff_id)
                | models.Q(handover_to_id=staff_id)
            ))
            first_tomorrow = tomorrow_assignments.select_related("shift").first()
            if first_tomorrow:
                return True, (
                    f"A user can only work for 8 hours in a single day. "
                    f"This overnight shift extends into {tomorrow:%Y-%m-%d}, where staff is already assigned to {first_tomorrow.shift.name}."
                )

        # 4. Adjacent window overlap
        adjacent_assignments = exclude_mutable_assignments(ShiftAssignment.objects.filter(
            date__range=(assignment_date - timedelta(days=1), assignment_date + timedelta(days=1)),
            is_deleted=False,
            shift__is_deleted=False,
        ).filter(
            models.Q(staff_id=staff_id)
            | models.Q(temp_supervisor_id=staff_id)
            | models.Q(handover_to_id=staff_id)
        ))
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
        exclude_assignment_ids=None,
    ) -> tuple[bool, Optional[str]]:
        """
        Validates if staff member is eligible to be assigned to this shift on assignment_date.
        Gated by: active status, plant scope, section requirement, leave/unavailability, conflict checks.
        """
        if not staff.is_active:
            return False, f"Staff member '{staff.get_username()}' is inactive."
        if self.is_deleted:
            return False, "Assignments cannot be made to an inactive shift."

        if is_user_incharge(staff, section=section or staff.section_id):
            return False, f"Staff member '{staff.get_username()}' is the section incharge and is not assigned to shifts."

        target_section_id = section.pk if hasattr(section, "pk") else (section or staff.section_id)
        if staff.assigned_treatment_plant_id != self.treatment_plant_id:
            return False, "The staff member and shift must belong to the same treatment plant."
        if not target_section_id or staff.section_id != target_section_id:
            return False, "The staff member does not belong to the target section."
        if not section_belongs_to_plant(staff.section, self.treatment_plant_id):
            return False, "The staff member's section must belong to the shift's treatment plant."

        req_exists = self.section_requirements.filter(
            section_id=target_section_id,
            section__is_active=True,
        ).exists()
        if not req_exists:
            try:
                ShiftSectionRequirement.objects.get_or_create(
                    shift=self,
                    section_id=target_section_id,
                    defaults={
                        "minimum_required": 1,
                        "preferred_staff": 2,
                        "maximum_allowed": 20,
                    }
                )
                req_exists = True
            except Exception:
                pass
        if not req_exists:
            return False, "The shift must be configured for the staff member's assigned section."

        if AnnualLeave.is_unavailable(staff.pk, assignment_date):
            return False, "This staff member is marked on annual leave or off duty for the selected date."

        if AnnualLeave.objects.filter(
            staff_id=staff.pk,
            status=AnnualLeave.STATUS_IN_CHARGE,
            start_date__lte=assignment_date,
            end_date__gte=assignment_date,
        ).exists():
            return False, "This staff member is marked in charge and is exempt from routine shift work."

        if is_user_temporarily_supervising(staff.pk, assignment_date):
            return False, "This staff member is temporarily supervising and is exempt from routine shift work."

        has_conflict, reason = self.check_staff_conflict(
            staff.pk,
            assignment_date,
            exclude_assignment_id=exclude_assignment_id,
            exclude_assignment_ids=exclude_assignment_ids,
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
        exclude_assignment_id: Optional[int] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        Checks whether this shift has capacity for another assignment in the given section.
        Weekdays use maximum_allowed; weekends and holidays are capped at their minimum.
        """
        section_id = section.pk if hasattr(section, "pk") else section
        if not section_id or not assignment_date:
            return False, "Choose an active section and roster date."
        if self.is_deleted:
            return False, "Assignments cannot be made to an inactive shift."
        requirement = self.section_requirements.filter(
            section_id=section_id,
            section__is_active=True,
        ).select_related("section").first()
        if not requirement:
            try:
                requirement, _ = ShiftSectionRequirement.objects.get_or_create(
                    shift=self,
                    section_id=section_id,
                    defaults={
                        "minimum_required": 1,
                        "preferred_staff": 2,
                        "maximum_allowed": 20,
                    }
                )
            except Exception:
                pass
        if not requirement:
            return False, "This shift is not configured for the selected section."
        if not section_belongs_to_plant(requirement.section, self.treatment_plant_id):
            return False, "The selected section is not available to this shift's treatment plant."

        assignments = ShiftAssignment.objects.filter(
            shift=self,
            date=assignment_date,
            staff__section_id=section_id,
            is_deleted=False,
        )
        if exclude_assignment_id:
            assignments = assignments.exclude(pk=exclude_assignment_id)
        current_count = assignments.count()

        # Calendar weekends always use the weekend requirement, even if a caller
        # supplies a stale or mislabeled day_type.
        effective_day_type = (
            ShiftAssignment.DAYTYPE_WEEKEND
            if assignment_date.weekday() >= 5
            else day_type or ShiftAssignment.determine_day_type(assignment_date)
        )
        if effective_day_type not in {choice for choice, _label in ShiftAssignment.DAYTYPE_CHOICES}:
            return False, "Choose a valid duty day type."
        if effective_day_type == ShiftAssignment.DAYTYPE_WEEKEND and assignment_date.weekday() < 5:
            return False, "A weekend requirement cannot be used on a weekday date."

        capacity_limit = requirement.maximum_allowed
        if current_count >= capacity_limit:
            return False, (
                f"Maximum allowed staffing limit ({capacity_limit}) reached "
                f"for {self.name} in {requirement.section.name}."
            )
        return True, None


class ShiftSectionRequirementQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Update shift section requirements individually so active roster limits can be validated.")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("Create shift section requirements individually so their scope can be validated.")

    def delete(self):
        with transaction.atomic():
            requirement_scopes = list(
                self.select_for_update().values("pk", "shift_id", "section_id")
            )
            for scope in requirement_scopes:
                if ShiftAssignment.objects.filter(
                    shift_id=scope["shift_id"],
                    staff__section_id=scope["section_id"],
                    is_deleted=False,
                ).exists():
                    raise ValidationError(
                        "Move or remove active assignments before deleting a shift section requirement."
                    )
            return super().delete()


class ShiftSectionRequirement(models.Model):
    objects = ShiftSectionRequirementQuerySet.as_manager()

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
        limit = (
            self.get_required_staff(day_type)
            if day_type in {
                ShiftAssignment.DAYTYPE_WEEKEND,
                ShiftAssignment.DAYTYPE_HOLIDAY,
            }
            else self.maximum_allowed
        )
        return assigned_count > limit

    def has_optimal_staff(
        self,
        assigned_count,
        day_type=None,
    ):
        if day_type in {
            ShiftAssignment.DAYTYPE_WEEKEND,
            ShiftAssignment.DAYTYPE_HOLIDAY,
        }:
            return assigned_count == self.get_required_staff(day_type)
        return self.get_required_staff(day_type) <= assigned_count <= self.maximum_allowed

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
        errors = {}

        # When a minimum is configured, it is the governing weekend/holiday target.
        if self.minimum_required:
            self.weekend_required = self.minimum_required
            self.holiday_required = self.minimum_required

        if self.shift_id and self.section_id:
            shift_plant_id = self.shift.treatment_plant_id
            if self.shift.is_deleted:
                errors["shift"] = "An inactive shift cannot have an active section requirement."
            if not section_belongs_to_plant(self.section, shift_plant_id):
                errors["section"] = "The section must be available to the shift's treatment plant."
            if not self.section.is_active:
                errors["section"] = "An inactive section cannot have an active shift requirement."

        if self.weekday_required > self.maximum_allowed:
            errors["weekday_required"] = "Weekday staffing cannot exceed the maximum allowed."
        if self.minimum_required > self.maximum_allowed:
            errors["minimum_required"] = "Minimum staffing cannot exceed the maximum allowed."
        if self.preferred_required < self.minimum_required:
            errors["preferred_required"] = "Preferred staffing cannot be below the minimum required."
        if self.preferred_required > self.maximum_allowed:
            errors["preferred_required"] = "Preferred staffing cannot exceed the maximum allowed."
        if self.weekend_required > self.maximum_allowed:
            errors["weekend_required"] = "Weekend staffing cannot exceed the maximum allowed."
        if self.holiday_required > self.maximum_allowed:
            errors["holiday_required"] = "Holiday staffing cannot exceed the maximum allowed."

        # Creating or editing a requirement must not leave any roster date outside its limits.
        if self.shift_id and self.section_id and not errors:
            existing_rows = ShiftAssignment.objects.filter(
                shift_id=self.shift_id,
                staff__section_id=self.section_id,
                is_deleted=False,
            ).values("date", "day_type").annotate(assigned_count=Count("pk"))
            cell_counts = {}
            for row in existing_rows:
                cell_date = row["date"]
                if row["day_type"] not in {choice for choice, _label in ShiftAssignment.DAYTYPE_CHOICES}:
                    errors["maximum_allowed"] = f"Roster cell {cell_date:%Y-%m-%d} has an invalid duty day type."
                cell_counts[cell_date] = cell_counts.get(cell_date, 0) + row["assigned_count"]

            day_types_by_date = {
                row["date"]: row["day_type"]
                for row in existing_rows
            }
            for cell_date, assigned_count in cell_counts.items():
                day_type = (
                    ShiftAssignment.DAYTYPE_WEEKEND
                    if cell_date.weekday() >= 5
                    else day_types_by_date.get(cell_date, ShiftAssignment.DAYTYPE_WEEKDAY)
                )
                cell_limit = (
                    self.get_required_staff(day_type)
                    if day_type in {
                        ShiftAssignment.DAYTYPE_WEEKEND,
                        ShiftAssignment.DAYTYPE_HOLIDAY,
                    }
                    else self.maximum_allowed
                )
                limit_field = "maximum_allowed"
                if assigned_count > cell_limit:
                    errors[limit_field] = (
                        f"Roster cell {cell_date:%Y-%m-%d} already has {assigned_count} active assignments; "
                        f"the applicable staffing limit would be {cell_limit}."
                    )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        with transaction.atomic():
            previous_scope = None
            if self.pk:
                previous_scope = type(self).objects.select_for_update().filter(pk=self.pk).values(
                    "shift_id", "section_id"
                ).first()
            if previous_scope and (
                previous_scope["shift_id"] != self.shift_id
                or previous_scope["section_id"] != self.section_id
            ) and ShiftAssignment.objects.filter(
                shift_id=previous_scope["shift_id"],
                staff__section_id=previous_scope["section_id"],
                is_deleted=False,
            ).exists():
                raise ValidationError({
                    "section": "Move or remove active assignments before changing a shift section requirement's scope."
                })
            self.full_clean()
            if kwargs.get("update_fields") is not None and self.minimum_required:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {
                    "weekend_required",
                    "holiday_required",
                }
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        using = kwargs.get("using") or self._state.db
        return type(self).objects.using(using).filter(pk=self.pk).delete()

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
    STATUS_IN_CHARGE = "in_charge"
    STATUS_CHOICES = [
        (STATUS_ANNUAL_LEAVE, "Annual leave"),
        (STATUS_OFF_DUTY, "Off duty"),
        (STATUS_IN_CHARGE, "In charge"),
    ]
    UNAVAILABLE_STATUSES = (STATUS_ANNUAL_LEAVE, STATUS_OFF_DUTY)
    EXEMPT_FROM_ROUTINE_STATUSES = (*UNAVAILABLE_STATUSES, STATUS_IN_CHARGE)

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
            ("manage_staff_availability", "Can manage staff availability periods"),
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

    @classmethod
    def is_exempt_from_routine_work(cls, staff_id, target_date, at=None):
        if not staff_id or not target_date:
            return False
        if cls.objects.filter(
            staff_id=staff_id,
            status__in=cls.EXEMPT_FROM_ROUTINE_STATUSES,
            start_date__lte=target_date,
            end_date__gte=target_date,
        ).exists():
            return True
        return is_user_temporarily_supervising(staff_id, target_date, at=at)

    def clean(self):
        super().clean()
        errors = {}

        if self.status not in self.EXEMPT_FROM_ROUTINE_STATUSES:
            errors["status"] = "Choose annual leave, off duty, or in charge."

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
            )
            if self.status == self.STATUS_IN_CHARGE:
                existing_duties = existing_duties.filter(staff_id=self.staff_id)
            else:
                existing_duties = existing_duties.filter(
                    models.Q(staff_id=self.staff_id)
                    | models.Q(supervisor_id=self.staff_id)
                    | models.Q(temp_supervisor_id=self.staff_id)
                    | models.Q(handover_to_id=self.staff_id)
                )
            conflict_messages = []
            if existing_duties.exists():
                conflict_messages.append("Reassign or remove overlapping shift duties before setting this status.")

            if self.status != self.STATUS_IN_CHARGE and ShiftSupervisor.objects.filter(
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

            if self.status != self.STATUS_IN_CHARGE:
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


class ShiftAssignmentQuerySet(SoftDeleteQuerySet):
    placement_fields = {
        "staff", "staff_id", "shift", "shift_id", "date", "day_type",
        "duty_roster", "duty_roster_id", "is_deleted",
    }

    def update(self, **kwargs):
        if self.placement_fields.intersection(kwargs):
            raise ValidationError("Change roster placement through ShiftAssignment.save() so shift requirements are enforced.")
        return super().update(**kwargs)

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("Use the weekly roster workflow so shift requirements are enforced.")

    def bulk_create_weekly_crew(self, objs, **kwargs):
        """Validate a complete weekly crew batch before writing it in one query."""
        objs = list(objs)
        if not objs:
            return []
        if any(not isinstance(obj, self.model) or not obj._state.adding for obj in objs):
            raise ValidationError("Only new roster assignments can be added to a weekly crew batch.")

        scopes = {
            (obj.duty_roster_id, obj.shift_id, obj.staff.section_id)
            for obj in objs
            if obj.staff_id and obj.duty_roster_id and obj.shift_id
        }
        if len(scopes) != 1 or any(not obj.staff_id or not obj.duty_roster_id or not obj.shift_id for obj in objs):
            raise ValidationError("A weekly crew batch must belong to one roster, shift, and section.")

        team_key = next(iter(scopes))
        weeks = {MonthlyDutyRoster.get_week_bounds(obj.date) for obj in objs if obj.date}
        if len(weeks) != 1 or any(not obj.date for obj in objs):
            raise ValidationError("A weekly crew batch must stay within one roster week.")
        week_start, week_end = next(iter(weeks))
        duty_roster_id, shift_id, section_id = team_key
        team = WeeklyShiftTeam.objects.filter(
            duty_roster_id=duty_roster_id,
            shift_id=shift_id,
            section_id=section_id,
            week_start=week_start,
            week_end=week_end,
        ).first()
        if not team:
            raise ValidationError("Update the weekly crew before creating its roster assignments.")
        batch_staff_ids = {obj.staff_id for obj in objs}
        if not batch_staff_ids.issubset(set(team.members.values_list("pk", flat=True))):
            raise ValidationError("Every assigned staff member must be part of the saved weekly crew.")

        assignment_keys = [(obj.staff_id, obj.date) for obj in objs if not obj.is_deleted]
        if len(assignment_keys) != len(set(assignment_keys)):
            raise ValidationError("A staff member can only be assigned once per day.")

        for obj in objs:
            obj.full_clean()
        return super().bulk_create(objs, **kwargs)

    def delete(self):
        deleted_count = 0
        with transaction.atomic():
            assignments = self.filter(is_deleted=False).select_for_update().select_related(
                "staff", "shift", "duty_roster"
            ).order_by("pk")
            for assignment in assignments:
                assignment.delete()
                deleted_count += 1
        return deleted_count, {"sc.ShiftAssignment": deleted_count}


class ShiftAssignment(AuditMixin, SoftDeleteMixin, models.Model):
    objects = ShiftAssignmentQuerySet.as_manager()

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
            "temp_supervisor_id", "reassigned_from_id", "day_type", "is_deleted"
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
                ("day_type", self.day_type),
                ("is_deleted", self.is_deleted),
            )
        )

    def clean(self):
        super().clean()
        errors = {}
        period_changed = self._assignment_period_changed()

        if not self.is_deleted and self.date:
            if self.date.weekday() >= 5:
                self.day_type = self.DAYTYPE_WEEKEND
            elif self.day_type == self.DAYTYPE_WEEKEND:
                errors["day_type"] = "Only Saturday and Sunday can use the weekend shift requirement."

        # Rule: No user should appear in multiple shifts in a single day. A user can only work for a single shift in any given day.
        # During inter-shift moves, excluded assignment IDs are set so the move
        # is not blocked by the assignments being relocated.
        if not self.is_deleted and self.staff_id and self.date and period_changed:
            existing_same_day = ShiftAssignment.objects.filter(
                staff_id=self.staff_id,
                date=self.date,
                is_deleted=False,
            )
            if self.pk:
                existing_same_day = existing_same_day.exclude(pk=self.pk)
            excluded_ids = getattr(self, "_eligibility_excluded_assignment_ids", None)
            if excluded_ids:
                existing_same_day = existing_same_day.exclude(pk__in=excluded_ids)
            conflict_assignment = existing_same_day.select_related("shift").first()
            if conflict_assignment:
                errors["staff"] = (
                    f"A user can only work for a single shift in any given day. "
                    f"Staff is already assigned to {conflict_assignment.shift.name} on {self.date:%Y-%m-%d}."
                )

        if self.staff_id and self.shift_id:
            if not self.is_deleted and self.shift.is_deleted:
                errors["shift"] = "An inactive shift cannot receive active duty assignments."
            if not self.is_deleted and not self.staff.is_active:
                errors["staff"] = "Inactive staff cannot receive active duty assignments."
            if not self.is_deleted and self.staff.assigned_treatment_plant_id != self.shift.treatment_plant_id:
                errors["staff"] = "The staff member and shift must belong to the same treatment plant."
            if not self.is_deleted and self.staff.section_id and not section_belongs_to_plant(
                self.staff.section,
                self.shift.treatment_plant_id,
            ):
                errors["staff"] = "The staff member's section must belong to the shift's treatment plant."
            if not self.is_deleted:
                if not self.staff.section_id:
                    errors["staff"] = "The staff member must belong to a section."
                elif not ShiftSectionRequirement.objects.filter(
                    shift_id=self.shift_id,
                    section_id=self.staff.section_id,
                    section__is_active=True,
                ).exists():
                    try:
                        ShiftSectionRequirement.objects.get_or_create(
                            shift=self.shift,
                            section_id=self.staff.section_id,
                            defaults={
                                "minimum_required": 1,
                                "preferred_staff": 2,
                                "maximum_allowed": 20,
                            }
                        )
                    except Exception:
                        errors["staff"] = "The shift must have an active section requirement for the staff member's assigned section."

        if self.duty_roster_id and self.shift_id:
            if self.duty_roster.treatment_plant_id != self.shift.treatment_plant_id:
                errors["duty_roster"] = "The roster and shift must belong to the same treatment plant."
            if self.date and (self.date.month != self.duty_roster.month or self.date.year != self.duty_roster.year):
                errors["date"] = "The assignment date must fall within the roster month."

        if (
            not self.is_deleted
            and self.staff_id
            and self.shift_id
            and self.duty_roster_id
            and self.date
            and self.date.month == self.duty_roster.month
            and self.date.year == self.duty_roster.year
        ):
            week_start, week_end = MonthlyDutyRoster.get_week_bounds(self.date)
            weekly_team = WeeklyShiftTeam.objects.filter(
                duty_roster_id=self.duty_roster_id,
                shift_id=self.shift_id,
                section_id=self.staff.section_id,
                week_start=week_start,
                week_end=week_end,
            ).first()
            if weekly_team and not weekly_team.members.filter(pk=self.staff_id).exists() and not getattr(self, "_weekly_team_change_allowed", False):
                errors["staff"] = (
                    "This staff member is not part of the saved weekly crew. "
                    "Update the weekly crew through the duty roster before assigning them."
                )
            if weekly_team and weekly_team.is_reconciled and self.pk and not getattr(
                self,
                "_weekly_team_change_allowed",
                False,
            ):
                previous_placement = type(self).objects.filter(pk=self.pk).values(
                    "staff_id", "shift_id", "date", "duty_roster_id", "is_deleted"
                ).first()
                if previous_placement and not previous_placement["is_deleted"] and any(
                    previous_placement[field_name] != new_value
                    for field_name, new_value in (
                        ("staff_id", self.staff_id),
                        ("shift_id", self.shift_id),
                        ("date", self.date),
                        ("duty_roster_id", self.duty_roster_id),
                    )
                ):
                    errors["date"] = (
                        "This weekly crew is already reconciled. Move or change its members through "
                        "the weekly roster action so every day stays in sync."
                    )

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
                    if not still_assigned_within_shift and not replacement_exists and not getattr(self, "_weekly_team_change_allowed", False):
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
                        | models.Q(supervisor_id=person.pk)
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
            if field_name == "temp_supervisor" and self.shift_id and self.date:
                temp_designation = ShiftSupervisor.objects.filter(
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
                ).filter(
                    models.Q(week_start__isnull=True)
                    | models.Q(week_start__lte=self.date, week_end__gte=self.date)
                ).exists()
                if not temp_designation and not is_user_incharge(person, section=assignment_section_id):
                    errors[field_name] = "The temporary supervisor must be a designated shift supervisor or in-charge."

        if self.temp_supervisor_id:
            if not self.temp_supervisor_start or not self.temp_supervisor_end:
                errors["temp_supervisor_end"] = "A temporary supervisor must have a defined start and end time."
            elif self.temp_supervisor_end <= self.temp_supervisor_start:
                errors["temp_supervisor_end"] = "The temporary supervisor end time must be after the start time."
            elif self.temp_supervisor_end - self.temp_supervisor_start > timedelta(hours=3):
                errors["temp_supervisor_end"] = "A temporary supervisor duty cannot exceed three hours."
            elif self.date and (
                timezone.localtime(self.temp_supervisor_start).date() != self.date
                or timezone.localtime(self.temp_supervisor_end).date() != self.date
            ):
                errors["temp_supervisor_start"] = "Temporary supervisor times must fall on the roster assignment date."

        if self.reassigned_from_id and self.shift_id:
            source = self.reassigned_from
            if source.shift.treatment_plant_id != self.shift.treatment_plant_id:
                errors["reassigned_from"] = "The source assignment must belong to the same treatment plant."
            if source.staff.section_id != assignment_section_id:
                errors["reassigned_from"] = "The source assignment must belong to the same section."

        if (
            not self.is_deleted
            and self.staff_id
            and self.date
            and period_changed
            and AnnualLeave.is_unavailable(self.staff_id, self.date)
        ):
            errors["staff"] = "This staff member is marked on annual leave or off duty for the selected date."
        if not self.is_deleted and self.date and period_changed:
            for field_name in ("supervisor", "temp_supervisor"):
                user_id = getattr(self, f"{field_name}_id")
                if user_id and AnnualLeave.is_unavailable(user_id, self.date):
                    errors[field_name] = "A staff member marked on annual leave or off duty cannot supervise a duty on this date."
                if user_id and field_name == "temp_supervisor":
                    if ShiftAssignment.objects.filter(
                        staff_id=user_id,
                        date=self.date,
                        is_deleted=False,
                    ).exists():
                        errors[field_name] = "A temporary supervisor cannot also be rostered for routine shift work that day."
                    active_temporary_supervisor = ShiftAssignment.objects.filter(
                        temp_supervisor_id=user_id,
                        date=self.date,
                        is_deleted=False,
                        temp_supervisor_start__lte=timezone.now(),
                        temp_supervisor_end__gt=timezone.now(),
                    ).exclude(pk=self.pk).exists()
                    if active_temporary_supervisor:
                        errors[field_name] = "This staff member is already temporarily supervising another duty."
                    from django.apps import apps
                    if apps.get_model("weeklytasks", "DailyTaskAssignment").objects.filter(
                        user_id=user_id,
                        date=self.date,
                    ).exists():
                        errors[field_name] = "A temporary supervisor cannot also have a routine task assignment that day."

        # Recheck capacity and all staff eligibility rules at the model boundary,
        # so admin, service, and direct model saves use the same shift requirement.
        if self.shift_id and self.staff_id and self.date and period_changed and not self.is_deleted:
            is_eligible, eligibility_reason = self.shift.is_staff_eligible(
                self.staff,
                self.date,
                section=self.staff.section_id,
                exclude_assignment_id=self.pk,
                exclude_assignment_ids=getattr(self, "_eligibility_excluded_assignment_ids", None),
            )
            if not is_eligible and not getattr(self, "_weekly_team_change_allowed", False):
                errors["staff"] = eligibility_reason

            can_accept, capacity_reason = self.shift.can_accept_assignment(
                self.staff.section_id,
                self.date,
                day_type=self.day_type,
                exclude_assignment_id=self.pk,
            )
            if not can_accept and not getattr(self, "_allow_capacity_override", False) and not getattr(self, "_weekly_team_change_allowed", False):
                errors["shift"] = capacity_reason
        elif not self.is_deleted and self.shift_id:
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
        if not self.is_deleted and self.date:
            normalized_day_type = (
                self.DAYTYPE_WEEKEND
                if self.date.weekday() >= 5
                else self.DAYTYPE_WEEKDAY if self.day_type == self.DAYTYPE_WEEKEND else self.day_type
            )
            if self.day_type != normalized_day_type:
                self.day_type = normalized_day_type
                if update_fields is not None:
                    update_fields = set(update_fields) | {"day_type"}
                    kwargs["update_fields"] = update_fields
        assignment_fields = {
            "staff", "staff_id", "shift", "shift_id", "date", "duty_roster", "duty_roster_id",
            "day_type",
            "supervisor", "supervisor_id", "handover_to", "handover_to_id", "temp_supervisor",
            "temp_supervisor_id", "reassigned_from", "reassigned_from_id", "is_deleted",
        }
        validate_assignment = not update_fields or assignment_fields.intersection(update_fields)
        with transaction.atomic():
            period_changed = validate_assignment and self._assignment_period_changed()
            previous_scope = None
            if validate_assignment:
                if (
                    not self.is_deleted
                    and self.staff_id
                    and self.shift_id
                    and self.duty_roster_id
                    and self.date
                    and self.date.month == self.duty_roster.month
                    and self.date.year == self.duty_roster.year
                    and self.staff.section_id
                ):
                    weekly_team, team_created = WeeklyShiftTeam.get_for_date(
                        duty_roster=self.duty_roster,
                        shift=self.shift,
                        section=self.staff.section,
                        target_date=self.date,
                        create=True,
                    )
                    if team_created:
                        weekly_team.replace_staff_ids({self.staff_id}, reconciled=False)
                if not self.is_deleted and self.staff_id and self.shift_id:
                    list(ShiftSectionRequirement.objects.select_for_update().filter(
                        shift_id=self.shift_id,
                        section_id=self.staff.section_id,
                        section__is_active=True,
                    ).values_list("pk", flat=True)[:1])
                previous = None
                if self.pk:
                    previous = type(self).objects.filter(pk=self.pk).values(
                        "staff_id", "staff__section_id", "shift_id", "date", "duty_roster_id", "is_deleted",
                        "supervisor_id", "temp_supervisor_id", "handover_to_id",
                    ).first()
                    previous_scope = previous
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
            if validate_assignment:
                crew_scope_changed = previous_scope is None or any(
                    previous_scope.get(field_name) != new_value
                    for field_name, new_value in (
                        ("staff_id", self.staff_id),
                        ("staff__section_id", self.staff.section_id if self.staff_id else None),
                        ("shift_id", self.shift_id),
                        ("date", self.date),
                        ("duty_roster_id", self.duty_roster_id),
                        ("is_deleted", self.is_deleted),
                    )
                )
                if crew_scope_changed:
                    old_team_scope = None
                    if previous_scope and previous_scope.get("date") and previous_scope.get("duty_roster_id"):
                        old_week_start, old_week_end = MonthlyDutyRoster.get_week_bounds(previous_scope["date"])
                        old_team_scope = WeeklyShiftTeam.objects.filter(
                            duty_roster_id=previous_scope["duty_roster_id"],
                            shift_id=previous_scope["shift_id"],
                            section_id=previous_scope["staff__section_id"],
                            week_start=old_week_start,
                            week_end=old_week_end,
                        ).first()
                    new_team_scope = None
                    if self.date and self.duty_roster_id and self.staff_id:
                        new_week_start, new_week_end = MonthlyDutyRoster.get_week_bounds(self.date)
                        new_team_scope = WeeklyShiftTeam.objects.filter(
                            duty_roster_id=self.duty_roster_id,
                            shift_id=self.shift_id,
                            section_id=self.staff.section_id,
                            week_start=new_week_start,
                            week_end=new_week_end,
                        ).first()
                    affected_teams = {
                        team.pk: team
                        for team in (old_team_scope, new_team_scope)
                        if team is not None
                    }
                    for team in affected_teams.values():
                        if team.is_reconciled:
                            team.is_reconciled = False
                            team.save(update_fields=("is_reconciled", "updated_at"))

    def delete(self, using=None, keep_parents=False) -> None:
        team_scope = None
        if self.date and self.duty_roster_id and self.shift_id and self.staff_id:
            week_start, week_end = MonthlyDutyRoster.get_week_bounds(self.date)
            team_scope = WeeklyShiftTeam.objects.filter(
                duty_roster_id=self.duty_roster_id,
                shift_id=self.shift_id,
                section_id=self.staff.section_id,
                week_start=week_start,
                week_end=week_end,
            ).first()
        if (
            team_scope
            and team_scope.is_reconciled
            and not getattr(self, "_weekly_team_change_allowed", False)
        ):
            raise ValidationError(
                "This weekly team is reconciled. Remove the person from the complete weekly crew through the roster action."
            )
        super().delete(using=using, keep_parents=keep_parents)
        if team_scope:
            remaining_member_assignments = type(self).objects.filter(
                duty_roster_id=team_scope.duty_roster_id,
                shift_id=team_scope.shift_id,
                staff_id=self.staff_id,
                date__range=(team_scope.week_start, team_scope.week_end),
                is_deleted=False,
            ).exists()
            if not remaining_member_assignments:
                team_scope.replace_staff_ids(
                    set(team_scope.members.values_list("pk", flat=True)) - {self.staff_id},
                    reconciled=False,
                    enforce_maximum=False,
                )
            else:
                team_scope.is_reconciled = False
                team_scope.save(update_fields=("is_reconciled", "updated_at"))
            team_scope.refresh_reconciliation()

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
            raise ValidationError(f"{self.staff} is not present and cannot be temporarily supervised.")

        try:
            duration_hours = float(duration_hours)
        except (TypeError, ValueError):
            raise ValidationError("Choose a temporary supervisor duration from 0 to 3 hours.")
        if duration_hours <= 0 or duration_hours > 3:
            raise ValidationError("A temporary supervisor duty must be longer than 0 and no more than 3 hours.")
        if self.date != timezone.localdate():
            raise ValidationError("A temporary supervisor can only be assigned to today's active duty.")
        if not new_supervisor or not new_supervisor.is_active:
            raise ValidationError("Choose an active temporary supervisor.")
        if not new_supervisor.is_superuser and (
            new_supervisor.assigned_treatment_plant_id != self.shift.treatment_plant_id
            or new_supervisor.section_id != self.staff.section_id
        ):
            raise ValidationError("The temporary supervisor must belong to this shift's treatment plant and section.")
        if new_supervisor.pk == self.staff_id:
            raise ValidationError("The assigned staff member cannot temporarily supervise their own duty.")
        if AnnualLeave.is_unavailable(new_supervisor.pk, self.date):
            raise ValidationError("A staff member on annual leave or off duty cannot temporarily supervise.")
        if is_user_temporarily_supervising(new_supervisor.pk, self.date):
            raise ValidationError("This staff member is already temporarily supervising another duty.")
        if ShiftAssignment.objects.filter(staff_id=new_supervisor.pk, date=self.date, is_deleted=False).exists():
            raise ValidationError("A temporary supervisor cannot also be rostered for routine shift work that day.")
        other_supervisory_duty = ShiftAssignment.objects.filter(
            date=self.date,
            is_deleted=False,
        ).exclude(shift_id=self.shift_id).filter(
            models.Q(supervisor_id=new_supervisor.pk)
            | models.Q(handover_to_id=new_supervisor.pk)
        ).exists()
        if other_supervisory_duty:
            raise ValidationError("A temporary supervisor cannot supervise or hand over another shift that day.")
        from django.apps import apps
        if apps.get_model("weeklytasks", "DailyTaskAssignment").objects.filter(
            user_id=new_supervisor.pk,
            date=self.date,
        ).exists():
            raise ValidationError("Reassign this staff member's routine task before appointing them temporary supervisor.")

        is_designated = ShiftSupervisor.objects.filter(
            treatment_plant_id=self.shift.treatment_plant_id,
            section_id=self.staff.section_id,
            user_id=new_supervisor.pk,
            is_active=True,
            user__is_active=True,
            section__is_active=True,
        ).filter(
            models.Q(shift_id=self.shift_id)
            | models.Q(shift__isnull=True)
            | models.Q(shift__treatment_plant_id=self.shift.treatment_plant_id)
        ).filter(
            models.Q(week_start__isnull=True)
            | models.Q(week_start__lte=self.date, week_end__gte=self.date)
        ).exists()
        if not is_designated and not is_user_incharge(new_supervisor, section=self.staff.section_id):
            raise ValidationError("Choose an active shift supervisor or in-charge staff member.")

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
        if target_date.weekday() in (5, 6):
            return cls.DAYTYPE_WEEKEND
        if holidays and target_date in holidays:
            return cls.DAYTYPE_HOLIDAY
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
        if date.weekday() >= 5:
            day_type = cls.DAYTYPE_WEEKEND
        elif day_type is None:
            day_type = cls.determine_day_type(date)
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

    def move_assignment(
        self,
        *,
        target_shift,
        target_date,
        day_type=None,
        moved_by=None,
        remarks="",
        exclude_assignment_ids=None,
    ):
        """
        Moves this assignment to target_shift and target_date, gated by sc domain model invariants.
        """
        old_shift = self.shift
        old_date = self.date
        old_day_type = self.day_type
        old_roster = self.duty_roster
        old_section = self.staff.section
        old_week_start, old_week_end = MonthlyDutyRoster.get_week_bounds(old_date)
        old_team = WeeklyShiftTeam.objects.filter(
            duty_roster=old_roster,
            shift=old_shift,
            section=old_section,
            week_start=old_week_start,
            week_end=old_week_end,
        ).first()

        if target_date.weekday() >= 5:
            day_type = self.DAYTYPE_WEEKEND
        elif day_type is None:
            day_type = self.determine_day_type(target_date)

        if target_shift.treatment_plant_id != self.shift.treatment_plant_id:
            raise ValidationError("Duty assignments cannot be moved between treatment plants.")

        can_accept, capacity_reason = target_shift.can_accept_assignment(
            self.staff.section_id,
            target_date,
            day_type=day_type,
            exclude_assignment_id=self.pk,
        )
        if not can_accept:
            raise ValidationError({"shift": capacity_reason})

        is_eligible, eligibility_reason = target_shift.is_staff_eligible(
            self.staff,
            target_date,
            section=self.staff.section_id,
            exclude_assignment_id=self.pk,
            exclude_assignment_ids=exclude_assignment_ids,
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
        target_team, _ = WeeklyShiftTeam.get_for_date(
            duty_roster=target_roster,
            shift=target_shift,
            section=self.staff.section,
            target_date=target_date,
            create=True,
        )
        target_team.replace_staff_ids(
            set(target_team.members.values_list("pk", flat=True)) | {self.staff_id},
            reconciled=False,
        )
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

        self._eligibility_excluded_assignment_ids = set(exclude_assignment_ids or ())
        self._weekly_team_change_allowed = True
        try:
            self.full_clean()
            self.save()
        finally:
            del self._eligibility_excluded_assignment_ids
            del self._weekly_team_change_allowed

        if old_team and (old_team.shift_id != self.shift_id or old_team.duty_roster_id != self.duty_roster_id):
            remaining_source_rows = type(self).objects.filter(
                duty_roster_id=old_roster.pk,
                shift_id=old_shift.pk,
                staff_id=self.staff_id,
                staff__section_id=old_section.pk,
                date__range=(old_week_start, old_week_end),
                is_deleted=False,
            ).exists()
            if not remaining_source_rows:
                old_team.replace_staff_ids(
                    set(old_team.members.values_list("pk", flat=True)) - {self.staff_id},
                    reconciled=False,
                )
                old_team.refresh_reconciliation()
        target_team.refresh_reconciliation()

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
            end_date: Optional[date] = None,
            rotation_start_date: Optional[date] = None,
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
                status__in=AnnualLeave.EXEMPT_FROM_ROUTINE_STATUSES,
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
            assignment_start = max(month_start, start_date) if start_date else month_start
            assignment_end = min(month_end, end_date) if end_date else month_end
            if assignment_end < month_start:
                return
            if end_date and assignment_end < month_end:
                assignment_end = MonthlyDutyRoster.get_week_bounds(assignment_end)[1]
            if assignment_end < assignment_start:
                return
            regenerated_dates = set()
            for roster_week_start, roster_week_end in roster_weeks:
                if roster_week_end < assignment_start or roster_week_start > assignment_end:
                    continue
                current_roster_date = roster_week_start
                while current_roster_date <= roster_week_end:
                    regenerated_dates.add(current_roster_date)
                    current_roster_date += timedelta(days=1)
            affected_week_starts = [
                roster_week_start
                for roster_week_start, roster_week_end in roster_weeks
                if roster_week_end >= assignment_start and roster_week_start <= assignment_end
            ]
            WeeklyShiftTeam.objects.filter(
                duty_roster=duty_roster,
                section=section,
                week_start__in=affected_week_starts,
            ).update(is_reconciled=False, updated_at=timezone.now())

            if end_date is None:
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
            weekly_supervisor_team_keys = {
                (week_start, shift_id, supervisor.pk)
                for (shift_id, week_start), supervisor in weekly_supervisors_by_shift_week.items()
            }
            roster_week_start_by_date = {}
            for roster_week_start, roster_week_end in roster_weeks:
                roster_date = roster_week_start
                while roster_date <= roster_week_end:
                    roster_week_start_by_date[roster_date] = roster_week_start
                    roster_date += timedelta(days=1)
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
            holidays.update(
                assignment.date
                for assignment in existing
                if assignment.date.weekday() < 5
                and assignment.day_type == ShiftAssignment.DAYTYPE_HOLIDAY
            )
            existing_assignment_lookup = {
                (assignment.date, assignment.shift_id, assignment.staff_id): assignment
                for assignment in existing
            }
            mutable_assignment_ids = {
                assignment.pk
                for assignment in existing
                if assignment.date in regenerated_dates
                and not assignment.temp_supervisor_id
                and not assignment.handover_to_id
                and (
                    roster_week_start_by_date.get(assignment.date),
                    assignment.shift_id,
                    assignment.staff_id,
                ) not in weekly_supervisor_team_keys
            }

            existing_by_shift = defaultdict(set)
            staff_load = defaultdict(int)
            assigned_by_date = defaultdict(set)
            planned_by_date = defaultdict(set)
            supervisory_shift_by_date = defaultdict(set)

            for assignment in existing:
                existing_by_shift[
                    (assignment.date, assignment.shift_id)
                ].add(assignment.staff_id)
                if assignment.pk not in mutable_assignment_ids:
                    assigned_by_date[assignment.date].add(assignment.staff_id)
                for supervisor_id in (assignment.supervisor_id, assignment.temp_supervisor_id, assignment.handover_to_id):
                    if supervisor_id:
                        supervisory_shift_by_date[(supervisor_id, assignment.date)].add(assignment.shift_id)
                if assignment.shift.is_overnight and assignment.pk not in mutable_assignment_ids:
                    assigned_by_date[assignment.date + timedelta(days=1)].add(assignment.staff_id)

                staff_load[assignment.staff_id] += 1

            # Query all active assignments across all sections/plants for these staff members
            # to guarantee that no user can appear in multiple shifts in a single day
            all_active_period_assignments = ShiftAssignment.objects.filter(
                staff_id__in=staff_ids,
                date__gte=month_start - timedelta(days=1),
                date__lte=month_end + timedelta(days=1),
                is_deleted=False,
            ).select_related("shift")
            for assignment in all_active_period_assignments:
                if assignment.pk in mutable_assignment_ids:
                    continue
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
            weekly_teams = {}
            weekly_requireds = {}
            weekly_category_teams = {}

            def rotation_order(shift):
                normalized_name = "".join(character for character in shift.name.casefold() if character.isalnum())
                if "midnight" in normalized_name or shift.start_time == time(0, 0):
                    rank = 0
                elif "evening" in normalized_name or "afternoon" in normalized_name or shift.start_time.hour >= 16:
                    rank = 1
                elif "day" in normalized_name or "morning" in normalized_name or 6 <= shift.start_time.hour < 16:
                    rank = 2
                else:
                    rank = 3
                return rank, shift.start_time, shift.name.casefold(), shift.pk

            section_shifts = [
                shift for shift in shifts
                if (shift.pk, section.pk) in requirement_map
            ]
            rotation_shifts = sorted(section_shifts, key=rotation_order)
            rotation_enabled = len(rotation_shifts) > 2
            rotation_source_by_target = {
                target_shift.pk: rotation_shifts[(index - 1) % len(rotation_shifts)].pk
                for index, target_shift in enumerate(rotation_shifts)
            } if rotation_enabled else {}
            rotation_previous_week_bounds = {}
            for week_index, (week_start, _week_end) in enumerate(roster_weeks):
                if week_index:
                    rotation_previous_week_bounds[week_start] = roster_weeks[week_index - 1]
                else:
                    previous_date = week_start - timedelta(days=1)
                    rotation_previous_week_bounds[week_start] = MonthlyDutyRoster.get_week_bounds(previous_date)

            def dates_in_week(week_start, week_end):
                dates = []
                cursor = week_start
                while cursor <= week_end:
                    dates.append(cursor)
                    cursor += timedelta(days=1)
                return dates

            def previous_week_crew(week_start, source_shift_id):
                source_bounds = rotation_previous_week_bounds.get(week_start)
                if source_bounds is None:
                    source_bounds = MonthlyDutyRoster.get_week_bounds(week_start - timedelta(days=1))
                source_week_start, source_week_end = source_bounds
                source_key = (source_week_start, source_shift_id)
                if source_key in weekly_teams:
                    source_team = weekly_teams[source_key]
                    if source_team is None:
                        raise ValidationError(
                            "The previous week's source crew is not a valid complete team; "
                            "reconcile that week before rotating crews."
                        )
                    return set(source_team) or None

                source_roster = MonthlyDutyRoster.objects.filter(
                    treatment_plant_id=duty_roster.treatment_plant_id,
                    year=source_week_start.year,
                    month=source_week_start.month,
                ).first()
                if source_roster:
                    persisted_source_team = WeeklyShiftTeam.objects.filter(
                        duty_roster=source_roster,
                        shift_id=source_shift_id,
                        section=section,
                        week_start=source_week_start,
                        week_end=source_week_end,
                    ).first()
                    if persisted_source_team:
                        # The saved team is the source of truth during a new
                        # rotation. This lets generation repair legacy daily
                        # mismatches while still validating the full crew
                        # against destination availability and maximums.
                        return set(persisted_source_team.members.values_list("pk", flat=True)) or None

                source_assignments = ShiftAssignment.objects.filter(
                    shift_id=source_shift_id,
                    staff__section_id=section.pk,
                    staff__assigned_treatment_plant_id=duty_roster.treatment_plant_id,
                    date__range=(source_week_start, source_week_end),
                    is_deleted=False,
                ).values_list("date", "staff_id", "day_type")
                team_by_date = defaultdict(set)
                source_holidays = set()
                for source_date, source_staff_id, source_day_type in source_assignments:
                    team_by_date[source_date].add(source_staff_id)
                    if source_date.weekday() < 5 and source_day_type == ShiftAssignment.DAYTYPE_HOLIDAY:
                        source_holidays.add(source_date)
                source_dates = dates_in_week(source_week_start, source_week_end)
                teams_by_category = defaultdict(set)
                for source_date in source_dates:
                    source_category = (
                        ShiftAssignment.DAYTYPE_WEEKEND
                        if source_date.weekday() >= 5
                        else ShiftAssignment.DAYTYPE_HOLIDAY
                        if source_date in source_holidays
                        else ShiftAssignment.DAYTYPE_WEEKDAY
                    )
                    teams_by_category[source_category].add(
                        frozenset(team_by_date.get(source_date, set()))
                    )
                if any(len(category_teams) > 1 for category_teams in teams_by_category.values()):
                    raise ValidationError(
                        f"The previous week's crew on shift {source_shift_id} differs within a staffing category. "
                        "Reconcile that weekly team before rotating it."
                    )
                source_team = set().union(*(
                    set(next(iter(category_teams)))
                    for category_teams in teams_by_category.values()
                    if category_teams
                )) if teams_by_category else set()
                return source_team or None

            for week_start, week_end in MonthlyDutyRoster.get_weeks_in_month(duty_roster.year,duty_roster.month,):

                # A partial run must still reconcile the entire affected weekly
                # roster block. Otherwise an auto-assignment run on Wednesday
                # would leave Monday and Tuesday under a different team.
                if week_end < assignment_start or week_start > assignment_end:
                    continue
                current = week_start

                while current <= week_end:

                    if current.weekday() >= 5:
                        day_type = ShiftAssignment.DAYTYPE_WEEKEND

                    elif current in holidays:
                        day_type = ShiftAssignment.DAYTYPE_HOLIDAY

                    else:
                        day_type = ShiftAssignment.DAYTYPE_WEEKDAY

                    for shift in shifts:

                        req = requirement_map.get(
                            (shift.id, section.id)
                        )

                        if req is None:
                            continue

                        assigned = existing_by_shift.setdefault(
                            (current, shift.id),
                            set(),
                        )

                        team_key = (week_start, shift.id)
                        if team_key not in weekly_teams:
                            group_dates = dates_in_week(week_start, week_end)
                            group_day_types = {
                                group_date: (
                                    ShiftAssignment.DAYTYPE_WEEKEND
                                    if group_date.weekday() >= 5
                                    else ShiftAssignment.DAYTYPE_HOLIDAY
                                    if group_date in holidays
                                    else ShiftAssignment.DAYTYPE_WEEKDAY
                                )
                                for group_date in group_dates
                            }
                            required = min(
                                req.maximum_allowed,
                                max(
                                    (req.get_required_staff(group_day_types[group_date]) for group_date in group_dates),
                                    default=0,
                                ),
                            )

                            existing_teams = [
                                existing_by_shift.get((group_date, shift.id), set())
                                for group_date in group_dates
                            ]
                            fixed_staff_ids = {
                                assignment.staff_id
                                for assignment in existing
                                if assignment.date in group_dates
                                and assignment.shift_id == shift.id
                                and assignment.pk not in mutable_assignment_ids
                            }
                            common_existing_team = (
                                set.intersection(*(set(team) for team in existing_teams))
                                if existing_teams else set()
                            )
                            mutable_assignment_ids_for_week = {
                                assignment.pk
                                for assignment in existing
                                if assignment.pk in mutable_assignment_ids
                                and assignment.date in group_dates
                            }
                            candidates = []
                            for staff in staff_order:
                                if not group_dates:
                                    continue
                                available_for_group = True
                                for group_date in group_dates:
                                    if group_date in unavailable_days_by_staff.get(staff.id, set()):
                                        available_for_group = False
                                        break
                                    existing_assignment = existing_assignment_lookup.get(
                                        (group_date, shift.id, staff.id)
                                    )
                                    if (
                                        staff.id in assigned_by_date[group_date]
                                        and not existing_assignment
                                    ) or staff.id in planned_by_date[group_date]:
                                        available_for_group = False
                                        break
                                    designated_shifts = weekly_supervisor_shift_by_user_date.get((staff.id, group_date), set())
                                    if designated_shifts and shift.id not in designated_shifts:
                                        available_for_group = False
                                        break
                                    static_designated_shifts = static_supervisor_shifts_by_user.get(staff.id, set())
                                    if static_designated_shifts and any(
                                        designated_shift_id != shift.id for designated_shift_id in static_designated_shifts
                                    ):
                                        available_for_group = False
                                        break
                                    if shift.is_overnight:
                                        next_day = group_date + timedelta(days=1)
                                        next_day_designations = weekly_supervisor_shift_by_user_date.get((staff.id, next_day), set())
                                        if next_day_designations and shift.id not in next_day_designations:
                                            available_for_group = False
                                            break
                                        if (
                                            staff.id in assigned_by_date[next_day]
                                            or staff.id in planned_by_date[next_day]
                                        ):
                                            available_for_group = False
                                            break
                                    is_eligible, _reason = shift.is_staff_eligible(
                                        staff,
                                        group_date,
                                        section=section,
                                        exclude_assignment_id=existing_assignment.pk if existing_assignment else None,
                                        exclude_assignment_ids=mutable_assignment_ids_for_week,
                                    )
                                    if not is_eligible:
                                        available_for_group = False
                                        break
                                if available_for_group:
                                    candidates.append(staff)
                            designated_user = weekly_supervisors_by_shift_week.get(
                                (shift.id, week_start)
                            ) or supervisors_by_shift.get(shift.id)
                            candidate_ids = {staff.id for staff in candidates}
                            starts_rotation_sequence = (
                                rotation_start_date is not None
                                and week_start == rotation_start_date
                            )
                            rotating_team = (
                                previous_week_crew(week_start, rotation_source_by_target[shift.pk])
                                if rotation_enabled and not starts_rotation_sequence
                                else None
                            )
                            if (
                                len(fixed_staff_ids) > req.maximum_allowed
                                or not fixed_staff_ids.issubset(candidate_ids)
                            ):
                                if rotating_team is not None:
                                    raise ValidationError(
                                        f"The complete crew for {shift.name} cannot rotate into "
                                        f"{section.name}; a protected or designated assignment conflicts."
                                    )
                                # Supervisory duties attached to these assignments
                                # cannot be safely moved into one weekly team.
                                weekly_teams[team_key] = None
                                weekly_requireds[team_key] = required
                                continue
                            if rotating_team is not None:
                                unavailable_rotating_staff = rotating_team - candidate_ids
                                if unavailable_rotating_staff:
                                    raise ValidationError(
                                        f"The complete crew from the previous week cannot move together "
                                        f"to {shift.name}; staff IDs {', '.join(map(str, sorted(unavailable_rotating_staff)))} "
                                        "are unavailable or conflict with this shift."
                                    )
                                selected_ids = rotating_team | fixed_staff_ids
                                if len(selected_ids) > req.maximum_allowed:
                                    raise ValidationError(
                                        f"The complete incoming crew exceeds the maximum of "
                                        f"{req.maximum_allowed} staff for {shift.name} in {section.name}."
                                    )
                                if len(selected_ids) < required:
                                    fill_candidates = sorted(
                                        (staff for staff in candidates if staff.id not in selected_ids),
                                        key=lambda staff: (
                                            0 if designated_user and staff.id == designated_user.pk else 1,
                                            0 if staff.id in common_existing_team else 1,
                                            staff_load[staff.id],
                                            (staff.id + week_start.day) % len(staff_list),
                                        ),
                                    )
                                    needed = required - len(selected_ids)
                                    if len(fill_candidates) < needed:
                                        raise ValidationError(
                                            f"The incoming crew for {shift.name} can stay together, but only "
                                            f"{len(selected_ids) + len(fill_candidates)} eligible staff are available; "
                                            f"{required} are required in {section.name}."
                                        )
                                    selected_ids.update(staff.id for staff in fill_candidates[:needed])
                                required = max(required, len(selected_ids))
                            else:
                                required = min(
                                    req.maximum_allowed,
                                    max(required, len(fixed_staff_ids)),
                                )
                                candidates.sort(key=lambda staff: (
                                    0 if staff.id in fixed_staff_ids else 1,
                                    0 if designated_user and staff.id == designated_user.pk else 1,
                                    0 if staff.id in common_existing_team else 1,
                                    staff_load[staff.id],
                                    (staff.id + week_start.day) % len(staff_list),
                                ))
                                selected_ids = {staff.id for staff in candidates[:required]}
                            weekly_requireds[team_key] = required
                            weekly_teams[team_key] = fixed_staff_ids | selected_ids
                            team = weekly_teams[team_key]
                            category_teams = {}
                            for category in (
                                ShiftAssignment.DAYTYPE_WEEKDAY,
                                ShiftAssignment.DAYTYPE_WEEKEND,
                                ShiftAssignment.DAYTYPE_HOLIDAY,
                            ):
                                category_dates = [
                                    group_date for group_date in group_dates
                                    if group_day_types[group_date] == category
                                ]
                                if not category_dates:
                                    category_teams[category] = set()
                                    continue

                                category_required = req.get_required_staff(category)
                                category_existing_teams = [
                                    existing_by_shift.get((group_date, shift.id), set())
                                    for group_date in category_dates
                                ]
                                category_common_existing = (
                                    set.intersection(*(set(items) for items in category_existing_teams))
                                    if category_existing_teams else set()
                                )
                                category_fixed_ids = {
                                    assignment.staff_id
                                    for assignment in existing
                                    if assignment.date in category_dates
                                    and assignment.shift_id == shift.id
                                    and assignment.pk not in mutable_assignment_ids
                                }
                                category_cap = (
                                    category_required
                                    if category in {
                                        ShiftAssignment.DAYTYPE_WEEKEND,
                                        ShiftAssignment.DAYTYPE_HOLIDAY,
                                    }
                                    else req.maximum_allowed
                                )
                                category_target = (
                                    category_required
                                    if category in {
                                        ShiftAssignment.DAYTYPE_WEEKEND,
                                        ShiftAssignment.DAYTYPE_HOLIDAY,
                                    }
                                    else max(
                                        len(team) if category_required else 0,
                                        len(category_fixed_ids),
                                    )
                                )
                                if len(category_fixed_ids) > category_cap:
                                    raise ValidationError(
                                        f"Protected assignments leave {len(category_fixed_ids)} staff on "
                                        f"{shift.name} for {category}, above its limit of {category_cap}."
                                    )
                                if category_target > len(team):
                                    raise ValidationError(
                                        f"The weekly crew for {shift.name} is too small to meet its "
                                        f"{category} requirement of {category_target}."
                                    )
                                category_ordered_ids = sorted(
                                    team,
                                    key=lambda staff_id: (
                                        0 if staff_id in category_fixed_ids else 1,
                                        0 if designated_user and staff_id == designated_user.pk else 1,
                                        0 if staff_id in category_common_existing else 1,
                                        staff_load[staff_id],
                                        (staff_id + week_start.day) % len(staff_list),
                                    ),
                                )
                                category_team = set(category_fixed_ids)
                                category_team.update(
                                    staff_id for staff_id in category_ordered_ids
                                    if staff_id in category_common_existing
                                    and len(category_team) < category_target
                                )
                                category_team.update(
                                    staff_id for staff_id in category_ordered_ids
                                    if len(category_team) < category_target
                                )
                                if len(category_team) != category_target:
                                    raise ValidationError(
                                        f"Could not select exactly {category_target} staff for the "
                                        f"{category} requirement on {shift.name}."
                                    )
                                category_teams[category] = category_team

                            assigned_weekly_pool = set().union(*category_teams.values())
                            if assigned_weekly_pool != team:
                                raise ValidationError(
                                    f"The complete weekly crew for {shift.name} cannot be placed across "
                                    f"the {section.name} staffing categories without leaving staff unused."
                                )
                            weekly_category_teams[team_key] = category_teams
                            for group_date in group_dates:
                                daily_team = category_teams[group_day_types[group_date]]
                                planned_by_date[group_date].update(daily_team)
                                if shift.is_overnight:
                                    planned_by_date[group_date + timedelta(days=1)].update(daily_team)

                        required = weekly_requireds[team_key]
                        team = weekly_teams[team_key]
                        if team is None:
                            continue
                        daily_team = weekly_category_teams[team_key].get(day_type, set())
                        # Keep one stable crew per day type; Saturday and Sunday share one crew.
                        extras = [
                            assignment for assignment in existing
                            if assignment.date == current
                            and assignment.shift_id == shift.id
                            and assignment.staff_id not in daily_team
                            and assignment.pk in mutable_assignment_ids
                        ]
                        for extra in extras:
                            try:
                                extra._weekly_team_change_allowed = True
                                extra.delete()
                            except ValidationError as error:
                                raise ValidationError(
                                    f"Could not reduce {shift.name} to its {day_type} staffing limit: "
                                    f"{'; '.join(error.messages)}"
                                )
                            finally:
                                if hasattr(extra, "_weekly_team_change_allowed"):
                                    del extra._weekly_team_change_allowed
                            assigned.discard(extra.staff_id)
                            staff_load[extra.staff_id] = max(0, staff_load[extra.staff_id] - 1)
                            existing_assignment_lookup.pop((current, shift.id, extra.staff_id), None)
                            existing.remove(extra)

                        for kept in existing:
                            if (
                                kept.date == current
                                and kept.shift_id == shift.id
                                and kept.staff_id in daily_team
                                and kept.day_type != day_type
                            ):
                                kept.day_type = day_type
                                kept.save(update_fields=("day_type",))

                        for staff_id in daily_team - assigned:
                            staff = staff_lookup[staff_id]
                            new_assignments.append(ShiftAssignment(
                                duty_roster=duty_roster,
                                shift=shift,
                                staff=staff,
                                date=current,
                                day_type=day_type,
                            ))
                            assigned.add(staff_id)
                            assigned_by_date[current].add(staff_id)
                            if shift.is_overnight:
                                assigned_by_date[current + timedelta(days=1)].add(staff_id)
                            staff_load[staff_id] += 1

                    current += timedelta(days=1)

            weekly_team_records = []
            for (week_start, shift_id), staff_ids in weekly_teams.items():
                if staff_ids is None:
                    continue
                shift = next((item for item in shifts if item.pk == shift_id), None)
                if not shift:
                    continue
                weekly_team, _created = WeeklyShiftTeam.get_for_date(
                    duty_roster=duty_roster,
                    shift=shift,
                    section=section,
                    target_date=week_start,
                    create=True,
                )
                weekly_team.replace_staff_ids(staff_ids, reconciled=False)
                weekly_team_records.append(weekly_team)

            if new_assignments:
                for assignment in new_assignments:
                    assignment.save()

            for weekly_team in weekly_team_records:
                weekly_team.refresh_reconciliation()

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
                    date__lte=assignment_end,
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


class WeeklyShiftTeam(models.Model):
    """The canonical staff crew for one shift and section in a roster week."""

    duty_roster = models.ForeignKey(
        MonthlyDutyRoster,
        on_delete=models.CASCADE,
        related_name="weekly_teams",
    )
    shift = models.ForeignKey(
        Shift,
        on_delete=models.CASCADE,
        related_name="weekly_teams",
    )
    section = models.ForeignKey(
        "core.Section",
        on_delete=models.CASCADE,
        related_name="weekly_shift_teams",
    )
    week_start = models.DateField()
    week_end = models.DateField()
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="WeeklyShiftTeamMember",
        related_name="weekly_shift_teams",
    )
    is_reconciled = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("week_start", "shift__start_time", "section__name")
        constraints = [
            models.UniqueConstraint(
                fields=("duty_roster", "shift", "section", "week_start"),
                name="unique_weekly_shift_team_scope",
            ),
            models.CheckConstraint(
                condition=models.Q(week_end__gte=models.F("week_start")),
                name="weekly_shift_team_valid_date_range",
            ),
        ]

    def __str__(self):
        return f"{self.section} · {self.shift} · {self.week_start:%d %b}–{self.week_end:%d %b %Y}"

    def clean(self):
        super().clean()
        errors = {}
        if self.duty_roster_id and self.shift_id:
            if self.duty_roster.treatment_plant_id != self.shift.treatment_plant_id:
                errors["shift"] = "The weekly team and roster must belong to the same treatment plant."
            if self.week_start and self.week_end:
                if (
                    self.week_start.month != self.duty_roster.month
                    or self.week_start.year != self.duty_roster.year
                    or self.week_end.month != self.duty_roster.month
                    or self.week_end.year != self.duty_roster.year
                ):
                    errors["week_start"] = "The weekly team dates must fall within the roster month."
                else:
                    expected_bounds = MonthlyDutyRoster.get_week_bounds(self.week_start)
                    if expected_bounds != (self.week_start, self.week_end):
                        errors["week_start"] = "The team dates must match one complete roster week."
            if self.section_id and not section_belongs_to_plant(
                self.section,
                self.duty_roster.treatment_plant_id,
            ):
                errors["section"] = "The weekly team section must belong to the roster's treatment plant."
            if self.section_id and not ShiftSectionRequirement.objects.filter(
                shift_id=self.shift_id,
                section_id=self.section_id,
                section__is_active=True,
            ).exists():
                errors["section"] = "The shift must have an active requirement for this section."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    @classmethod
    def get_for_date(cls, *, duty_roster, shift, section, target_date, create=False):
        week_start, week_end = MonthlyDutyRoster.get_week_bounds(target_date)
        scope = {
            "duty_roster": duty_roster,
            "shift": shift,
            "section": section,
            "week_start": week_start,
            "week_end": week_end,
        }
        if not create:
            return cls.objects.filter(**scope).first(), False
        team, created = cls.objects.get_or_create(
            duty_roster=duty_roster,
            shift=shift,
            section=section,
            week_start=week_start,
            defaults={"week_end": week_end},
        )
        if team.week_end != week_end:
            raise ValidationError("The saved weekly team has invalid roster date bounds.")
        return team, created

    @transaction.atomic
    def replace_staff_ids(self, staff_ids, *, reconciled=False, enforce_maximum=True):
        """Set the planned team while preventing assignments from escaping it."""
        if not self.pk:
            self.save()
        self = type(self).objects.select_for_update().get(pk=self.pk)
        staff_ids = {int(staff_id) for staff_id in staff_ids}
        requirement = ShiftSectionRequirement.objects.filter(
            shift_id=self.shift_id,
            section_id=self.section_id,
            section__is_active=True,
        ).first()
        if not requirement:
            raise ValidationError("This shift is not configured for the weekly team's section.")
        if enforce_maximum and len(staff_ids) > requirement.maximum_allowed:
            raise ValidationError(
                f"A weekly team cannot exceed the maximum of {requirement.maximum_allowed} staff."
            )
        staff_by_id = User.objects.filter(
            pk__in=staff_ids,
            is_active=True,
            assigned_treatment_plant_id=self.duty_roster.treatment_plant_id,
            section_id=self.section_id,
        ).in_bulk()
        if len(staff_by_id) != len(staff_ids):
            raise ValidationError("Every weekly team member must be active in this plant and section.")
        incharge_staff_ids = set(User.objects.filter(
            pk__in=staff_ids,
            groups__name__in=INCHARGE_GROUPS,
        ).values_list("pk", flat=True).distinct())
        if incharge_staff_ids:
            raise ValidationError("In-charge staff are exempt from routine weekly shift teams.")

        orphaned_assignment = ShiftAssignment.objects.filter(
            duty_roster_id=self.duty_roster_id,
            shift_id=self.shift_id,
            staff__section_id=self.section_id,
            date__range=(self.week_start, self.week_end),
            is_deleted=False,
        ).exclude(staff_id__in=staff_ids).exists()
        if orphaned_assignment:
            raise ValidationError(
                "Remove or move every assignment outside the replacement weekly team before changing its membership."
            )

        membership_model = self.members.through
        membership_model.objects.filter(team_id=self.pk).exclude(staff_id__in=staff_ids).delete()
        existing_ids = set(membership_model.objects.filter(team_id=self.pk).values_list("staff_id", flat=True))
        membership_model.objects.bulk_create([
            membership_model(team=self, staff=staff_by_id[staff_id])
            for staff_id in sorted(staff_ids - existing_ids)
        ])
        self.is_reconciled = bool(reconciled)
        self.save(update_fields=("is_reconciled", "updated_at"))
        return self

    @transaction.atomic
    def refresh_reconciliation(self):
        self = type(self).objects.select_for_update().get(pk=self.pk)
        dates = []
        current = self.week_start
        while current <= self.week_end:
            dates.append(current)
            current += timedelta(days=1)
        members = set(self.members.values_list("pk", flat=True))
        holiday_dates = set(ShiftAssignment.objects.filter(
            duty_roster_id=self.duty_roster_id,
            staff__section_id=self.section_id,
            date__range=(self.week_start, self.week_end),
            day_type=ShiftAssignment.DAYTYPE_HOLIDAY,
            is_deleted=False,
        ).values_list("date", flat=True))
        assignments_by_date = defaultdict(set)
        for assignment_date, staff_id in ShiftAssignment.objects.filter(
            duty_roster_id=self.duty_roster_id,
            shift_id=self.shift_id,
            staff__section_id=self.section_id,
            date__range=(self.week_start, self.week_end),
            is_deleted=False,
        ).values_list("date", "staff_id"):
            assignments_by_date[assignment_date].add(staff_id)
        requirement = ShiftSectionRequirement.objects.filter(
            shift_id=self.shift_id,
            section_id=self.section_id,
            section__is_active=True,
        ).first()
        category_dates = {
            ShiftAssignment.DAYTYPE_WEEKDAY: [
                roster_date for roster_date in dates
                if roster_date.weekday() < 5 and roster_date not in holiday_dates
            ],
            ShiftAssignment.DAYTYPE_WEEKEND: [
                roster_date for roster_date in dates if roster_date.weekday() >= 5
            ],
            ShiftAssignment.DAYTYPE_HOLIDAY: [
                roster_date for roster_date in dates
                if roster_date.weekday() < 5 and roster_date in holiday_dates
            ],
        }
        category_crews = {}
        reconciled = bool(dates) and requirement is not None
        for category, category_days in category_dates.items():
            daily_crews = [frozenset(assignments_by_date.get(roster_date, set())) for roster_date in category_days]
            if len(set(daily_crews)) > 1:
                reconciled = False
            crew = set(daily_crews[0]) if daily_crews else set()
            category_crews[category] = crew
            if requirement and category_days:
                required = requirement.get_required_staff(category)
                if category in {ShiftAssignment.DAYTYPE_WEEKEND, ShiftAssignment.DAYTYPE_HOLIDAY}:
                    reconciled = reconciled and len(crew) == required
                else:
                    reconciled = reconciled and required <= len(crew) <= requirement.maximum_allowed
        union_crews = set().union(*category_crews.values()) if category_crews else set()
        reconciled = reconciled and union_crews == members
        if requirement and any(
            len(assignments_by_date.get(roster_date, set())) > requirement.maximum_allowed
            for roster_date in dates
        ):
            reconciled = False
        self.is_reconciled = reconciled
        self.save(update_fields=("is_reconciled", "updated_at"))
        return self.is_reconciled


class WeeklyShiftTeamMember(models.Model):
    team = models.ForeignKey(
        WeeklyShiftTeam,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="weekly_shift_team_memberships",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("team", "staff"),
                name="unique_weekly_shift_team_member",
            ),
        ]

    def clean(self):
        super().clean()
        if self.team_id and self.staff_id:
            if (
                not self.staff.is_active
                or self.staff.assigned_treatment_plant_id != self.team.duty_roster.treatment_plant_id
                or self.staff.section_id != self.team.section_id
            ):
                raise ValidationError({"staff": "A team member must be active in the team's plant and section."})
            if is_user_incharge(self.staff, section_id=self.team.section_id):
                raise ValidationError({"staff": "In-charge staff are exempt from routine weekly shift teams."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


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
