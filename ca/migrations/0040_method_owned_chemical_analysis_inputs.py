import django.db.models.deletion
from django.db import migrations, models


def _definitions(structure):
    if isinstance(structure, dict):
        definitions = [
            {"name": name, **(value if isinstance(value, dict) else {"label": value})}
            for name, value in structure.items()
        ]
    elif isinstance(structure, str):
        definitions = [{"name": name.strip()} for name in structure.split(",") if name.strip()]
    elif isinstance(structure, (list, tuple)):
        definitions = [
            {"name": item, "required": True, "type": "number"}
            if isinstance(item, str)
            else dict(item)
            for item in structure
            if isinstance(item, (str, dict)) and (not isinstance(item, dict) or item.get("name"))
        ]
    else:
        definitions = []

    result = []
    seen = set()
    for item in definitions:
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append({**item, "name": name})
    return result


def move_inputs_to_method_and_result(apps, schema_editor):
    database = schema_editor.connection.alias
    OldInput = apps.get_model("ca", "ChemicalAnalysisInputRecord")
    InputDefinition = apps.get_model("ca", "ChemicalAnalysisInput")
    BatchTestResult = apps.get_model("ca", "BatchTestResult")
    ChemicalAnalysisMethod = apps.get_model("analysis_methods", "ChemicalAnalysisMethod")

    for record in OldInput.objects.using(database).iterator(chunk_size=500):
        if record.test_result_id:
            BatchTestResult.objects.using(database).filter(pk=record.test_result_id).update(
                method_inputs=record.inputs or {}
            )

    definitions_by_method = {}
    for method in ChemicalAnalysisMethod.objects.using(database).only("pk", "inputs_structure").iterator(chunk_size=500):
        rows = []
        for position, definition in enumerate(_definitions(method.inputs_structure)):
            name = definition["name"]
            rows.append(InputDefinition(
                method_id=method.pk,
                name=name,
                label=definition.get("label") or name.replace("_", " ").title(),
                input_type=definition.get("type") or "number",
                unit=definition.get("unit") or "",
                help_text=definition.get("help_text") or "",
                required=definition.get("required", True),
                min_value=definition.get("min_value"),
                max_value=definition.get("max_value"),
                choices=definition.get("choices") or [],
                position=position,
            ))
        if rows:
            definitions_by_method[method.pk] = rows

    for rows in definitions_by_method.values():
        InputDefinition.objects.using(database).bulk_create(rows, batch_size=500)


def restore_result_inputs(apps, schema_editor):
    database = schema_editor.connection.alias
    OldInput = apps.get_model("ca", "ChemicalAnalysisInputRecord")
    BatchTestResult = apps.get_model("ca", "BatchTestResult")
    for result in BatchTestResult.objects.using(database).exclude(method_inputs={}).iterator(chunk_size=500):
        OldInput.objects.using(database).update_or_create(
            test_result_id=result.pk,
            defaults={
                "inputs": result.method_inputs or {},
                "calculated_value": result.value,
                "method_snapshot": result.method_snapshot or {},
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("analysis_methods", "0003_chemical_method_rounding_mode"),
        ("ca", "0039_restore_chemicalanalysisinput_inputs"),
    ]

    operations = [
        migrations.AddField(
            model_name="batchtestresult",
            name="method_inputs",
            field=models.JSONField(blank=True, default=dict, editable=False),
        ),
        migrations.RenameModel(
            old_name="ChemicalAnalysisInput",
            new_name="ChemicalAnalysisInputRecord",
        ),
        migrations.CreateModel(
            name="ChemicalAnalysisInput",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100)),
                ("label", models.CharField(blank=True, max_length=180)),
                ("input_type", models.CharField(choices=[("number", "Number"), ("decimal", "Decimal"), ("float", "Float"), ("integer", "Integer"), ("int", "Integer"), ("boolean", "Boolean"), ("bool", "Boolean"), ("choice", "Choice"), ("select", "Select"), ("text", "Text"), ("string", "String")], default="number", max_length=20)),
                ("unit", models.CharField(blank=True, max_length=50)),
                ("help_text", models.TextField(blank=True)),
                ("required", models.BooleanField(default=True)),
                ("min_value", models.DecimalField(blank=True, decimal_places=12, max_digits=24, null=True)),
                ("max_value", models.DecimalField(blank=True, decimal_places=12, max_digits=24, null=True)),
                ("choices", models.JSONField(blank=True, default=list)),
                ("position", models.PositiveIntegerField(default=0)),
                ("method", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="analysis_inputs", to="analysis_methods.chemicalanalysismethod")),
            ],
            options={
                "ordering": ("method__method", "position", "pk"),
                "constraints": [models.UniqueConstraint(fields=("method", "name"), name="ca_method_input_name_uniq")],
            },
        ),
        migrations.RunPython(move_inputs_to_method_and_result, restore_result_inputs),
        migrations.DeleteModel(name="ChemicalAnalysisInputRecord"),
    ]
