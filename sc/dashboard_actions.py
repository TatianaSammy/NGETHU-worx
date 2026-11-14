import json
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from core.plant_section_scope import section_scope_q, user_plant_section
from sc.models import (
    AnnualLeave,
    MonthlyDutyRoster,
    Shift,
    ShiftAssignment,
    ShiftHandover,
    ShiftSectionRequirement,
    ShiftSupervisor,
)
from t.models import TreatmentPlant
from accounts.models import CustomUser


DAY_TYPES = {choice for choice, _label in ShiftAssignment.DAYTYPE_CHOICES}


def _allowed(user, permission):
    return user.is_superuser or user.has_perm(permission)


def _json_body(request):
    try:
        return json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _json_error(message, status=400):
    return JsonResponse({"ok": False, "message": message}, status=status)


def _scoped_assignment_queryset(user):
    queryset = ShiftAssignment.objects.select_related(
        "staff", "staff__section", "shift", "duty_roster", "supervisor", "temp_supervisor"
    ).filter(is_deleted=False)
    if user.is_superuser:
        return queryset
    plant_id, section_id = user_plant_section(user)
    if not plant_id or not section_id:
        return queryset.none()
    return queryset.filter(
        shift__treatment_plant_id=plant_id,
        duty_roster__treatment_plant_id=plant_id,
        staff__assigned_treatment_plant_id=plant_id,
        staff__section_id=section_id,
        shift__section_requirements__section_id=section_id,
    ).distinct()


def _parse_range(data):
    try:
        start = date.fromisoformat(data.get("week_start", ""))
        end = date.fromisoformat(data.get("week_end", ""))
    except (TypeError, ValueError):
        return None, None
    if end < start or (end - start).days > 6:
        return None, None
    return start, end


def _is_roster_week(start, end):
    return (start, end) in MonthlyDutyRoster.get_weeks_in_month(start.year, start.month)


def _dates_for_type(start, end, day_type):
    days = []
    current = start
    while current <= end:
        if day_type == ShiftAssignment.DAYTYPE_WEEKDAY and current.weekday() < 5:
            days.append(current)
        elif day_type == ShiftAssignment.DAYTYPE_WEEKEND and current.weekday() >= 5:
            days.append(current)
        elif day_type == ShiftAssignment.DAYTYPE_HOLIDAY:
            days.append(current)
        current += timedelta(days=1)
    return days


def _section_for_actor(request, plant_id, section_id):
    if request.user.is_superuser:
        return True
    actor_plant_id, actor_section_id = user_plant_section(request.user)
    return actor_plant_id == plant_id and actor_section_id == section_id


def _can_manage_weekly_supervisors(user):
    return user.is_superuser or any(
        user.has_perm(permission)
        for permission in (
            "sc.add_shiftsupervisor",
            "sc.change_shiftsupervisor",
        )
    )


