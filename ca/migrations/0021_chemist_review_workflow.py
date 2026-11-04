from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ca", "0020_backfill_ca_voice_reminders"),
    ]

    operations = [
        migrations.AlterField(
            model_name="chemicalcorrectiveaction",
            name="status",
            field=models.CharField(
                choices=[
                    ("OPEN", "Awaiting independent chemist verification"),
                    ("VERIFIED", "Verified"),
                    ("REJECTED", "Rejected"),
                ],
                db_index=True,
                default="OPEN",
                max_length=16,
            ),
        ),
    ]
