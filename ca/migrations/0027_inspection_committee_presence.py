from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import ca.models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("ca", "0026_chemical_batch_inspection_workflow"),
        ("t", "0003_filter_status_and_capacity"),
    ]

    operations = [
        migrations.CreateModel(
            name="ChemicalInspectionPlantNetwork",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("lan_cidr", models.CharField(help_text="Plant inspection LAN in CIDR notation, for example 192.168.10.0/24.", max_length=64, validators=[ca.models.validate_inspection_lan_cidr])),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("treatment_plant", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="chemical_inspection_network", to="t.treatmentplant")),
            ],
        ),
        migrations.CreateModel(
            name="ChemicalInspectionCommitteeMember",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=150)),
                ("date_of_appointment", models.DateField()),
                ("role", models.CharField(choices=[("CHAIRPERSON", "Chairperson"), ("SECRETARY", "Secretary"), ("MEMBER", "Member")], default="MEMBER", max_length=16, verbose_name="Committee role")),
                ("appointment_letter", models.ImageField(upload_to="ca/inspection_committee_appointments/")),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("treatment_plant", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="chemical_inspection_committee_members", to="t.treatmentplant")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="chemical_inspection_appointments", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("treatment_plant__name", "role", "name", "pk")},
        ),
        migrations.AddConstraint(
            model_name="chemicalinspectioncommitteemember",
            constraint=models.UniqueConstraint(condition=models.Q(("is_active", True)), fields=("treatment_plant", "user"), name="ca_committee_active_user_uniq"),
        ),
        migrations.AddConstraint(
            model_name="chemicalinspectioncommitteemember",
            constraint=models.UniqueConstraint(condition=models.Q(("is_active", True), ("role", "CHAIRPERSON")), fields=("treatment_plant",), name="ca_committee_chair_uniq"),
        ),
        migrations.AddConstraint(
            model_name="chemicalinspectioncommitteemember",
            constraint=models.UniqueConstraint(condition=models.Q(("is_active", True), ("role", "SECRETARY")), fields=("treatment_plant",), name="ca_committee_secretary_uniq"),
        ),
        migrations.CreateModel(
            name="ChemicalInspectionAttendance",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("acknowledged_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("client_ip", models.GenericIPAddressField()),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="inspection_attendance", to="ca.chemicalbatch")),
                ("committee_member", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="batch_attendance", to="ca.chemicalinspectioncommitteemember")),
            ],
            options={"ordering": ("acknowledged_at", "pk")},
        ),
        migrations.AddConstraint(
            model_name="chemicalinspectionattendance",
            constraint=models.UniqueConstraint(fields=("batch", "committee_member"), name="ca_inspection_attendee_uniq"),
        ),
        migrations.AddIndex(
            model_name="chemicalinspectionattendance",
            index=models.Index(fields=["batch", "acknowledged_at"], name="ca_inspect_attendance_idx"),
        ),
    ]
