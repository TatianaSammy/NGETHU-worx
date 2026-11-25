from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("ca", "0029_batch_receipt_datetime_and_inspected_quantity"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="chemicalinspectioncommitteemember",
            name="name",
        ),
    ]
