from django.db import migrations


def restore_analysis_input_values(apps, schema_editor):
    connection = schema_editor.connection
    AnalysisInput = apps.get_model("ca", "ChemicalAnalysisInput")
    table_name = AnalysisInput._meta.db_table
    field = AnalysisInput._meta.get_field("inputs")
    column_name = field.column

    with connection.cursor() as cursor:
        tables = set(connection.introspection.table_names(cursor))
        if table_name not in tables:
            return
        columns = {
            getattr(column, "name", None) or column[0]
            for column in connection.introspection.get_table_description(cursor, table_name)
        }
        if column_name not in columns:
            schema_editor.add_field(AnalysisInput, field)

        value_table = "ca_chemicalanalysisinputvalue"
        if value_table not in tables:
            return

        value_columns = {
            getattr(column, "name", None) or column[0]
            for column in connection.introspection.get_table_description(cursor, value_table)
        }
        required_columns = {"analysis_input_id", "name", "value"}
        if not required_columns.issubset(value_columns):
            raise RuntimeError(
                "The legacy chemical analysis input value table has an unexpected structure."
            )

        ordering = ["analysis_input_id"]
        if "position" in value_columns:
            ordering.append("position")
        if "id" in value_columns:
            ordering.append("id")
        q = connection.ops.quote_name
        cursor.execute(
            "SELECT analysis_input_id, name, value FROM %s ORDER BY %s"
            % (q(value_table), ", ".join(q(column) for column in ordering))
        )
        values_by_input = {}
        for input_id, name, value in cursor.fetchall():
            values_by_input.setdefault(input_id, {})[name] = value

        if values_by_input:
            placeholders = ", ".join(["%s"] * len(values_by_input))
            cursor.execute(
                f"SELECT {q(AnalysisInput._meta.pk.column)}, {q(column_name)} "
                f"FROM {q(table_name)} WHERE {q(AnalysisInput._meta.pk.column)} "
                f"IN ({placeholders})",
                list(values_by_input),
            )
            existing_by_id = dict(cursor.fetchall())
            for input_id, captured_values in values_by_input.items():
                existing = existing_by_id.get(input_id) or {}
                payload = {**existing, **captured_values}
                adapted = connection.ops.adapt_json_value(payload, field.encoder)
                cursor.execute(
                    "UPDATE %s SET %s = %%s WHERE %s = %%s"
                    % (q(table_name), q(column_name), q(AnalysisInput._meta.pk.column)),
                    [adapted, input_id],
                )

        schema_editor.execute("DROP TABLE %s" % q(value_table))


class Migration(migrations.Migration):
    dependencies = [
        ("ca", "0038_allow_incomplete_checklist_drafts"),
    ]

    operations = [
        migrations.RunPython(restore_analysis_input_values, migrations.RunPython.noop),
    ]
