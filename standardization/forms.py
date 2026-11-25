from django import forms
from django.utils import timezone

from inventory.models import LaboratoryReagent

from .models import Standardization


class StandardizationForm(forms.ModelForm):
    class Meta:
        model = Standardization
        fields = (
            "standard",
            "titrant",
            "standard_normality",
            "aliquot_volume_ml",
            "titre1",
            "titre2",
            "titre3",
            "preparation_date",
            "remarks",
        )
        widgets = {
            "titrant": forms.Select(attrs={"class": "std-input"}),
            "standard": forms.Select(attrs={"class": "std-input"}),
            "standard_normality": forms.NumberInput(attrs={"class": "std-input", "step": "0.0001", "min": "0.0001"}),
            "aliquot_volume_ml": forms.NumberInput(attrs={"class": "std-input", "step": "0.01", "min": "0.01"}),
            "titre1": forms.NumberInput(attrs={"class": "std-input", "step": "0.01", "min": "0"}),
            "titre2": forms.NumberInput(attrs={"class": "std-input", "step": "0.01", "min": "0"}),
            "titre3": forms.NumberInput(attrs={"class": "std-input", "step": "0.01", "min": "0"}),
            "preparation_date": forms.DateInput(attrs={"class": "std-input", "type": "date"}),
            "remarks": forms.Textarea(attrs={"class": "std-input", "rows": 2, "placeholder": "Optional run notes"}),
        }
        labels = {
            "titrant": "Titrant reagent",
            "standard": "Aliquot (titrand)",
            "standard_normality": "Known titrand normality",
            "aliquot_volume_ml": "Aliquot volume (mL)",
            "preparation_date": "Preparation date",
        }
        help_texts = {
            "standard": "Select the reagent solution used as the titrand.",
            "standard_normality": "Enter the known normality of the selected titrand.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        reagents = LaboratoryReagent.objects.select_related("category").order_by("rname", "pk")
        self.fields["titrant"].queryset = reagents
        self.fields["standard"].queryset = reagents
        self.fields["preparation_date"].initial = timezone.localdate()
        for name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "std-input")
            if field.required:
                field.widget.attrs["required"] = "required"

    def clean(self):
        cleaned = super().clean()
        titrant = cleaned.get("titrant")
        standard = cleaned.get("standard")
        if titrant and standard and titrant.pk == standard.pk:
            self.add_error("standard", "Choose a different reagent for the aliquot (titrand).")

        aliquot = cleaned.get("aliquot_volume_ml")
        if aliquot is not None and aliquot <= 0:
            self.add_error("aliquot_volume_ml", "Aliquot volume must be greater than zero.")

        standard_normality = cleaned.get("standard_normality")
        if standard_normality is not None and standard_normality <= 0:
            self.add_error("standard_normality", "Known titrand normality must be greater than zero.")

        titres = [cleaned.get(name) for name in ("titre1", "titre2", "titre3")]
        if all(value is not None for value in titres) and sum(titres) <= 0:
            self.add_error("titre3", "The three titre readings must have a positive average.")
        return cleaned

    def save(self, commit=True, *, user=None):
        instance = super().save(commit=False)
        if user is not None:
            instance.prepared_by = user
        if commit:
            instance.save()
            self.save_m2m()
        return instance
