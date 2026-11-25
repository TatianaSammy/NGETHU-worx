from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sc", "0016_update_shiftsupervisor_ordering"),
    ]

    operations = [
        migrations.AlterField(
            model_name="annualleave",
            name="status",
            field=models.CharField(
                choices=[
                    ("annual_leave", "Annual leave"),
                    ("off_duty", "Off duty"),
                    ("in_charge", "In charge"),
                ],
                db_index=True,
                default="annual_leave",
                max_length=16,
            ),
        ),
        migrations.AlterModelOptions(
            name="annualleave",
            options={
                "ordering": ["start_date", "staff__last_name", "staff__first_name"],
                "permissions": [("manage_staff_availability", "Can manage staff availability periods")],
                "verbose_name": "Staff availability",
                "verbose_name_plural": "Staff availability",
            },
        ),
    ]
