# ca/models.py
from __future__ import annotations

import ast
import hashlib
import json
import logging
import math
import secrets
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, models, transaction
from django.db.models import Count, F, Max, OuterRef, Q, Subquery
from django.utils import timezone
from django_countries.fields import CountryField
from viewflow.workflow.models import Process

from CompanyInfo.models import CompanyInfo
from t.models import TreatmentPlant

from .events import batch_status_changed, checklist_completed, request_status_changed
from .services.formulas import (
    safe_eval_formula,
    validate_formula_expression,
)

from .services.policies import require_ca_actor
from .utils.coa_pdf import create_celestial_watermark

logger = logging.getLogger(__name__)

def today_date():
    return timezone.now().date()

def default_received_date():
    return timezone.now().date()
# ----------------------------
# Core Models
# ----------------------------
class Chemical(models.Model):
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=25, unique=True, help_text="Short code for the chemical, e.g. ALUM, CL, POLY")
    description = models.TextField(blank=True)
    grade = models.CharField(max_length=100, blank=True)
    hydrometer_formula = models.TextField(
        blank=True,
        help_text="Python formula using 'reading' as the input variable. Example: (reading * 1.05 + 2)"
    )

    def clean(self):
        super().clean()
        if self.hydrometer_formula:
            formula = self.hydrometer_formula.strip()
            validate_formula_expression(formula, allow_math_attr=True)
            tree = ast.parse(formula, mode="eval")
            unknown_names = sorted({
                node.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Name)
                and node.id not in {"reading", "math"}
            })
            if unknown_names:
                raise ValidationError(
                    "The hydrometer formula references undefined variable(s): "
                    + ", ".join(unknown_names)
                )

    def calculate_hydrometer_value(self, reading):
        if reading is None:
            raise ValidationError("A hydrometer reading is required.")
        try:
            value = float(reading)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Hydrometer reading must be numeric.") from exc
        if not math.isfinite(value):
            raise ValidationError("Hydrometer reading must be finite.")
        if not self.hydrometer_formula:
            return value
        formula = self.hydrometer_formula.strip()
        self.clean()
        result = safe_eval_formula(formula, {"reading": value, "math": math})
        if isinstance(result, bool):
            raise ValidationError("The hydrometer formula must return a numeric value.")
        try:
            result = float(result)
        except (TypeError, ValueError) as exc:
            raise ValidationError("The hydrometer formula must return a numeric value.") from exc
        if not math.isfinite(result):
            raise ValidationError("The hydrometer formula produced a non-finite result.")
        return result

    def apply_hydrometer_formula(self, reading):
        return self.calculate_hydrometer_value(reading)

    def __str__(self):
        return f"{self.code} - {self.name}"


def validate_contract_document(value):
    allowed_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".webp"}
    extension = value.name.lower().rsplit(".", 1)[-1] if "." in value.name else ""

    if f".{extension}" not in allowed_extensions:
        raise ValidationError(
            "Contract document must be a PDF or an image file "
            "(JPG, JPEG, PNG, or WEBP)."
        )


class Supplier(models.Model):
    name = models.CharField(max_length=100)
    contract_number = models.CharField(
        max_length=100,
        unique=True,
    )
    contract_document = models.FileField(
        upload_to="chemical/supplier_contracts/%Y/%m/",
        validators=[validate_contract_document],
        blank=True,
        null=True,
    )
    contact_info = models.TextField(blank=True)

    def __str__(self):
        return self.name

class ChemicalLocation(models.Model):
    name = models.CharField(
        max_length=100,
    )

    treatment_plant = models.ForeignKey(
        TreatmentPlant,
        on_delete=models.SET_NULL,
        null=True,
        related_name="chemical_locations",
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["name", "treatment_plant"],
                name="ca_unique_location_plant",
            ),
        ]
        indexes = [
            models.Index(
                fields=["treatment_plant"],
                name="ca_loc_plant_idx",
            ),
            models.Index(
                fields=["name"],
                name="ca_loc_name_idx",
            ),
        ]

    def __str__(self):
        return self.name


class ChemicalBatchSequence(models.Model):
    """Transactional sequence allocator for human-readable batch sample IDs."""

    prefix = models.CharField(max_length=160, unique=True)
    last_value = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(last_value__gte=0),
                name="ca_batch_sequence_nonnegative",
            ),
        ]




class ChemicalSpecification(models.Model):
    chemical = models.ForeignKey(Chemical, on_delete=models.CASCADE, related_name="specifications")
    parameter_name = models.CharField(max_length=100)
    unit = models.CharField(max_length=20)
    method = models.ForeignKey("analysis_methods.ChemicalAnalysisMethod", on_delete=models.SET_NULL,null=True )
    min_limit = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    max_limit = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    is_required = models.BooleanField(default=True)
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Inactive specifications are retained for audit but are not used by new analysis requests.",
    )
    requires_instrument = models.BooleanField(
        default=False,
        help_text="Require a plant instrument with a current calibration for this test.",
    )
    version = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    treatment_plant = models.ForeignKey(TreatmentPlant, on_delete=models.CASCADE, related_name="chemical_specifications")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["chemical", "parameter_name", "version", "treatment_plant"],
                nulls_distinct=False,
                name="ca_spec_version_plant_unique",
            ),
            models.UniqueConstraint(
                fields=["chemical", "parameter_name", "treatment_plant"],
                condition=Q(is_active=True),
                nulls_distinct=False,
                name="ca_one_active_spec_per_plant",
            ),
            models.CheckConstraint(
                condition=Q(min_limit__isnull=True) | Q(max_limit__isnull=True) | Q(min_limit__lte=F("max_limit")),
                name="ca_spec_min_lte_max",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1),
                name="ca_spec_version_positive",
            ),
        ]
        ordering = ['chemical__name', 'parameter_name']

    @classmethod
    def current_for(cls, queryset=None):
        queryset = queryset if queryset is not None else cls.objects.all()
        latest_version = cls.objects.filter(
            chemical_id=OuterRef("chemical_id"),
            treatment_plant_id=OuterRef("treatment_plant_id"),
            parameter_name=OuterRef("parameter_name"),
        ).order_by("-version").values("version")[:1]
        return queryset.filter(is_active=True).annotate(_current_version=Subquery(latest_version)).filter(
            version=F("_current_version")
        )

    @transaction.atomic
    def save(self, *args, **kwargs):
        if self.chemical_id:
            Chemical.objects.select_for_update().get(pk=self.chemical_id)
        previous_pk = self.pk
        superseding = False
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).first()
            if previous:
                versioned_fields = (
                    "chemical_id", "parameter_name", "unit", "method_id",
                    "min_limit", "max_limit", "is_required", "is_active", "requires_instrument",
                    "treatment_plant_id", "version",
                )
                superseding = any(getattr(previous, field) != getattr(self, field) for field in versioned_fields)
                if superseding:
                    type(self).objects.filter(pk=previous_pk).update(is_active=False)
        specification_versions = type(self).objects.select_for_update().filter(
            chemical_id=self.chemical_id,
            treatment_plant_id=self.treatment_plant_id,
            parameter_name=self.parameter_name,
        )
        # A superseding specification must be numbered after the version it
        # replaces. That old row remains in the database for audit history,
        # so excluding its pk here would reuse its version and fail the
        # (chemical, parameter, version, plant) uniqueness constraint.
        version_candidates = (
            specification_versions
            if superseding
            else specification_versions.exclude(pk=self.pk)
        )
        latest_version = version_candidates.aggregate(value=Max("version"))["value"] or 0
        if superseding:
            self.pk = None
            self._state.adding = True
            self.version = latest_version + 1
        elif self._state.adding and self.version <= latest_version:
            self.version = latest_version + 1
        self.full_clean()
        super().save(*args, **kwargs)
        if superseding and previous_pk:
            remarks = list(ChemicalSpecificationRemark.objects.filter(
                specification_id=previous_pk
            ).values("on_pass", "on_fail"))
            if remarks:
                ChemicalSpecificationRemark.objects.bulk_create([
                    ChemicalSpecificationRemark(specification=self, **remark)
                    for remark in remarks
                ])

    def __str__(self):
        return f"{self.chemical.name} - {self.parameter_name} (v{self.version})"

    def delete(self, *args, **kwargs):
        raise ValidationError("Chemical specification versions cannot be deleted.")


class ChemicalMethodQualityPlan(models.Model):
    """Plant scoped QC and interlaboratory decision settings for a method."""

    class ConsensusRule(models.TextChoices):
        EQUAL_LAB_MEAN = "EQUAL_LAB_MEAN", "Equal weight per laboratory"
        POOLED_READINGS = "POOLED_READINGS", "Pool all numerical readings"
        UNCERTAINTY_WEIGHTED = "UNCERTAINTY_WEIGHTED", "Inverse variance weighted laboratory means"

    method = models.ForeignKey(
        "analysis_methods.ChemicalAnalysisMethod",
        on_delete=models.PROTECT,
        related_name="ca_quality_plans",
    )
    treatment_plant = models.ForeignKey(
        "t.TreatmentPlant",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="chemical_method_quality_plans",
        help_text="Leave blank to provide a system-wide default for this method.",
    )
    version = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True, db_index=True)
    consensus_rule = models.CharField(max_length=24, choices=ConsensusRule.choices, default=ConsensusRule.EQUAL_LAB_MEAN)
    interlab_tolerance_absolute = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    interlab_tolerance_percent = models.DecimalField(max_digits=9, decimal_places=4, null=True, blank=True)
    uncertainty_coverage_factor = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal("2"))
    require_measurement_uncertainty = models.BooleanField(default=False)
    require_reference_standard_lot = models.BooleanField(default=False)
    require_reagent_lot_numbers = models.BooleanField(default=False)
    change_reason = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_ca_quality_plans")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["method__method", "treatment_plant__name", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["method", "treatment_plant", "version"], nulls_distinct=False, name="ca_quality_plan_version_unique"),
            models.CheckConstraint(condition=Q(version__gte=1), name="ca_quality_plan_version_positive"),
            models.CheckConstraint(condition=Q(uncertainty_coverage_factor__gt=0), name="ca_quality_plan_k_positive"),
            models.CheckConstraint(condition=Q(interlab_tolerance_absolute__isnull=True) | Q(interlab_tolerance_absolute__gte=0), name="ca_quality_abs_tol_nonnegative"),
            models.CheckConstraint(condition=Q(interlab_tolerance_percent__isnull=True) | Q(interlab_tolerance_percent__gte=0), name="ca_quality_pct_tol_nonnegative"),
            models.UniqueConstraint(fields=["method", "treatment_plant"], condition=Q(is_active=True), nulls_distinct=False, name="ca_one_active_quality_plan"),
        ]

    def clean(self):
        super().clean()
        if self.consensus_rule not in self.ConsensusRule.values:
            raise ValidationError({"consensus_rule": "Select a supported interlaboratory decision rule."})
        if self.consensus_rule == self.ConsensusRule.UNCERTAINTY_WEIGHTED and not self.require_measurement_uncertainty:
            raise ValidationError({"require_measurement_uncertainty": "Uncertainty-weighted consensus requires measurement uncertainty on each lab result."})

    @classmethod
    def current_for(cls, method, treatment_plant=None):
        if not method:
            return None
        if treatment_plant is not None:
            local = cls.objects.filter(method=method, treatment_plant=treatment_plant, is_active=True).order_by("-version").first()
            if local:
                return local
        return cls.objects.filter(method=method, treatment_plant__isnull=True, is_active=True).order_by("-version").first()

    def __str__(self):
        scope = self.treatment_plant.name if self.treatment_plant_id else "System default"
        return f"{self.method} · {scope} · QC v{self.version}"


class ChemicalMethodQCControl(models.Model):
    class CheckType(models.TextChoices):
        BLANK = "BLANK", "Method blank"
        REFERENCE = "REFERENCE", "Reference standard"
        DUPLICATE = "DUPLICATE", "Duplicate / replicate"
        SPIKE = "SPIKE", "Matrix spike"
        INSTRUMENT = "INSTRUMENT", "Instrument verification"

    class Evaluation(models.TextChoices):
        DIRECT_RANGE = "DIRECT_RANGE", "Observed value range"
        RECOVERY_PERCENT = "RECOVERY_PERCENT", "Percent recovery"
        RPD_PERCENT = "RPD_PERCENT", "Relative percent difference"
        BLANK_MAXIMUM = "BLANK_MAXIMUM", "Blank maximum"

    plan = models.ForeignKey(ChemicalMethodQualityPlan, on_delete=models.PROTECT, related_name="controls")
    code = models.SlugField(max_length=40)
    label = models.CharField(max_length=120)
    check_type = models.CharField(max_length=16, choices=CheckType.choices)
    evaluation = models.CharField(max_length=20, choices=Evaluation.choices, default=Evaluation.DIRECT_RANGE)
    required = models.BooleanField(default=True)
    target_value = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    spike_amount = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    acceptance_min = models.DecimalField(max_digits=12, decimal_places=5, null=True, blank=True)
    acceptance_max = models.DecimalField(max_digits=12, decimal_places=5, null=True, blank=True)
    warning_sigma = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("2"))
    corrective_instruction = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["plan", "label", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "code"], name="ca_qc_control_plan_code_unique"),
            models.CheckConstraint(condition=Q(acceptance_min__isnull=True) | Q(acceptance_max__isnull=True) | Q(acceptance_min__lte=F("acceptance_max")), name="ca_qc_acceptance_min_lte_max"),
            models.CheckConstraint(condition=Q(warning_sigma__gt=0), name="ca_qc_warning_sigma_positive"),
        ]

    def clean(self):
        super().clean()
        if self.evaluation == self.Evaluation.RECOVERY_PERCENT and self.check_type not in {self.CheckType.REFERENCE, self.CheckType.SPIKE}:
            raise ValidationError({"evaluation": "Recovery is supported for reference standards and matrix spikes."})
        if self.evaluation == self.Evaluation.RPD_PERCENT and self.check_type != self.CheckType.DUPLICATE:
            raise ValidationError({"evaluation": "Relative difference is supported for duplicate controls."})
        if self.evaluation == self.Evaluation.BLANK_MAXIMUM and self.check_type != self.CheckType.BLANK:
            raise ValidationError({"evaluation": "Blank maximum is supported only for blank controls."})
        if self.evaluation == self.Evaluation.RECOVERY_PERCENT and self.check_type == self.CheckType.SPIKE and (self.spike_amount is None or self.spike_amount <= 0):
            raise ValidationError({"spike_amount": "A matrix spike control requires a positive spike amount."})
        if self.evaluation == self.Evaluation.RECOVERY_PERCENT and self.check_type == self.CheckType.REFERENCE and (self.target_value is None or self.target_value <= 0):
            raise ValidationError({"target_value": "A reference standard control requires a positive target value."})
        if self.evaluation == self.Evaluation.DIRECT_RANGE and self.acceptance_min is None and self.acceptance_max is None:
            raise ValidationError({"acceptance_max": "Configure at least one direct acceptance bound."})
        if self.evaluation == self.Evaluation.BLANK_MAXIMUM and self.acceptance_max is None:
            raise ValidationError({"acceptance_max": "A blank control requires a maximum acceptable response."})
        if self.evaluation == self.Evaluation.RPD_PERCENT and self.acceptance_max is None:
            raise ValidationError({"acceptance_max": "A duplicate control requires a maximum acceptable relative percent difference."})
        if self.evaluation == self.Evaluation.RECOVERY_PERCENT and (self.acceptance_min is None or self.acceptance_max is None):
            raise ValidationError({"acceptance_min": "A recovery check requires both minimum and maximum acceptable recovery percentages."})

    def __str__(self):
        return f"{self.plan.method} · {self.label}"


class ChemicalSpecificationRemark(models.Model):
    specification = models.ForeignKey(ChemicalSpecification, on_delete=models.CASCADE, related_name='remarks')
    on_pass = models.TextField()
    on_fail = models.TextField()

    def __str__(self):
        return f"Remarks for {self.specification}"


class PackagingStatus(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class ChemicalCertApprover(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE
    )
    signature = models.ImageField(upload_to='approvers/signatures/', null=True, blank=True)
    rubber_stamp = models.ImageField(upload_to='approvers/stamps/', null=True, blank=True)

    def __str__(self):
        return self.user.full_name




class ChemicalBatch(models.Model):
    class Status(models.TextChoices):
        RECEIVED = 'RECEIVED', 'Received'
        ANALYSED = 'ANALYSED', 'Analysed'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'
        UTILIZED = 'UTILIZED', 'Utilized in Treatment'
        RETURNED = 'RETURNED', 'Return to Supplier (RTS)'

    sample_id = models.CharField(max_length=50, unique=True, editable=False)
    delivery_note_number = models.CharField(max_length=100, unique=True, blank=True)
    batch_number = models.CharField(max_length=100, unique=True)
    chemical = models.ForeignKey("Chemical", on_delete=models.CASCADE)
    location = models.ForeignKey(
        ChemicalLocation,
        on_delete=models.PROTECT,
        related_name="chemical_batches",
        null=True,
        blank=True,
    )
    supplier = models.ForeignKey(Supplier,on_delete=models.PROTECT,related_name="chemical_batches",related_query_name="chemical_batches",)
    treatment_plant = models.ForeignKey("t.TreatmentPlant", on_delete=models.SET_NULL, null=True,related_name='chemical_batches')
    received_date = models.DateField(default=timezone.localdate)
    quantity_received = models.DecimalField(max_digits=18, decimal_places=6)
    unit = models.CharField(max_length=10, default='kg')
    package_count = models.PositiveIntegerField(null=True,blank=True,help_text=(
            "Total number of discrete packages, bags, drums, "
            "containers, or other sampling units in the batch."),
    )
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,related_name='received_batches')
    status = models.CharField(max_length=25, choices=Status.choices, default=Status.RECEIVED)
    coa_generated = models.BooleanField(default=False)
    remarks = models.TextField(blank=True)
    quantity_utilized = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)
    utilization_remarks = models.TextField(blank=True)
    utilized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="utilized_chemical_batches",
    )
    utilized_at = models.DateTimeField(null=True, blank=True)
    quantity_returned = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)
    return_reason = models.TextField(blank=True)
    returned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returned_chemical_batches",
    )
    returned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Allowed transitions
    batch_transitions = {
        Status.RECEIVED: {Status.ANALYSED, Status.REJECTED},
        Status.ANALYSED: {Status.APPROVED, Status.REJECTED},
        Status.APPROVED: {Status.UTILIZED, Status.RETURNED},
        Status.REJECTED: {Status.RETURNED},
        Status.UTILIZED: set(),
        Status.RETURNED: set(),
    }

    def clean(self):
        super().clean()
        errors = {}
        try:
            quantity_received = Decimal(str(self.quantity_received))
        except (InvalidOperation, TypeError, ValueError):
            quantity_received = None
        if quantity_received is None or not quantity_received.is_finite():
            errors["quantity_received"] = "Quantity received must be a finite number."
        elif quantity_received <= 0:
            errors["quantity_received"] = "Quantity received must be greater than zero."
        else:
            self.quantity_received = quantity_received
        if self.received_date is None:
            errors["received_date"] = "Date received is required."
        if self.package_count is not None and self.package_count < 1:
            errors["package_count"] = "Package count must be at least one."
        if self.treatment_plant_id is None:
            errors["treatment_plant"] = "A treatment plant is required."
        if self._state.adding and not self.location_id:
            errors["location"] = "A chemical storage location is required."
        if self.location_id and self.treatment_plant_id:
            if self.location.treatment_plant_id != self.treatment_plant_id:
                errors["location"] = "The location must belong to the selected treatment plant."
        if errors:
            raise ValidationError(errors)

    # -------------------------
    # Auto-generate sample_id
    # -------------------------
    @transaction.atomic
    def save(self, *args, **kwargs):
        if self.pk and not getattr(self, "_allow_status_transition", False):
            persisted_status = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if persisted_status is not None and persisted_status != self.status:
                raise ValidationError("Chemical batch status can only change through the authorized transition workflow.")
        if self._state.adding and not self.location_id:
            raise ValidationError({"location": "A chemical storage location is required."})
        if (
            self.location_id
            and self.treatment_plant_id
            and self.location.treatment_plant_id != self.treatment_plant_id
        ):
            raise ValidationError({"location": "The location must belong to the selected treatment plant."})

        if not self.sample_id:
            year = timezone.localdate().strftime("%Y")

            # Chemical prefix
            chemical_prefix = (
                self.chemical.code.upper()
                if hasattr(self.chemical, "code") and self.chemical.code
                else self.chemical.name[:4].upper()
            )

            # Plant prefix
            plant_prefix = (
                self.treatment_plant.code.upper()
                if self.treatment_plant and hasattr(self.treatment_plant, "code") and self.treatment_plant.code
                else (self.treatment_plant.name[:3].upper() if self.treatment_plant else "PLT")
            )

            prefix = f"{chemical_prefix}-{plant_prefix}-{year}"
            Chemical.objects.select_for_update().get(pk=self.chemical_id)
            sequence, created_sequence = ChemicalBatchSequence.objects.get_or_create(prefix=prefix)
            sequence = ChemicalBatchSequence.objects.select_for_update().get(pk=sequence.pk)
            if created_sequence:
                highest_existing = 0
                for existing_id in type(self).objects.filter(
                    sample_id__startswith=f"{prefix}-"
                ).values_list("sample_id", flat=True):
                    suffix = existing_id.rsplit("-", 1)[-1]
                    if suffix.isdigit():
                        highest_existing = max(highest_existing, int(suffix))
                sequence.last_value = max(sequence.last_value, highest_existing)
            sequence.last_value += 1
            sequence.save(update_fields=["last_value", "updated_at"])
            new_seq = sequence.last_value
            self.sample_id = f"{prefix}-{new_seq:03d}"

        self.clean()

        super().save(*args, **kwargs)

    # -------------------------
    # Transitions
    # -------------------------
    @transaction.atomic
    def transition_to(self, target_status: str, performed_by=None, auto=False, permission=None):
        if not self.pk:
            raise ValidationError("A saved chemical batch is required for a status transition.")

        batch = type(self).objects.select_for_update().get(pk=self.pk)
        old_status = batch.status

        target_status = str(target_status or "").upper()
        if target_status not in self.Status.values:
            raise ValidationError(f"Unknown chemical batch status: {target_status}.")
        if auto:
            if not (
                old_status == self.Status.RECEIVED
                and target_status == self.Status.ANALYSED
            ):
                raise ValidationError("Only completed-analysis synchronization may automatically change batch status.")
        else:
            required_permission = permission or {
                self.Status.ANALYSED: "ca.complete_chemical_analysis",
                self.Status.APPROVED: "ca.approve_chemical_batch",
                self.Status.REJECTED: "ca.reject_chemical_batch",
                self.Status.UTILIZED: "ca.utilize_chemical_batch",
                self.Status.RETURNED: "ca.return_chemical_batch",
            }.get(target_status)
            if target_status == self.Status.REJECTED and permission is None:
                if performed_by is None or not (
                    performed_by.is_superuser
                    or performed_by.has_perm("ca.reject_chemical_batch")
                    or performed_by.has_perm("ca.resolve_chemical_batch_hold")
                ):
                    raise PermissionDenied("You do not have permission to reject this chemical batch.")
                require_ca_actor(performed_by, plant_id=batch.treatment_plant_id)
            else:
                require_ca_actor(
                    performed_by,
                    required_permission,
                    plant_id=batch.treatment_plant_id,
                )

        if batch.has_active_holds and target_status != self.Status.REJECTED:
            raise ValidationError("The batch is on intake hold. Resolve the discrepancy before advancing it.")
        if not batch.can_transition_to(target_status):
            raise ValidationError(f"Invalid transition: {old_status} → {target_status}")

        if target_status == self.Status.APPROVED:
            if hasattr(batch, "test_results"):
                results = batch.test_results.all()
                if results.filter(Q(status__isnull=True) | Q(status="")).exists():
                    raise ValidationError("Cannot approve: pending test results exist.")
                if results.filter(status=BatchTestResult.Status.FAIL).exists():
                    raise ValidationError("Cannot approve: one or more test results failed.")

        batch.status = target_status
        batch._allow_status_transition = True
        try:
            batch.save(update_fields=["status", "updated_at"])
        finally:
            batch.__dict__.pop("_allow_status_transition", None)
        self.status = target_status
        self.updated_at = batch.updated_at

        transaction.on_commit(
            lambda batch_id=batch.pk, old_status=old_status, target_status=target_status,
            performed_by=performed_by, auto=auto: batch_status_changed.send(
                sender=type(self),
                instance=type(self).objects.get(pk=batch_id),
                old_status=old_status,
                new_status=target_status,
                performed_by=performed_by if performed_by else "SYSTEM" if auto else None,
            )
        )

    # -------------------------
    # Helper Methods
    # -------------------------
    def can_transition_to(self, target_status: str):
        return target_status in self.batch_transitions.get(self.status, [])

    @property
    def has_active_holds(self):
        return self.workflow_holds.filter(
            status=ChemicalBatchHold.Status.OPEN
        ).exists()

    @property
    def available_quantity(self):
        latest_balance = self.inventory_movements.order_by("-sequence").values_list(
            "quantity_after", flat=True
        ).first()
        if latest_balance is not None:
            return latest_balance
        # Preserve usable stock visibility for legacy batches that predate
        # the append-only movement ledger, until they receive a ledger entry.
        return max(
            Decimal("0"),
            Decimal(str(self.quantity_received or 0))
            - Decimal(str(self.quantity_utilized or 0))
            - Decimal(str(self.quantity_returned or 0)),
        )

    def is_approved(self):
        return self.status == self.Status.APPROVED

    def __str__(self):
        return f"Batch {self.sample_id} - {self.chemical.name} [{self.status}]"

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["RECEIVED", "ANALYSED", "APPROVED", "REJECTED", "UTILIZED", "RETURNED"]),
                name="ca_batch_status_valid",
            ),
            models.CheckConstraint(condition=Q(quantity_received__gt=0), name="ca_batch_quantity_positive"),
            models.CheckConstraint(
                condition=Q(quantity_utilized__isnull=True) | Q(quantity_utilized__gte=0),
                name="ca_batch_utilized_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(quantity_returned__isnull=True) | Q(quantity_returned__gte=0),
                name="ca_batch_returned_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(package_count__isnull=True) | Q(package_count__gte=1),
                name="ca_batch_package_count_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["treatment_plant", "status", "-received_date"], name="ca_batch_plant_status_date_idx"),
            models.Index(fields=["chemical", "-received_date"], name="ca_batch_chemical_date_idx"),
        ]
        ordering = ["-received_date", "-created_at"]
        verbose_name = "Batch"
        verbose_name_plural = "Batches"

class ChemicalAnalysisRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    REQUEST_DEADLINE_HOURS = 9
    CHEMICAL_ANALYSIS_TASK_NAME = "Water Treatment Chemical Analysis"
    MAX_AUTO_ASSIGNED_ANALYSTS = 1

    request_id = models.CharField(
        max_length=50,
        unique=True,
        blank=True,
        editable=False,
    )

    batch = models.OneToOneField(
        ChemicalBatch,
        on_delete=models.CASCADE,
        related_name="analysis_request",
        null=True,
        blank=True,
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chemical_requests",
    )

    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_analysis_requests",
    )

    delivery_date = models.DateField()



    analysis_started_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    analysis_deadline = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
    )

    completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="completed_chemical_requests",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    specification_snapshot = models.JSONField(
        default=dict,
        blank=True,
        editable=False,
        help_text="Immutable analysis requirements captured when this request is created.",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    # Kept as a compatibility constant for older task payloads. Assignment is
    # immediate; this value intentionally no longer represents a grace period.
    ASSIGNMENT_TIMEOUT_MINUTES = 0

    assignment_due_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Timestamp when this request became eligible for immediate analyst assignment.",
    )

    auto_assigned_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
    )

    manual_assigned_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
    )

    assigned_analysts = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="assigned_requests",
        blank=True,
        editable=True,
    )

    request_transitions = {
        Status.PENDING: (
            Status.IN_PROGRESS,
            Status.CANCELLED,
        ),
        Status.IN_PROGRESS: (
            Status.COMPLETED,
            Status.CANCELLED,
        ),
        Status.COMPLETED: (),
    }

    request_to_batch_cascade = {
        Status.PENDING: None,
        Status.IN_PROGRESS: None,
        Status.COMPLETED: ChemicalBatch.Status.ANALYSED,
    }

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["PENDING", "IN_PROGRESS", "COMPLETED", "CANCELLED"]),
                name="ca_request_status_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "-created_at"],
                name="ca_req_status_created_idx",
            ),
            models.Index(
                fields=["analysis_deadline", "status"],
                name="ca_req_deadline_status_idx",
            ),
            models.Index(
                fields=["delivery_date", "-created_at"],
                name="ca_req_delivery_created_idx",
            ),
            models.Index(
                fields=["batch", "status"],
                name="ca_req_batch_status_idx",
            ),
        ]

    @property
    def chemical(self):
        if not self.batch_id:
            return None
        return self.batch.chemical

    @property
    def supplier(self):
        if not self.batch_id:
            return None
        return self.batch.supplier

    @property
    def treatment_plant(self):
        if not self.batch_id:
            return None
        return self.batch.treatment_plant

    @property
    def quantity(self):
        if not self.batch_id:
            return None
        return self.batch.quantity_received

    @property
    def unit(self):
        if not self.batch_id:
            return None
        return self.batch.unit

    @property
    def sample_id(self):
        if not self.batch_id:
            return None
        return self.batch.sample_id

    @property
    def batch_number(self):
        if not self.batch_id:
            return None
        return self.batch.batch_number

    @property
    def is_pending(self):
        return self.status == self.Status.PENDING

    @property
    def is_in_progress(self):
        return self.status == self.Status.IN_PROGRESS

    @property
    def is_completed(self):
        return self.status == self.Status.COMPLETED

    @property
    def is_overdue(self):
        return bool(
            self.status != self.Status.COMPLETED
            and self.analysis_deadline
            and timezone.now() > self.analysis_deadline
        )

    @property
    def time_remaining(self):
        if self.status == self.Status.COMPLETED:
            return timedelta(0)

        if not self.analysis_deadline:
            return None

        return self.analysis_deadline - timezone.now()

    @property
    def assigned_analyst_count(self):
        return self.assigned_analysts.count()

    @property
    def has_assigned_analysts(self):
        return self.assigned_analysts.exists()

    @property
    def assignment_pending(self):
        return (
            self.status == self.Status.PENDING
            and not self.has_assigned_analysts
        )

    @property
    def assignment_overdue(self):
        # Assignment eligibility starts as soon as the request is created.
        return (
            self.status in {self.Status.PENDING, self.Status.IN_PROGRESS}
            and not self.has_assigned_analysts
        )

    @property
    def latest_checklist(self):
        return (
            self.checklists
            .select_related(
                "chemical",
                "sampled_by",
                "requested_by",
            )
            .order_by("-created_at")
            .first()
        )

    def get_current_assigned_analysts(self):
        """Return analysts currently scheduled for this task.

        This method only discovers the roster. It never changes the
        request's manual assignments.
        """
        if not self.batch_id:
            return get_user_model().objects.none()

        from weeklytasks.models import DailyTaskAssignment, WeeklyDutyTask

        now = timezone.localtime()
        current_date = now.date()
        current_time = now.time()

        plant_id = self.batch.treatment_plant_id
        if not plant_id:
            return get_user_model().objects.none()

        section_id = getattr(self.batch, "section_id", None)
        task_queryset = (
            WeeklyDutyTask.objects
            .filter(
                task_template__name__iexact=self.CHEMICAL_ANALYSIS_TASK_NAME,
                task_template__is_active=True,
                treatment_plant_id=plant_id,
                week__start_date__lte=current_date,
                week__end_date__gte=current_date,
            )
            .order_by("-week__start_date", "-created_at")
        )
        if section_id:
            task_queryset = task_queryset.filter(section_id=section_id)
        if not task_queryset.exists():
            return get_user_model().objects.none()

        assignment_queryset = DailyTaskAssignment.objects.filter(
                task__in=task_queryset,
                date=current_date,
                user__is_active=True,
                shift__is_deleted=False,
            )
        assignment_queryset = assignment_queryset.filter(
            Q(user__is_superuser=True)
            | Q(user__assigned_treatment_plant_id=plant_id)
        )
        if section_id:
            assignment_queryset = assignment_queryset.filter(
                Q(user__is_superuser=True) | Q(user__section_id=section_id)
            )
        assignments = (
            assignment_queryset.filter(
                Q(user__is_superuser=True)
                | Q(
                    user__groups__permissions__content_type__app_label="ca",
                    user__groups__permissions__codename="start_chemical_analysis",
                )
                | Q(
                    user__user_permissions__content_type__app_label="ca",
                    user__user_permissions__codename="start_chemical_analysis",
                )
            )
            .filter(
                Q(
                    shift__start_time__lte=current_time,
                    shift__end_time__gt=current_time,
                )
                | (
                    Q(shift__start_time__gt=models.F("shift__end_time"))
                    & (
                        Q(shift__start_time__lte=current_time)
                        | Q(shift__end_time__gt=current_time)
                    )
                )
            )
            .values_list("user_id", flat=True)
            .distinct()
        )

        candidates = (
            get_user_model().objects
            .filter(
                pk__in=assignments,
                is_active=True,
            )
            .filter(
                Q(is_superuser=True)
                | Q(assigned_treatment_plant_id=plant_id)
            )
        )
        if section_id:
            candidates = candidates.filter(Q(is_superuser=True) | Q(section_id=section_id))
        candidates = self.eligible_analysts(candidates)
        return candidates.annotate(
            active_ca_workload=Count(
                "performed_chemical_analysis_processes",
                filter=Q(
                    performed_chemical_analysis_processes__status__in=(
                        ChemicalAnalysisProcess.WorkflowStatus.ANALYSIS,
                        ChemicalAnalysisProcess.WorkflowStatus.RETEST,
                    )
                ),
                distinct=True,
            )
        ).order_by("active_ca_workload", "last_name", "first_name", "username")

    def eligible_analysts(self, queryset=None):
        """Filter analysts by the permissions required to perform CA analysis."""
        User = get_user_model()
        if queryset is None:
            queryset = User.objects.filter(is_active=True)
        queryset = queryset.filter(is_active=True)

        # Use EXISTS subqueries for each required permission instead of nested
        # IN (...SELECT pk...) sub-selects.  EXISTS short-circuits on the
        # first matching row and lets PostgreSQL use covering indexes on the
        # auth permission / group-permission tables, shrinking this lookup
        # from several seconds to single-digit milliseconds.
        # We go through User's forward relations (groups__permissions and
        # user_permissions) to avoid relying on the custom reverse accessor
        # name that the accounts app assigns to the M2M through table.
        from django.db.models import Exists, OuterRef
        User = get_user_model()

        for codename in (
            "start_chemical_analysis",
            "record_chemical_result",
            "complete_receiving_checklist",
            "complete_chemical_analysis",
        ):
            has_perm = User.objects.filter(
                pk=OuterRef("pk"),
            ).filter(
                Q(
                    groups__permissions__content_type__app_label="ca",
                    groups__permissions__codename=codename,
                )
                | Q(
                    user_permissions__content_type__app_label="ca",
                    user_permissions__codename=codename,
                )
            )
            queryset = queryset.filter(
                Q(is_superuser=True) | Exists(has_perm)
            )

        return queryset.distinct()

    @staticmethod
    def _specification_snapshot_item(specification):
        method = specification.method
        return {
            "specification_id": specification.pk,
            "version": specification.version,
            "chemical_id": specification.chemical_id,
            "treatment_plant_id": specification.treatment_plant_id,
            "parameter_name": specification.parameter_name,
            "unit": specification.unit,
            "min_limit": format(specification.min_limit, "f") if specification.min_limit is not None else None,
            "max_limit": format(specification.max_limit, "f") if specification.max_limit is not None else None,
            "is_required": specification.is_required,
            "is_active": specification.is_active,
            "requires_instrument": specification.requires_instrument,
            "method_id": str(specification.method_id) if specification.method_id else None,
            "method_name": method.method if method else "",
            "method_version": method.version if method else None,
            "method_reference": method.kebs_reference if method else "",
            "method_formula_hash": method.formula_hash if method else "",
            "method_inputs": method.inputs_structure if method else [],
            "method_unit": method.unit if method else "",
            "method_rounding_precision": method.rounding_precision if method else None,
            "method_rounding_mode": method.rounding_mode if method else "HALF_UP",
        }

    def capture_specification_snapshot(self):
        if not self.batch_id:
            return {"captured": False, "items": []}
        specifications = ChemicalSpecification.current_for(
            ChemicalSpecification.objects.filter(
                chemical_id=self.batch.chemical_id,
                treatment_plant_id=self.batch.treatment_plant_id,
                is_required=True,
            ).select_related("method")
        ).order_by("parameter_name", "pk")
        return {
            "captured": True,
            "captured_at": timezone.now().isoformat(),
            "items": [self._specification_snapshot_item(item) for item in specifications],
        }

    @transaction.atomic
    def assign_analysts(self, users, *, assigned_by=None, manual=True, _form_validated=False):
        """Assign analysts explicitly without allowing later auto-assignment
        to overwrite the selection.
        """
        if not self.pk:
            raise ValidationError("Save the analysis request before assigning analysts.")

        type(self).objects.select_for_update().get(pk=self.pk)
        self.refresh_from_db()
        if self.status not in {self.Status.PENDING, self.Status.IN_PROGRESS}:
            raise ValidationError("Analysts can only be assigned while the request is active.")

        if manual and assigned_by is None:
            raise ValidationError("An authorized user is required to assign analysts.")

        if manual:
            if not getattr(assigned_by, "is_active", False):
                raise ValidationError("An active user is required to assign analysts.")
            if not (assigned_by.is_superuser or assigned_by.has_perm("ca.create_analysis_request")):
                raise ValidationError("You do not have permission to assign chemical analysts.")
            if (
                self.batch_id
                and self.batch.treatment_plant_id
                and not assigned_by.is_superuser
                and getattr(assigned_by, "assigned_treatment_plant_id", None)
                != self.batch.treatment_plant_id
            ):
                raise ValidationError("You cannot assign analysts outside your treatment-plant scope.")

        users = list(users or [])

        if not users:
            raise ValidationError(
                "At least one active analyst must be assigned."
            )

        if _form_validated:
            # The caller (e.g. AnalystAssignmentForm) already verified every
            # user against the eligible-analyst queryset, so skip the
            # expensive eligibility re-check and individual permission probes
            # to keep the row-level lock duration short.
            user_ids = [getattr(u, "pk", u) for u in users]
        else:
            user_ids = []
            eligible_ids = set(self.eligible_analysts().values_list("pk", flat=True))
            User = get_user_model()

            for user in users:
                user_id = getattr(user, "pk", user)
                if not user_id:
                    raise ValidationError("Every analyst must be a valid user.")

                user_obj = (
                    user
                    if getattr(user, "pk", None)
                    else User.objects.filter(pk=user_id).first()
                )

                if user_obj is None:
                    raise ValidationError(
                        f"Analyst {user_id} could not be found."
                    )

                if not user_obj.is_active:
                    raise ValidationError(
                        f"{user_obj.get_username()} is inactive and cannot be assigned."
                    )

                if not user_obj.has_perm("ca.start_chemical_analysis") or not user_obj.has_perm("ca.record_chemical_result"):
                    raise ValidationError(
                        f"{user_obj.get_username()} does not have permission "
                        "to start and record chemical analysis."
                    )
                if not user_obj.has_perm("ca.complete_receiving_checklist"):
                    raise ValidationError(
                        f"{user_obj.get_username()} does not have permission to complete receiving checklists."
                    )
                if not user_obj.has_perm("ca.complete_chemical_analysis"):
                    raise ValidationError(
                        f"{user_obj.get_username()} does not have permission to complete chemical analysis."
                    )

                if (
                    self.batch_id
                    and self.batch.treatment_plant_id
                    and not user_obj.is_superuser
                    and getattr(user_obj, "assigned_treatment_plant_id", None)
                    != self.batch.treatment_plant_id
                ):
                    raise ValidationError(
                        f"{user_obj.get_username()} is not assigned to the "
                        "treatment plant for this chemical batch."
                    )

                if user_obj.pk not in eligible_ids:
                    raise ValidationError(
                        f"{user_obj.get_username()} does not have the required chemical-analysis permissions for this batch."
                    )

                user_ids.append(user_obj.pk)

        now = timezone.now()
        self._ca_assignment_actor = assigned_by if manual else None
        try:
            self.assigned_analysts.set(sorted(set(user_ids)))
        finally:
            self.__dict__.pop("_ca_assignment_actor", None)

        if manual:
            self.manual_assigned_at = now
            self.auto_assigned_at = None
            type(self).objects.filter(pk=self.pk).update(
                manual_assigned_at=now,
                auto_assigned_at=None,
                updated_at=now,
            )

        return self.assigned_analysts.all()

    @transaction.atomic
    def clear_analyst_assignments(self, *, allow_reassignment=False):
        if not self.pk:
            raise ValidationError("Save the analysis request before changing analyst assignments.")
        type(self).objects.select_for_update().get(pk=self.pk)
        self.refresh_from_db()
        if self.status != self.Status.PENDING and not allow_reassignment:
            raise ValidationError(
                "Analysts can only be cleared while the request is pending."
            )

        self.assigned_analysts.clear()

        now = timezone.now()
        type(self).objects.filter(pk=self.pk).update(
            manual_assigned_at=None,
            auto_assigned_at=None,
            updated_at=now,
        )

        self.manual_assigned_at = None
        self.auto_assigned_at = None
        return self

    def auto_assign_analysts_if_due(self, *, now=None):
        """Immediately assign one available analyst to an unassigned request.

        A current duty-roster analyst is preferred. If the roster has no
        eligible member, the least-loaded eligible analyst at the batch plant
        is selected. Existing manual or automatic assignments are preserved.
        """
        if not self.pk:
            return get_user_model().objects.none()

        now = now or timezone.now()

        with transaction.atomic():
            request = (
                type(self).objects
                .select_for_update()
                .get(pk=self.pk)
            )

            if request.status != type(self).Status.PENDING:
                return request.assigned_analysts.all()

            if request.assigned_analysts.exists():
                return request.assigned_analysts.all()

            analysts = request.get_current_assigned_analysts()[:1]
            if not analysts.exists() and request.batch_id and request.batch.treatment_plant_id:
                plant_id = request.batch.treatment_plant_id
                analysts = request.eligible_analysts().filter(
                    Q(is_superuser=True) | Q(assigned_treatment_plant_id=plant_id)
                ).annotate(
                    active_ca_workload=Count(
                        "performed_chemical_analysis_processes",
                        filter=Q(
                            performed_chemical_analysis_processes__status__in=(
                                ChemicalAnalysisProcess.WorkflowStatus.ANALYSIS,
                                ChemicalAnalysisProcess.WorkflowStatus.RETEST,
                            )
                        ),
                        distinct=True,
                    )
                ).order_by("active_ca_workload", "last_name", "first_name", "username")[:1]

            analyst_ids = list(analysts.values_list("pk", flat=True))
            if not analyst_ids:
                return request.assigned_analysts.all()

            request.assigned_analysts.set(analyst_ids)

            request.auto_assigned_at = now
            request.save(
                update_fields=[
                    "auto_assigned_at",
                    "updated_at",
                ]
            )

            return request.assigned_analysts.all()

    def populate_assigned_analysts(self):
        """Backward-compatible entry point for immediate assignment."""
        return self.auto_assign_analysts_if_due()

    def is_assigned_analyst(self, user):
        if user is None or not getattr(user, "is_active", False):
            return False

        return self.assigned_analysts.filter(pk=user.pk).exists()

    def clean(self):
        super().clean()

        errors = {}

        if self.batch_id:
            batch = self.batch

            if batch is None:
                errors["batch"] = (
                    "The selected chemical batch could not be found."
                )
            else:
                if (
                    self.status in (
                        self.Status.IN_PROGRESS,
                        self.Status.COMPLETED,
                    )
                    and not batch.chemical_id
                ):
                    errors["batch"] = (
                        "The chemical batch must have an associated "
                        "chemical before analysis can proceed."
                    )

        if self.received_by_id:
            if not self.received_by.is_active:
                errors["received_by"] = (
                    "The staff member receiving the analysis request "
                    "must be active."
                )
            elif self.pk and not self.assigned_analysts.filter(
                pk=self.received_by_id
            ).exists():
                errors["received_by"] = (
                    "The staff member receiving the chemical analysis "
                    "request must be one of the assigned analysts."
                )

        if (
            self.status in (
                self.Status.IN_PROGRESS,
                self.Status.COMPLETED,
            )
            and not self.analysis_started_at
        ):
            errors["analysis_started_at"] = (
                "An analysis start time is required."
            )

        if self.status == self.Status.IN_PROGRESS:
            if not self.analysis_deadline:
                errors["analysis_deadline"] = (
                    "An analysis deadline is required while "
                    "analysis is in progress."
                )

            if self.completed_at:
                errors["completed_at"] = (
                    "An in-progress request cannot have a completion time."
                )

            if self.completed_by_id:
                errors["completed_by"] = (
                    "An in-progress request cannot have a completing user."
                )

        if self.status == self.Status.COMPLETED:
            if not self.completed_at:
                errors["completed_at"] = (
                    "A completed request must have a completion time."
                )

            if not self.completed_by_id:
                errors["completed_by"] = (
                    "A completed request must have a completing user."
                )

        if (
            self.analysis_started_at
            and self.analysis_deadline
            and self.analysis_deadline < self.analysis_started_at
        ):
            errors["analysis_deadline"] = (
                "The analysis deadline cannot be earlier than "
                "the analysis start time."
            )

        if (
            self.completed_at
            and self.analysis_started_at
            and self.completed_at < self.analysis_started_at
        ):
            errors["completed_at"] = (
                "The completion time cannot be earlier than "
                "the analysis start time."
            )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk and not getattr(self, "_allow_status_transition", False):
            persisted_status = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if persisted_status is not None and persisted_status != self.status:
                raise ValidationError("Analysis request status can only change through its authorized transition workflow.")
        if not self.request_id:
            delivery_date = (
                self.delivery_date
                or timezone.now().date()
            )
            self.request_id = (
                f"REQ-"
                f"{delivery_date.strftime('%Y%m%d')}-"
                f"{uuid.uuid4().hex[:8].upper()}"
            )

        if self.assignment_due_at is None:
            self.assignment_due_at = timezone.now()

        captured_specifications = False
        if not (self.specification_snapshot or {}).get("captured") and self.batch_id:
            self.specification_snapshot = self.capture_specification_snapshot()
            captured_specifications = True
        if captured_specifications and kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"specification_snapshot"}

        if self.pk:
            prior_snapshot = type(self).objects.filter(pk=self.pk).values_list(
                "specification_snapshot", flat=True
            ).first()
            if (prior_snapshot or {}).get("captured") and prior_snapshot != self.specification_snapshot:
                raise ValidationError("An analysis request's specification snapshot is immutable.")

        super().save(*args, **kwargs)

        if self.received_by_id and self.pk:
            if not self.assigned_analysts.filter(
                pk=self.received_by_id
            ).exists():
                raise ValidationError(
                    "The staff member receiving the chemical analysis "
                    "request must be one of the assigned analysts."
                )

    def can_transition_to(self, target_status):
        return target_status in self.request_transitions.get(
            self.status,
            (),
        )

    def _validate_start_requirements(self):
        if not self.batch_id:
            raise ValidationError(
                "Cannot start analysis without an associated "
                "chemical batch."
            )

        if not self.batch_id or not self.batch.location_id:
            raise ValidationError(
                "Cannot start analysis without an analysis location."
            )

        if not self.assigned_analysts.exists():
            raise ValidationError(
                "No analyst is currently assigned to perform "
                "Water Treatment Chemical Analysis for this "
                "treatment plant, section, date, and shift."
            )

        if not self.batch.chemical_id:
            raise ValidationError(
                "The chemical batch must have an associated chemical."
            )

    def _validate_completion_requirements(self):
        if not self.batch_id:
            raise ValidationError(
                "Cannot complete an analysis request without "
                "an associated chemical batch."
            )

        if not self.analysis_started_at:
            raise ValidationError(
                "Cannot complete an analysis request that "
                "has not been started."
            )

        if not self.assigned_analysts.exists():
            raise ValidationError(
                "At least one analyst must currently be assigned "
                "to the chemical analysis task."
            )

        results = self.batch.test_results.select_related(
            "parameter",
            "parameter__chemical",
        )

        if not results.exists():
            raise ValidationError(
                "Cannot complete the analysis request without "
                "analytical test results."
            )

        required_specifications = self.get_required_specifications()

        if required_specifications.exists():
            completed_parameter_ids = set(
                results.exclude(
                    Q(status__isnull=True) | Q(status="")
                ).values_list(
                    "parameter_id",
                    flat=True,
                )
            )

            missing = required_specifications.exclude(
                pk__in=completed_parameter_ids
            )

            if missing.exists():
                names = list(
                    missing.values_list(
                        "parameter_name",
                        flat=True,
                    )[:10]
                )

                raise ValidationError(
                    "Cannot complete the analysis because the "
                    "following required parameters have no applicable "
                    f"results: {', '.join(names)}."
                )

        if results.filter(Q(status__isnull=True) | Q(status="")).exists():
            raise ValidationError(
                "All analytical test results must have a status "
                "before the request can be completed."
            )

    def transition_to(
        self,
        target_status,
        performed_by=None,
        auto=False,
    ):
        if target_status not in self.Status.values:
            raise ValidationError(
                f"Unknown analysis request status: {target_status}"
            )

        permission = {
            self.Status.IN_PROGRESS: "ca.start_chemical_analysis",
            self.Status.COMPLETED: "ca.complete_chemical_analysis",
        }.get(target_status)
        plant_id = self.batch.treatment_plant_id if self.batch_id else None
        require_ca_actor(performed_by, permission, plant_id=plant_id)
        if target_status == self.Status.CANCELLED and not (
            performed_by.is_superuser
            or performed_by.has_perm("ca.create_analysis_request")
            or performed_by.has_perm("ca.resolve_chemical_batch_hold")
        ):
            raise PermissionDenied("You do not have permission to cancel this chemical analysis request.")
        if target_status == self.Status.IN_PROGRESS and not self.is_assigned_analyst(performed_by):
            raise PermissionDenied("Only an analyst assigned to this request may start it.")

        with transaction.atomic():
            request = (
                type(self).objects
                # ``batch`` and especially ``batch.location`` are nullable
                # relations. Lock the request row itself so PostgreSQL does
                # not attempt FOR UPDATE on the nullable outer-join side.
                .select_for_update(of=("self",))
                .select_related(
                    "batch",
                    "batch__location",
                )
                .get(pk=self.pk)
            )

            old_status = request.status

            if target_status == old_status:
                raise ValidationError(
                    f"Analysis request is already {old_status}."
                )

            if target_status not in request.request_transitions.get(
                old_status,
                (),
            ):
                raise ValidationError(
                    f"Invalid transition: "
                    f"{old_status} → {target_status}"
                )

            now = timezone.now()

            if (
                old_status == self.Status.PENDING
                and target_status == self.Status.IN_PROGRESS
            ):
                request._validate_start_requirements()

                request.analysis_started_at = now
                request.analysis_deadline = (
                    now
                    + timedelta(
                        hours=self.REQUEST_DEADLINE_HOURS
                    )
                )

            elif (
                old_status == self.Status.IN_PROGRESS
                and target_status == self.Status.COMPLETED
            ):
                request._validate_completion_requirements()

                request.completed_at = now
                request.completed_by = performed_by

                if not request.analysis_started_at:
                    request.analysis_started_at = now

                if not request.analysis_deadline:
                    request.analysis_deadline = (
                        request.analysis_started_at
                        + timedelta(
                            hours=self.REQUEST_DEADLINE_HOURS
                        )
                    )

            request.status = target_status

            request._allow_status_transition = True
            try:
                request.save(
                    update_fields=[
                    "status",
                    "analysis_started_at",
                    "analysis_deadline",
                    "completed_at",
                    "completed_by",
                    "updated_at",
                    ]
                )
            finally:
                request.__dict__.pop("_allow_status_transition", None)

            target_batch_status = (
                request.request_to_batch_cascade.get(
                    target_status
                )
            )

            batch = request.batch

            if (
                target_batch_status
                and batch
                and batch.status != target_batch_status
            ):
                if not batch.can_transition_to(
                    target_batch_status
                ):
                    raise ValidationError(
                        f"Cannot transition batch "
                        f"{batch.sample_id} from "
                        f"{batch.status} to "
                        f"{target_batch_status}."
                    )

                batch.transition_to(
                    target_batch_status,
                    performed_by=performed_by,
                    auto=True,
                )

            self.status = request.status
            self.analysis_started_at = (
                request.analysis_started_at
            )
            self.analysis_deadline = (
                request.analysis_deadline
            )
            self.completed_at = request.completed_at
            self.completed_by_id = request.completed_by_id
            self.updated_at = request.updated_at

            transaction.on_commit(
                lambda request_id=request.pk,
                old_status=old_status,
                new_status=target_status,
                performed_by=performed_by,
                auto=auto: request_status_changed.send(
                    sender=type(self),
                    instance=type(self).objects.get(
                        pk=request_id
                    ),
                    old_status=old_status,
                    new_status=new_status,
                    performed_by=(
                        performed_by
                        if performed_by
                        else "SYSTEM"
                        if auto
                        else None
                    ),
                )
            )

        return self

    def start_analysis(self, performed_by=None):
        return self.transition_to(
            self.Status.IN_PROGRESS,
            performed_by=performed_by,
            auto=False,
        )

    def mark_completed(self, performed_by=None):
        if performed_by is None:
            raise ValidationError(
                "A user is required to complete the analysis request."
            )

        return self.transition_to(
            self.Status.COMPLETED,
            performed_by=performed_by,
            auto=False,
        )

    def get_latest_checklist(self):
        return (
            self.checklists
            .select_related(
                "chemical",
                "sampled_by",
                "requested_by",
            )
            .order_by("-created_at")
            .first()
        )

    def get_checklists(self):
        return self.checklists.select_related(
            "chemical",
            "sampled_by",
            "requested_by",
        ).order_by("-created_at")

    def get_test_results(self):
        if not self.batch_id:
            return BatchTestResult.objects.none()

        return (
            self.batch.test_results
            .select_related(
                "parameter",
                "parameter__chemical",
                "parameter__method",
                "analyst",
            )
            .order_by(
                "parameter__chemical__name",
                "parameter__parameter_name",
                "id",
            )
        )

    def get_required_specifications(self):
        if not self.batch_id:
            return ChemicalSpecification.objects.none()
        snapshot = self.specification_snapshot or {}
        if snapshot.get("captured"):
            specification_ids = [
                item.get("specification_id")
                for item in snapshot.get("items", [])
                if item.get("is_required") and item.get("specification_id")
            ]
            return ChemicalSpecification.objects.filter(
                pk__in=specification_ids,
                chemical_id=self.batch.chemical_id,
                treatment_plant_id=self.batch.treatment_plant_id,
            ).select_related("chemical", "method").prefetch_related("remarks")
        return ChemicalSpecification.current_for(
            ChemicalSpecification.objects.filter(
                chemical_id=self.batch.chemical_id,
                treatment_plant_id=self.batch.treatment_plant_id,
                is_required=True,
            ).select_related("chemical", "method").prefetch_related("remarks")
        )

    def get_missing_required_specifications(self):
        if not self.batch_id:
            return ChemicalSpecification.objects.none()

        required = self.get_required_specifications()

        result_parameter_ids = self.batch.test_results.filter(
            status__in=[
                BatchTestResult.Status.PASS,
                BatchTestResult.Status.FAIL,
            ]
        ).values_list(
            "parameter_id",
            flat=True,
        )

        return required.exclude(
            pk__in=result_parameter_ids
        )

    def has_test_results(self):
        return bool(
            self.batch_id
            and self.batch.test_results.exists()
        )

    def has_failed_results(self):
        return bool(
            self.batch_id
            and self.batch.test_results.filter(
                status=BatchTestResult.Status.FAIL
            ).exists()
        )

    def has_not_applicable_results(self):
        return bool(
            self.batch_id
            and self.batch.test_results.filter(
                status=BatchTestResult.Status.NA
            ).exists()
        )

    def has_incomplete_results(self):
        if not self.batch_id:
            return True

        return self.get_missing_required_specifications().exists()

    def get_result_counts(self):
        if not self.batch_id:
            return {
                "total": 0,
                "pass": 0,
                "fail": 0,
                "na": 0,
            }

        queryset = self.batch.test_results

        return {
            "total": queryset.count(),
            "pass": queryset.filter(
                status=BatchTestResult.Status.PASS
            ).count(),
            "fail": queryset.filter(
                status=BatchTestResult.Status.FAIL
            ).count(),
            "na": queryset.filter(
                status=BatchTestResult.Status.NA
            ).count(),
        }

    def can_generate_coa(self):
        if self.status != self.Status.COMPLETED:
            return False

        if not self.batch_id:
            return False

        if not self.has_test_results():
            return False

        if self.has_incomplete_results():
            return False

        return True

    def get_or_create_coa(self):
        if not self.can_generate_coa():
            raise ValidationError(
                "A CoA can only be generated after the chemical "
                "analysis request has been completed with all "
                "required analytical results."
            )

        with transaction.atomic():
            batch = (
                ChemicalBatch.objects
                .select_for_update()
                .get(pk=self.batch_id)
            )

            coa, created = ChemicalCoA.objects.get_or_create(
                batch=batch,
            )

            if created:
                coa.auto_populate()

            return coa, created

    def mark_overdue(self):
        if self.status == self.Status.COMPLETED:
            return False

        if not self.analysis_deadline:
            return False

        return timezone.now() > self.analysis_deadline

    def refresh_from_db(
        self,
        using=None,
        fields=None,
        from_queryset=None,
    ):
        super().refresh_from_db(
            using=using,
            fields=fields,
            from_queryset=from_queryset,
        )

        return self

    def __str__(self):
        chemical_name = (
            self.chemical.name
            if self.chemical
            else "Unknown Chemical"
        )

        sample_id = (
            self.sample_id
            or "N/A"
        )

        return (
            f"Analysis Request {self.request_id} - "
            f"{chemical_name} ({sample_id})"
        )

