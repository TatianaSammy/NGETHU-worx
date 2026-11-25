from __future__ import annotations

from calendar import monthrange
from collections import Counter, defaultdict
from datetime import date, timedelta
from types import SimpleNamespace

from django.db.models import Exists, F, OuterRef, Q
from django.shortcuts import render
from django.utils import timezone
from django.views import View

from core.mixins import WanjalaMixin
from core.models import Section
from core.plant_section_scope import section_scope_q, user_plant_section
from sc.exports import export_shift_assignments
from sc.models import (
    AnnualLeave,
    INCHARGE_GROUPS,
    MonthlyDutyRoster,
    Shift,
    ShiftAssignment,
    ShiftSectionRequirement,
    ShiftSupervisor,
    WeeklyShiftTeam,
)
from django.contrib.auth import get_user_model
from t.models import TreatmentPlant


class DashboardView(WanjalaMixin, View):
    template_name = "sc/dashboard.html"
    app_label = "sc"

    def handle_export(self, request):
        return self.get(request)

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()

        today = timezone.localdate()
        now = timezone.now()
        month = self._bounded_int(request.GET.get("month"), 1, 12, today.month)
        year = self._bounded_int(request.GET.get("year"), 2000, 2100, today.year)
        month_start = date(year, month, 1)
        month_end = date(year, month, monthrange(year, month)[1])
        is_superuser = bool(request.user.is_superuser)
        assigned_plant_id, assigned_section_id = user_plant_section(request.user)
        plant_options = TreatmentPlant.objects.order_by("name") if is_superuser else TreatmentPlant.objects.filter(pk=assigned_plant_id)
        selected_plant = None
        invalid_plant = False

        if is_superuser:
            requested_plant = (request.GET.get("plant") or "").strip()
            if requested_plant and requested_plant.isdecimal():
                selected_plant = TreatmentPlant.objects.filter(pk=requested_plant).first()
                invalid_plant = selected_plant is None
            elif requested_plant:
                invalid_plant = True
        elif assigned_plant_id:
            selected_plant = TreatmentPlant.objects.filter(pk=assigned_plant_id).first()

        sections = Section.objects.filter(
            is_active=True,
            shift_requirements__shift__is_deleted=False,
            shift_requirements__section__is_active=True,
        ).filter(section_scope_q(
            "treatment_plant_id",
            F("shift_requirements__shift__treatment_plant_id"),
        )).distinct().order_by("name")
        if selected_plant:
            sections = sections.filter(
                section_scope_q("treatment_plant_id", selected_plant.pk),
                shift_requirements__shift__treatment_plant_id=selected_plant.pk,
            )
        elif not is_superuser or invalid_plant:
            sections = sections.none()

        scope_issue = ""
        if not is_superuser and not assigned_plant_id:
            scope_issue = "plant"
        elif not is_superuser and assigned_plant_id and not selected_plant:
            scope_issue = "plant"
        elif not is_superuser and not assigned_section_id:
            scope_issue = "section"
        elif invalid_plant:
            scope_issue = "invalid_plant"

        section_filter = (request.GET.get("filter_section") or "").strip()
        selected_section = None
        invalid_section = False
        if is_superuser:
            if section_filter and section_filter.isdecimal():
                selected_section = sections.filter(pk=section_filter).first()
                invalid_section = selected_section is None
            elif section_filter:
                invalid_section = True
        elif assigned_section_id and scope_issue != "section":
            selected_section = sections.filter(pk=assigned_section_id).first()
            if selected_section is None:
                scope_issue = "section"

        if invalid_section and not invalid_plant:
            scope_issue = "invalid_section"

        scoped_shifts = Shift.objects.filter(
            is_deleted=False,
            section_requirements__section__is_active=True,
        ).filter(section_scope_q(
            "section_requirements__section__treatment_plant_id",
            F("treatment_plant_id"),
        )).select_related("treatment_plant").distinct().order_by("start_time", "name")
        if selected_plant:
            scoped_shifts = scoped_shifts.filter(treatment_plant_id=selected_plant.pk)
        elif not is_superuser:
            scoped_shifts = scoped_shifts.none()
        if selected_section:
            scoped_shifts = scoped_shifts.filter(section_requirements__section_id=selected_section.pk).distinct()
            if not selected_plant and selected_section.treatment_plant_id:
                scoped_shifts = scoped_shifts.filter(treatment_plant_id=selected_section.treatment_plant_id)
        elif not is_superuser:
            scoped_shifts = scoped_shifts.filter(section_requirements__section_id=assigned_section_id).distinct() if assigned_section_id else scoped_shifts.none()
        if invalid_plant or invalid_section or scope_issue in {"plant", "section"}:
            scoped_shifts = scoped_shifts.none()

        requirements = ShiftSectionRequirement.objects.filter(
            shift__in=scoped_shifts,
            section__is_active=True,
        ).filter(section_scope_q("section__treatment_plant_id", F("shift__treatment_plant_id")))
        if selected_section:
            requirements = requirements.filter(section_id=selected_section.pk)
        if not is_superuser and assigned_section_id:
            requirements = requirements.filter(section_id=assigned_section_id)

        shift_filter = (request.GET.get("shift") or "").strip()
        if shift_filter:
            if shift_filter.isdecimal() and scoped_shifts.filter(pk=shift_filter).exists():
                requirements = requirements.filter(shift_id=shift_filter)
            else:
                requirements = requirements.none()

        assignments = ShiftAssignment.objects.filter(
            is_deleted=False,
            date__range=(month_start, month_end),
            shift__is_deleted=False,
            duty_roster__month=month,
            duty_roster__year=year,
            shift__treatment_plant_id=F("duty_roster__treatment_plant_id"),
            staff__assigned_treatment_plant_id=F("shift__treatment_plant_id"),
            shift__section_requirements__section__is_active=True,
            shift__section_requirements__section_id=F("staff__section_id"),
        ).filter(section_scope_q(
            "shift__section_requirements__section__treatment_plant_id",
            F("shift__treatment_plant_id"),
        )).exclude(
            staff__groups__name__in=INCHARGE_GROUPS,
        ).exclude(
            staff__annual_leave_records__status__in=AnnualLeave.EXEMPT_FROM_ROUTINE_STATUSES,
            staff__annual_leave_records__start_date__lte=F("date"),
            staff__annual_leave_records__end_date__gte=F("date"),
        ).annotate(
            _is_active_temp_supervisor=Exists(
                ShiftAssignment.objects.filter(
                    temp_supervisor_id=OuterRef("staff_id"),
                    date=OuterRef("date"),
                    is_deleted=False,
                    temp_supervisor_start__lte=now,
                    temp_supervisor_end__gt=now,
                ).exclude(pk=OuterRef("pk"))
            )
        ).filter(_is_active_temp_supervisor=False).select_related(
            "staff",
            "staff__section",
            "shift",
            "shift__treatment_plant",
            "duty_roster",
            "supervisor",
            "temp_supervisor",
            "handover_to",
            "reassigned_from",
            "reassigned_from__staff",
            "reassigned_from__shift",
        ).distinct()

        if is_superuser:
            if selected_plant:
                assignments = assignments.filter(
                    shift__treatment_plant_id=selected_plant.pk,
                    duty_roster__treatment_plant_id=selected_plant.pk,
                    staff__assigned_treatment_plant_id=selected_plant.pk,
                )
            elif selected_section and selected_section.treatment_plant_id:
                assignments = assignments.filter(
                    shift__treatment_plant_id=selected_section.treatment_plant_id,
                    duty_roster__treatment_plant_id=selected_section.treatment_plant_id,
                    staff__assigned_treatment_plant_id=selected_section.treatment_plant_id,
                )
        elif assigned_plant_id and assigned_section_id and scope_issue != "section":
            assignments = assignments.filter(
                shift__treatment_plant_id=assigned_plant_id,
                duty_roster__treatment_plant_id=assigned_plant_id,
                staff__assigned_treatment_plant_id=assigned_plant_id,
                staff__section_id=assigned_section_id,
                shift__section_requirements__section_id=assigned_section_id,
            )
        else:
            assignments = assignments.none()

        if selected_section:
            assignments = assignments.filter(
                staff__section_id=selected_section.pk,
                shift__section_requirements__section_id=selected_section.pk,
            )
        if scope_issue in {"plant", "section", "invalid_plant", "invalid_section"}:
            assignments = assignments.none()

        roster_assignments = assignments
        query = (request.GET.get("q") or "").strip()
        if query:
            assignments = assignments.filter(
                Q(staff__first_name__icontains=query)
                | Q(staff__last_name__icontains=query)
                | Q(staff__username__icontains=query)
                | Q(staff__section__name__icontains=query)
                | Q(shift__name__icontains=query)
                | Q(shift__treatment_plant__name__icontains=query)
                | Q(supervisor__first_name__icontains=query)
                | Q(supervisor__last_name__icontains=query)
                | Q(remarks__icontains=query)
            )

        attendance_filter = (request.GET.get("attendance") or "").strip()
        attendance_values = {value for value, _label in ShiftAssignment.ATTENDANCE_CHOICES}
        if attendance_filter in attendance_values:
            assignments = assignments.filter(attendance_status=attendance_filter)
        elif attendance_filter:
            assignments = assignments.none()

        day_type_filter = (request.GET.get("day_type") or "").strip()
        if day_type_filter == ShiftAssignment.DAYTYPE_WEEKEND:
            # Calendar date wins over stale legacy labels; Django week_day: Sun=1, Sat=7.
            assignments = assignments.filter(date__week_day__in=(1, 7))
        elif day_type_filter == ShiftAssignment.DAYTYPE_WEEKDAY:
            assignments = assignments.filter(date__week_day__range=(2, 6)).exclude(
                day_type=ShiftAssignment.DAYTYPE_HOLIDAY
            )
        elif day_type_filter == ShiftAssignment.DAYTYPE_HOLIDAY:
            assignments = assignments.filter(
                day_type=ShiftAssignment.DAYTYPE_HOLIDAY,
                date__week_day__range=(2, 6),
            )
        elif day_type_filter:
            assignments = assignments.none()

        assignments = assignments.distinct().order_by("date", "shift__start_time", "staff__last_name", "staff__first_name", "pk")
        export_format = (request.GET.get("export") or request.GET.get("type") or "").lower()
        if export_format in {"csv", "xlsx", "pdf", "docx"}:
            return export_shift_assignments(assignments, month_start.strftime("%B %Y"), export_format)

        total_assignments = assignments.count()
        assignment_rows = list(assignments)
        roster_assignment_rows = list(roster_assignments)
        weeks = MonthlyDutyRoster.get_weeks_in_month(year, month)
        week_lookup = {
            start: {"start": start, "end": end, "rows": []}
            for start, end in weeks
        }

        def get_assignment_week(target_date):
            for start, end in weeks:
                if start <= target_date <= end:
                    return start, end
            week_start = max(month_start, target_date - timedelta(days=target_date.weekday()))
            week_end = min(month_end, week_start + timedelta(days=6))
            return week_start, week_end

        def group_assignments(rows):
            grouped = {}
            for assignment in rows:
                week_start, week_end = get_assignment_week(assignment.date)
                assignment.roster_week_start = week_start
                assignment.roster_week_end = week_end
                group_key = (week_start, assignment.shift_id, assignment.staff.section_id)
                grouped.setdefault(group_key, {"weekday": [], "weekend": [], "holiday": []})
                if assignment.date.weekday() >= 5:
                    effective_day_type = ShiftAssignment.DAYTYPE_WEEKEND
                elif assignment.day_type == ShiftAssignment.DAYTYPE_HOLIDAY:
                    effective_day_type = ShiftAssignment.DAYTYPE_HOLIDAY
                else:
                    effective_day_type = ShiftAssignment.DAYTYPE_WEEKDAY
                grouped[group_key].setdefault(effective_day_type, []).append(assignment)
            return grouped

        assignment_groups = group_assignments(assignment_rows)
        roster_assignment_groups = group_assignments(roster_assignment_rows)

        requirement_rows = list(requirements.select_related("shift", "shift__treatment_plant", "section").order_by(
            "shift__treatment_plant__name", "section__name", "shift__start_time", "shift__name"
        ))
        leave_query = AnnualLeave.objects.filter(
            status__in=AnnualLeave.EXEMPT_FROM_ROUTINE_STATUSES,
            start_date__lte=month_end,
            end_date__gte=month_start,
            staff__assigned_treatment_plant_id=F("treatment_plant_id"),
        ).select_related("staff", "staff__section", "treatment_plant").order_by(
            "staff__last_name", "staff__first_name", "start_date"
        )
        plant_ids = {requirement.shift.treatment_plant_id for requirement in requirement_rows}
        section_ids = {requirement.section_id for requirement in requirement_rows}
        shift_ids = {requirement.shift_id for requirement in requirement_rows}
        if selected_plant:
            leave_query = leave_query.filter(treatment_plant_id=selected_plant.pk)
        elif not is_superuser:
            leave_query = leave_query.filter(treatment_plant_id=assigned_plant_id)
        elif selected_section and selected_section.treatment_plant_id:
            leave_query = leave_query.filter(treatment_plant_id=selected_section.treatment_plant_id)
        elif plant_ids:
            leave_query = leave_query.filter(treatment_plant_id__in=plant_ids)
        else:
            leave_query = leave_query.none()
        if selected_section:
            leave_query = leave_query.filter(staff__section_id=selected_section.pk)
        elif not is_superuser:
            leave_query = leave_query.filter(staff__section_id=assigned_section_id)
        elif section_ids:
            leave_query = leave_query.filter(staff__section_id__in=section_ids)
        else:
            leave_query = leave_query.none()
        leave_rows = list(leave_query)

        designated_supervisors = ShiftSupervisor.get_supervisors_map(
            plant_ids,
            section_ids,
            shift_ids=shift_ids,
        ) if plant_ids and section_ids and shift_ids else {}
        section_supervisor_user_ids_by_section = defaultdict(set)
        for supervisor_section_id, supervisor_user_id in ShiftSupervisor.objects.filter(
                treatment_plant_id__in=plant_ids,
                section_id__in=section_ids,
                is_active=True,
                user__is_active=True,
            ).values_list("section_id", "user_id"):
            section_supervisor_user_ids_by_section[supervisor_section_id].add(supervisor_user_id)
        weekly_supervisors = ShiftSupervisor.objects.filter(
            treatment_plant_id__in=plant_ids,
            shift_id__in=shift_ids,
            section_id__in=section_ids,
            week_start__in=[start for start, _end in weeks],
            week_start__isnull=False,
        ).select_related("user") if plant_ids and section_ids and shift_ids else ShiftSupervisor.objects.none()
        weekly_supervisor_map = {
            (item.shift_id, item.section_id, item.week_start): item
            for item in weekly_supervisors
        }
        weekly_team_query = WeeklyShiftTeam.objects.filter(
            duty_roster__month=month,
            duty_roster__year=year,
            shift_id__in=shift_ids,
            section_id__in=section_ids,
            week_start__range=(month_start, month_end),
        ).select_related("duty_roster", "shift", "section").prefetch_related("members", "members__groups")
        if plant_ids:
            weekly_team_query = weekly_team_query.filter(duty_roster__treatment_plant_id__in=plant_ids)
        else:
            weekly_team_query = weekly_team_query.none()
        weekly_team_map = {
            (item.week_start, item.shift_id, item.section_id): item
            for item in weekly_team_query
        }

        for week_start, week_end in weeks:
            week = week_lookup[week_start]
            for requirement in requirement_rows:
                plant_id = requirement.shift.treatment_plant_id
                section_id = requirement.section_id
                group_key = (week_start, requirement.shift_id, section_id)
                category_assignments = assignment_groups.get(group_key, {"weekday": [], "weekend": [], "holiday": []})
                category_roster_assignments = roster_assignment_groups.get(group_key, {"weekday": [], "weekend": [], "holiday": []})
                all_week_assignments = [
                    assignment
                    for category_rows in category_roster_assignments.values()
                    for assignment in category_rows
                ]
                all_week_assignments.sort(key=lambda assignment: (
                    assignment.date,
                    assignment.staff.last_name.casefold(),
                    assignment.staff.first_name.casefold(),
                    assignment.staff_id,
                    assignment.pk,
                ))
                all_week_dates = []
                current_week_date = week_start
                while current_week_date <= week_end:
                    all_week_dates.append(current_week_date)
                    current_week_date += timedelta(days=1)
                weekly_teams_by_date = {
                    roster_date: frozenset(
                        assignment.staff_id
                        for assignment in all_week_assignments
                        if assignment.date == roster_date
                    )
                    for roster_date in all_week_dates
                }
                stored_weekly_team = weekly_team_map.get((week_start, requirement.shift_id, section_id))
                canonical_weekly_staff_ids = (
                    {staff.pk for staff in stored_weekly_team.members.all()}
                    if stored_weekly_team else set().union(*weekly_teams_by_date.values())
                )
                stored_weekly_staff_count = len(canonical_weekly_staff_ids)
                holiday_dates = set(ShiftAssignment.objects.filter(
                    shift__treatment_plant_id=plant_id,
                    date__range=(week_start, week_end),
                    day_type=ShiftAssignment.DAYTYPE_HOLIDAY,
                    is_deleted=False,
                ).values_list("date", flat=True))
                category_dates_by_type = {
                    ShiftAssignment.DAYTYPE_HOLIDAY: sorted(holiday_dates),
                    ShiftAssignment.DAYTYPE_WEEKDAY: [
                        roster_date for roster_date in all_week_dates
                        if roster_date.weekday() < 5 and roster_date not in holiday_dates
                    ],
                    ShiftAssignment.DAYTYPE_WEEKEND: [
                        roster_date for roster_date in all_week_dates if roster_date.weekday() >= 5
                    ],
                }
                category_crews = {}
                category_stable = True
                for category, category_dates in category_dates_by_type.items():
                    crews = {
                        frozenset(
                            assignment.staff_id
                            for assignment in all_week_assignments
                            if assignment.date == roster_date
                        )
                        for roster_date in category_dates
                    }
                    if len(crews) > 1:
                        category_stable = False
                    category_crews[category] = set(next(iter(crews))) if crews else set()
                category_union = set().union(*category_crews.values()) if category_crews else set()
                category_requirements_met = all(
                    (
                        len(category_crews[category]) == requirement.get_required_staff(category)
                        if category in {
                            ShiftAssignment.DAYTYPE_WEEKEND,
                            ShiftAssignment.DAYTYPE_HOLIDAY,
                        }
                        else requirement.get_required_staff(category)
                        <= len(category_crews[category])
                        <= requirement.maximum_allowed
                    )
                    for category, category_dates in category_dates_by_type.items()
                    if category_dates
                )
                weekly_team_consistent = bool(all_week_dates) and (
                    category_stable
                    and category_union == canonical_weekly_staff_ids
                    and category_requirements_met
                    and (stored_weekly_team is None or stored_weekly_team.is_reconciled)
                )
                if not stored_weekly_team and not stored_weekly_staff_count:
                    weekly_team_consistent = not any(weekly_teams_by_date.values())

                common_weekly_staff_ids = canonical_weekly_staff_ids
                category_slots = []
                for value, label in (
                    (ShiftAssignment.DAYTYPE_HOLIDAY, "Public holiday"),
                    (ShiftAssignment.DAYTYPE_WEEKDAY, "Weekdays"),
                    (ShiftAssignment.DAYTYPE_WEEKEND, "Saturday and Sunday"),
                ):
                    staffing_assignments = category_roster_assignments.get(value, [])
                    daily_counts = Counter(item.date for item in staffing_assignments)
                    category_assignment_by_staff = {}
                    for item in staffing_assignments:
                        category_assignment_by_staff.setdefault(item.staff_id, item)
                    category_dates = category_dates_by_type[value]
                    required_per_day = requirement.get_required_staff(value)
                    daily_statuses = [
                        requirement.get_staffing_status(daily_counts.get(day, 0), value)
                        for day in category_dates
                    ]
                    team_consistent = weekly_team_consistent
                    distinct_staff_count = len(category_assignment_by_staff)
                    distinct_assignments = []
                    daily_limit = requirement.maximum_allowed
                    category_staff_assignments = sorted(
                        category_assignment_by_staff.values(),
                        key=lambda assignment: (
                            assignment.staff.last_name.casefold(),
                            assignment.staff.first_name.casefold(),
                            assignment.staff_id,
                        ),
                    )
                    for existing_assignment in category_staff_assignments[:daily_limit]:
                        staff = existing_assignment.staff
                        distinct_assignments.append(SimpleNamespace(
                            pk=existing_assignment.pk,
                            staff=staff,
                            staff_id=staff.pk,
                            date=existing_assignment.date,
                            shift_id=requirement.shift_id,
                            attendance_status=existing_assignment.attendance_status,
                            remarks=existing_assignment.remarks,
                            has_assignment=True,
                        ))
                    can_add = bool(category_dates) and (
                        distinct_staff_count < requirement.maximum_allowed
                        or any(
                            daily_counts.get(roster_date, 0) < requirement.maximum_allowed
                            for roster_date in category_dates
                        )
                    )
                    covered_days = sum(status == "optimal" for status in daily_statuses)
                    if not category_dates:
                        staffing_state = "unplanned"
                    elif "overstaffed" in daily_statuses:
                        staffing_state = "overstaffed"
                    elif not team_consistent:
                        staffing_state = "inconsistent"
                    elif not any(daily_counts.values()):
                        staffing_state = "empty"
                    elif covered_days < len(category_dates):
                        staffing_state = "understaffed"
                    else:
                        staffing_state = "optimal"
                    staffing_label = (
                        {
                            ShiftAssignment.DAYTYPE_HOLIDAY: "No public holiday dates are recorded in this week",
                            ShiftAssignment.DAYTYPE_WEEKDAY: "No weekdays are included in this roster week",
                            ShiftAssignment.DAYTYPE_WEEKEND: "No Saturday or Sunday dates are included in this roster week",
                        }[value]
                        if staffing_state == "unplanned"
                        else {
                            "empty": "No staff assigned",
                            "understaffed": "Below daily requirement",
                            "optimal": "Daily requirement met",
                            "overstaffed": (
                                "Minimum exceeded"
                                if value in {
                                    ShiftAssignment.DAYTYPE_WEEKEND,
                                    ShiftAssignment.DAYTYPE_HOLIDAY,
                                }
                                else "Maximum exceeded"
                            ),
                            "inconsistent": "Staff team differs across roster days",
                        }[staffing_state]
                    )
                    category_slots.append({
                        "value": value,
                        "label": label,
                        "assignments": distinct_assignments,
                        "assigned_count": distinct_staff_count,
                        "required_per_day": required_per_day,
                        "required_total": required_per_day * len(category_dates),
                        "minimum_required": requirement.minimum_required,
                        "preferred_required": requirement.preferred_required,
                        "maximum_allowed": requirement.maximum_allowed,
                        "daily_limit": daily_limit,
                        "can_add": can_add,
                        "team_consistent": team_consistent,
                        "stored_weekly_staff_count": stored_weekly_staff_count,
                        "display_limit_exceeded": distinct_staff_count > daily_limit,
                        "drop_label": (
                            "No public holiday dates are recorded in this week."
                            if value == ShiftAssignment.DAYTYPE_HOLIDAY and not category_dates
                            else "No Saturday or Sunday dates are included in this week."
                            if value == ShiftAssignment.DAYTYPE_WEEKEND and not category_dates
                            else "No weekdays are included in this week."
                            if not category_dates
                            else "The assigned team differs between dates. Regenerate the roster to restore the weekly group."
                            if not team_consistent
                            else "This category is at its shift-section staffing limit."
                        ),
                        "scheduled_days": len(category_dates),
                        "date_values": [day.isoformat() for day in category_dates],
                        "covered_days": covered_days,
                        "coverage_percent": round(covered_days * 100 / len(category_dates)) if category_dates else 0,
                        "staffing_state": staffing_state,
                        "staffing_label": staffing_label,
                    })

                weekly_assignments = []
                for category_rows in category_roster_assignments.values():
                    weekly_assignments.extend(category_rows)
                eligible_supervisor_ids = section_supervisor_user_ids_by_section.get(section_id, set())
                supervisor_candidate_by_user = {}
                for assignment in weekly_assignments:
                    if (
                        assignment.staff_id in eligible_supervisor_ids
                        and assignment.staff_id in common_weekly_staff_ids
                    ):
                        supervisor_candidate_by_user.setdefault(assignment.staff_id, assignment)
                supervisor_candidates = sorted(
                    supervisor_candidate_by_user.values(),
                    key=lambda assignment: (
                        assignment.staff.last_name.casefold(),
                        assignment.staff.first_name.casefold(),
                        assignment.staff_id,
                    ),
                )

                active_temporary_supervisors = {}
                for assignment in weekly_assignments:
                    if assignment.temp_supervisor_id and assignment.temp_supervisor_end and now >= assignment.temp_supervisor_end:
                        assignment.check_revert_temp_supervisor()
                    if (
                        assignment.temp_supervisor_id
                        and assignment.date == today
                        and assignment.temp_supervisor_start
                        and assignment.temp_supervisor_end
                        and assignment.temp_supervisor_start <= now <= assignment.temp_supervisor_end
                    ):
                        candidate = {
                            "user": assignment.temp_supervisor,
                            "start": assignment.temp_supervisor_start,
                            "end": assignment.temp_supervisor_end,
                        }
                        existing = active_temporary_supervisors.get(assignment.temp_supervisor_id)
                        candidate_start = candidate["start"].timestamp() if candidate["start"] else 0
                        existing_start = existing["start"].timestamp() if existing and existing["start"] else 0
                        if existing is None or candidate_start > existing_start:
                            active_temporary_supervisors[assignment.temp_supervisor_id] = candidate

                temporary_supervisor = max(
                    active_temporary_supervisors.values(),
                    key=lambda item: item["start"].timestamp() if item["start"] else 0,
                    default=None,
                )
                temporary_supervisor_assignment = next((
                    assignment
                    for assignment in weekly_assignments
                    if assignment.date == today
                    and assignment.attendance_status == ShiftAssignment.ATTENDANCE_PRESENT
                ), None)
                slot_supervisor = designated_supervisors.get((requirement.shift_id, section_id))
                weekly_designation = weekly_supervisor_map.get((requirement.shift_id, section_id, week_start))
                supervisor_is_rostered = bool(
                    slot_supervisor
                    and any(assignment.staff_id == slot_supervisor.pk for assignment in weekly_assignments)
                )
                weekly_designation_is_valid = bool(
                    weekly_designation
                    and (
                        (slot_supervisor and weekly_designation.user_id == slot_supervisor.pk)
                        or (weekly_designation.user_id in eligible_supervisor_ids)
                    )
                    and any(assignment.staff_id == weekly_designation.user_id for assignment in weekly_assignments)
                )
                designated_user = (
                    weekly_designation.user
                    if weekly_designation_is_valid
                    else slot_supervisor if not weekly_designation and supervisor_is_rostered else None
                )
                rebuild_needed = (
                    not weekly_team_consistent
                    or stored_weekly_staff_count > requirement.maximum_allowed
                    or any(
                        slot["scheduled_days"]
                        and slot["required_per_day"] > 0
                        and slot["staffing_state"] in {"empty", "understaffed", "overstaffed"}
                        for slot in category_slots
                    )
                )
                week["rows"].append({
                    "shift": requirement.shift,
                    "section": requirement.section,
                    "plant_id": plant_id,
                    "slots": category_assignments,
                    "category_slots": category_slots,
                    "weekly_team_consistent": weekly_team_consistent,
                    "weekly_team_rebuild_needed": rebuild_needed,
                    "shift_supervisor_id": slot_supervisor.pk if slot_supervisor else None,
                    "shift_supervisor": slot_supervisor,
                    "eligible_supervisor_ids": eligible_supervisor_ids,
                    "supervisor_candidates": supervisor_candidates,
                    "temporary_supervisor_assignment": temporary_supervisor_assignment,
                    "supervisor": temporary_supervisor["user"] if temporary_supervisor else designated_user,
                    "supervisor_is_temporary": bool(temporary_supervisor),
                    "supervisor_is_weekly": weekly_designation_is_valid,
                    "supervisor_is_rostered": supervisor_is_rostered,
                    "supervisor_temp_start": temporary_supervisor["start"] if temporary_supervisor else None,
                    "supervisor_temp_end": temporary_supervisor["end"] if temporary_supervisor else None,
                    "supervisor_conflict": len(active_temporary_supervisors) > 1,
                    "requirements": requirement,
                })
            week["leave"] = {
                (leave.staff_id, leave.status, leave.start_date, leave.end_date): leave
                for leave in [
                    item for item in leave_rows
                    if item.start_date <= week_end and item.end_date >= week_start
                ]
            }.values()
            week["row_count"] = len(week["rows"])

        query_url = self._query_url(request)
        previous_month = (month_start - timedelta(days=1)).replace(day=1)
        next_month = (month_end + timedelta(days=1)).replace(day=1)

        # Section-specific incharge user lookup
        User = get_user_model()
        incharge_user = None
        if selected_section:
            incharge_user = User.objects.filter(
                section_id=selected_section.pk,
                is_active=True,
                groups__name__in=INCHARGE_GROUPS,
            ).first()

        if selected_plant and selected_section:
            scope_description = f"{selected_plant.name} · {selected_section.name}"
        elif selected_plant:
            scope_description = selected_plant.name
        elif selected_section and selected_section.treatment_plant_id:
            scope_description = f"{selected_section.treatment_plant.name} · {selected_section.name}"
        elif selected_section:
            scope_description = f"All matching plants · {selected_section.name}"
        elif is_superuser:
            scope_description = "All treatment plants"
        else:
            scope_description = "Assigned treatment plant and section"
        all_category_slots = [
            slot
            for week in week_lookup.values()
            for row in week["rows"]
            for slot in row["category_slots"]
        ]
        scheduled_slots = [slot for slot in all_category_slots if slot["scheduled_days"]]
        covered_days = sum(slot["covered_days"] for slot in scheduled_slots)
        scheduled_days = sum(slot["scheduled_days"] for slot in scheduled_slots)
        context = {
            "app": {"name": "Shift Work Management", "label": "Shift Work Management"},
            "today": today,
            "selected_month": month,
            "selected_year": year,
            "month_label": month_start.strftime("%B %Y"),
            "month_choices": [(value, date(year, value, 1).strftime("%B")) for value in range(1, 13)],
            "year_choices": range(max(2000, year - 3), min(2100, year + 3) + 1),
            "plant_options": plant_options,
            "selected_plant": selected_plant,
            "show_plant_selector": is_superuser,
            "is_superuser": is_superuser,
            "sections": sections,
            "selected_section": selected_section,
            "incharge_user": incharge_user,
            "shift_options": scoped_shifts,
            "selected_shift": shift_filter,
            "attendance_choices": ShiftAssignment.ATTENDANCE_CHOICES,
            "day_type_choices": ShiftAssignment.DAYTYPE_CHOICES,
            "attendance_filter": attendance_filter,
            "day_type_filter": day_type_filter,
            "scope_issue": scope_issue,
            "weeks": list(week_lookup.values()),
            "total_assignments": total_assignments,
            "search_query": query,
            "can_add_roster": is_superuser or request.user.has_perm("sc.add_shiftassignment") or request.user.has_perm("sc.change_shiftassignment"),
            "can_edit_roster": is_superuser or request.user.has_perm("sc.change_shiftassignment"),
            "can_autopopulate_year": (
                (is_superuser or request.user.has_perm("sc.change_shiftassignment"))
                and not scope_issue
                and (is_superuser or bool(assigned_plant_id and assigned_section_id))
            ),
            "can_manage_availability": is_superuser or request.user.has_perm("sc.change_shiftassignment") or request.user.has_perm("sc.add_annualleave") or request.user.has_perm("sc.manage_staff_availability"),
            "can_manage_supervisors": is_superuser or any(request.user.has_perm(permission) for permission in (
                "sc.add_shiftsupervisor",
                "sc.change_shiftsupervisor",
            )),
            "can_manage_temporary_supervisors": is_superuser or request.user.has_perm("sc.change_shiftassignment") or any(
                request.user.has_perm(permission)
                for permission in ("sc.add_shiftsupervisor", "sc.change_shiftsupervisor")
            ),
            "scope_description": scope_description,
            "overall_coverage": round(covered_days * 100 / scheduled_days) if scheduled_days else 0,
            "understaffed_cells": sum(slot["staffing_state"] in {"empty", "understaffed"} for slot in scheduled_slots),
            "scheduled_staffing_days": scheduled_days,
            "previous_month_url": query_url(month=previous_month.month, year=previous_month.year),
            "next_month_url": query_url(month=next_month.month, year=next_month.year),
            "export_urls": {file_format: query_url(export=file_format) for file_format in ("csv", "xlsx", "pdf", "docx")},
            "table_row_count": sum(week["row_count"] for week in week_lookup.values()),
        }
        response = render(request, self.template_name, context)
        response["Cache-Control"] = "no-store, no-cache, max-age=0, must-revalidate"
        return response

    @staticmethod
    def _query_url(request):
        def build(**updates):
            params = request.GET.copy()
            params.pop("export", None)
            for key, value in updates.items():
                if value in (None, ""):
                    params.pop(key, None)
                else:
                    params[key] = str(value)
            encoded = params.urlencode()
            return f"?{encoded}" if encoded else "?"

        return build

    @staticmethod
    def _bounded_int(value, minimum, maximum, fallback):
        try:
            result = int(value)
        except (TypeError, ValueError):
            return fallback
        return result if minimum <= result <= maximum else fallback
