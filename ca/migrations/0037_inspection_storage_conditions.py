from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ca", "0036_committee_inspection_package_details"),
    ]

    operations = [
        migrations.AddField(
            model_name="chemicalbatchinspection",
            name="observed_storage_conditions",
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