class ChecklistForWaterTreatmentChemicalAnalysis(models.Model):
    class Grade(models.TextChoices):
        STANDARD = "STANDARD", "Standard"
        PREMIUM = "PREMIUM", "Premium"
        NA = "NA", "Not Applicable"

    chemical_analysis_request = models.ForeignKey(
        "ChemicalAnalysisRequest",
        on_delete=models.CASCADE,
        related_name="checklists",
        db_index=True,
    )

    chemical = models.ForeignKey(
        "Chemical",
        on_delete=models.PROTECT,
        related_name="chemical_analysis_checklists",
    )

    manufacturer_name_indicated = models.BooleanField(
        default=False,
    )

    chemical_name_on_label = models.BooleanField(
        default=False,
    )

    min_mass_indicated = models.BooleanField(
        default=False,
    )

    grade = models.CharField(
        max_length=20,
        choices=Grade.choices,
        default=Grade.NA,
    )

    manufacture_date = models.DateField()

    expiry_date = models.DateField()

    chemical_label_photo = models.ImageField(
        upload_to="ca/chemical-labels/%Y/%m/",
        blank=True,
        help_text="Photo of the container label with the expiry date legible.",
    )

    storage_conditions = models.CharField(
        max_length=255,
    )

    net_weight = models.DecimalField(
        max_digits=12,
        decimal_places=3,
    )

    country_of_origin = CountryField(
        default="KE",
    )

    handling_instructions = models.TextField(
        blank=True,
    )

    is_oxidizing = models.BooleanField(
        default=False,
    )

    is_corrosive = models.BooleanField(
        default=False,
    )

    may_cause_fire = models.BooleanField(
        default=False,
    )

    contact_with_acids_liberates_toxic_gas = models.BooleanField(
        default=False,
    )

    causes_burns = models.BooleanField(
        default=False,
    )

    keep_out_of_reach_of_children = models.BooleanField(
        default=False,
    )

    contact_with_eyes_action = models.BooleanField(
        default=False,
    )

    fire_action_requires_respiratory_equipment = models.BooleanField(
        default=False,
    )

    sampled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chemical_checklists_sampled",
    )

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chemical_checklists_requested",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    completed = models.BooleanField(
        default=False,
        db_index=True,
    )

    completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="completed_chemical_checklists",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(completed=False, completed_at__isnull=True) | Q(completed=True, completed_at__isnull=False)),
                name="ca_checklist_completion_timestamp_consistent",
            ),
        ]
        indexes = [
            models.Index(
                fields=[
                    "chemical_analysis_request",
                    "-created_at",
                ],
                name="ca_check_req_created_idx",
            ),
            models.Index(
                fields=[
                    "completed",
                    "-created_at",
                ],
                name="ca_check_completed_idx",
            ),
            models.Index(
                fields=[
                    "chemical",
                    "-created_at",
                ],
                name="ca_check_chemical_created_idx",
            ),
            models.Index(
                fields=[
                    "expiry_date",
                    "completed",
                ],
                name="ca_check_expiry_completed_idx",
            ),
        ]

    @property
    def batch(self):
        request = self.chemical_analysis_request

        if request is None or not request.batch_id:
            return None

        return request.batch

    @property
    def sample_id(self):
        batch = self.batch
        return batch.sample_id if batch else None

    @property
    def batch_number(self):
        batch = self.batch
        return batch.batch_number if batch else None

    @property
    def treatment_plant(self):
        batch = self.batch
        return batch.treatment_plant if batch else None

    @property
    def analysis_status(self):
        request = self.chemical_analysis_request
        return request.status if request else None

    @property
    def is_expired(self) -> bool:
        if not self.expiry_date:
            return False

        return self.expiry_date < timezone.localdate()

    @property
    def is_valid_at_receipt(self):
        if not self.manufacture_date or not self.expiry_date:
            return False

        return (
            self.manufacture_date <= self.expiry_date
            and self.expiry_date >= timezone.localdate()
        )

    @property
    def is_completed(self):
        return self.completed

    @property
    def completion_status(self):
        return "COMPLETED" if self.completed else "PENDING"

    @property
    def days_until_expiry(self):
        if not self.expiry_date:
            return None

        return (
            self.expiry_date - timezone.localdate()
        ).days

    @property
    def expiry_status(self):
        if not self.expiry_date:
            return "UNKNOWN"

        if self.expiry_date < timezone.localdate():
            return "EXPIRED"

        if self.expiry_date == timezone.localdate():
            return "EXPIRES_TODAY"

        days = self.days_until_expiry

        if days is not None and days <= 30:
            return "EXPIRING_SOON"

        return "VALID"

    def _get_request(self):
        if not self.chemical_analysis_request_id:
            return None

        try:
            return (
                ChemicalAnalysisRequest.objects
                .select_related(
                    "batch",
                    "batch__chemical",
                    "batch__treatment_plant",
                    "batch__location",
                )
                .get(
                    pk=self.chemical_analysis_request_id
                )
            )
        except ChemicalAnalysisRequest.DoesNotExist:
            return None

    def _get_batch(self, request=None):
        request = request or self._get_request()

        if request is None or not request.batch_id:
            return None

        return request.batch

    def clean(self):
        super().clean()

        errors = {}

        request = self._get_request()
        batch = self._get_batch(request)

        if not self.chemical_analysis_request_id:
            errors["chemical_analysis_request"] = (
                "An analysis request is required."
            )
        elif request is None:
            errors["chemical_analysis_request"] = (
                "The selected analysis request could not be found."
            )

        if request is not None and not request.batch_id:
            errors["chemical_analysis_request"] = (
                "The analysis request must have an associated "
                "chemical batch."
            )

        if request is not None and batch is None:
            errors["chemical_analysis_request"] = (
                "The associated chemical batch could not be found."
            )

        if not self.chemical_id:
            errors["chemical"] = "A chemical is required."

        if batch is not None:
            if (
                self.chemical_id
                and batch.chemical_id
                and self.chemical_id != batch.chemical_id
            ):
                errors["chemical"] = (
                    "The checklist chemical must match the "
                    "chemical associated with the batch."
                )

            if (
                batch.location_id
                and batch.location.treatment_plant_id
                and batch.treatment_plant_id
                and batch.location.treatment_plant_id != batch.treatment_plant_id
            ):
                errors["chemical_analysis_request"] = (
                    "The batch location must belong "
                    "to the same treatment plant as the batch."
                )

        if (
            self.manufacture_date
            and self.expiry_date
            and self.expiry_date < self.manufacture_date
        ):
            errors["expiry_date"] = (
                "The expiry date cannot be earlier than "
                "the manufacture date."
            )

        if (
            self.net_weight is None
            or self.net_weight <= Decimal("0")
        ):
            errors["net_weight"] = (
                "Net weight must be greater than zero."
            )

        if (
            not isinstance(self.storage_conditions, str)
            or not self.storage_conditions.strip()
        ):
            errors["storage_conditions"] = (
                "Storage conditions are required."
            )

        if self.completed:
            if not self.completed_at:
                errors["completed_at"] = (
                    "A completion time is required for "
                    "a completed checklist."
                )

            if not self.completed_by_id:
                errors["completed_by"] = (
                    "A completing user is required for "
                    "a completed checklist."
                )
        else:
            if self.completed_at:
                errors["completed_at"] = (
                    "An incomplete checklist cannot have "
                    "a completion time."
                )

            if self.completed_by_id:
                errors["completed_by"] = (
                    "An incomplete checklist cannot have "
                    "a completing user."
                )

        if errors:
            raise ValidationError(errors)

    def validate_completion_requirements(
        self,
        request=None,
        batch=None,
    ):
        errors = {}

        request = request or self._get_request()

        if request is None:
            errors["chemical_analysis_request"] = (
                "The associated analysis request could not be found."
            )
        else:
            if not request.batch_id:
                errors["chemical_analysis_request"] = (
                    "The analysis request must have an associated "
                    "chemical batch."
                )

            if (
                request.status
                == ChemicalAnalysisRequest.Status.COMPLETED
            ):
                errors["chemical_analysis_request"] = (
                    "A checklist cannot be completed after the "
                    "analysis request has already been completed."
                )

        batch = batch or self._get_batch(request)

        if batch is None:
            errors["chemical_analysis_request"] = (
                "The analysis request must have an associated "
                "chemical batch."
            )

        if not self.chemical_id:
            errors["chemical"] = (
                "A chemical is required before completing "
                "the checklist."
            )

        if batch is not None:
            if (
                batch.chemical_id
                and self.chemical_id
                and batch.chemical_id != self.chemical_id
            ):
                errors["chemical"] = (
                    "The checklist chemical must match "
                    "the batch chemical."
                )

        if (
            self.manufacture_date
            and self.expiry_date
            and self.expiry_date < self.manufacture_date
        ):
            errors["expiry_date"] = (
                "The expiry date cannot be earlier than "
                "the manufacture date."
            )

        if (
            self.expiry_date
            and self.expiry_date < timezone.localdate()
        ):
            errors["expiry_date"] = (
                "The chemical batch has expired."
            )

        if not self.chemical_label_photo:
            errors["chemical_label_photo"] = (
                "Attach a photo of the chemical container label showing its expiry date."
            )

        if (
            self.net_weight is None
            or self.net_weight <= Decimal("0")
        ):
            errors["net_weight"] = (
                "Net weight must be greater than zero."
            )

        if (
            not isinstance(self.storage_conditions, str)
            or not self.storage_conditions.strip()
        ):
            errors["storage_conditions"] = (
                "Storage conditions are required."
            )

        if self.sampled_by_id is None:
            errors["sampled_by"] = (
                "A user who sampled the chemical is required."
            )

        if self.requested_by_id is None:
            errors["requested_by"] = (
                "The user who requested the analysis is required."
            )

        if errors:
            raise ValidationError(errors)

    def mark_completed(self, performed_by=None):
        if self.completed:
            raise ValidationError(
                "Checklist is already marked as completed."
            )

        if performed_by is None:
            raise ValidationError(
                "A user is required to complete the checklist."
            )

        if not getattr(
            performed_by,
            "is_authenticated",
            False,
        ):
            raise ValidationError(
                "The completing user must be authenticated."
            )

        if not getattr(
            performed_by,
            "is_active",
            False,
        ):
            raise ValidationError(
                "An inactive user cannot complete a checklist."
            )

        request = self._get_request()
        plant_id = request.batch.treatment_plant_id if request and request.batch_id else None
        require_ca_actor(
            performed_by,
            "ca.complete_receiving_checklist",
            plant_id=plant_id,
        )
        if request and not performed_by.is_superuser and not request.is_assigned_analyst(performed_by):
            raise PermissionDenied("Only an analyst assigned to this request may complete its receiving checklist.")

        with transaction.atomic():
            checklist = (
                type(self).objects
                # The request, batch location, and chemical relations may be
                # nullable. Lock only the checklist row to avoid PostgreSQL
                # attempting to lock rows on the nullable outer joins.
                .select_for_update(of=("self",))
                .select_related(
                    "chemical_analysis_request",
                    "chemical_analysis_request__batch",
                    "chemical_analysis_request__batch__location",
                    "chemical",
                )
                .get(pk=self.pk)
            )

            if checklist.completed:
                raise ValidationError(
                    "Checklist is already marked as completed."
                )

            request = checklist.chemical_analysis_request
            batch = checklist._get_batch(request)

            checklist.validate_completion_requirements(
                request=request,
                batch=batch,
            )

            now = timezone.now()

            checklist.completed = True
            checklist.completed_at = now
            checklist.completed_by = performed_by

            checklist.full_clean()

            type(self).objects.filter(
                pk=checklist.pk,
            ).update(
                completed=True,
                completed_at=now,
                completed_by_id=performed_by.pk,
                updated_at=now,
            )

            checklist_id = checklist.pk
            batch_id = batch.pk if batch else None
            from ca.services.outbox import enqueue_ca_event

            enqueue_ca_event(
                event_type="checklist_completed",
                aggregate_type="checklist",
                aggregate_id=checklist_id,
                idempotency_key=f"checklist:{checklist_id}:completed:{now.isoformat()}",
                payload={
                    "batch_id": batch_id,
                    "plant_id": batch.treatment_plant_id if batch else None,
                },
            )

            transaction.on_commit(
                lambda checklist_id=checklist_id: (
                    checklist_completed.send(
                        sender=type(self),
                        instance=type(self).objects.get(
                            pk=checklist_id,
                        ),
                        old_status="PENDING",
                        new_status="COMPLETED",
                        performed_by=performed_by,
                    )
                )
            )

        self.completed = True
        self.completed_at = now
        self.completed_by_id = performed_by.pk

        return self

    def reopen(self, performed_by=None):
        if not self.completed:
            raise ValidationError(
                "Checklist is not completed."
            )

        if performed_by is None:
            raise ValidationError(
                "A user is required to reopen the checklist."
            )

        if not getattr(
            performed_by,
            "is_authenticated",
            False,
        ):
            raise ValidationError(
                "The reopening user must be authenticated."
            )

        if not getattr(
            performed_by,
            "is_active",
            False,
        ):
            raise ValidationError(
                "An inactive user cannot reopen a checklist."
            )

        request = self._get_request()
        plant_id = request.batch.treatment_plant_id if request and request.batch_id else None
        require_ca_actor(
            performed_by,
            "ca.complete_receiving_checklist",
            plant_id=plant_id,
        )
        if request and not performed_by.is_superuser and not request.is_assigned_analyst(performed_by):
            raise PermissionDenied("Only an analyst assigned to this request may reopen its checklist.")

        with transaction.atomic():
            checklist = (
                type(self).objects
                .select_for_update()
                .get(pk=self.pk)
            )

            if not checklist.completed:
                raise ValidationError(
                    "Checklist is already open."
                )

            now = timezone.now()

            type(self).objects.filter(
                pk=checklist.pk,
            ).update(
                completed=False,
                completed_at=None,
                completed_by_id=None,
                updated_at=now,
            )
            from ca.services.outbox import enqueue_ca_event

            batch = request.batch if request and request.batch_id else None
            enqueue_ca_event(
                event_type="checklist_reopened",
                aggregate_type="checklist",
                aggregate_id=checklist.pk,
                idempotency_key=f"checklist:{checklist.pk}:reopened:{now.isoformat()}",
                payload={
                    "batch_id": batch.pk if batch else None,
                    "plant_id": batch.treatment_plant_id if batch else None,
                },
            )

        self.completed = False
        self.completed_at = None
        self.completed_by_id = None

        return self

    def save(self, *args, **kwargs):
        if self.pk and not getattr(self, "_allow_checklist_state_update", False):
            previous = type(self).objects.filter(pk=self.pk).values(
                "completed", "completed_at", "completed_by_id"
            ).first()
            if previous and (
                previous["completed"] != self.completed
                or previous["completed_at"] != self.completed_at
                or previous["completed_by_id"] != self.completed_by_id
            ):
                raise ValidationError("Checklist completion state can only change through its authorized workflow actions.")
        elif self._state.adding and self.completed:
            raise ValidationError("A new checklist must be completed through its authorized workflow action.")
        if self.completed:
            if not self.completed_at:
                self.completed_at = timezone.now()

            if not self.completed_by_id:
                raise ValidationError(
                    "A completed checklist must have "
                    "a completing user."
                )
        else:
            self.completed_at = None
            self.completed_by_id = None

        self.full_clean()

        return super().save(
            *args,
            **kwargs,
        )

    def __str__(self):
        chemical_name = (
            self.chemical.name
            if self.chemical_id
            else "Unknown Chemical"
        )

        batch_number = self.batch_number or "N/A"

        return (
            f"Checklist - {chemical_name} "
            f"({batch_number})"
        )

