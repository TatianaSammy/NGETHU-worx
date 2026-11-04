#t/models.py
import string
import random
from datetime import timedelta
from django.conf import settings
from django.db import models, transaction
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from parameters.models import PhysicochemicalParameter, MicrobialParameter, HeavyMetalParameter


def generate_uuid():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))


class TreatmentPlant(models.Model):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=10, unique=True)  # e.g., KSM, NRB, THK
    location = models.CharField(max_length=200)  # Simplified from GIS PointField

    def __str__(self):
        return f"{self.code} - {self.name}"



class TreatmentPlantPhase(models.Model):
    name = models.CharField(max_length=100)
    treatment_plant = models.ForeignKey(TreatmentPlant, related_name='phases', on_delete=models.CASCADE)
    design_capacity = models.FloatField(help_text="Design capacity in cubic meters per hour")
    operational_capacity = models.FloatField(help_text="Operational capacity in cubic meters per hour")

    def __str__(self):
        return f"{self.name} - {self.treatment_plant.name}"


# ========================
# Filter System
# ========================
class FilterType(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.name


class WaterFilter(models.Model):
    # Compatibility choices for existing dashboard and serializer consumers;
    # the persisted state now lives on FilterStatus.
    STATUS_CHOICES = [
        ('working', 'Working'),
        ('backwashing', 'Backwashing'),
        ('isolated', 'Isolated'),
        ('maintenance', 'Maintenance'),
        ('fault', 'Fault'),
        ('out_of_service', 'Out of service'),
    ]

    name = models.CharField(max_length=100)
    filter_type = models.ForeignKey(FilterType, on_delete=models.PROTECT, related_name='water_filters')
    location = models.CharField(max_length=200, blank=True)
    capacity = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(0.001)],
        help_text="Rated filter throughput in m³/hr.",
    )
    flow_rate = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text="Latest recorded flow in m³/hr.",
    )
    treatment_plant = models.ForeignKey(TreatmentPlant, on_delete=models.CASCADE, related_name='filters', null=True, blank=True)
    phase = models.ForeignKey(TreatmentPlantPhase, on_delete=models.SET_NULL, related_name='filters', null=True, blank=True)

    class Meta:
        ordering = ["treatment_plant__name", "phase__name", "name"]
        indexes = [
            models.Index(fields=["treatment_plant", "phase", "name"], name="t_filter_plant_phase_idx"),
        ]

    def __str__(self):
        return self.name

    @property
    def status(self):
        try:
            return self.filter_status.status
        except FilterStatus.DoesNotExist:
            return "unknown"

    def get_status_display(self):
        try:
            return self.filter_status.get_status_display()
        except FilterStatus.DoesNotExist:
            return "Status not recorded"

    @property
    def capacity_utilization_percent(self):
        if self.capacity is None or self.capacity <= 0 or self.flow_rate is None:
            return None
        return round(float(self.flow_rate) * 100 / float(self.capacity), 1)

    @property
    def remaining_capacity(self):
        if self.capacity is None or self.flow_rate is None:
            return None
        return max(0.0, float(self.capacity) - float(self.flow_rate))

    @property
    def is_over_capacity(self):
        utilization = self.capacity_utilization_percent
        return utilization is not None and utilization > 100

    def clean(self):
        super().clean()
        errors = {}
        if self.capacity is not None and self.capacity <= 0:
            errors["capacity"] = "Rated capacity must be greater than zero."
        if self.flow_rate is not None and self.flow_rate < 0:
            errors["flow_rate"] = "Flow rate cannot be negative."
        if (
            self.phase_id
            and self.treatment_plant_id
            and self.phase.treatment_plant_id != self.treatment_plant_id
        ):
            errors["phase"] = "The selected phase must belong to the selected treatment plant."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
        FilterStatus.objects.get_or_create(water_filter=self)


