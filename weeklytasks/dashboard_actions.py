import json
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from core.plant_section_scope import user_plant_section
from sc.models import AnnualLeave, Shift, ShiftAssignment
from accounts.models import CustomUser
from weeklytasks.models import DailyTaskAssignment, WeeklyDutyTask


def _allowed(user, permission):
    return user.is_superuser or user.has_perm(permission)


def _json_body(request):
    try:
        return json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _json_error(message, status=400):
    return JsonResponse({"ok": False, "message": message}, status=status)


def _task_queryset(user):
    queryset = WeeklyDutyTask.objects.select_related(
        "week", "week__schedule", "task_template", "section", "treatment_plant"
    )
    if user.is_superuser:
        return queryset
    plant_id, section_id = user_plant_section(user)
    if not plant_id or not section_id:
        return queryset.none()
    return queryset.filter(
        treatment_plant_id=plant_id,
        section_id=section_id,
        week__schedule__treatment_plant_id=plant_id,
        week__schedule__section_id=section_id,
    )


def _assignment_queryset(user):
    queryset = DailyTaskAssignment.objects.select_related(
        "task", "task__week", "task__week__schedule", "task__section", "user", "user__section", "shift"
    )
    if user.is_superuser:
        return queryset
    plant_id, section_id = user_plant_section(user)
    if not plant_id or not section_id:
        return queryset.none()
    return queryset.filter(
        task__treatment_plant_id=plant_id,
        task__section_id=section_id,
        task__week__schedule__treatment_plant_id=plant_id,
        task__week__schedule__section_id=section_id,
        user__assigned_treatment_plant_id=plant_id,
        user__section_id=section_id,
    )


def _valid_shift_assignment(user_id, shift_id, assignment_date, plant_id, section_id):
    return ShiftAssignment.objects.filter(
        staff_id=user_id,
        shift_id=shift_id,
        date=assignment_date,
        is_deleted=False,
        shift__is_deleted=False,
        shift__treatment_plant_id=plant_id,
        shift__section_requirements__section_id=section_id,
        staff__assigned_treatment_plant_id=plant_id,
        staff__section_id=section_id,
    ).exists()


