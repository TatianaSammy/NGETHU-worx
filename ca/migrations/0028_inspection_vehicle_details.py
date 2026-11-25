from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ca", "0027_inspection_committee_presence"),
    ]

    operations = [
        migrations.AddField(
            model_name="chemicalbatchinspection",
            name="vehicle_registration",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="chemicalbatchinspection",
            name="vehicle_seal_number",
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
