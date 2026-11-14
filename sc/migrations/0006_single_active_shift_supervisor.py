from django.db import migrations, models


def keep_one_active_supervisor(apps, schema_editor):
    ShiftSupervisor = apps.get_model("sc", "ShiftSupervisor")
    database = schema_editor.connection.alias
    seen_scopes = set()
    duplicate_ids = []
    records = ShiftSupervisor.objects.using(database).filter(is_active=True).order_by("-pk")
    for supervisor_id, plant_id, section_id in records.values_list("pk", "treatment_plant_id", "section_id"):
        scope = (plant_id, section_id)
        if scope in seen_scopes:
            duplicate_ids.append(supervisor_id)
        else:
            seen_scopes.add(scope)
    if duplicate_ids:
        ShiftSupervisor.objects.using(database).filter(pk__in=duplicate_ids).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [
        ("sc", "0005_shiftassignment_unique_active_staff_assignment_per_day"),
    ]

    operations = [
        migrations.RunPython(keep_one_active_supervisor, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="shiftsupervisor",
            constraint=models.UniqueConstraint(
                condition=models.Q(is_active=True, section__isnull=False),
                fields=("treatment_plant", "section"),
                name="sc_one_active_supervisor_per_section",
            ),
        ),
        migrations.AddConstraint(
            model_name="shiftsupervisor",
            constraint=models.UniqueConstraint(
                condition=models.Q(is_active=True, section__isnull=True),
                fields=("treatment_plant",),
                name="sc_one_active_plant_supervisor",
            ),
        ),
    ]
