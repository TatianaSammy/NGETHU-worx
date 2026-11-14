# sc/services/shift_service.py
from __future__ import annotations

import logging
from datetime import datetime, date, time, timedelta
from typing import Optional, Dict, Any, List

from django.utils import timezone
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from core.plant_section_scope import scoped_queryset, user_plant_section

from t.models import TreatmentPlant
from sc.models import (
    AnnualLeave,
    Shift,
    ShiftHandover,
    ShiftAssignment,
    ShiftSectionRequirement,
    ShiftSupervisor,
    MonthlyDutyRoster,
    Apparatus,
    ApparatusCondition
)

logger = logging.getLogger(__name__)
User = get_user_model()


def ensure_default_shifts(plant: TreatmentPlant) -> List[Shift]:
    """
    Ensure standard utility shifts exist for the given treatment plant:
      1. Morning Shift:   06:00 - 14:00 (Handover: 13:45, Takeover: 14:00)
      2. Afternoon Shift: 14:00 - 22:00 (Handover: 21:45, Takeover: 22:00)
      3. Night Shift:     22:00 - 06:00 (Handover: 05:45, Takeover: 06:00)
    """
    existing = list(Shift.objects.filter(treatment_plant=plant, is_deleted=False))
    if existing:
        return existing

    defaults = [
        {
            "name": "Morning Shift",
            "start_time": time(6, 0),
            "end_time": time(14, 0),
            "handover_time": time(13, 45),
            "takeover_time": time(14, 0),
        },
        {
            "name": "Afternoon Shift",
            "start_time": time(14, 0),
            "end_time": time(22, 0),
            "handover_time": time(21, 45),
            "takeover_time": time(22, 0),
        },
        {
            "name": "Night Shift",
            "start_time": time(22, 0),
            "end_time": time(6, 0),
            "handover_time": time(5, 45),
            "takeover_time": time(6, 0),
        },
    ]

    created_shifts = []
    for data in defaults:
        shift, _ = Shift.objects.get_or_create(
            treatment_plant=plant,
            name=data["name"],
            defaults={
                "start_time": data["start_time"],
                "end_time": data["end_time"],
                "handover_time": data["handover_time"],
                "takeover_time": data["takeover_time"],
                "is_deleted": False,
            }
        )
        created_shifts.append(shift)

    logger.info("Created default shifts for plant %s", plant.name)
    return created_shifts


def get_next_shift(current_shift: Shift, plant: TreatmentPlant, section_id=None) -> Optional[Shift]:
    """
    Finds the next shift following the current shift in rotation.
    """
    shifts_qs = Shift.objects.filter(treatment_plant=plant, is_deleted=False)
    if section_id:
        shifts_qs = shifts_qs.filter(section_requirements__section_id=section_id).distinct()
    shifts = list(shifts_qs.order_by("start_time"))
    if not shifts:
        return None

    try:
        idx = shifts.index(current_shift)
        return shifts[(idx + 1) % len(shifts)]
    except ValueError:
        # Fallback by start_time
        for s in shifts:
            if s.start_time >= current_shift.end_time:
                return s
        return shifts[0]


