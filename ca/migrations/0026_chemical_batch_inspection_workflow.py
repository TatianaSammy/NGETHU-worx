from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("ca", "0025_chemical_label_photo"),
    ]

    operations = [
        migrations.AddField(
            model_name="chemical",
            name="weight_tolerance",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("1.000"),
                help_text="Maximum absolute package-weight deviation allowed during receipt inspection, in the batch unit.",
                max_digits=12,
            ),
        ),
        migrations.CreateModel(
            name="ChemicalBatchInspection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("attempt", models.PositiveSmallIntegerField()),
                ("package_count", models.PositiveIntegerField(editable=False)),
                ("sample_count", models.PositiveIntegerField(editable=False)),
                ("sampling_interval", models.DecimalField(decimal_places=6, editable=False, max_digits=18)),
                ("random_start", models.PositiveIntegerField(editable=False)),
                ("declared_package_weight", models.DecimalField(decimal_places=6, max_digits=18)),
                ("weight_tolerance", models.DecimalField(decimal_places=3, editable=False, max_digits=12)),
                ("checklist_results", models.JSONField(blank=True, default=dict)),
                ("failed_criteria", models.JSONField(blank=True, default=list)),
                ("status", models.CharField(choices=[("PENDING", "Pending inspection"), ("PASS", "Passed"), ("FAIL", "Failed")], db_index=True, default="PENDING", max_length=12)),
                ("inspection_notes", models.TextField(blank=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("supply_chain_decision", models.CharField(blank=True, choices=[("REINSPECT", "Authorize a new inspection"), ("REJECT", "Reject and return to supplier")], max_length=12)),
                ("supply_chain_remarks", models.TextField(blank=True)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="inspections", to="ca.chemicalbatch")),
                ("completed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="completed_chemical_batch_inspections", to=settings.AUTH_USER_MODEL)),
                ("committee_members", models.ManyToManyField(blank=True, related_name="chemical_batch_inspections", to=settings.AUTH_USER_MODEL)),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reviewed_chemical_inspections", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("batch_id", "attempt")},
        ),
        migrations.CreateModel(
            name="ChemicalBatchInspectionSample",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("package_number", models.PositiveIntegerField()),
                ("declared_weight", models.DecimalField(decimal_places=6, max_digits=18)),
                ("measured_weight", models.DecimalField(blank=True, decimal_places=6, max_digits=18, null=True)),
                ("weight_difference", models.DecimalField(blank=True, decimal_places=6, editable=False, max_digits=18, null=True)),
                ("within_tolerance", models.BooleanField(blank=True, editable=False, null=True)),
                ("inspection", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="samples", to="ca.chemicalbatchinspection")),
            ],
            options={"ordering": ("package_number",)},
        ),
        migrations.AddConstraint(
            model_name="chemicalbatchinspection",
            constraint=models.UniqueConstraint(fields=("batch", "attempt"), name="ca_inspection_batch_attempt_uniq"),
        ),
        migrations.AddConstraint(
            model_name="chemicalbatchinspection",
            constraint=models.CheckConstraint(condition=models.Q(("attempt__gte", 1)), name="ca_inspection_attempt_positive"),
        ),
        migrations.AddConstraint(
            model_name="chemicalbatchinspection",
            constraint=models.CheckConstraint(condition=models.Q(("sample_count__gte", 1)), name="ca_inspection_sample_count_positive"),
        ),
        migrations.AddConstraint(
            model_name="chemicalbatchinspection",
            constraint=models.CheckConstraint(condition=models.Q(("sampling_interval__gte", 1)), name="ca_inspection_interval_positive"),
        ),
        migrations.AddConstraint(
            model_name="chemicalbatchinspection",
            constraint=models.CheckConstraint(condition=models.Q(("random_start__gte", 1)), name="ca_inspection_random_start_positive"),
        ),
        migrations.AddConstraint(
            model_name="chemicalbatchinspection",
            constraint=models.CheckConstraint(condition=models.Q(("random_start__lte", models.F("package_count"))), name="ca_inspection_random_start_within_batch"),
        ),
        migrations.AddConstraint(
            model_name="chemicalbatchinspectionsample",
            constraint=models.UniqueConstraint(fields=("inspection", "package_number"), name="ca_inspection_sample_package_uniq"),
        ),
        migrations.AddConstraint(
            model_name="chemicalbatchinspectionsample",
            constraint=models.CheckConstraint(condition=models.Q(("package_number__gte", 1)), name="ca_inspection_package_positive"),
        ),
        migrations.AddConstraint(
            model_name="chemicalbatchinspectionsample",
            constraint=models.CheckConstraint(condition=models.Q(("declared_weight__gt", 0)), name="ca_inspection_declared_weight_positive"),
        ),
        migrations.AddConstraint(
            model_name="chemicalbatchinspectionsample",
            constraint=models.CheckConstraint(condition=models.Q(("measured_weight__isnull", True), ("measured_weight__gt", 0), _connector="OR"), name="ca_inspection_measured_weight_positive"),
        ),
        migrations.AlterModelOptions(
            name="chemicalanalysisprocess",
            options={
                "ordering": ["-created_at"],
                "permissions": [
                    ("receive_chemical_batch", "Can receive chemical batches"),
                    ("create_analysis_request", "Can create chemical analysis requests"),
                    ("complete_receiving_checklist", "Can complete receiving checklists"),
                    ("start_chemical_analysis", "Can start chemical analysis"),
                    ("record_chemical_result", "Can record chemical test results"),
                    ("complete_chemical_analysis", "Can complete chemical analysis"),
                    ("perform_chemical_retest", "Can perform chemical retests"),
                    ("review_chemical_analysis", "Can review chemical analysis"),
                    ("verify_chemical_coa", "Can verify chemical CoA"),
                    ("approve_chemical_coa", "Can generate and approve chemical CoA"),
                    ("approve_chemical_batch", "Can approve chemical batches"),
                    ("reject_chemical_batch", "Can reject chemical batches"),
                    ("utilize_chemical_batch", "Can utilize chemical batches"),
                    ("return_chemical_batch", "Can return chemical batches"),
                    ("reopen_chemical_checklist", "Can reopen chemical checklists"),
                    ("resolve_chemical_batch_hold", "Can resolve chemical batch intake holds"),
                    ("view_chemical_workflow_event", "Can view chemical workflow audit events"),
                    ("record_chemical_custody", "Can record chemical batch custody transfers"),
                ],
            },
        ),
    ]
