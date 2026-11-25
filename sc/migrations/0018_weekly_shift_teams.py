from calendar import monthrange
from datetime import date, timedelta

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def create_weekly_team_records(apps, schema_editor):
    database = schema_editor.connection.alias
    ShiftAssignment = apps.get_model("sc", "ShiftAssignment")
    WeeklyShiftTeam = apps.get_model("sc", "WeeklyShiftTeam")
    WeeklyShiftTeamMember = apps.get_model("sc", "WeeklyShiftTeamMember")
    MonthlyDutyRoster = apps.get_model("sc", "MonthlyDutyRoster")
    AnnualLeave = apps.get_model("sc", "AnnualLeave")
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(app_label, model_name)
    incharge_group_names = (
        "CHEMIST OFFICER",
        "QUALITY ASSURANCE MANAGER",
        "QUALITY ASSURANCE CO-ORDINATOR",
        "PRODUCTION MANAGER",
        "TREATMENT WORKS CO-ORDINATOR",
        "TREATMENT WORKS OFFICER",
        "MAINTENANCE SUPERVISOR",
        "STORES SUPERVISOR",
        "WSP CO-ORDINATOR",
    )
    incharge_staff_ids = set(User.objects.using(database).filter(
        groups__name__in=incharge_group_names
    ).values_list("pk", flat=True).distinct())
    leave_ranges = {}
    for staff_id, start_date, end_date in AnnualLeave.objects.using(database).filter(
        status__in=("annual_leave", "off_duty", "in_charge")
    ).values_list("staff_id", "start_date", "end_date"):
        leave_ranges.setdefault(staff_id, []).append((start_date, end_date))

    grouped = {}
    rows = ShiftAssignment.objects.using(database).filter(is_deleted=False).values_list(
        "duty_roster_id",
        "shift_id",
        "staff__section_id",
        "date",
        "staff_id",
    ).iterator(chunk_size=2000)
    rosters = {}
    for roster_id, shift_id, section_id, assignment_date, staff_id in rows:
        if not section_id:
            continue
        roster = rosters.get(roster_id)
        if roster is None:
            roster = MonthlyDutyRoster.objects.using(database).only("year", "month").get(pk=roster_id)
            rosters[roster_id] = roster
        month_start = date(roster.year, roster.month, 1)
        month_end = date(roster.year, roster.month, monthrange(roster.year, roster.month)[1])
        monday = assignment_date - timedelta(days=assignment_date.weekday())
        week_start = max(monday, month_start)
        week_end = min(monday + timedelta(days=6), month_end)
        key = (roster_id, shift_id, section_id, week_start, week_end)
        teams = grouped.setdefault(key, {"all": {}, "eligible": {}})
        teams["all"].setdefault(assignment_date, set()).add(staff_id)
        unavailable = (
            staff_id in incharge_staff_ids
            or any(
                leave_start <= assignment_date <= leave_end
                for leave_start, leave_end in leave_ranges.get(staff_id, ())
            )
        )
        if not unavailable:
            teams["eligible"].setdefault(assignment_date, set()).add(staff_id)

    for (roster_id, shift_id, section_id, week_start, week_end), crew_sets in grouped.items():
        daily_crews = crew_sets["all"]
        eligible_crews = crew_sets["eligible"]
        member_ids = set().union(*eligible_crews.values()) if eligible_crews else set()
        cursor = week_start
        reconciled = True
        while cursor <= week_end:
            if daily_crews.get(cursor, set()) != member_ids:
                reconciled = False
                break
            cursor += timedelta(days=1)
        team = WeeklyShiftTeam.objects.using(database).create(
            duty_roster_id=roster_id,
            shift_id=shift_id,
            section_id=section_id,
            week_start=week_start,
            week_end=week_end,
            is_reconciled=reconciled,
        )
        WeeklyShiftTeamMember.objects.using(database).bulk_create([
            WeeklyShiftTeamMember(team_id=team.pk, staff_id=staff_id)
            for staff_id in sorted(member_ids)
        ])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_group_dashboard_redirect"),
        ("sc", "0017_in_charge_availability"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WeeklyShiftTeam",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("week_start", models.DateField()),
                ("week_end", models.DateField()),
                ("is_reconciled", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("duty_roster", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="weekly_teams", to="sc.monthlydutyroster")),
                ("section", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="weekly_shift_teams", to="core.section")),
                ("shift", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="weekly_teams", to="sc.shift")),
            ],
            options={"ordering": ("week_start", "shift__start_time", "section__name")},
        ),
        migrations.CreateModel(
            name="WeeklyShiftTeamMember",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("staff", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="weekly_shift_team_memberships", to=settings.AUTH_USER_MODEL)),
                ("team", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="memberships", to="sc.weeklyshiftteam")),
            ],
        ),
        migrations.AddField(
            model_name="weeklyshiftteam",
            name="members",
            field=models.ManyToManyField(related_name="weekly_shift_teams", through="sc.WeeklyShiftTeamMember", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddConstraint(
            model_name="weeklyshiftteam",
            constraint=models.UniqueConstraint(fields=("duty_roster", "shift", "section", "week_start"), name="unique_weekly_shift_team_scope"),
        ),
        migrations.AddConstraint(
            model_name="weeklyshiftteam",
            constraint=models.CheckConstraint(condition=models.Q(("week_end__gte", models.F("week_start"))), name="weekly_shift_team_valid_date_range"),
        ),
        migrations.AddConstraint(
            model_name="weeklyshiftteammember",
            constraint=models.UniqueConstraint(fields=("team", "staff"), name="unique_weekly_shift_team_member"),
        ),
        migrations.RunPython(create_weekly_team_records, migrations.RunPython.noop),
    ]