class FilterStatus(models.Model):
    class Status(models.TextChoices):
        WORKING = "working", "Working"
        BACKWASHING = "backwashing", "Backwashing"
        ISOLATED = "isolated", "Isolated"
        MAINTENANCE = "maintenance", "Maintenance"
        FAULT = "fault", "Fault"
        OUT_OF_SERVICE = "out_of_service", "Out of service"

    water_filter = models.OneToOneField(
        WaterFilter,
        on_delete=models.CASCADE,
        related_name="filter_status",
    )
    installation_date = models.DateField(null=True, blank=True)
    last_maintenance_date = models.DateField(null=True, blank=True)
    next_maintenance_date = models.DateField(null=True, blank=True, db_index=True)
    maintenance_interval_days = models.PositiveSmallIntegerField(
        default=180,
        validators=[MinValueValidator(1), MaxValueValidator(3650)],
        help_text="Planned maintenance interval used when no next-service date is set.",
    )
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.WORKING, db_index=True)
    condition_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    status_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["water_filter__name"]
        indexes = [
            models.Index(fields=["status", "next_maintenance_date"], name="t_fstatus_due_idx"),
        ]

    def __str__(self):
        return f"{self.water_filter.name}: {self.get_status_display()}"

    @property
    def age_days(self):
        if not self.installation_date:
            return None
        return max(0, (timezone.localdate() - self.installation_date).days)

    @property
    def age_years(self):
        days = self.age_days
        return round(days / 365.25, 1) if days is not None else None

    @property
    def effective_next_maintenance_date(self):
        if self.next_maintenance_date:
            return self.next_maintenance_date
        baseline = self.last_maintenance_date or self.installation_date
        if baseline and self.maintenance_interval_days:
            return baseline + timedelta(days=self.maintenance_interval_days)
        return None

    @property
    def maintenance_due(self):
        due_date = self.effective_next_maintenance_date
        return bool(due_date and due_date <= timezone.localdate())

    @property
    def days_to_maintenance(self):
        due_date = self.effective_next_maintenance_date
        return (due_date - timezone.localdate()).days if due_date else None

    def clean(self):
        super().clean()
        errors = {}
        if self.installation_date and self.installation_date > timezone.localdate():
            errors["installation_date"] = "Installation date cannot be in the future."
        if self.last_maintenance_date and self.installation_date and self.last_maintenance_date < self.installation_date:
            errors["last_maintenance_date"] = "Maintenance date cannot precede installation."
        if self.next_maintenance_date and self.last_maintenance_date and self.next_maintenance_date < self.last_maintenance_date:
            errors["next_maintenance_date"] = "Next maintenance cannot precede the last maintenance date."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        prior = None
        if self.pk:
            prior = type(self).objects.filter(pk=self.pk).values(
                "status", "last_maintenance_date", "condition_percent"
            ).first()
        with transaction.atomic():
            super().save(*args, **kwargs)
            if prior is None:
                return
            events = []
            if prior["status"] != self.status:
                events.append((
                    FilterStatusEvent.EventType.STATUS_CHANGE,
                    f"Status changed from {prior['status']} to {self.status}.",
                ))
            if prior["last_maintenance_date"] != self.last_maintenance_date and self.last_maintenance_date:
                events.append((
                    FilterStatusEvent.EventType.MAINTENANCE,
                    f"Maintenance date updated to {self.last_maintenance_date:%Y-%m-%d}.",
                ))
            if prior["condition_percent"] != self.condition_percent and self.condition_percent is not None:
                events.append((
                    FilterStatusEvent.EventType.CONDITION_UPDATE,
                    f"Condition updated to {self.condition_percent}%.",
                ))
            for event_type, remarks in events:
                FilterStatusEvent.objects.create(
                    filter_status=self,
                    event_type=event_type,
                    status=self.status,
                    condition_percent=self.condition_percent,
                    remarks=remarks,
                    performed_by=getattr(self, "_changed_by", None),
                )


class FilterStatusEvent(models.Model):
    class EventType(models.TextChoices):
        STATUS_CHANGE = "status_change", "Status change"
        MAINTENANCE = "maintenance", "Maintenance update"
        CONDITION_UPDATE = "condition_update", "Condition update"
        INSPECTION = "inspection", "Inspection"
        BACKWASH = "backwash", "Backwash"

    filter_status = models.ForeignKey(FilterStatus, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=24, choices=EventType.choices)
    status = models.CharField(max_length=24, choices=FilterStatus.Status.choices)
    condition_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    remarks = models.TextField(blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="filter_status_events",
    )
    recorded_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-recorded_at", "-pk"]
        indexes = [
            models.Index(fields=["filter_status", "-recorded_at"], name="t_fevent_status_time_idx"),
        ]

    def __str__(self):
        return f"{self.filter_status.water_filter} · {self.get_event_type_display()} · {self.recorded_at:%Y-%m-%d %H:%M}"


