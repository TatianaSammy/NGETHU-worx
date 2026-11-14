import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import sc.models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_group_dashboard_redirect"),
        ("sc", "0011_shift_section_supervisor_constraints"),
        ("t", "0003_filter_status_and_capacity"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WeeklyShiftSupervisorAssignment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("week_start", models.DateField()),
                ("week_end", models.DateField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("designated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_weekly_shift_supervisor_assignments", to=settings.AUTH_USER_MODEL)),
                ("section", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="weekly_shift_supervisors", to="core.section")),
                ("shift", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="weekly_supervisor_assignments", to="sc.shift")),
                ("treatment_plant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="weekly_shift_supervisors", to="t.treatmentplant")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="weekly_shift_supervisor_assignments", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ("week_start", "shift__start_time", "section__name"),
            },
            bases=(sc.models.AuditMixin, models.Model),
        ),
        migrations.AddConstraint(
            model_name="weeklyshiftsupervisorassignment",
            constraint=models.UniqueConstraint(fields=("shift", "section", "week_start"), name="sc_unique_weekly_shift_section_supervisor"),
        ),
        migrations.AddConstraint(
            model_name="weeklyshiftsupervisorassignment",
            constraint=models.CheckConstraint(condition=models.Q(week_end__gte=models.F("week_start")), name="sc_weekly_supervisor_valid_dates"),
        ),
        migrations.AddIndex(
            model_name="weeklyshiftsupervisorassignment",
            index=models.Index(fields=["treatment_plant", "week_start", "shift", "section"], name="sc_week_sup_scope_week_idx"),
        ),
        migrations.AddIndex(
            model_name="weeklyshiftsupervisorassignment",
            index=models.Index(fields=["user", "week_start", "week_end"], name="sc_week_sup_user_week_idx"),
        ),
    ]
