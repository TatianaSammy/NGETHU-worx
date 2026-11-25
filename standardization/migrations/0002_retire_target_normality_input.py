from django.db import migrations


def remove_legacy_columns(apps, schema_editor):
    model = apps.get_model("standardization", "Standardization")
    table_name = model._meta.db_table
    quote = schema_editor.quote_name
    with schema_editor.connection.cursor() as cursor:
        columns = {
            column.name
            for column in schema_editor.connection.introspection.get_table_description(
                cursor, table_name
            )
        }

    for column_name in ("target_normality", "factor"):
        if column_name in columns:
            schema_editor.execute(
                f"ALTER TABLE {quote(table_name)} DROP COLUMN {quote(column_name)}"
            )


class Migration(migrations.Migration):

    dependencies = [
        ("standardization", "0001_initial"),
    ]

    operations = [
        # Older applied schemas may still have the original non-null columns,
        # even though the current initial migration state no longer includes them.
        migrations.RunPython(remove_legacy_columns),
    ]
