import json
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from core.plant_section_scope import user_plant_section
from sc.models import AnnualLeave, MonthlyDutyRoster, Shift, ShiftAssignment, ShiftSectionRequirement
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


def _shift_interval(shift, day_offset=0):
    start = day_offset + shift.start_time.hour * 60 + shift.start_time.minute
    end = day_offset + shift.end_time.hour * 60 + shift.end_time.minute
    if end <= start:
        end += 1440
    return start, end


def _shifts_overlap(first, first_date, second, second_date):
    first_start, first_end = _shift_interval(first)
    second_offset = (second_date - first_date).days * 1440
    second_start, second_end = _shift_interval(second, second_offset)
    return first_start < second_end and second_start < first_end


def _staff_has_conflict(staff_id, assignment_date, shift, *, exclude_id=None):
    existing = ShiftAssignment.objects.filter(
        staff_id=staff_id,
        date__range=(assignment_date - timedelta(days=1), assignment_date + timedelta(days=1)),
        is_deleted=False,
        shift__is_deleted=False,
        shift__treatment_plant_id=shift.treatment_plant_id,
    ).exclude(pk=exclude_id).select_related("shift")
    for item in existing:
        if _shifts_overlap(shift, assignment_date, item.shift, item.date):
            return True
    return False


def _section_for_actor(request, plant_id, section_id):
    if request.user.is_superuser:
        return True
    actor_plant_id, actor_section_id = user_plant_section(request.user)
    return actor_plant_id == plant_id and actor_section_id == section_id