def check_handover_window(shift: Shift, now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Determines if current time is within ±15 minutes of shift handover time.
    """
    now = now or timezone.localtime()
    current_date = now.date()

    expected_handover_time = shift.handover_time or shift.end_time
    expected_takeover_time = shift.takeover_time or shift.end_time

    # Construct expected datetime
    expected_dt = datetime.combine(current_date, expected_handover_time)
    if shift.is_overnight and expected_handover_time < shift.start_time:
        # Expected handover is on next day
        expected_dt += timedelta(days=1)

    expected_dt = timezone.make_aware(expected_dt, timezone.get_current_timezone())

    window_start = expected_dt - timedelta(minutes=15)
    window_end = expected_dt + timedelta(minutes=15)

    is_active = window_start <= now <= window_end

    return {
        "is_active": is_active,
        "window_start": window_start,
        "window_end": window_end,
        "window_display": f"{window_start.strftime('%H:%M')} – {window_end.strftime('%H:%M')}",
        "expected_handover_time": expected_handover_time.strftime("%H:%M"),
        "expected_takeover_time": expected_takeover_time.strftime("%H:%M"),
    }


def get_current_shift_data(user, request=None) -> Dict[str, Any]:
    """
    Returns complete shift telemetry and supervisor info for the active user session.
    """
    # 1. Resolve Treatment Plant
    is_superuser = bool(getattr(user, "is_superuser", False))
    plant_id, section_id = user_plant_section(user)
    plant = getattr(user, "assigned_treatment_plant", None)
    if not plant and is_superuser:
        plant = TreatmentPlant.objects.filter(code="NGH").first() or TreatmentPlant.objects.first()
    elif not plant and plant_id:
        plant = TreatmentPlant.objects.filter(pk=plant_id).first()

    if not plant or (not is_superuser and not section_id):
        return {
            "plant": None,
            "current_shift": None,
            "next_shift": None,
            "supervisor": None,
            "is_handover_window_active": False,
            "handover_window_time": "",
        }

    # 2. Ensure shifts exist
    shifts = ensure_default_shifts(plant) if is_superuser else list(
        Shift.objects.filter(treatment_plant=plant, is_deleted=False).order_by("start_time")
    )
    if not is_superuser:
        shifts = [
            shift for shift in shifts
            if ShiftSectionRequirement.objects.filter(shift_id=shift.pk, section_id=section_id).exists()
        ]

    # 3. Get current shift
    now = timezone.localtime()
    current_shift = Shift.get_shift_for_time(now, treatment_plant=plant)
    if not is_superuser and current_shift not in shifts:
        current_shift = None
    if not current_shift and shifts:
        current_shift = shifts[0]

    # 4. Get next shift in rotation
    next_shift = get_next_shift(current_shift, plant, None if is_superuser else section_id) if current_shift else None

    # 5. Resolve active Shift Supervisor
    supervisor = None
    unavailable_ids = set(AnnualLeave.objects.filter(
        status__in=AnnualLeave.UNAVAILABLE_STATUSES,
        start_date__lte=now.date(),
        end_date__gte=now.date(),
    ).values_list("staff_id", flat=True))
    if current_shift:
        # Check if handover already occurred today
        latest_handover = (
            scoped_queryset(ShiftHandover.objects
            .filter(shift=current_shift, date=now.date())
            .order_by("-handed_over_at"), user)
            .first()
        )
        if latest_handover and latest_handover.is_taken_over and latest_handover.incoming_supervisor:
            supervisor = latest_handover.incoming_supervisor
        elif latest_handover and latest_handover.outgoing_supervisor:
            supervisor = latest_handover.outgoing_supervisor

        if supervisor and AnnualLeave.is_unavailable(supervisor.pk, now.date()):
            supervisor = None

        if not supervisor:
            # Check shift assignment for today
            assignment_queryset = scoped_queryset(
                ShiftAssignment.objects.filter(
                    shift=current_shift,
                    date=now.date(),
                    is_deleted=False,
                ).filter(Q(supervisor__isnull=False) | Q(temp_supervisor__isnull=False)),
                user,
            )
            assignment = assignment_queryset.first()
            if assignment and assignment.temp_supervisor_id:
                if (
                    assignment.date == now.date()
                    and assignment.temp_supervisor_start
                    and assignment.temp_supervisor_end
                    and assignment.temp_supervisor_start <= now <= assignment.temp_supervisor_end
                ):
                    supervisor = assignment.effective_supervisor
                if supervisor and AnnualLeave.is_unavailable(supervisor.pk, now.date()):
                    supervisor = None

        if not supervisor:
            supervisor = ShiftSupervisor.get_active_supervisor(
                treatment_plant_id=plant.pk,
                section_id=None if is_superuser else section_id,
                shift_id=current_shift.pk,
                assignment_date=now.date(),
            )
            if supervisor and supervisor.pk in unavailable_ids:
                supervisor = None

    # 6. Handover window check
    window_data = check_handover_window(current_shift, now) if current_shift else {"is_active": False, "window_display": ""}

    is_window_active = window_data["is_active"]
    # Allow superusers / staff to initiate handover for testing/admin purposes
    if is_superuser:
        is_window_active = True

    return {
        "plant": plant,
        "current_shift": current_shift,
        "next_shift": next_shift,
        "supervisor": supervisor,
        "is_handover_window_active": is_window_active,
        "handover_window_time": window_data.get("window_display", ""),
        "window_data": window_data,
    }


def user_belongs_to_shift(user, shift: Shift, target_date: Optional[date] = None) -> bool:
    """
    Checks if a user belongs to the specified shift.
    Superusers automatically have cross-shift authorization.
    """
    target_date = target_date or timezone.localdate()
    if AnnualLeave.is_unavailable(user.pk, target_date):
        return False
    if getattr(user, "is_superuser", False):
        return True

    if (
        not user.section_id
        or user.assigned_treatment_plant_id != shift.treatment_plant_id
        or not ShiftSectionRequirement.objects.filter(shift_id=shift.pk, section_id=user.section_id).exists()
    ):
        return False
    designated = ShiftSupervisor.get_active_supervisor(
        treatment_plant_id=shift.treatment_plant_id,
        section_id=user.section_id,
        shift_id=shift.pk,
        assignment_date=target_date,
    )
    if designated and designated.pk == user.pk:
        return True
    now = timezone.now()
    return ShiftAssignment.objects.filter(
        shift_id=shift.pk,
        date=target_date,
        staff__section_id=user.section_id,
        is_deleted=False,
        temp_supervisor_id=user.pk,
        temp_supervisor_start__lte=now,
        temp_supervisor_end__gte=now,
    ).exists()


def get_eligible_supervisors_for_shift(shift: Shift, target_date: Optional[date] = None, section_id=None, allow_cross_scope=False) -> List[User]:
    """
    Returns users who belong to the target shift and can take over or hand over.
    """
    target_date = target_date or timezone.localdate()
    if not section_id and not allow_cross_scope:
        return []
    unavailable_ids = set(AnnualLeave.objects.filter(
        status__in=AnnualLeave.UNAVAILABLE_STATUSES,
        start_date__lte=target_date,
        end_date__gte=target_date,
    ).values_list("staff_id", flat=True))

    if section_id:
        active_supervisor = ShiftSupervisor.get_active_supervisor(
            shift.treatment_plant_id,
            section_id=section_id,
            shift_id=shift.pk,
            assignment_date=target_date,
        )
        eligible = {active_supervisor} if active_supervisor and active_supervisor.is_active and active_supervisor.pk not in unavailable_ids else set()
        temporary_ids = ShiftAssignment.objects.filter(
            shift_id=shift.pk,
            date=target_date,
            staff__section_id=section_id,
            is_deleted=False,
            temp_supervisor__isnull=False,
            temp_supervisor_start__lte=timezone.now(),
            temp_supervisor_end__gte=timezone.now(),
        ).values_list("temp_supervisor_id", flat=True)
        eligible.update(User.objects.filter(pk__in=temporary_ids, is_active=True).exclude(pk__in=unavailable_ids))
        return sorted(eligible, key=lambda user: (not user.is_superuser, user.get_full_name() or user.username))

    designations = ShiftSupervisor.objects.filter(
        treatment_plant=shift.treatment_plant,
        shift=shift,
        is_active=True,
        user__is_active=True,
    ).exclude(user_id__in=unavailable_ids).select_related("user", "section")
    if section_id:
        designations = designations.filter(section_id=section_id)
    week_start, week_end = MonthlyDutyRoster.get_week_bounds(target_date)
    weekly_by_section = {
        designation.section_id: designation.user
        for designation in ShiftSupervisor.objects.filter(
            treatment_plant_id=shift.treatment_plant_id,
            shift_id=shift.pk,
            week_start=week_start,
            week_end=week_end,
            week_start__isnull=False,
            user__is_active=True,
        ).select_related("user")
    }
    eligible = set()
    static_by_section = {designation.section_id: designation for designation in designations}
    for supervisor_section_id in set(static_by_section) | set(weekly_by_section):
        designation = static_by_section.get(supervisor_section_id)
        candidate = weekly_by_section.get(supervisor_section_id) or (designation.user if designation else None)
        if candidate and candidate.pk not in unavailable_ids and (
            candidate.is_superuser
            or (
                candidate.assigned_treatment_plant_id == shift.treatment_plant_id
                and candidate.section_id == supervisor_section_id
            )
        ):
            eligible.add(candidate)

    temporary_ids = ShiftAssignment.objects.filter(
        shift_id=shift.pk,
        date=target_date,
        is_deleted=False,
        temp_supervisor__is_active=True,
        temp_supervisor_start__lte=timezone.now(),
        temp_supervisor_end__gte=timezone.now(),
    ).values_list("temp_supervisor_id", flat=True)
    eligible.update(User.objects.filter(pk__in=temporary_ids).exclude(pk__in=unavailable_ids))
    return sorted(eligible, key=lambda user: (not user.is_superuser, user.get_full_name() or user.username))


def seed_default_apparatus_if_needed(plant: TreatmentPlant, section_id=None) -> List[Apparatus]:
    """
    Ensures baseline equipment and apparatus exist for the handover checklist.
    """
    from core.models import Section
    from inventory.models import Category, Equipment

    existing_qs = Apparatus.objects.filter(equipment__section__treatment_plant=plant)
    if section_id:
        existing_qs = existing_qs.filter(equipment__section_id=section_id)
    existing = list(existing_qs)
    if existing:
        return existing

    section_qs = Section.objects.filter(treatment_plant=plant)
    if section_id:
        section_qs = section_qs.filter(pk=section_id)
    section = section_qs.order_by("name").first()
    if not section:
        return []

    category, _ = Category.objects.get_or_create(
        name="Laboratory & Operations Apparatus",
        defaults={"description": "Critical plant analytical equipment and instrumentation"}
    )

    items = [
        {"name": "Laboratory Turbidimeter", "desc": "Benchtop turbidimeter for settled & treated water compliance"},
        {"name": "Digital pH / Temperature Meter", "desc": "Calibrated dual-electrode pH/Temp sensor for raw & coagulated water"},
        {"name": "Chlorine Residual Photometer", "desc": "DPD colorimetric online/benchtop free and total chlorine analyzer"},
        {"name": "Jar Test Flocculator Unit", "desc": "6-paddle automated jar tester for coagulant dose optimization"},
        {"name": "Online Filter Turbidity Monitor", "desc": "Continuous Nephelometric monitor for rapid sand filter bank"},
    ]

    created = []
    for i, it in enumerate(items, 1):
        eq, _ = Equipment.objects.get_or_create(
            serial_number=f"APP-{plant.code or 'NGH'}-{i:03d}",
            defaults={
                "name": it["name"],
                "category": category,
                "section": section,
                "next_calibration_date": timezone.localdate() + timedelta(days=90),
                "next_maintenance_date": timezone.localdate() + timedelta(days=60),
            }
        )
        app, _ = Apparatus.objects.get_or_create(
            name=it["name"],
            defaults={
                "equipment": eq,
                "description": it["desc"]
            }
        )
        created.append(app)

    logger.info("Seeded %d apparatus units for handover tracking.", len(created))
    return created