@login_required
@require_GET
def task_assignment_options(request):
    if not _allowed(request.user, "weeklytasks.add_dailytaskassignment"):
        return _json_error("You do not have permission to assign weekly tasks.", 403)
    try:
        task_id = int(request.GET.get("task_id", ""))
    except (TypeError, ValueError):
        return _json_error("Choose a valid weekly task.")
    task = _task_queryset(request.user).filter(pk=task_id).first()
    if not task:
        return _json_error("The task is not available in your treatment plant and section.", 404)
    if task.section.treatment_plant_id != task.treatment_plant_id or not task.section.is_active:
        return _json_error("This task has an inactive or mismatched section.", 409)

    users = list(CustomUser.objects.filter(
        is_active=True,
        assigned_treatment_plant_id=task.treatment_plant_id,
        section_id=task.section_id,
    ).order_by("last_name", "first_name", "username"))
    today = timezone.localdate()
    first_day = max(task.start_date, today)
    user_ids = [user.pk for user in users]
    unavailable_by_date = {}
    if user_ids and first_day <= task.end_date:
        leave_ranges = AnnualLeave.objects.filter(
            staff_id__in=user_ids,
            treatment_plant_id=task.treatment_plant_id,
            status__in=AnnualLeave.UNAVAILABLE_STATUSES,
            start_date__lte=task.end_date,
            end_date__gte=first_day,
        ).values_list("staff_id", "start_date", "end_date")
        for user_id, leave_start, leave_end in leave_ranges:
            current_leave_day = max(first_day, leave_start)
            leave_last_day = min(task.end_date, leave_end)
            while current_leave_day <= leave_last_day:
                unavailable_by_date.setdefault(current_leave_day, set()).add(user_id)
                current_leave_day += timedelta(days=1)
    assigned_by_date = {}
    for user_id, assigned_date in DailyTaskAssignment.objects.filter(
        task_id=task.pk,
        date__range=(first_day, task.end_date),
    ).values_list("user_id", "date"):
        assigned_by_date.setdefault(assigned_date, set()).add(user_id)
    shifts_by_user_date = {}
    if user_ids and first_day <= task.end_date:
        roster_rows = ShiftAssignment.objects.filter(
            staff_id__in=user_ids,
            staff__assigned_treatment_plant_id=task.treatment_plant_id,
            staff__section_id=task.section_id,
            date__range=(first_day, task.end_date),
            is_deleted=False,
            shift__is_deleted=False,
            shift__treatment_plant_id=task.treatment_plant_id,
            shift__section_requirements__section_id=task.section_id,
        ).select_related("shift").order_by("date", "shift__start_time").distinct()
        for row in roster_rows:
            shifts_by_user_date.setdefault((row.staff_id, row.date), []).append(row)
    slots = []
    current = first_day
    while current <= task.end_date:
        unavailable = unavailable_by_date.get(current, set())
        assigned = assigned_by_date.get(current, set())
        day_users = []
        for user in users:
            if user.pk in unavailable or user.pk in assigned:
                continue
            shifts = shifts_by_user_date.get((user.pk, current), [])
            day_users.append({
                "id": user.pk,
                "name": user.get_full_name() or user.get_username(),
                "shifts": [{"id": row.shift_id, "name": row.shift.name} for row in shifts],
            })
        if day_users:
            slots.append({
                "date": current.isoformat(),
                "label": current.strftime("%a, %d %b"),
                "users": day_users,
            })
        current += timedelta(days=1)
    return JsonResponse({"ok": True, "task": task.pk, "slots": slots})


