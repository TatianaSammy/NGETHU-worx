import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import django.utils.timezone


def migrate_filter_lifecycle(apps, schema_editor):
    WaterFilter = apps.get_model("t", "WaterFilter")
    FilterStatus = apps.get_model("t", "FilterStatus")
    FilterStatusEvent = apps.get_model("t", "FilterStatusEvent")
    database = schema_editor.connection.alias
    for water_filter in WaterFilter.objects.using(database).all().iterator():
        status_record = FilterStatus.objects.using(database).create(
            water_filter_id=water_filter.pk,
            installation_date=water_filter.installation_date,
            last_maintenance_date=water_filter.last_maintenance_date,
            status=water_filter.status,
            maintenance_interval_days=180,
        )
        FilterStatusEvent.objects.using(database).create(
            filter_status_id=status_record.pk,
            event_type="inspection",
            status=water_filter.status,
            remarks="Baseline filter status and lifecycle dates migrated from the previous WaterFilter record.",
        )


class Migration(migrations.Migration):
    dependencies = [
        ("t", "0002_filteredwaterlimit_water_filter"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FilterStatus",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("installation_date", models.DateField(blank=True, null=True)),
                ("last_maintenance_date", models.DateField(blank=True, null=True)),
                ("next_maintenance_date", models.DateField(blank=True, db_index=True, null=True)),
                ("maintenance_interval_days", models.PositiveSmallIntegerField(default=180, help_text="Planned maintenance interval used when no next-service date is set.", validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(3650)])),
                ("status", models.CharField(choices=[("working", "Working"), ("backwashing", "Backwashing"), ("isolated", "Isolated"), ("maintenance", "Maintenance"), ("fault", "Fault"), ("out_of_service", "Out of service")], db_index=True, default="working", max_length=24)),
                ("condition_percent", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)])),
                ("status_note", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("water_filter", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="filter_status", to="t.waterfilter")),
            ],
            options={
                "ordering": ["water_filter__name"],
                "indexes": [models.Index(fields=["status", "next_maintenance_date"], name="t_fstatus_due_idx")],
            },
        ),
        migrations.CreateModel(
            name="FilterStatusEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(choices=[("status_change", "Status change"), ("maintenance", "Maintenance update"), ("condition_update", "Condition update"), ("inspection", "Inspection"), ("backwash", "Backwash")], max_length=24)),
                ("status", models.CharField(choices=[("working", "Working"), ("backwashing", "Backwashing"), ("isolated", "Isolated"), ("maintenance", "Maintenance"), ("fault", "Fault"), ("out_of_service", "Out of service")], max_length=24)),
                ("condition_percent", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("remarks", models.TextField(blank=True)),
                ("recorded_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("filter_status", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="events", to="t.filterstatus")),
                ("performed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="filter_status_events", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-recorded_at", "-pk"],
                "indexes": [models.Index(fields=["filter_status", "-recorded_at"], name="t_fevent_status_time_idx")],
            },
        ),
        migrations.RenameField(
            model_name="waterfilter",
            old_name="design_capacity",
            new_name="capacity",
        ),
        migrations.AlterField(
            model_name="waterfilter",
            name="capacity",
            field=models.DecimalField(decimal_places=3, help_text="Rated filter throughput in m³/hr.", max_digits=12, validators=[django.core.validators.MinValueValidator(0.001)]),
        ),
        migrations.AlterField(
            model_name="waterfilter",
            name="flow_rate",
            field=models.FloatField(blank=True, help_text="Latest recorded flow in m³/hr.", null=True, validators=[django.core.validators.MinValueValidator(0)]),
        ),
        migrations.AlterField(
            model_name="waterfilter",
            name="location",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AlterField(
            model_name="waterfilter",
            name="filter_type",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="water_filters", to="t.filtertype"),
        ),
        migrations.AlterField(
            model_name="waterfilter",
            name="phase",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="filters", to="t.treatmentplantphase"),
        ),
        migrations.RunPython(migrate_filter_lifecycle, migrations.RunPython.noop),
        migrations.RemoveField(model_name="waterfilter", name="installation_date"),
        migrations.RemoveField(model_name="waterfilter", name="last_maintenance_date"),
        migrations.RemoveField(model_name="waterfilter", name="status"),
        migrations.RemoveField(model_name="waterfilter", name="depth"),
        migrations.RemoveField(model_name="waterfilter", name="length"),
        migrations.RemoveField(model_name="waterfilter", name="width"),
        migrations.RemoveField(model_name="waterfilter", name="volume"),
        migrations.AlterModelOptions(
            name="waterfilter",
            options={"ordering": ["treatment_plant__name", "phase__name", "name"]},
        ),
        migrations.AddIndex(
            model_name="waterfilter",
            index=models.Index(fields=["treatment_plant", "phase", "name"], name="t_filter_plant_phase_idx"),
        ),
    ]
