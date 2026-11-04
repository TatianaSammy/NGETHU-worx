from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta

from accounts.models import CustomUser
from django.db.models import Count, F, Q
from django.shortcuts import render
from django.utils import timezone
from django.views import View

from core.mixins import WanjalaMixin
from core.models import Section
from core.plant_section_scope import scoped_queryset, user_plant_section
from sc.exports import export_duty_roster
from sc.models import (
    Apparatus,
    ApparatusCondition,
    AnnualLeave,
    AuditLog,
    MonthlyDutyRoster,
    Shift,
    ShiftAssignment,
    ShiftHandover,
    ShiftSectionRequirement,
    ShiftSupervisor,
)
from t.models import TreatmentPlant


class DashboardView(WanjalaMixin, View):
    """Operational shift dashboard built from SC domain records."""

    template_name = "sc/dashboard.html"
    app_label = "sc"

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        today = timezone.localdate()
        month = self._bounded_int(request.GET.get("month"), 1, 12, today.month)
        year = self._bounded_int(request.GET.get("year"), 2000, 2100, today.year)
        period_start = date(year, month, 1)
        period_end = date(year, month, monthrange(year, month)[1])
        start_of_month = period_start
        previous_period = (period_start - timedelta(days=1)).replace(day=1)
        next_period = (period_end + timedelta(days=1)).replace(day=1)
        start_of_week = today - timedelta(days=today.weekday())
        today_time = timezone.localtime().time()

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

        shifts = Shift.objects.filter(is_deleted=False)
        if selected_plant:
            shifts = shifts.filter(treatment_plant=selected_plant)
            if not is_superuser:
                shifts = shifts.filter(section_requirements__section_id=user_section_id).distinct() if user_section_id else shifts.none()
        elif not is_superuser:
            shifts = shifts.none()
        assignments = ShiftAssignment.objects.filter(is_deleted=False, shift__in=shifts)
        if not is_superuser:
            assignments = assignments.filter(staff__assigned_treatment_plant_id=user_plant_id, staff__section_id=user_section_id)
        handovers = scoped_queryset(ShiftHandover.objects.filter(shift__in=shifts), request.user)
        requirements = ShiftSectionRequirement.objects.filter(
            shift__in=shifts,
            section__is_active=True,
            section__treatment_plant_id=F("shift__treatment_plant_id"),
        ).select_related(
            "shift", "section"
        ).order_by("shift__start_time", "section__name")
        supervisors = ShiftSupervisor.objects.filter(is_active=True)
        apparatus = Apparatus.objects.select_related("equipment", "equipment__section")
        if selected_plant:
            if not is_superuser:
                requirements = requirements.filter(section_id=user_section_id)
            supervisors = supervisors.filter(treatment_plant=selected_plant)
            apparatus = apparatus.filter(equipment__section__treatment_plant=selected_plant)
            if not is_superuser:
                if user_section_id:
                    supervisors = supervisors.filter(section_id=user_section_id, user__assigned_treatment_plant_id=user_plant_id, user__section_id=user_section_id)
                    apparatus = apparatus.filter(equipment__section_id=user_section_id)
                else:
                    supervisors = supervisors.none()
                    apparatus = apparatus.none()
        elif not is_superuser:
            supervisors = supervisors.none()
            apparatus = apparatus.none()

        shift_rows = list(shifts.select_related("treatment_plant").order_by("start_time", "name"))
        current_shifts = []
        for shift in shift_rows:
            on_duty = (
                shift.start_time <= today_time < shift.end_time
                if not shift.is_overnight
                else today_time >= shift.start_time or today_time < shift.end_time
            )
            shift.is_active_now = on_duty
            if on_duty:
                current_shifts.append(shift)

        today_assignments = assignments.filter(date=today)
        today_assignment_rows = today_assignments.select_related(
            "staff", "staff__section", "shift"
        ).order_by("shift__start_time", "staff__last_name", "staff__first_name")[:30]
        roster_day_type = today_assignments.values_list("day_type", flat=True).first()
        if roster_day_type not in {choice for choice, _label in ShiftAssignment.DAYTYPE_CHOICES}:
            roster_day_type = (
                ShiftAssignment.DAYTYPE_WEEKEND if today.weekday() >= 5 else ShiftAssignment.DAYTYPE_WEEKDAY
            )
        today_attendance = {
            row["attendance_status"]: row["count"]
            for row in today_assignments.values("attendance_status").annotate(count=Count("pk"))
        }
        month_attendance = {
            row["attendance_status"]: row["count"]
            for row in assignments.filter(date__gte=start_of_month, date__lte=period_end)
            .values("attendance_status").annotate(count=Count("pk"))
        }
        monthly_total = sum(month_attendance.values())
        present_month = month_attendance.get(ShiftAssignment.ATTENDANCE_PRESENT, 0)
        attendance_rate = round(present_month * 100 / monthly_total) if monthly_total else 0

        today_by_slot = {
            (row["shift_id"], row["staff__section_id"]): row["count"]
            for row in today_assignments.values("shift_id", "staff__section_id").annotate(count=Count("pk"))
        }
        coverage_rows = []
        required_total = assigned_total = understaffed_count = 0
        for requirement in requirements:
            required = requirement.get_required_staff(roster_day_type)
            assigned = today_by_slot.get((requirement.shift_id, requirement.section_id), 0)
            fill = round(min(assigned, required) * 100 / required) if required else 100
            gap = max(0, required - assigned)
            required_total += required
            assigned_total += min(assigned, required)
            understaffed_count += int(gap > 0)
            coverage_rows.append({
                "shift": requirement.shift.name,
                "section": requirement.section.name,
                "assigned": assigned,
                "required": required,
                "gap": gap,
                "fill": fill,
            })
        coverage_rate = round(assigned_total * 100 / required_total) if required_total else 0

        week_assignments = assignments.filter(date__gte=start_of_week, date__lte=start_of_week + timedelta(days=6))
        planned_by_day = {
            row["date"]: row["total"]
            for row in week_assignments.values("date").annotate(total=Count("pk"))
        }
        present_by_day = {
            row["date"]: row["present"]
            for row in week_assignments.filter(date__lte=today).values("date").annotate(
                present=Count("pk", filter=Q(attendance_status=ShiftAssignment.ATTENDANCE_PRESENT))
            )
        }
        trend = []
        for offset in range(7):
            day = start_of_week + timedelta(days=offset)
            trend.append({
                "label": day.strftime("%a"),
                "date": day.isoformat(),
                "total": planned_by_day.get(day, 0),
                "present": present_by_day.get(day, 0),
            })

        latest_handover_ids = list(handovers.order_by("-date", "-handed_over_at").values_list("pk", flat=True)[:10])
        latest_handovers = handovers.filter(pk__in=latest_handover_ids).select_related(
            "shift", "outgoing_supervisor", "incoming_supervisor"
        ).order_by("-date", "-handed_over_at")

        latest_conditions = scoped_queryset(
            ApparatusCondition.objects.filter(apparatus__in=apparatus).select_related("apparatus", "handover"),
            request.user,
        )
        latest_by_apparatus = {}
        for condition in latest_conditions.order_by("apparatus_id", "-handover__date", "-handover__handed_over_at"):
            latest_by_apparatus.setdefault(condition.apparatus_id, condition)
        apparatus_list = list(apparatus.order_by("name"))
        apparatus_rows = []
        for item in apparatus_list:
            condition = latest_by_apparatus.get(item.pk)
            apparatus_rows.append({
                "name": item.name,
                "equipment": item.equipment.name,
                "status": condition.status if condition else "unreported",
                "remarks": condition.remarks if condition else "No condition recorded yet",
                "date": condition.handover.date if condition else None,
            })
        apparatus_attention = sum(
            (latest_by_apparatus.get(item.pk).status if latest_by_apparatus.get(item.pk) else "unreported")
            in {"needs_repair", "out_of_service", "unreported"}
            for item in apparatus_list
        )
        apparatus_rows = apparatus_rows[:15]

        sections = Section.objects.order_by("name")
        if selected_plant:
            sections = sections.filter(pk=user_section_id) if not is_superuser else sections.filter(treatment_plant=selected_plant)
        elif not is_superuser:
            sections = sections.none()
        section_count = sections.count()
        month_rosters = MonthlyDutyRoster.objects.filter(month=month, year=year)
        if selected_plant:
            month_rosters = month_rosters.filter(treatment_plant=selected_plant)
            if not is_superuser:
                if user_section_id:
                    month_rosters = month_rosters.filter(
                        assignments__is_deleted=False,
                        assignments__staff__assigned_treatment_plant_id=user_plant_id,
                        assignments__staff__section_id=user_section_id,
                    ).distinct()
                else:
                    month_rosters = month_rosters.none()
        elif not is_superuser:
            month_rosters = month_rosters.none()
        roster_count = month_rosters.count()
        roster_rows = month_rosters.select_related("treatment_plant", "created_by").annotate(
            assignment_count=Count("assignments", filter=Q(assignments__is_deleted=False, **({"assignments__staff__section_id": user_section_id, "assignments__staff__assigned_treatment_plant_id": user_plant_id} if not is_superuser else {})), distinct=True)
        ).order_by(
            "treatment_plant__name", "month", "year"
        )[:12]

        staff_availability = AnnualLeave.objects.filter(
            status__in=AnnualLeave.UNAVAILABLE_STATUSES,
            start_date__lte=period_end,
            end_date__gte=period_start,
        ).select_related("staff", "treatment_plant", "created_by", "updated_by")
        if selected_plant:
            staff_availability = staff_availability.filter(treatment_plant=selected_plant)
            if not is_superuser:
                staff_availability = staff_availability.filter(staff__assigned_treatment_plant_id=user_plant_id, staff__section_id=user_section_id) if user_section_id else staff_availability.none()
        elif not is_superuser:
            staff_availability = staff_availability.none()
        active_availability = list(staff_availability.order_by("start_date", "staff__last_name", "staff__first_name"))
        availability_rows = active_availability[:100]
        availability_count = len(active_availability)
        annual_leave_by_plant_week = {}
        weeks = MonthlyDutyRoster.get_weeks_in_month(year, month)
        for record in active_availability:
            staff_name = record.staff.get_full_name() or record.staff.get_username()
            for week_start, week_end in weeks:
                unavailable_start = max(record.start_date, week_start)
                unavailable_end = min(record.end_date, week_end)
                if unavailable_start <= unavailable_end:
                    dates = f"{unavailable_start:%d %b}–{unavailable_end:%d %b}"
                    annual_leave_by_plant_week.setdefault(
                        (record.treatment_plant_id, week_start), set()
                    ).add(f"{staff_name} · {record.get_status_display()} ({dates})")

        roster_assignments = assignments.filter(date__range=(period_start, period_end)).select_related(
            "staff", "staff__section", "supervisor", "temp_supervisor", "shift", "shift__treatment_plant"
        ).order_by("date", "shift__start_time", "staff__last_name", "staff__first_name")
        roster_groups = {}
        for assignment in roster_assignments:
            week_start, week_end = next(
                ((week_start, week_end) for week_start, week_end in weeks if week_start <= assignment.date <= week_end),
                (None, None),
            )
            if week_start is None:
                continue
            key = (assignment.shift.treatment_plant_id, week_start, assignment.shift_id)
            group = roster_groups.setdefault(key, {
                "plant": assignment.shift.treatment_plant,
                "week_start": week_start,
                "week_end": week_end,
                "shift": assignment.shift,
                "holiday": set(),
                "weekday": set(),
                "weekend": set(),
                "holiday_assignments": [],
                "weekday_assignments": [],
                "weekend_assignments": [],
                "supervisors": set(),
                "sections": set(),
            })
            name = assignment.staff.get_full_name() or assignment.staff.get_username()
            staff_entry = {
                "id": assignment.pk,
                "name": name,
                "date": assignment.date,
                "day_type": assignment.day_type,
                "section_id": assignment.staff.section_id,
                "section_name": assignment.staff.section.name if assignment.staff.section_id else "",
                "shift_id": assignment.shift_id,
                "shift_name": assignment.shift.name,
            }
            if assignment.day_type == ShiftAssignment.DAYTYPE_HOLIDAY:
                group["holiday"].add(name)
                group["holiday_assignments"].append(staff_entry)
            elif assignment.day_type == ShiftAssignment.DAYTYPE_WEEKEND:
                group["weekend"].add(name)
                group["weekend_assignments"].append(staff_entry)
            else:
                group["weekday"].add(name)
                group["weekday_assignments"].append(staff_entry)
            if assignment.staff.section_id:
                group["sections"].add(assignment.staff.section_id)
            supervisor = assignment.temp_supervisor or assignment.supervisor
            if supervisor:
                group["supervisors"].add(supervisor.get_full_name() or supervisor.get_username())

        designated_supervisors = {}
        for supervisor in supervisors.select_related("user", "section"):
            key = (supervisor.treatment_plant_id, supervisor.section_id)
            designated_supervisors.setdefault(key, set()).add(
                supervisor.user.get_full_name() or supervisor.user.get_username()
            )

        roster_documents = {}
        requirements_by_shift = {}
        for requirement in requirements:
            requirements_by_shift.setdefault(requirement.shift_id, []).append(requirement)
        plant_by_id = {shift.treatment_plant_id: shift.treatment_plant for shift in shift_rows}
        for record in active_availability:
            plant_by_id[record.treatment_plant_id] = record.treatment_plant
        if selected_plant:
            plant_by_id[selected_plant.pk] = selected_plant
        for plant_id, plant in sorted(plant_by_id.items(), key=lambda item: item[1].name.casefold()):
            plant_shifts = [shift for shift in shift_rows if shift.treatment_plant_id == plant_id]
            document_rows = []
            for week_start, week_end in weeks:
                for shift in plant_shifts:
                    group = roster_groups.get((plant_id, week_start, shift.pk), {})
                    holiday_assignments = sorted(group.get("holiday_assignments", []), key=lambda item: (item["date"], item["name"].casefold()))
                    weekday_assignments = sorted(group.get("weekday_assignments", []), key=lambda item: (item["date"], item["name"].casefold()))
                    weekend_assignments = sorted(group.get("weekend_assignments", []), key=lambda item: (item["date"], item["name"].casefold()))
                    week_dates = [week_start + timedelta(days=offset) for offset in range((week_end - week_start).days + 1)]
                    category_specs = [
                        ("Public holiday", ShiftAssignment.DAYTYPE_HOLIDAY, holiday_assignments, None),
                        ("Weekdays", ShiftAssignment.DAYTYPE_WEEKDAY, weekday_assignments, sum(day.weekday() < 5 for day in week_dates)),
                        ("Saturday and Sunday", ShiftAssignment.DAYTYPE_WEEKEND, weekend_assignments, sum(day.weekday() >= 5 for day in week_dates)),
                    ]
                    roster_cells = []
                    for category, day_type, cell_assignments, days_in_category in category_specs:
                        has_dates = days_in_category is None or days_in_category > 0
                        no_dates_label = (
                            "No weekdays in this week"
                            if day_type == ShiftAssignment.DAYTYPE_WEEKDAY
                            else "No weekend days in this week"
                        )
                        required = None if days_in_category is None else sum(
                            item.get_required_staff(day_type) * days_in_category
                            for item in requirements_by_shift.get(shift.pk, [])
                        )
                        assigned_count = len(cell_assignments)
                        fill = min(100, round(assigned_count * 100 / required)) if required else (100 if required == 0 else None)
                        roster_cells.append({
                            "category": category,
                            "day_type": day_type,
                            "has_dates": has_dates,
                            "no_dates_label": no_dates_label,
                            "assignments": cell_assignments,
                            "assigned_count": assigned_count,
                            "required": required,
                            "gap": max(0, required - assigned_count) if required is not None else None,
                            "fill": fill,
                            "status": "unavailable" if not has_dates else "ready" if required is not None and assigned_count >= required else "open" if required is not None else "variable",
                        })
                    supervisor_names = group.get("supervisors", set())
                    if not supervisor_names:
                        for section_id in group.get("sections", set()):
                            supervisor_names.update(designated_supervisors.get((plant_id, section_id), set()))
                            supervisor_names.update(designated_supervisors.get((plant_id, None), set()))
                    if not supervisor_names and not group.get("sections"):
                        supervisor_names.update(designated_supervisors.get((plant_id, None), set()))
                    document_rows.append({
                        "plant_id": plant_id,
                        "shift_id": shift.pk,
                        "week_start": week_start,
                        "week_end": week_end,
                        "shift_name": shift.name,
                        "start_time": shift.start_time,
                        "end_time": shift.end_time,
                        "holiday": sorted(group.get("holiday", set()), key=str.casefold),
                        "weekday": sorted(group.get("weekday", set()), key=str.casefold),
                        "weekend": sorted(group.get("weekend", set()), key=str.casefold),
                        "holiday_assignments": holiday_assignments,
                        "weekday_assignments": weekday_assignments,
                        "weekend_assignments": weekend_assignments,
                        "roster_cells": roster_cells,
                        "assignment_count": len(holiday_assignments) + len(weekday_assignments) + len(weekend_assignments),
                        "supervisors": sorted(supervisor_names, key=str.casefold),
                        "annual_leave": sorted(
                            annual_leave_by_plant_week.get((plant_id, week_start), set()),
                            key=str.casefold,
                        ),
                    })
            roster_documents[plant_id] = {"plant": plant, "rows": document_rows}

        audit_logs = AuditLog.objects.select_related("user", "content_type").order_by("-timestamp")
        if not is_superuser:
            audit_logs = audit_logs.filter(user=request.user)
        audit_logs = audit_logs[:10]

        nav_items = [
            {"id": "overview", "label": "Duty roster", "icon": "table-list"},
            {"id": "operations-metrics", "label": "Operations metrics", "icon": "gauge-high"},
            {"id": "shift-catalog", "label": "Shift catalogue", "icon": "arrows-rotate"},
            {"id": "staffing", "label": "Staffing coverage", "icon": "people-group"},
            {"id": "attendance", "label": "Attendance", "icon": "user-check"},
            {"id": "rosters", "label": "Monthly duty rosters", "icon": "calendar-days"},
            {"id": "annual-leave", "label": "Annual leave & off duty", "icon": "calendar-minus"},
            {"id": "supervisors", "label": "Shift supervisors", "icon": "user-shield"},
            {"id": "handovers", "label": "Shift handovers", "icon": "right-left"},
            {"id": "apparatus", "label": "Apparatus condition", "icon": "screwdriver-wrench"},
            {"id": "activity", "label": "Audit activity", "icon": "clock-rotate-left"},
        ]
        selected_section = request.GET.get("section", "overview")
        if selected_section not in {item["id"] for item in nav_items}:
            selected_section = "overview"
        active_staff = CustomUser.objects.filter(is_active=True)
        if selected_plant:
            active_staff = active_staff.filter(assigned_treatment_plant_id=selected_plant.pk)
        elif is_superuser:
            active_staff = active_staff.filter(assigned_treatment_plant_id__in={shift.treatment_plant_id for shift in shift_rows})
        elif user_plant_id:
            active_staff = active_staff.filter(assigned_treatment_plant_id=user_plant_id)
        else:
            active_staff = active_staff.none()
        if not is_superuser and user_section_id:
            active_staff = active_staff.filter(section_id=user_section_id)
        context = {
            "app": {"name": "Shift Control", "label": "Shift Control"},
            "plant_options": plant_options,
            "selected_plant": selected_plant,
            "show_plant_selector": is_superuser,
            "scope_issue": "plant" if not is_superuser and not user_plant_id else "section" if not is_superuser and not user_section_id else "",
            "selected_section": selected_section,
            "today": today,
            "month_label": period_start.strftime("%B %Y"),
            "selected_month": month,
            "selected_year": year,
            "month_choices": [(value, date(year, value, 1).strftime("%B")) for value in range(1, 13)],
            "year_choices": range(max(2000, year - 3), min(2100, year + 3) + 1),
            "previous_month_url": self._period_url(previous_period, selected_plant),
            "next_month_url": self._period_url(next_period, selected_plant),
            "roster_documents": list(roster_documents.values()),
            "can_add_roster": request.user.is_superuser or request.user.has_perm("sc.add_shiftassignment"),
            "can_edit_roster": request.user.is_superuser or request.user.has_perm("sc.change_shiftassignment"),
            "roster_assignment_count": len(roster_assignments),
            "roster_staff_count": active_staff.count(),
            "availability_rows": availability_rows,
            "availability_count": availability_count,
            "roster_count": roster_count,
            "nav_items": nav_items,
            "kpis": [
                {"label": "Active shifts", "value": len(shift_rows), "detail": f"{len(current_shifts)} running now", "icon": "arrows-spin", "tone": "cyan", "target": "operations-metrics"},
                {"label": "Today's roster", "value": today_assignments.count(), "detail": f"{today_attendance.get('present', 0)} marked present", "icon": "clipboard-list", "tone": "blue", "target": "attendance"},
                {"label": "Staffing coverage", "value": f"{coverage_rate}%", "detail": f"{understaffed_count} shift-section gaps", "icon": "people-group", "tone": "amber", "target": "staffing"},
                {"label": "Attendance this month", "value": f"{attendance_rate}%", "detail": f"{monthly_total:,} recorded assignments", "icon": "user-check", "tone": "green", "target": "attendance"},
                {"label": "Handover waiting", "value": handovers.filter(is_taken_over=False).count(), "detail": "Awaiting takeover", "icon": "right-left", "tone": "rose", "target": "handovers"},
                {"label": "Active supervisors", "value": supervisors.count(), "detail": f"Across {section_count} operational sections", "icon": "user-shield", "tone": "violet", "target": "supervisors"},
                {"label": "Apparatus attention", "value": apparatus_attention, "detail": "Repair, out of service or unreported", "icon": "screwdriver-wrench", "tone": "rose", "target": "apparatus"},
                {"label": "Rosters this month", "value": roster_count, "detail": "Monthly duty plans", "icon": "calendar-days", "tone": "blue", "target": "rosters"},
                {"label": "Staff unavailable", "value": availability_count, "detail": "Annual leave and off-duty periods", "icon": "calendar-minus", "tone": "violet", "target": "annual-leave"},
            ],
            "current_shifts": current_shifts,
            "shift_rows": shift_rows,
            "today_assignment_rows": today_assignment_rows,
            "shift_count": len(shift_rows),
            "coverage_rows": coverage_rows,
            "coverage_rate": coverage_rate,
            "understaffed_count": understaffed_count,
            "attendance_today": today_attendance,
            "attendance_month": month_attendance,
            "attendance_trend": trend,
            "coverage_chart": {
                "labels": [row["shift"] + " · " + row["section"] for row in coverage_rows[:10]],
                "values": [row["fill"] for row in coverage_rows[:10]],
            },
            "latest_handovers": latest_handovers,
            "roster_rows": roster_rows,
            "apparatus_rows": apparatus_rows,
            "apparatus_attention": apparatus_attention,
            "supervisors": supervisors.select_related("user", "section", "treatment_plant").order_by(
                "section__name", "user__last_name", "user__first_name"
            )[:20],
            "audit_logs": audit_logs,
        }
        export_format = request.GET.get("export", "").lower()
        if export_format in {"csv", "xlsx", "pdf", "docx"}:
            return export_duty_roster(
                context["roster_documents"],
                context["month_label"],
                export_format,
            )
        return render(request, self.template_name, context)

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
