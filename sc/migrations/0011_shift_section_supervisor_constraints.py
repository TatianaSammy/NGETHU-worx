from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("sc", "0010_alter_shiftsupervisor_section_and_more"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="shiftsupervisor",
            constraint=models.UniqueConstraint(
                condition=models.Q(is_active=True),
                fields=("shift", "section"),
                name="sc_one_active_supervisor_per_shift_section",
            ),
        ),
        migrations.AddConstraint(
            model_name="shiftsupervisor",
            constraint=models.CheckConstraint(
                condition=models.Q(is_active=False) | models.Q(shift__isnull=False, section__isnull=False),
                name="sc_active_supervisor_requires_shift_section",
            ),
        ),
        migrations.AddIndex(
            model_name="shiftsupervisor",
            index=models.Index(fields=["shift", "section", "is_active"], name="sc_shiftsup_shift_sec_act"),
        ),
    ]
