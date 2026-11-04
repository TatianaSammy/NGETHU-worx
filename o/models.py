#o/models.py
import random
import string
from datetime import time
from decimal import Decimal
from typing import Optional

from django.db import models, connection
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db.models.signals import post_migrate
from django.dispatch import receiver
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounts.models import CustomUser
from t.models import TreatmentPlant, TreatmentPlantPhase
from ca.models import Chemical
from sc.models import ShiftAssignment
from xmicalconcentration.models import XmicalConcentration


def generate_uuid() -> str:
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))


def get_current_shift_assignment(user: CustomUser, date) -> Optional[ShiftAssignment]:
    try:
        return ShiftAssignment.objects.filter(employee=user, date=date).latest('id')
    except ShiftAssignment.DoesNotExist:
        return None


def get_latest_concentration(chemical: Chemical, plant: TreatmentPlant) -> Optional[XmicalConcentration]:
    return XmicalConcentration.objects.filter(
        chemical=chemical,
        treatment_plant=plant
    ).order_by('-measurement_date').first()


def get_latest_record(model_name: str, chemical: Chemical, plant: TreatmentPlant, chemical_lookup: Optional[str] = None):
    app_label, model_name = model_name.split('.', 1)
    try:
        Model = apps.get_model(app_label, model_name)
    except LookupError:
        return None

    dosage_field = next(
        (field for field in ("established_dosage_mg_per_l", "established_dosage_ml") if any(item.name == field for item in Model._meta.fields)),
        None,
    )
    if dosage_field is None:
        return None

    filters = {
        "treatment_plant": plant,
        f"{dosage_field}__isnull": False,
    }
    if chemical_lookup:
        filters[chemical_lookup] = chemical
    if any(field.name == "approved_at" for field in Model._meta.fields):
        filters["approved_at__isnull"] = False
    return Model.objects.filter(**filters).order_by("-test_date", "-pk").first()


def calculate_expected_dosage(flow: float, dosage: float, concentration: float) -> Optional[float]:
    if not concentration or concentration <= 0:
        return None
    return round((flow * dosage) / (concentration * 10), 2)


class MonitoringTime(models.Model):
    time = models.TimeField(unique=True)

    class Meta:
        ordering = ['time']
        verbose_name = _("Monitoring Time")
        verbose_name_plural = _("Monitoring Times")

    def __str__(self) -> str:
        return self.time.strftime("%H:%M")


@receiver(post_migrate)
def create_hourly_monitoring_times(sender, **kwargs):
    if sender.name == __name__.rsplit('.', 1)[0]:
        MonitoringTimeModel = apps.get_model(sender.name, 'MonitoringTime')
        table_name = MonitoringTimeModel._meta.db_table
        with connection.cursor() as cursor:
            if table_name not in connection.introspection.table_names(cursor):
                return
        if not MonitoringTimeModel.objects.exists():
            MonitoringTimeModel.objects.bulk_create([
                MonitoringTimeModel(time=time(hour=h)) for h in range(24)
            ])


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ChemicalCategory(models.Model):
    class Category(models.TextChoices):
        COAGULANT = 'Coagulant', _('Coagulant')
        PH_ADJUSTER = 'PH Adjuster', _('PH Adjuster')
        DISINFECTANT = 'Disinfectant', _('Disinfectant')
        FLOCULANT = 'Flocculant', _('Flocculant')

    chemical = models.ForeignKey(Chemical, on_delete=models.CASCADE, related_name='categories')
    category = models.CharField(max_length=50, choices=Category.choices)

    class Meta:
        indexes = [models.Index(fields=["chemical", "category"])]
        verbose_name = _("Chemical Category")
        verbose_name_plural = _("Chemical Categories")

    def __str__(self) -> str:
        return f"{self.chemical.name} - {self.get_category_display()}"


class ChemicalPump(models.Model):
    name = models.CharField(max_length=100)
    phase = models.ForeignKey(TreatmentPlantPhase, on_delete=models.CASCADE, related_name='chemical_pumps')
    category = models.CharField(max_length=50, choices=ChemicalCategory.Category.choices)

    def __str__(self) -> str:
        return f"{self.name} - {self.get_category_display()} ({self.phase.name})"


class PumpStatus(TimeStampedModel):
    class Status(models.TextChoices):
        WORKING = 'working', _('Working')
        FAULTY = 'faulty', _('Faulty')
        MAINTENANCE = 'maintenance', _('Maintenance')
        DECOMMISSIONED = 'decommissioned', _('Decommissioned')

    pump = models.ForeignKey(ChemicalPump, on_delete=models.CASCADE, related_name='statuses')
    treatment_plant = models.ForeignKey(TreatmentPlant, on_delete=models.CASCADE, related_name='pump_statuses')
    status = models.CharField(max_length=20, choices=Status.choices)
    remarks = models.TextField(blank=True, null=True)
    updated_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.pump.name} - {self.get_status_display()}"


