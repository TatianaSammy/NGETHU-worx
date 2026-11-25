from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ca", "0035_chemicalbatch_supplier_collection_sla"),
    ]

    operations = [
        migrations.AddField(
            model_name="chemicalbatchinspection",
            name="manufacturer_batch_number",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="chemicalbatchinspection",
            name="manufacture_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chemicalbatchinspection",
            name="expiry_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chemicalbatchinspection",
            name="observed_package_count",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chemicalbatchinspection",
            name="expiry_label_attachment",
            field=models.FileField(
                blank=True,
                upload_to="chemical_batch_inspections/expiry_labels/%Y/%m/",
            ),
        ),
        migrations.AddField(
            model_name="chemicalbatchinspection",
            name="batch_number_label_attachment",
            field=models.FileField(
                blank=True,
                upload_to="chemical_batch_inspections/batch_labels/%Y/%m/",
            ),
        ),
    ]