class ChemicalAnalysisProcess(Process):
    class WorkflowStatus(models.TextChoices):
        CREATED = "CREATED", "Created"
        RECEIVED = "RECEIVED", "Received"
        REQUESTED = "REQUESTED", "Requested"
        ASSIGNED = "ASSIGNED", "Assigned"
        CHECKLIST = "CHECKLIST", "Checklist"
        ANALYSIS = "ANALYSIS", "Analysis"
        RETEST = "RETEST", "Retest"
        QA = "QA", "Chemist Review"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        UTILIZED = "UTILIZED", "Utilized"
        RETURNED = "RETURNED", "Returned"
        COMPLETED = "COMPLETED", "Completed"

    workflow_transitions = {
        WorkflowStatus.CREATED: {WorkflowStatus.RECEIVED},
        WorkflowStatus.RECEIVED: {
            WorkflowStatus.REQUESTED,
            WorkflowStatus.ASSIGNED,
            WorkflowStatus.CHECKLIST,
        },
        WorkflowStatus.REQUESTED: {WorkflowStatus.CHECKLIST, WorkflowStatus.ASSIGNED},
        WorkflowStatus.ASSIGNED: {WorkflowStatus.CHECKLIST, WorkflowStatus.ANALYSIS},
        WorkflowStatus.CHECKLIST: {WorkflowStatus.ASSIGNED, WorkflowStatus.ANALYSIS},
        WorkflowStatus.ANALYSIS: {WorkflowStatus.QA},
        WorkflowStatus.RETEST: {WorkflowStatus.QA},
        WorkflowStatus.QA: {WorkflowStatus.RETEST},
        WorkflowStatus.APPROVED: set(),
        WorkflowStatus.REJECTED: set(),
        WorkflowStatus.UTILIZED: set(),
        WorkflowStatus.RETURNED: set(),
        WorkflowStatus.COMPLETED: set(),
    }

    batch = models.OneToOneField(
        "ChemicalBatch",
        on_delete=models.CASCADE,
        related_name="analysis_process",
    )

    analysis_request = models.OneToOneField(
        "ChemicalAnalysisRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="analysis_process",
    )

    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="initiated_chemical_analysis_processes",
    )

    assigned_analyst = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="performed_chemical_analysis_processes",
    )

    qa_officer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="qa_chemical_analysis_processes",
    )

    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_chemical_analysis_processes",
    )

    started_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    checklist_completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    analysis_started_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    analysis_completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    qa_reviewed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    approved_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    rejected_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    rejection_reason = models.CharField(
        max_length=1000,
        blank=True,
    )

    completed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    rework_count = models.PositiveIntegerField(
        default=0,
    )

    retest_parameter = models.ForeignKey(
        "ChemicalSpecification",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="chemical_analysis_retests",
    )
    retest_reason = models.CharField(max_length=1000, blank=True)

    @property
    def escalation_count(self):
        return 0

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["-created_at"]
        permissions = [
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
        ]
        indexes = [

            models.Index(
                fields=["assigned_analyst", "analysis_started_at"],
                name="ca_proc_analyst_start_idx",
            ),
            models.Index(
                fields=["qa_officer", "qa_reviewed_at"],
                name="ca_proc_qa_review_idx",
            ),
            models.Index(
                fields=["approver", "approved_at"],
                name="ca_proc_approver_idx",
            ),
            models.Index(
                fields=["analysis_started_at", "analysis_completed_at"],
                name="ca_proc_analysis_period_idx",
            ),
            models.Index(
                fields=["completed_at"],
                name="ca_proc_completed_idx",
            ),
        ]

    @property
    def chemical(self):
        return self.batch.chemical if self.batch_id else None

    @property
    def supplier(self):
        return self.batch.supplier if self.batch_id else None

    @property
    def treatment_plant(self):
        return self.batch.treatment_plant if self.batch_id else None

    @property
    def sample_id(self):
        return self.batch.sample_id if self.batch_id else None

    @property
    def batch_number(self):
        return self.batch.batch_number if self.batch_id else None

    @property
    def test_results(self):
        if not self.batch_id:
            return BatchTestResult.objects.none()

        return self.batch.test_results.all()

    def required_specifications(self):
        if not self.batch_id or not self.batch.chemical_id:
            return ChemicalSpecification.objects.none()
        if self.analysis_request_id:
            return self.analysis_request.get_required_specifications()
        return ChemicalSpecification.current_for(
            ChemicalSpecification.objects
            .filter(
                chemical_id=self.batch.chemical_id,
                treatment_plant_id=self.batch.treatment_plant_id,
                is_required=True,
            )
            .select_related(
                "chemical",
                "method",
            )
            .prefetch_related("remarks")
        )

    @property
    def pending_tests(self):
        if not self.batch_id:
            return BatchTestResult.objects.none()

        return (
            self.batch.test_results
            .filter(
                Q(status__isnull=True) | Q(status="")
            )
            .select_related(
                "parameter",
                "parameter__chemical",
            )
            .order_by(
                "parameter__parameter_name",
                "id",
            )
        )

    @property
    def completed_tests(self):
        if not self.batch_id:
            return BatchTestResult.objects.none()

        return (
            self.batch.test_results
            .exclude(
                Q(status__isnull=True) | Q(status="")
            )
            .select_related(
                "parameter",
                "parameter__chemical",
            )
            .order_by(
                "parameter__parameter_name",
                "id",
            )
        )

    @property
    def failed_tests(self):
        if not self.batch_id:
            return BatchTestResult.objects.none()

        return (
            self.batch.test_results
            .filter(
                status=BatchTestResult.Status.FAIL
            )
            .select_related(
                "parameter",
                "parameter__chemical",
            )
            .order_by(
                "parameter__parameter_name",
                "id",
            )
        )

    @property
    def has_pending_tests(self):
        return self.pending_tests.exists()

    @property
    def next_test(self):
        return self.pending_tests.first()

    @property
    def pending_inputs(self):
        if not self.batch_id:
            return ChemicalAnalysisInput.objects.none()

        return (
            ChemicalAnalysisInput.objects
            .filter(
                test_result__batch_id=self.batch_id
            )
            .filter(
                Q(calculated_value__isnull=True)
                | Q(inputs={})
            )
            .select_related(
                "test_result",
                "test_result__parameter",
            )
        )

    @property
    def required_test_count(self):
        return self.required_specifications().count()

    @property
    def completed_test_count(self):
        required_ids = self.required_specifications().values_list(
            "pk",
            flat=True,
        )

        if not required_ids.exists():
            return self.completed_tests.count()

        return (
            self.batch.test_results
            .filter(
                parameter_id__in=required_ids
            )
            .exclude(
                Q(status__isnull=True) | Q(status="")
            )
            .values("parameter_id")
            .distinct()
            .count()
        )

    @property
    def passed_test_count(self):
        if not self.batch_id:
            return 0

        return self.batch.test_results.filter(
            status=BatchTestResult.Status.PASS
        ).count()

    @property
    def analysis_progress(self):
        required = self.required_test_count

        if required == 0:
            return 100

        return round(
            min(
                self.completed_test_count,
                required,
            ) / required * 100,
            2,
        )

    @property
    def is_analysis_complete(self):
        if not self.batch_id:
            return False

        required = self.required_specifications()

        if not required.exists():
            return self.batch.test_results.exists()

        completed_ids = (
            self.batch.test_results
            .exclude(
                Q(status__isnull=True) | Q(status="")
            )
            .values_list(
                "parameter_id",
                flat=True,
            )
        )

        return not required.exclude(
            pk__in=completed_ids
        ).exists()

    @property
    def is_overdue(self):
        return bool(
            self.analysis_request_id
            and self.analysis_request.is_overdue
        )

    @property
    def analysis_ready(self):
        if not self.analysis_request_id:
            return False
        if self.batch_id and self.batch.has_active_holds:
            return False

        checklist = (
            self.analysis_request.checklists
            .filter(completed=True)
            .order_by("-completed_at", "-pk")
            .first()
        )

        return bool(checklist)

    @property
    def assigned_analysts(self):
        if not self.analysis_request_id:
            return get_user_model().objects.none()

        return self.analysis_request.assigned_analysts.all()

    @property
    def has_assigned_analysts(self):
        return self.assigned_analysts.exists()

    def is_authorized_analyst(self, user):
        if (
            user is None
            or not getattr(user, "is_active", False)
            or not self.analysis_request_id
            or not self.analysis_request.is_assigned_analyst(user)
        ):
            return False
        if not all(user.has_perm(permission) for permission in (
            "ca.start_chemical_analysis",
            "ca.record_chemical_result",
            "ca.complete_receiving_checklist",
            "ca.complete_chemical_analysis",
        )):
            return False
        if (
            not getattr(user, "is_superuser", False)
            and self.batch.treatment_plant_id
            and getattr(user, "assigned_treatment_plant_id", None) != self.batch.treatment_plant_id
        ):
            return False
        return self.analysis_request.eligible_analysts(
            get_user_model().objects.filter(pk=user.pk)
        ).exists()

    def refresh_assigned_analysts(self):
        if not self.analysis_request_id:
            return get_user_model().objects.none()

        return self.analysis_request.assigned_analysts.all()

    @property
    def performing_analyst_ids(self):
        results = self.test_results.exclude(analyst__isnull=True)
        ids = set(results.values_list("analyst_id", flat=True))
        if not ids and self.analysis_request_id:
            ids = set(self.assigned_analysts.values_list("pk", flat=True))
        return ids

    def is_analysis_performer(self, user):
        """Return whether this user performed, or is assigned to, this batch analysis."""
        return bool(
            user is not None
            and getattr(user, "is_authenticated", False)
            and user.pk in self.performing_analyst_ids
        )

    def is_independent_approver(self, user):
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if self.qa_officer_id and user.pk == self.qa_officer_id:
            return False
        if user.pk in self.performing_analyst_ids:
            return False
        return True

    def _lock_for_transition(self):
        """Refresh this process while holding its row lock before a transition."""
        if not self.pk:
            raise ValidationError("A saved chemical analysis process is required for a transition.")
        type(self).objects.select_for_update().get(pk=self.pk)
        self.refresh_from_db()
        if self.batch_id:
            self.batch.refresh_from_db()
        if self.analysis_request_id:
            self.analysis_request.refresh_from_db()
        return self

    def _assert_actor_plant_scope(self, actor):
        if (
            self.batch_id
            and self.batch.treatment_plant_id
            and not getattr(actor, "is_superuser", False)
            and getattr(actor, "assigned_treatment_plant_id", None) != self.batch.treatment_plant_id
        ):
            raise ValidationError("You cannot advance a workflow outside your treatment-plant scope.")

    def _save_workflow_transition(self, update_fields):
        self._allow_workflow_transition = True
        try:
            self.save(update_fields=update_fields)
        finally:
            self.__dict__.pop("_allow_workflow_transition", None)

    @transaction.atomic
    def transition_to(
        self,
        target_status,
        *,
        performed_by=None,
        extra_fields=None,
        _retest_result=None,
        auto=False,
    ):
        """Apply a guarded intermediate workflow transition.

        Terminal and quality decisions use the specialized methods below so
        their inventory, request, QA and audit gates cannot be skipped.
        """
        self._lock_for_transition()
        target_status = str(target_status or "").upper()
        if target_status not in self.WorkflowStatus.values:
            raise ValidationError(f"Unknown chemical workflow status: {target_status}.")
        old_status = self.status
        if target_status == old_status:
            raise ValidationError(f"Chemical workflow is already {old_status}.")
        if target_status not in self.workflow_transitions.get(old_status, set()):
            raise ValidationError(f"Invalid chemical workflow transition: {old_status} → {target_status}.")
        if auto:
            auto_assignment = (
                target_status == self.WorkflowStatus.ASSIGNED
                and old_status in {
                    self.WorkflowStatus.RECEIVED,
                    self.WorkflowStatus.REQUESTED,
                    self.WorkflowStatus.CHECKLIST,
                }
                and self.has_assigned_analysts
            )
            auto_receipt_sync = (
                target_status == self.WorkflowStatus.RECEIVED
                and old_status == self.WorkflowStatus.CREATED
                and self.analysis_request_id
                and self.batch_id
                and self.batch.status == ChemicalBatch.Status.RECEIVED
            )
            auto_checklist_sync = (
                target_status == self.WorkflowStatus.CHECKLIST
                and old_status in {
                    self.WorkflowStatus.RECEIVED,
                    self.WorkflowStatus.REQUESTED,
                    self.WorkflowStatus.ASSIGNED,
                }
                and self.analysis_request_id
                and self.analysis_request.checklists.filter(completed=True).exists()
            )
            if not (auto_assignment or auto_receipt_sync or auto_checklist_sync):
                raise ValidationError("This automatic workflow status synchronization is not valid for the saved batch state.")
        if not auto and (performed_by is None or not getattr(performed_by, "is_active", False)):
            raise ValidationError("An active user is required to advance the chemical workflow.")

        permission_for_status = {
            self.WorkflowStatus.RECEIVED: "ca.receive_chemical_batch",
            self.WorkflowStatus.REQUESTED: "ca.create_analysis_request",
            self.WorkflowStatus.ASSIGNED: "ca.create_analysis_request",
            self.WorkflowStatus.CHECKLIST: "ca.complete_receiving_checklist",
            self.WorkflowStatus.ANALYSIS: "ca.start_chemical_analysis",
            self.WorkflowStatus.RETEST: "ca.perform_chemical_retest",
            self.WorkflowStatus.QA: "ca.complete_chemical_analysis",
        }
        permission = (
            "ca.perform_chemical_retest"
            if old_status == self.WorkflowStatus.RETEST and target_status == self.WorkflowStatus.QA
            else permission_for_status.get(target_status)
        )
        if permission and not auto and not (performed_by.is_superuser or performed_by.has_perm(permission)):
            raise ValidationError("You do not have permission to advance the workflow to this stage.")
        if performed_by is not None:
            self._assert_actor_plant_scope(performed_by)
        if self.batch_id and self.batch.has_active_holds and target_status not in {
            self.WorkflowStatus.RECEIVED,
            self.WorkflowStatus.ASSIGNED,
            self.WorkflowStatus.REJECTED,
        }:
            raise ValidationError("Resolve all intake holds before advancing the chemical workflow.")

        if target_status == self.WorkflowStatus.REQUESTED and not self.analysis_request_id:
            raise ValidationError("Create the analysis request before advancing to analyst assignment.")
        if target_status == self.WorkflowStatus.ASSIGNED and not self.has_assigned_analysts:
            raise ValidationError("Assign at least one qualified analyst before advancing.")
        if target_status == self.WorkflowStatus.CHECKLIST:
            if not self.analysis_request_id or not self.analysis_request.checklists.filter(completed=True).exists():
                raise ValidationError("Complete a receiving checklist before advancing to the checklist stage.")
        if target_status == self.WorkflowStatus.ANALYSIS:
            if not self.analysis_ready or not self.is_authorized_analyst(performed_by):
                raise ValidationError("Only an assigned analyst may start a batch with a completed checklist and no active holds.")
        fields = dict(extra_fields or {})
        if target_status == self.WorkflowStatus.RETEST:
            parameter = fields.get("retest_parameter", self.retest_parameter)
            reason = str(fields.get("retest_reason", self.retest_reason) or "").strip()
            if parameter is None or not reason:
                raise ValidationError("Select a required parameter and provide a reason before requesting a retest.")
            if not self.required_specifications().filter(pk=parameter.pk).exists():
                raise ValidationError("The retest parameter must belong to this analysis request's required specifications.")

        if target_status == self.WorkflowStatus.QA:
            if not self.is_analysis_complete or self.has_pending_tests:
                raise ValidationError("Finalize every required analytical result before chemist review.")
            if old_status == self.WorkflowStatus.RETEST:
                retest_result = (
                    BatchTestResult.objects.select_for_update()
                    .filter(pk=getattr(_retest_result, "pk", None), batch_id=self.batch_id)
                    .first()
                )
                if (
                    retest_result is None
                    or not retest_result.status
                    or not self.required_specifications().filter(pk=retest_result.parameter_id).exists()
                    or not self.is_authorized_analyst(performed_by)
                ):
                    raise ValidationError("Use a finalized required retest result from an assigned analyst to return the process to QA.")

        protected = {"status", "pk", "id", "batch"}
        if protected.intersection(fields):
            raise ValidationError("A workflow transition cannot overwrite identity or status fields.")
        for field, value in fields.items():
            if field not in {field.name for field in self._meta.concrete_fields}:
                raise ValidationError(f"Unknown workflow field: {field}.")
            setattr(self, field, value)
        self.status = target_status
        self._save_workflow_transition(["status", *fields.keys(), "updated_at"])
        return self

    @transaction.atomic
    def request_retest(self, *, parameter, reason, performed_by):
        self._lock_for_transition()
        if self.status != self.WorkflowStatus.QA:
            raise ValidationError("Retesting can only be requested during chemist review.")
        if performed_by is None or not self.is_authorized_analyst(performed_by):
            raise ValidationError("Only an assigned active analyst can request the retest.")
        reason = str(reason or "").strip()
        if parameter is None or not reason:
            raise ValidationError("A required analytical parameter and retest reason are required.")
        if not self.required_specifications().filter(pk=parameter.pk).exists():
            raise ValidationError("The selected parameter is not required for this analysis.")
        return self.transition_to(
            self.WorkflowStatus.RETEST,
            performed_by=performed_by,
            extra_fields={
                "rework_count": self.rework_count + 1,
                "retest_parameter": parameter,
                "retest_reason": reason,
            },
        )

    @transaction.atomic
    def mark_retest_completed(self, *, performed_by, result):
        self._lock_for_transition()
        if self.status != self.WorkflowStatus.RETEST:
            raise ValidationError("The analysis is not currently in the retest stage.")
        if performed_by is None or not self.is_authorized_analyst(performed_by):
            raise ValidationError("Only an assigned active analyst can complete the retest.")
        if result is None or result.batch_id != self.batch_id or not result.status:
            raise ValidationError("A finalized retest result for this chemical batch is required.")
        if result.parameter_id != self.retest_parameter_id or not self.required_specifications().filter(pk=result.parameter_id).exists():
            raise ValidationError("The retest result must match a required analysis parameter.")
        return self.transition_to(
            self.WorkflowStatus.QA,
            performed_by=performed_by,
            _retest_result=result,
        )

    def ensure_result_for_parameter(
        self,
        parameter,
        *,
        analyst=None,
    ):
        if not self.batch_id:
            raise ValidationError(
                "An analysis batch is required."
            )

        if parameter is None:
            raise ValidationError(
                "An analysis parameter is required."
            )

        if parameter.chemical_id != self.batch.chemical_id:
            raise ValidationError(
                "The analysis parameter does not belong "
                "to the batch chemical."
            )

        if analyst is not None and not self.is_authorized_analyst(
            analyst
        ):
            raise ValidationError(
                "The analyst recording the result must be "
                "assigned to this analysis."
            )

        result, created = BatchTestResult.objects.get_or_create(
            batch=self.batch,
            parameter=parameter,
            defaults={
                "analyst": analyst,
            },
        )

        if (
            not created
            and analyst is not None
            and result.analyst_id is None
        ):
            result.analyst = analyst
            result.save(
                update_fields=["analyst"]
            )

        return result

    @transaction.atomic
    def move_to_analysis(self, *, performed_by):
        """Advance directly from completed checklist to analysis.

        There is deliberately no random/systematic sampling stage in the
        chemical-analysis workflow.
        """
        self._lock_for_transition()
        if self.status not in {
            self.WorkflowStatus.CHECKLIST,
            self.WorkflowStatus.ASSIGNED,
        }:
            raise ValidationError(
                f"Analysis cannot start from status {self.status}."
            )

        if not self.batch_id or not self.analysis_request_id:
            raise ValidationError(
                "A chemical batch and analysis request are required."
            )

        if not self.has_assigned_analysts:
            raise ValidationError(
                "At least one analyst must be assigned before analysis."
            )

        if self.batch.has_active_holds:
            raise ValidationError("Resolve all intake holds before analysis can start.")

        checklist = (
            self.analysis_request.checklists
            .filter(completed=True)
            .order_by("-completed_at", "-pk")
            .first()
        )

        if checklist is None:
            raise ValidationError(
                "A completed receiving checklist is required."
            )

        now = timezone.now()
        self.checklist_completed_at = (
            self.checklist_completed_at
            or checklist.completed_at
            or now
        )
        self.transition_to(
            self.WorkflowStatus.ANALYSIS,
            performed_by=performed_by,
            extra_fields={"checklist_completed_at": self.checklist_completed_at},
        )

        return self

    @transaction.atomic
    def mark_analysis_started(
        self,
        performed_by=None,
    ):
        self._lock_for_transition()
        if self.status != self.WorkflowStatus.ANALYSIS:
            raise ValidationError(
                "Analysis can only be started from the analysis stage."
            )

        if not self.analysis_ready:
            raise ValidationError(
                "A completed receiving checklist is required before analysis."
            )

        if (
            performed_by is None
            or not self.is_authorized_analyst(performed_by)
        ):
            raise ValidationError(
                "Only an assigned analyst can start this analysis."
            )

        request = self.analysis_request
        if request.status == ChemicalAnalysisRequest.Status.PENDING:
            request.start_analysis(
                performed_by=performed_by
            )
        elif request.status != ChemicalAnalysisRequest.Status.IN_PROGRESS:
            raise ValidationError(
                "Only a pending analysis request can start chemical analysis."
            )

        now = timezone.now()

        self.status = self.WorkflowStatus.ANALYSIS
        self.started_at = self.started_at or now
        self.analysis_started_at = (
            self.analysis_started_at
            or now
        )
        self.assigned_analyst = performed_by

        self._save_workflow_transition(
            [
                "status",
                "started_at",
                "analysis_started_at",
                "assigned_analyst",
                "updated_at",
            ]
        )

        return self

    @transaction.atomic
    def mark_analysis_completed(
        self,
        performed_by=None,
    ):
        self._lock_for_transition()
        if self.status != self.WorkflowStatus.ANALYSIS:
            raise ValidationError(
                "Chemical analysis is not currently in progress."
            )

        if performed_by is None or not self.is_authorized_analyst(performed_by):
            raise ValidationError("Only an assigned active analyst can complete the analysis.")

        if self.assigned_analyst_id and self.assigned_analyst_id != performed_by.pk:
            raise ValidationError("The analysis must be completed by the analyst who started it.")

        if (
            not self.is_analysis_complete
            or self.has_pending_tests
        ):
            raise ValidationError(
                "All required analytical results "
                "must be finalized."
            )

        now = timezone.now()

        if self.analysis_request_id:
            request = self.analysis_request

            if request.status == (
                ChemicalAnalysisRequest.Status.IN_PROGRESS
            ):
                request.mark_completed(
                    performed_by=performed_by
                )

        # Keep the inventory batch lifecycle in step with the process. Without
        # this transition, the chemist review approval (ANALYSED -> APPROVED) cannot
        # succeed because newly received batches remain in RECEIVED state.
        if self.batch_id:
            self.batch.refresh_from_db()
            if self.batch.status == ChemicalBatch.Status.RECEIVED:
                self.batch.transition_to(
                    ChemicalBatch.Status.ANALYSED,
                    performed_by=performed_by,
                )

        self.transition_to(
            self.WorkflowStatus.QA,
            performed_by=performed_by,
            extra_fields={"analysis_completed_at": now},
        )

        return self

    @transaction.atomic
    def mark_approved(self, approved_by):
        self._lock_for_transition()
        if self.status != self.WorkflowStatus.QA:
            raise ValidationError("Chemical analysis must be in chemist review before approval.")

        if approved_by is None:
            raise ValidationError(
                "An approving user is required."
            )

        if not getattr(approved_by, "is_active", False):
            raise ValidationError("An active chemist reviewer is required to approve the batch.")
        self._assert_actor_plant_scope(approved_by)

        if not self.analysis_completed_at:
            raise ValidationError(
                "Chemical analysis must be completed before approval."
            )

        if self.batch.has_active_holds:
            raise ValidationError("An intake hold prevents batch approval.")

        if not self.qa_officer_id or not self.qa_reviewed_at:
            raise ValidationError("A chemist review is required before the batch can be approved.")
        qa_officer = self.qa_officer
        if not qa_officer.is_active or not (
            qa_officer.is_superuser or qa_officer.has_perm("ca.review_chemical_analysis")
        ):
            raise ValidationError("The recorded chemist reviewer is no longer authorized to review chemical analysis.")
        self._assert_actor_plant_scope(qa_officer)
        is_chemist_reviewer = self.qa_officer_id == approved_by.pk
        if is_chemist_reviewer:
            if self.is_analysis_performer(approved_by) or not (
                approved_by.is_superuser or approved_by.has_perm("ca.review_chemical_analysis")
            ):
                raise ValidationError("The chemist approving this batch must be authorized and independent of its analysis.")
        elif not (
            approved_by.is_superuser or approved_by.has_perm("ca.approve_chemical_batch")
        ) or not self.is_independent_approver(approved_by):
            raise ValidationError("Batch approval must be performed by the recorded chemist reviewer or an authorized legacy approver.")

        if ChemicalCorrectiveAction.objects.filter(
            process_id=self.pk,
            status=ChemicalCorrectiveAction.Status.OPEN,
        ).exists():
            raise ValidationError("Open corrective actions must be reviewed and verified before approval.")

        if (
            self.has_pending_tests
            or self.failed_tests.exists()
        ):
            raise ValidationError(
                "Pending or failed analytical results prevent approval."
            )

        now = timezone.now()

        self.approver = approved_by
        self.approved_at = now
        self.status = self.WorkflowStatus.APPROVED

        self.batch.transition_to(
            self.batch.Status.APPROVED,
            performed_by=approved_by,
            permission=("ca.review_chemical_analysis" if is_chemist_reviewer else None),
        )

        self._save_workflow_transition(
            [
                "status",
                "qa_officer",
                "qa_reviewed_at",
                "approver",
                "approved_at",
                "updated_at",
            ]
        )

        return self

    @transaction.atomic
    def mark_rejected(self, *, performed_by, reason, permission="ca.reject_chemical_batch"):
        self._lock_for_transition()
        allowed_statuses = {
            self.WorkflowStatus.QA,
            self.WorkflowStatus.RETEST,
            self.WorkflowStatus.CREATED,
            self.WorkflowStatus.RECEIVED,
            self.WorkflowStatus.REQUESTED,
            self.WorkflowStatus.ASSIGNED,
            self.WorkflowStatus.CHECKLIST,
            self.WorkflowStatus.ANALYSIS,
        }

        if self.status not in allowed_statuses:
            raise ValidationError(
                f"Chemical process cannot be rejected "
                f"from {self.status}."
            )

        reason = str(reason or "").strip()
        if not reason:
            raise ValidationError("A rejection reason is required.")
        if performed_by is None or not getattr(performed_by, "is_active", False):
            raise ValidationError("An active user is required to reject this batch.")
        if permission not in {"ca.reject_chemical_batch", "ca.resolve_chemical_batch_hold"}:
            raise ValidationError("Unsupported permission for chemical batch rejection.")
        if not (performed_by.is_superuser or performed_by.has_perm(permission)):
            raise ValidationError("You do not have permission to reject this batch.")
        self._assert_actor_plant_scope(performed_by)

        if permission == "ca.resolve_chemical_batch_hold" and not self.batch.workflow_holds.filter(
            status=ChemicalBatchHold.Status.REJECTED,
            resolved_by=performed_by,
            resolution=reason,
        ).exists():
            raise ValidationError("A hold must be resolved with rejection before the batch can be rejected under hold authority.")

        self.status = self.WorkflowStatus.REJECTED
        self.rejected_at = timezone.now()
        self.rejection_reason = reason

        if self.batch_id:
            self.batch.transition_to(
                self.batch.Status.REJECTED,
                performed_by=performed_by,
                permission=permission,
            )

        self._save_workflow_transition(
            [
                "status",
                "rejected_at",
                "rejection_reason",
                "updated_at",
            ]
        )

        return self

    @transaction.atomic
    def mark_disposition_completed(
        self,
        disposition,
        *,
        performed_by=None,
    ):
        self._lock_for_transition()
        disposition = str(
            disposition or ""
        ).strip().upper()

        valid_dispositions = {
            self.WorkflowStatus.UTILIZED,
            self.WorkflowStatus.RETURNED,
        }

        if disposition not in valid_dispositions:
            raise ValidationError(
                "Invalid batch disposition."
            )

        allowed_source_statuses = {self.WorkflowStatus.APPROVED}
        if disposition == self.WorkflowStatus.RETURNED:
            allowed_source_statuses.add(self.WorkflowStatus.REJECTED)

        if self.status not in allowed_source_statuses:
            raise ValidationError(
                "Batch disposition requires an approved analysis, or a rejected batch being returned."
            )

        if not self.batch_id:
            raise ValidationError(
                "A chemical batch is required."
            )

        required_permission = {
            self.WorkflowStatus.UTILIZED: "ca.utilize_chemical_batch",
            self.WorkflowStatus.RETURNED: "ca.return_chemical_batch",
        }[disposition]
        if performed_by is None or not getattr(performed_by, "is_active", False):
            raise ValidationError("An active user is required to record the batch disposition.")
        if not (performed_by.is_superuser or performed_by.has_perm(required_permission)):
            raise ValidationError("You do not have permission to record this batch disposition.")
        self._assert_actor_plant_scope(performed_by)

        if self.batch.status != disposition:
            self.batch.transition_to(disposition, performed_by=performed_by)
        self.status = disposition

        self._save_workflow_transition(
            [
                "status",
                "updated_at",
            ]
        )

        return self

    @transaction.atomic
    def mark_completed(self, *, performed_by):
        self._lock_for_transition()
        if self.status not in {
            self.WorkflowStatus.UTILIZED,
            self.WorkflowStatus.RETURNED,
        }:
            raise ValidationError(
                "The process can only be completed "
                "after batch disposition."
            )

        if performed_by is None or not getattr(performed_by, "is_active", False):
            raise ValidationError("An active user is required to close the workflow.")
        self._assert_actor_plant_scope(performed_by)
        required_permission = (
            "ca.utilize_chemical_batch"
            if self.status == self.WorkflowStatus.UTILIZED
            else "ca.return_chemical_batch"
        )
        if not (performed_by.is_superuser or performed_by.has_perm(required_permission)):
            raise ValidationError("You do not have permission to close this chemical workflow.")

        self.status = self.WorkflowStatus.COMPLETED
        self.completed_at = timezone.now()

        self._save_workflow_transition(
            [
                "status",
                "completed_at",
                "updated_at",
            ]
        )

        return self

    def clean(self):
        super().clean()

        errors = {}

        if not self.batch_id:
            errors["batch"] = (
                "A chemical batch is required."
            )

        if (
            self.analysis_request_id
            and self.batch_id
            and self.analysis_request.batch_id
            != self.batch_id
        ):
            errors["analysis_request"] = (
                "The analysis request must belong "
                "to this chemical batch."
            )

        if (
            self.assigned_analyst_id
            and self.analysis_request_id
            and not self.analysis_request.assigned_analysts.filter(
                pk=self.assigned_analyst_id
            ).exists()
        ):
            errors["assigned_analyst"] = (
                "The assigned analyst must belong "
                "to the analysis request."
            )

        if (
            self.status == self.WorkflowStatus.ANALYSIS
            and not self.analysis_started_at
        ):
            errors["analysis_started_at"] = (
                "Analysis start time is required."
            )

        if (
            self.status in {
                self.WorkflowStatus.RETEST,
                self.WorkflowStatus.QA,
            }
            and not self.analysis_completed_at
        ):
            errors["analysis_completed_at"] = (
                "Original analysis must be completed first."
            )

        if self.status == self.WorkflowStatus.APPROVED:
            if not self.approver_id:
                errors["approver"] = (
                    "An approver is required."
                )

            if not self.approved_at:
                errors["approved_at"] = (
                    "Approval time is required."
                )

        if self.status == self.WorkflowStatus.REJECTED:
            if not self.rejected_at:
                errors["rejected_at"] = (
                    "Rejection time is required."
                )

        if self.status == self.WorkflowStatus.COMPLETED:
            if not self.completed_at:
                errors["completed_at"] = (
                    "Completion time is required."
                )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk and not getattr(self, "_allow_workflow_transition", False):
            persisted_status = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if persisted_status is not None and persisted_status != self.status:
                raise ValidationError("Chemical workflow status can only change through its authorized transition actions.")
        now = timezone.now()

        if (
            self.status == self.WorkflowStatus.ANALYSIS
            and not self.analysis_started_at
        ):
            self.analysis_started_at = now

        if (
            self.status == self.WorkflowStatus.APPROVED
            and not self.approved_at
        ):
            self.approved_at = now

        if (
            self.status == self.WorkflowStatus.REJECTED
            and not self.rejected_at
        ):
            self.rejected_at = now

        if (
            self.status == self.WorkflowStatus.COMPLETED
            and not self.completed_at
        ):
            self.completed_at = now

        self.clean()

        return super().save(
            *args,
            **kwargs,
        )

    def __str__(self):
        return (
            f"Chemical Analysis Process - "
            f"{self.sample_id or self.batch_id or 'N/A'} "
            f"[{self.status}]"
        )