def _assign_weekly_supervisor(request, data):
    if not _can_manage_weekly_supervisors(request.user):
        return _json_error("You do not have permission to assign a weekly shift supervisor.", 403)
    try:
        shift_id = int(data.get("shift_id"))
        section_id = int(data.get("section_id"))
        plant_id = int(data.get("plant_id"))
        user_id = int(data.get("user_id"))
    except (TypeError, ValueError):
        return _json_error("Choose a shift, plant, section, and staff member.")
    week_start, week_end = _parse_range(data)
    if not week_start or not week_end or not _is_roster_week(week_start, week_end):
        return _json_error("Choose a valid duty roster week.")

    try:
        with transaction.atomic():
            shift = Shift.objects.select_for_update().filter(
                pk=shift_id,
                treatment_plant_id=plant_id,
                is_deleted=False,
            ).first()
            if not shift:
                return _json_error("The selected shift is not available at this treatment plant.", 404)
            if not _section_for_actor(request, plant_id, section_id):
                return _json_error("The selected section is outside your assigned scope.", 403)
            requirement = ShiftSectionRequirement.objects.select_for_update().filter(
                shift_id=shift_id,
                section_id=section_id,
                section__is_active=True,
            ).filter(section_scope_q("section__treatment_plant_id", plant_id)).first()
            if not requirement:
                return _json_error("This shift is not configured for the selected section.", 409)
            staff = CustomUser.objects.select_for_update().filter(
                pk=user_id,
                is_active=True,
            ).filter(
                Q(is_superuser=True)
                | Q(assigned_treatment_plant_id=plant_id, section_id=section_id)
            ).first()
            if not staff:
                return _json_error("The selected user does not belong to this plant and section.", 409)
            if not ShiftSupervisor.objects.filter(
                treatment_plant_id=plant_id,
                section_id=section_id,
                user_id=user_id,
                is_active=True,
                user__is_active=True,
                section__is_active=True,
            ).filter(
                Q(shift_id=shift_id) | Q(shift__isnull=True) | Q(shift__treatment_plant_id=plant_id)
            ).exists():
                return _json_error("Choose an active shift supervisor designated for this plant and section.", 409)
            has_assignment = ShiftAssignment.objects.filter(
                shift_id=shift_id,
                staff_id=user_id,
                staff__section_id=section_id,
                date__range=(week_start, week_end),
                is_deleted=False,
            ).exists()

            if not has_assignment:
                assignment_id = data.get("assignment_id")
                moved_assignment = None
                if assignment_id:
                    try:
                        moved_assignment = ShiftAssignment.objects.select_for_update().filter(
                            pk=int(assignment_id),
                            staff_id=user_id,
                            is_deleted=False,
                        ).first()
                    except (ValueError, TypeError):
                        pass
                if not moved_assignment:
                    moved_assignment = ShiftAssignment.objects.select_for_update().filter(
                        staff_id=user_id,
                        staff__section_id=section_id,
                        date__range=(week_start, week_end),
                        is_deleted=False,
                    ).first()

                if moved_assignment:
                    candidate_dates = [moved_assignment.date]
                    curr = week_start
                    while curr <= week_end:
                        if curr != moved_assignment.date:
                            candidate_dates.append(curr)
                        curr += timedelta(days=1)

                    moved = False
                    for candidate_date in candidate_dates:
                        day_type = (
                            ShiftAssignment.DAYTYPE_WEEKEND
                            if candidate_date.weekday() >= 5
                            else ShiftAssignment.DAYTYPE_WEEKDAY
                        )
                        can_accept, _ = shift.can_accept_assignment(section_id, candidate_date, day_type=day_type)
                        is_eligible, _ = shift.is_staff_eligible(
                            staff, candidate_date, section=section_id, exclude_assignment_id=moved_assignment.pk
                        )
                        if can_accept and is_eligible:
                            try:
                                moved_assignment.move_assignment(
                                    target_shift=shift,
                                    target_date=candidate_date,
                                    day_type=day_type,
                                    moved_by=request.user,
                                    remarks=f"Reassigned as weekly supervisor for {shift.name}.",
                                )
                                moved = True
                                break
                            except ValidationError:
                                pass
                    if not moved:
                        # Fall back to moving with original date to surface domain error
                        moved_assignment.move_assignment(
                            target_shift=shift,
                            target_date=moved_assignment.date,
                            day_type=moved_assignment.day_type,
                            moved_by=request.user,
                            remarks=f"Reassigned as weekly supervisor for {shift.name}.",
                        )
                else:
                    return _json_error("The shift supervisor must have a roster assignment on this shift during the selected week.", 409)

            # Remove any weekly supervisor designation this user held on any other shift for this week
            ShiftSupervisor.objects.filter(
                user_id=user_id,
                week_start__isnull=False,
                week_start__lte=week_end,
                week_end__gte=week_start,
            ).exclude(shift_id=shift_id, section_id=section_id).delete()

            designation, _created = ShiftSupervisor.objects.update_or_create(
                shift_id=shift_id,
                section_id=section_id,
                week_start=week_start,
                defaults={
                    "treatment_plant_id": plant_id,
                    "week_end": week_end,
                    "user_id": user_id,
                    "is_active": True,
                    "designated_by": request.user,
                },
            )

    except (IntegrityError, ValidationError) as error:
        detail = "; ".join(error.messages) if isinstance(error, ValidationError) else "The weekly supervisor assignment conflicts with another duty."
        return _json_error(detail, 409)

    return JsonResponse({
        "ok": True,
        "message": f"{staff.get_full_name() or staff.get_username()} is assigned to supervise {shift.name} for {week_start:%d %b}–{week_end:%d %b %Y}.",
        "user_id": staff.pk,
        "shift_id": shift.pk,
        "section_id": section_id,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "assignment_id": designation.pk,
    })


