from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ca", "0033_plant_inspection_ip_restriction_toggle"),
    ]

    operations = [
        migrations.AddField(
            model_name="chemicalbatch",
            name="delivery_note_attachment",
            field=models.FileField(
                blank=True,
                upload_to="chemical_batches/delivery_notes/%Y/%m/",
            ),
        ),
        migrations.AddField(
            model_name="chemicalbatch",
            name="manufacturer_certificate_of_analysis",
            field=models.FileField(
                blank=True,
                upload_to="chemical_batches/manufacturer_certificates/%Y/%m/",
            ),
        ),
    ]
