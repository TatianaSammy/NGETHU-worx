#heavy_metal/models.py
import string
import random
import logging
from datetime import datetime, timedelta, time as dt_time

from django.db import models
from django.apps import apps
from django.db.models.signals import post_migrate
from django.dispatch import receiver
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from accounts.models import CustomUser
from t.models import (
    TreatmentPlant,
    TreatmentPlantPhase,
    HeavyMetalLimit,
)
from sc.models import Shift

User = CustomUser

MAX_DIGITS = 10
DECIMAL_PLACES = 3
logger = logging.getLogger(__name__)

# ==========================================================
# 🧱 BASE UTILITIES
# ==========================================================

class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


def generate_reference_id(length=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

# ==========================================================
# ⏱ SAMPLING SYSTEM (QC-MIRRORED)
# ==========================================================

class HeavyMetalSamplingSchedule(TimeStampedModel):
    start_time = models.TimeField()
    interval_minutes = models.PositiveIntegerField()
    enabled = models.BooleanField(default=True)
    adaptive = models.BooleanField(default=False)
    last_auto_update = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["start_time"]

    def __str__(self):
        return f"Heavy Metal every {self.interval_minutes} min from {self.start_time}"


class HeavyMetalSamplingTime(models.Model):
    schedule = models.ForeignKey(
        HeavyMetalSamplingSchedule,
        on_delete=models.CASCADE,
        related_name="times"
    )
    time = models.TimeField()

    class Meta:
        unique_together = ("schedule", "time")
        ordering = ["time"]

    def __str__(self):
        return self.time.strftime("%H:%M")


@receiver(post_migrate)
def create_default_heavymetal_schedule(sender, **kwargs):
    if sender.name != "heavymetal":
        return

    Schedule = HeavyMetalSamplingSchedule
    TimeModel = HeavyMetalSamplingTime

    if not Schedule.objects.exists():
        Schedule.objects.create(
            start_time=dt_time(6, 0),
            interval_minutes=240
        )

    for schedule in Schedule.objects.filter(enabled=True):
        start = datetime.combine(timezone.localdate(), schedule.start_time)
        end = datetime.combine(timezone.localdate(), dt_time(23, 59))
        delta = timedelta(minutes=schedule.interval_minutes)

        while start <= end:
            TimeModel.objects.get_or_create(
                schedule=schedule,
                time=start.time()
            )
            start += delta

# ==========================================================
# ⚙️ HEAVY METAL ANALYSIS CORE
# ==========================================================

class HeavyMetalAnalysisState(models.TextChoices):
    NEW = 'new', _('New')
    ANALYSED = 'analysed', _('Analysed')
    VERIFIED = 'verified', _('Verified')
    CONFIRMED = 'confirmed', _('Confirmed')
    APPROVED = 'approved', _('Approved')


class HeavyMetalAnalysis(TimeStampedModel):
    reference_id = models.CharField(max_length=16, unique=True, editable=False)

    treatment_plant = models.ForeignKey(
        TreatmentPlant, on_delete=models.CASCADE, related_name="heavymetal_analyses"
    )
    treatment_plant_phase = models.ForeignKey(
        TreatmentPlantPhase, on_delete=models.CASCADE, related_name="heavymetal_analyses"
    )
    shift = models.ForeignKey(
        Shift, on_delete=models.PROTECT, null=True, blank=True, related_name="heavymetal_analyses"
    )

    sampling_time = models.ForeignKey(
        HeavyMetalSamplingTime,
        on_delete=models.PROTECT,
        related_name="analyses"
    )
    analysis_date = models.DateField(default=timezone.localdate)

    state = models.CharField(
        max_length=20,
        choices=HeavyMetalAnalysisState.choices,
        default=HeavyMetalAnalysisState.NEW
    )

    analysed_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)

    analysed_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="analysed_heavymetal"
    )
    verified_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="verified_heavymetal"
    )
    approved_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="approved_heavymetal"
    )

    remarks = models.TextField(blank=True, null=True)

    class Meta:
        unique_together = ("treatment_plant", "treatment_plant_phase", "analysis_date", "sampling_time", "shift")
        ordering = ["analysis_date", "sampling_time__time"]

    def __str__(self):
        return f"{self.treatment_plant.name} | {self.reference_id}"

    def clean(self):
        if self.treatment_plant_phase.treatment_plant != self.treatment_plant:
            raise ValidationError(_("Phase does not belong to selected treatment plant."))

    def save(self, *args, **kwargs):
        if not self.reference_id:
            self.reference_id = generate_reference_id()
        is_new = self.pk is None
        super().save(*args, **kwargs)

        if is_new:
            HeavyMetalAnalysisLog.objects.create(
                analysis=self,
                action="created",
                remarks="Analysis slot created",
                auto_generated=True
            )
            self._auto_create_expected_results()

    def _auto_create_expected_results(self):
        limits = HeavyMetalLimit.objects.filter(
            treatment_plant=self.treatment_plant
        )

        existing = set(
            HeavyMetalParameter.objects.filter(analysis=self)
            .values_list("parameter_id", flat=True)
        )

        placeholders = [
            HeavyMetalParameter(
                analysis=self,
                parameter=limit,
                value=0.0,
                manually_editable=True,
                entry_status=HeavyMetalParameter.EntryStatus.PENDING,
            )
            for limit in limits if limit.id not in existing
        ]

        if placeholders:
            HeavyMetalParameter.objects.bulk_create(placeholders)
            try:
                from alerts.engine import AlertEngine

                AlertEngine.trigger_alert(self, alert_type="heavymetal_creation")
            except Exception:
                logger.exception("Could not raise the heavy metal analysis creation alert")

