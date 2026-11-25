# standardization/models.py

from django.db import models
from django.contrib.auth import get_user_model
from django.core.validators import MinValueValidator
from django.core.exceptions import ValidationError

from inventory.models import LaboratoryReagent

User = get_user_model()


class Standardization(models.Model):
    titrant = models.ForeignKey(
        LaboratoryReagent,
        on_delete=models.CASCADE,
        related_name="titrant_standardizations"
    )

    standard = models.ForeignKey(
        LaboratoryReagent,
        on_delete=models.CASCADE,
        related_name="standard_standardizations"
    )

    standard_normality = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        help_text="Known normality of standard"
    )

    aliquot_volume_ml = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        help_text="Volume pipetted"
    )

    titre1 = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(0)]
    )

    titre2 = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(0)]
    )

    titre3 = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(0)]
    )

    average_titre = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True
    )

    actual_normality = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        null=True,
        blank=True
    )

    prepared_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    preparation_date = models.DateField()

    remarks = models.TextField(
        blank=True,
        null=True
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    class Meta:
        ordering = ("-preparation_date", "-created_at", "-pk")

    def clean(self):
        super().clean()
        errors = {}
        if self.standard_normality is not None and self.standard_normality <= 0:
            errors["standard_normality"] = "Known titrand normality must be greater than zero."
        if self.aliquot_volume_ml is not None and self.aliquot_volume_ml <= 0:
            errors["aliquot_volume_ml"] = "Aliquot volume must be greater than zero."
        titres = (self.titre1, self.titre2, self.titre3)
        if all(value is not None for value in titres) and sum(titres) <= 0:
            errors["titre3"] = "The three titre readings must have a positive average."
        if self.titrant_id and self.standard_id and self.titrant_id == self.standard_id:
            errors["standard"] = "Choose a different reagent for the aliquot (titrand)."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):

        self.full_clean()

        self.average_titre = (
            self.titre1 +
            self.titre2 +
            self.titre3
        ) / 3

        # N1V1 = N2V2
        if self.average_titre <= 0:
            raise ValidationError("Titre average must be greater than zero.")

        self.actual_normality = (
            self.standard_normality *
            self.aliquot_volume_ml
        ) / self.average_titre

        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"{self.titrant.rname} "
            f"standardized with {self.standard.rname}"
        )