def _balance_supervisors_action(request, data):
    if not _can_manage_weekly_supervisors(request.user):
        return _json_error("You do not have permission to balance shift supervisors.", 403)
    try:
        plant_id = int(data.get("plant_id"))
        section_id = int(data.get("section_id"))
        year = int(data.get("year"))
        month = int(data.get("month"))
    except (TypeError, ValueError):
        return _json_error("A valid plant, section, year, and month must be provided.")

    if not _section_for_actor(request, plant_id, section_id):
        return _json_error("The selected section is outside your assigned scope.", 403)

    from sc.services.supervisor_balance_service import balance_supervisors_for_roster

    overwrite_existing = bool(data.get("overwrite_existing", False))
    try:
        result = balance_supervisors_for_roster(
            treatment_plant_id=plant_id,
            section_id=section_id,
            year=year,
            month=month,
            designated_by=request.user,
            overwrite_existing=overwrite_existing,
        )
    except Exception as exc:
        return _json_error(f"Balancing failed: {str(exc)}", 500)

    if not result.get("success"):
        return _json_error(result.get("message", "Balancing failed."), 409)

    return JsonResponse({
        "ok": True,
        "message": result.get("message"),
        "slots_assigned": result.get("slots_assigned"),
        "total_slots": result.get("total_slots"),
        "summary": result.get("summary"),
    })


@login_required
@require_GET
def shift_supervisor_options(request):
    if not (
        request.user.is_superuser
        or request.user.has_perm("sc.add_shiftsupervisor")
        or request.user.has_perm("sc.change_shiftsupervisor")
    ):
        return _json_error("You do not have permission to manage shift supervisors.", 403)

    plant_id = (request.GET.get("plant_id") or "").strip()
    if not plant_id.isdecimal():
        return _json_error("Choose a treatment plant.")
    plant_id = int(plant_id)
    actor_plant_id, actor_section_id = user_plant_section(request.user)
    if not request.user.is_superuser and (
        actor_plant_id != plant_id or not actor_section_id
    ):
        return _json_error("The selected plant is outside your assigned scope.", 403)

    shifts = Shift.objects.filter(
        treatment_plant_id=plant_id,
        is_deleted=False,
        section_requirements__section__is_active=True,
    ).filter(section_scope_q(
        "section_requirements__section__treatment_plant_id",
        plant_id,
    )).distinct().order_by("start_time", "name")
    if not request.user.is_superuser:
        shifts = shifts.filter(section_requirements__section_id=actor_section_id)

    shift_id = (request.GET.get("shift_id") or "").strip()
    selected_shift = None
    if shift_id:
        if not shift_id.isdecimal():
            return _json_error("Choose a valid shift.")
        selected_shift = shifts.filter(pk=shift_id).first()
        if not selected_shift:
            return _json_error("The selected shift is not configured for this plant and section.", 409)

    sections = ShiftSectionRequirement.objects.none()
    if selected_shift:
        sections = ShiftSectionRequirement.objects.filter(
            shift=selected_shift,
            section__is_active=True,
        ).filter(section_scope_q("section__treatment_plant_id", plant_id))
        if not request.user.is_superuser:
            sections = sections.filter(section_id=actor_section_id)
        sections = sections.select_related("section").order_by("section__name")

    section_id = (request.GET.get("section_id") or "").strip()
    users = CustomUser.objects.none()
    if section_id:
        if not section_id.isdecimal() or not sections.filter(section_id=section_id).exists():
            return _json_error("The selected section is not configured for this shift.", 409)
        users = CustomUser.objects.filter(is_active=True).filter(
            Q(is_superuser=True)
            | Q(assigned_treatment_plant_id=plant_id, section_id=section_id)
        ).order_by("last_name", "first_name", "username")

    return JsonResponse({
        "ok": True,
        "shifts": [{
            "id": shift.pk,
            "label": f"{shift.name} · {shift.start_time:%H:%M}–{shift.end_time:%H:%M}",
        } for shift in shifts],
        "sections": [{"id": item.section_id, "label": item.section.name} for item in sections],
        "users": [{
            "id": user.pk,
            "label": user.get_full_name() or user.get_username(),
        } for user in users],
    })