class ChemicalTank(models.Model):
    name = models.CharField(max_length=100)
    phase = models.ForeignKey(TreatmentPlantPhase, on_delete=models.CASCADE, related_name='chemical_tanks')
    capacity_liters = models.FloatField()
    category = models.CharField(max_length=50, choices=ChemicalCategory.Category.choices)

    def __str__(self) -> str:
        return f"{self.name} - {self.get_category_display()} ({self.phase.name})"


class ChemicalStock(TimeStampedModel):
    treatment_plant = models.ForeignKey(TreatmentPlant, on_delete=models.CASCADE, related_name="chemical_stocks")
    chemical = models.ForeignKey(Chemical, on_delete=models.CASCADE, related_name="chemical_stocks")
    quantity_kg = models.FloatField(default=0.0)

    class Meta:
        unique_together = ("treatment_plant", "chemical")

    def deduct(self, amount: float):
        self.quantity_kg = max(0.0, self.quantity_kg - amount)
        self.save(update_fields=["quantity_kg", "updated_at"])


class BaseUsageRecord(TimeStampedModel):
    treatment_plant = models.ForeignKey(TreatmentPlant, on_delete=models.CASCADE)
    phase = models.ForeignKey(TreatmentPlantPhase, on_delete=models.CASCADE)
    raw_water_flow_rate = models.FloatField()
    pump_used = models.ForeignKey(ChemicalPump, on_delete=models.SET_NULL, null=True)
    tank_used = models.ForeignKey(ChemicalTank, on_delete=models.SET_NULL, null=True)
    actual_dosage = models.FloatField()
    expected_dosage = models.FloatField(null=True, blank=True)
    concentration_ppm = models.FloatField(null=True, blank=True)
    date = models.DateField(default=timezone.now)
    monitoring_time = models.ForeignKey(MonitoringTime, on_delete=models.PROTECT)
    recorded_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
    shift_assignment = models.ForeignKey(ShiftAssignment, on_delete=models.SET_NULL, null=True, blank=True)
    remarks = models.TextField(blank=True, null=True)

    class Meta:
        abstract = True

    def clean(self):
        if self.raw_water_flow_rate <= 0:
            raise ValidationError(_("Flow rate must be greater than zero."))
        if self.actual_dosage < 0:
            raise ValidationError(_("Dosage cannot be negative."))

    def set_shift_if_missing(self):
        if self.recorded_by and not self.shift_assignment:
            self.shift_assignment = get_current_shift_assignment(self.recorded_by, self.date)

    def update_stock_and_log(self, chemical, qty):
        stock, _ = ChemicalStock.objects.get_or_create(treatment_plant=self.treatment_plant, chemical=chemical)
        stock.deduct(qty)
        ChemicalUsageRecord.objects.update_or_create(
            treatment_plant=self.treatment_plant,
            chemical=chemical,
            date_used=self.date,
            defaults={
                "quantity_used_kg": models.F("quantity_used_kg") + qty if ChemicalUsageRecord.objects.filter(
                    treatment_plant=self.treatment_plant,
                    chemical=chemical,
                    date_used=self.date
                ).exists() else qty,
                "recorded_by": self.recorded_by
            }
        )


class CoagulantUsageRecord(BaseUsageRecord):
    chemical = models.ForeignKey(Chemical, on_delete=models.CASCADE, limit_choices_to={'categories__category': 'Coagulant'})

    def save(self, *args, **kwargs):
        self.set_shift_if_missing()
        if not self.chemical.categories.filter(category='Coagulant').exists():
            raise ValidationError(_("Selected chemical is not a Coagulant."))
        concentration = get_latest_concentration(self.chemical, self.treatment_plant)
        dosage = get_latest_record('jartest.JarTest', self.chemical, self.treatment_plant, "coagulant")
        if concentration and dosage:
            self.concentration_ppm = float(concentration.concentration_mg_per_l)
            self.expected_dosage = calculate_expected_dosage(self.raw_water_flow_rate, float(dosage.established_dosage_mg_per_l), self.concentration_ppm)
        super().save(*args, **kwargs)
        self.update_stock_and_log(self.chemical, self.actual_dosage)


class PHAdjusterUsageRecord(BaseUsageRecord):
    chemical = models.ForeignKey(Chemical, on_delete=models.CASCADE, limit_choices_to={'categories__category': 'PH Adjuster'})

    def save(self, *args, **kwargs):
        self.set_shift_if_missing()
        if not self.chemical.categories.filter(category='PH Adjuster').exists():
            raise ValidationError(_("Selected chemical is not a PH Adjuster."))
        concentration = get_latest_concentration(self.chemical, self.treatment_plant)
        dosage = get_latest_record('PHadjustment.PHAdjustmentTest', self.chemical, self.treatment_plant, "source_jar__jar_test__coagulant")
        if concentration and dosage:
            self.concentration_ppm = float(concentration.concentration_mg_per_l)
            dosage_value = getattr(dosage, "established_dosage_mg_per_l", None)
            if dosage_value is not None:
                self.expected_dosage = calculate_expected_dosage(self.raw_water_flow_rate, float(dosage_value), self.concentration_ppm)
        super().save(*args, **kwargs)
        self.update_stock_and_log(self.chemical, self.actual_dosage)


