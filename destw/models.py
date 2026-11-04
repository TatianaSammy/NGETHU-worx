#destw/models.py
from django.db import models
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.core.exceptions import ValidationError
import string
import random

from accounts.models import CustomUser
from t.models import TreatmentPlant
from sc.models import Shift, ShiftAssignment
from parameters.models import WWQParameter_qad
from analysis_methods.models import WWAnalysisMethod
from core.quality_results import parameter_result_metadata

def generate_reference_id(length=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

class AuditModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField(default=False)

    class Meta:
        abstract = True

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

# ------------------ Waste Water Sampling Sites ------------------ #
class WasteWaterSamplingSite(AuditModel):
    treatment_plant = models.ForeignKey(TreatmentPlant, on_delete=models.CASCADE, related_name='destw_sampling_sites')
    name = models.CharField(max_length=100)
    location_description = models.TextField(blank=True, null=True)

    class Meta:
        unique_together = ('treatment_plant', 'name')
        verbose_name = 'Waste Water Sampling Site'
        verbose_name_plural = 'Waste Water Sampling Sites'

    def __str__(self):
        return f"{self.name} ({self.treatment_plant.name})"

# ------------------ Waste Water Samples ------------------ #
class WasteWaterSample(AuditModel):
    reference_id = models.CharField(max_length=10, default=generate_reference_id, unique=True, db_index=True)
    treatment_plant = models.ForeignKey(TreatmentPlant, on_delete=models.CASCADE, related_name='destw_samples')
    sampling_site = models.ForeignKey(WasteWaterSamplingSite, on_delete=models.PROTECT, related_name='destw_samples')
    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, related_name='destw_samples')
    assignment = models.ForeignKey(ShiftAssignment, on_delete=models.PROTECT, related_name='destw_samples')
    sampling_date = models.DateField()
    sampling_time = models.TimeField()

    def save(self, *args, **kwargs):
        if self.sampling_site.treatment_plant != self.treatment_plant:
            raise ValidationError("Sampling Site does not belong to the selected Treatment Plant.")

        if not self.shift_id:
            self.shift = Shift.objects.filter(
                start_time__lte=self.sampling_time,
                end_time__gt=self.sampling_time
            ).first()

        super().save(*args, **kwargs)

    def __str__(self):
        return f"Sample {self.reference_id} at {self.sampling_site.name} ({self.treatment_plant.name})"

# ------------------ Waste Water Analysis ------------------ #
class WasteWaterAnalysis(AuditModel):
    sample = models.OneToOneField(WasteWaterSample, on_delete=models.CASCADE, related_name='destw_analysis')
    status = models.CharField(max_length=20, choices=WaterAnalysisState.choices, default=WaterAnalysisState.NEW, db_index=True)

    analysed_by = models.ForeignKey(CustomUser, on_delete=models.PROTECT, null=True, blank=True, related_name="destw_analysed_by")
    verified_by = models.ForeignKey(CustomUser, on_delete=models.PROTECT, null=True, blank=True, related_name="destw_verified_by")
    confirmed_by = models.ForeignKey(CustomUser, on_delete=models.PROTECT, null=True, blank=True, related_name="destw_confirmed_by")
    approved_by = models.ForeignKey(CustomUser, on_delete=models.PROTECT, null=True, blank=True, related_name="destw_approved_by")

    analysis_datetime = models.DateTimeField(default=timezone.now)
    remarks = models.TextField(blank=True)

    def clean(self):
        if isinstance(self.analysis_datetime, str):
            parsed = parse_datetime(self.analysis_datetime)
            if parsed:
                self.analysis_datetime = parsed
            else:
                raise ValidationError("Invalid datetime string format for analysis_datetime.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Analysis for {self.sample.reference_id} [{self.get_status_display()}]"

    def change_status(self, new_state, user, user_has_permission=True):
        if new_state not in STATE_TRANSITIONS[self.status]:
            raise ValidationError(f"Invalid transition from {self.status} to {new_state}")

        required_permission = STATE_PERMISSIONS.get(new_state)
        if required_permission and not user_has_permission:
            raise ValidationError(f"User lacks permission: {required_permission}")

        field = STATE_USER_FIELD.get(new_state)
        if field:
            setattr(self, field, user)

        self.status = new_state
        self.save()

# ------------------ Waste Water Parameter Results ------------------ #
class WasteWaterParameterResult(AuditModel):
    analysis = models.ForeignKey(WasteWaterAnalysis, on_delete=models.CASCADE, related_name='destw_parameter_results')
    parameter = models.ForeignKey(WWQParameter_qad, on_delete=models.CASCADE, related_name='destw_parameter_results')
    analysis_method = models.ForeignKey(WWAnalysisMethod, on_delete=models.CASCADE, related_name='destw_parameter_results')
    result = models.DecimalField(max_digits=10, decimal_places=2)
    effluent_standard = models.CharField(max_length=100, editable=False)
    kebs_reference = models.CharField(max_length=255, editable=False)
    quality_compliance = models.CharField(max_length=20, editable=False)

    class Meta:
        unique_together = ('analysis', 'parameter')

    def save(self, *args, **kwargs):
        metadata = parameter_result_metadata(self.parameter, self.analysis_method, self.result)
        for field, value in metadata.items():
            setattr(self, field, value)
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | set(metadata) | {"updated_at"}
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.parameter.name}: {self.result}"

