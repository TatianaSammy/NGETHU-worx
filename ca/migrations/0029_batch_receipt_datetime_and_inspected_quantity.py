from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ca", "0028_inspection_vehicle_details"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="chemicalbatch",
            name="ca_batch_quantity_positive",
        ),
        migrations.AddField(
            model_name="chemicalbatch",
            name="received_at",
            field=models.DateTimeField(blank=True, db_index=True, editable=False, null=True),
        ),
        migrations.AlterField(
            model_name="chemicalbatch",
            name="quantity_received",
            field=models.DecimalField(
                blank=True,
                decimal_places=6,
                help_text="Set from verified package weight when the committee inspection passes.",
                max_digits=18,
                null=True,
            ),
        ),
        migrations.AddConstraint(
            model_name="chemicalbatch",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(quantity_received__isnull=True, status="RECEIVED")
                    | models.Q(quantity_received__gt=0)
                ),
                name="ca_batch_quantity_positive",
            ),
        ),
    ]
