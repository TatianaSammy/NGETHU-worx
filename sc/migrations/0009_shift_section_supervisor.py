from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("sc", "0008_single_active_shift_supervisor"),
    ]

    operations = [
        migrations.AddField(
            model_name="shiftsupervisor",
            name="shift",
            field=models.ForeignKey(
                blank=True,
                help_text="Shift covered by this supervisor designation.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="supervisor_designations",
                to="sc.shift",
            ),
        ),
        migrations.AlterUniqueTogether(
            name="shiftsupervisor",
            unique_together=set(),
        ),
        migrations.RunSQL(
            sql='DROP INDEX IF EXISTS "sc_only_one_active_shift_supervisor"',
            reverse_sql=migrations.RunSQL.noop,
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
