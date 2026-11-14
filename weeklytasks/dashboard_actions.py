import json
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from core.plant_section_scope import section_belongs_to_plant, user_plant_section
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
    if not section_belongs_to_plant(task.section, task.treatment_plant_id) or not task.section.is_active:
        return _json_error("This task has an inactive or mismatched section.", 409)

    first_day = task.start_date
    slots = []
    current = first_day
    while current <= task.end_date:
        already_assigned_ids = set(DailyTaskAssignment.objects.filter(
            task_id=task.pk,
            date=current,
        ).values_list("user_id", flat=True))

        candidates = task.get_eligible_candidates(current)
        day_users = [
            {
                "id": candidate["user"].pk,
                "name": candidate["user"].get_full_name() or candidate["user"].get_username(),
                "shifts": [{"id": shift.pk, "name": shift.name} for shift in candidate["shifts"]],
            }
            for candidate in candidates
            if candidate["user"].pk not in already_assigned_ids
        ]
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
    if not target_task.start_date <= assignment_date <= target_task.end_date:
        return _json_error("The selected date is outside this weekly task window.")
    if (
        not section_belongs_to_plant(target_task.section, target_task.treatment_plant_id)
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

                shift = None
                shift_id = data.get("shift_id") or None
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
                    if not shift:
                        return _json_error("The selected shift is unavailable in this plant and section.", 409)

                # Gate creation directly through DailyTaskAssignment.create_assignment
                assignment = DailyTaskAssignment.create_assignment(
                    task=target_task,
                    user=staff,
                    date=assignment_date,
                    shift=shift,
                    remarks=f"Assigned from the weekly schedule by {request.user.get_username()}.",
                    created_by=request.user,
                )
            else:
                try:
                    assignment_id = int(data.get("assignment_id", ""))
                except (TypeError, ValueError):
                    return _json_error("Choose a valid assignment to move.")
                assignment = _assignment_queryset(request.user).select_for_update().filter(pk=assignment_id).first()
                if not assignment:
                    return _json_error("The assignment was not found in your treatment plant and section.", 404)

                shift = None
                requested_shift_id = data.get("shift_id") or None
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
                    if not shift:
                        return _json_error("The selected shift is unavailable in this plant and section.", 409)

                # Gate move directly through assignment.move_assignment
                note = f"Moved on the weekly schedule by {request.user.get_username()} at {timezone.localtime():%Y-%m-%d %H:%M}."
                assignment.move_assignment(
                    target_task=target_task,
                    target_date=assignment_date,
                    shift=shift,
                    moved_by=request.user,
                    remarks=note,
                )
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
