import json
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
from sc.ViewDashboard import DashboardView
from t.models import TreatmentPlant

User = get_user_model()


class ShiftModelArchitectureTests(TestCase):
    def setUp(self):
        self.plant = TreatmentPlant.objects.create(name="Central Plant", code="CP")
        self.section = Section.objects.create(name="Operations", treatment_plant=self.plant, is_active=True)

        self.user = User.objects.create_user(
            "NCWSC10001",
            "operator1@nairobiwater.co.ke",
            password="TestPassword123!",
            first_name="John",
            last_name="Doe",
            assigned_treatment_plant=self.plant,
            section=self.section,
            is_active=True,
        )
        self.supervisor_user = User.objects.create_user(
            "NCWSC10002",
            "supervisor1@nairobiwater.co.ke",
            password="TestPassword123!",
            first_name="Jane",
            last_name="Smith",
            assigned_treatment_plant=self.plant,
            section=self.section,
            is_active=True,
        )

        self.morning_shift = Shift.objects.create(
            name="Morning Shift",
            treatment_plant=self.plant,
            start_time=time(8, 0),
            end_time=time(16, 0),
        )
        self.night_shift = Shift.objects.create(
            name="Night Shift",
            treatment_plant=self.plant,
            start_time=time(22, 0),
            end_time=time(6, 0),
        )

        self.requirement = ShiftSectionRequirement.objects.create(
            shift=self.morning_shift,
            section=self.section,
            weekday_required=2,
            weekend_required=1,
            holiday_required=1,
            minimum_required=1,
            maximum_allowed=5,
        )
        self.night_requirement = ShiftSectionRequirement.objects.create(
            shift=self.night_shift,
            section=self.section,
            weekday_required=1,
            weekend_required=1,
            holiday_required=1,
            minimum_required=1,
            maximum_allowed=3,
        )

        self.supervisor = ShiftSupervisor.objects.create(
            treatment_plant=self.plant,
            section=self.section,
            shift=self.morning_shift,
            user=self.supervisor_user,
            is_active=True,
        )

        self.test_date = date(2026, 11, 10)  # A Tuesday
        self.roster, _ = MonthlyDutyRoster.objects.get_or_create(
            treatment_plant=self.plant,
            month=self.test_date.month,
            year=self.test_date.year,
        )

    def test_shift_window_and_overlap(self):
        start_dt, end_dt = self.morning_shift.get_window(self.test_date)
        self.assertEqual(start_dt.time(), time(8, 0))
        self.assertEqual(end_dt.time(), time(16, 0))
        self.assertFalse(self.morning_shift.is_overnight)

        night_start, night_end = self.night_shift.get_window(self.test_date)
        self.assertTrue(self.night_shift.is_overnight)
        self.assertEqual(night_end.date(), self.test_date + timedelta(days=1))

        self.assertFalse(self.morning_shift.overlaps_with(self.test_date, self.night_shift, self.test_date))

    def test_single_shift_per_day_gating(self):
        # Assign operator to morning shift
        assignment = ShiftAssignment.create_assignment(
            staff=self.user,
            shift=self.morning_shift,
            date=self.test_date,
            duty_roster=self.roster,
            supervisor=self.supervisor_user,
        )
        self.assertIsNotNone(assignment.pk)

        # Checking conflict for another shift on same day must report conflict
        has_conflict, reason = self.night_shift.check_staff_conflict(self.user.pk, self.test_date)
        self.assertTrue(has_conflict)
        self.assertIn("cannot work across shifts", reason)

        # Attempting to create duplicate same-day assignment must raise ValidationError
        with self.assertRaises(ValidationError):
            ShiftAssignment.create_assignment(
                staff=self.user,
                shift=self.night_shift,
                date=self.test_date,
                duty_roster=self.roster,
            )

    def test_overnight_shift_yesterday_blocks_today(self):
        yesterday = self.test_date - timedelta(days=1)
        yesterday_roster, _ = MonthlyDutyRoster.objects.get_or_create(
            treatment_plant=self.plant,
            month=yesterday.month,
            year=yesterday.year,
        )
        ShiftAssignment.create_assignment(
            staff=self.user,
            shift=self.night_shift,
            date=yesterday,
            duty_roster=yesterday_roster,
            supervisor=self.supervisor_user,
        )

        has_conflict, reason = self.morning_shift.check_staff_conflict(self.user.pk, self.test_date)
        self.assertTrue(has_conflict)
        self.assertIn("overnight shift", reason)

    def test_leave_unavailability_gating(self):
        AnnualLeave.objects.create(
            staff=self.user,
            treatment_plant=self.plant,
            status=AnnualLeave.STATUS_ANNUAL_LEAVE,
            start_date=self.test_date,
            end_date=self.test_date + timedelta(days=3),
        )

        is_eligible, reason = self.morning_shift.is_staff_eligible(self.user, self.test_date)
        self.assertFalse(is_eligible)
        self.assertIn("annual leave", reason)

    def test_active_supervisor_resolution(self):
        active_sup = ShiftSupervisor.get_active_supervisor(self.plant.pk, self.section.pk)
        self.assertEqual(active_sup.pk, self.supervisor_user.pk)

        sup_map = ShiftSupervisor.get_supervisors_map([self.plant.pk], [self.section.pk])
        self.assertEqual(sup_map.get((self.plant.pk, self.section.pk)).pk, self.supervisor_user.pk)

    def test_move_assignment(self):
        target_date = self.test_date + timedelta(days=1)
        assignment = ShiftAssignment.create_assignment(
            staff=self.user,
            shift=self.morning_shift,
            date=self.test_date,
            duty_roster=self.roster,
        )

        assignment.move_assignment(
            target_shift=self.morning_shift,
            target_date=target_date,
            remarks="Moved to next day",
        )
        self.assertEqual(assignment.date, target_date)
        self.assertIn("Moved to next day", assignment.remarks)

    def test_dashboard_view_rendering(self):
        self.client.force_login(self.supervisor_user)
        response = self.client.get("/sc/?month=11&year=2026")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Shift Work Management", response.content.decode())
        self.assertIn("weeks", response.context)
        self.assertIn("table_row_count", response.context)

    def test_pool_supervisor_and_workload_balancing(self):
        from sc.services.supervisor_balance_service import (
            balance_supervisors_for_roster,
            get_supervisor_workload_summary,
        )

        # Create two more active pool supervisors in the same section
        user_b = User.objects.create_user(
            "NCWSC10003",
            "sup_b@nairobiwater.co.ke",
            password="TestPassword123!",
            first_name="Pool",
            last_name="Two",
            assigned_treatment_plant=self.plant,
            section=self.section,
            is_active=True,
        )
        user_c = User.objects.create_user(
            "NCWSC10004",
            "sup_c@nairobiwater.co.ke",
            password="TestPassword123!",
            first_name="Pool",
            last_name="Three",
            assigned_treatment_plant=self.plant,
            section=self.section,
            is_active=True,
        )

        ShiftSupervisor.objects.create(
            treatment_plant=self.plant,
            section=self.section,
            shift=None,
            user=user_b,
            is_active=True,
        )
        ShiftSupervisor.objects.create(
            treatment_plant=self.plant,
            section=self.section,
            shift=None,
            user=user_c,
            is_active=True,
        )

        res = balance_supervisors_for_roster(
            treatment_plant_id=self.plant.pk,
            section_id=self.section.pk,
            year=2026,
            month=11,
            overwrite_existing=True,
        )
        self.assertTrue(res["success"])
        self.assertGreater(res["slots_assigned"], 0)
        summary = res["summary"]
        self.assertEqual(summary["total_supervisors"], 3)
        self.assertLessEqual(summary["fairness_spread"], 1)

        # Test AJAX endpoint
        self.supervisor_user.is_superuser = True
        self.supervisor_user.save(update_fields=("is_superuser",))
        self.client.force_login(self.supervisor_user)
        post_res = self.client.post(
            "/sc/roster/assignment-action/",
            data=json.dumps({
                "action": "balance_supervisors",
                "plant_id": self.plant.pk,
                "section_id": self.section.pk,
                "year": 2026,
                "month": 11,
            }),
            content_type="application/json",
        )
        self.assertEqual(post_res.status_code, 200)
        self.assertTrue(post_res.json()["ok"])

