import string
import random
from decimal import Decimal

from django.db import models, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.core.exceptions import ValidationError

from accounts.models import CustomUser
from t.models import TreatmentPlant
from sc.models import Shift, ShiftAssignment
from parameters.models import WWQParameter_qad
from analysis_methods.models import WWAnalysisMethod
from core.quality_results import parameter_result_metadata


# =========================================================
# UTIL
# =========================================================

def generate_reference_id(length=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


# =========================================================
# BASE AUDIT MODEL
# =========================================================

class AuditModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField(default=False, db_index=True)

    class Meta:
        abstract = True


# =========================================================
# STATE MACHINE (SINGLE SOURCE OF TRUTH)
# =========================================================

class WaterAnalysisState(models.TextChoices):
    NEW = "NEW", "New"
    ANALYSED = "ANALYSED", "Analysed"
    VERIFIED = "VERIFIED", "Verified"
    CONFIRMED = "CONFIRMED", "Confirmed"
    APPROVED = "APPROVED", "Approved"


STATE_TRANSITIONS = {
    WaterAnalysisState.NEW: [WaterAnalysisState.ANALYSED],
    WaterAnalysisState.ANALYSED: [WaterAnalysisState.VERIFIED],
    WaterAnalysisState.VERIFIED: [WaterAnalysisState.CONFIRMED],
    WaterAnalysisState.CONFIRMED: [WaterAnalysisState.APPROVED],
    WaterAnalysisState.APPROVED: [],
}

STATE_PERMISSIONS = {
    WaterAnalysisState.ANALYSED: "can_analyse",
    WaterAnalysisState.VERIFIED: "can_verify",
    WaterAnalysisState.CONFIRMED: "can_confirm",
    WaterAnalysisState.APPROVED: "can_approve",
}

STATE_USER_FIELD = {
    WaterAnalysisState.ANALYSED: "analysed_by",
    WaterAnalysisState.VERIFIED: "verified_by",
    WaterAnalysisState.CONFIRMED: "confirmed_by",
    WaterAnalysisState.APPROVED: "approved_by",
}


# =========================================================
# SAMPLING SITE
# =========================================================

class WasteWaterSamplingSite(AuditModel):
    treatment_plant = models.ForeignKey(
        TreatmentPlant,
        on_delete=models.CASCADE,
        related_name="lab_sampling_sites"
    )
    name = models.CharField(max_length=100)
    location_description = models.TextField(blank=True, null=True)

    class Meta:
        unique_together = ("treatment_plant", "name")

    def __str__(self):
        return f"{self.name} ({self.treatment_plant.name})"


# =========================================================
# SAMPLE
# =========================================================

class WasteWaterSample(AuditModel):
    reference_id = models.CharField(
        max_length=10,
        default=generate_reference_id,
        unique=True,
        db_index=True
    )

    treatment_plant = models.ForeignKey(TreatmentPlant, on_delete=models.CASCADE)
    sampling_site = models.ForeignKey(WasteWaterSamplingSite, on_delete=models.PROTECT, related_name="samples")
    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, null=True, blank=True)
    assignment = models.ForeignKey(ShiftAssignment, on_delete=models.PROTECT)

    sampling_date = models.DateField()
    sampling_time = models.TimeField()

    def clean(self):
        if self.sampling_site.treatment_plant_id != self.treatment_plant_id:
            raise ValidationError("Sampling site does not belong to selected plant.")

    def save(self, *args, **kwargs):
        self.full_clean()

        # Auto shift resolution (safe fallback)
        if not self.shift_id:
            self.shift = Shift.objects.filter(
                start_time__lte=self.sampling_time,
                end_time__gt=self.sampling_time
            ).first()

        super().save(*args, **kwargs)

    def __str__(self):
        return f"Sample {self.reference_id}"


# =========================================================
# ANALYSIS (BASE PATTERN)
# =========================================================

class BaseAnalysis(AuditModel):
    status = models.CharField(
        max_length=20,
        choices=WaterAnalysisState.choices,
        default=WaterAnalysisState.NEW,
        db_index=True
    )

    analysed_by = models.ForeignKey(CustomUser, on_delete=models.PROTECT, null=True, blank=True, related_name="%(class)s_analysed")
    verified_by = models.ForeignKey(CustomUser, on_delete=models.PROTECT, null=True, blank=True, related_name="%(class)s_verified")
    confirmed_by = models.ForeignKey(CustomUser, on_delete=models.PROTECT, null=True, blank=True, related_name="%(class)s_confirmed")
    approved_by = models.ForeignKey(CustomUser, on_delete=models.PROTECT, null=True, blank=True, related_name="%(class)s_approved")

    analysis_datetime = models.DateTimeField(default=timezone.now)
    remarks = models.TextField(blank=True)

    class Meta:
        abstract = True

    def clean(self):
        if isinstance(self.analysis_datetime, str):
            parsed = parse_datetime(self.analysis_datetime)
            if not parsed:
                raise ValidationError("Invalid datetime format.")
            self.analysis_datetime = parsed

    def transition(self, new_state, user=None, has_permission=True):
        if new_state not in STATE_TRANSITIONS[self.status]:
            raise ValidationError(f"Invalid transition {self.status} → {new_state}")

        if new_state in STATE_PERMISSIONS and not has_permission:
            raise ValidationError("Permission denied for state transition.")

        user_field = STATE_USER_FIELD.get(new_state)
        if user_field and user:
            setattr(self, user_field, user)

        self.status = new_state
        self.save(update_fields=["status", user_field] if user_field else ["status"])


