import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("ca", "0034_chemicalbatch_receipt_documents"),
    ]

    operations = [
        migrations.AddField(
            model_name="chemicalbatch",
            name="supplier_collection_due_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="chemicalbatch",
            name="supplier_collected_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chemicalbatch",
            name="supplier_collection_recorded_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="recorded_supplier_collections",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
