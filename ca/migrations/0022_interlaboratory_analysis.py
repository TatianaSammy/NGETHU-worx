import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("ca", "0021_chemist_review_workflow"),
        ("instruments", "0001_initial"),
        ("laboratory", "0001_initial"),
        ("t", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ChemicalLabReferral",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("parameter_ids", models.JSONField(blank=True, default=list)),
                ("status", models.CharField(choices=[("PENDING", "Awaiting laboratory results"), ("COMPLETED", "Results submitted"), ("CANCELLED", "Cancelled")], db_index=True, default="PENDING", max_length=16)),
                ("remarks", models.TextField(blank=True)),
                ("requested_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("submission_remarks", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="interlaboratory_referrals", to="ca.chemicalbatch")),
                ("process", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="interlaboratory_referrals", to="ca.chemicalanalysisprocess")),
                ("requested_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_chemical_lab_referrals", to=settings.AUTH_USER_MODEL)),
                ("submitted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="submitted_chemical_lab_referrals", to=settings.AUTH_USER_MODEL)),
                ("treatment_plant", models.ForeignKey(help_text="The independent treatment-plant laboratory receiving this referral.", on_delete=django.db.models.deletion.PROTECT, related_name="received_chemical_lab_referrals", to="t.treatmentplant")),
            ],
            options={
                "ordering": ["status", "requested_at", "pk"],
            },
        ),
        migrations.CreateModel(
            name="ChemicalLabComparisonResult",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source", models.CharField(choices=[("ORIGINAL", "Originating analysis"), ("REASSIGNMENT", "Independent local repeat"), ("INTERLAB", "Independent laboratory")], db_index=True, max_length=16)),
                ("run_number", models.PositiveIntegerField(default=1)),
                ("value", models.DecimalField(blank=True, decimal_places=12, max_digits=26, null=True)),
                ("textual_result", models.CharField(blank=True, max_length=255)),
                ("status", models.CharField(choices=[("PASS", "Pass"), ("FAIL", "Fail"), ("NA", "Not applicable")], max_length=10)),
                ("remarks", models.TextField(blank=True)),
                ("recorded_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("analyst", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="chemical_lab_comparison_results", to=settings.AUTH_USER_MODEL)),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lab_comparison_results", to="ca.chemicalbatch")),
                ("instrument", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="chemical_lab_comparison_results", to="instruments.instrument")),
                ("parameter", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="laboratory_comparison_results", to="ca.chemicalspecification")),
                ("process", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lab_comparison_results", to="ca.chemicalanalysisprocess")),
                ("referral", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="results", to="ca.chemicallabreferral")),
                ("treatment_plant", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="chemical_lab_comparison_results", to="t.treatmentplant")),
            ],
            options={
                "ordering": ["parameter__parameter_name", "treatment_plant__name", "run_number", "pk"],
            },
        ),
        migrations.AddConstraint(
            model_name="chemicallabreferral",
            constraint=models.UniqueConstraint(fields=("process", "treatment_plant"), name="ca_interlab_process_plant_unique"),
        ),
        migrations.AddConstraint(
            model_name="chemicallabreferral",
            constraint=models.CheckConstraint(condition=models.Q(status__in=["PENDING", "COMPLETED", "CANCELLED"]), name="ca_interlab_referral_status_valid"),
        ),
        migrations.AddIndex(
            model_name="chemicallabreferral",
            index=models.Index(fields=["treatment_plant", "status", "requested_at"], name="ca_interlab_plant_status_idx"),
        ),
        migrations.AddIndex(
            model_name="chemicallabreferral",
            index=models.Index(fields=["batch", "status"], name="ca_interlab_batch_status_idx"),
        ),
        migrations.AddConstraint(
            model_name="chemicallabcomparisonresult",
            constraint=models.UniqueConstraint(fields=("batch", "treatment_plant", "parameter", "source", "run_number"), name="ca_lab_result_reading_unique"),
        ),
        migrations.AddConstraint(
            model_name="chemicallabcomparisonresult",
            constraint=models.CheckConstraint(condition=models.Q(status__in=["PASS", "FAIL", "NA"]), name="ca_lab_result_status_valid"),
        ),
        migrations.AddIndex(
            model_name="chemicallabcomparisonresult",
            index=models.Index(fields=["batch", "parameter", "source"], name="ca_lab_result_compare_idx"),
        ),
        migrations.AddIndex(
            model_name="chemicallabcomparisonresult",
            index=models.Index(fields=["treatment_plant", "recorded_at"], name="ca_lab_result_plant_time_idx"),
        ),
    ]
