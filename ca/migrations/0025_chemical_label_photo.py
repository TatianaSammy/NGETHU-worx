from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ca", "0024_batchtestresult_inventory_equipment_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="chemicalanalysisrequest",
            name="assignment_due_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                help_text="Timestamp when this request became eligible for immediate analyst assignment.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="checklistforwatertreatmentchemicalanalysis",
            name="chemical_label_photo",
            field=models.ImageField(
                blank=True,
                help_text="Photo of the container label with the expiry date legible.",
                upload_to="ca/chemical-labels/%Y/%m/",
            ),
        ),
    ]