@login_required
@require_GET
def roster_assignment_options(request):
    if not _allowed(request.user, "sc.add_shiftassignment"):
        return _json_error("You do not have permission to add roster assignments.", 403)
    try:
        shift_id = int(request.GET.get("shift_id", ""))
    except (TypeError, ValueError):
        return _json_error("Choose a valid shift.")
    shift = Shift.objects.filter(is_deleted=False).select_related("treatment_plant").filter(pk=shift_id).first()
    if not shift:
        return _json_error("The selected shift is unavailable.", 404)
    requested_plant = (request.GET.get("plant_id") or "").strip()
    if not requested_plant or not requested_plant.isdecimal():
        return _json_error("Choose a treatment plant before loading roster options.")
    if int(requested_plant) != shift.treatment_plant_id:
        return _json_error("The selected shift does not belong to this treatment plant.", 409)
    start, end = _parse_range({"week_start": request.GET.get("week_start"), "week_end": request.GET.get("week_end")})
    day_type = request.GET.get("day_type")
    if not start or not end or not _is_roster_week(start, end) or day_type not in DAY_TYPES:
        return _json_error("Choose a valid roster week and day type.")

    plant_id = shift.treatment_plant_id
    actor_plant_id, actor_section_id = user_plant_section(request.user)
    if not request.user.is_superuser:
        if actor_plant_id != plant_id or not actor_section_id:
            return _json_error("This shift is outside your assigned treatment plant and section.", 403)

    requirements = ShiftSectionRequirement.objects.filter(
        shift_id=shift.pk,
        shift__treatment_plant_id=plant_id,
        section__is_active=True,
    ).filter(section_scope_q("section__treatment_plant_id", plant_id)).select_related("section").order_by("section__name")
    requested_section = (request.GET.get("section_id") or "").strip()
    if requested_section:
        if not requested_section.isdecimal():
            return _json_error("Choose a valid section.")
        if not request.user.is_superuser and int(requested_section) != actor_section_id:
            return _json_error("The requested section is outside your assigned scope.", 403)
        requirements = requirements.filter(section_id=requested_section)
        if not requirements.exists():
            return _json_error("The selected section is not configured for this shift.", 403)
    if not request.user.is_superuser:
        requirements = requirements.filter(section_id=actor_section_id)
    preferred_section = (request.GET.get("preferred_section_id") or "").strip()
    if preferred_section and (
        not preferred_section.isdecimal()
        or not requirements.filter(section_id=preferred_section).exists()
    ):
        preferred_section = ""
    locked_section_id = None if request.user.is_superuser else actor_section_id

    dates = _dates_for_type(start, end, day_type)
    if not dates:
        day_label = "weekdays" if day_type == ShiftAssignment.DAYTYPE_WEEKDAY else "weekend days" if day_type == ShiftAssignment.DAYTYPE_WEEKEND else "dates"
        return JsonResponse({"ok": True, "slots": [], "plant": {"id": shift.treatment_plant_id, "name": shift.treatment_plant.name}, "locked_section_id": locked_section_id, "preferred_section_id": preferred_section or None, "can_select_section": request.user.is_superuser, "message": f"No {day_label} fall within this duty week."})

    requirements = list(requirements)
    if not requirements:
        return JsonResponse({
            "ok": True,
            "slots": [],
            "plant": {"id": shift.treatment_plant_id, "name": shift.treatment_plant.name},
            "locked_section_id": locked_section_id,
            "preferred_section_id": preferred_section or None,
            "can_select_section": request.user.is_superuser,
            "message": "No active section is configured for this shift within the selected treatment plant.",
        })

    slots = []
    capacity_messages = []
    for assignment_date in dates:
        sections = []
        for requirement in requirements:
            can_accept, capacity_message = shift.can_accept_assignment(requirement.section, assignment_date, day_type=day_type)
            candidates = shift.get_available_staff(assignment_date, section=requirement.section) if can_accept else []
            if capacity_message:
                capacity_messages.append(capacity_message)
            sections.append({
                "id": requirement.section_id,
                "name": requirement.section.name,
                "capacity_available": can_accept,
                "capacity_message": capacity_message or "",
                "users": [{
                    "id": person.pk,
                    "name": person.get_full_name() or person.get_username(),
                } for person in candidates],
            })
        slots.append({
            "date": assignment_date.isoformat(),
            "label": assignment_date.strftime("%a, %d %b"),
            "sections": sections,
        })
    has_active_staff = CustomUser.objects.filter(
        is_active=True,
        assigned_treatment_plant_id=plant_id,
        section_id__in=[requirement.section_id for requirement in requirements],
    ).exists()
    has_candidates = any(section["users"] for slot in slots for section in slot["sections"])
    capacity_open = any(section["capacity_available"] for slot in slots for section in slot["sections"])
    if has_candidates:
        message = ""
    elif not has_active_staff:
        message = "No active staff are assigned to the configured section."
    elif not capacity_open:
        message = capacity_messages[0] if capacity_messages else "The shift is at capacity."
    else:
        message = "No staff are eligible for these dates; check current assignments, leave or off-duty records, and shift capacity."
    return JsonResponse({
        "ok": True,
        "slots": slots,
        "plant": {"id": shift.treatment_plant_id, "name": shift.treatment_plant.name},
        "locked_section_id": locked_section_id,
        "preferred_section_id": preferred_section or None,
        "can_select_section": request.user.is_superuser,
        "message": message,
    })


