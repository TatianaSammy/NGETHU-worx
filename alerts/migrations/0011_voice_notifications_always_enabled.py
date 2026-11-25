from django.db import migrations, models


def enable_voice_notifications(apps, schema_editor):
    AlertTemplate = apps.get_model("alerts", "AlertTemplate")
    AlertTemplate.objects.using(schema_editor.connection.alias).update(
        auto_voice_enabled=True
    )


class Migration(migrations.Migration):
    dependencies = [
        ("alerts", "0010_remove_voicealert_voice_over_and_more"),
    ]

    operations = [
        migrations.RunPython(
            enable_voice_notifications,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="alerttemplate",
            name="auto_voice_enabled",
            field=models.BooleanField(default=True, editable=False),
        ),
    ]