# ==========================================================
# 🔬 PARAMETER RESULTS
# ==========================================================

class HeavyMetalParameter(TimeStampedModel):
    class EntryStatus(models.TextChoices):
        RECORDED = "recorded", _("Recorded")
        PENDING = "pending", _("Pending entry")
        LEGACY_UNVERIFIED = "legacy_unverified", _("Verify historical value")

    analysis = models.ForeignKey(
        HeavyMetalAnalysis,
        on_delete=models.CASCADE,
        related_name="parameters"
    )
    parameter = models.ForeignKey(HeavyMetalLimit, on_delete=models.CASCADE)
    value = models.FloatField(default=0.0)
    manually_editable = models.BooleanField(default=False)
    entry_status = models.CharField(
        max_length=24,
        choices=EntryStatus.choices,
        default=EntryStatus.RECORDED,
        db_index=True,
    )

    class Meta:
        unique_together = ("analysis", "parameter")

    def __str__(self):
        return f"{self.parameter.parameter.name}: {self.value}"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        update_fields = kwargs.get("update_fields")
        status_changed = bool(update_fields and "entry_status" in update_fields)
        value_updated = bool(update_fields and "value" in update_fields)
        if self.pk and update_fields is None:
            previous_value = type(self).objects.filter(pk=self.pk).values_list("value", flat=True).first()
            if previous_value != self.value:
                value_updated = True
        if value_updated:
            self.entry_status = self.EntryStatus.RECORDED
            if update_fields is not None:
                kwargs["update_fields"] = tuple(set(update_fields) | {"entry_status"})
        super().save(*args, **kwargs)
        if self.entry_status != self.EntryStatus.RECORDED:
            return
        if not (is_new or value_updated or status_changed):
            return
        from .utils import detect_heavymetal_nonconformity
        detect_heavymetal_nonconformity(
            self.parameter,
            self.value,
            self.analysis,
            self.analysis.verified_by
        )

# ==========================================================
# 🚨 NONCONFORMITIES
# ==========================================================

class NonConformityStatus(models.TextChoices):
    OPEN = "open", _("Open")
    UNDER_REVIEW = "under_review", _("Under Review")
    CLOSED = "closed", _("Closed")


class Cause(TimeStampedModel):
    name = models.CharField(max_length=255, unique=True)
    description = models.TextField(blank=True, null=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True
    )

    def __str__(self):
        return self.name


class HeavyMetalNonConformity(TimeStampedModel):
    analysis = models.ForeignKey(
        HeavyMetalAnalysis,
        on_delete=models.CASCADE,
        related_name="nonconformities"
    )
    parameter_name = models.CharField(max_length=128)
    observed_value = models.FloatField()
    min_limit = models.FloatField()
    max_limit = models.FloatField()
    deviation = models.FloatField()

    triggered_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True
    )
    auto_detected = models.BooleanField(default=True)

    status = models.CharField(
        max_length=32,
        choices=NonConformityStatus.choices,
        default=NonConformityStatus.OPEN
    )

    corrective_action = models.TextField(blank=True, null=True)
    preventive_action = models.TextField(blank=True, null=True)
    root_cause = models.TextField(blank=True, null=True)

    causes = models.ManyToManyField(Cause, blank=True)

    reviewed_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reviewed_heavymetal_nc"
    )

    resolved_at = models.DateTimeField(null=True, blank=True)
    escalation_level = models.PositiveIntegerField(default=0)
    risk_score = models.FloatField(default=0.0)

    def __str__(self):
        return f"HM-NC {self.parameter_name} ({self.status})"

    def evaluate_risk(self):
        deviation_pct = abs(self.deviation) / max(self.max_limit, 1)
        score = 1 + deviation_pct * 5
        self.risk_score = round(min(score, 10), 2)
        self.save(update_fields=["risk_score"])

    def close(self, actor=None, remarks=""):
        self.status = NonConformityStatus.CLOSED
        self.resolved_at = timezone.now()
        self.save(update_fields=["status", "resolved_at"])

        HeavyMetalNonConformityLog.objects.create(
            nonconformity=self,
            actor=actor,
            action="closed",
            remarks=remarks or "Automatically closed",
            auto_generated=True
        )

# ==========================================================
# 🧾 LOGS
# ==========================================================

class HeavyMetalNonConformityLog(TimeStampedModel):
    nonconformity = models.ForeignKey(
        HeavyMetalNonConformity,
        on_delete=models.CASCADE,
        related_name="logs"
    )
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True
    )
    action = models.CharField(max_length=64)
    remarks = models.TextField(blank=True, null=True)
    auto_generated = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} on {self.nonconformity}"


class HeavyMetalAnalysisLog(TimeStampedModel):
    analysis = models.ForeignKey(
        HeavyMetalAnalysis,
        on_delete=models.CASCADE,
        related_name="logs"
    )
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True
    )
    action = models.CharField(max_length=64)
    remarks = models.TextField(blank=True, null=True)
    auto_generated = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} on {self.analysis.reference_id}"