def _assign_leave(request, data):
    if not (
        request.user.is_superuser
        or request.user.has_perm("sc.change_shiftassignment")
        or request.user.has_perm("sc.add_annualleave")
        or request.user.has_perm("sc.manage_staff_availability")
    ):
        return _json_error("You do not have permission to manage staff availability.", 403)

    try:
        staff_id = int(data.get("staff_id"))
        plant_id = int(data.get("plant_id"))
    except (TypeError, ValueError):
        return _json_error("Choose a treatment plant and staff member.")

    week_start, week_end = _parse_range(data)
    if not week_start or not week_end or not _is_roster_week(week_start, week_end):
        return _json_error("Choose a valid duty roster week.")

    try:
        with transaction.atomic():
            plant = TreatmentPlant.objects.select_for_update().filter(pk=plant_id).first()
            if not plant:
                return _json_error("Treatment plant not found.", 404)

            staff = CustomUser.objects.select_for_update().filter(pk=staff_id, is_active=True).first()
            if not staff:
                return _json_error("Staff member not found or inactive.", 404)

            if not staff.is_superuser and staff.assigned_treatment_plant_id != plant_id:
                return _json_error("Staff member does not belong to this treatment plant.", 409)

            # 1. Remove any weekly supervisor assignments held by this staff for this week
            ShiftSupervisor.objects.filter(
                user_id=staff_id,
                week_start__isnull=False,
                week_start__lte=week_end,
                week_end__gte=week_start,
            ).delete()

            # 2. For any shifts where staff was supervising, reset supervisor to active static supervisor or None
            supervised_assignments = ShiftAssignment.objects.select_for_update().filter(
                supervisor_id=staff_id,
                date__range=(week_start, week_end),
                is_deleted=False,
            )
            for sa in supervised_assignments:
                fallback_sup = ShiftSupervisor.get_active_supervisor(
                    treatment_plant_id=plant_id,
                    section_id=sa.staff.section_id,
                    shift_id=sa.shift_id,
                    assignment_date=sa.date,
                )
                sa.supervisor = fallback_sup if fallback_sup and fallback_sup.pk != staff_id else None
                sa.save(update_fields=("supervisor",))

            # 3. Clear temporary supervisor or handover assignments for this staff
            ShiftAssignment.objects.filter(
                temp_supervisor_id=staff_id,
                date__range=(week_start, week_end),
                is_deleted=False,
            ).update(temp_supervisor=None, temp_supervisor_start=None, temp_supervisor_end=None)

            ShiftAssignment.objects.filter(
                handover_to_id=staff_id,
                date__range=(week_start, week_end),
                is_deleted=False,
            ).update(handover_to=None)

            # Clear any ShiftHandover supervisor references for this staff during this week
            ShiftHandover.objects.filter(
                date__range=(week_start, week_end),
                incoming_supervisor_id=staff_id,
            ).update(incoming_supervisor=None)

            ShiftHandover.objects.filter(
                date__range=(week_start, week_end),
                outgoing_supervisor_id=staff_id,
            ).update(outgoing_supervisor=None)

            # 4. Remove all roster assignments for this staff during this week
            staff_assignments = ShiftAssignment.objects.select_for_update().filter(
                staff_id=staff_id,
                date__range=(week_start, week_end),
                is_deleted=False,
            )
            for sa in staff_assignments:
                sa.delete()

            # 5. Clear DailyTaskAssignments if any
            try:
                from weeklytasks.models import DailyTaskAssignment
                DailyTaskAssignment.objects.filter(
                    user_id=staff_id,
                    date__range=(week_start, week_end),
                ).delete()
            except Exception:
                pass

            # 6. Create or update AnnualLeave record
            leave = AnnualLeave.objects.select_for_update().filter(
                staff=staff,
                treatment_plant=plant,
                start_date__lte=week_end,
                end_date__gte=week_start,
            ).first()

            if not leave:
                leave = AnnualLeave(
                    staff=staff,
                    treatment_plant=plant,
                    start_date=week_start,
                    end_date=week_end,
                    status=AnnualLeave.STATUS_ANNUAL_LEAVE,
                    reason=f"Assigned from weekly duty roster by {request.user.get_username()}.",
                    created_by=request.user,
                    updated_by=request.user,
                )
            else:
                leave.start_date = min(leave.start_date, week_start)
                leave.end_date = max(leave.end_date, week_end)
                leave.status = AnnualLeave.STATUS_ANNUAL_LEAVE
                leave.updated_by = request.user
                # Clean up any assignments and handovers across the expanded range
                ShiftAssignment.objects.filter(
                    staff_id=staff_id,
                    date__range=(leave.start_date, leave.end_date),
                    is_deleted=False,
                ).delete()
                ShiftSupervisor.objects.filter(
                    user_id=staff_id,
                    week_start__isnull=False,
                    week_start__lte=leave.end_date,
                    week_end__gte=leave.start_date,
                ).delete()
                ShiftHandover.objects.filter(
                    date__range=(leave.start_date, leave.end_date),
                    incoming_supervisor_id=staff_id,
                ).update(incoming_supervisor=None)
                ShiftHandover.objects.filter(
                    date__range=(leave.start_date, leave.end_date),
                    outgoing_supervisor_id=staff_id,
                ).update(outgoing_supervisor=None)

            leave.full_clean()
            leave.save()

            return JsonResponse({
                "ok": True,
                "message": f"{staff.get_full_name() or staff.get_username()} placed on annual leave for {week_start:%d %b}–{week_end:%d %b %Y}.",
                "staff_id": staff.pk,
                "leave_id": leave.pk,
            })
    except (IntegrityError, ValidationError) as error:
        detail = "; ".join(error.messages) if isinstance(error, ValidationError) else "Could not record annual leave due to a conflict."
        return _json_error(detail, 409)


