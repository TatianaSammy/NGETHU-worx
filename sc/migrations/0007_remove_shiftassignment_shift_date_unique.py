from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("sc", "0006_single_active_shift_supervisor"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="shiftassignment",
            unique_together=set(),
        ),
    ]
