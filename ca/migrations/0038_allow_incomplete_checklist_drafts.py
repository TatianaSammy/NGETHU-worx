from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ca", "0037_inspection_storage_conditions"),
    ]

    operations = [
        migrations.AlterField(
            model_name="checklistforwatertreatmentchemicalanalysis",
            name="manufacture_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="checklistforwatertreatmentchemicalanalysis",
            name="expiry_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="checklistforwatertreatmentchemicalanalysis",
            name="storage_conditions",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AlterField(
            model_name="checklistforwatertreatmentchemicalanalysis",
            name="net_weight",
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True),
        ),
    ]
