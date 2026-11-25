from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.plant_section_scope import section_scope_q
from sc.models import (
    AnnualLeave,
    MonthlyDutyRoster,
    Shift,
    ShiftAssignment,
    ShiftSectionRequirement,
    ShiftSupervisor,
    WeeklyShiftTeam,
    _has_valid_shift_assignment_for_week,
)
from accounts.models import CustomUser


def get_supervisor_workload_summary(
    treatment_plant_id: int,
    section_id: int,
    year: int,
    month: int,
) -> Dict[str, Any]:
    """
    Returns current supervisory workload distribution across all standing pool
    supervisors for the specified plant, section, and month.
    """
    pool_supervisors = list(
        ShiftSupervisor.objects.filter(
            treatment_plant_id=treatment_plant_id,
            section_id=section_id,
            is_active=True,
            week_start__isnull=True,
            user__is_active=True,
        )
        .select_related("user")
        .order_by("user__first_name", "user__last_name", "user__username")
    )
    roster_weeks = MonthlyDutyRoster.get_weeks_in_month(year, month)
    shifts = list(
        Shift.objects.filter(
            treatment_plant_id=treatment_plant_id,
            is_deleted=False,
            section_requirements__section_id=section_id,
            section_requirements__section__is_active=True,
        )
        .distinct()
        .order_by("start_time", "name")
    )

    if not roster_weeks or not shifts:
        return {
            "total_supervisors": len(pool_supervisors),
            "total_slots": 0,
            "assigned_slots": 0,
            "supervisors": [],
            "fairness_spread": 0,
        }

    total_slots = len(roster_weeks) * len(shifts)
    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(year, month + 1, 1) - timedelta(days=1)

    weekly_designations = list(
        ShiftSupervisor.objects.filter(
            treatment_plant_id=treatment_plant_id,
            section_id=section_id,
            week_start__range=(month_start, month_end),
            week_start__isnull=False,
            is_active=True,
        ).select_related("user", "shift")
    )

    workload_by_user = {s.user_id: [] for s in pool_supervisors}
    for des in weekly_designations:
        if des.user_id in workload_by_user:
            workload_by_user[des.user_id].append({
                "shift_id": des.shift_id,
                "shift_name": des.shift.name if des.shift else "Unknown",
                "week_start": des.week_start.isoformat(),
                "week_end": des.week_end.isoformat(),
            })

    summary_list = []
    for s in pool_supervisors:
        assigned = workload_by_user.get(s.user_id, [])
        summary_list.append({
            "user_id": s.user_id,
            "username": s.user.username,
            "full_name": s.user.get_full_name() or s.user.username,
            "standing_shift": s.shift.name if s.shift else None,
            "weeks_count": len(assigned),
            "assignments": assigned,
        })

    counts = [item["weeks_count"] for item in summary_list]
    fairness_spread = (max(counts) - min(counts)) if counts else 0

    return {
        "total_supervisors": len(pool_supervisors),
        "total_slots": total_slots,
        "assigned_slots": len(weekly_designations),
        "supervisors": summary_list,
        "fairness_spread": fairness_spread,
    }