class BatchTestResult(models.Model):
    class Status(models.TextChoices):
        PASS = 'PASS', 'Pass'
        FAIL = 'FAIL', 'Fail'
        NA = 'NA', 'Not Applicable'

    batch = models.ForeignKey(ChemicalBatch, on_delete=models.CASCADE, related_name='test_results')
    parameter = models.ForeignKey(ChemicalSpecification, on_delete=models.SET_NULL, null=True)
    value = models.DecimalField(max_digits=26, decimal_places=12, null=True, blank=True)
    textual_result = models.CharField(max_length=100, blank=True)

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        null=True,
        blank=True,
        db_index=True,
    )
    tested_at = models.DateTimeField(default=timezone.now)
    analyst = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="performed_tests",
    )
    instrument = models.ForeignKey(
        "instruments.Instrument",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chemical_batch_results",
    )
    inventory_equipment = models.ForeignKey(
        "inventory.Equipment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="chemical_batch_results",
        help_text="Laboratory equipment selected from the Inventory register.",
    )
    instrument_calibration = models.ForeignKey(
        "instruments.InstrumentCalibration",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chemical_batch_results",
        editable=False,
    )
    reference_standard_name = models.CharField(max_length=180, blank=True)
    reference_standard_lot = models.CharField(max_length=120, blank=True)
    reagent_lot_numbers = models.CharField(max_length=500, blank=True)
    expanded_uncertainty = models.DecimalField(max_digits=24, decimal_places=12, null=True, blank=True)
    uncertainty_coverage_factor = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    instrument_snapshot = models.JSONField(default=dict, blank=True, editable=False)
    specification_snapshot = models.JSONField(default=dict, blank=True, editable=False)
    method_snapshot = models.JSONField(default=dict, blank=True, editable=False)
    quality_flags = models.JSONField(default=list, blank=True)
    revision_number = models.PositiveIntegerField(default=1, editable=False)
    revision_reason = models.CharField(max_length=500, blank=True)
    auto_remark = models.TextField(blank=True)

    class Meta:
        ordering = ["parameter__parameter_name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "parameter"],
                name="ca_unique_batch_parameter_result",
            ),
            models.CheckConstraint(
                condition=Q(status__isnull=True) | Q(status__in=["PASS", "FAIL", "NA"]),
                name="ca_result_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(revision_number__gte=1),
                name="ca_result_revision_positive",
            ),
        ]
        indexes = [
            models.Index(fields=["batch", "status"], name="ca_result_batch_status_idx"),
            models.Index(fields=["analyst", "tested_at"], name="ca_result_analyst_tested_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        flags = set(self.quality_flags or [])
        flags.difference_update({"OUT_OF_SPEC_LOW", "OUT_OF_SPEC_HIGH", "INSTRUMENT_UNCALIBRATED", "INSTRUMENT_UNAVAILABLE", "INSTRUMENT_MAINTENANCE_DUE"})
        if self.inventory_equipment_id:
            self.instrument = None
            self.instrument_calibration = None
        if not self.batch_id:
            errors["batch"] = "A chemical batch is required."
        if not self.parameter_id:
            errors["parameter"] = "An analytical parameter is required."
        if self.parameter_id and self.batch_id:
            parameter = self.parameter
            if parameter.chemical_id != self.batch.chemical_id:
                errors["parameter"] = "The parameter must belong to the batch chemical."
            try:
                analysis_request = self.batch.analysis_request
            except ChemicalAnalysisRequest.DoesNotExist:
                analysis_request = None
            if (
                analysis_request is not None
                and (analysis_request.specification_snapshot or {}).get("captured")
                and not analysis_request.get_required_specifications().filter(pk=parameter.pk).exists()
            ):
                errors["parameter"] = "The parameter is not part of this request's captured specification set."
            if self.value is not None:
                try:
                    if not Decimal(str(self.value)).is_finite():
                        errors["value"] = "The analytical value must be finite."
                except (InvalidOperation, TypeError, ValueError):
                    errors["value"] = "The analytical value must be a finite decimal number."
            if parameter.requires_instrument and not (self.inventory_equipment_id or self.instrument_id):
                errors["inventory_equipment"] = "Select laboratory equipment from the Inventory register for this specification."
            quality_plan = (
                ChemicalMethodQualityPlan.current_for(parameter.method, self.batch.treatment_plant)
                if parameter.method_id else None
            )
            if quality_plan and quality_plan.require_measurement_uncertainty and self.expanded_uncertainty is None:
                errors["expanded_uncertainty"] = "This method requires a documented expanded measurement uncertainty."
            if quality_plan and quality_plan.require_reference_standard_lot and not self.reference_standard_lot.strip():
                errors["reference_standard_lot"] = "Record the reference-standard lot required by this method's quality plan."
            if quality_plan and quality_plan.require_reagent_lot_numbers and not self.reagent_lot_numbers.strip():
                errors["reagent_lot_numbers"] = "Record the reagent lot numbers required by this method's quality plan."
            if self.expanded_uncertainty is not None and self.expanded_uncertainty < 0:
                errors["expanded_uncertainty"] = "Expanded measurement uncertainty cannot be negative."
            if self.uncertainty_coverage_factor is not None and self.uncertainty_coverage_factor <= 0:
                errors["uncertainty_coverage_factor"] = "The uncertainty coverage factor must be positive."
            if self.instrument_calibration_id and self.instrument_calibration.instrument_id != self.instrument_id:
                errors["instrument_calibration"] = "The calibration record must belong to the selected instrument."
            if self.inventory_equipment_id:
                from .inventory_instruments import equipment_calibration_info, equipment_is_unavailable, equipment_maintenance_info

                equipment = self.inventory_equipment
                equipment_plant_id = equipment.section.treatment_plant_id if equipment.section_id else None
                if equipment_plant_id != self.batch.treatment_plant_id:
                    errors["inventory_equipment"] = "Select equipment assigned to a section at the batch's treatment plant."
                if equipment_is_unavailable(equipment):
                    flags.add("INSTRUMENT_UNAVAILABLE")
                    self.status = self.Status.FAIL
                calibration = equipment_calibration_info(equipment)
                if not calibration["is_current"]:
                    flags.add("INSTRUMENT_UNCALIBRATED")
                    self.status = self.Status.FAIL
                maintenance = equipment_maintenance_info(equipment)
                if maintenance["status"] in {"DUE", "OVERDUE"}:
                    flags.add("INSTRUMENT_MAINTENANCE_DUE")
            elif self.instrument_id:
                instrument = self.instrument
                if instrument.treatment_plant_id != self.batch.treatment_plant_id:
                    errors["instrument"] = "The instrument must belong to the batch treatment plant."
                if not instrument.is_active:
                    errors["instrument"] = "Only active instruments can be used for chemical analysis."
                latest_calibration = instrument.calibrations.filter(
                    performed_at__lte=timezone.now()
                ).order_by("-performed_at", "-pk").first()
                if (
                    latest_calibration is None
                    or not latest_calibration.valid_until
                    or latest_calibration.valid_until < timezone.localdate()
                    or latest_calibration.performed_at > timezone.now()
                ):
                    flags.add("INSTRUMENT_UNCALIBRATED")
                    self.quality_flags = sorted(flags)
                    self.status = self.Status.FAIL
            if self.value is not None:
                if parameter.min_limit is not None and self.value < parameter.min_limit:
                    flags.add("OUT_OF_SPEC_LOW")
                    self.quality_flags = sorted(flags)
                    self.status = self.Status.FAIL
                if parameter.max_limit is not None and self.value > parameter.max_limit:
                    flags.add("OUT_OF_SPEC_HIGH")
                    self.quality_flags = sorted(flags)
                    self.status = self.Status.FAIL
            self.quality_flags = sorted(flags)
        if errors:
            raise ValidationError(errors)

    def _capture_instrument_snapshot(self):
        if self.inventory_equipment_id:
            from .inventory_instruments import equipment_snapshot

            snapshot = equipment_snapshot(self.inventory_equipment)
            snapshot["legacy_instrument_id"] = str(self.instrument_id or "")
            return snapshot
        if not self.instrument_id:
            return {"instrument_id": None}
        instrument = self.instrument
        calibration = instrument.calibrations.filter(
            performed_at__lte=timezone.now()
        ).order_by("-performed_at", "-pk").first()
        return {
            "instrument_id": str(instrument.pk),
            "name": instrument.name,
            "serial_number": instrument.serial_number,
            "treatment_plant_id": instrument.treatment_plant_id,
            "is_active": instrument.is_active,
            "calibration_id": calibration.pk if calibration else None,
            "calibration_performed_at": calibration.performed_at.isoformat() if calibration else None,
            "calibration_valid_until": calibration.valid_until.isoformat() if calibration and calibration.valid_until else None,
            "calibration_method": calibration.method if calibration else "",
            "calibration_performed_by": calibration.performed_by if calibration else "",
            "calibration_valid": bool(
                calibration
                and calibration.valid_until
                and calibration.valid_until >= timezone.localdate()
            ),
        }

    @transaction.atomic
    def save(self, *args, **kwargs):
        if self.inventory_equipment_id:
            self.instrument = None
            self.instrument_calibration = None
        if self.parameter_id and not self.specification_snapshot:
            parameter = self.parameter
            method = parameter.method
            self.specification_snapshot = {
                "id": parameter.pk,
                "chemical_id": parameter.chemical_id,
                "treatment_plant_id": parameter.treatment_plant_id,
                "parameter_name": parameter.parameter_name,
                "unit": parameter.unit,
                "min_limit": format(parameter.min_limit, "f") if parameter.min_limit is not None else None,
                "max_limit": format(parameter.max_limit, "f") if parameter.max_limit is not None else None,
                "version": parameter.version,
                "is_required": parameter.is_required,
                "is_active": parameter.is_active,
                "requires_instrument": parameter.requires_instrument,
            }
            if method:
                self.method_snapshot = {
                    "id": str(method.pk),
                    "name": method.method,
                    "version": method.version,
                    "reference": method.kebs_reference,
                    "formula": method.formula,
                    "inputs_structure": method.inputs_structure,
                    "unit": method.unit,
                    "rounding_precision": method.rounding_precision,
                    "rounding_mode": method.rounding_mode,
                    "formula_hash": method.formula_hash,
                }

        if not self.pk:
            if self.instrument_id and not self.inventory_equipment_id and not self.instrument_calibration_id:
                self.instrument_calibration = self.instrument.calibrations.filter(
                    performed_at__lte=timezone.now()
                ).order_by("-performed_at", "-pk").first()
            self.instrument_snapshot = self._capture_instrument_snapshot()

        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                "batch_id", "parameter_id", "value", "textual_result", "status", "analyst_id", "instrument_id", "inventory_equipment_id", "tested_at",
                "instrument_calibration_id", "reference_standard_name", "reference_standard_lot", "reagent_lot_numbers",
                "expanded_uncertainty", "uncertainty_coverage_factor",
                "instrument_snapshot", "specification_snapshot", "method_snapshot", "quality_flags",
            ).first()
            if previous:
                if (
                    previous["instrument_id"] != self.instrument_id
                    or previous["inventory_equipment_id"] != self.inventory_equipment_id
                ):
                    self.instrument_snapshot = self._capture_instrument_snapshot()
                    self.instrument_calibration = (
                        self.instrument.calibrations.filter(performed_at__lte=timezone.now()).order_by("-performed_at", "-pk").first()
                        if self.instrument_id and not self.inventory_equipment_id else None
                    )
                else:
                    self.instrument_snapshot = previous["instrument_snapshot"] or self._capture_instrument_snapshot()
                if previous["batch_id"] != self.batch_id or previous["parameter_id"] != self.parameter_id:
                    raise ValidationError("A recorded result cannot be moved to another batch or parameter.")
                if (
                    previous["specification_snapshot"]
                    and previous["specification_snapshot"] != self.specification_snapshot
                ) or (
                    previous["method_snapshot"]
                    and previous["method_snapshot"] != self.method_snapshot
                ):
                    raise ValidationError("The specification and method snapshots for a recorded result are immutable.")
                changed = any(
                    previous[field] != getattr(self, field)
                    for field in (
                        "value", "textual_result", "status", "analyst_id", "instrument_id", "inventory_equipment_id", "quality_flags", "tested_at",
                        "instrument_calibration_id", "reference_standard_name", "reference_standard_lot", "reagent_lot_numbers",
                        "expanded_uncertainty", "uncertainty_coverage_factor",
                    )
                )
                if changed:
                    if not self.revision_reason.strip():
                        raise ValidationError({
                            "revision_reason": "A reason is required to amend an existing result."
                        })
                    previous_inputs = ChemicalAnalysisInput.objects.filter(
                        test_result_id=self.pk
                    ).values("inputs", "calculated_value", "method_snapshot").first()
                    BatchTestResultRevision.objects.create(
                        result_id=self.pk,
                        batch_id=self.batch_id,
                        parameter_id=self.parameter_id,
                        revision_number=self.revision_number,
                        revision_reason=self.revision_reason.strip(),
                        snapshot={
                            "value": format(previous["value"], "f") if previous["value"] is not None else None,
                            "textual_result": previous["textual_result"],
                            "status": previous["status"],
                            "analyst_id": previous["analyst_id"],
                            "instrument_id": str(previous["instrument_id"] or ""),
                            "inventory_equipment_id": str(previous["inventory_equipment_id"] or ""),
                            "instrument_calibration_id": str(previous["instrument_calibration_id"] or ""),
                            "reference_standard_name": previous["reference_standard_name"],
                            "reference_standard_lot": previous["reference_standard_lot"],
                            "reagent_lot_numbers": previous["reagent_lot_numbers"],
                            "expanded_uncertainty": format(previous["expanded_uncertainty"], "f") if previous["expanded_uncertainty"] is not None else None,
                            "uncertainty_coverage_factor": format(previous["uncertainty_coverage_factor"], "f") if previous["uncertainty_coverage_factor"] is not None else None,
                            "instrument_snapshot": previous["instrument_snapshot"] or {},
                            "specification": previous["specification_snapshot"],
                            "method": previous["method_snapshot"],
                            "quality_flags": previous["quality_flags"],
                            "tested_at": previous["tested_at"].isoformat() if previous["tested_at"] else None,
                            "method_inputs": previous_inputs["inputs"] if previous_inputs else {},
                            "calculated_value": (
                                format(previous_inputs["calculated_value"], "f")
                                if previous_inputs and previous_inputs["calculated_value"] is not None
                                else None
                            ),
                            "input_method_snapshot": previous_inputs["method_snapshot"] if previous_inputs else {},
                        },
                        actor=self.analyst,
                    )
                    self.revision_number += 1
                    update_fields = kwargs.get("update_fields")
                    if update_fields is not None:
                        kwargs["update_fields"] = set(update_fields) | {
                            "revision_number", "revision_reason", "tested_at", "quality_flags", "status", "instrument_snapshot", "inventory_equipment",
                            "instrument_calibration", "reference_standard_name", "reference_standard_lot", "reagent_lot_numbers",
                            "expanded_uncertainty", "uncertainty_coverage_factor",
                        }
        self.full_clean()
        if self.parameter_id:
            remark = self.parameter.remarks.first()
            if self.status == self.Status.PASS:
                self.auto_remark = (
                    remark.on_pass if remark and remark.on_pass.strip()
                    else (
                        f"Pass: result {self.value:g} {self.parameter.unit} "
                        "is within the configured specification limits."
                        if self.value is not None and (
                            self.parameter.min_limit is not None
                            or self.parameter.max_limit is not None
                        )
                        else "Pass: the analytical result was recorded as passing."
                    )
                )
            elif self.status == self.Status.FAIL:
                if remark and remark.on_fail.strip():
                    self.auto_remark = remark.on_fail
                elif "INSTRUMENT_UNCALIBRATED" in (self.quality_flags or []):
                    self.auto_remark = "Fail: the result was recorded using an instrument without a valid current calibration."
                elif "INSTRUMENT_UNAVAILABLE" in (self.quality_flags or []):
                    self.auto_remark = "Fail: the result was recorded using equipment marked unavailable in the Inventory register."
                elif any(flag in (self.quality_flags or []) for flag in ("OUT_OF_SPEC_LOW", "OUT_OF_SPEC_HIGH")):
                    bounds = []
                    if self.parameter.min_limit is not None:
                        bounds.append(f"minimum {self.parameter.min_limit:g}")
                    if self.parameter.max_limit is not None:
                        bounds.append(f"maximum {self.parameter.max_limit:g}")
                    limits = " and ".join(bounds)
                    self.auto_remark = (
                        f"Fail: result {self.value:g} {self.parameter.unit} "
                        f"is outside the specification ({limits} {self.parameter.unit})."
                    )
                else:
                    self.auto_remark = "Fail: the analytical result was recorded as failing."
            else:
                self.auto_remark = "Not applicable."
            if "INSTRUMENT_MAINTENANCE_DUE" in (self.quality_flags or []):
                maintenance_due = (self.instrument_snapshot or {}).get("maintenance_due_date") or "date not recorded"
                self.auto_remark = (
                    f"{self.auto_remark} Inventory equipment maintenance is due ({maintenance_due}); "
                    "chemist review should consider equipment readiness."
                )[:500]
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Test Result - {self.parameter} ({self.status or 'Pending'})"

    def delete(self, *args, **kwargs):
        raise ValidationError("Recorded chemical analysis results cannot be deleted; use a documented amendment or retest.")

class ChemicalAnalysisInput(models.Model):
    test_result = models.OneToOneField(
        "BatchTestResult",
        on_delete=models.CASCADE,
        related_name="inputs",
    )

    inputs = models.JSONField(
        default=dict,
        blank=False,
        help_text=(
            "Input values required by the analysis method, "
            "for example: "
            "{'mass1': 5.0, 'mass2': 5.2, 'mass3': 5.1, 'volume': 10.0}"
        ),
    )

    calculated_value = models.DecimalField(
        max_digits=26,
        decimal_places=12,
        null=True,
        blank=True,
    )
    method_snapshot = models.JSONField(default=dict, blank=True, editable=False)

    class Meta:
        ordering = ["-pk"]

    def __str__(self):
        return f"Inputs for TestResult #{self.test_result_id}"

    @property
    def test_parameter(self):
        test_result = self._state.fields_cache.get("test_result")
        if test_result is None and self.test_result_id:
            test_result = self.test_result
        return getattr(test_result, "parameter", None)

    @property
    def analysis_method(self):
        parameter = self.test_parameter

        if parameter is None:
            return None

        return parameter.method

    @property
    def required_input_fields(self):
        method = self.analysis_method

        if method is None:
            return []

        fields = method.get_input_fields()

        if fields is None:
            return []

        if isinstance(fields, str):
            fields = [
                field.strip()
                for field in fields.split(",")
                if field.strip()
            ]

        try:
            fields = list(fields)
        except TypeError as exc:
            raise ValidationError(
                "The analysis method returned an invalid "
                "input-field definition."
            ) from exc

        normalized = []

        for field in fields:
            if not isinstance(field, str):
                raise ValidationError(
                    "Analysis method input-field names must be strings."
                )

            field = field.strip()

            if not field:
                continue

            if not field.isidentifier():
                raise ValidationError(
                    f"Invalid analysis input field name: '{field}'."
                )

            if field in {"math", "__builtins__"}:
                raise ValidationError(
                    f"Reserved input field name is not allowed: '{field}'."
                )

            if field not in normalized:
                normalized.append(field)

        return normalized

    @property
    def formula(self):
        method = self.analysis_method

        if method is None:
            return None

        return getattr(method, "formula", None)

    @property
    def rounding_precision(self):
        method = self.analysis_method

        if method is None:
            return None

        precision = getattr(
            method,
            "rounding_precision",
            None,
        )

        if precision is None:
            return None

        try:
            precision = int(precision)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "The analysis method rounding precision "
                "must be an integer."
            ) from exc

        if precision < 0 or precision > 15:
            raise ValidationError(
                "The analysis method rounding precision "
                "must be between 0 and 15."
            )

        return precision

    @property
    def rounding_mode(self):
        method = self.analysis_method
        return getattr(method, "rounding_mode", "HALF_UP") if method is not None else "HALF_UP"

    def clean(self):
        super().clean()

        errors = {}

        if not self.test_result_id:
            errors["test_result"] = (
                "A test result is required."
            )

        if not isinstance(self.inputs, dict):
            errors["inputs"] = (
                "Analysis inputs must be provided as a JSON object."
            )

        if errors:
            raise ValidationError(errors)

        if self.test_result_id:
            try:
                test_result = (
                    BatchTestResult.objects
                    .select_related(
                        "parameter",
                        "parameter__method",
                    )
                    .get(pk=self.test_result_id)
                )
            except BatchTestResult.DoesNotExist as exc:
                errors["test_result"] = (
                    "The selected test result does not exist."
                )
            else:
                if test_result.parameter_id is None:
                    errors["test_result"] = (
                        "The test result must have an associated "
                        "parameter."
                    )

                elif test_result.parameter.method_id is None:
                    errors["test_result"] = (
                        "The selected parameter does not have "
                        "an analysis method."
                    )

        if errors:
            raise ValidationError(errors)

        self._validate_inputs()

    def _validate_inputs(self):
        if not isinstance(self.inputs, dict):
            raise ValidationError(
                {
                    "inputs": (
                        "Analysis inputs must be provided "
                        "as a JSON object."
                    )
                }
            )

        required_fields = self.required_input_fields

        if not required_fields:
            if self.formula:
                raise ValidationError(
                    "The analysis method defines a formula "
                    "but no input fields."
                )

            return

        missing = [
            field
            for field in required_fields
            if field not in self.inputs
        ]

        if missing:
            raise ValidationError(
                {
                    "inputs": (
                        "Missing required analysis input(s): "
                        + ", ".join(missing)
                    )
                }
            )

        invalid = []

        for field in required_fields:
            value = self.inputs.get(field)

            if value is None or (
                isinstance(value, str)
                and not value.strip()
            ):
                invalid.append(
                    f"{field} must contain a numeric value"
                )
                continue

            try:
                numeric_value = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                invalid.append(
                    f"{field} must contain a numeric value"
                )
                continue

            if not numeric_value.is_finite():
                invalid.append(
                    f"{field} must contain a finite numeric value"
                )
                continue

            method = self.analysis_method
            definitions = method.get_input_definitions() if method is not None else []
            definition = next((item for item in definitions if item.get("name") == field), {})
            minimum = definition.get("min_value")
            maximum = definition.get("max_value")
            try:
                if minimum is not None and numeric_value < Decimal(str(minimum)):
                    invalid.append(f"{field} is below its configured minimum")
                if maximum is not None and numeric_value > Decimal(str(maximum)):
                    invalid.append(f"{field} exceeds its configured maximum")
            except (InvalidOperation, TypeError, ValueError):
                invalid.append(f"{field} has invalid configured range limits")

        if invalid:
            raise ValidationError(
                {"inputs": invalid}
            )

    def _build_safe_context(self):
        self._validate_inputs()

        context = {
            "math": math,
        }

        for field in self.required_input_fields:
            try:
                value = Decimal(str(self.inputs[field]))
            except (
                TypeError,
                ValueError,
                KeyError,
                InvalidOperation,
            ) as exc:
                raise ValidationError(
                    {
                        "inputs": (
                            f"Invalid numeric value for input "
                            f"'{field}'."
                        )
                    }
                ) from exc

            if not value.is_finite():
                raise ValidationError(
                    {
                        "inputs": (
                            f"Input '{field}' must contain "
                            "a finite number."
                        )
                    }
                )

            context[field] = value

        return context

    def _validate_formula(self):
        formula = self.formula

        if formula is None:
            return None

        if not isinstance(formula, str):
            raise ValidationError(
                "The analysis method formula must be a string."
            )

        formula = formula.strip()

        if not formula:
            raise ValidationError(
                "The analysis method formula cannot be empty."
            )

        validate_formula_expression(
            formula,
            allow_math_attr=True,
        )

        tree = ast.parse(
            formula,
            mode="eval",
        )

        allowed_names = set(
            self.required_input_fields
        )
        allowed_names.add("math")

        unknown_names = sorted(
            {
                node.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Name)
                and node.id not in allowed_names
            }
        )

        if unknown_names:
            raise ValidationError(
                "The analysis formula references undefined "
                "input variable(s): "
                + ", ".join(unknown_names)
            )

        return formula

    def calculate(self):
        method = self.analysis_method

        if method is None:
            self.calculated_value = None
            return None

        formula = self._validate_formula()

        if not formula:
            self.calculated_value = None
            return None

        context = self._build_safe_context()

        try:
            result = safe_eval_formula(
                formula,
                context,
            )
        except (
            ArithmeticError,
            TypeError,
            ValueError,
            ZeroDivisionError,
            OverflowError,
        ) as exc:
            raise ValidationError(
                f"Formula evaluation error: {exc}"
            ) from exc
        except Exception as exc:
            raise ValidationError(
                f"Formula evaluation failed: {exc}"
            ) from exc

        precision = self.rounding_precision

        if precision is not None:
            from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP

            mode_map = {
                "HALF_UP": ROUND_HALF_UP,
                "HALF_EVEN": ROUND_HALF_EVEN,
                "DOWN": ROUND_DOWN,
                "UP": ROUND_UP,
            }
            quantum = Decimal(1).scaleb(-precision)
            result = result.quantize(quantum, rounding=mode_map.get(self.rounding_mode, ROUND_HALF_UP))

        self.calculated_value = result

        return result

    def recalculate(self):
        self.calculated_value = self.calculate()
        if self.pk:
            self.save(update_fields={"calculated_value"})
        return self.calculated_value

    def save(self, *args, **kwargs):
        input_values_changed = False
        if not self.pk and not self.method_snapshot:
            method = self.analysis_method
            if method is not None:
                self.method_snapshot = {
                    "id": str(method.pk),
                    "name": method.method,
                    "version": method.version,
                    "reference": method.kebs_reference,
                    "formula": method.formula,
                    "inputs_structure": method.inputs_structure,
                    "unit": method.unit,
                    "rounding_precision": method.rounding_precision,
                    "rounding_mode": method.rounding_mode,
                    "formula_hash": method.formula_hash,
                }
        elif self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                "test_result_id", "inputs", "calculated_value", "method_snapshot"
            ).first()
            if previous:
                if previous["test_result_id"] != self.test_result_id:
                    raise ValidationError("Method inputs cannot be moved to another test result.")
                if previous["method_snapshot"] and previous["method_snapshot"] != self.method_snapshot:
                    raise ValidationError("The analysis method snapshot for a recorded input is immutable.")
                if previous["inputs"] != self.inputs:
                    if not getattr(self, "_allow_retest_update", False):
                        raise ValidationError("Recorded method inputs are immutable; use the retest workflow to correct them.")
                    prior_revision = BatchTestResultRevision.objects.filter(
                        result_id=self.test_result_id,
                        revision_number=self.test_result.revision_number - 1,
                    ).order_by("-pk").first()
                    if (
                        prior_revision is None
                        or (prior_revision.snapshot or {}).get("method_inputs", {}) != previous["inputs"]
                    ):
                        raise ValidationError("Method inputs can only be changed when the previous values were captured in the retest audit trail.")
                    input_values_changed = True
        self.full_clean()
        self.calculate()

        if self.test_result_id and self.calculated_value != self.test_result.value:
            raise ValidationError({
                "calculated_value": "The recorded result must equal the value reproduced by its saved method inputs."
            })

        if self.pk:
            previous_value = type(self).objects.filter(pk=self.pk).values_list(
                "calculated_value", flat=True
            ).first()
            if previous_value != self.calculated_value and not input_values_changed:
                raise ValidationError("Recalculation changed a finalized method result; perform a documented retest instead.")

        super().save(
            *args,
            **kwargs,
        )




