from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta

from django.db.models import Count, Q
from django.shortcuts import render
from django.utils import timezone
from django.views import View

from core.mixins import WanjalaMixin
from core.models import Section
from core.plant_section_scope import user_plant_section
from t.models import TreatmentPlant
from .models import (
    DailyTaskAssignment,
    DutyWeek,
    MonthlyDutySchedule,
    StandardWeeklyTask,
    WeeklyDutyTask,
)


class DashboardView(WanjalaMixin, View):
    template_name = "weeklytasks/dashboard.html"
    app_label = "weeklytasks"

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()

        today = timezone.localdate()
        month = self._bounded_int(request.GET.get("month"), 1, 12, today.month)
        year = self._bounded_int(request.GET.get("year"), 2000, 2100, today.year)
        period_start = date(year, month, 1)
        period_end = date(year, month, monthrange(year, month)[1])
        previous_period = (period_start - timedelta(days=1)).replace(day=1)
        next_period = (period_end + timedelta(days=1)).replace(day=1)
        is_superuser = bool(request.user.is_superuser)
        user_plant_id, user_section_id = user_plant_section(request.user)

        plant_options = TreatmentPlant.objects.order_by("name") if is_superuser else TreatmentPlant.objects.filter(pk=user_plant_id) if user_plant_id else TreatmentPlant.objects.none()
        selected_plant = None
        if is_superuser:
            requested_plant = request.GET.get("plant")
            if requested_plant and requested_plant.isdigit():
                selected_plant = TreatmentPlant.objects.filter(pk=int(requested_plant)).first()
        else:
            selected_plant = TreatmentPlant.objects.filter(pk=user_plant_id).first() if user_plant_id else None

        if is_superuser:
            from weeklytasks.services.duty_week_sync import sync_period_from_shift_control

            sync_period_from_shift_control(
                month,
                year,
                plant_id=selected_plant.pk if selected_plant else None,
            )
        elif user_plant_id and user_section_id:
            from weeklytasks.services.duty_week_sync import sync_period_from_shift_control

            sync_period_from_shift_control(
                month,
                year,
                plant_id=user_plant_id,
                section_id=user_section_id,
            )

        schedules = MonthlyDutySchedule.objects.select_related("treatment_plant", "section", "created_by")
        weekly_tasks = WeeklyDutyTask.objects.select_related(
            "week", "week__schedule", "week__schedule__treatment_plant", "week__schedule__section",
            "task_template", "section", "treatment_plant",
        )
        assignments = DailyTaskAssignment.objects.select_related(
            "task", "task__week", "task__week__schedule", "task__task_template", "user", "user__section", "shift",
        )
        if selected_plant:
            if is_superuser:
                schedules = schedules.filter(treatment_plant=selected_plant)
                weekly_tasks = weekly_tasks.filter(treatment_plant=selected_plant)
                assignments = assignments.filter(task__treatment_plant=selected_plant)
            elif user_section_id:
                schedules = schedules.filter(treatment_plant=selected_plant, section_id=user_section_id)
                weekly_tasks = weekly_tasks.filter(treatment_plant=selected_plant, section_id=user_section_id)
                assignments = assignments.filter(
                    task__treatment_plant=selected_plant,
                    task__section_id=user_section_id,
                    user__assigned_treatment_plant_id=user_plant_id,
                    user__section_id=user_section_id,
                )
            else:
                schedules = schedules.none()
                weekly_tasks = weekly_tasks.none()
                assignments = assignments.none()
        elif not is_superuser:
            schedules = schedules.none()
            weekly_tasks = weekly_tasks.none()
            assignments = assignments.none()

        month_schedules = schedules.filter(month=month, year=year)
        month_tasks_qs = weekly_tasks.filter(week__schedule__in=month_schedules)
        month_tasks = list(month_tasks_qs.order_by(
            "week__start_date", "treatment_plant__name", "task_template__code", "section__name"
        ))
        month_assignments = assignments.filter(
            date__range=(period_start, period_end),
            task__week__schedule__in=month_schedules,
        ).order_by("date", "shift__start_time", "user__last_name", "user__first_name")

        week_records = list(
            DutyWeek.objects.filter(schedule__in=month_schedules)
            .select_related("schedule", "schedule__treatment_plant", "schedule__section")
            .order_by("start_date", "schedule__treatment_plant__name", "schedule__section__name")
        )
        row_groups = {}
        for week in week_records:
            key = (week.schedule.treatment_plant_id, week.start_date, week.end_date)
            row = row_groups.setdefault(key, {
                "plant": week.schedule.treatment_plant,
                "start_date": week.start_date,
                "end_date": week.end_date,
                "sections": set(),
            })
            row["sections"].add(week.schedule.section.name)

        plan_cells = {}
        task_to_cell = {}
        task_records_by_id = {}
        for task in month_tasks:
            key = (
                task.treatment_plant_id,
                task.week.start_date,
                task.week.end_date,
                task.task_template_id,
            )
            cell = plan_cells.setdefault(key, {
                "task_ids": set(),
                "staffed_task_ids": set(),
                "planned_sections": set(),
                "people": {},
                "task_records": [],
                "assignment_count": 0,
            })
            cell["task_ids"].add(task.pk)
            cell["planned_sections"].add(task.section.name)
            task_record = {
                "id": task.pk,
                "section_id": task.section_id,
                "section_name": task.section.name,
                "start_date": task.start_date,
                "end_date": task.end_date,
                "assignments": [],
            }
            cell["task_records"].append(task_record)
            task_records_by_id[task.pk] = task_record
            task_to_cell[task.pk] = cell

        for assignment in month_assignments:
            cell = task_to_cell.get(assignment.task_id)
            if not cell:
                continue
            cell["staffed_task_ids"].add(assignment.task_id)
            cell["assignment_count"] += 1
            task_records_by_id[assignment.task_id]["assignments"].append({
                "id": assignment.pk,
                "user_id": assignment.user_id,
                "name": assignment.user.get_full_name() or assignment.user.get_username(),
                "date": assignment.date,
                "shift_id": assignment.shift_id,
                "shift_name": assignment.shift.name if assignment.shift else "",
            })
            person = cell["people"].setdefault(
                assignment.user_id,
                {"name": assignment.user.get_full_name() or assignment.user.get_username(), "count": 0},
            )
            person["count"] += 1

        task_columns = list(
            StandardWeeklyTask.objects.filter(weekly_duty_tasks__week__schedule__in=month_schedules)
            .distinct().order_by("code", "name")
        )
        matrix_rows = []
        for key, row in sorted(
            row_groups.items(),
            key=lambda item: (item[1]["start_date"], item[1]["plant"].name.casefold()),
        ):
            plant_id, week_start, week_end = key
            cells = []
            for template in task_columns:
                summary = plan_cells.get((plant_id, week_start, week_end, template.pk))
                if summary is None:
                    cells.append({"template": template, "status": "not-planned", "people": [], "plan_count": 0, "staffed_count": 0})
                    continue
                planned_count = len(summary["task_ids"])
                staffed_count = len(summary["staffed_task_ids"])
                if staffed_count == planned_count:
                    status = "staffed"
                elif staffed_count:
                    status = "partial"
                else:
                    status = "unassigned"
                cells.append({
                    "template": template,
                    "status": status,
                    "people": sorted(summary["people"].values(), key=lambda item: item["name"].casefold()),
                    "assignment_count": summary["assignment_count"],
                    "plan_count": planned_count,
                    "staffed_count": staffed_count,
                    "planned_sections": sorted(summary["planned_sections"], key=str.casefold),
                    "task_records": sorted(summary["task_records"], key=lambda item: (item["section_name"].casefold(), item["id"])),
                })
            row["sections"] = sorted(row["sections"], key=str.casefold)
            row["planned_task_count"] = sum(cell["plan_count"] for cell in cells)
            row["staffed_task_count"] = sum(cell["staffed_count"] for cell in cells)
            row["assignment_count"] = sum(cell.get("assignment_count", 0) for cell in cells)
            row["readiness"] = round(row["staffed_task_count"] * 100 / row["planned_task_count"]) if row["planned_task_count"] else 0
            row["cells"] = cells
            matrix_rows.append(row)

        task_ids_with_assignments = {
            assignment.task_id for assignment in month_assignments
        }
        total_task_plans = len(month_tasks)
        staffed_task_plans = len(task_ids_with_assignments)
        unstaffed_count = max(0, total_task_plans - staffed_task_plans)

        unstaffed_qs = month_tasks_qs.annotate(
            assignment_count=Count(
                "daily_assignments",
                filter=Q(daily_assignments__date__range=(period_start, period_end)),
            )
        ).filter(assignment_count=0)
        unstaffed_tasks = list(unstaffed_qs.order_by(
            "week__start_date", "section__name", "task_template__name"
        )[:25])

        no_shift_count = month_assignments.filter(shift__isnull=True).count()
        shift_distribution_rows = (
            month_assignments.order_by().values("shift__name").annotate(count=Count("pk")).order_by("shift__name")
        )
        shift_labels = [row["shift__name"] or "Shift not set" for row in shift_distribution_rows]
        shift_values = [row["count"] for row in shift_distribution_rows]

        assignment_by_day = {
            row["date"]: row["count"]
            for row in month_assignments.order_by().values("date").annotate(count=Count("pk"))
        }
        trend = [
            {"label": f"{day.day:02d}", "date": day.isoformat(), "count": assignment_by_day.get(day, 0)}
            for day in (period_start + timedelta(days=offset) for offset in range((period_end - period_start).days + 1))
        ]

        section_task_counts = {
            row["section_id"]: row
            for row in month_tasks_qs.values("section_id", "section__name").annotate(
                tasks=Count("pk", distinct=True),
                staffed=Count(
                    "pk",
                    filter=Q(daily_assignments__date__range=(period_start, period_end)),
                    distinct=True,
                ),
            )
        }
        section_assignment_counts = {
            row["task__section_id"]: row["count"]
            for row in month_assignments.order_by().values("task__section_id").annotate(count=Count("pk"))
        }
        section_rows = []
        for section_id, row in section_task_counts.items():
            task_count = row["tasks"]
            staffed = row["staffed"]
            section_rows.append({
                "name": row["section__name"],
                "tasks": task_count,
                "assignments": section_assignment_counts.get(section_id, 0),
                "tasks_with_assignees": staffed,
                "coverage": round(staffed * 100 / task_count) if task_count else 0,
            })
        section_rows.sort(key=lambda row: row["name"] or "")

        recent_schedules = month_schedules.annotate(week_count=Count("weeks", distinct=True)).order_by(
            "treatment_plant__name", "section__name"
        )[:50]
        schedule_notes = list(
            month_schedules.exclude(notes="").values(
                "treatment_plant__name", "section__name", "notes"
            ).order_by("treatment_plant__name", "section__name")[:30]
        )
        weeks = DutyWeek.objects.filter(schedule__in=month_schedules).select_related(
            "schedule", "schedule__section", "schedule__treatment_plant"
        ).annotate(
            task_count=Count("weekly_tasks", distinct=True),
            daily_assignment_count=Count("weekly_tasks__daily_assignments", distinct=True),
        ).order_by("start_date", "schedule__treatment_plant__name", "schedule__section__name")[:100]
        current_tasks = month_tasks_qs.annotate(
            assignment_count=Count(
                "daily_assignments",
                filter=Q(daily_assignments__date__range=(period_start, period_end)),
            )
        ).order_by("week__start_date", "section__name", "task_template__name")[:100]
        missing_shift_assignments = month_assignments.filter(shift__isnull=True).order_by(
            "date", "user__last_name", "user__first_name"
        )[:30]
        daily_assignment_rows = month_assignments[:100]

        sections = Section.objects.order_by("name")
        if selected_plant:
            sections = sections.filter(pk=user_section_id) if not is_superuser else sections.filter(treatment_plant=selected_plant)
        elif not is_superuser:
            sections = sections.none()

        nav_items = [
            {"id": "overview", "label": "Monthly overview", "icon": "gauge-high"},
            {"id": "schedules", "label": "Monthly schedules", "icon": "calendar-days"},
            {"id": "weeks", "label": "Duty weeks", "icon": "calendar-week"},
            {"id": "weekly-plans", "label": "Weekly duty tasks", "icon": "list-check"},
            {"id": "daily-assignments", "label": "Daily assignments", "icon": "user-clock"},
            {"id": "section-load", "label": "Section workload", "icon": "layer-group"},
            {"id": "shift-load", "label": "Shift distribution", "icon": "clock"},
            {"id": "task-library", "label": "Task library", "icon": "book-open"},
        ]

        total_assignments = month_assignments.count()
        staffed_percent = round(staffed_task_plans * 100 / total_task_plans) if total_task_plans else 0
        return render(request, self.template_name, {
            "app": {"name": "Weekly Tasks", "label": "Weekly Tasks"},
            "plant_options": plant_options,
            "selected_plant": selected_plant,
            "show_plant_selector": is_superuser,
            "scope_issue": "plant" if not is_superuser and not user_plant_id else "section" if not is_superuser and not user_section_id else "",
            "selected_section": request.GET.get("section", "overview"),
            "can_add_task_assignments": request.user.is_superuser or request.user.has_perm("weeklytasks.add_dailytaskassignment"),
            "can_edit_task_assignments": request.user.is_superuser or request.user.has_perm("weeklytasks.change_dailytaskassignment"),
            "today": today,
            "month": month,
            "year": year,
            "period_start": period_start,
            "period_end": period_end,
            "month_label": period_start.strftime("%B %Y"),
            "month_choices": [(value, date(year, value, 1).strftime("%B")) for value in range(1, 13)],
            "year_choices": range(max(2000, year - 3), min(2100, year + 3) + 1),
            "previous_period_url": self._period_url(previous_period, selected_plant),
            "next_period_url": self._period_url(next_period, selected_plant),
            "week_label": f"{period_start:%d %b} – {period_end:%d %b %Y}",
            "nav_items": nav_items,
            "task_columns": task_columns,
            "matrix_rows": matrix_rows,
            "matrix_week_count": len(matrix_rows),
            "staffed_task_plans": staffed_task_plans,
            "total_task_plans": total_task_plans,
            "unstaffed_count": unstaffed_count,
            "staffed_percent": staffed_percent,
            "kpis": [
                {"label": "Scheduled weeks", "value": len(matrix_rows), "detail": "Plant-level week windows", "icon": "calendar-week", "tone": "cyan", "target": "overview"},
                {"label": "Routine task plans", "value": total_task_plans, "detail": f"{len(task_columns)} task types in {period_start:%B}", "icon": "list-check", "tone": "blue", "target": "weekly-plans"},
                {"label": "Staffed task plans", "value": f"{staffed_task_plans}/{total_task_plans}", "detail": f"{staffed_percent}% of planned work has an assignee", "icon": "user-check", "tone": "green", "target": "weekly-plans"},
                {"label": "Needs assignment", "value": unstaffed_count, "detail": "Task plans with no assigned staff", "icon": "user-slash", "tone": "amber", "target": "weekly-plans"},
                {"label": "People assignments", "value": total_assignments, "detail": "Daily assignments in selected month", "icon": "users", "tone": "violet", "target": "daily-assignments"},
                {"label": "Shift gaps", "value": no_shift_count, "detail": "Assignments missing a shift", "icon": "clock", "tone": "rose", "target": "shift-gaps"},
            ],
            "recent_schedules": recent_schedules,
            "schedule_notes": schedule_notes,
            "weeks": weeks,
            "current_tasks": current_tasks,
            "unstaffed_tasks": unstaffed_tasks,
            "missing_shift_assignments": missing_shift_assignments,
            "daily_assignment_rows": daily_assignment_rows,
            "section_rows": section_rows,
            "trend": trend,
            "shift_chart": {"labels": shift_labels, "values": shift_values},
            "week_start": period_start,
            "week_end": period_end,
            "horizon_end": period_end,
            "template_rows": StandardWeeklyTask.objects.filter(is_active=True).order_by("code", "name")[:30],
            "weekly_task_count": total_task_plans,
            "daily_assignment_count": total_assignments,
            "section_count": sections.count(),
        })

    @staticmethod
    def _bounded_int(value, minimum, maximum, fallback):
        try:
            result = int(value)
        except (TypeError, ValueError):
            return fallback
        return result if minimum <= result <= maximum else fallback

    @staticmethod
    def _period_url(period, selected_plant):
        query = f"month={period.month}&year={period.year}"
        if selected_plant:
            query += f"&plant={selected_plant.pk}"
        return f"?{query}#overview"