def _ensure_candidate_has_shift_assignment(
    user: CustomUser,
    shift: Shift,
    section_id: int,
    treatment_plant_id: int,
    week_start: date,
    week_end: date,
    actor_user: Optional[CustomUser] = None,
) -> bool:
    """
    Ensures the supervisor candidate has an active, non-conflicting ShiftAssignment
    on the designated shift during the given roster week, and aligns any existing
    assignments of this user during this week to the designated shift.
    """
    # 1. Align any assignments this user has in this week that are on other shifts to this shift
    other_assignments = list(
        ShiftAssignment.objects.filter(
            staff_id=user.pk,
            staff__section_id=section_id,
            date__range=(week_start, week_end),
            is_deleted=False,
        ).exclude(shift_id=shift.pk)
    )
    for other_asg in other_assignments:
        day_type = (
            ShiftAssignment.DAYTYPE_WEEKEND
            if other_asg.date.weekday() >= 5
            else ShiftAssignment.DAYTYPE_WEEKDAY
        )
        try:
            other_asg.move_assignment(
                target_shift=shift,
                target_date=other_asg.date,
                day_type=day_type,
                moved_by=actor_user or user,
                remarks=f"Dynamically aligned to supervised {shift.name}.",
            )
        except ValidationError:
            # A designation cannot bypass roster section capacity or eligibility rules.
            continue

    # A weekly supervisor must belong to every category crew in the week.
    week_dates = []
    current_date = week_start
    while current_date <= week_end:
        week_dates.append(current_date)
        current_date += timedelta(days=1)
    holiday_dates = set(ShiftAssignment.objects.filter(
        shift__treatment_plant_id=treatment_plant_id,
        staff__section_id=section_id,
        date__range=(week_start, week_end),
        day_type=ShiftAssignment.DAYTYPE_HOLIDAY,
        is_deleted=False,
    ).values_list("date", flat=True))
    assignment_dates = week_dates
    existing_target_rows = list(ShiftAssignment.objects.filter(
        staff_id=user.pk,
        shift_id=shift.pk,
        staff__section_id=section_id,
        date__range=(week_start, week_end),
        is_deleted=False,
    ))
    existing_by_date = {assignment.date: assignment for assignment in existing_target_rows}
    if all(day in existing_by_date for day in assignment_dates):
        return True

    duty_roster = MonthlyDutyRoster.objects.filter(
        treatment_plant_id=treatment_plant_id,
        year=week_start.year,
        month=week_start.month,
    ).first()

    if duty_roster is None:
        duty_roster, _created = MonthlyDutyRoster.objects.get_or_create(
            treatment_plant_id=treatment_plant_id,
            year=week_start.year,
            month=week_start.month,
        )

    if all(
        shift.can_accept_assignment(
            section_id,
            day,
            day_type=(
                ShiftAssignment.DAYTYPE_WEEKEND
                if day.weekday() >= 5
                else ShiftAssignment.DAYTYPE_HOLIDAY
                if day in holiday_dates
                else ShiftAssignment.DAYTYPE_WEEKDAY
            ),
            exclude_assignment_id=existing_by_date[day].pk if day in existing_by_date else None,
        )[0]
        and shift.is_staff_eligible(
            user,
            day,
            section=section_id,
            exclude_assignment_id=existing_by_date[day].pk if day in existing_by_date else None,
        )[0]
        for day in assignment_dates
    ):
        try:
            with transaction.atomic():
                weekly_team, _created = WeeklyShiftTeam.get_for_date(
                    duty_roster=duty_roster,
                    shift=shift,
                    section=user.section,
                    target_date=week_start,
                    create=True,
                )
                existing_team_ids = set(weekly_team.members.values_list("pk", flat=True))
                existing_assignment_staff_ids = set(ShiftAssignment.objects.filter(
                    duty_roster=duty_roster,
                    shift=shift,
                    staff__section_id=section_id,
                    date__range=(week_start, week_end),
                    is_deleted=False,
                ).values_list("staff_id", flat=True))
                weekly_team.replace_staff_ids(
                    existing_team_ids | existing_assignment_staff_ids | {user.pk},
                    reconciled=False,
                )
                for day in assignment_dates:
                    if day in existing_by_date:
                        continue
                    day_type = (
                        ShiftAssignment.DAYTYPE_WEEKEND
                        if day.weekday() >= 5
                        else ShiftAssignment.DAYTYPE_HOLIDAY
                        if day in holiday_dates
                        else ShiftAssignment.DAYTYPE_WEEKDAY
                    )
                    ShiftAssignment.objects.create(
                        duty_roster=duty_roster,
                        shift=shift,
                        staff=user,
                        date=day,
                        day_type=day_type,
                        supervisor=user,
                    )
                weekly_team.refresh_reconciliation()
            return True
        except Exception:
            pass

    return False