class ChemicalCoA(models.Model):
    batch = models.OneToOneField(
        "ChemicalBatch",
        on_delete=models.CASCADE,
        related_name="coa",
    )
    title = models.CharField(
        max_length=255,
        default="Certificate of Analysis",
    )
    coa_header = models.TextField(blank=True)
    coa_footer = models.TextField(blank=True)

    footer_image = models.ImageField(
        upload_to="reports/chemical_coas/footers/",
        null=True,
        blank=True,
    )
    watermark = models.ImageField(
        upload_to="reports/chemical_coas/watermarks/",
        null=True,
        blank=True,
    )
    qr_code = models.ImageField(
        upload_to="reports/chemical_coas/qr_codes/",
        null=True,
        blank=True,
    )
    pdf_file = models.FileField(
        upload_to="reports/chemical_coas/pdfs/",
        null=True,
        blank=True,
    )

    chemical_name = models.CharField(
        max_length=255,
        editable=False,
        blank=True,
    )
    supplier_name = models.CharField(
        max_length=255,
        editable=False,
        blank=True,
    )
    quantity = models.DecimalField(
        max_digits=18,
        decimal_places=6,
        editable=False,
        null=True,
        blank=True,
    )
    delivered_to = models.CharField(
        max_length=255,
        editable=False,
        blank=True,
    )
    delivery_date = models.DateField(
        editable=False,
        null=True,
        blank=True,
    )
    delivery_note_number = models.CharField(
        max_length=100,
        editable=False,
        blank=True,
    )
    packaging_status = models.ForeignKey(
        "PackagingStatus",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chemical_coas",
    )
    sampling_date = models.DateField(
        editable=False,
        null=True,
        blank=True,
    )
    analysis_date = models.DateField(
        editable=False,
        null=True,
        blank=True,
    )

    results_summary = models.TextField(blank=True)
    remarks = models.TextField(blank=True)

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_chemical_coas",
    )
    approved_on = models.DateTimeField(
        null=True,
        blank=True,
    )

    date_of_report = models.DateTimeField(
        auto_now_add=True,
    )
    updated_at = models.DateTimeField(
        auto_now=True,
    )

    verified_hash = models.CharField(
        max_length=64,
        blank=True,
        editable=False,
    )
    hash_version = models.PositiveSmallIntegerField(default=5, editable=False)
    approved_payload = models.JSONField(default=dict, blank=True, editable=False)
    pdf_sha256 = models.CharField(max_length=64, blank=True, editable=False)

    class Meta:
        ordering = ["-date_of_report"]
        indexes = [
            models.Index(
                fields=["approved_by", "-approved_on"],
                name="ca_coa_approved_idx",
            ),
            models.Index(
                fields=["-date_of_report"],
                name="ca_coa_report_date_idx",
            ),
            models.Index(
                fields=["batch", "approved_on"],
                name="ca_coa_batch_approval_idx",
            ),
        ]

    def __str__(self):
        return (
            f"COA - {self.chemical_name or 'N/A'} "
            f"[{self.sample_id or 'N/A'}]"
        )

    @property
    def analysis_request(self):
        if not self.batch_id:
            return None

        return getattr(
            self.batch,
            "analysis_request",
            None,
        )

    @property
    def sample_id(self):
        if not self.batch_id:
            return None

        return getattr(
            self.batch,
            "sample_id",
            None,
        )

    @property
    def treatment_plant(self):
        if not self.batch_id:
            return None

        return getattr(
            self.batch,
            "treatment_plant",
            None,
        )

    @property
    def chemical(self):
        if not self.batch_id:
            return None

        return getattr(
            self.batch,
            "chemical",
            None,
        )

    @property
    def supplier(self):
        if not self.batch_id:
            return None

        return getattr(
            self.batch,
            "supplier",
            None,
        )

    @property
    def analysis_status(self):
        request = self.analysis_request

        if request is None:
            return None

        return getattr(
            request,
            "status",
            None,
        )

    @property
    def has_failed_results(self):
        return (
            bool(self.batch_id)
            and self.batch.test_results.filter(
                status=BatchTestResult.Status.FAIL
            ).exists()
        )

    @property
    def has_na_results(self):
        return (
            bool(self.batch_id)
            and self.batch.test_results.filter(
                status=BatchTestResult.Status.NA
            ).exists()
        )

    @property
    def has_pending_results(self):
        if not self.batch_id:
            return False

        return self.batch.test_results.filter(
            Q(status__isnull=True)
            | Q(status="")
        ).exists()

    @property
    def is_approved(self):
        return bool(
            self.approved_by_id
            and self.approved_on
        )

    @property
    def is_locked(self):
        return self.is_approved

    @property
    def is_complete(self):
        return not self.missing_assets

    @property
    def missing_assets(self):
        missing = []

        if not self.qr_code or not self.qr_code.name:
            missing.append("QR Code")

        if not self.pdf_file or not self.pdf_file.name:
            missing.append("PDF File")

        return missing

    @property
    def result_counts(self):
        return self.get_result_counts()

    @property
    def integrity_status(self):
        if not self.is_approved:
            return "UNAPPROVED"

        if not self.verified_hash:
            return "UNVERIFIED"

        return "VALID" if self.verify_integrity() else "INVALID"

    @property
    def generation_status(self):
        if not self.batch_id:
            return "NO_BATCH"

        if not self.can_generate():
            return "NOT_READY"

        if not self.watermark:
            return "PENDING_WATERMARK"

        if not self.qr_code:
            return "PENDING_QR"

        if not self.pdf_file:
            return "PENDING_PDF"

        if self.is_approved:
            return "APPROVED"

        return "READY"

    def clean(self):
        super().clean()

        errors = {}

        if not self.batch_id:
            errors["batch"] = (
                "A chemical batch is required for a CoA."
            )
        else:
            batch = self.batch

            if batch is None:
                errors["batch"] = (
                    "The selected chemical batch does not exist."
                )
            else:
                request = getattr(
                    batch,
                    "analysis_request",
                    None,
                )

                if request is None:
                    errors["batch"] = (
                        "The chemical batch must have an associated "
                        "chemical analysis request."
                    )

                if batch.quantity_received is not None:
                    try:
                        quantity_received = float(
                            batch.quantity_received
                        )
                    except (TypeError, ValueError):
                        errors["batch"] = (
                            "The chemical batch quantity must be numeric."
                        )
                    else:
                        if not math.isfinite(quantity_received):
                            errors["batch"] = (
                                "The chemical batch quantity must be finite."
                            )
                        elif quantity_received <= 0:
                            errors["batch"] = (
                                "The chemical batch quantity must be "
                                "greater than zero."
                            )

                if request is not None:
                    if (
                        request.batch_id
                        and request.batch_id != batch.pk
                    ):
                        errors["batch"] = (
                            "The CoA batch and analysis request "
                            "are inconsistent."
                        )

        if self.quantity is not None:
            try:
                quantity = float(self.quantity)
            except (TypeError, ValueError):
                errors["quantity"] = (
                    "CoA quantity must be numeric."
                )
            else:
                if not math.isfinite(quantity):
                    errors["quantity"] = (
                        "CoA quantity must be finite."
                    )
                elif quantity <= 0:
                    errors["quantity"] = (
                        "CoA quantity must be greater than zero."
                    )

        if self.delivery_date and self.sampling_date:
            if self.sampling_date < self.delivery_date:
                errors["sampling_date"] = (
                    "Sampling date cannot be earlier "
                    "than the delivery date."
                )

        if self.sampling_date and self.analysis_date:
            if self.analysis_date < self.sampling_date:
                errors["analysis_date"] = (
                    "Analysis date cannot be earlier "
                    "than the sampling date."
                )

        if self.approved_on and not self.approved_by_id:
            errors["approved_by"] = (
                "An approving user is required when an "
                "approval timestamp is present."
            )

        if self.approved_by_id and not self.approved_on:
            errors["approved_on"] = (
                "An approval timestamp is required when an "
                "approving user is present."
            )

        if self.is_approved and not self.verified_hash:
            errors["verified_hash"] = (
                "An approved CoA must have a verification hash."
            )

        if errors:
            raise ValidationError(errors)

    def _get_results_queryset(self):
        if not self.batch_id:
            return BatchTestResult.objects.none()

        return (
            self.batch.test_results
            .select_related(
                "parameter",
                "parameter__chemical",
                "parameter__method",
                "analyst",
            )
            .order_by(
                "parameter__chemical__name",
                "parameter__parameter_name",
                "id",
            )
        )

    def get_results(self):
        return self._get_results_queryset()

    def get_result_counts(self):
        queryset = self._get_results_queryset()
        from django.db.models import Count

        counts = queryset.aggregate(
            total=Count("pk"),
            passed=Count("pk", filter=Q(status=BatchTestResult.Status.PASS)),
            failed=Count("pk", filter=Q(status=BatchTestResult.Status.FAIL)),
            not_applicable=Count("pk", filter=Q(status=BatchTestResult.Status.NA)),
            pending=Count("pk", filter=Q(status__isnull=True) | Q(status="")),
        )
        return {
            "total": counts["total"], "pass": counts["passed"],
            "fail": counts["failed"], "na": counts["not_applicable"],
            "pending": counts["pending"],
        }

    def _serialize_results_digest(self):
        results = []

        for result in self._get_results_queryset():
            parameter = result.parameter
            consensus_value = (result.textual_result or "").startswith(
                ("Interlaboratory ", "Qualitative interlaboratory comparison")
            )
            input_record = getattr(result, "inputs", None)

            chemical_name = ""
            parameter_name = "Unspecified Parameter"

            if parameter is not None:
                parameter_name = (
                    parameter.parameter_name
                    or "Unnamed Parameter"
                )

                if parameter.chemical_id:
                    chemical = parameter.chemical
                    chemical_name = (
                        getattr(
                            chemical,
                            "name",
                            "",
                        )
                        or ""
                    )

            results.append(
                {
                    "id": result.pk,
                    "parameter_id": result.parameter_id or "",
                    "parameter": parameter_name,
                    "chemical": chemical_name,
                    "value": (
                        format(result.value, "f")
                        if self.hash_version >= 5 and result.value is not None
                        else (float(result.value) if result.value is not None else None)
                    ),
                    "textual_result": (
                        result.textual_result or ""
                    ),
                    "status": result.status or "",
                    "tested_at": (
                        result.tested_at.isoformat()
                        if result.tested_at
                        else None
                    ),
                    "analyst_id": result.analyst_id or "",
                    **({
                        "instrument_id": str(result.instrument_id or ""),
                        "inventory_equipment_id": str(result.inventory_equipment_id or ""),
                        "instrument_calibration_id": str(result.instrument_calibration_id or ""),
                        "instrument_snapshot": result.instrument_snapshot or {},
                        "reference_standard_name": result.reference_standard_name,
                        "reference_standard_lot": result.reference_standard_lot,
                        "reagent_lot_numbers": result.reagent_lot_numbers,
                        "expanded_uncertainty": format(result.expanded_uncertainty, "f") if result.expanded_uncertainty is not None else None,
                        "uncertainty_coverage_factor": format(result.uncertainty_coverage_factor, "f") if result.uncertainty_coverage_factor is not None else None,
                        "quality_flags": result.quality_flags or [],
                        "revision_number": result.revision_number,
                        "specification_snapshot": result.specification_snapshot or {},
                        "method_snapshot": result.method_snapshot or {},
                        "method_inputs": {} if consensus_value else getattr(input_record, "inputs", {}),
                        "calculated_value": (
                            format(input_record.calculated_value, "f")
                            if self.hash_version >= 5
                            and not consensus_value
                            and input_record is not None
                            and getattr(input_record, "calculated_value", None) is not None
                            else (
                                float(input_record.calculated_value)
                                if not consensus_value
                                and input_record is not None
                                and getattr(input_record, "calculated_value", None) is not None
                                else None
                            )
                        ),
                    } if self.hash_version >= 4 else {}),
                }
            )

        return json.dumps(
            results,
            separators=(",", ":"),
            ensure_ascii=False,
            sort_keys=True,
        )

    def generate_hash(self):
        if self.hash_version >= 5 and self.approved_payload:
            payload = json.dumps(
                self.approved_payload,
                separators=(",", ":"),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()

        content = {
            "version": self.hash_version,
            "batch_id": self.batch_id or "",
            "sample_id": self.sample_id or "",
            "title": self.title or "",
            "chemical_name": self.chemical_name or "",
            "supplier_name": self.supplier_name or "",
            "quantity": (
                float(self.quantity)
                if self.quantity is not None
                else None
            ),
            "delivered_to": self.delivered_to or "",
            "delivery_date": (
                self.delivery_date.isoformat()
                if self.delivery_date
                else ""
            ),
            "delivery_note_number": (
                self.delivery_note_number or ""
            ),
            "sampling_date": (
                self.sampling_date.isoformat()
                if self.sampling_date
                else ""
            ),
            "analysis_date": (
                self.analysis_date.isoformat()
                if self.analysis_date
                else ""
            ),
            "packaging_status_id": (
                self.packaging_status_id or ""
            ),
            "approved_by": (
                self.approved_by_id or ""
            ),
            "approved_on": (
                self.approved_on.isoformat()
                if self.approved_on
                else ""
            ),
            "results_digest": (
                self._serialize_results_digest()
            ),
            "results_summary": (
                self.results_summary or ""
            ),
            "remarks": (
                self.remarks or ""
            ),
        }

        payload = json.dumps(
            content,
            separators=(",", ":"),
            ensure_ascii=False,
            sort_keys=True,
        )

        return hashlib.sha256(
            payload.encode("utf-8")
        ).hexdigest()

    def build_approved_payload(self, *, approved_by, approved_on):
        """Freeze the certificate data that was actually approved."""
        results = json.loads(self._serialize_results_digest())
        for result in results:
            if result.get("value") is not None:
                result["value"] = format(Decimal(str(result["value"])), "f")
            if result.get("calculated_value") is not None:
                result["calculated_value"] = format(Decimal(str(result["calculated_value"])), "f")

        def asset_digest(field):
            if not field or not getattr(field, "name", ""):
                return None
            digest = hashlib.sha256()
            with field.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()

        return {
            "schema": "chemical-coa/v1",
            "coa_id": self.pk,
            "batch_id": self.batch_id,
            "sample_id": self.sample_id or "",
            "title": self.title or "",
            "coa_header": self.coa_header or "",
            "coa_footer": self.coa_footer or "",
            "chemical_name": self.chemical_name or "",
            "supplier_name": self.supplier_name or "",
            "quantity": format(Decimal(str(self.quantity)), "f") if self.quantity is not None else None,
            "delivered_to": self.delivered_to or "",
            "delivery_date": self.delivery_date.isoformat() if self.delivery_date else None,
            "delivery_note_number": self.delivery_note_number or "",
            "sampling_date": self.sampling_date.isoformat() if self.sampling_date else None,
            "analysis_date": self.analysis_date.isoformat() if self.analysis_date else None,
            "packaging_status_id": self.packaging_status_id,
            "footer_image_sha256": asset_digest(self.footer_image),
            "watermark_sha256": asset_digest(self.watermark),
            "results": results,
            "results_summary": self.results_summary or "",
            "remarks": self.remarks or "",
            "approved_by_id": approved_by.pk,
            "approved_on": approved_on.isoformat(),
        }

    def generate_remarks(self):
        queryset = self._get_results_queryset()

        if not queryset.exists():
            return "No test results available."

        pending = queryset.filter(
            Q(status__isnull=True)
            | Q(status="")
        ).exists()

        if pending:
            return (
                "Analytical testing is incomplete. "
                "All test results must be finalized before "
                "the CoA can be approved."
            )

        failed = queryset.filter(
            status=BatchTestResult.Status.FAIL
        ).exists()
        consensus_event = None
        if self.batch_id:
            consensus_event = self.batch.workflow_events.filter(
                event_type="interlaboratory_consensus_recorded"
            ).order_by("-occurred_at", "-pk").first()
        consensus_note = ""
        if consensus_event:
            details = consensus_event.details or {}
            laboratory_count = details.get("laboratory_count") or 0
            origin_name = self.batch.treatment_plant.name if self.batch.treatment_plant_id else "originating treatment plant"
            consensus_note = (
                f" Interlaboratory comparison included {laboratory_count} treatment-plant laboratories; "
                f"quantitative results are reported as the mean of laboratory means. "
                f"This final certificate is issued from {origin_name}, where the chemical batch was first received."
            )
            chemist_remarks = str(details.get("remarks") or "").strip()
            if chemist_remarks:
                consensus_note += f" Chemist comparison remarks: {chemist_remarks}"
            laboratory_remarks = [
                f"{item.get('treatment_plant')}: {item.get('remarks')}"
                for item in details.get("laboratory_remarks", [])
                if isinstance(item, dict) and str(item.get("remarks") or "").strip()
            ]
            if laboratory_remarks:
                consensus_note += " Peer laboratory notes: " + "; ".join(laboratory_remarks)
            discordant_parameters = [
                str(item.get("parameter") or "")
                for item in (details.get("comparison") or [])
                if isinstance(item, dict)
                and len({
                    str(lab.get("status") or "")
                    for lab in (item.get("laboratories") or [])
                    if isinstance(lab, dict)
                }) > 1
                and str(item.get("parameter") or "").strip()
            ]
            if discordant_parameters:
                consensus_note += (
                    " Participating laboratories reported differing outcomes for "
                    + ", ".join(discordant_parameters[:12])
                    + "; the reported certificate values reflect the consolidated comparison."
                )
            spread_flags = [
                str(item.get("parameter") or "")
                for item in (details.get("comparison") or [])
                if isinstance(item, dict) and item.get("tolerance_breached")
            ]
            if spread_flags:
                consensus_note += " Configured interlaboratory spread tolerance was exceeded for " + ", ".join(spread_flags[:12]) + "; chemist acknowledgment is recorded in the audit trail."
            uncertain = [
                str(item.get("parameter") or "")
                for item in (details.get("comparison") or [])
                if isinstance(item, dict) and not item.get("uncertainty_complete", True)
            ]
            if uncertain:
                consensus_note += " Measurement uncertainty was incomplete for " + ", ".join(uncertain[:12]) + "; use the reported values with this limitation."
            rules = sorted({str(item.get("consensus_rule") or "") for item in (details.get("comparison") or []) if isinstance(item, dict) and item.get("consensus_rule")})
            if rules:
                consensus_note += " Configured consensus rule(s): " + ", ".join(rule.replace("_", " ").lower() for rule in rules) + "."

        if failed:
            return (
                "NON-CONFORMING: one or more final analytical results remain outside the approved specifications. "
                "This certificate records the final outcome and does not authorize chemical use. "
                "Not suitable for use."
                + consensus_note
            )

        applicable_count = queryset.exclude(
            status=BatchTestResult.Status.NA
        ).count()

        na_count = queryset.filter(
            status=BatchTestResult.Status.NA
        ).count()

        if applicable_count == 0:
            return (
                "No applicable quality parameters were identified."
                + consensus_note
            )

        if na_count:
            return (
                "Batch passed all applicable quality specifications. "
                "Some parameters were not applicable."
                + consensus_note
            )

        return (
            "Batch passed all quality specifications. "
            "Approved for use."
            + consensus_note
        )

    def generate_results_summary(self):
        queryset = self._get_results_queryset()

        if not queryset.exists():
            return "No test results available."

        lines = []

        for result in queryset:
            parameter = result.parameter

            if parameter is None:
                label = "Unspecified Parameter"
            else:
                chemical_name = ""

                if parameter.chemical_id:
                    chemical_name = (
                        getattr(
                            parameter.chemical,
                            "name",
                            "",
                        )
                        or ""
                    )

                parameter_name = (
                    parameter.parameter_name
                    or "Unnamed Parameter"
                )

                label = (
                    f"{chemical_name} - {parameter_name}"
                    if chemical_name
                    else parameter_name
                )

            if result.value is not None:
                value = str(result.value)
            elif result.textual_result:
                value = result.textual_result
            else:
                value = "N/A"

            status = (
                result.get_status_display()
                if result.status
                else "Pending"
            )

            lines.append(
                f"{label}: {value} ({status})"
            )

        summary = "\n".join(lines)
        if self.batch_id:
            consensus_event = self.batch.workflow_events.filter(
                event_type="interlaboratory_consensus_recorded"
            ).order_by("-occurred_at", "-pk").first()
            if consensus_event:
                details = consensus_event.details or {}
                laboratory_count = details.get("laboratory_count") or 0
                origin_name = self.batch.treatment_plant.name if self.batch.treatment_plant_id else "originating treatment plant"
                summary += (
                    f"\nInterlaboratory consensus: quantitative values use the configured method decision rule. "
                    f"Comparison included {laboratory_count} treatment-plant laboratories. "
                    f"Certificate issued by {origin_name}, the plant of initial receipt."
                )
        return summary

    def auto_populate(self, force=False):
        if not self.batch_id:
            raise ValidationError(
                "Cannot populate CoA without an associated "
                "chemical batch."
            )

        batch = (
            ChemicalBatch.objects
            .select_related(
                "chemical",
                "supplier",
                "treatment_plant",
                "location",
            )
            .get(pk=self.batch_id)
        )

        request = getattr(
            batch,
            "analysis_request",
            None,
        )

        if request is None:
            raise ValidationError(
                "Cannot populate CoA without an associated "
                "chemical analysis request."
            )

        request = (
            ChemicalAnalysisRequest.objects
            .select_related(
                "batch",
                "batch__location",
            )
            .get(pk=request.pk)
        )

        chemical = batch.chemical
        supplier = batch.supplier
        treatment_plant = batch.treatment_plant

        if force or not self.chemical_name:
            self.chemical_name = (
                getattr(
                    chemical,
                    "name",
                    "",
                )
                or ""
            )

        if force or not self.supplier_name:
            self.supplier_name = (
                getattr(
                    supplier,
                    "name",
                    "",
                )
                or ""
            )

        if force or self.quantity is None:
            self.quantity = batch.quantity_received

        location = batch.location

        if force or not self.delivered_to:
            self.delivered_to = (
                getattr(
                    location,
                    "name",
                    "",
                )
                or getattr(
                    treatment_plant,
                    "name",
                    "",
                )
                or ""
            )

        if force or not self.delivery_date:
            self.delivery_date = (
                getattr(
                    request,
                    "delivery_date",
                    None,
                )
                or getattr(
                    batch,
                    "received_date",
                    None,
                )
                or timezone.now().date()
            )

        if force or not self.delivery_note_number:
            self.delivery_note_number = (
                getattr(
                    batch,
                    "delivery_note_number",
                    "",
                )
                or ""
            )

        if force or not self.sampling_date:
            if getattr(
                request,
                "analysis_started_at",
                None,
            ):
                self.sampling_date = (
                    request.analysis_started_at.date()
                )
            else:
                self.sampling_date = (
                    getattr(
                        batch,
                        "received_date",
                        None,
                    )
                    or timezone.now().date()
                )

        latest_checklist = None

        if hasattr(request, "checklists"):
            latest_checklist = (
                request.checklists
                .order_by("-created_at")
                .first()
            )

        if force or not self.analysis_date:
            if latest_checklist:
                self.analysis_date = (
                    latest_checklist.created_at.date()
                )
            elif getattr(
                request,
                "completed_at",
                None,
            ):
                self.analysis_date = (
                    request.completed_at.date()
                )
            elif getattr(
                request,
                "analysis_started_at",
                None,
            ):
                self.analysis_date = (
                    request.analysis_started_at.date()
                )
            else:
                self.analysis_date = self.sampling_date

        plant_name = (
            getattr(
                treatment_plant,
                "name",
                "",
            )
            or ""
        )

        plant_address = (
            getattr(
                treatment_plant,
                "address",
                "",
            )
            or ""
        )

        if force or not self.coa_header:
            self.coa_header = " - ".join(
                value
                for value in (
                    plant_name,
                    plant_address,
                )
                if value
            )

        chemical_grade = (
            getattr(
                chemical,
                "grade",
                "",
            )
            or "N/A"
        )

        if force or not self.coa_footer:
            self.coa_footer = (
                f"Chemical Grade: {chemical_grade}"
            )

        return self

    def validate_generation_requirements(self):
        if not self.batch_id:
            raise ValidationError(
                "Cannot generate CoA without an associated "
                "chemical batch."
            )

        batch = self.batch

        if batch is None:
            raise ValidationError(
                "The associated chemical batch does not exist."
            )

        request = self.analysis_request

        if request is None:
            raise ValidationError(
                "Cannot generate CoA without an associated "
                "chemical analysis request."
            )

        if request.status != (
            ChemicalAnalysisRequest.Status.COMPLETED
        ):
            raise ValidationError(
                "Cannot generate CoA before the chemical analysis "
                "request is completed."
            )

        if batch.status not in (
            ChemicalBatch.Status.ANALYSED,
            ChemicalBatch.Status.APPROVED,
            ChemicalBatch.Status.UTILIZED,
            ChemicalBatch.Status.RETURNED,
        ):
            raise ValidationError(
                "The chemical batch must complete analysis before "
                "a CoA can be generated."
            )

        results = self._get_results_queryset()

        if not results.exists():
            raise ValidationError(
                "Cannot generate CoA without analytical test results."
            )

        if results.filter(
            Q(status__isnull=True)
            | Q(status="")
        ).exists():
            raise ValidationError(
                "All analytical test results must have a status "
                "before a CoA can be generated."
            )

        request = self.analysis_request
        required_specifications = (
            request.get_required_specifications()
            if request is not None
            else ChemicalSpecification.current_for(
                ChemicalSpecification.objects.filter(
                    chemical_id=batch.chemical_id,
                    treatment_plant_id=batch.treatment_plant_id,
                    is_required=True,
                )
            )
        )

        if required_specifications.exists():
            completed_parameter_ids = set(
                results.exclude(
                    status=BatchTestResult.Status.NA
                )
                .exclude(
                    parameter_id__isnull=True
                )
                .values_list(
                    "parameter_id",
                    flat=True,
                )
            )

            missing = (
                required_specifications
                .exclude(
                    pk__in=completed_parameter_ids
                )
            )

            if missing.exists():
                names = list(
                    missing.values_list(
                        "parameter_name",
                        flat=True,
                    )[:20]
                )

                raise ValidationError(
                    "Cannot generate CoA because required analytical "
                    "parameters are missing: "
                    + ", ".join(
                        name
                        for name in names
                        if name
                    )
                )

    def can_generate(self):
        try:
            self.validate_generation_requirements()
        except ValidationError:
            return False

        return True

    @transaction.atomic
    def approve(self, approved_by):
        if not self.pk:
            raise ValidationError("Save the CoA before approving it.")
        type(self).objects.select_for_update().get(pk=self.pk)
        self.refresh_from_db()
        batch = self.batch
        require_ca_actor(
            approved_by,
            "ca.approve_chemical_coa",
            plant_id=batch.treatment_plant_id if batch else None,
        )
        if self.is_approved:
            raise ValidationError("This CoA has already been approved.")
        if self.has_failed_results:
            raise ValidationError(
                "A Certificate of Analysis with failed results can be generated "
                "for reporting, but it cannot be approved or released for use."
            )
        self.validate_generation_requirements()

        process = getattr(batch, "analysis_process", None) if batch else None
        if process:
            performing_ids = process.performing_analyst_ids
            if approved_by.pk in performing_ids or process.qa_officer_id == approved_by.pk or process.approver_id == approved_by.pk:
                raise ValidationError("CoA approval must be independent of the analyst and chemist batch reviewer.")
        if batch.status != ChemicalBatch.Status.APPROVED:
            raise ValidationError("The chemical batch must be approved before the CoA can be approved.")

        self.auto_populate()
        if not self.watermark:
            self.generate_watermark()
        self.results_summary = self.generate_results_summary()
        self.remarks = self.generate_remarks()
        self.approved_by = approved_by
        self.approved_on = timezone.now()
        self.hash_version = 5
        self.approved_payload = self.build_approved_payload(
            approved_by=approved_by,
            approved_on=self.approved_on,
        )
        self.verified_hash = self.generate_hash()
        self._allow_approval_write = True
        try:
            self.save(
                populate=False,
                update_fields=[
                    "chemical_name", "supplier_name", "quantity", "delivered_to",
                    "delivery_date", "delivery_note_number", "sampling_date",
                    "analysis_date", "coa_header", "coa_footer", "watermark", "results_summary",
                    "remarks", "approved_by", "approved_on", "approved_payload",
                    "verified_hash", "hash_version", "updated_at",
                ],
            )
        finally:
            self.__dict__.pop("_allow_approval_write", None)
        ChemicalCoAAuditEvent.objects.create(
            coa=self,
            action=ChemicalCoAAuditEvent.Action.APPROVED,
            actor=approved_by,
            content_hash=self.verified_hash,
            details={"hash_version": self.hash_version},
        )
        coa_id = self.pk
        transaction.on_commit(
            lambda: _regenerate_approved_coa_assets(coa_id),
            robust=True,
        )
        return self

    @transaction.atomic
    def revoke_approval(self, *, revoked_by, reason):
        if not self.pk:
            raise ValidationError("Save the CoA before revoking approval.")
        type(self).objects.select_for_update().get(pk=self.pk)
        self.refresh_from_db()
        batch = self.batch
        require_ca_actor(
            revoked_by,
            "ca.approve_chemical_coa",
            plant_id=batch.treatment_plant_id if batch else None,
        )
        if not self.is_approved:
            raise ValidationError("This CoA is not currently approved.")
        reason = str(reason or "").strip()
        if not reason:
            raise ValidationError("A reason is required to revoke a CoA approval.")
        ChemicalCoAAuditEvent.objects.create(
            coa=self,
            action=ChemicalCoAAuditEvent.Action.REVOKED,
            actor=revoked_by,
            content_hash=self.verified_hash,
            details={"reason": reason, "hash_version": self.hash_version},
        )
        self.approved_by = None
        self.approved_on = None
        self.approved_payload = {}
        self.verified_hash = ""
        self._allow_approval_write = True
        try:
            self.save(
                populate=False,
                update_fields=["approved_by", "approved_on", "approved_payload", "verified_hash", "updated_at"],
            )
        finally:
            self.__dict__.pop("_allow_approval_write", None)
        return self

    def regenerate_content(self):
        if self.is_approved:
            raise ValidationError(
                "Approved CoA content is immutable. "
                "Revoke approval before regenerating content."
            )

        self.validate_generation_requirements()

        self.auto_populate(force=True)

        self.results_summary = (
            self.generate_results_summary()
        )

        self.remarks = (
            self.generate_remarks()
        )

        self.generate_watermark()

        self.verified_hash = ""

        self.full_clean()

        return self

    def verify_integrity(self):
        if not self.verified_hash:
            return False

        if not self.is_approved:
            return False

        try:
            current_hash = self.generate_hash()
            if self.hash_version >= 5:
                if not self.pdf_sha256 or not self.pdf_file:
                    return False
                digest = hashlib.sha256()
                with self.pdf_file.open("rb") as file_handle:
                    for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if not secrets.compare_digest(self.pdf_sha256, digest.hexdigest()):
                    return False
        except Exception:
            return False

        return (
            secrets.compare_digest(
                self.verified_hash,
                current_hash,
            )
        )

    def refresh_hash(self):
        if not self.pk:
            raise ValidationError(
                "The CoA must be saved before its hash "
                "can be refreshed."
            )
        if self.is_approved:
            raise ValidationError(
                "Approved certificate hashes are immutable; issue a new certificate revision instead."
            )
        self.verified_hash = self.generate_hash()
        self.save(
            populate=False,
            update_fields=["verified_hash", "updated_at"],
        )
        return self.verified_hash

    def prepare_for_generation(self):
        if self.is_approved:
            raise ValidationError(
                "An approved CoA cannot be regenerated."
            )

        self.validate_generation_requirements()

        self.auto_populate()

        self.results_summary = (
            self.generate_results_summary()
        )

        self.remarks = (
            self.generate_remarks()
        )

        if not self.watermark:
            self.generate_watermark()

        self.verified_hash = ""

        self.full_clean()

        return self

    def ensure_watermark(self):
        if self.watermark and self.watermark.name:
            return self.watermark

        if self.is_approved:
            raise ValidationError(
                "Cannot modify assets of an approved CoA."
            )

        return self.generate_watermark()

    def generate_watermark(self):
        """Create a generated watermark asset without recursively saving the CoA."""
        if self.is_approved and not getattr(self, "_allow_approval_write", False):
            raise ValidationError("Approved CoA assets are immutable.")
        image = create_celestial_watermark(
            text=f"CONTROLLED LABORATORY DOCUMENT · {self.sample_id or self.pk or 'DRAFT'}"
        )
        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        buffer.seek(0)
        self.watermark.save(
            f"coa-{self.pk or 'draft'}-watermark.png",
            ContentFile(buffer.getvalue()),
            save=False,
        )
        return self.watermark

    def clear_generated_assets(self):
        if self.is_approved:
            raise ValidationError(
                "Cannot remove generated assets from an approved CoA."
            )

        self.watermark = None
        self.qr_code = None
        self.pdf_file = None
        self.verified_hash = ""

        return self

    def get_result_statistics(self):
        counts = self.get_result_counts()

        total = counts["total"]
        passed = counts["pass"]
        failed = counts["fail"]
        na = counts["na"]
        pending = counts["pending"]

        applicable = max(
            total - na,
            0,
        )

        pass_rate = (
            round(
                (passed / applicable) * 100,
                2,
            )
            if applicable
            else 0.0
        )

        fail_rate = (
            round(
                (failed / applicable) * 100,
                2,
            )
            if applicable
            else 0.0
        )

        completion_rate = (
            round(
                (
                    (total - pending)
                    / total
                ) * 100,
                2,
            )
            if total
            else 0.0
        )

        return {
            "total": total,
            "pass": passed,
            "fail": failed,
            "na": na,
            "pending": pending,
            "applicable": applicable,
            "pass_rate": pass_rate,
            "fail_rate": fail_rate,
            "completion_rate": completion_rate,
        }

    def get_verification_payload(self):
        return {
            "coa_id": self.pk,
            "batch_id": self.batch_id,
            "sample_id": self.sample_id,
            "chemical_name": self.chemical_name,
            "approved": self.is_approved,
            "approved_by_id": self.approved_by_id,
            "approved_on": (
                self.approved_on.isoformat()
                if self.approved_on
                else None
            ),
            "verified_hash": self.verified_hash,
            "integrity": self.integrity_status,
        }

    def save(
        self,
        *args,
        populate=True,
        regenerate_watermark=False,
        regenerate_hash=False,
        generate_assets=False,
        **kwargs,
    ):
        update_fields = kwargs.get(
            "update_fields"
        )

        protected_fields = {
                "batch",
                "title",
                "coa_header",
                "coa_footer",
                "footer_image",
                "watermark",
                "qr_code",
                "pdf_file",
                "chemical_name",
                "supplier_name",
                "quantity",
                "delivered_to",
                "delivery_date",
                "delivery_note_number",
                "packaging_status",
                "sampling_date",
                "analysis_date",
                "results_summary",
                "remarks",
                "approved_by",
                "approved_on",
                "verified_hash",
                "approved_payload",
                "hash_version",
                "pdf_sha256",
            }
        persisted_approval = None
        if self.pk:
            persisted_approval = type(self).objects.filter(pk=self.pk).values(
                "approved_by_id", "approved_on"
            ).first()
        was_approved = bool(
            persisted_approval
            and persisted_approval["approved_by_id"]
            and persisted_approval["approved_on"]
        )
        if self.is_approved and not getattr(self, "_allow_approval_write", False):
            raise ValidationError("Use the authorized CoA approval action to approve a certificate.")
        if was_approved and not getattr(self, "_allow_approval_write", False):
            allowed_asset_fields = {"watermark", "qr_code", "pdf_file", "pdf_sha256", "updated_at"}
            if not getattr(self, "_ca_asset_write", False):
                if update_fields is None or set(update_fields) & protected_fields:
                    raise ValidationError("Approved CoA content is immutable.")
            elif update_fields is None or not set(update_fields).issubset(allowed_asset_fields):
                raise ValidationError("Only generated CoA assets may be updated after approval.")

        if populate:
            self.auto_populate()

        if not self.remarks:
            self.remarks = (
                self.generate_remarks()
            )

        if not self.results_summary:
            self.results_summary = (
                self.generate_results_summary()
            )

        if (
            generate_assets
            and (
                regenerate_watermark
                or not self.watermark
            )
        ):
            self.generate_watermark()

        if (
            regenerate_hash
            and not self.is_approved
        ):
            self.verified_hash = ""

        if (
            self.is_approved
            and regenerate_hash
        ):
            self.verified_hash = self.generate_hash()

        self.full_clean()

        super().save(
            *args,
            **kwargs,
        )


class ChemicalBatchHold(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        RESOLVED = "RESOLVED", "Resolved"
        REJECTED = "REJECTED", "Rejected"

    class Issue(models.TextChoices):
        DAMAGED_PACKAGING = "DAMAGED_PACKAGING", "Damaged packaging"
        QUANTITY_MISMATCH = "QUANTITY_MISMATCH", "Quantity mismatch"
        MISSING_DOCUMENTS = "MISSING_DOCUMENTS", "Missing documents"
        LABEL_MISMATCH = "LABEL_MISMATCH", "Label mismatch"
        EXPIRED = "EXPIRED", "Expired chemical"
        OTHER = "OTHER", "Other discrepancy"

    batch = models.ForeignKey(
        ChemicalBatch, on_delete=models.PROTECT, related_name="workflow_holds"
    )
    issue = models.CharField(max_length=32, choices=Issue.choices)
    details = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN, db_index=True)
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="opened_chemical_batch_holds",
    )
    opened_at = models.DateTimeField(default=timezone.now, db_index=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="resolved_chemical_batch_holds",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution = models.TextField(blank=True)

    class Meta:
        ordering = ["-opened_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["OPEN", "RESOLVED", "REJECTED"]),
                name="ca_batch_hold_status_valid",
            ),
        ]
        indexes = [models.Index(fields=["status", "opened_at"], name="ca_hold_status_open_idx")]

    @transaction.atomic
    def resolve(self, *, actor, resolution, reject=False):
        if not self.pk:
            raise ValidationError("Save the intake hold before resolving it.")
        type(self).objects.select_for_update().get(pk=self.pk)
        self.refresh_from_db()
        if self.status != self.Status.OPEN:
            raise ValidationError("This intake hold is already closed.")
        require_ca_actor(
            actor,
            "ca.resolve_chemical_batch_hold",
            plant_id=self.batch.treatment_plant_id,
        )
        resolution = str(resolution or "").strip()
        if not resolution:
            raise ValidationError("Enter the action taken to resolve this hold.")
        self.status = self.Status.REJECTED if reject else self.Status.RESOLVED
        self.resolved_by = actor
        self.resolved_at = timezone.now()
        self.resolution = resolution
        self._allow_hold_resolution = True
        try:
            self.save(update_fields=["status", "resolved_by", "resolved_at", "resolution"])
        finally:
            self.__dict__.pop("_allow_hold_resolution", None)
        return self

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                "batch_id", "issue", "details", "opened_by_id", "status"
            ).first()
            if previous is None:
                raise ValidationError("This intake hold no longer exists.")
            if any(getattr(self, field) != previous[field] for field in (
                "batch_id", "issue", "details", "opened_by_id"
            )):
                raise ValidationError("Intake hold details are immutable after creation.")
            if previous["status"] != self.status and not getattr(self, "_allow_hold_resolution", False):
                raise ValidationError("Intake hold status can only change through its resolution workflow.")
        super().save(*args, **kwargs)


