from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("ca", "0031_batch_package_quantity_details"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="chemicalinspectioncommitteemember",
            options={
                "ordering": (
                    "treatment_plant__name",
                    "role",
                    "user__last_name",
                    "user__first_name",
                    "user__username",
                    "pk",
                ),
            },
        ),
    ]
