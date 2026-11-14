from django.db import migrations, models
from django.db.models import Count


def normalize_supervisor_assignments(apps, schema_editor):
    alias = schema_editor.connection.alias
    ShiftSupervisor = apps.get_model("sc", "ShiftSupervisor")
    WeeklyShiftSupervisorAssignment = apps.get_model("sc", "WeeklyShiftSupervisorAssignment")

    active_rows = ShiftSupervisor.objects.using(alias).filter(is_active=True).order_by("user_id", "pk")
    seen_users = set()
    for row in active_rows.values("pk", "user_id").iterator():
        if row["user_id"] in seen_users:
            ShiftSupervisor.objects.using(alias).filter(pk=row["pk"]).update(is_active=False)
        else:
            seen_users.add(row["user_id"])

    weekly_conflicts = WeeklyShiftSupervisorAssignment.objects.using(alias).values(
        "user_id", "week_start"
    ).annotate(row_count=Count("pk")).filter(row_count__gt=1)
    if weekly_conflicts.exists():
        raise RuntimeError(
            "Resolve weekly supervisor assignments where one user has multiple shifts in the same roster week before applying this migration."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("sc", "0012_weekly_shift_supervisor_assignment"),
    ]

    operations = [
        migrations.RunPython(normalize_supervisor_assignments, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="shiftsupervisor",
            constraint=models.UniqueConstraint(
                condition=models.Q(is_active=True),
                fields=("user",),
                name="sc_active_sup_one_shift",
            ),
        ),
        migrations.AddConstraint(
            model_name="weeklyshiftsupervisorassignment",
            constraint=models.UniqueConstraint(
                fields=("user", "week_start"),
                name="sc_week_sup_one_shift_week",
            ),
        ),
    ]