class ChemicalWorkflowEvent(models.Model):
    """Append-only workflow journal used for audit and stage-time reporting."""

    process = models.ForeignKey(
        ChemicalAnalysisProcess, on_delete=models.PROTECT, related_name="workflow_events"
    )
    batch = models.ForeignKey(
        ChemicalBatch, on_delete=models.PROTECT, related_name="workflow_events"
    )
    analysis_request = models.ForeignKey(
        ChemicalAnalysisRequest, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="workflow_events",
    )
    event_type = models.CharField(max_length=48, db_index=True)
    action = models.CharField(max_length=120, blank=True)
    from_status = models.CharField(max_length=24, blank=True)
    to_status = models.CharField(max_length=24, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="chemical_workflow_events",
    )
    details = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-occurred_at", "-pk"]
        indexes = [
            models.Index(fields=["batch", "occurred_at"], name="ca_event_batch_time_idx"),
            models.Index(fields=["process", "occurred_at"], name="ca_event_proc_time_idx"),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Workflow journal events are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Workflow journal events cannot be deleted.")


class ChemicalLabReferral(models.Model):
    """A controlled request for an independent laboratory to verify a batch."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Awaiting laboratory results"
        COMPLETED = "COMPLETED", "Results submitted"
        CANCELLED = "CANCELLED", "Cancelled"

    process = models.ForeignKey(
        ChemicalAnalysisProcess,
        on_delete=models.PROTECT,
        related_name="interlaboratory_referrals",
    )
    batch = models.ForeignKey(
        ChemicalBatch,
        on_delete=models.PROTECT,
        related_name="interlaboratory_referrals",
    )
    treatment_plant = models.ForeignKey(
        "t.TreatmentPlant",
        on_delete=models.PROTECT,
        related_name="received_chemical_lab_referrals",
        help_text="The independent treatment-plant laboratory receiving this referral.",
    )
    parameter_ids = models.JSONField(default=list, blank=True)
    comparison_rules_snapshot = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    remarks = models.TextField(blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_chemical_lab_referrals",
    )
    requested_at = models.DateTimeField(default=timezone.now, db_index=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_chemical_lab_referrals",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    submission_remarks = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "requested_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["process", "treatment_plant"],
                name="ca_interlab_process_plant_unique",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["PENDING", "COMPLETED", "CANCELLED"]),
                name="ca_interlab_referral_status_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["treatment_plant", "status", "requested_at"], name="ca_interlab_plant_status_idx"),
            models.Index(fields=["batch", "status"], name="ca_interlab_batch_status_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if not self.batch_id or not self.process_id:
            errors["batch"] = "A batch and its analysis process are required for a laboratory referral."
        elif self.process.batch_id != self.batch_id:
            errors["process"] = "The referral process must belong to the selected batch."
        if self.batch_id and self.treatment_plant_id:
            if self.batch.treatment_plant_id == self.treatment_plant_id:
                errors["treatment_plant"] = "Choose a laboratory at another treatment plant."
            if not type(self.treatment_plant).objects.filter(
                pk=self.treatment_plant_id,
                laboratory__isnull=False,
            ).exists():
                errors["treatment_plant"] = "The selected treatment plant has no registered laboratory."
        if not isinstance(self.parameter_ids, list) or not self.parameter_ids:
            errors["parameter_ids"] = "At least one failed parameter must be included in the referral."
        else:
            try:
                requested_ids = {int(value) for value in self.parameter_ids}
            except (TypeError, ValueError):
                requested_ids = set()
            if not requested_ids:
                errors["parameter_ids"] = "The referral parameter list is invalid."
            elif self.batch_id and not ChemicalSpecification.objects.filter(
                pk__in=requested_ids,
                chemical_id=self.batch.chemical_id,
            ).count() == len(requested_ids):
                errors["parameter_ids"] = "Every referred parameter must belong to this batch chemical."
        if self.status == self.Status.COMPLETED and (not self.submitted_by_id or not self.submitted_at):
            errors["status"] = "A completed referral must record who submitted results and when."
        if errors:
            raise ValidationError(errors)

    @transaction.atomic
    def mark_completed(self, *, actor, remarks=""):
        referral = type(self).objects.select_for_update().get(pk=self.pk)
        if referral.status != self.Status.PENDING:
            raise ValidationError("This laboratory referral is no longer awaiting results.")
        required_ids = {int(value) for value in referral.parameter_ids}
        recorded_ids = set(
            referral.results.values_list("parameter_id", flat=True)
        )
        if required_ids != recorded_ids:
            raise ValidationError("Record a result for every referred failed parameter before submitting.")
        if actor is None or not getattr(actor, "is_active", False):
            raise ValidationError("An active laboratory analyst is required to submit results.")
        if not (actor.is_superuser or actor.has_perm("ca.record_chemical_result")):
            raise ValidationError("You do not have permission to submit laboratory results.")
        if not actor.is_superuser and actor.assigned_treatment_plant_id != referral.treatment_plant_id:
            raise ValidationError("Only staff at the receiving laboratory can submit these results.")
        referral.status = self.Status.COMPLETED
        referral.submitted_by = actor
        referral.submitted_at = timezone.now()
        referral.submission_remarks = str(remarks or "").strip()
        referral._allow_status_transition = True
        try:
            referral.full_clean()
            referral.save(update_fields=["status", "submitted_by", "submitted_at", "submission_remarks", "updated_at"])
        finally:
            referral.__dict__.pop("_allow_status_transition", None)
        return referral

    def save(self, *args, **kwargs):
        if self.pk and not getattr(self, "_allow_status_transition", False):
            old_status = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if old_status is not None and old_status != self.status:
                raise ValidationError("Laboratory referral status can only change through result submission.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Chemical laboratory referrals cannot be deleted.")

    def __str__(self):
        return f"{self.batch.sample_id} → {self.treatment_plant.code} [{self.status}]"


class ChemicalLabComparisonResult(models.Model):
    """Immutable source, reassignment, and peer-laboratory readings for consensus."""

    class Source(models.TextChoices):
        ORIGINAL = "ORIGINAL", "Originating analysis"
        REASSIGNMENT = "REASSIGNMENT", "Independent local repeat"
        INTERLAB = "INTERLAB", "Independent laboratory"

    batch = models.ForeignKey(
        ChemicalBatch,
        on_delete=models.PROTECT,
        related_name="lab_comparison_results",
    )
    process = models.ForeignKey(
        ChemicalAnalysisProcess,
        on_delete=models.PROTECT,
        related_name="lab_comparison_results",
    )
    treatment_plant = models.ForeignKey(
        "t.TreatmentPlant",
        on_delete=models.PROTECT,
        related_name="chemical_lab_comparison_results",
    )
    referral = models.ForeignKey(
        ChemicalLabReferral,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="results",
    )
    parameter = models.ForeignKey(
        ChemicalSpecification,
        on_delete=models.PROTECT,
        related_name="laboratory_comparison_results",
    )
    source = models.CharField(max_length=16, choices=Source.choices, db_index=True)
    run_number = models.PositiveIntegerField(default=1)
    value = models.DecimalField(max_digits=26, decimal_places=12, null=True, blank=True)
    textual_result = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=10,
        choices=(("PASS", "Pass"), ("FAIL", "Fail"), ("NA", "Not applicable")),
    )
    analyst = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chemical_lab_comparison_results",
    )
    instrument = models.ForeignKey(
        "instruments.Instrument",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chemical_lab_comparison_results",
    )
    inventory_equipment = models.ForeignKey(
        "inventory.Equipment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="chemical_lab_comparison_results",
    )
    instrument_snapshot = models.JSONField(default=dict, blank=True)
    instrument_calibration = models.ForeignKey(
        "instruments.InstrumentCalibration",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chemical_lab_comparison_results",
        editable=False,
    )
    reference_standard_name = models.CharField(max_length=180, blank=True)
    reference_standard_lot = models.CharField(max_length=120, blank=True)
    reagent_lot_numbers = models.CharField(max_length=500, blank=True)
    expanded_uncertainty = models.DecimalField(max_digits=24, decimal_places=12, null=True, blank=True)
    uncertainty_coverage_factor = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    specification_snapshot = models.JSONField(default=dict, blank=True)
    method_snapshot = models.JSONField(default=dict, blank=True)
    remarks = models.TextField(blank=True)
    recorded_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["parameter__parameter_name", "treatment_plant__name", "run_number", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "treatment_plant", "parameter", "source", "run_number"],
                name="ca_lab_result_reading_unique",
            ),
            models.CheckConstraint(
                condition=Q(status__in=["PASS", "FAIL", "NA"]),
                name="ca_lab_result_status_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["batch", "parameter", "source"], name="ca_lab_result_compare_idx"),
            models.Index(fields=["treatment_plant", "recorded_at"], name="ca_lab_result_plant_time_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.inventory_equipment_id:
            self.instrument = None
            self.instrument_calibration = None
        if not self.batch_id or not self.process_id or self.process.batch_id != self.batch_id:
            errors["process"] = "A comparison result must belong to the batch analysis process."
        if self.parameter_id and self.batch_id and self.parameter.chemical_id != self.batch.chemical_id:
            errors["parameter"] = "The comparison parameter must belong to the batch chemical."
        if self.source == self.Source.INTERLAB:
            if not self.referral_id:
                errors["referral"] = "An independent laboratory result must be attached to a referral."
            elif (
                self.referral.batch_id != self.batch_id
                or self.referral.process_id != self.process_id
                or self.referral.treatment_plant_id != self.treatment_plant_id
                or int(self.parameter_id or 0) not in {int(value) for value in self.referral.parameter_ids}
            ):
                errors["referral"] = "The result does not match its laboratory referral."
        elif self.referral_id:
            errors["referral"] = "Only independent laboratory results can be attached to a referral."
        if self.source == self.Source.ORIGINAL and self.batch_id and self.treatment_plant_id != self.batch.treatment_plant_id:
            errors["treatment_plant"] = "The original analysis must belong to the originating treatment plant."
        if self.source == self.Source.REASSIGNMENT and self.batch_id and self.treatment_plant_id != self.batch.treatment_plant_id:
            errors["treatment_plant"] = "A local reassignment must remain at the originating treatment plant."
        equipment_failed = False
        if self.parameter_id and self.parameter.requires_instrument and not (self.inventory_equipment_id or self.instrument_id):
            errors["inventory_equipment"] = "Select laboratory equipment from the Inventory register for this parameter."
        if self.parameter_id and self.parameter.method_id:
            quality_plan = ChemicalMethodQualityPlan.current_for(self.parameter.method, self.treatment_plant)
            rule_snapshot = (
                (self.referral.comparison_rules_snapshot or {}).get(str(self.parameter_id), {})
                if self.source == self.Source.INTERLAB and self.referral_id
                else {}
            )
            require_uncertainty = rule_snapshot.get("require_measurement_uncertainty", quality_plan and quality_plan.require_measurement_uncertainty)
            require_standard_lot = rule_snapshot.get("require_reference_standard_lot", quality_plan and quality_plan.require_reference_standard_lot)
            require_reagent_lots = rule_snapshot.get("require_reagent_lot_numbers", quality_plan and quality_plan.require_reagent_lot_numbers)
            if require_uncertainty and self.expanded_uncertainty is None:
                errors["expanded_uncertainty"] = "This method requires a documented expanded measurement uncertainty."
            if require_standard_lot and not self.reference_standard_lot.strip():
                errors["reference_standard_lot"] = "Record the reference-standard lot required by this method's quality plan."
            if require_reagent_lots and not self.reagent_lot_numbers.strip():
                errors["reagent_lot_numbers"] = "Record the reagent lot numbers required by this method's quality plan."
        if self.expanded_uncertainty is not None and self.expanded_uncertainty < 0:
            errors["expanded_uncertainty"] = "Expanded measurement uncertainty cannot be negative."
        if self.uncertainty_coverage_factor is not None and self.uncertainty_coverage_factor <= 0:
            errors["uncertainty_coverage_factor"] = "The uncertainty coverage factor must be positive."
        if self.instrument_id:
            instrument = self.instrument
            if instrument.treatment_plant_id != self.treatment_plant_id:
                errors["instrument"] = "The instrument must belong to the laboratory recording the result."
            elif not instrument.is_active:
                errors["instrument"] = "Only active instruments can be used for laboratory comparison results."
            else:
                latest_calibration = instrument.calibrations.filter(
                    performed_at__lte=timezone.now()
                ).order_by("-performed_at", "-pk").first()
                if (
                    latest_calibration is None
                    or not latest_calibration.valid_until
                    or latest_calibration.valid_until < timezone.localdate()
                    or latest_calibration.performed_at > timezone.now()
                ):
                    errors["instrument"] = "The laboratory instrument must have a valid current calibration."
        if self.inventory_equipment_id:
            from .inventory_instruments import equipment_calibration_info, equipment_is_unavailable, equipment_snapshot

            equipment = self.inventory_equipment
            equipment_plant_id = equipment.section.treatment_plant_id if equipment.section_id else None
            if equipment_plant_id != self.treatment_plant_id:
                errors["inventory_equipment"] = "Select equipment assigned to the laboratory recording this result."
            calibration = equipment_calibration_info(equipment)
            equipment_failed = equipment_is_unavailable(equipment) or not calibration["is_current"]
            self.instrument_snapshot = equipment_snapshot(equipment)
            if equipment_is_unavailable(equipment):
                self.remarks = (self.remarks + " Equipment is marked unavailable in Inventory.").strip()
            if not calibration["is_current"]:
                self.remarks = (
                    self.remarks + f" Inventory calibration status: {calibration['status']} (due {calibration['due_date'] or 'date not recorded'})."
                ).strip()
        if self.value is not None:
            try:
                number = Decimal(str(self.value))
                if not number.is_finite():
                    raise InvalidOperation
                self.value = number
            except (InvalidOperation, TypeError, ValueError):
                errors["value"] = "The laboratory reading must be a finite number."
            else:
                minimum = self.parameter.min_limit if self.parameter_id else None
                maximum = self.parameter.max_limit if self.parameter_id else None
                if minimum is not None or maximum is not None:
                    self.status = (
                        BatchTestResult.Status.PASS
                        if (minimum is None or number >= minimum) and (maximum is None or number <= maximum)
                        else BatchTestResult.Status.FAIL
                    )
        if equipment_failed:
            self.status = BatchTestResult.Status.FAIL
        if self.value is None and not (self.textual_result or "").strip():
            errors["value"] = "Enter a numeric or textual result."
        if self.status not in BatchTestResult.Status.values:
            errors["status"] = "Select a valid laboratory result outcome."
        if errors:
            raise ValidationError(errors)

        if self.instrument_id and not self.instrument_calibration_id:
            self.instrument_calibration = self.instrument.calibrations.filter(
                performed_at__lte=timezone.now()
            ).order_by("-performed_at", "-pk").first()

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Laboratory comparison results are immutable; submit a new documented run.")
        if self.parameter_id and not self.specification_snapshot:
            parameter = self.parameter
            self.specification_snapshot = {
                "id": parameter.pk,
                "chemical_id": parameter.chemical_id,
                "treatment_plant_id": parameter.treatment_plant_id,
                "parameter_name": parameter.parameter_name,
                "unit": parameter.unit,
                "minimum": str(parameter.min_limit) if parameter.min_limit is not None else None,
                "maximum": str(parameter.max_limit) if parameter.max_limit is not None else None,
                "version": parameter.version,
            }
            if parameter.method_id:
                method = parameter.method
                self.method_snapshot = {
                    "id": str(method.pk),
                    "name": method.method,
                    "version": method.version,
                    "reference": method.kebs_reference,
                    "formula_hash": method.formula_hash,
                }
        if self.inventory_equipment_id and not self.instrument_snapshot:
            from .inventory_instruments import equipment_snapshot

            self.instrument_snapshot = equipment_snapshot(self.inventory_equipment)
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Laboratory comparison results cannot be deleted.")


class ChemicalQCCheckResult(models.Model):
    """Immutable QC observation and evaluated control result for one run."""

    class Status(models.TextChoices):
        PASS = "PASS", "Within control limits"
        FAIL = "FAIL", "Outside control limits"

    batch = models.ForeignKey(ChemicalBatch, on_delete=models.PROTECT, related_name="qc_checks")
    process = models.ForeignKey(ChemicalAnalysisProcess, on_delete=models.PROTECT, related_name="qc_checks")
    control = models.ForeignKey(ChemicalMethodQCControl, on_delete=models.PROTECT, related_name="results")
    parameter = models.ForeignKey(ChemicalSpecification, on_delete=models.PROTECT, related_name="qc_check_results")
    run_number = models.PositiveIntegerField(default=1)
    observed_value = models.DecimalField(max_digits=24, decimal_places=12)
    evaluated_value = models.DecimalField(max_digits=24, decimal_places=12)
    comparison_value = models.DecimalField(max_digits=24, decimal_places=12, null=True, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, db_index=True)
    limits_snapshot = models.JSONField(default=dict, blank=True)
    instrument = models.ForeignKey("instruments.Instrument", on_delete=models.SET_NULL, null=True, blank=True, related_name="chemical_qc_checks")
    inventory_equipment = models.ForeignKey("inventory.Equipment", on_delete=models.PROTECT, null=True, blank=True, related_name="chemical_qc_checks")
    instrument_calibration = models.ForeignKey("instruments.InstrumentCalibration", on_delete=models.SET_NULL, null=True, blank=True, related_name="chemical_qc_checks")
    instrument_snapshot = models.JSONField(default=dict, blank=True)
    remarks = models.TextField(blank=True)
    analyst = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="chemical_qc_checks")
    recorded_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-recorded_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["batch", "control", "parameter", "run_number"], name="ca_qc_batch_control_param_run_unique"),
            models.CheckConstraint(condition=Q(run_number__gte=1), name="ca_qc_run_positive"),
        ]
        indexes = [
            models.Index(fields=["control", "status", "recorded_at"], name="ca_qc_control_status_time_idx"),
            models.Index(fields=["batch", "parameter", "recorded_at"], name="ca_qc_batch_param_time_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.process_id and self.batch_id and self.process.batch_id != self.batch_id:
            errors["process"] = "The QC run must belong to the selected chemical batch process."
        if self.parameter_id and self.control_id and self.parameter.method_id != self.control.plan.method_id:
            errors["parameter"] = "The QC control method must match the affected analytical parameter method."
        if self.batch_id and self.control_id and self.control.plan.treatment_plant_id not in (None, self.batch.treatment_plant_id):
            errors["control"] = "The QC plan is not configured for the batch's originating treatment plant."
        if self.control_id and self.control.check_type == ChemicalMethodQCControl.CheckType.INSTRUMENT and not (self.inventory_equipment_id or self.instrument_id):
            errors["inventory_equipment"] = "Select the Inventory equipment used for this instrument verification control."
        if self.instrument_id:
            if self.instrument.treatment_plant_id != self.batch.treatment_plant_id:
                errors["instrument"] = "The QC instrument must belong to the batch treatment plant."
            latest_calibration = self.instrument.calibrations.filter(performed_at__lte=timezone.now()).order_by("-performed_at", "-pk").first()
            if latest_calibration is None or not latest_calibration.valid_until or latest_calibration.valid_until < timezone.localdate():
                errors["instrument"] = "The instrument must have a current calibration for QC verification."
            elif self.instrument_calibration_id and self.instrument_calibration.instrument_id != self.instrument_id:
                errors["instrument_calibration"] = "The calibration record must belong to the selected QC instrument."
            elif not self.instrument_calibration_id:
                self.instrument_calibration = latest_calibration
            self.instrument_snapshot = {
                "instrument_id": str(self.instrument.pk),
                "name": self.instrument.name,
                "serial_number": self.instrument.serial_number,
                "treatment_plant_id": self.instrument.treatment_plant_id,
                "calibration_id": self.instrument_calibration_id,
                "calibration_valid_until": self.instrument_calibration.valid_until.isoformat() if self.instrument_calibration and self.instrument_calibration.valid_until else None,
            }
        if self.inventory_equipment_id:
            from .inventory_instruments import equipment_calibration_info, equipment_is_unavailable, equipment_snapshot

            equipment = self.inventory_equipment
            equipment_plant_id = equipment.section.treatment_plant_id if equipment.section_id else None
            if equipment_plant_id != self.batch.treatment_plant_id:
                errors["inventory_equipment"] = "Select equipment assigned to a section at the batch's treatment plant."
            self.instrument_snapshot = equipment_snapshot(equipment)
            calibration = equipment_calibration_info(equipment)
            if equipment_is_unavailable(equipment) or not calibration["is_current"]:
                self.status = self.Status.FAIL
                details = []
                if equipment_is_unavailable(equipment):
                    details.append("equipment marked unavailable in Inventory")
                if not calibration["is_current"]:
                    details.append(f"calibration {calibration['status'].lower()}")
                note = "Instrument QC failed: " + " and ".join(details) + "."
                self.remarks = f"{self.remarks} {note}".strip()
        if not Decimal(str(self.observed_value)).is_finite() or not Decimal(str(self.evaluated_value)).is_finite():
            errors["observed_value"] = "QC values must be finite decimals."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("QC check results are immutable; repeat the control as a new run.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("QC check results cannot be deleted.")


class ChemicalOOSInvestigation(models.Model):
    """Auditable investigation linking original, repeat and final outcomes."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Investigation open"
        CLOSED = "CLOSED", "Investigation closed"

    process = models.ForeignKey(ChemicalAnalysisProcess, on_delete=models.PROTECT, related_name="oos_investigations")
    batch = models.ForeignKey(ChemicalBatch, on_delete=models.PROTECT, related_name="oos_investigations")
    parameter = models.ForeignKey(ChemicalSpecification, on_delete=models.PROTECT, related_name="oos_investigations")
    initial_result_snapshot = models.JSONField(default=dict)
    repeat_result_snapshots = models.JSONField(default=list, blank=True)
    comparison_snapshot = models.JSONField(default=dict, blank=True)
    assignable_cause = models.TextField()
    chemist_decisions = models.JSONField(default=list, blank=True)
    corrective_action = models.TextField(blank=True)
    final_disposition = models.TextField(blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN, db_index=True)
    opened_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="opened_ca_oos_investigations")
    opened_at = models.DateTimeField(default=timezone.now, db_index=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="closed_ca_oos_investigations")
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-opened_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["process", "parameter"], condition=Q(status="OPEN"), name="ca_oos_one_open_process_param"),
        ]
        indexes = [models.Index(fields=["batch", "status", "opened_at"], name="ca_oos_batch_status_time_idx")]

    @staticmethod
    def result_snapshot(result):
        if result is None:
            return {}
        return {
            "result_id": result.pk,
            "value": str(result.value) if result.value is not None else None,
            "textual_result": result.textual_result,
            "status": result.status,
            "analyst_id": result.analyst_id,
            "analyst": str(result.analyst) if result.analyst_id else "",
            "inventory_equipment_id": str(result.inventory_equipment_id or ""),
            "instrument_snapshot": result.instrument_snapshot or {},
            "method_snapshot": result.method_snapshot or {},
            "specification_snapshot": result.specification_snapshot or {},
            "quality_flags": result.quality_flags or [],
            "tested_at": result.tested_at.isoformat() if result.tested_at else None,
            "revision_number": result.revision_number,
            "revision_reason": result.revision_reason,
        }

    @classmethod
    def record_decision(cls, *, process, result, actor, cause, decision, remarks, corrective_action=""):
        if not str(cause or "").strip():
            raise ValidationError("Record the potential assignable cause for every failed result.")
        record, created = cls.objects.get_or_create(
            process=process,
            parameter=result.parameter,
            status=cls.Status.OPEN,
            defaults={
                "batch": process.batch,
                "initial_result_snapshot": cls.result_snapshot(result),
                "assignable_cause": str(cause).strip(),
                "corrective_action": str(corrective_action or "").strip(),
                "opened_by": actor,
            },
        )
        if not created:
            latest = list(record.chemist_decisions or [])
            latest.append({"decision": str(decision), "remarks": str(remarks or "").strip(), "actor_id": actor.pk, "at": timezone.now().isoformat()})
            cls.objects.filter(pk=record.pk, status=cls.Status.OPEN).update(chemist_decisions=latest)
        else:
            cls.objects.filter(pk=record.pk).update(chemist_decisions=[{"decision": str(decision), "remarks": str(remarks or "").strip(), "actor_id": actor.pk, "at": timezone.now().isoformat()}])
        return record

    @transaction.atomic
    def append_repeat(self, result):
        record = type(self).objects.select_for_update().get(pk=self.pk)
        if record.status != self.Status.OPEN:
            raise ValidationError("A closed out-of-specification investigation cannot receive new results.")
        snapshots = list(record.repeat_result_snapshots or [])
        snapshots.append(self.result_snapshot(result))
        type(self).objects.filter(pk=record.pk).update(repeat_result_snapshots=snapshots)
        self.repeat_result_snapshots = snapshots
        return self

    @transaction.atomic
    def close(self, *, actor, disposition, comparison=None):
        record = type(self).objects.select_for_update().get(pk=self.pk)
        if record.status != self.Status.OPEN:
            raise ValidationError("This investigation has already been closed.")
        disposition = str(disposition or "").strip()
        if not disposition:
            raise ValidationError("Record the final batch disposition before closing the investigation.")
        type(self).objects.filter(pk=record.pk).update(
            status=self.Status.CLOSED,
            closed_by=actor,
            closed_at=timezone.now(),
            final_disposition=disposition,
            comparison_snapshot=comparison or record.comparison_snapshot,
        )
        return type(self).objects.get(pk=record.pk)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("OOS investigation records change only through audited workflow actions.")
        if self.process_id and self.batch_id and self.process.batch_id != self.batch_id:
            raise ValidationError("The OOS investigation must belong to its analysis process batch.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("OOS investigation records cannot be deleted.")


class ChemicalKEBSReferral(models.Model):
    """Documented failed-chemical referral and physical transfer to KEBS."""

    class Status(models.TextChoices):
        DISPATCHED = "DISPATCHED", "Dispatched to KEBS"

    batch = models.OneToOneField(ChemicalBatch, on_delete=models.PROTECT, related_name="kebs_referral")
    process = models.OneToOneField(ChemicalAnalysisProcess, on_delete=models.PROTECT, related_name="kebs_referral")
    coa = models.ForeignKey("ChemicalCoA", on_delete=models.PROTECT, related_name="kebs_referrals")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DISPATCHED)
    destination = models.CharField(max_length=255, default="Kenya Bureau of Standards (KEBS)")
    kebs_reference = models.CharField(max_length=120, blank=True)
    dispatch_method = models.CharField(max_length=80)
    shipment_reference = models.CharField(max_length=120, blank=True)
    assignable_cause = models.TextField()
    comparison_summary = models.JSONField(default=dict, blank=True)
    remarks = models.TextField(blank=True)
    evidence = models.FileField(upload_to="ca/kebs-referrals/%Y/%m/", null=True, blank=True)
    dispatched_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="dispatched_ca_kebs_referrals")
    dispatched_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-dispatched_at", "-pk"]
        indexes = [models.Index(fields=["status", "dispatched_at"], name="ca_kebs_status_time_idx")]

    def clean(self):
        super().clean()
        errors = {}
        if self.process_id and self.batch_id and self.process.batch_id != self.batch_id:
            errors["process"] = "The KEBS referral must use the batch's analysis process."
        if self.coa_id and self.coa.batch_id != self.batch_id:
            errors["coa"] = "The attached certificate must belong to the referred chemical batch."
        if self.coa_id and not self.coa.pdf_file:
            errors["coa"] = "Generate the failed-result Certificate of Analysis before referring the chemical to KEBS."
        process = ChemicalAnalysisProcess.objects.filter(pk=self.process_id).first()
        if process is None or not process.failed_tests.exists():
            errors["process"] = "A failed final analysis is required for KEBS referral."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("KEBS dispatch records are immutable.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("KEBS dispatch records cannot be deleted.")


class ChemicalCustodyEvent(models.Model):
    class EventType(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        SAMPLED = "SAMPLED", "Sample collected"
        TRANSFERRED = "TRANSFERRED", "Transferred"
        ANALYSIS_STARTED = "ANALYSIS_STARTED", "Analysis started"
        ANALYSIS_COMPLETED = "ANALYSIS_COMPLETED", "Analysis completed"
        UTILIZED = "UTILIZED", "Utilized"
        RETURNED = "RETURNED", "Returned to supplier"
        MANUAL = "MANUAL", "Manual custody record"

    batch = models.ForeignKey(
        ChemicalBatch, on_delete=models.PROTECT, related_name="custody_events"
    )
    event_type = models.CharField(max_length=24, choices=EventType.choices, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="chemical_custody_events",
    )
    from_location = models.CharField(max_length=255, blank=True)
    to_location = models.CharField(max_length=255, blank=True)
    seal_number = models.CharField(max_length=100, blank=True)
    evidence = models.FileField(upload_to="ca/custody/%Y/%m/", null=True, blank=True)
    remarks = models.TextField(blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-occurred_at", "-pk"]
        indexes = [models.Index(fields=["batch", "occurred_at"], name="ca_custody_batch_time_idx")]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Custody records are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Custody records cannot be deleted.")


class ChemicalInventoryMovement(models.Model):
    class MovementType(models.TextChoices):
        RECEIPT = "RECEIPT", "Batch receipt"
        UTILIZATION = "UTILIZATION", "Treatment utilization"
        RETURN = "RETURN", "Supplier return"
        ADJUSTMENT = "ADJUSTMENT", "Adjustment"

    batch = models.ForeignKey(
        ChemicalBatch, on_delete=models.PROTECT, related_name="inventory_movements"
    )
    sequence = models.PositiveBigIntegerField(default=1)
    movement_type = models.CharField(max_length=16, choices=MovementType.choices, db_index=True)
    quantity_delta = models.DecimalField(max_digits=18, decimal_places=6)
    quantity_after = models.DecimalField(max_digits=18, decimal_places=6)
    unit = models.CharField(max_length=10)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="chemical_inventory_movements",
    )
    idempotency_key = models.CharField(max_length=120, unique=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["batch", "sequence"], name="ca_stock_batch_sequence_idx")]
        constraints = [
            models.UniqueConstraint(fields=["batch", "sequence"], name="ca_stock_batch_sequence_unique"),
            models.CheckConstraint(condition=Q(quantity_after__gte=0), name="ca_stock_balance_nonnegative"),
            models.CheckConstraint(condition=~Q(quantity_delta=0), name="ca_stock_delta_nonzero"),
            models.CheckConstraint(
                condition=(
                    Q(movement_type="RECEIPT", quantity_delta__gt=0)
                    | Q(movement_type__in=["UTILIZATION", "RETURN"], quantity_delta__lt=0)
                    | Q(movement_type="ADJUSTMENT")
                ),
                name="ca_stock_movement_direction_valid",
            ),
        ]

    @classmethod
    @transaction.atomic
    def record(cls, *, batch, movement_type, quantity, actor=None, idempotency_key, notes=""):
        locked_batch = ChemicalBatch.objects.select_for_update().get(pk=batch.pk)
        if not idempotency_key or not str(idempotency_key).strip():
            raise ValidationError("An idempotency key is required for inventory movements.")
        quantity = Decimal(str(quantity))
        if not quantity.is_finite() or quantity <= 0:
            raise ValidationError("Inventory movement quantity must be a positive finite number.")
        movement_type = str(movement_type).upper()
        if movement_type == cls.MovementType.RECEIPT:
            delta = quantity
        elif movement_type in {cls.MovementType.UTILIZATION, cls.MovementType.RETURN}:
            delta = -quantity
        elif movement_type == cls.MovementType.ADJUSTMENT:
            delta = quantity
        else:
            raise ValidationError("Unsupported chemical inventory movement.")
        required_permission = {
            cls.MovementType.RECEIPT: "ca.receive_chemical_batch",
            cls.MovementType.UTILIZATION: "ca.utilize_chemical_batch",
            cls.MovementType.RETURN: "ca.return_chemical_batch",
            cls.MovementType.ADJUSTMENT: "ca.change_chemicalbatch",
        }[movement_type]
        require_ca_actor(actor, required_permission, plant_id=locked_batch.treatment_plant_id)
        existing = cls.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            if (
                existing.batch_id != locked_batch.pk
                or existing.movement_type != movement_type
                or existing.quantity_delta != delta
            ):
                raise ValidationError("This idempotency key was already used for a different inventory movement.")
            return existing
        if delta == 0:
            raise ValidationError("Inventory movement quantity must be non-zero.")
        latest = cls.objects.filter(batch=locked_batch).order_by("-sequence").first()
        if latest:
            available = latest.quantity_after
        elif movement_type == cls.MovementType.RECEIPT:
            # The receipt is the opening movement; quantity_received already
            # includes this delivery and must not be counted as opening stock.
            available = Decimal("0")
        else:
            available = Decimal(str(locked_batch.quantity_received or 0))
            already_used = locked_batch.quantity_utilized or Decimal("0")
            already_returned = locked_batch.quantity_returned or Decimal("0")
            available = max(Decimal("0"), available - already_used - already_returned)
        if delta < 0 and -delta > available:
            raise ValidationError("Movement exceeds the remaining quantity in this batch.")
        movement = cls(
            batch=locked_batch,
            sequence=(latest.sequence if latest else 0) + 1,
            movement_type=movement_type,
            quantity_delta=delta,
            quantity_after=available + delta,
            unit=locked_batch.unit,
            actor=actor,
            idempotency_key=idempotency_key,
            notes=notes,
        )
        movement._ledger_write = True
        try:
            with transaction.atomic():
                movement.save()
        except IntegrityError:
            existing = cls.objects.filter(idempotency_key=idempotency_key).first()
            if existing and (
                existing.batch_id == locked_batch.pk
                and existing.movement_type == movement_type
                and existing.quantity_delta == delta
            ):
                return existing
            raise ValidationError("Inventory movement sequence or idempotency key conflicted with another write.")
        return movement

    def clean(self):
        super().clean()
        if self.quantity_delta == 0:
            raise ValidationError({"quantity_delta": "Inventory movement quantity must be non-zero."})
        if self.quantity_after < 0:
            raise ValidationError({"quantity_after": "Inventory balance cannot be negative."})
        if self.batch_id and self.unit != self.batch.unit:
            raise ValidationError({"unit": "Inventory movement unit must match the batch unit."})
        if self.movement_type == self.MovementType.RECEIPT and self.quantity_delta < 0:
            raise ValidationError({"quantity_delta": "A receipt must increase stock."})
        if self.movement_type in {self.MovementType.UTILIZATION, self.MovementType.RETURN} and self.quantity_delta > 0:
            raise ValidationError({"quantity_delta": "Utilization and return movements must decrease stock."})

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Chemical inventory movements are append-only.")
        if not getattr(self, "_ledger_write", False):
            raise ValidationError("Record inventory movements through ChemicalInventoryMovement.record().")
        if self.sequence < 1:
            raise ValidationError({"sequence": "A positive ledger sequence is required."})
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Chemical inventory movements cannot be deleted.")


class BatchTestResultRevision(models.Model):
    result = models.ForeignKey(
        BatchTestResult, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="revisions",
    )
    batch = models.ForeignKey(
        ChemicalBatch, on_delete=models.PROTECT, related_name="result_revisions"
    )
    parameter = models.ForeignKey(
        ChemicalSpecification, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="result_revisions",
    )
    revision_number = models.PositiveSmallIntegerField()
    revision_reason = models.CharField(max_length=500)
    snapshot = models.JSONField(default=dict)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="chemical_result_revisions",
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["batch", "created_at"], name="ca_result_rev_batch_idx")]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Result revision snapshots are append-only.")
        if not str(self.revision_reason or "").strip():
            raise ValidationError({"revision_reason": "A documented reason is required for every result revision."})
        if self.actor_id is None:
            raise ValidationError({"actor": "The staff member responsible for the result revision must be recorded."})
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Result revision snapshots cannot be deleted.")


