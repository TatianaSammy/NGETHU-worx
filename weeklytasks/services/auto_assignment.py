import logging
from collections import Counter, defaultdict
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from sc.models import AnnualLeave, ShiftAssignment

from weeklytasks.models import DailyTaskAssignment, WeeklyDutyTask


logger = logging.getLogger(__name__)
User = get_user_model()
AUTO_ASSIGNMENT_REMARK = "Automatically assigned by the weekly duty scheduler."


def assign_task(task_id, from_date=None):
    from_date = from_date or timezone.localdate()
    try:
        with transaction.atomic():
            task = (
                WeeklyDutyTask.objects.select_for_update()
                .select_related("week", "week__schedule", "section")
                .filter(pk=task_id)
                .first()
            )
            if not task:
                return 0

            plant_id = task.treatment_plant_id
            section_id = task.section_id
            if (
                not plant_id
                or not section_id
                or task.section.treatment_plant_id != plant_id
                or not task.section.is_active
                or task.week.schedule.treatment_plant_id != plant_id
                or task.week.schedule.section_id != section_id
            ):
                logger.warning("Skipping weekly task %s with inconsistent plant or section scope", task.pk)
                return 0

            first_day = max(task.start_date, from_date)
            last_day = task.end_date
            if first_day > last_day:
                return 0

            valid_existing = DailyTaskAssignment.objects.filter(
                task_id=task.pk,
                date__range=(task.start_date, task.end_date),
                user__is_active=True,
                user__assigned_treatment_plant_id=plant_id,
                user__section_id=section_id,
            ).exists()
            if valid_existing:
                return 0

            staff = list(
                User.objects.select_for_update()
                .filter(
                    is_active=True,
                    assigned_treatment_plant_id=plant_id,
                    section_id=section_id,
                )
                .order_by("pk")
            )
            if not staff:
                return 0

            staff_ids = [person.pk for person in staff]
            unavailable = defaultdict(set)
            leave_periods = AnnualLeave.objects.filter(
                staff_id__in=staff_ids,
                treatment_plant_id=plant_id,
                status__in=AnnualLeave.UNAVAILABLE_STATUSES,
                start_date__lte=last_day,
                end_date__gte=first_day,
            ).values_list("staff_id", "start_date", "end_date")
            for staff_id, leave_start, leave_end in leave_periods:
                current = max(leave_start, first_day)
                leave_end = min(leave_end, last_day)
                while current <= leave_end:
                    unavailable[staff_id].add(current)
                    current += timedelta(days=1)

            task_load = Counter(
                DailyTaskAssignment.objects.filter(
                    user_id__in=staff_ids,
                    task__treatment_plant_id=plant_id,
                    task__section_id=section_id,
                    date__range=(first_day, last_day),
                ).values_list("user_id", flat=True)
            )
            shift_load = Counter(
                ShiftAssignment.objects.filter(
                    staff_id__in=staff_ids,
                    staff__assigned_treatment_plant_id=plant_id,
                    staff__section_id=section_id,
                    date__range=(first_day, last_day),
                    is_deleted=False,
                    shift__treatment_plant_id=plant_id,
                    shift__section_requirements__section_id=section_id,
                ).values_list("staff_id", flat=True)
            )
            shifts_by_person_day = defaultdict(list)
            shift_assignments = (
                ShiftAssignment.objects.filter(
                    staff_id__in=staff_ids,
                    staff__assigned_treatment_plant_id=plant_id,
                    staff__section_id=section_id,
                    date__range=(first_day, last_day),
                    is_deleted=False,
                    shift__treatment_plant_id=plant_id,
                    shift__section_requirements__section_id=section_id,
                    shift__is_deleted=False,
                )
                .select_related("shift")
                .order_by("shift__start_time", "pk")
            )
            for assignment in shift_assignments:
                shifts_by_person_day[(assignment.staff_id, assignment.date)].append(assignment.shift)

            candidates = []
            for person in staff:
                for day_offset in range((last_day - first_day).days + 1):
                    assignment_date = first_day + timedelta(days=day_offset)
                    if assignment_date in unavailable.get(person.pk, set()):
                        continue
                    assigned_shifts = shifts_by_person_day.get((person.pk, assignment_date), [])
                    shift = assigned_shifts[0] if assigned_shifts else None
                    candidates.append((
                        0 if shift else 1,
                        task_load[person.pk] * 3 + shift_load[person.pk],
                        task_load[person.pk],
                        assignment_date.weekday() >= 5,
                        day_offset,
                        person.pk,
                        person,
                        assignment_date,
                        shift,
                    ))

            if not candidates:
                return 0

            candidates.sort(key=lambda item: item[:6])
            _, _, _, _, _, _, person, assignment_date, shift = candidates[0]
            assignment = DailyTaskAssignment(
                task=task,
                user=person,
                date=assignment_date,
                shift=shift,
                remarks=AUTO_ASSIGNMENT_REMARK,
            )
            assignment.save()
            return 1
    except IntegrityError:
        return 0
    except ValidationError:
        logger.exception("Automatic assignment validation failed for weekly task %s", task_id)
        return 0
    except Exception:
        logger.exception("Automatic assignment failed for weekly task %s", task_id)
        return 0


def assign_for_scope(plant_id, section_id, from_date=None):
    if not plant_id or not section_id:
        return 0
    from_date = from_date or timezone.localdate()
    task_ids = WeeklyDutyTask.objects.filter(
        treatment_plant_id=plant_id,
        section_id=section_id,
        week__end_date__gte=from_date,
        week__schedule__treatment_plant_id=plant_id,
        week__schedule__section_id=section_id,
        section__treatment_plant_id=plant_id,
        section__is_active=True,
    ).order_by("week__start_date", "pk").values_list("pk", flat=True)
    return sum(assign_task(task_id, from_date=from_date) for task_id in task_ids)


def assign_for_user(user_id):
    user = User.objects.filter(pk=user_id, is_active=True).select_related("section").first()
    if not user or not user.assigned_treatment_plant_id or not user.section_id:
        return 0
    if (
        user.section.treatment_plant_id != user.assigned_treatment_plant_id
        or not user.section.is_active
    ):
        logger.info(
            "Skipping weekly task allocation for user %s because the plant and section do not match",
            user.pk,
        )
        return 0
    return assign_for_scope(
        user.assigned_treatment_plant_id,
        user.section_id,
        from_date=timezone.localdate(),
    )