# ------------------ Waste Water Stabilization Ponds ------------------ #
class WasteWaterStabilizationPond(AuditModel):
    name = models.CharField(max_length=100)
    treatment_plant = models.ForeignKey(TreatmentPlant, on_delete=models.CASCADE, related_name='destw_stabilization_ponds')
    description = models.TextField(blank=True)

    def __str__(self):
        return f"{self.name} - {self.treatment_plant.name}"

class WasteWaterPondSamplingSite(AuditModel):
    pond = models.ForeignKey(WasteWaterStabilizationPond, on_delete=models.CASCADE, related_name='destw_sampling_sites')
    site_name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    def __str__(self):
        return f"{self.site_name} ({self.pond.name})"

class WasteWaterPondSample(AuditModel):
    site = models.ForeignKey(WasteWaterPondSamplingSite, on_delete=models.CASCADE, related_name='destw_samples')
    reference_id = models.CharField(max_length=10, default=generate_reference_id, unique=True, db_index=True)
    sampling_date = models.DateField()
    sampling_time = models.TimeField()
    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, related_name='destw_pond_samples')
    assignment = models.ForeignKey(ShiftAssignment, on_delete=models.PROTECT, related_name='destw_pond_samples')

    def save(self, *args, **kwargs):
        if not self.shift_id:
            self.shift = Shift.objects.filter(
                start_time__lte=self.sampling_time,
                end_time__gt=self.sampling_time
            ).first()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Pond Sample {self.reference_id} at {self.site}"

class WasteWaterPondAnalysis(AuditModel):
    sample = models.OneToOneField(WasteWaterPondSample, on_delete=models.CASCADE, related_name='destw_pond_analysis')
    status = models.CharField(max_length=20, choices=WaterAnalysisState.choices, default=WaterAnalysisState.NEW, db_index=True)

    analysed_by = models.ForeignKey(CustomUser, on_delete=models.PROTECT, null=True, blank=True, related_name="destw_pond_analysed_by")
    verified_by = models.ForeignKey(CustomUser, on_delete=models.PROTECT, null=True, blank=True, related_name="destw_pond_verified_by")
    confirmed_by = models.ForeignKey(CustomUser, on_delete=models.PROTECT, null=True, blank=True, related_name="destw_pond_confirmed_by")
    approved_by = models.ForeignKey(CustomUser, on_delete=models.PROTECT, null=True, blank=True, related_name="destw_pond_approved_by")

    analysis_datetime = models.DateTimeField(default=timezone.now)
    remarks = models.TextField(blank=True)

    def clean(self):
        if isinstance(self.analysis_datetime, str):
            parsed = parse_datetime(self.analysis_datetime)
            if parsed:
                self.analysis_datetime = parsed
            else:
                raise ValidationError("Invalid datetime string format for analysis_datetime.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Pond Analysis for {self.sample.reference_id} [{self.get_status_display()}]"

    def change_status(self, new_state, user, user_has_permission=True):
        if new_state not in STATE_TRANSITIONS[self.status]:
            raise ValidationError(f"Invalid transition from {self.status} to {new_state}")

        required_permission = STATE_PERMISSIONS.get(new_state)
        if required_permission and not user_has_permission:
            raise ValidationError(f"User lacks permission: {required_permission}")

        field = STATE_USER_FIELD.get(new_state)
        if field:
            setattr(self, field, user)

        self.status = new_state
        self.save()

class WasteWaterPondParameterResult(AuditModel):
    analysis = models.ForeignKey(WasteWaterPondAnalysis, on_delete=models.CASCADE, related_name='destw_pond_parameter_results')
    parameter = models.ForeignKey(WWQParameter_qad, on_delete=models.CASCADE, related_name='destw_pond_parameter_results')
    analysis_method = models.ForeignKey(WWAnalysisMethod, on_delete=models.CASCADE, related_name='destw_pond_parameter_results')
    result = models.DecimalField(max_digits=10, decimal_places=2)
    effluent_standard = models.CharField(max_length=100, editable=False)
    kebs_reference = models.CharField(max_length=255, editable=False)
    quality_compliance = models.CharField(max_length=20, editable=False)

    class Meta:
        unique_together = ('analysis', 'parameter')

    def save(self, *args, **kwargs):
        metadata = parameter_result_metadata(self.parameter, self.analysis_method, self.result)
        for field, value in metadata.items():
            setattr(self, field, value)
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | set(metadata) | {"updated_at"}
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.parameter.name}: {self.result}"
