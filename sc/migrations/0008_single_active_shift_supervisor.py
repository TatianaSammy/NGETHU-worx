from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sc", "0007_remove_shiftassignment_shift_date_unique"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="shiftsupervisor",
            name="sc_one_active_supervisor_per_section",
        ),
        migrations.RemoveConstraint(
            model_name="shiftsupervisor",
            name="sc_one_active_plant_supervisor",
        ),
        migrations.AlterField(
            model_name="shiftsupervisor",
            name="is_active",
            field=models.BooleanField(
                db_index=True,
                default=True,
                help_text="Only one active supervisor is allowed for each shift and section.",
            ),
        ),
    ]
