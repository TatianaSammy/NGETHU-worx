import logging

from django.db import transaction
from django.db.models import F

from core.models import Section
from sc.models import MonthlyDutyRoster
from weeklytasks.models import DailyTaskAssignment, DutyWeek, MonthlyDutySchedule, WeeklyDutyTask


logger = logging.getLogger(__name__)


def _target_week(source_week, expected_weeks):
    def overlap(bounds):
        start = max(source_week.start_date, bounds[0])
        end = min(source_week.end_date, bounds[1])
        return max(0, (end - start).days + 1)

    return max(
        expected_weeks,
        key=lambda bounds: (overlap(bounds), -abs((source_week.start_date - bounds[0]).days)),
    )


def _rehome_legacy_tasks(source_week, target_week):
    tasks = list(
        WeeklyDutyTask.objects.filter(week_id=source_week.pk)
        .select_related("task_template")
        .prefetch_related("daily_assignments")
        .order_by("pk")
    )
    for task in tasks:
        assignments = list(task.daily_assignments.all())
        if any(not target_week.start_date <= assignment.date <= target_week.end_date for assignment in assignments):
            continue
        target_task = WeeklyDutyTask.objects.filter(
            week_id=target_week.pk,
            task_template_id=task.task_template_id,
            treatment_plant_id=task.treatment_plant_id,
            section_id=task.section_id,
        ).first()
        if not target_task:
            task.week_id = target_week.pk
            task.save(update_fields=["week", "updated_at"])
            continue
        existing_assignments = set(target_task.daily_assignments.values_list("user_id", "date"))
        assignment_keys = {(assignment.user_id, assignment.date) for assignment in assignments}
        if existing_assignments.intersection(assignment_keys):
            continue
        if assignments:
            DailyTaskAssignment.objects.filter(task_id=task.pk).update(task_id=target_task.pk)
        task.delete()


def ensure_duty_weeks(schedule_id):
    with transaction.atomic():
        schedule = MonthlyDutySchedule.objects.select_for_update().filter(pk=schedule_id).first()
        if not schedule:
            return 0
        expected = MonthlyDutyRoster.get_weeks_in_month(schedule.year, schedule.month)
        expected_set = set(expected)
        existing = list(DutyWeek.objects.filter(schedule_id=schedule.pk).order_by("start_date", "end_date", "pk"))
        existing_ranges = {(week.start_date, week.end_date) for week in existing}
        DutyWeek.objects.bulk_create(
            [
                DutyWeek(schedule=schedule, start_date=start_date, end_date=end_date)
                for start_date, end_date in expected
                if (start_date, end_date) not in existing_ranges
            ],
            ignore_conflicts=True,
        )
        canonical_weeks = {
            (week.start_date, week.end_date): week
            for week in DutyWeek.objects.filter(schedule_id=schedule.pk, start_date__gte=schedule.month_start, start_date__lte=schedule.month_end)
        }
        for legacy_week in existing:
            legacy_range = (legacy_week.start_date, legacy_week.end_date)
            if legacy_range in expected_set:
                continue
            if legacy_week.weekly_tasks.exists():
                target_range = _target_week(legacy_week, expected)
                target_week = canonical_weeks.get(target_range)
                if target_week:
                    _rehome_legacy_tasks(legacy_week, target_week)
            if not legacy_week.weekly_tasks.exists():
                legacy_week.delete()
            else:
                logger.warning(
                    "Retaining legacy duty week %s for schedule %s because its task assignments cannot be moved safely",
                    legacy_week.pk,
                    schedule.pk,
                )
        return len(expected)


def sync_shift_control_roster(roster_id, section_id=None):
    roster = MonthlyDutyRoster.objects.filter(pk=roster_id).select_related("treatment_plant").first()
    if not roster:
        return 0
    return len(_sync_plant_month(roster.month, roster.year, roster.treatment_plant_id, section_id))


def _sync_plant_month(month, year, plant_id=None, section_id=None):
    sections = Section.objects.filter(
        is_active=True,
        shift_requirements__shift__is_deleted=False,
        shift_requirements__shift__treatment_plant_id=F("treatment_plant_id"),
    )
    if plant_id:
        sections = sections.filter(treatment_plant_id=plant_id)
    if section_id:
        sections = sections.filter(pk=section_id)
    synced_schedule_ids = []
    for section in sections.distinct().order_by("treatment_plant_id", "pk"):
        schedule, created = MonthlyDutySchedule.objects.get_or_create(
            month=month,
            year=year,
            treatment_plant_id=section.treatment_plant_id,
            section_id=section.pk,
        )
        if not created:
            ensure_duty_weeks(schedule.pk)
        synced_schedule_ids.append(schedule.pk)
    return synced_schedule_ids


def sync_period_from_shift_control(month, year, plant_id=None, section_id=None):
    synced_schedule_ids = _sync_plant_month(month, year, plant_id=plant_id, section_id=section_id)
    schedules = MonthlyDutySchedule.objects.filter(month=month, year=year)
    if plant_id:
        schedules = schedules.filter(treatment_plant_id=plant_id)
    if section_id:
        schedules = schedules.filter(section_id=section_id)
    if synced_schedule_ids:
        schedules = schedules.exclude(pk__in=synced_schedule_ids)
    for schedule_id in schedules.values_list("pk", flat=True):
        ensure_duty_weeks(schedule_id)