@login_required
@require_POST
def roster_assignment_action(request):
    data = _json_body(request)
    if data is None:
        return _json_error("The request body is not valid JSON.")
    action = data.get("action")
    if action == "supervisor":
        return _assign_weekly_supervisor(request, data)
    if action == "balance_supervisors":
        return _balance_supervisors_action(request, data)
    if action == "leave":
        return _assign_leave(request, data)
    required_permission = "sc.add_shiftassignment" if action == "add" else "sc.change_shiftassignment"
    if action not in {"add", "move"}:
        return _json_error("Choose an add or move action.")
    if not _allowed(request.user, required_permission):
        return _json_error("You do not have permission to change the duty roster.", 403)

    try:
        assignment_date = date.fromisoformat(data.get("date", ""))
        day_type = data.get("day_type")
        shift_id = int(data.get("shift_id"))
    except (TypeError, ValueError):
        return _json_error("Choose a valid shift and roster date.")
    if day_type not in DAY_TYPES:
        return _json_error("Choose a valid duty day type.")
    if day_type == ShiftAssignment.DAYTYPE_WEEKDAY and assignment_date.weekday() >= 5:
        return _json_error("The selected date is not a working day.")
    if day_type == ShiftAssignment.DAYTYPE_WEEKEND and assignment_date.weekday() < 5:
        return _json_error("The selected date is not a weekend day.")
    week_start, week_end = _parse_range(data)
    if (
        not week_start
        or not week_end
        or not _is_roster_week(week_start, week_end)
        or assignment_date not in _dates_for_type(week_start, week_end, day_type)
    ):
        return _json_error("The selected date does not match this roster cell.")

    try:
        with transaction.atomic():
            if action == "add":
                try:
                    section_id = int(data.get("section_id"))
                    staff_id = int(data.get("staff_id"))
                except (TypeError, ValueError):
                    return _json_error("Choose a section and staff member.")
                shift = Shift.objects.filter(is_deleted=False, pk=shift_id).first()
                if not shift:
                    return _json_error("The selected shift is unavailable.", 404)
                requested_plant = str(data.get("plant_id") or "").strip()
                if not requested_plant or not requested_plant.isdecimal():
                    return _json_error("Choose a treatment plant before adding staff.")
                if int(requested_plant) != shift.treatment_plant_id:
                    return _json_error("The selected shift does not belong to this treatment plant.", 409)
                if not _section_for_actor(request, shift.treatment_plant_id, section_id):
                    return _json_error("The selected section is outside your assigned scope.", 403)
                requirement = ShiftSectionRequirement.objects.select_for_update().filter(
                    shift_id=shift.pk,
                    section_id=section_id,
                    section__is_active=True,
                ).filter(section_scope_q("section__treatment_plant_id", shift.treatment_plant_id)).first()
                if not requirement:
                    return _json_error("This shift is not configured for the selected section.", 400)
                staff = CustomUser.objects.select_for_update().filter(
                    pk=staff_id,
                    is_active=True,
                    assigned_treatment_plant_id=shift.treatment_plant_id,
                    section_id=section_id,
                ).first()
                if not staff:
                    return _json_error("That staff member no longer belongs to this plant and section.", 409)

                # Gate creation directly through ShiftAssignment.create_assignment
                assignment = ShiftAssignment.create_assignment(
                    staff=staff,
                    shift=shift,
                    date=assignment_date,
                    day_type=day_type,
                    remarks=f"Added from the weekly duty roster by {request.user.get_username()}.",
                    created_by=request.user,
                )
            else:
                assignment_id = int(data.get("assignment_id"))
                if not _scoped_assignment_queryset(request.user).filter(pk=assignment_id).exists():
                    return _json_error("The roster entry was not found in your scope.", 404)
                assignment = ShiftAssignment.objects.select_for_update().filter(
                    pk=assignment_id,
                    is_deleted=False,
                ).first()
                if not assignment or not _scoped_assignment_queryset(request.user).filter(pk=assignment.pk).exists():
                    return _json_error("The roster entry was not found in your scope.", 404)
                target_shift = Shift.objects.filter(
                    is_deleted=False,
                    treatment_plant_id=assignment.shift.treatment_plant_id,
                    pk=shift_id,
                ).first()
                if not target_shift:
                    return _json_error("The destination shift is unavailable in this treatment plant.", 404)
                requested_plant = str(data.get("plant_id") or "").strip()
                if not requested_plant or not requested_plant.isdecimal():
                    return _json_error("Choose a treatment plant before moving staff.")
                if int(requested_plant) != target_shift.treatment_plant_id:
                    return _json_error("The selected shift does not belong to this treatment plant.", 409)
                start, end = _parse_range(data)
                if not start or not end or not start <= assignment_date <= end:
                    return _json_error("The destination date is outside the selected roster week.")

                # Gate move directly through assignment.move_assignment
                eligible_date = assignment_date
                is_eligible, _ = target_shift.is_staff_eligible(
                    assignment.staff, assignment_date, section=assignment.staff.section_id, exclude_assignment_id=assignment.pk
                )
                can_accept, _ = target_shift.can_accept_assignment(
                    assignment.staff.section_id, assignment_date, day_type=day_type
                )
                if not is_eligible or not can_accept:
                    category_dates = _dates_for_type(start, end, day_type)
                    for cd in category_dates:
                        can_acc, _ = target_shift.can_accept_assignment(assignment.staff.section_id, cd, day_type=day_type)
                        is_el, _ = target_shift.is_staff_eligible(assignment.staff, cd, section=assignment.staff.section_id, exclude_assignment_id=assignment.pk)
                        if can_acc and is_el:
                            eligible_date = cd
                            break

                note = f"Moved on the weekly duty roster by {request.user.get_username()} at {timezone.localtime():%Y-%m-%d %H:%M}."
                assignment.move_assignment(
                    target_shift=target_shift,
                    target_date=eligible_date,
                    day_type=day_type,
                    moved_by=request.user,
                    remarks=note,
                )
            return JsonResponse({
                "ok": True,
                "message": "Roster updated.",
                "assignment_id": assignment.pk,
                "staff": assignment.staff.get_full_name() or assignment.staff.get_username(),
                "date": assignment.date.isoformat(),
                "shift": assignment.shift.name,
            })
    except (IntegrityError, ValidationError) as error:
        if isinstance(error, ValidationError):
            detail = "; ".join(error.messages)
        else:
            detail = "A duplicate assignment or concurrent roster update was detected."
        return _json_error(detail, 409)