# =========================================================
# WASTE WATER ANALYSIS
# =========================================================

class WasteWaterAnalysis(BaseAnalysis):
    sample = models.OneToOneField(WasteWaterSample, on_delete=models.CASCADE, related_name="analysis")

    def __str__(self):
        return f"WW Analysis {self.sample.reference_id}"


# =========================================================
# PARAMETER RESULT
# =========================================================

class WasteWaterParameterResult(AuditModel):
    analysis = models.ForeignKey(WasteWaterAnalysis, on_delete=models.CASCADE, related_name="parameter_results")

    parameter = models.ForeignKey(WWQParameter_qad, on_delete=models.CASCADE)
    analysis_method = models.ForeignKey(WWAnalysisMethod, on_delete=models.CASCADE)

    result = models.DecimalField(max_digits=10, decimal_places=2)

    effluent_standard = models.CharField(max_length=100, editable=False)
    kebs_reference = models.CharField(max_length=255, editable=False)
    quality_compliance = models.CharField(max_length=20, editable=False)

    class Meta:
        unique_together = ("analysis", "parameter")

    def save(self, *args, **kwargs):
        metadata = parameter_result_metadata(self.parameter, self.analysis_method, self.result)
        for field, value in metadata.items():
            setattr(self, field, value)
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | set(metadata) | {"updated_at"}
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.parameter.name}: {self.result}"


# =========================================================
# SAMPLE TYPES
# =========================================================

class IndustrialSample(AuditModel):
    reference_id = models.CharField(max_length=12, default=generate_reference_id, unique=True)
    client_name = models.CharField(max_length=255)
    location = models.CharField(max_length=255)

    sampling_date = models.DateField()
    sampling_time = models.TimeField()

    shift = models.ForeignKey(Shift, on_delete=models.PROTECT)
    assignment = models.ForeignKey(ShiftAssignment, on_delete=models.PROTECT)


class TreatmentPlantSample(AuditModel):
    reference_id = models.CharField(max_length=12, default=generate_reference_id, unique=True)
    treatment_plant = models.ForeignKey(TreatmentPlant, on_delete=models.CASCADE)

    sampling_point_description = models.CharField(max_length=255)

    sampling_date = models.DateField()
    sampling_time = models.TimeField()

    shift = models.ForeignKey(Shift, on_delete=models.PROTECT)
    assignment = models.ForeignKey(ShiftAssignment, on_delete=models.PROTECT)


class ExhausterSample(AuditModel):
    reference_id = models.CharField(max_length=12, default=generate_reference_id, unique=True)

    exhauster_reg_number = models.CharField(max_length=50)
    driver_name = models.CharField(max_length=255)

    sampling_date = models.DateField()
    sampling_time = models.TimeField()

    shift = models.ForeignKey(Shift, on_delete=models.PROTECT)
    assignment = models.ForeignKey(ShiftAssignment, on_delete=models.PROTECT)


# =========================================================
# ANALYSIS SPECIALIZATIONS
# =========================================================

class IndustrialSampleAnalysis(BaseAnalysis):
    sample = models.OneToOneField(IndustrialSample, on_delete=models.CASCADE, related_name="analysis")


class TreatmentPlantSampleAnalysis(BaseAnalysis):
    sample = models.OneToOneField(TreatmentPlantSample, on_delete=models.CASCADE, related_name="analysis")


class ExhausterSampleAnalysis(BaseAnalysis):
    sample = models.OneToOneField(ExhausterSample, on_delete=models.CASCADE, related_name="analysis")


# =========================================================
# GENERIC PARAMETER RESULT BASE
# =========================================================

class BaseParameterResult(AuditModel):
    parameter = models.ForeignKey(WWQParameter_qad, on_delete=models.CASCADE)
    analysis_method = models.ForeignKey(WWAnalysisMethod, on_delete=models.CASCADE)

    result = models.DecimalField(max_digits=10, decimal_places=2)

    effluent_standard = models.CharField(max_length=100, editable=False)
    kebs_reference = models.CharField(max_length=255, editable=False)
    quality_compliance = models.CharField(max_length=20, editable=False)

    class Meta:
        abstract = True

    def compute(self):
        metadata = parameter_result_metadata(self.parameter, self.analysis_method, self.result)
        for field, value in metadata.items():
            setattr(self, field, value)

    def save(self, *args, **kwargs):
        self.compute()
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {
                "effluent_standard",
                "kebs_reference",
                "quality_compliance",
                "updated_at",
            }
        super().save(*args, **kwargs)


class IndustrialSampleParameterResult(BaseParameterResult):
    analysis = models.ForeignKey(IndustrialSampleAnalysis, on_delete=models.CASCADE, related_name="parameter_results")


class TreatmentPlantSampleParameterResult(BaseParameterResult):
    analysis = models.ForeignKey(TreatmentPlantSampleAnalysis, on_delete=models.CASCADE, related_name="parameter_results")


class ExhausterSampleParameterResult(BaseParameterResult):
    analysis = models.ForeignKey(ExhausterSampleAnalysis, on_delete=models.CASCADE, related_name="parameter_results")