# ========================
# Shared Status Model
# ========================
class Status(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        verbose_name_plural = "Statuses"

    def __str__(self):
        return self.name


# ========================
# Clarifier and Valves
# ========================
class Clarifier(models.Model):
    FLOW_TYPE_CHOICES = [
        ('vertical', 'Vertical Flow'),
        ('horizontal', 'Horizontal Flow'),
    ]
    DESIGN_CHOICES = [
        ('cuboid', 'Cuboid'),
        ('cylindrical', 'Cylindrical'),
    ]

    name = models.CharField(max_length=100)
    design_capacity = models.FloatField(help_text="Design capacity in m³/hr", default=10)
    installation_date = models.DateField()
    last_maintenance_date = models.DateField()
    treatment_plant = models.ForeignKey(TreatmentPlant, on_delete=models.CASCADE, related_name='clarifiers')
    phase = models.ForeignKey(TreatmentPlantPhase, on_delete=models.CASCADE, related_name='clarifiers', null=True, blank=True)
    clarifier_capacity = models.FloatField(help_text="Clarifier capacity in m³", default=10)
    surface_area = models.FloatField(help_text="Surface area in m²", default=10000)
    surface_loading = models.FloatField(help_text="Surface loading rate in m³/m²/hr", default=10000)
    flow_type = models.CharField(max_length=50, choices=FLOW_TYPE_CHOICES, default='vertical')
    design = models.CharField(max_length=50, choices=DESIGN_CHOICES, default='cuboid')
    depth = models.FloatField(help_text="Depth in meters", default=10)
    diameter = models.FloatField(blank=True, null=True, help_text="Diameter (for cylindrical)")
    length = models.FloatField(blank=True, null=True, help_text="Length (for cuboid)")
    width = models.FloatField(blank=True, null=True, help_text="Width (for cuboid)")

    def __str__(self):
        return self.name

    def clean(self):
        if self.design == 'cylindrical':
            if self.flow_type != 'horizontal':
                raise ValidationError('Cylindrical must use horizontal flow.')
            if self.length or self.width:
                raise ValidationError('Length and width must be empty for cylindrical.')
            if not self.diameter:
                raise ValidationError('Diameter is required for cylindrical.')
        elif self.design == 'cuboid':
            if self.flow_type != 'vertical':
                raise ValidationError('Cuboid must use vertical flow.')
            if self.diameter:
                raise ValidationError('Diameter must be empty for cuboid.')
            if not (self.length and self.width):
                raise ValidationError('Length and width are required for cuboid.')

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class DisludgeValve(models.Model):
    clarifier = models.ForeignKey(Clarifier, on_delete=models.CASCADE, related_name='disludge_valves')
    name = models.CharField(max_length=100)
    status = models.ForeignKey(Status, on_delete=models.PROTECT)

    def __str__(self):
        return f"{self.name} - {self.status}"


# ========================
# Abstract Water Quality Limit Base
# ========================
class AbstractWaterLimit(models.Model):
    treatment_plant = models.ForeignKey(TreatmentPlant, on_delete=models.CASCADE)
    min_value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    max_value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        abstract = True


class TreatedWaterLimit(AbstractWaterLimit):
    parameter = models.ForeignKey(PhysicochemicalParameter, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.parameter.name} limit for {self.treatment_plant.name}"


class MicrobialLimit(AbstractWaterLimit):
    parameter = models.ForeignKey(MicrobialParameter, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.parameter.name} limit for {self.treatment_plant.name}"


class HeavyMetalLimit(AbstractWaterLimit):
    parameter = models.ForeignKey(HeavyMetalParameter, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.parameter.name} limit for {self.treatment_plant.name}"


class RawWaterLimit(AbstractWaterLimit):
    parameter = models.ForeignKey(PhysicochemicalParameter, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.parameter.name} limit for {self.treatment_plant.name}"


class ClarifiedWaterLimit(AbstractWaterLimit):
    parameter = models.ForeignKey(PhysicochemicalParameter, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.parameter.name} limit for {self.treatment_plant.name}"


class CoagulatedWaterLimit(AbstractWaterLimit):
    parameter = models.ForeignKey(PhysicochemicalParameter, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.parameter.name} limit for {self.treatment_plant.name}"


class FilteredWaterLimit(AbstractWaterLimit):
    parameter = models.ForeignKey(PhysicochemicalParameter, on_delete=models.CASCADE)
    water_filter = models.ForeignKey(
        WaterFilter,
        on_delete=models.CASCADE,
        related_name='filtered_water_limits',
        null=True,
        blank=True,
        help_text="Filter this parameter limit applies to.",
    )

    def clean(self):
        super().clean()
        if not self.water_filter_id:
            return
        water_filter = self.water_filter
        if water_filter.treatment_plant_id not in (None, self.treatment_plant_id):
            raise ValidationError("The selected filter belongs to a different treatment plant.")
        if not water_filter.phase_id:
            raise ValidationError("A filtered-water limit must be linked to a filter assigned to a phase.")
        if water_filter.phase.treatment_plant_id != self.treatment_plant_id:
            raise ValidationError("The selected filter phase belongs to a different treatment plant.")

    def __str__(self):
        return f"{self.parameter.name} limit for {self.treatment_plant.name}"
