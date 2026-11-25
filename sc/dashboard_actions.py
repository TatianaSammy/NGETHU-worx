import json
from collections import Counter, defaultdict
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from core.models import Section
from core.plant_section_scope import section_scope_q, user_plant_section
from sc.models import (
    AnnualLeave,
    AuditLog,
    INCHARGE_GROUPS,
    MonthlyDutyRoster,
    Shift,
    ShiftAssignment,
    ShiftHandover,
    ShiftSectionRequirement,
    ShiftSupervisor,
    WeeklyShiftTeam,
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
        elif day_type == ShiftAssignment.DAYTYPE_HOLIDAY and current.weekday() < 5:
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


def _can_manage_temporary_supervisors(user):
    return _can_manage_weekly_supervisors(user) or _allowed(user, "sc.change_shiftassignment")


@login_required
@require_GET
def temporary_supervisor_options(request):
    if not _can_manage_temporary_supervisors(request.user):
        return _json_error("You do not have permission to appoint a temporary supervisor.", 403)
    try:
        plant_id = int(request.GET.get("plant_id", ""))
        section_id = int(request.GET.get("section_id", ""))
        shift_id = int(request.GET.get("shift_id", ""))
        assignment_date = date.fromisoformat(request.GET.get("date", ""))
    except (TypeError, ValueError):
        return _json_error("Choose a plant, section, shift, and date.")
    if assignment_date != timezone.localdate():
        return _json_error("Temporary supervisors can only be appointed for today's active duty.", 409)
    if not _section_for_actor(request, plant_id, section_id):
        return _json_error("The selected section is outside your assigned scope.", 403)

    shift = Shift.objects.filter(
        pk=shift_id,
        treatment_plant_id=plant_id,
        is_deleted=False,
        section_requirements__section_id=section_id,
        section_requirements__section__is_active=True,
    ).first()
    if not shift:
        return _json_error("The selected shift is not configured for this section.", 409)

    eligible_supervisor_ids = ShiftSupervisor.objects.filter(
        treatment_plant_id=plant_id,
        section_id=section_id,
        is_active=True,
        user__is_active=True,
        section__is_active=True,
    ).filter(
        Q(shift_id=shift_id)
        | Q(shift__isnull=True)
        | Q(shift__treatment_plant_id=plant_id)
    ).filter(
        Q(week_start__isnull=True)
        | Q(week_start__lte=assignment_date, week_end__gte=assignment_date)
    ).values("user_id")
    candidates = CustomUser.objects.filter(is_active=True).filter(
        Q(pk__in=eligible_supervisor_ids)
        | Q(groups__name__in=INCHARGE_GROUPS)
    ).filter(
        Q(is_superuser=True)
        | Q(assigned_treatment_plant_id=plant_id, section_id=section_id)
    ).exclude(
        annual_leave_records__status__in=AnnualLeave.UNAVAILABLE_STATUSES,
        annual_leave_records__start_date__lte=assignment_date,
        annual_leave_records__end_date__gte=assignment_date,
    ).exclude(
        shift_assignments__date=assignment_date,
        shift_assignments__is_deleted=False,
    ).exclude(
        temp_supervised_assignments__date=assignment_date,
        temp_supervised_assignments__is_deleted=False,
        temp_supervised_assignments__temp_supervisor_start__lte=timezone.now(),
        temp_supervised_assignments__temp_supervisor_end__gt=timezone.now(),
    )
    from weeklytasks.models import DailyTaskAssignment
    candidates = candidates.exclude(pk__in=DailyTaskAssignment.objects.filter(
        date=assignment_date,
    ).values("user_id"))

    users = candidates.distinct().order_by("last_name", "first_name", "username")
    return JsonResponse({
        "ok": True,
        "users": [{"id": user.pk, "label": user.get_full_name() or user.get_username()} for user in users],
    })


def _assign_temporary_supervisor(request, data):
    if not _can_manage_temporary_supervisors(request.user):
        return _json_error("You do not have permission to appoint a temporary supervisor.", 403)
    try:
        assignment_id = int(data.get("assignment_id"))
        user_id = int(data.get("user_id"))
        duration_hours = float(data.get("duration_hours", 3))
    except (TypeError, ValueError):
        return _json_error("Choose a roster assignment, supervisor, and valid duration.")
    try:
        with transaction.atomic():
            if not _scoped_assignment_queryset(request.user).filter(pk=assignment_id).exists():
                return _json_error("The roster entry was not found in your scope.", 404)
            assignment = ShiftAssignment.objects.select_for_update().select_related(
                "staff", "staff__section", "shift"
            ).filter(pk=assignment_id, is_deleted=False).first()
            if not assignment or not _scoped_assignment_queryset(request.user).filter(pk=assignment_id).exists():
                return _json_error("The roster entry was not found in your scope.", 404)
            supervisor = CustomUser.objects.select_for_update().filter(pk=user_id, is_active=True).first()
            if not supervisor:
                return _json_error("The selected temporary supervisor is inactive or unavailable.", 404)
            assignment.assign_temp_supervisor(supervisor, duration_hours=duration_hours)
    except (IntegrityError, ValidationError) as error:
        detail = "; ".join(error.messages) if isinstance(error, ValidationError) else "Could not appoint the temporary supervisor."
        return _json_error(detail, 409)
    return JsonResponse({
        "ok": True,
        "message": f"{supervisor.get_full_name() or supervisor.get_username()} is temporary supervisor until {assignment.temp_supervisor_end:%H:%M}.",
    })


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
            week_dates = []
            current_date = week_start
            while current_date <= week_end:
                week_dates.append(current_date)
                current_date += timedelta(days=1)
            holiday_dates = set(ShiftAssignment.objects.select_for_update().filter(
                shift__treatment_plant_id=plant_id,
                date__range=(week_start, week_end),
                day_type=ShiftAssignment.DAYTYPE_HOLIDAY,
                is_deleted=False,
            ).values_list("date", flat=True))
            existing_target_rows = list(ShiftAssignment.objects.select_for_update().filter(
                shift_id=shift_id,
                staff_id=user_id,
                staff__section_id=section_id,
                date__range=(week_start, week_end),
                is_deleted=False,
            ))
            existing_target_dates = {row.date for row in existing_target_rows}
            if existing_target_rows:
                for target_date in week_dates:
                    if target_date in existing_target_dates:
                        continue
                    target_day_type = (
                        ShiftAssignment.DAYTYPE_WEEKEND
                        if target_date.weekday() >= 5
                        else ShiftAssignment.DAYTYPE_HOLIDAY
                        if target_date in holiday_dates
                        else ShiftAssignment.DAYTYPE_WEEKDAY
                    )
                    can_accept, capacity_message = shift.can_accept_assignment(
                        section_id,
                        target_date,
                        day_type=target_day_type,
                    )
                    is_eligible, eligibility_message = shift.is_staff_eligible(
                        staff,
                        target_date,
                        section=section_id,
                    )
                    if not can_accept:
                        return _json_error(capacity_message, 409)
                    if not is_eligible:
                        return _json_error(eligibility_message, 409)
                for target_date in week_dates:
                    if target_date in existing_target_dates:
                        continue
                    target_day_type = (
                        ShiftAssignment.DAYTYPE_WEEKEND
                        if target_date.weekday() >= 5
                        else ShiftAssignment.DAYTYPE_HOLIDAY
                        if target_date in holiday_dates
                        else ShiftAssignment.DAYTYPE_WEEKDAY
                    )
                    ShiftAssignment.create_assignment(
                        staff=staff,
                        shift=shift,
                        date=target_date,
                        day_type=target_day_type,
                        created_by=request.user,
                    )
            has_assignment = bool(existing_target_rows) and all(
                day in existing_target_dates or ShiftAssignment.objects.filter(
                    shift_id=shift_id,
                    staff_id=user_id,
                    staff__section_id=section_id,
                    date=day,
                    is_deleted=False,
                ).exists()
                for day in week_dates
            )

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
                    source_rows = list(ShiftAssignment.objects.select_for_update().filter(
                        staff_id=staff.pk,
                        shift_id=moved_assignment.shift_id,
                        staff__section_id=section_id,
                        date__range=(week_start, week_end),
                        is_deleted=False,
                    ).order_by("date", "pk"))
                    if not source_rows:
                        source_rows = [moved_assignment]
                    for source_row in source_rows:
                        target_day_type = (
                            ShiftAssignment.DAYTYPE_WEEKEND
                            if source_row.date.weekday() >= 5
                            else ShiftAssignment.DAYTYPE_HOLIDAY
                            if source_row.day_type == ShiftAssignment.DAYTYPE_HOLIDAY
                            else ShiftAssignment.DAYTYPE_WEEKDAY
                        )
                        can_accept, capacity_message = shift.can_accept_assignment(
                            section_id,
                            source_row.date,
                            day_type=target_day_type,
                            exclude_assignment_id=source_row.pk,
                        )
                        is_eligible, eligibility_message = shift.is_staff_eligible(
                            staff,
                            source_row.date,
                            section=section_id,
                            exclude_assignment_id=source_row.pk,
                        )
                        if not is_eligible:
                            raise ValidationError(eligibility_message)
                        if not can_accept:
                            raise ValidationError(capacity_message)
                        source_row.move_assignment(
                            target_shift=shift,
                            target_date=source_row.date,
                            day_type=target_day_type,
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
    if not (_allowed(request.user, "sc.add_shiftassignment") or _allowed(request.user, "sc.change_shiftassignment")):
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

    dates = []
    current_date = start
    while current_date <= end:
        dates.append(current_date)
        current_date += timedelta(days=1)
    requested_date = (request.GET.get("date") or "").strip()
    if requested_date:
        try:
            requested_date = date.fromisoformat(requested_date)
        except ValueError:
            return _json_error("Choose a valid duty date.")
        if requested_date not in dates:
            return _json_error("The selected date is outside this roster week.", 409)
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

    holiday_dates = set(ShiftAssignment.objects.filter(
        shift__treatment_plant_id=plant_id,
        date__range=(start, end),
        day_type=ShiftAssignment.DAYTYPE_HOLIDAY,
        is_deleted=False,
    ).values_list("date", flat=True))
    if day_type == ShiftAssignment.DAYTYPE_WEEKDAY:
        dates = [day for day in dates if day.weekday() < 5 and day not in holiday_dates]
    elif day_type == ShiftAssignment.DAYTYPE_WEEKEND:
        dates = [day for day in dates if day.weekday() >= 5]
    else:
        dates = [day for day in dates if day in holiday_dates]
    if not dates:
        message = (
            "No public holiday dates are recorded in this roster week."
            if day_type == ShiftAssignment.DAYTYPE_HOLIDAY
            else "No dates in this roster week match the selected staffing category."
        )
        return JsonResponse({
            "ok": True,
            "slots": [],
            "plant": {"id": shift.treatment_plant_id, "name": shift.treatment_plant.name},
            "locked_section_id": locked_section_id,
            "preferred_section_id": preferred_section or None,
            "can_select_section": request.user.is_superuser,
            "message": message,
        })
    if requested_date and requested_date not in dates:
        return _json_error("The selected date does not match a recorded date in this staffing category.", 409)
    assigned_counts = {
        (row["date"], row["staff__section_id"]): row["total"]
        for row in ShiftAssignment.objects.filter(
            shift_id=shift.pk,
            date__range=(start, end),
            staff__section_id__in=[requirement.section_id for requirement in requirements],
            is_deleted=False,
        ).values("date", "staff__section_id").annotate(total=Count("pk"))
    }

    slots = []
    capacity_messages = []
    for assignment_date in dates:
        sections = []
        for requirement in requirements:
            assignment_day_type = (
                ShiftAssignment.DAYTYPE_WEEKEND
                if assignment_date.weekday() >= 5
                else ShiftAssignment.DAYTYPE_HOLIDAY
                if assignment_date in holiday_dates
                else ShiftAssignment.DAYTYPE_WEEKDAY
            )
            can_accept, capacity_message = shift.can_accept_assignment(
                requirement.section,
                assignment_date,
                day_type=assignment_day_type,
            )
            assigned_count = assigned_counts.get((assignment_date, requirement.section_id), 0)
            candidates = shift.get_available_staff(assignment_date, section=requirement.section) if can_accept else []
            staffing_limit = requirement.maximum_allowed
            if capacity_message:
                capacity_messages.append(capacity_message)
            sections.append({
                "id": requirement.section_id,
                "name": requirement.section.name,
                "capacity_available": can_accept,
                "capacity_message": capacity_message or "",
                "max_selectable": max(0, staffing_limit - assigned_count),
                "users": [{
                    "id": person.pk,
                    "name": person.get_full_name() or person.get_username(),
                } for person in candidates],
            })
        slots.append({
            "date": assignment_date.isoformat(),
            "label": assignment_date.strftime("%a, %d %b"),
            "day_type": assignment_day_type,
            "sections": sections,
        })
    if slots:
        # A category crew must be available and fit the section limit on every
        # date represented by the selected category cell.
        common_users_by_section = {}
        for requirement in requirements:
            available_on_every_date = None
            for slot in slots:
                section = next(
                    (item for item in slot["sections"] if item["id"] == requirement.section_id),
                    None,
                )
                user_ids = {person["id"] for person in section["users"]} if section else set()
                available_on_every_date = (
                    user_ids if available_on_every_date is None
                    else available_on_every_date.intersection(user_ids)
                )
            common_users_by_section[requirement.section_id] = available_on_every_date or set()
        for slot in slots:
            for section in slot["sections"]:
                common_ids = common_users_by_section.get(section["id"], set())
                section["users"] = [person for person in section["users"] if person["id"] in common_ids]
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
    availability_status = str(data.get("status") or AnnualLeave.STATUS_ANNUAL_LEAVE).strip()
    if availability_status not in AnnualLeave.EXEMPT_FROM_ROUTINE_STATUSES:
        return _json_error("Choose annual leave, off duty, or in charge.")

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

            if availability_status != AnnualLeave.STATUS_IN_CHARGE:
                # Leave and off-duty periods also clear supervisory duties. In-charge
                # status exempts routine work while preserving supervisory coverage.
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

                ShiftHandover.objects.filter(
                    date__range=(week_start, week_end),
                    incoming_supervisor_id=staff_id,
                ).update(incoming_supervisor=None)

                ShiftHandover.objects.filter(
                    date__range=(week_start, week_end),
                    outgoing_supervisor_id=staff_id,
                ).update(outgoing_supervisor=None)

            # 4. Remove all roster assignments for this staff during this week
            if staff.section_id:
                WeeklyShiftTeam.objects.filter(
                    duty_roster__treatment_plant_id=plant_id,
                    section_id=staff.section_id,
                    week_start__lte=week_end,
                    week_end__gte=week_start,
                ).update(is_reconciled=False, updated_at=timezone.now())
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
                    status=availability_status,
                    reason=f"Availability set from weekly duty roster by {request.user.get_username()}.",
                    created_by=request.user,
                    updated_by=request.user,
                )
            else:
                leave.start_date = min(leave.start_date, week_start)
                leave.end_date = max(leave.end_date, week_end)
                leave.status = availability_status
                leave.updated_by = request.user
                # Remove week-level supervisor anchors before their roster rows.
                ShiftSupervisor.objects.filter(
                    user_id=staff_id,
                    week_start__isnull=False,
                    week_start__lte=leave.end_date,
                    week_end__gte=leave.start_date,
                ).delete()
                if availability_status != AnnualLeave.STATUS_IN_CHARGE:
                    ShiftHandover.objects.filter(
                        date__range=(leave.start_date, leave.end_date),
                        incoming_supervisor_id=staff_id,
                    ).update(incoming_supervisor=None)
                    ShiftHandover.objects.filter(
                        date__range=(leave.start_date, leave.end_date),
                        outgoing_supervisor_id=staff_id,
                    ).update(outgoing_supervisor=None)
                if staff.section_id:
                    WeeklyShiftTeam.objects.filter(
                        duty_roster__treatment_plant_id=plant_id,
                        section_id=staff.section_id,
                        week_start__lte=leave.end_date,
                        week_end__gte=leave.start_date,
                    ).update(is_reconciled=False, updated_at=timezone.now())
                ShiftAssignment.objects.filter(
                    staff_id=staff_id,
                    date__range=(leave.start_date, leave.end_date),
                    is_deleted=False,
                ).delete()

            _remove_staff_from_weekly_teams(
                staff_id=staff.pk,
                plant_id=plant.pk,
                section_id=staff.section_id,
                start_date=leave.start_date,
                end_date=leave.end_date,
            )
            leave.full_clean()
            leave.save()

            status_label = leave.get_status_display().lower()
            return JsonResponse({
                "ok": True,
                "message": f"{staff.get_full_name() or staff.get_username()} marked {status_label} for {week_start:%d %b}–{week_end:%d %b %Y}.",
                "staff_id": staff.pk,
                "leave_id": leave.pk,
            })
    except (IntegrityError, ValidationError) as error:
        detail = "; ".join(error.messages) if isinstance(error, ValidationError) else "Could not record annual leave due to a conflict."
        return _json_error(detail, 409)


def _remove_staff_from_weekly_teams(*, staff_id, plant_id, section_id, start_date, end_date):
    teams = WeeklyShiftTeam.objects.filter(
        duty_roster__treatment_plant_id=plant_id,
        week_start__lte=end_date,
        week_end__gte=start_date,
    )
    if section_id:
        teams = teams.filter(section_id=section_id)
    for team in teams.prefetch_related("members"):
        if team.members.filter(pk=staff_id).exists():
            team.members.through.objects.filter(team_id=team.pk, staff_id=staff_id).delete()
            team.refresh_reconciliation()


def _rebuild_weekly_teams_action(request, data):
    if not _allowed(request.user, "sc.change_shiftassignment"):
        return _json_error("You do not have permission to rebuild duty teams.", 403)
    try:
        plant_id = int(data.get("plant_id"))
        section_id = int(data.get("section_id"))
    except (TypeError, ValueError):
        return _json_error("Choose a treatment plant and section.")
    week_start, week_end = _parse_range(data)
    if not week_start or not week_end or not _is_roster_week(week_start, week_end):
        return _json_error("Choose a valid duty roster week.")
    if not _section_for_actor(request, plant_id, section_id):
        return _json_error("The selected section is outside your assigned scope.", 403)

    section = Section.objects.filter(pk=section_id, is_active=True).filter(
        section_scope_q("treatment_plant_id", plant_id)
    ).first()
    if not section:
        return _json_error("The selected section is not available at this treatment plant.", 404)
    if not ShiftSectionRequirement.objects.filter(
        section_id=section_id,
        section__is_active=True,
        shift__is_deleted=False,
        shift__treatment_plant_id=plant_id,
    ).filter(section_scope_q("section__treatment_plant_id", plant_id)).exists():
        return _json_error("No active shift requirements are configured for this section.", 409)

    try:
        with transaction.atomic():
            roster = MonthlyDutyRoster.objects.select_for_update().filter(
                treatment_plant_id=plant_id,
                year=week_start.year,
                month=week_start.month,
            ).first()
            if not roster:
                return _json_error("Create this month's duty roster before rebuilding its teams.", 404)
            MonthlyDutyRoster.assign_monthly_shifts_bulk(
                roster,
                section,
                start_date=week_start,
                end_date=week_end,
            )
    except (IntegrityError, ValidationError) as error:
        detail = "; ".join(error.messages) if isinstance(error, ValidationError) else "The weekly team rebuild conflicts with another assignment."
        return _json_error(detail, 409)

    return JsonResponse({
        "ok": True,
        "message": f"Weekly crews auto-populated and rotated for {section.name}, {week_start:%d %b}–{week_end:%d %b %Y}.",
    })


def _autopopulate_year_action(request, data):
    if not _allowed(request.user, "sc.change_shiftassignment"):
        return _json_error("You do not have permission to generate the duty roster.", 403)
    try:
        year = int(data.get("year"))
    except (TypeError, ValueError):
        return _json_error("Choose a valid roster year.")
    if not 2000 <= year <= 2100:
        return _json_error("Choose a year between 2000 and 2100.")
    def optional_id(name):
        value = data.get(name)
        if value in (None, ""):
            return None
        return int(value)

    try:
        plant_filter = optional_id("plant_id")
        section_filter = optional_id("section_id")
    except (TypeError, ValueError):
        return _json_error("Choose a valid treatment plant or section filter.")

    if not request.user.is_superuser:
        actor_plant_id, actor_section_id = user_plant_section(request.user)
        if not actor_plant_id or not actor_section_id:
            return _json_error("Your account does not have a valid treatment plant and section scope.", 403)
        if plant_filter and plant_filter != actor_plant_id:
            return _json_error("The selected treatment plant is outside your assigned scope.", 403)
        if section_filter and section_filter != actor_section_id:
            return _json_error("The selected section is outside your assigned scope.", 403)
        plant_filter, section_filter = actor_plant_id, actor_section_id

    requirements = ShiftSectionRequirement.objects.filter(
        section__is_active=True,
        shift__is_deleted=False,
    ).filter(section_scope_q(
        "section__treatment_plant_id",
        F("shift__treatment_plant_id"),
    ))
    if plant_filter:
        requirements = requirements.filter(shift__treatment_plant_id=plant_filter)
    if section_filter:
        requirements = requirements.filter(section_id=section_filter)
    scopes = list(requirements.order_by().values_list(
        "shift__treatment_plant_id", "section_id"
    ).distinct().order_by("shift__treatment_plant_id", "section_id"))
    if not scopes:
        return _json_error("No active shift requirements are configured in the selected roster scope.", 409)

    completed_scopes = []
    failures = []
    assignment_count = 0
    for plant_id, section_id in scopes:
        plant = TreatmentPlant.objects.filter(pk=plant_id).first()
        section = Section.objects.filter(pk=section_id, is_active=True).filter(
            section_scope_q("treatment_plant_id", plant_id)
        ).first()
        if not plant or not section:
            failures.append(f"Plant {plant_id}, section {section_id}: scope is no longer active.")
            continue
        if not CustomUser.objects.filter(
            is_active=True,
            assigned_treatment_plant_id=plant_id,
            section_id=section_id,
        ).exists():
            failures.append(f"{section.name} ({plant.name}): no active staff are assigned.")
            continue

        current_month = 1
        try:
            # Each scope is atomic across all twelve months. A conflict in one
            # week rolls that section's year back without blocking other scopes.
            with transaction.atomic():
                for current_month in range(1, 13):
                    roster, _created = MonthlyDutyRoster.objects.get_or_create(
                        treatment_plant=plant,
                        year=year,
                        month=current_month,
                        defaults={"created_by": request.user},
                    )
                    roster = MonthlyDutyRoster.objects.select_for_update().get(pk=roster.pk)
                    MonthlyDutyRoster.assign_monthly_shifts_bulk(
                        roster,
                        section,
                        rotation_start_date=date(year, 1, 1),
                    )
            assignment_count += ShiftAssignment.objects.filter(
                duty_roster__treatment_plant_id=plant_id,
                duty_roster__year=year,
                staff__section_id=section_id,
                is_deleted=False,
            ).count()
            completed_scopes.append((plant.name, section.name))
        except (IntegrityError, ValidationError) as error:
            detail = "; ".join(error.messages) if isinstance(error, ValidationError) else "a database conflict occurred"
            month_label = date(year, current_month, 1).strftime("%B")
            failures.append(f"{section.name} ({plant.name}), {month_label}: {detail}")

    if not completed_scopes:
        return _json_error("No yearly rosters were generated. " + " ".join(failures[:3]), 409)

    scope_summary = ", ".join(f"{section_name} · {plant_name}" for plant_name, section_name in completed_scopes[:3])
    if len(completed_scopes) > 3:
        scope_summary += f" and {len(completed_scopes) - 3} more scope(s)"
    message = (
        f"Generated all 12 monthly rosters for {len(completed_scopes)} plant/section scope(s) "
        f"in {year}: {scope_summary} ({assignment_count} active duty assignments)."
    )
    if failures:
        message += f" {len(failures)} scope(s) could not be generated: " + " ".join(failures[:3])
    populated_months = len(completed_scopes) * 12
    return JsonResponse({
        "ok": True,
        "message": message,
        "year": year,
        "months": populated_months,
        "assignments": assignment_count,
        "failed_scopes": failures,
    })


def _move_mixed_source_weekly_crew(
    request,
    *,
    swap_staff_ids,
    source_rows,
    target_shift,
    section_id,
    week_start,
    week_end,
    assignment_date,
    day_type,
):
    moving_rows = [row for row in source_rows if row.shift_id != target_shift.pk]
    moving_staff_ids = {row.staff_id for row in moving_rows}
    if not moving_rows:
        return JsonResponse({"ok": True, "message": "The selected staff are already on this shift."})

    week_dates = []
    cursor = week_start
    while cursor <= week_end:
        week_dates.append(cursor)
        cursor += timedelta(days=1)
    holiday_dates = set(ShiftAssignment.objects.select_for_update().filter(
        shift__treatment_plant_id=target_shift.treatment_plant_id,
        date__range=(week_start, week_end),
        day_type=ShiftAssignment.DAYTYPE_HOLIDAY,
        is_deleted=False,
    ).values_list("date", flat=True))
    category_by_date = {
        roster_date: (
            ShiftAssignment.DAYTYPE_WEEKEND
            if roster_date.weekday() >= 5
            else ShiftAssignment.DAYTYPE_HOLIDAY
            if roster_date in holiday_dates
            else ShiftAssignment.DAYTYPE_WEEKDAY
        )
        for roster_date in week_dates
    }
    if day_type == ShiftAssignment.DAYTYPE_HOLIDAY and assignment_date not in holiday_dates:
        raise ValidationError("Choose a configured public holiday date in this week.")

    source_shift_ids = {row.shift_id for row in moving_rows}
    source_shifts = {
        shift.pk: shift
        for shift in Shift.objects.select_for_update().filter(
            pk__in=source_shift_ids,
            treatment_plant_id=target_shift.treatment_plant_id,
            is_deleted=False,
        ).order_by("pk")
    }
    if set(source_shifts) != source_shift_ids:
        return _json_error("One or more source shifts are no longer available at this plant.", 409)
    source_rosters = {}
    source_crews = {}
    source_teams = {}
    moved_ids_by_shift = defaultdict(set)
    source_rows_by_shift = defaultdict(list)
    for row in moving_rows:
        source_rows_by_shift[row.shift_id].append(row)
        moved_ids_by_shift[row.shift_id].add(row.staff_id)
        source_rosters.setdefault(row.shift_id, row.duty_roster)

    source_assignment_ids = {row.pk for row in moving_rows}
    for source_shift_id in source_shift_ids:
        shift_rows = list(ShiftAssignment.objects.select_for_update().filter(
            shift_id=source_shift_id,
            staff__section_id=section_id,
            date__range=(week_start, week_end),
            is_deleted=False,
        ).select_related("staff", "shift", "duty_roster").order_by("date", "staff_id", "pk"))
        daily_crews = defaultdict(set)
        for row in shift_rows:
            daily_crews[row.date].add(row.staff_id)
        weekly_crew = set().union(*daily_crews.values()) if daily_crews else set()
        source_crews[source_shift_id] = weekly_crew
        team, _created = WeeklyShiftTeam.get_for_date(
            duty_roster=source_rosters[source_shift_id],
            shift=source_shifts[source_shift_id],
            section=shift_rows[0].staff.section,
            target_date=week_start,
            create=False,
        )
        source_teams[source_shift_id] = team

    target_requirement = ShiftSectionRequirement.objects.select_for_update().filter(
        shift_id=target_shift.pk,
        section_id=section_id,
        section__is_active=True,
    ).filter(section_scope_q("section__treatment_plant_id", target_shift.treatment_plant_id)).first()
    if not target_requirement:
        return _json_error("The destination shift is not configured for this section.", 409)

    target_daily_crews = defaultdict(set)
    for roster_date, person_id in ShiftAssignment.objects.select_for_update().filter(
        shift_id=target_shift.pk,
        staff__section_id=section_id,
        date__range=(week_start, week_end),
        is_deleted=False,
    ).values_list("date", "staff_id"):
        target_daily_crews[roster_date].add(person_id)
    target_weekly_crew = set().union(*target_daily_crews.values()) if target_daily_crews else set()
    target_roster, _created = MonthlyDutyRoster.objects.get_or_create(
        treatment_plant_id=target_shift.treatment_plant_id,
        month=week_start.month,
        year=week_start.year,
    )
    target_team, _created = WeeklyShiftTeam.get_for_date(
        duty_roster=target_roster,
        shift=target_shift,
        section=moving_rows[0].staff.section,
        target_date=week_start,
        create=False,
    )

    incoming_count_by_date = Counter(row.date for row in moving_rows)
    over_capacity = False
    for roster_date, incoming_count in incoming_count_by_date.items():
        current_count = ShiftAssignment.objects.select_for_update().filter(
            shift_id=target_shift.pk,
            staff__section_id=section_id,
            date=roster_date,
            is_deleted=False,
        ).count()
        category = category_by_date[roster_date]
        limit = (
            target_requirement.get_required_staff(category)
            if category in {ShiftAssignment.DAYTYPE_WEEKEND, ShiftAssignment.DAYTYPE_HOLIDAY}
            else target_requirement.maximum_allowed
        )
        over_capacity = over_capacity or current_count + incoming_count > limit
    over_capacity = over_capacity or len(target_weekly_crew | moving_staff_ids) > target_requirement.maximum_allowed
    if swap_staff_ids and not over_capacity:
        return _json_error("The destination has room now. Retry the move without a swap.", 409)

    incoming_patterns = {
        person_id: Counter(row.date for row in moving_rows if row.staff_id == person_id)
        for person_id in moving_staff_ids
    }
    destination_rows = list(ShiftAssignment.objects.select_for_update().filter(
        shift_id=target_shift.pk,
        staff__section_id=section_id,
        date__range=(week_start, week_end),
        is_deleted=False,
    ).select_related("staff").order_by("date", "staff_id", "pk"))
    destination_rows_by_staff = defaultdict(list)
    for row in destination_rows:
        destination_rows_by_staff[row.staff_id].append(row)
    candidate_ids = {
        person_id for person_id in target_weekly_crew
        if person_id not in moving_staff_ids
        and destination_rows_by_staff.get(person_id)
        and not any(row.handover_to_id or row.temp_supervisor_id for row in destination_rows_by_staff[person_id])
        and Counter(row.date for row in destination_rows_by_staff[person_id]) in incoming_patterns.values()
    }

    if over_capacity and swap_staff_ids is None:
        needed_patterns = Counter(
            tuple(sorted(pattern.items())) for pattern in incoming_patterns.values()
        )
        available_patterns = Counter(
            tuple(sorted(Counter(row.date for row in destination_rows_by_staff[person_id]).items()))
            for person_id in candidate_ids
        )
        if any(available_patterns[pattern] < count for pattern, count in needed_patterns.items()):
            return _json_error("The destination is full, but there are not enough eligible staff to swap with this crew.", 409)
        candidates = [
            {
                "id": person_id,
                "name": destination_rows_by_staff[person_id][0].staff.get_full_name()
                or destination_rows_by_staff[person_id][0].staff.get_username(),
                "schedule": ", ".join(
                    roster_date.strftime("%a")
                    for roster_date in sorted({row.date for row in destination_rows_by_staff[person_id]})
                ),
            }
            for person_id in sorted(candidate_ids, key=lambda item: (
                destination_rows_by_staff[item][0].staff.last_name,
                destination_rows_by_staff[item][0].staff.first_name,
                destination_rows_by_staff[item][0].staff.get_username(),
            ))
        ]
        return JsonResponse({
            "ok": False,
            "message": "The destination is full. Select staff there to swap with the dragged crew.",
            "swap_required": True,
            "swap_count": len(moving_staff_ids),
            "swap_candidates": candidates,
        }, status=409)
    if not over_capacity:
        swap_staff_ids = []
    if over_capacity:
        if not isinstance(swap_staff_ids, list) or len(swap_staff_ids) != len(moving_staff_ids):
            raise ValidationError(f"Select exactly {len(moving_staff_ids)} destination staff to swap.")
        if len(set(swap_staff_ids)) != len(swap_staff_ids) or set(swap_staff_ids) & moving_staff_ids:
            raise ValidationError("Choose distinct destination staff who are not part of the dragged crew.")
        if not set(swap_staff_ids).issubset(candidate_ids):
            raise ValidationError("Choose eligible staff from the destination crew.")
        outgoing_rows = [row for person_id in swap_staff_ids for row in destination_rows_by_staff[person_id]]
        if Counter(row.date for row in outgoing_rows) != Counter(row.date for row in moving_rows):
            raise ValidationError("Select destination staff assigned on the same roster dates as the dragged crew.")
        available_outgoing = set(swap_staff_ids)
        outgoing_for_incoming = {}
        for person_id in sorted(moving_staff_ids):
            pattern = incoming_patterns[person_id]
            match = next((candidate_id for candidate_id in sorted(available_outgoing)
                          if Counter(row.date for row in destination_rows_by_staff[candidate_id]) == pattern), None)
            if match is None:
                raise ValidationError("Choose one destination staff member with matching roster dates for each dragged staff member.")
            outgoing_for_incoming[person_id] = match
            available_outgoing.remove(match)
    else:
        outgoing_rows = []
        outgoing_for_incoming = {}

    if any(row.handover_to_id or row.temp_supervisor_id for row in moving_rows):
        raise ValidationError("A selected source assignment has an active handover or temporary supervisor and cannot be moved.")
    outgoing_assignment_ids = {row.pk for row in outgoing_rows}
    all_move_assignment_ids = source_assignment_ids | outgoing_assignment_ids
    source_shift_by_staff_date = {(row.staff_id, row.date): row.shift_id for row in moving_rows}
    outgoing_shift_by_staff_date = {}
    for person_id, outgoing_id in outgoing_for_incoming.items():
        for row in destination_rows_by_staff[outgoing_id]:
            outgoing_shift_by_staff_date[(outgoing_id, row.date)] = source_shift_by_staff_date[(person_id, row.date)]

    for row in moving_rows:
        eligible, reason = target_shift.is_staff_eligible(
            row.staff,
            row.date,
            section=section_id,
            exclude_assignment_ids=all_move_assignment_ids,
        )
        if not eligible:
            raise ValidationError(f"{row.staff.get_full_name() or row.staff.get_username()} cannot move on {row.date:%A, %d %b}: {reason}")
    for row in outgoing_rows:
        source_shift = source_shifts[outgoing_shift_by_staff_date[(row.staff_id, row.date)]]
        eligible, reason = source_shift.is_staff_eligible(
            row.staff,
            row.date,
            section=section_id,
            exclude_assignment_ids=all_move_assignment_ids,
        )
        if not eligible:
            raise ValidationError(f"{row.staff.get_full_name() or row.staff.get_username()} cannot move on {row.date:%A, %d %b}: {reason}")

    note = f"Weekly crew move by {request.user.get_username()} at {timezone.localtime():%Y-%m-%d %H:%M}."
    before_by_assignment = {
        row.pk: {"shift": row.shift_id, "staff": row.staff_id, "date": row.date.isoformat()}
        for row in moving_rows + outgoing_rows
    }
    for row in moving_rows + outgoing_rows:
        if row.handover_to_id or row.temp_supervisor_id:
            raise ValidationError("Assignments with a handover or temporary supervisor cannot be moved.")
        row._weekly_team_change_allowed = True
        try:
            row.delete()
        finally:
            del row._weekly_team_change_allowed

    people_leaving_by_shift = {shift_id: set(ids) for shift_id, ids in moved_ids_by_shift.items()}
    people_added_by_shift = defaultdict(set)
    for (_person_id, _roster_date), source_shift_id in outgoing_shift_by_staff_date.items():
        people_added_by_shift[source_shift_id].add(_person_id)
    old_shift_people = {shift_id: set(ids) for shift_id, ids in people_leaving_by_shift.items()}
    for shift_id, person_ids in people_added_by_shift.items():
        old_shift_people.setdefault(shift_id, set()).update(person_ids)
    old_shift_people[target_shift.pk] = set(swap_staff_ids or [])
    shifts_for_supervisors = dict(source_shifts)
    shifts_for_supervisors[target_shift.pk] = target_shift
    for old_shift_id, person_ids in old_shift_people.items():
        old_shift = shifts_for_supervisors[old_shift_id]
        for person_id in person_ids:
            ShiftSupervisor.objects.filter(
                user_id=person_id,
                shift_id=old_shift_id,
                section_id=section_id,
                week_start=week_start,
                week_end=week_end,
                week_start__isnull=False,
            ).delete()
            fallback = ShiftSupervisor.get_active_supervisor(
                old_shift.treatment_plant_id,
                section_id,
                shift_id=old_shift_id,
                assignment_date=week_start,
            )
            ShiftAssignment.objects.filter(
                shift_id=old_shift_id,
                staff__section_id=section_id,
                supervisor_id=person_id,
                date__range=(week_start, week_end),
                is_deleted=False,
            ).update(supervisor=fallback)

    for source_shift_id in source_shift_ids:
        team = source_teams[source_shift_id]
        if team is None:
            team, _created = WeeklyShiftTeam.get_for_date(
                duty_roster=source_rosters[source_shift_id],
                shift=source_shifts[source_shift_id],
                section=source_rows_by_shift[source_shift_id][0].staff.section,
                target_date=week_start,
                create=True,
            )
            source_teams[source_shift_id] = team
        team.replace_staff_ids(
            (source_crews[source_shift_id] - moved_ids_by_shift[source_shift_id]) | people_added_by_shift[source_shift_id],
            reconciled=False,
        )
    if target_team is None:
        target_team, _created = WeeklyShiftTeam.get_for_date(
            duty_roster=target_roster,
            shift=target_shift,
            section=moving_rows[0].staff.section,
            target_date=week_start,
            create=True,
        )
    target_team.replace_staff_ids(
        (target_weekly_crew - set(swap_staff_ids or [])) | moving_staff_ids,
        reconciled=False,
    )

    move_plan = [(row, target_shift, target_roster) for row in moving_rows]
    for row in outgoing_rows:
        source_shift = source_shifts[outgoing_shift_by_staff_date[(row.staff_id, row.date)]]
        move_plan.append((row, source_shift, source_rosters[source_shift.pk]))
    for row, destination_shift, destination_roster in move_plan:
        row.shift = destination_shift
        row.duty_roster = destination_roster
        row.day_type = category_by_date[row.date]
        row.supervisor = ShiftSupervisor.get_active_supervisor(
            destination_shift.treatment_plant_id,
            section_id,
            shift_id=destination_shift.pk,
            assignment_date=row.date,
        )
        row.remarks = f"{row.remarks}\n{note}".strip() if row.remarks else note
        row.is_deleted = False
        row.deleted_at = None
        row._weekly_team_change_allowed = True
        row._eligibility_excluded_assignment_ids = all_move_assignment_ids
        try:
            row.save()
        finally:
            del row._weekly_team_change_allowed
            del row._eligibility_excluded_assignment_ids
        ShiftAssignment.create_audit(
            user=request.user,
            action="update",
            instance=row,
            before=before_by_assignment[row.pk],
            after={"shift": destination_shift.pk, "staff": row.staff_id, "date": row.date.isoformat()},
        )
    for team in list(source_teams.values()) + [target_team]:
        team.refresh_reconciliation()
    first_assignment = moving_rows[0]
    return JsonResponse({
        "ok": True,
        "message": "Weekly staff moved across shifts." if not outgoing_rows else "Weekly crews swapped across shifts.",
        "assignment_id": first_assignment.pk,
        "staff": first_assignment.staff.get_full_name() or first_assignment.staff.get_username(),
        "date": first_assignment.date.isoformat(),
        "shift": target_shift.name,
    })


def _flexible_roster_crew_move(
    request,
    *,
    data,
    target_shift,
    section_id,
    week_start,
    week_end,
    assignment_date,
    day_type,
    staff_ids,
):
    """
    Executes flexible inter-shift, intra-shift, weekwise, and weekendwise movements
    without restrictive swap requirements or artificial capacity blocks.
    """
    raw_target_dates = data.get("target_dates")
    target_dates = []
    if raw_target_dates:
        for d in str(raw_target_dates).split(","):
            d = d.strip()
            if d:
                try:
                    target_dates.append(date.fromisoformat(d))
                except (ValueError, TypeError):
                    pass
    if not target_dates:
        target_dates = _dates_for_type(week_start, week_end, day_type)
    if not target_dates and assignment_date:
        target_dates = [assignment_date]

    if not target_dates:
        return _json_error("No target dates available in this roster cell.", 400)

    try:
        source_week_start = date.fromisoformat(data["source_week_start"]) if data.get("source_week_start") else week_start
        source_week_end = date.fromisoformat(data["source_week_end"]) if data.get("source_week_end") else week_end
    except (ValueError, TypeError):
        source_week_start, source_week_end = week_start, week_end

    source_shift_id = None
    if data.get("source_shift_id"):
        try:
            source_shift_id = int(data["source_shift_id"])
        except (ValueError, TypeError):
            source_shift_id = None

    source_day_type = data.get("source_day_type")
    source_date = None
    if data.get("source_date"):
        try:
            source_date = date.fromisoformat(data["source_date"])
        except (ValueError, TypeError):
            source_date = None

    staff_members = list(CustomUser.objects.filter(
        pk__in=staff_ids,
        is_active=True,
    ).select_related("section"))
    if not staff_members:
        return _json_error("No active staff found for this move.", 404)

    for staff in staff_members:
        staff_sec_id = staff.section_id or section_id
        if staff_sec_id:
            ShiftSectionRequirement.objects.get_or_create(
                shift=target_shift,
                section_id=staff_sec_id,
                defaults={
                    "minimum_required": 1,
                    "preferred_staff": 2,
                    "maximum_allowed": 20,
                }
            )

    target_roster, _ = MonthlyDutyRoster.objects.get_or_create(
        treatment_plant_id=target_shift.treatment_plant_id,
        month=week_start.month,
        year=week_start.year,
    )
    first_sec = staff_members[0].section or Section.objects.filter(pk=section_id).first()
    target_team, _ = WeeklyShiftTeam.get_for_date(
        duty_roster=target_roster,
        shift=target_shift,
        section=first_sec,
        target_date=week_start,
        create=True,
    )

    source_qs = ShiftAssignment.objects.select_for_update().filter(
        staff_id__in=staff_ids,
        is_deleted=False,
    )
    if source_week_start and source_week_end:
        source_qs = source_qs.filter(date__range=(source_week_start, source_week_end))
    if source_shift_id:
        source_qs = source_qs.filter(shift_id=source_shift_id)

    if source_day_type:
        cat_filtered = source_qs.filter(day_type=source_day_type)
        if cat_filtered.exists():
            source_qs = cat_filtered
    elif source_date:
        d_type = ShiftAssignment.DAYTYPE_WEEKEND if source_date.weekday() >= 5 else ShiftAssignment.DAYTYPE_WEEKDAY
        cat_filtered = source_qs.filter(day_type=d_type)
        if cat_filtered.exists():
            source_qs = cat_filtered

    source_rows = list(source_qs.select_related("staff", "shift", "duty_roster"))

    source_dates = {r.date for r in source_rows}
    if (
        source_shift_id == target_shift.pk
        and source_week_start == week_start
        and source_dates
        and source_dates == set(target_dates)
    ):
        return JsonResponse({
            "ok": True,
            "message": "Staff member is already assigned to this shift category.",
        })

    source_teams = set()
    for row in source_rows:
        if row.date and row.duty_roster and row.staff and row.staff.section:
            w_start, w_end = MonthlyDutyRoster.get_week_bounds(row.date)
            old_team = WeeklyShiftTeam.objects.filter(
                duty_roster=row.duty_roster,
                shift=row.shift,
                section=row.staff.section,
                week_start=w_start,
                week_end=w_end,
            ).first()
            if old_team:
                source_teams.add(old_team)

    if source_shift_id and source_shift_id != target_shift.pk:
        for person_id in staff_ids:
            ShiftSupervisor.objects.filter(
                user_id=person_id,
                shift_id=source_shift_id,
                week_start=source_week_start,
                week_start__isnull=False,
            ).delete()

    for row in source_rows:
        row._weekly_team_change_allowed = True
        try:
            row.delete()
        except Exception:
            pass

    for staff in staff_members:
        for t_date in target_dates:
            conflicts = ShiftAssignment.objects.filter(
                staff_id=staff.pk,
                date=t_date,
                is_deleted=False,
            )
            for conf in conflicts:
                conf._weekly_team_change_allowed = True
                try:
                    conf.delete()
                except Exception:
                    pass

    note = f"Flexible shift move by {request.user.get_username()} at {timezone.localtime():%Y-%m-%d %H:%M}."
    created_assignments = []
    for staff in staff_members:
        for target_date in target_dates:
            effective_day_type = (
                ShiftAssignment.DAYTYPE_WEEKEND
                if target_date.weekday() >= 5
                else day_type or ShiftAssignment.determine_day_type(target_date)
            )
            active_sup = ShiftSupervisor.get_active_supervisor(
                target_shift.treatment_plant_id,
                staff.section_id,
                shift_id=target_shift.pk,
                assignment_date=target_date,
            )
            assignment = ShiftAssignment(
                staff=staff,
                shift=target_shift,
                date=target_date,
                day_type=effective_day_type,
                duty_roster=target_roster,
                supervisor=active_sup,
                attendance_status=ShiftAssignment.ATTENDANCE_PRESENT,
                remarks=note,
            )
            assignment._weekly_team_change_allowed = True
            assignment._allow_capacity_override = True
            assignment.save()
            created_assignments.append(assignment)

    if target_team:
        try:
            current_members = set(target_team.members.values_list("pk", flat=True))
            target_team.replace_staff_ids(current_members | set(staff_ids), reconciled=False, enforce_maximum=False)
            target_team.refresh_reconciliation()
        except Exception:
            pass

    for steam in source_teams:
        if steam.pk != (target_team.pk if target_team else None):
            try:
                remaining_staff_ids = set(ShiftAssignment.objects.filter(
                    duty_roster=steam.duty_roster,
                    shift=steam.shift,
                    section=steam.section,
                    date__range=(steam.week_start, steam.week_end),
                    is_deleted=False,
                ).values_list("staff_id", flat=True))
                steam.replace_staff_ids(remaining_staff_ids, reconciled=False, enforce_maximum=False)
                steam.refresh_reconciliation()
            except Exception:
                pass

    return JsonResponse({
        "ok": True,
        "message": f"{len(staff_members)} staff moved to {target_shift.name} ({day_type}).",
        "assignment_id": created_assignments[0].pk if created_assignments else None,
        "staff": ", ".join(s.get_full_name() or s.get_username() for s in staff_members),
        "date": target_dates[0].isoformat() if target_dates else assignment_date.isoformat(),
        "shift": target_shift.name,
    })


@login_required
@require_POST
def roster_assignment_action(request):
    data = _json_body(request)
    if data is None:
        return _json_error("The request body is not valid JSON.")
    action = data.get("action")
    if action == "supervisor":
        return _assign_weekly_supervisor(request, data)
    if action == "temporary_supervisor":
        return _assign_temporary_supervisor(request, data)
    if action == "balance_supervisors":
        return _balance_supervisors_action(request, data)
    if action == "leave":
        return _assign_leave(request, data)
    if action == "rebuild_week":
        return _rebuild_weekly_teams_action(request, data)
    if action == "autopopulate_year":
        return _autopopulate_year_action(request, data)
    required_permission = "sc.add_shiftassignment" if action == "add" else "sc.change_shiftassignment"
    if action not in {"add", "move", "move_team"}:
        return _json_error("Choose an add, move, or move-team action.")
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
    if day_type == ShiftAssignment.DAYTYPE_HOLIDAY and assignment_date.weekday() >= 5:
        return _json_error("Saturday and Sunday use the weekend shift requirement.")
    week_start, week_end = _parse_range(data)
    if (
        not week_start
        or not week_end
        or not _is_roster_week(week_start, week_end)
        or assignment_date not in _dates_for_type(week_start, week_end, day_type)
    ):
        return _json_error("The selected date does not match this roster cell.")

    add_result = None
    try:
        with transaction.atomic():
            if action == "add":
                try:
                    section_id = int(data.get("section_id"))
                except (TypeError, ValueError):
                    return _json_error("Choose a section and at least one staff member.")
                raw_staff_ids = data.get("staff_ids")
                if raw_staff_ids is None:
                    raw_staff_ids = [data.get("staff_id")]
                if not isinstance(raw_staff_ids, list) or not raw_staff_ids:
                    return _json_error("Choose at least one staff member.")
                try:
                    staff_ids = [int(value) for value in raw_staff_ids]
                except (TypeError, ValueError):
                    return _json_error("Choose valid staff members.")
                if any(staff_id <= 0 for staff_id in staff_ids) or len(set(staff_ids)) != len(staff_ids):
                    return _json_error("Choose each staff member once.")

                shift = Shift.objects.select_for_update().filter(is_deleted=False, pk=shift_id).first()
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
                if len(staff_ids) > requirement.maximum_allowed:
                    return _json_error(
                        f"Select no more than {requirement.maximum_allowed} staff for this shift section.",
                        409,
                    )
                staff_by_id = CustomUser.objects.select_for_update().filter(
                    pk__in=staff_ids,
                    is_active=True,
                    assigned_treatment_plant_id=shift.treatment_plant_id,
                    section_id=section_id,
                ).in_bulk()
                if len(staff_by_id) != len(staff_ids):
                    return _json_error("One or more selected staff no longer belong to this active plant section.", 409)
                staff_members = [staff_by_id[staff_id] for staff_id in staff_ids]

                # A roster cell represents one stable crew for its day type.
                holiday_dates = set(ShiftAssignment.objects.filter(
                    shift__treatment_plant_id=shift.treatment_plant_id,
                    date__range=(week_start, week_end),
                    day_type=ShiftAssignment.DAYTYPE_HOLIDAY,
                    is_deleted=False,
                ).values_list("date", flat=True))
                if day_type == ShiftAssignment.DAYTYPE_HOLIDAY and assignment_date not in holiday_dates:
                    return _json_error("Choose a configured public holiday date in this week.", 409)
                assignment_dates = _dates_for_type(week_start, week_end, day_type)
                if day_type == ShiftAssignment.DAYTYPE_WEEKDAY:
                    assignment_dates = [day for day in assignment_dates if day not in holiday_dates]
                elif day_type == ShiftAssignment.DAYTYPE_HOLIDAY:
                    assignment_dates = [day for day in assignment_dates if day in holiday_dates]
                if not assignment_dates:
                    return _json_error("There are no dates in this roster week for that staffing category.", 409)
                duty_roster, _created = MonthlyDutyRoster.objects.get_or_create(
                    treatment_plant_id=shift.treatment_plant_id,
                    month=week_start.month,
                    year=week_start.year,
                )
                daily_crews = []
                for target_date in assignment_dates:
                    daily_rows = list(ShiftAssignment.objects.select_for_update().filter(
                        shift_id=shift.pk,
                        date=target_date,
                        staff__section_id=section_id,
                        is_deleted=False,
                    ).values_list("pk", "staff_id"))
                    daily_crews.append({staff_id for _row_id, staff_id in daily_rows})
                if len({frozenset(crew) for crew in daily_crews}) > 1:
                    raise ValidationError(
                        "This staffing category already has different crews by date. Rebuild the weekly team before adding staff."
                    )
                weekly_team, _created = WeeklyShiftTeam.get_for_date(
                    duty_roster=duty_roster,
                    shift=shift,
                    section=requirement.section,
                    target_date=week_start,
                    create=True,
                )
                saved_team_ids = set(weekly_team.members.values_list("pk", flat=True))
                all_week_crew_ids = set(ShiftAssignment.objects.filter(
                    shift_id=shift.pk,
                    date__range=(week_start, week_end),
                    staff__section_id=section_id,
                    is_deleted=False,
                ).values_list("staff_id", flat=True))
                if saved_team_ids and saved_team_ids != all_week_crew_ids:
                    raise ValidationError(
                        "The saved weekly team does not match the assigned category crews. Rebuild it before adding staff."
                    )
                if all_week_crew_ids and not saved_team_ids:
                    weekly_team.replace_staff_ids(all_week_crew_ids, reconciled=False)
                target_team_ids = all_week_crew_ids | set(staff_ids)
                if len(target_team_ids) > requirement.maximum_allowed:
                    raise ValidationError(
                        f"The weekly team would exceed the maximum of {requirement.maximum_allowed} staff."
                    )
                category_limit = (
                    requirement.get_required_staff(day_type)
                    if day_type in {
                        ShiftAssignment.DAYTYPE_WEEKEND,
                        ShiftAssignment.DAYTYPE_HOLIDAY,
                    }
                    else requirement.maximum_allowed
                )
                assignment_plan = []
                for target_date in assignment_dates:
                    existing_rows = ShiftAssignment.objects.select_for_update().filter(
                        shift_id=shift.pk,
                        date=target_date,
                        staff__section_id=section_id,
                        is_deleted=False,
                    ).values_list("staff_id", flat=True)
                    existing_staff_ids = set(existing_rows)
                    staff_to_add = [person for person in staff_members if person.pk not in existing_staff_ids]
                    current_count = len(existing_staff_ids)
                    if current_count + len(staff_to_add) > category_limit:
                        raise ValidationError(
                            f"Adding the selected team would exceed the {day_type} staffing limit of {category_limit} "
                            f"staff for {target_date:%A, %d %b} ({current_count} assigned, {len(staff_to_add)} selected)."
                        )
                    target_day_type = (
                        ShiftAssignment.DAYTYPE_WEEKEND
                        if target_date.weekday() >= 5
                        else ShiftAssignment.DAYTYPE_HOLIDAY
                        if target_date in holiday_dates
                        else ShiftAssignment.DAYTYPE_WEEKDAY
                    )
                    for staff in staff_to_add:
                        assignment_plan.append((staff, target_date, target_day_type))

                if not assignment_plan:
                    return _json_error("The selected staff are already assigned for every date in this staffing category.", 409)

                # Validate overlap between dates in this request as well as
                # conflicts with assignments already stored in the roster.
                planned_dates_by_staff = defaultdict(list)
                for staff, target_date, _target_day_type in assignment_plan:
                    planned_dates_by_staff[staff.pk].append(target_date)
                staff_by_id_for_plan = {staff.pk: staff for staff in staff_members}
                for staff_id, planned_dates in planned_dates_by_staff.items():
                    for target_date in planned_dates:
                        for other_date in planned_dates:
                            if target_date == other_date or abs((target_date - other_date).days) > 1:
                                continue
                            if shift.overlaps_with(target_date, shift, other_date):
                                staff = staff_by_id_for_plan[staff_id]
                                raise ValidationError(
                                    f"{staff.get_full_name() or staff.get_username()} has overlapping planned "
                                    f"shift times on {target_date:%A, %d %b} and {other_date:%A, %d %b}."
                                )

                weekly_team.replace_staff_ids(target_team_ids, reconciled=False)

                created_assignments = ShiftAssignment.objects.bulk_create_weekly_crew([
                    ShiftAssignment(
                        staff=staff,
                        shift=shift,
                        date=target_date,
                        day_type=target_day_type,
                        duty_roster=duty_roster,
                        remarks=f"Added to the weekly duty team by {request.user.get_username()}.",
                    )
                    for staff, target_date, target_day_type in assignment_plan
                ])
                # Resolve designated supervisors after the new assignments
                # exist, so a supervisor selected into this same bulk crew is
                # recognized just as it would be by sequential creation.
                supervisors_by_date = {}
                for assignment in created_assignments:
                    if assignment.date not in supervisors_by_date:
                        supervisor = ShiftSupervisor.get_active_supervisor(
                            shift.treatment_plant_id,
                            section_id,
                            shift_id=shift.pk,
                            assignment_date=assignment.date,
                        )
                        has_other_weekly_shift = supervisor and ShiftSupervisor.objects.filter(
                            user_id=supervisor.pk,
                            week_start__isnull=False,
                            week_start__lte=assignment.date,
                            week_end__gte=assignment.date,
                        ).exclude(shift_id=shift.pk).exists()
                        if (
                            supervisor
                            and (
                                AnnualLeave.is_unavailable(supervisor.pk, assignment.date)
                                or has_other_weekly_shift
                            )
                        ):
                            supervisor = None
                        supervisors_by_date[assignment.date] = supervisor
                    assignment.supervisor = supervisors_by_date[assignment.date]
                ShiftAssignment.objects.bulk_update(created_assignments, ["supervisor"])
                assignment_content_type = ContentType.objects.get_for_model(ShiftAssignment)
                AuditLog.objects.bulk_create([
                    AuditLog(
                        user=request.user,
                        action="create",
                        content_type=assignment_content_type,
                        object_id=str(assignment.pk),
                        object_repr=str(assignment)[:512],
                        changes={
                            "before": None,
                            "after": {
                                "staff": assignment.staff_id,
                                "shift": assignment.shift_id,
                                "date": assignment.date.isoformat(),
                                "day_type": assignment.day_type,
                                "supervisor": assignment.supervisor_id,
                            },
                        },
                    )
                    for assignment in created_assignments
                ])
                weekly_team.refresh_reconciliation()
                eligible_supervisor_ids = set(ShiftSupervisor.objects.filter(
                    treatment_plant_id=shift.treatment_plant_id,
                    section_id=section_id,
                    is_active=True,
                    user__is_active=True,
                ).values_list("user_id", flat=True))
                add_result = {
                    "ok": True,
                    "message": f"Weekly shift team updated for {len(staff_ids)} selected staff.",
                    "assignment_id": created_assignments[0].pk,
                    "assignments": [
                        {
                            "id": assignment.pk,
                            "staff_id": assignment.staff_id,
                            "shift_id": shift.pk,
                            "section_id": section_id,
                            "plant_id": shift.treatment_plant_id,
                            "week_start": week_start.isoformat(),
                            "week_end": week_end.isoformat(),
                            "staff_name": (
                                assignment.staff.get_full_name()
                                or assignment.staff.get_username()
                            ),
                            "date": assignment.date.isoformat(),
                            "day_type": assignment.day_type,
                            "supervisor_eligible": assignment.staff_id in eligible_supervisor_ids,
                        }
                        for assignment in created_assignments
                    ],
                }
            elif action == "move_team":
                requested_plant = str(data.get("plant_id") or "").strip()
                try:
                    section_id = int(data.get("section_id"))
                except (TypeError, ValueError):
                    return _json_error("Choose a valid section for the selected crew.")
                raw_staff_ids = data.get("staff_ids")
                if not isinstance(raw_staff_ids, list) or not raw_staff_ids:
                    return _json_error("Select at least one staff member to move.")
                try:
                    staff_ids = [int(value) for value in raw_staff_ids]
                except (TypeError, ValueError):
                    return _json_error("Choose valid staff members for the weekly move.")
                if any(staff_id <= 0 for staff_id in staff_ids) or len(set(staff_ids)) != len(staff_ids):
                    return _json_error("Choose each staff member once for the weekly move.")
                if not requested_plant.isdecimal():
                    return _json_error("Choose a treatment plant before moving staff.")

                target_shift = Shift.objects.select_for_update().filter(
                    pk=shift_id,
                    treatment_plant_id=int(requested_plant),
                    is_deleted=False,
                ).first()
                if not target_shift:
                    return _json_error("The destination shift is unavailable at this plant.", 404)
                if not _section_for_actor(request, target_shift.treatment_plant_id, section_id):
                    return _json_error("The selected section is outside your assigned scope.", 403)

                return _flexible_roster_crew_move(
                    request,
                    data=data,
                    target_shift=target_shift,
                    section_id=section_id,
                    week_start=week_start,
                    week_end=week_end,
                    assignment_date=assignment_date,
                    day_type=day_type,
                    staff_ids=staff_ids,
                )
            else:
                assignment_id = int(data.get("assignment_id"))
                assignment = ShiftAssignment.objects.select_for_update().filter(
                    pk=assignment_id,
                    is_deleted=False,
                ).first()
                if not assignment:
                    return _json_error("The roster entry was not found in your scope.", 404)
                target_shift = Shift.objects.filter(
                    is_deleted=False,
                    treatment_plant_id=assignment.shift.treatment_plant_id,
                    pk=shift_id,
                ).first()
                if not target_shift:
                    return _json_error("The destination shift is unavailable in this treatment plant.", 404)
                return _flexible_roster_crew_move(
                    request,
                    data=data,
                    target_shift=target_shift,
                    section_id=assignment.staff.section_id,
                    week_start=week_start,
                    week_end=week_end,
                    assignment_date=assignment_date,
                    day_type=day_type,
                    staff_ids=[assignment.staff_id],
                )
        if action == "add" and add_result is not None:
            return JsonResponse(add_result)
    except (IntegrityError, ValidationError) as error:
        if isinstance(error, ValidationError):
            detail = "; ".join(error.messages)
        else:
            detail = "A duplicate assignment or concurrent roster update was detected."
        return _json_error(detail, 409)
