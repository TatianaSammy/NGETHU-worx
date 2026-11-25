from django import forms

from ca.models import Chemical


class ChemicalChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        formula_status = "formula ready" if obj.hydrometer_formula else "formula not set"
        return f"{obj.name} · {obj.code} · {formula_status}"


class HydrometerConcentrationForm(forms.Form):
    chemical = ChemicalChoiceField(
        queryset=Chemical.objects.none(),
        label="Chemical",
        empty_label="Choose a chemical",
        widget=forms.Select(attrs={"class": "hydro-input", "autocomplete": "off"}),
    )
    reading = forms.DecimalField(
        label="Hydrometer reading",
        max_digits=10,
        decimal_places=3,
        min_value=0.001,
        widget=forms.NumberInput(attrs={
            "class": "hydro-input",
            "step": "0.001",
            "min": "0.001",
            "inputmode": "decimal",
            "placeholder": "Enter the observed reading",
        }),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["chemical"].queryset = Chemical.objects.order_by("name", "code")