@login_required
@require_POST
def task_assignment_action(request):
    data = _json_body(request)
    if data is None:
        return _json_error("The request body is not valid JSON.")
    action = data.get("action")
    if action not in {"add", "move"}:
        return _json_error("Choose an add or move action.")
    permission = "weeklytasks.add_dailytaskassignment" if action == "add" else "weeklytasks.change_dailytaskassignment"
    if not _allowed(request.user, permission):
        return _json_error("You do not have permission to change these task assignments.", 403)
    try:
        target_task_id = int(data.get("task_id", ""))
        assignment_date = date.fromisoformat(data.get("date", ""))
    except (TypeError, ValueError):
        return _json_error("Choose a valid task and date.")
    target_task = _task_queryset(request.user).filter(pk=target_task_id).first()
    if not target_task:
        return _json_error("The task is not available in your treatment plant and section.", 404)
    today = timezone.localdate()
    if assignment_date < today:
        return _json_error("Past task dates cannot be changed from the live schedule.", 409)
    if not target_task.start_date <= assignment_date <= target_task.end_date:
        return _json_error("The selected date is outside this weekly task window.")
    if (
        target_task.section.treatment_plant_id != target_task.treatment_plant_id
        or not target_task.section.is_active
        or target_task.week.schedule.treatment_plant_id != target_task.treatment_plant_id
        or target_task.week.schedule.section_id != target_task.section_id
    ):
        return _json_error("The task plant and section do not match.", 409)

    try:
        with transaction.atomic():
            if action == "add":
                try:
                    user_id = int(data.get("user_id", ""))
                except (TypeError, ValueError):
                    return _json_error("Choose a staff member.")
                staff = CustomUser.objects.select_for_update().filter(
                    pk=user_id,
                    is_active=True,
                    assigned_treatment_plant_id=target_task.treatment_plant_id,
                    section_id=target_task.section_id,
                ).first()
                if not staff:
                    return _json_error("That staff member no longer belongs to this plant and section.", 409)
                if AnnualLeave.is_unavailable(staff.pk, assignment_date):
                    return _json_error("This staff member is on annual leave or off duty for that date.", 409)
                shift_id = data.get("shift_id") or None
                shift = None
                if shift_id:
                    try:
                        shift_id = int(shift_id)
                    except (TypeError, ValueError):
                        return _json_error("Choose a valid shift.")
                    shift = Shift.objects.filter(
                        pk=shift_id,
                        is_deleted=False,
                        treatment_plant_id=target_task.treatment_plant_id,
                        section_requirements__section_id=target_task.section_id,
                    ).first()
                    if not shift or not _valid_shift_assignment(
                        staff.pk,
                        shift.pk,
                        assignment_date,
                        target_task.treatment_plant_id,
                        target_task.section_id,
                    ):
                        return _json_error("The selected shift is not assigned to this staff member on that date.", 409)
                assignment = DailyTaskAssignment(
                    task=target_task,
                    user=staff,
                    date=assignment_date,
                    shift=shift,
                    remarks=f"Assigned from the weekly schedule by {request.user.get_username()}.",
                )
                assignment.save()
            else:
                try:
                    assignment_id = int(data.get("assignment_id", ""))
                except (TypeError, ValueError):
                    return _json_error("Choose a valid assignment to move.")
                assignment = _assignment_queryset(request.user).select_for_update().filter(pk=assignment_id).first()
                if not assignment:
                    return _json_error("The assignment was not found in your treatment plant and section.", 404)
                staff = assignment.user
                if not staff.is_superuser and (
                    staff.assigned_treatment_plant_id != target_task.treatment_plant_id
                    or staff.section_id != target_task.section_id
                ):
                    return _json_error("A staff member can only be assigned within their treatment plant and section.", 409)
                if AnnualLeave.is_unavailable(staff.pk, assignment_date):
                    return _json_error("This staff member is on annual leave or off duty for that date.", 409)
                requested_shift_id = data.get("shift_id") or None
                shift = None
                if requested_shift_id:
                    try:
                        requested_shift_id = int(requested_shift_id)
                    except (TypeError, ValueError):
                        return _json_error("Choose a valid shift.")
                    shift = Shift.objects.filter(
                        pk=requested_shift_id,
                        is_deleted=False,
                        treatment_plant_id=target_task.treatment_plant_id,
                        section_requirements__section_id=target_task.section_id,
                    ).first()
                    if not shift or not _valid_shift_assignment(
                        staff.pk,
                        shift.pk,
                        assignment_date,
                        target_task.treatment_plant_id,
                        target_task.section_id,
                    ):
                        return _json_error("The selected shift does not match this assignment.", 409)
                assignment.task = target_task
                assignment.date = assignment_date
                if shift:
                    assignment.shift = shift
                else:
                    assignment.shift = Shift.objects.filter(
                        assignments__staff_id=staff.pk,
                        assignments__date=assignment_date,
                        assignments__is_deleted=False,
                        is_deleted=False,
                        treatment_plant_id=target_task.treatment_plant_id,
                        section_requirements__section_id=target_task.section_id,
                    ).order_by("start_time").first()
                note = f"Moved on the weekly schedule by {request.user.get_username()} at {timezone.localtime():%Y-%m-%d %H:%M}."
                assignment.remarks = "\n".join(part for part in (assignment.remarks, note) if part)
                assignment.save()
            return JsonResponse({
                "ok": True,
                "message": "Weekly task assignment updated.",
                "assignment_id": assignment.pk,
                "task_id": assignment.task_id,
                "user_id": assignment.user_id,
                "user": assignment.user.get_full_name() or assignment.user.get_username(),
                "date": assignment.date.isoformat(),
                "shift": assignment.shift.name if assignment.shift else "",
            })
    except (IntegrityError, ValidationError) as error:
        if isinstance(error, ValidationError):
            detail = "; ".join(error.messages)
        else:
            detail = "This user already has an assignment for that task and date."
        return _json_error(detail, 409)
