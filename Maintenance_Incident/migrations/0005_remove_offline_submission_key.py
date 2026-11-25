from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("Maintenance_Incident", "0004_incident_lifecycle_fields"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="maintenanceincident",
            name="offline_submission_key",
        ),
    ]