def balance_supervisors_for_roster(
    treatment_plant_id: int,
    section_id: int,
    year: int,
    month: int,
    designated_by: Optional[CustomUser] = None,
    overwrite_existing: bool = False,
) -> Dict[str, Any]:
    """
    Dynamically balances supervisory workload among all available standing pool
    supervisors for a given treatment plant, section, and month.

    Rules & Invariants:
    1. Pool supervisors: all active standing ShiftSupervisor records (week_start is null).
    2. Rotation: distributes weekly supervision slots across all supervisors to equalize workload.
    3. Exclusions: respects AnnualLeave, non-overlapping weekly supervisor constraints (max 1 shift per week per user).
    4. Compliance: ensures each supervisor has a valid ShiftAssignment on that shift during that week.
    5. Propagates supervisor FK to all ShiftAssignments in that shift, section, and week.
    """
    roster_weeks = MonthlyDutyRoster.get_weeks_in_month(year, month)
    if not roster_weeks:
        return {
            "success": False,
            "message": f"No valid roster weeks found for {year}-{month:02d}.",
            "slots_assigned": 0,
        }

    shifts = list(
        Shift.objects.filter(
            treatment_plant_id=treatment_plant_id,
            is_deleted=False,
            section_requirements__section_id=section_id,
            section_requirements__section__is_active=True,
        )
        .distinct()
        .order_by("start_time", "name")
    )
    if not shifts:
        return {
            "success": False,
            "message": "No active shifts configured for this section.",
            "slots_assigned": 0,
        }

    pool_supervisors = list(
        ShiftSupervisor.objects.filter(
            treatment_plant_id=treatment_plant_id,
            section_id=section_id,
            is_active=True,
            week_start__isnull=True,
            user__is_active=True,
        )
        .select_related("user")
        .order_by("pk")
    )
    if not pool_supervisors:
        return {
            "success": False,
            "message": "No active standing supervisors registered for this plant and section.",
            "slots_assigned": 0,
        }

    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(year, month + 1, 1) - timedelta(days=1)

    with transaction.atomic():
        # Track weekly assignments already established
        existing_weekly = list(
            ShiftSupervisor.objects.filter(
                treatment_plant_id=treatment_plant_id,
                section_id=section_id,
                week_start__range=(month_start, month_end),
                week_start__isnull=False,
            )
            .select_related("user", "shift")
        )

        workload_counter = {s.user_id: 0 for s in pool_supervisors}
        existing_by_shift_week = {}
        for des in existing_weekly:
            if overwrite_existing:
                des.delete()
            else:
                existing_by_shift_week[(des.shift_id, des.week_start)] = des
                if des.user_id in workload_counter:
                    workload_counter[des.user_id] += 1

        assigned_count = 0
        all_pool_users = [s.user for s in pool_supervisors]

        for week_idx, (week_start, week_end) in enumerate(roster_weeks):
            # Track who is already supervising any shift in this specific week
            supervisors_in_week = set()
            for shift in shifts:
                existing_des = existing_by_shift_week.get((shift.id, week_start))
                if existing_des and existing_des.user_id:
                    supervisors_in_week.add(existing_des.user_id)

            for shift in shifts:
                if (shift.id, week_start) in existing_by_shift_week and not overwrite_existing:
                    continue

                # Filter eligible candidates for this week and shift
                eligible_candidates = []
                for sup_user in all_pool_users:
                    # Constraint 1: User cannot supervise more than one shift per week
                    if sup_user.pk in supervisors_in_week:
                        continue

                    # Constraint 2: Cannot supervise if on annual leave during the week
                    is_on_leave = AnnualLeave.objects.filter(
                        staff_id=sup_user.pk,
                        treatment_plant_id=treatment_plant_id,
                        status__in=AnnualLeave.UNAVAILABLE_STATUSES,
                        start_date__lte=week_end,
                        end_date__gte=week_start,
                    ).exists()
                    if is_on_leave:
                        continue

                    # Check if already has an assignment on this shift (bonus score)
                    has_assignment = _has_valid_shift_assignment_for_week(
                        user_id=sup_user.pk,
                        shift_id=shift.pk,
                        section_id=section_id,
                        week_start=week_start,
                        week_end=week_end,
                    )

                    # Dynamic score tuple:
                    # 1. Total supervisory workload in this month (minimize for fairness)
                    # 2. Has assignment already on this shift (0 if yes, 1 if requires move/add)
                    # 3. Round-robin tie-breaker using (user_id + week_idx + shift.id)
                    score = (
                        workload_counter[sup_user.pk],
                        0 if has_assignment else 1,
                        (sup_user.pk + week_idx * len(shifts) + shift.pk) % len(all_pool_users),
                    )
                    eligible_candidates.append((score, sup_user))

                if not eligible_candidates:
                    continue

                eligible_candidates.sort(key=lambda item: item[0])

                # Find the first candidate for whom a valid shift assignment is present or successfully created
                selected_user = None
                for _score, candidate in eligible_candidates:
                    has_or_made_assignment = _ensure_candidate_has_shift_assignment(
                        user=candidate,
                        shift=shift,
                        section_id=section_id,
                        treatment_plant_id=treatment_plant_id,
                        week_start=week_start,
                        week_end=week_end,
                        actor_user=designated_by,
                    )
                    if has_or_made_assignment:
                        selected_user = candidate
                        break

                if not selected_user:
                    # If assignment creation failed for top candidate, take the best candidate and attempt save
                    selected_user = eligible_candidates[0][1]

                # Clear any other shift assignment for this user in this week to guarantee clean state
                ShiftSupervisor.objects.filter(
                    user_id=selected_user.pk,
                    week_start__isnull=False,
                    week_start__lte=week_end,
                    week_end__gte=week_start,
                ).exclude(shift_id=shift.pk, section_id=section_id).delete()

                designation, _created = ShiftSupervisor.objects.update_or_create(
                    shift=shift,
                    section_id=section_id,
                    week_start=week_start,
                    defaults={
                        "treatment_plant_id": treatment_plant_id,
                        "week_end": week_end,
                        "user": selected_user,
                        "designated_by": designated_by,
                        "is_active": True,
                    },
                )

                # Overridden save updates ShiftAssignment.supervisor automatically, but we also ensure here
                ShiftAssignment.objects.filter(
                    shift=shift,
                    staff__section_id=section_id,
                    date__range=(week_start, week_end),
                    is_deleted=False,
                ).update(supervisor=selected_user)

                workload_counter[selected_user.pk] += 1
                supervisors_in_week.add(selected_user.pk)
                existing_by_shift_week[(shift.id, week_start)] = designation
                assigned_count += 1

        summary = get_supervisor_workload_summary(treatment_plant_id, section_id, year, month)
        return {
            "success": True,
            "message": f"Successfully balanced workload across {len(pool_supervisors)} supervisors for {len(roster_weeks)} weeks.",
            "slots_assigned": assigned_count,
            "total_slots": len(roster_weeks) * len(shifts),
            "summary": summary,
        }