@login_required
@require_GET
def roster_assignment_options(request):
    if not _allowed(request.user, "sc.add_shiftassignment"):
        return _json_error("You do not have permission to add roster assignments.", 403)
    try:
        shift_id = int(request.GET.get("shift_id", ""))
    except (TypeError, ValueError):
        return _json_error("Choose a valid shift.")
    shift = Shift.objects.filter(is_deleted=False).filter(pk=shift_id).first()
    if not shift:
        return _json_error("The selected shift is unavailable.", 404)
    start, end = _parse_range({"week_start": request.GET.get("week_start"), "week_end": request.GET.get("week_end")})
    day_type = request.GET.get("day_type")
    if not start or not end or day_type not in DAY_TYPES:
        return _json_error("Choose a valid roster week and day type.")

    plant_id = shift.treatment_plant_id
    actor_plant_id, actor_section_id = user_plant_section(request.user)
    if not request.user.is_superuser:
        if actor_plant_id != plant_id or not actor_section_id:
            return _json_error("This shift is outside your assigned treatment plant and section.", 403)

    requirements = ShiftSectionRequirement.objects.filter(
        shift_id=shift.pk,
        shift__treatment_plant_id=plant_id,
        section__treatment_plant_id=plant_id,
        section__is_active=True,
    ).select_related("section").order_by("section__name")
    if not request.user.is_superuser:
        requirements = requirements.filter(section_id=actor_section_id)
    elif actor_section_id and requirements.filter(section_id=actor_section_id).exists():
        requirements = requirements.filter(section_id=actor_section_id)
    locked_section_id = actor_section_id if actor_section_id and requirements.filter(section_id=actor_section_id).exists() else None

    dates = _dates_for_type(start, end, day_type)
    if not dates:
        day_label = "weekdays" if day_type == ShiftAssignment.DAYTYPE_WEEKDAY else "weekend days" if day_type == ShiftAssignment.DAYTYPE_WEEKEND else "dates"
        return JsonResponse({"ok": True, "slots": [], "locked_section_id": locked_section_id, "message": f"No {day_label} fall within this duty week."})

    requirements = list(requirements)
    if not requirements:
        return JsonResponse({
            "ok": True,
            "slots": [],
            "locked_section_id": locked_section_id,
            "message": "No active section is configured for this shift within the selected treatment plant.",
        })

    staff_by_section = {
        requirement.section_id: list(CustomUser.objects.filter(
            is_active=True,
            assigned_treatment_plant_id=plant_id,
            section_id=requirement.section_id,
        ).order_by("last_name", "first_name", "username"))
        for requirement in requirements
    }
    slots = []
    for assignment_date in dates:
        sections = []
        for requirement in requirements:
            staff = staff_by_section[requirement.section_id]
            if not staff:
                continue
            staff_ids = [person.pk for person in staff]
            unavailable = set(AnnualLeave.objects.filter(
                staff_id__in=staff_ids,
                treatment_plant_id=plant_id,
                status__in=AnnualLeave.UNAVAILABLE_STATUSES,
                start_date__lte=assignment_date,
                end_date__gte=assignment_date,
            ).values_list("staff_id", flat=True))
            assigned = list(ShiftAssignment.objects.filter(
                shift_id=shift.pk,
                date=assignment_date,
                is_deleted=False,
                staff__assigned_treatment_plant_id=plant_id,
                staff__section_id=requirement.section_id,
            ).select_related("staff", "shift"))
            if len(assigned) >= requirement.maximum_allowed:
                continue
            assigned_ids = {item.staff_id for item in assigned}
            overlapping_ids = {
                item.staff_id
                for item in ShiftAssignment.objects.filter(
                    staff__in=staff,
                    date__range=(assignment_date - timedelta(days=1), assignment_date + timedelta(days=1)),
                    is_deleted=False,
                    shift__is_deleted=False,
                    shift__treatment_plant_id=plant_id,
                ).select_related("shift")
                if _shifts_overlap(shift, assignment_date, item.shift, item.date)
            }
            candidates = [
                person for person in staff
                if person.pk not in unavailable
                and person.pk not in assigned_ids
                and person.pk not in overlapping_ids
            ]
            sections.append({
                "id": requirement.section_id,
                "name": requirement.section.name,
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
    has_active_staff = any(staff_by_section.values())
    has_candidates = any(section["users"] for slot in slots for section in slot["sections"])
    message = "" if has_candidates else "No active staff are assigned to this section." if not has_active_staff else "No staff are eligible for these dates; check current assignments, leave or off-duty records, and shift capacity."
    return JsonResponse({"ok": True, "slots": slots, "locked_section_id": locked_section_id, "message": message})


@login_required
@require_POST
def roster_assignment_action(request):
    data = _json_body(request)
    if data is None:
        return _json_error("The request body is not valid JSON.")
    action = data.get("action")
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
                if not _section_for_actor(request, shift.treatment_plant_id, section_id):
                    return _json_error("The selected section is outside your assigned scope.", 403)
                requirement = ShiftSectionRequirement.objects.select_for_update().filter(
                    shift_id=shift.pk,
                    section_id=section_id,
                    section__treatment_plant_id=shift.treatment_plant_id,
                    section__is_active=True,
                ).first()
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
                count = ShiftAssignment.objects.filter(
                    shift_id=shift.pk,
                    date=assignment_date,
                    is_deleted=False,
                    staff__assigned_treatment_plant_id=shift.treatment_plant_id,
                    staff__section_id=section_id,
                ).count()
                if count >= requirement.maximum_allowed:
                    return _json_error("This shift has reached its maximum staffing limit.", 409)
                if AnnualLeave.is_unavailable(staff.pk, assignment_date):
                    return _json_error("This staff member is on annual leave or off duty for that date.", 409)
                if _staff_has_conflict(staff.pk, assignment_date, shift):
                    return _json_error("This staff member already has an overlapping shift on that date.", 409)
                roster, _created = MonthlyDutyRoster.objects.get_or_create(
                    treatment_plant_id=shift.treatment_plant_id,
                    month=assignment_date.month,
                    year=assignment_date.year,
                    defaults={"created_by": request.user, "notes": "Created from the duty roster dashboard."},
                )
                assignment = ShiftAssignment(
                    duty_roster=roster,
                    shift=shift,
                    staff=staff,
                    date=assignment_date,
                    day_type=day_type,
                )
                assignment._audit_user = request.user
                assignment.save()
            else:
                assignment_id = int(data.get("assignment_id"))
                assignment = _scoped_assignment_queryset(request.user).select_for_update().filter(pk=assignment_id).first()
                if not assignment:
                    return _json_error("The roster entry was not found in your scope.", 404)
                target_shift = Shift.objects.filter(
                    is_deleted=False,
                    treatment_plant_id=assignment.shift.treatment_plant_id,
                    pk=shift_id,
                ).first()
                if not target_shift:
                    return _json_error("The destination shift is unavailable in this treatment plant.", 404)
                section_id = assignment.staff.section_id
                if not assignment.staff.is_superuser and not ShiftSectionRequirement.objects.filter(
                    shift_id=target_shift.pk,
                    section_id=section_id,
                    section__treatment_plant_id=target_shift.treatment_plant_id,
                    section__is_active=True,
                ).exists():
                    return _json_error("The destination shift is not configured for this staff member's section.", 409)
                start, end = _parse_range(data)
                if not start or not end or not start <= assignment_date <= end:
                    return _json_error("The destination date is outside the selected roster week.")
                requirement = ShiftSectionRequirement.objects.select_for_update().filter(
                    shift_id=target_shift.pk,
                    section_id=section_id,
                    section__treatment_plant_id=target_shift.treatment_plant_id,
                    section__is_active=True,
                ).first()
                if not requirement:
                    return _json_error("The destination shift is not configured for this staff member's section.", 409)
                assigned_count = ShiftAssignment.objects.filter(
                    shift_id=target_shift.pk,
                    date=assignment_date,
                    is_deleted=False,
                    staff__assigned_treatment_plant_id=target_shift.treatment_plant_id,
                    staff__section_id=section_id,
                ).exclude(pk=assignment.pk).count()
                if assigned_count >= requirement.maximum_allowed:
                    return _json_error("The destination shift has reached its maximum staffing limit.", 409)
                if AnnualLeave.is_unavailable(assignment.staff_id, assignment_date):
                    return _json_error("This staff member is on annual leave or off duty for that date.", 409)
                if _staff_has_conflict(assignment.staff_id, assignment_date, target_shift, exclude_id=assignment.pk):
                    return _json_error("This staff member already has an overlapping shift on that date.", 409)
                roster, _created = MonthlyDutyRoster.objects.get_or_create(
                    treatment_plant_id=target_shift.treatment_plant_id,
                    month=assignment_date.month,
                    year=assignment_date.year,
                    defaults={"created_by": request.user, "notes": "Created from the duty roster dashboard."},
                )
                assignment.shift = target_shift
                assignment.date = assignment_date
                assignment.day_type = day_type
                assignment.duty_roster = roster
                assignment._audit_user = request.user
                assignment.save()
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
