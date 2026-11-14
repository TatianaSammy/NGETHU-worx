from datetime import date, time, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, RequestFactory

from core.models import Section
from sc.models import (
    AnnualLeave,
    MonthlyDutyRoster,
    Shift,
    ShiftAssignment,
    ShiftSectionRequirement,
    ShiftSupervisor,
)
from t.models import TreatmentPlant
from weeklytasks.models import (
    DailyTaskAssignment,
    DutyWeek,
    MonthlyDutySchedule,
    StandardWeeklyTask,
    WeeklyDutyTask,
)
from weeklytasks.ViewDashboard import DashboardView

User = get_user_model()


class WeeklyTasksModelArchitectureTests(TestCase):
    def setUp(self):
        self.plant = TreatmentPlant.objects.create(name="Central Plant", code="CP")
        self.section = Section.objects.create(name="Quality Lab", treatment_plant=self.plant, is_active=True)

        self.user = User.objects.create_user(
            "NCWSC20001",
            "labtech1@nairobiwater.co.ke",
            password="TestPassword123!",
            first_name="Alice",
            last_name="Wong",
            assigned_treatment_plant=self.plant,
            section=self.section,
            is_active=True,
        )

        self.shift = Shift.objects.create(
            name="Day Shift",
            treatment_plant=self.plant,
            start_time=time(8, 0),
            end_time=time(16, 0),
        )
        ShiftSectionRequirement.objects.create(
            shift=self.shift,
            section=self.section,
            weekday_required=1,
            maximum_allowed=5,
        )

        self.template = StandardWeeklyTask.objects.create(
            name="Turbidity Analysis",
            code="TURB-01",
            is_active=True,
        )

        self.test_date = date(2026, 11, 10)
        self.schedule = MonthlyDutySchedule.objects.create(
            month=self.test_date.month,
            year=self.test_date.year,
            treatment_plant=self.plant,
            section=self.section,
        )
        self.week = DutyWeek.objects.filter(
            schedule=self.schedule,
            start_date=date(2026, 11, 9),
        ).first()
        if not self.week:
            self.week = DutyWeek.objects.create(
                schedule=self.schedule,
                start_date=date(2026, 11, 9),
                end_date=date(2026, 11, 15),
            )
        self.task = WeeklyDutyTask.objects.create(
            week=self.week,
            task_template=self.template,
            section=self.section,
            treatment_plant=self.plant,
        )

        self.roster, _ = MonthlyDutyRoster.objects.get_or_create(
            treatment_plant=self.plant,
            month=self.test_date.month,
            year=self.test_date.year,
        )

    def test_task_can_assign_user_with_rostered_shift(self):
        # Without rostered shift in sc
        can_assign, reason = self.task.can_assign_user(self.user, self.test_date, shift=self.shift)
        self.assertFalse(can_assign)
        self.assertIn("not rostered", reason)

        # Create shift assignment in sc
        ShiftAssignment.create_assignment(
            staff=self.user,
            shift=self.shift,
            date=self.test_date,
            duty_roster=self.roster,
        )

        # Now can_assign_user should succeed
        can_assign, reason = self.task.can_assign_user(self.user, self.test_date, shift=self.shift)
        self.assertTrue(can_assign)
        self.assertIsNone(reason)

    def test_task_create_and_move_assignment(self):
        ShiftAssignment.create_assignment(
            staff=self.user,
            shift=self.shift,
            date=self.test_date,
            duty_roster=self.roster,
        )

        # Create task assignment
        assignment = DailyTaskAssignment.create_assignment(
            task=self.task,
            user=self.user,
            date=self.test_date,
            shift=self.shift,
            remarks="Routine water sampling",
        )
        self.assertIsNotNone(assignment.pk)
        self.assertEqual(assignment.shift, self.shift)

        # Move assignment to next day (with sc roster on next day)
        next_day = self.test_date + timedelta(days=1)
        ShiftAssignment.create_assignment(
            staff=self.user,
            shift=self.shift,
            date=next_day,
            duty_roster=self.roster,
        )

        assignment.move_assignment(
            target_task=self.task,
            target_date=next_day,
            shift=self.shift,
            remarks="Moved to Wednesday",
        )
        self.assertEqual(assignment.date, next_day)
        self.assertIn("Moved to Wednesday", assignment.remarks)

    def test_leave_blocks_task_assignment(self):
        AnnualLeave.objects.create(
            staff=self.user,
            treatment_plant=self.plant,
            status=AnnualLeave.STATUS_APPROVED,
            start_date=self.test_date,
            end_date=self.test_date + timedelta(days=2),
        )

        can_assign, reason = self.task.can_assign_user(self.user, self.test_date)
        self.assertFalse(can_assign)
        self.assertIn("annual leave", reason)

    def test_weeklytasks_dashboard_view(self):
        self.client.force_login(self.user)
        response = self.client.get("/weeklytasks/?month=11&year=2026")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Weekly Tasks", response.content.decode())
