import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sc", "0003_alter_shiftsupervisor_options_and_more"),
        ("t", "0002_filteredwaterlimit_water_filter"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AnnualLeave",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("start_date", models.DateField(db_index=True)),
                ("end_date", models.DateField(db_index=True)),
                ("status", models.CharField(choices=[("annual_leave", "Annual leave"), ("off_duty", "Off duty")], db_index=True, default="annual_leave", max_length=16)),
                ("reason", models.CharField(blank=True, max_length=255)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_sc_annual_leaves", to=settings.AUTH_USER_MODEL)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="updated_sc_annual_leaves", to=settings.AUTH_USER_MODEL)),
                ("staff", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="annual_leave_records", to=settings.AUTH_USER_MODEL)),
                ("treatment_plant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="annual_leave_records", to="t.treatmentplant")),
            ],
            options={
                "verbose_name": "Staff availability",
                "verbose_name_plural": "Staff availability",
                "ordering": ["start_date", "staff__last_name", "staff__first_name"],
                "permissions": [("manage_staff_availability", "Can set staff annual leave and off-duty periods")],
            },
        ),
        migrations.AddConstraint(
            model_name="annualleave",
            constraint=models.CheckConstraint(condition=models.Q(("end_date__gte", models.F("start_date"))), name="sc_leave_end_after_start"),
        ),
        migrations.AddIndex(
            model_name="annualleave",
            index=models.Index(fields=["treatment_plant", "status", "start_date"], name="sc_leave_plant_status_idx"),
        ),
        migrations.AddIndex(
            model_name="annualleave",
            index=models.Index(fields=["staff", "status", "start_date", "end_date"], name="sc_leave_staff_period_idx"),
        ),
    ]