class DisinfectantUsageRecord(BaseUsageRecord):
    chemical = models.ForeignKey(Chemical, on_delete=models.CASCADE, limit_choices_to={'categories__category': 'Disinfectant'})
    actual_reading = models.FloatField()
    expected_reading = models.FloatField(null=True, blank=True)

    def save(self, *args, **kwargs):
        self.set_shift_if_missing()
        if not self.chemical.categories.filter(category='Disinfectant').exists():
            raise ValidationError(_("Selected chemical is not a Disinfectant."))
        concentration = get_latest_concentration(self.chemical, self.treatment_plant)
        dosage = get_latest_record('chlorinedemand.ChlorineDemandTest', self.chemical, self.treatment_plant)
        if concentration and dosage:
            self.concentration_ppm = float(concentration.concentration_mg_per_l)
            self.expected_reading = calculate_expected_dosage(self.raw_water_flow_rate, float(dosage.established_dosage_mg_per_l), self.concentration_ppm)
        super().save(*args, **kwargs)
        self.update_stock_and_log(self.chemical, self.actual_dosage)


class FlocculantUsageRecord(BaseUsageRecord):
    chemical = models.ForeignKey(Chemical, on_delete=models.CASCADE, limit_choices_to={'categories__category': 'Flocculant'})
    batch_number = models.CharField(max_length=100, blank=True, null=True)
    flocculant_type = models.CharField(max_length=255)
    quantity_used_kg = models.FloatField()

    def save(self, *args, **kwargs):
        self.set_shift_if_missing()
        if not self.chemical.categories.filter(category='Flocculant').exists():
            raise ValidationError(_("Selected chemical is not a Flocculant."))
        concentration = get_latest_concentration(self.chemical, self.treatment_plant)
        dosage = get_latest_record('flocculation.FlocculantTest', self.chemical, self.treatment_plant)
        if concentration and dosage:
            self.concentration_ppm = float(concentration.concentration_mg_per_l)
            self.expected_dosage = calculate_expected_dosage(self.raw_water_flow_rate, float(dosage.established_dosage_mg_per_l), self.concentration_ppm)
        super().save(*args, **kwargs)
        self.update_stock_and_log(self.chemical, self.quantity_used_kg)


class ChemicalUsageRecord(TimeStampedModel):
    treatment_plant = models.ForeignKey(TreatmentPlant, on_delete=models.CASCADE, related_name='chemical_usages')
    chemical = models.ForeignKey(Chemical, on_delete=models.CASCADE, related_name='usage_records')
    quantity_used_kg = models.FloatField()
    date_used = models.DateField(default=timezone.now)
    recorded_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
    remarks = models.TextField(blank=True, null=True)

    class Meta:
        unique_together = ('treatment_plant', 'chemical', 'date_used')
        indexes = [models.Index(fields=['treatment_plant', 'chemical', 'date_used'])]
        verbose_name = _("Chemical Usage Record")
        verbose_name_plural = _("Chemical Usage Records")

    def __str__(self) -> str:
        return f"{self.chemical.name} on {self.date_used} at {self.treatment_plant.name}"


class Reservoir(models.Model):
    name = models.CharField(max_length=100)
    phase = models.ForeignKey(TreatmentPlantPhase, on_delete=models.CASCADE, related_name='reservoirs')
    length_m = models.DecimalField(max_digits=8, decimal_places=2)
    width_m = models.DecimalField(max_digits=8, decimal_places=2)
    depth_m = models.DecimalField(max_digits=8, decimal_places=2)

    def volume_m3(self) -> Decimal:
        return round(self.length_m * self.width_m * self.depth_m, 2)

    def __str__(self) -> str:
        return f"{self.name} ({self.phase})"


class ReservoirLevel(TimeStampedModel):
    reservoir = models.ForeignKey(Reservoir, on_delete=models.CASCADE, related_name='levels')
    level_m = models.DecimalField(max_digits=8, decimal_places=2)
    date = models.DateField(default=timezone.now)
    monitoring_time = models.ForeignKey(MonitoringTime, on_delete=models.PROTECT)

    def volume_m3(self) -> Decimal:
        return round(self.level_m * self.reservoir.length_m * self.reservoir.width_m, 2)

    def __str__(self) -> str:
        return f"{self.reservoir.name} - {self.level_m}m @ {self.monitoring_time}"