class ChemicalWorkflowAlert(models.Model):
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chemical_workflow_alerts"
    )
    batch = models.ForeignKey(
        ChemicalBatch, null=True, blank=True, on_delete=models.CASCADE, related_name="workflow_alerts"
    )
    analysis_request = models.ForeignKey(
        ChemicalAnalysisRequest, null=True, blank=True, on_delete=models.CASCADE,
        related_name="workflow_alerts",
    )
    alert_type = models.CharField(max_length=48, db_index=True)
    title = models.CharField(max_length=180)
    message = models.TextField()
    url = models.CharField(max_length=500)
    severity = models.CharField(max_length=16, default="medium")
    dedupe_key = models.CharField(max_length=160)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="acknowledged_chemical_workflow_alerts",
    )

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["recipient", "dedupe_key"], name="ca_alert_recipient_dedupe")]
        indexes = [models.Index(fields=["recipient", "acknowledged_at", "created_at"], name="ca_alert_inbox_idx")]

    @property
    def acknowledged(self):
        return self.acknowledged_at is not None

    @transaction.atomic
    def acknowledge(self, *, actor):
        alert = type(self).objects.select_for_update().select_related(
            "batch", "analysis_request"
        ).get(pk=self.pk)
        if alert.alert_type == "ca_interlab_referral":
            try:
                referral_id = int(str(alert.dedupe_key).rsplit(":", 1)[-1])
                referral = ChemicalLabReferral.objects.get(pk=referral_id)
            except (TypeError, ValueError, ChemicalLabReferral.DoesNotExist):
                raise ValidationError("The laboratory referral for this alert could not be found.")
            if referral.status != ChemicalLabReferral.Status.PENDING:
                raise ValidationError("This laboratory referral has already been submitted.")
            plant_id = referral.treatment_plant_id
        else:
            plant_id = alert.batch.treatment_plant_id if alert.batch_id else (
                alert.analysis_request.batch.treatment_plant_id
                if alert.analysis_request_id and alert.analysis_request.batch_id
                else None
            )
        require_ca_actor(actor, plant_id=plant_id)
        if alert.alert_type == "ca_analysis_assignment":
            if not alert.analysis_request_id or not alert.analysis_request.is_assigned_analyst(actor):
                raise PermissionDenied("Only an analyst assigned to this request may acknowledge it.")
        elif alert.alert_type in {"ca_assignment_pending", "ca_assignment_escalation"}:
            if not alert.analysis_request_id or not alert.analysis_request.assigned_analysts.exists():
                raise PermissionDenied("This assignment reminder remains active until an analyst is assigned.")
        elif actor.pk != alert.recipient_id and not actor.is_superuser:
            raise PermissionDenied("Only the notification recipient may acknowledge this alert.")
        if alert.acknowledged_at is None:
            alert.acknowledged_at = timezone.now()
            alert.acknowledged_by = actor
            alert.save(update_fields=["acknowledged_at", "acknowledged_by"])
            try:
                from alerts.models import VoiceAlert

                VoiceAlert.objects.filter(source_key=f"ca-workflow:{alert.pk}").update(repeat_due_at=None)
            except Exception:
                logger.exception("Could not stop CA workflow voice reminders for alert %s", alert.pk)

            def broadcast_acknowledgment():
                try:
                    from asgiref.sync import async_to_sync
                    from channels.layers import get_channel_layer

                    layer = get_channel_layer()
                    if layer:
                        async_to_sync(layer.group_send)(
                            f"user_{alert.recipient_id}_alerts",
                            {
                                "type": "alert_acknowledged",
                                "id": f"ca_workflow_alert_{alert.pk}",
                                "timestamp": alert.acknowledged_at.isoformat(),
                            },
                        )
                except Exception:
                    logger.exception("Could not broadcast acknowledgment for CA alert %s", alert.pk)

            transaction.on_commit(broadcast_acknowledgment)
        self.acknowledged_at = alert.acknowledged_at
        self.acknowledged_by_id = alert.acknowledged_by_id
        return self


class ChemicalCorrectiveAction(models.Model):
    """Append-only CAPA submissions with an independently recorded review."""

    class Status(models.TextChoices):
        OPEN = "OPEN", "Awaiting independent chemist verification"
        VERIFIED = "VERIFIED", "Verified"
        REJECTED = "REJECTED", "Rejected"

    process = models.ForeignKey(
        ChemicalAnalysisProcess,
        on_delete=models.PROTECT,
        related_name="corrective_actions",
    )
    batch = models.ForeignKey(
        ChemicalBatch,
        on_delete=models.PROTECT,
        related_name="corrective_actions",
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="submitted_chemical_corrective_actions",
    )
    qa_observation = models.TextField()
    corrective_action = models.TextField()
    preventive_action = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN, db_index=True)
    submitted_at = models.DateTimeField(default=timezone.now, db_index=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_chemical_corrective_actions",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-submitted_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["OPEN", "VERIFIED", "REJECTED"]),
                name="ca_capa_status_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["process", "status", "submitted_at"], name="ca_capa_process_status_idx"),
            models.Index(fields=["batch", "submitted_at"], name="ca_capa_batch_time_idx"),
        ]

    @classmethod
    @transaction.atomic
    def submit(cls, *, process, actor, qa_observation, corrective_action, preventive_action):
        if not process or not process.pk:
            raise ValidationError("A saved chemical analysis process is required for CAPA.")
        process = ChemicalAnalysisProcess.objects.select_for_update().get(pk=process.pk)
        if process.status != ChemicalAnalysisProcess.WorkflowStatus.QA:
            raise ValidationError("CAPA can only be submitted during chemist review.")
        if not process.analysis_completed_at or not process.is_analysis_complete or process.has_pending_tests:
            raise ValidationError("Finalize the required analytical results before submitting CAPA.")
        if actor is None or not getattr(actor, "is_active", False):
            raise ValidationError("An active QA officer is required to submit CAPA.")
        if not (actor.is_superuser or actor.has_perm("ca.review_chemical_analysis")):
            raise ValidationError("You do not have permission to submit CAPA.")
        if (
            process.batch.treatment_plant_id
            and not actor.is_superuser
            and getattr(actor, "assigned_treatment_plant_id", None) != process.batch.treatment_plant_id
        ):
            raise ValidationError("You cannot submit CAPA outside your treatment-plant scope.")
        if process.assigned_analysts.filter(pk=actor.pk).exists():
            raise ValidationError("An assigned analyst cannot submit QA corrective actions for their own analysis.")
        if process.qa_officer_id != actor.pk or not process.qa_reviewed_at:
            raise ValidationError("CAPA must be submitted by the QA officer who recorded the decision.")

        values = {
            "qa_observation": str(qa_observation or "").strip(),
            "corrective_action": str(corrective_action or "").strip(),
            "preventive_action": str(preventive_action or "").strip(),
        }
        missing = [label for key, label in (
            ("qa_observation", "QA observation"),
            ("corrective_action", "corrective action"),
            ("preventive_action", "preventive action"),
        ) if not values[key]]
        if missing:
            raise ValidationError("CAPA requires: " + ", ".join(missing) + ".")

        return cls.objects.create(
            process=process,
            batch=process.batch,
            submitted_by=actor,
            **values,
        )

    @transaction.atomic
    def review(self, *, actor, accepted, notes):
        record = type(self).objects.select_for_update().select_related("process", "batch").get(pk=self.pk)
        process = ChemicalAnalysisProcess.objects.select_for_update().get(pk=record.process_id)
        notes = str(notes or "").strip()
        if record.status != self.Status.OPEN:
            raise ValidationError("This corrective action has already been reviewed.")
        if actor is None or not getattr(actor, "is_active", False):
            raise ValidationError("An active chemist reviewer is required to review CAPA.")
        if not (actor.is_superuser or actor.has_perm("ca.review_chemical_analysis")):
            raise ValidationError("You do not have permission to review CAPA.")
        if (
            record.batch.treatment_plant_id
            and not actor.is_superuser
            and getattr(actor, "assigned_treatment_plant_id", None) != record.batch.treatment_plant_id
        ):
            raise ValidationError("You cannot review CAPA outside your treatment-plant scope.")
        if not notes:
            raise ValidationError("Record verification notes before closing this corrective action.")
        if record.submitted_by_id == actor.pk or process.assigned_analysts.filter(pk=actor.pk).exists():
            raise ValidationError("CAPA verification must be independent of its submitter and assigned analysts.")
        if process.status != ChemicalAnalysisProcess.WorkflowStatus.QA:
            raise ValidationError("CAPA can only be reviewed during chemist review.")
        if not process.analysis_completed_at or not process.is_analysis_complete or process.has_pending_tests:
            raise ValidationError("Finalize the required analytical results before reviewing CAPA.")
        if process.qa_officer_id == actor.pk:
            raise ValidationError("CAPA verification must be performed by a QA officer independent of the original decision.")
        if accepted and process.failed_tests.exists():
            raise ValidationError("CAPA cannot be verified as effective while analytical results remain failed.")

        record.status = self.Status.VERIFIED if accepted else self.Status.REJECTED
        record.reviewed_by = actor
        record.reviewed_at = timezone.now()
        record.review_notes = notes
        record._allow_review_update = True
        record.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_notes"])
        return record

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).values(
                "process_id", "batch_id", "submitted_by_id", "qa_observation",
                "corrective_action", "preventive_action", "submitted_at", "status",
            ).first()
            if original is None:
                raise ValidationError("The corrective action submission no longer exists.")
            immutable_fields = (
                "process_id", "batch_id", "submitted_by_id", "qa_observation",
                "corrective_action", "preventive_action", "submitted_at",
            )
            if any(getattr(self, field) != original[field] for field in immutable_fields):
                raise ValidationError("Submitted CAPA details are immutable; create a new corrective-action record.")
            if not getattr(self, "_allow_review_update", False):
                raise ValidationError("CAPA review changes must use the review workflow action.")
            if original["status"] != self.Status.OPEN or self.status not in {self.Status.VERIFIED, self.Status.REJECTED}:
                raise ValidationError("CAPA review status can only move once from open to a final decision.")
        elif self.process_id and self.batch_id and self.process.batch_id != self.batch_id:
            raise ValidationError("The CAPA record and chemical process must belong to the same batch.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Corrective-action submissions cannot be deleted.")


class ChemicalCoAAuditEvent(models.Model):
    class Action(models.TextChoices):
        APPROVED = "APPROVED", "Approved"
        REVOKED = "REVOKED", "Approval revoked"

    coa = models.ForeignKey(ChemicalCoA, on_delete=models.PROTECT, related_name="audit_events")
    action = models.CharField(max_length=16, choices=Action.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    content_hash = models.CharField(max_length=64, blank=True)
    details = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-occurred_at", "-pk"]
        indexes = [models.Index(fields=["coa", "occurred_at"], name="ca_coa_audit_time_idx")]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("CoA approval audit events are append-only.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("CoA approval audit events cannot be deleted.")


class ChemicalOutboxEvent(models.Model):
    """Durable, retryable publication record written with workflow changes."""

    event_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=180, unique=True)
    event_type = models.CharField(max_length=80, db_index=True)
    aggregate_type = models.CharField(max_length=80)
    aggregate_id = models.CharField(max_length=80)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    attempt_count = models.PositiveIntegerField(default=0)
    delivered_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at", "event_id"]
        indexes = [
            models.Index(fields=["delivered_at", "available_at"], name="ca_outbox_pending_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(attempt_count__gte=0), name="ca_outbox_attempts_nonnegative"),
        ]

    def __str__(self):
        return f"{self.event_type} · {self.aggregate_type} {self.aggregate_id}"


class ChemicalSuperuserPurgeAudit(models.Model):
    """Detached record of superuser purges that survives deleted CA roots."""

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    root_model = models.CharField(max_length=120)
    root_ids = models.JSONField(default=list)
    root_labels = models.JSONField(default=list)
    deleted_counts = models.JSONField(default=dict)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-occurred_at", "-pk"]


def _regenerate_approved_coa_assets(coa_id):
    """Generate the watermark, QR and PDF after the immutable approval hash is saved."""
    coa = ChemicalCoA.objects.get(pk=coa_id)
    if not coa.watermark or not coa.watermark.name:
        raise ValidationError("An approved certificate is missing its frozen watermark asset.")
    from ca.signals import _generate_coa_assets

    _generate_coa_assets(coa, force=True)



