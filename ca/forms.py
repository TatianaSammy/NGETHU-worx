from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import F, Q
from django.utils import timezone

from t.models import TreatmentPlant
from weeklytasks.models import DailyTaskAssignment

from .models import (
    BatchTestResult,
    Chemical,
    ChemicalAnalysisInput,
    ChemicalAnalysisProcess,
    ChemicalAnalysisRequest,
    ChemicalBatch,
    ChemicalBatchHold,
    ChemicalLabComparisonResult,
    ChemicalLabReferral,
    ChemicalMethodQualityPlan,
    ChemicalMethodQCControl,
    ChemicalQCCheckResult,
    ChemicalOOSInvestigation,
    ChemicalKEBSReferral,
    ChemicalCertApprover,
    ChemicalCoA,
    ChemicalLocation,
    ChemicalSpecification,
    ChemicalSpecificationRemark,
    PackagingStatus,
    ChecklistForWaterTreatmentChemicalAnalysis,
    Supplier,
)


User = get_user_model()

FORM_CONTROL_CLASS = (
    "block w-full rounded-lg border border-gray-300 bg-white "
    "px-3 py-2 text-sm text-gray-900 shadow-sm "
    "focus:border-primary-500 focus:ring-primary-500"
)

CHECKBOX_CLASS = (
    "h-4 w-4 rounded border-gray-300 text-primary-600 "
    "focus:ring-primary-500"
)

CHEMICAL_ANALYSIS_TASK_NAME = "Water Treatment Chemical Analysis"


class InventoryEquipmentChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, equipment):
        from .inventory_instruments import equipment_calibration_info

        category = getattr(getattr(equipment, "category", None), "name", "Equipment")
        calibration = equipment_calibration_info(equipment)
        serial = equipment.serial_number or "No serial"
        due = calibration["due_date"].isoformat() if calibration["due_date"] else "no due date"
        return f"{equipment.name} · {serial} · {category} · calibration {calibration['status']} ({due})"


def apply_field_classes(form: forms.BaseForm) -> None:
    for name, field in form.fields.items():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs.setdefault("class", "ca-compact-checkbox")
        elif isinstance(widget, forms.CheckboxSelectMultiple):
            widget.attrs.setdefault("class", "analyst-option")
        elif isinstance(widget, (forms.DateInput, forms.DateTimeInput)) or "date" in name.lower():
            if isinstance(widget, forms.DateInput):
                widget.input_type = "date"
            elif isinstance(widget, forms.DateTimeInput):
                widget.input_type = "datetime-local"
            widget.attrs["class"] = (widget.attrs.get("class", "") + " ca-compact-input ca-date-picker").strip()
            widget.attrs.setdefault("autocomplete", "off")
        elif isinstance(widget, forms.Select):
            widget.attrs["class"] = (widget.attrs.get("class", "") + " ca-compact-select").strip()
        elif isinstance(widget, forms.Textarea):
            widget.attrs["class"] = (widget.attrs.get("class", "") + " ca-compact-textarea").strip()
            if "rows" not in widget.attrs:
                widget.attrs["rows"] = 2
        else:
            widget.attrs["class"] = (widget.attrs.get("class", "") + " ca-compact-input").strip()


def _pk(value):
    return getattr(value, "pk", value)


def _plant_id(value):
    return _pk(value)


def _user_plant(user):
    return getattr(user, "assigned_treatment_plant", None)


def _user_plant_id(user):
    return getattr(user, "assigned_treatment_plant_id", None)


def _user_is_superuser(user) -> bool:
    return bool(user and getattr(user, "is_superuser", False))


def _user_active(user) -> bool:
    return bool(user and getattr(user, "is_active", False))


def _has_perm(user, permission: str) -> bool:
    return bool(
        user
        and _user_active(user)
        and (
            _user_is_superuser(user)
            or user.has_perm(permission)
        )
    )


def _require_active_user(
    user,
    message="An authenticated active user is required.",
):
    if user is None:
        raise ValidationError(message)

    if not user.is_active:
        raise ValidationError(
            "The selected user is inactive."
        )


def _require_permission(
    user,
    permission: str,
    message=None,
):
    if not _has_perm(user, permission):
        raise ValidationError(
            message
            or f"You do not have permission to perform this action."
        )


def _effective_plant(user=None, treatment_plant=None):
    if user is not None and not _user_is_superuser(user):
        return _user_plant(user)

    return treatment_plant


def _validate_plant_access(
    user,
    treatment_plant,
    field="treatment_plant",
):
    if (
        user is not None
        and not _user_is_superuser(user)
    ):
        assigned_id = _user_plant_id(user)

        if assigned_id is None:
            raise ValidationError(
                "Your account is not assigned to a treatment plant."
            )

        if (
            treatment_plant is None
            or _plant_id(treatment_plant) != assigned_id
        ):
            raise ValidationError(
                f"You cannot select another treatment plant."
            )


def _scope_plant_queryset(
    queryset,
    user=None,
    treatment_plant=None,
    field="treatment_plant",
):
    if user is not None and not _user_is_superuser(user):
        plant_id = _user_plant_id(user)

        if plant_id is None:
            return queryset.none()

        return queryset.filter(
            **{f"{field}_id": plant_id}
        )

    if treatment_plant is not None:
        return queryset.filter(
            **{
                f"{field}_id": _plant_id(
                    treatment_plant
                )
            }
        )

    return queryset


def scoped_user_queryset(
    treatment_plant=None,
    *,
    section=None,
    user=None,
):
    queryset = User.objects.filter(
        is_active=True
    )

    if user is not None and not _user_is_superuser(user):
        plant_id = _user_plant_id(user)

        if plant_id is None:
            return queryset.none()

        queryset = queryset.filter(
            assigned_treatment_plant_id=plant_id
        )
    elif treatment_plant is not None:
        queryset = queryset.filter(
            assigned_treatment_plant_id=_plant_id(
                treatment_plant
            )
        )

    if section is not None:
        queryset = queryset.filter(
            section_id=_pk(section)
        )

    return queryset.distinct().order_by(
        "first_name",
        "last_name",
        "username",
    )


def chemical_analysis_task_queryset(
    *,
    treatment_plant=None,
    section=None,
    assignment_date=None,
    current_time=None,
):
    assignment_date = (
        assignment_date
        or timezone.localdate()
    )

    current_time = (
        current_time
        or timezone.localtime().time()
    )

    queryset = DailyTaskAssignment.objects.filter(
        task__task_template__name__iexact=CHEMICAL_ANALYSIS_TASK_NAME,
        task__task_template__is_active=True,
        task__week__start_date__lte=assignment_date,
        task__week__end_date__gte=assignment_date,
        date=assignment_date,
        user__is_active=True,
        shift__is_deleted=False,
    )

    shift_match = (
        Q(
            shift__start_time__lte=current_time,
            shift__end_time__gt=current_time,
        )
        |
        Q(
            shift__start_time__gt=F("shift__end_time"),
        )
        & (
            Q(shift__start_time__lte=current_time)
            | Q(shift__end_time__gt=current_time)
        )
    )

    queryset = queryset.filter(shift_match)

    if treatment_plant is not None:
        plant_id = _plant_id(treatment_plant)

        queryset = queryset.filter(
            task__treatment_plant_id=plant_id,
            user__assigned_treatment_plant_id=plant_id,
        )

    if section is not None:
        section_id = _pk(section)

        queryset = queryset.filter(
            task__section_id=section_id,
            user__section_id=section_id,
        )

    return queryset.distinct()


def chemical_analysis_analyst_queryset(
    *,
    treatment_plant=None,
    batch=None,
    analysis_request=None,
    assignment_date=None,
    current_time=None,
):
    analysis_request = analysis_request or getattr(batch, "analysis_request", None)
    queryset = User.objects.filter(is_active=True)
    if treatment_plant is not None:
        queryset = queryset.filter(
            Q(is_superuser=True)
            | Q(assigned_treatment_plant_id=_plant_id(treatment_plant))
        )

    # Manual assignment is plant scoped, not section scoped. When the request
    # exists its eligibility query applies all required permissions together;
    # avoid separately querying start/result permissions before that check.
    if analysis_request is not None:
        queryset = analysis_request.eligible_analysts(queryset)
    else:
        # Keep the standalone form case aligned with assign_analysts(). Each
        # permission query is independent so a grant may come from any group.
        for permission in (
            "ca.start_chemical_analysis",
            "ca.record_chemical_result",
            "ca.complete_receiving_checklist",
            "ca.complete_chemical_analysis",
        ):
            permitted = permission_user_queryset(
                permission,
                treatment_plant=treatment_plant,
            )
            queryset = queryset.filter(pk__in=permitted.values("pk"))

    return queryset.distinct().order_by(
        "first_name",
        "last_name",
        "username",
    )


def permission_user_queryset(
    permission,
    treatment_plant=None,
):
    app_label, codename = permission.split(
        ".",
        1,
    )

    queryset = (
        User.objects.filter(
            is_active=True,
        )
        .filter(
            Q(is_superuser=True)
            | Q(
                groups__permissions__content_type__app_label=app_label,
                groups__permissions__codename=codename,
            )
            | Q(
                user_permissions__content_type__app_label=app_label,
                user_permissions__codename=codename,
            )
        )
    )

    if treatment_plant is not None:
        queryset = queryset.filter(
            Q(is_superuser=True)
            | Q(
                assigned_treatment_plant_id=_plant_id(
                    treatment_plant
                )
            )
        )

    return queryset.distinct().order_by(
        "first_name",
        "last_name",
        "username",
    )


def qa_user_queryset(treatment_plant=None):
    return permission_user_queryset(
        "ca.review_chemical_analysis",
        treatment_plant,
    )


def approver_queryset(treatment_plant=None):
    return permission_user_queryset(
        "ca.approve_chemical_coa",
        treatment_plant,
    )


def _input_definition_name(
    definition: Any,
) -> str:
    if isinstance(definition, str):
        return definition.strip()

    if isinstance(definition, dict):
        return str(
            definition.get("name")
            or definition.get("key")
            or definition.get("field")
            or ""
        ).strip()

    return str(
        getattr(
            definition,
            "name",
            getattr(
                definition,
                "key",
                getattr(
                    definition,
                    "field",
                    "",
                ),
            ),
        )
        or ""
    ).strip()


def _input_definition_value(
    definition: Any,
    key: str,
    default=None,
):
    if isinstance(definition, dict):
        return definition.get(
            key,
            default,
        )

    return getattr(
        definition,
        key,
        default,
    )


def _normalise_input_definitions(
    definitions: Any,
) -> list[dict[str, Any]]:
    if not definitions:
        return []

    if isinstance(definitions, str):
        definitions = [
            item.strip()
            for item in definitions.split(",")
            if item.strip()
        ]

    if isinstance(definitions, dict):
        definitions = [
            {
                "name": key,
                **(
                    value
                    if isinstance(value, dict)
                    else {"label": value}
                ),
            }
            for key, value in definitions.items()
        ]

    if not isinstance(definitions, (list, tuple)):
        return []

    result = []

    for definition in definitions:
        name = _input_definition_name(
            definition
        )

        if not name:
            continue

        choices = _input_definition_value(
            definition,
            "choices",
            (),
        )

        if choices is None:
            choices = ()

        result.append(
            {
                "name": name,
                "label": (
                    _input_definition_value(
                        definition,
                        "label",
                        name.replace(
                            "_",
                            " ",
                        ).title(),
                    )
                    or name.replace(
                        "_",
                        " ",
                    ).title()
                ),
                "required": bool(
                    _input_definition_value(
                        definition,
                        "required",
                        True,
                    )
                ),
                "type": str(
                    _input_definition_value(
                        definition,
                        "type",
                        "number",
                    )
                    or "number"
                ).lower(),
                "min_value": _input_definition_value(
                    definition,
                    "min_value",
                ),
                "max_value": _input_definition_value(
                    definition,
                    "max_value",
                ),
                "unit": str(
                    _input_definition_value(definition, "unit", "") or ""
                ).strip(),
                "help_text": str(
                    _input_definition_value(definition, "help_text", "") or ""
                ).strip(),
                "choices": choices,
            }
        )

    return result


def _decimal(value):
    if value in (None, ""):
        return None

    try:
        return Decimal(str(value))
    except (
        InvalidOperation,
        TypeError,
        ValueError,
    ) as exc:
        raise ValidationError(
            "The value must be numeric."
        ) from exc


def _process_sampling(process):
    if process is None or not process.batch_id:
        return None

    return getattr(
        process.batch,
        "systematic_random_sampling",
        None,
    )


def _validate_sampling_ready(
    process,
    user,
):
    _require_active_user(
        user,
        "An active analyst is required.",
    )

    if process is None:
        raise ValidationError(
            "The chemical analysis process is required."
        )

    _require_permission(
        user,
        "ca.perform_systematic_random_sampling",
        "You do not have permission to perform systematic random sampling.",
    )

    process.refresh_assigned_analysts()

    if not process.is_authorized_analyst(user):
        raise ValidationError(
            "You are not assigned to this chemical analysis."
        )

    batch = process.batch

    if batch.package_count is None:
        raise ValidationError(
            "The batch package count has not been recorded."
        )

    if batch.package_count < 1:
        raise ValidationError(
            "The batch package count must be at least one."
        )

    if not process.analysis_request_id:
        raise ValidationError(
            "An analysis request is required before sampling."
        )


class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = (
            "name",
            "contract_number",
            "contract_document",
            "contact_info",
        )
        widgets = {
            "name": forms.TextInput(),
            "contract_number": forms.TextInput(),
            "contact_info": forms.Textarea(
                attrs={"rows": 3}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_field_classes(self)

    def clean_name(self):
        value = (
            self.cleaned_data.get("name")
            or ""
        ).strip()

        if not value:
            raise ValidationError(
                "Supplier name is required."
            )

        queryset = Supplier.objects.filter(
            name__iexact=value
        )

        if self.instance.pk:
            queryset = queryset.exclude(
                pk=self.instance.pk
            )

        if queryset.exists():
            raise ValidationError(
                "A supplier with this name already exists."
            )

        return value

    def clean_contract_number(self):
        value = (
            self.cleaned_data.get(
                "contract_number"
            )
            or ""
        ).strip()

        if not value:
            raise ValidationError(
                "Contract number is required."
            )

        queryset = Supplier.objects.filter(
            contract_number__iexact=value
        )

        if self.instance.pk:
            queryset = queryset.exclude(
                pk=self.instance.pk
            )

        if queryset.exists():
            raise ValidationError(
                "This contract number is already registered."
            )

        return value


class ChemicalForm(forms.ModelForm):
    class Meta:
        model = Chemical
        fields = ("name", "code", "description", "grade", "hydrometer_formula")
        widgets = {
            "name": forms.TextInput(attrs={"autocomplete": "off"}),
            "code": forms.TextInput(attrs={"autocomplete": "off", "style": "text-transform: uppercase;"}),
            "description": forms.Textarea(attrs={"rows": 3}),
            "grade": forms.TextInput(),
            "hydrometer_formula": forms.Textarea(attrs={"rows": 3, "placeholder": "Example: (reading * 1.05 + 2)"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_field_classes(self)

    def clean_name(self):
        value = (self.cleaned_data.get("name") or "").strip()
        if not value:
            raise ValidationError("Chemical name is required.")
        queryset = Chemical.objects.filter(name__iexact=value)
        if self.instance.pk:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise ValidationError("A chemical with this name already exists.")
        return value

    def clean_code(self):
        value = (self.cleaned_data.get("code") or "").strip().upper()
        if not value:
            raise ValidationError("Chemical code is required.")
        queryset = Chemical.objects.filter(code__iexact=value)
        if self.instance.pk:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise ValidationError("A chemical with this code already exists.")
        return value

    def clean_hydrometer_formula(self):
        value = (self.cleaned_data.get("hydrometer_formula") or "").strip()
        if value:
            candidate = Chemical(hydrometer_formula=value)
            candidate.clean()
        return value


class HydrometerCalculationForm(forms.Form):
    reading = forms.FloatField(required=True, min_value=0, widget=forms.NumberInput(attrs={"step": "any", "min": "0"}))
    calculated_value = forms.FloatField(required=False, disabled=True, widget=forms.NumberInput(attrs={"step": "any", "readonly": True}))

    def __init__(self, *args, chemical=None, **kwargs):
        self.chemical = chemical
        super().__init__(*args, **kwargs)
        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()
        if self.chemical is None:
            raise ValidationError("A chemical is required for hydrometer calculation.")
        reading = cleaned.get("reading")
        if reading is not None:
            result = self.chemical.calculate_hydrometer_value(reading)
            cleaned["calculated_value"] = result
            self.fields["calculated_value"].initial = result
        return cleaned

    @property
    def result(self):
        return self.cleaned_data.get("calculated_value") if self.is_bound and self.is_valid() else None

class ChemicalLocationForm(forms.ModelForm):
    class Meta:
        model = ChemicalLocation
        fields = (
            "name",
            "treatment_plant",
        )

    def __init__(
        self,
        *args,
        user=None,
        treatment_plant=None,
        **kwargs,
    ):
        self.user = user
        self.treatment_plant = (
            _effective_plant(
                user,
                treatment_plant,
            )
        )

        super().__init__(
            *args,
            **kwargs,
        )

        plants = TreatmentPlant.objects.order_by(
            "name"
        )

        if self.treatment_plant is not None:
            plants = plants.filter(
                pk=_plant_id(
                    self.treatment_plant
                )
            )

        self.fields[
            "treatment_plant"
        ].queryset = plants

        if self.treatment_plant is not None:
            self.fields[
                "treatment_plant"
            ].initial = self.treatment_plant

        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        plant = cleaned.get(
            "treatment_plant"
        )

        if plant:
            _validate_plant_access(
                self.user,
                plant,
            )

        name = (
            cleaned.get("name")
            or ""
        ).strip()

        if not name:
            self.add_error(
                "name",
                "Location name is required.",
            )

        if plant and name:
            queryset = ChemicalLocation.objects.filter(
                name__iexact=name,
                treatment_plant=plant,
            )

            if self.instance.pk:
                queryset = queryset.exclude(
                    pk=self.instance.pk
                )

            if queryset.exists():
                self.add_error(
                    "name",
                    "This location already exists for the selected treatment plant.",
                )

        return cleaned


class ChemicalSpecificationForm(forms.ModelForm):
    # Keep the instrument requirement explicit in the controlled specification.
    class Meta:
        model = ChemicalSpecification
        fields = (
            "chemical",
            "treatment_plant",
            "parameter_name",
            "version",
            "min_limit",
            "max_limit",
            "unit",
            "is_required",
            "requires_instrument",
            "method",
        )
        widgets = {
            "version": forms.NumberInput(
                attrs={"min": 1}
            ),
            "min_limit": forms.NumberInput(
                attrs={"step": "any"}
            ),
            "max_limit": forms.NumberInput(
                attrs={"step": "any"}
            ),
        }

    def __init__(
        self,
        *args,
        user=None,
        treatment_plant=None,
        **kwargs,
    ):
        self.user = user
        self.treatment_plant = _effective_plant(
            user,
            treatment_plant,
        )

        super().__init__(
            *args,
            **kwargs,
        )

        self.fields[
            "chemical"
        ].queryset = Chemical.objects.order_by(
            "name"
        )

        plants = TreatmentPlant.objects.order_by(
            "name"
        )

        if self.treatment_plant is not None:
            plants = plants.filter(
                pk=_plant_id(
                    self.treatment_plant
                )
            )

        self.fields[
            "treatment_plant"
        ].queryset = plants

        if self.treatment_plant is not None:
            self.fields[
                "treatment_plant"
            ].initial = self.treatment_plant

        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        plant = cleaned.get(
            "treatment_plant"
        )

        if plant:
            _validate_plant_access(
                self.user,
                plant,
            )

        minimum = cleaned.get(
            "min_limit"
        )
        maximum = cleaned.get(
            "max_limit"
        )

        if (
            minimum is not None
            and maximum is not None
            and minimum > maximum
        ):
            self.add_error(
                "max_limit",
                "Maximum limit cannot be lower than minimum limit.",
            )

        parameter = (
            cleaned.get(
                "parameter_name"
            )
            or ""
        ).strip()

        if not parameter:
            self.add_error(
                "parameter_name",
                "Parameter name is required.",
            )

        chemical = cleaned.get(
            "chemical"
        )
        version = cleaned.get(
            "version"
        )

        return cleaned


class ChemicalSpecificationRemarkForm(forms.ModelForm):
    class Meta:
        model = ChemicalSpecificationRemark
        fields = (
            "specification",
            "on_pass",
            "on_fail",
        )
        widgets = {
            "on_pass": forms.Textarea(attrs={"rows": 3}),
            "on_fail": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, user=None, treatment_plant=None, **kwargs):
        self.user = user
        self.treatment_plant = _effective_plant(user, treatment_plant)
        super().__init__(*args, **kwargs)
        queryset = ChemicalSpecification.objects.select_related(
            "chemical",
            "treatment_plant",
        ).order_by(
            "chemical__name",
            "parameter_name",
            "-version",
        )
        queryset = _scope_plant_queryset(
            queryset,
            user=user,
            treatment_plant=treatment_plant,
        )
        self.fields["specification"].queryset = queryset
        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()
        specification = cleaned.get("specification")
        if specification is not None:
            _validate_plant_access(
                self.user,
                specification.treatment_plant,
            )
        return cleaned


class PackagingStatusForm(forms.ModelForm):
    class Meta:
        model = PackagingStatus
        fields = (
            "name",
            "description",
        )
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_field_classes(self)

    def clean_name(self):
        value = (self.cleaned_data.get("name") or "").strip()
        if not value:
            raise ValidationError("Packaging status name is required.")
        queryset = PackagingStatus.objects.filter(name__iexact=value)
        if self.instance.pk:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise ValidationError("A packaging status with this name already exists.")
        return value


class ChemicalCertApproverModelForm(forms.ModelForm):
    class Meta:
        model = ChemicalCertApprover
        fields = (
            "user",
            "signature",
            "rubber_stamp",
        )

    def __init__(self, *args, user=None, treatment_plant=None, **kwargs):
        self.user = user
        self.treatment_plant = _effective_plant(user, treatment_plant)
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = approver_queryset(self.treatment_plant)
        apply_field_classes(self)

    def clean_user(self):
        value = self.cleaned_data.get("user")
        if value is None:
            raise ValidationError("A certificate approver is required.")
        _require_active_user(value)
        if not _has_perm(value, "ca.approve_chemical_coa"):
            raise ValidationError(
                "The selected user does not have the required CoA approval permission."
            )
        if self.treatment_plant is not None and not _user_is_superuser(value):
            if _user_plant_id(value) != _plant_id(self.treatment_plant):
                raise ValidationError(
                    "The selected approver is assigned to another treatment plant."
                )
        return value






class ChemicalAnalysisProcessForm(forms.ModelForm):
    class Meta:
        model = ChemicalAnalysisProcess
        fields = (
            "batch",
            "analysis_request",
            "initiated_by",
            "assigned_analyst",
            "qa_officer",
            "approver",
        )

    def __init__(self, *args, user=None, treatment_plant=None, **kwargs):
        self.user = user
        self.treatment_plant = _effective_plant(user, treatment_plant)
        super().__init__(*args, **kwargs)
        batches = ChemicalBatch.objects.select_related(
            "chemical",
            "treatment_plant",
        ).order_by("-created_at")
        batches = _scope_plant_queryset(
            batches,
            user=user,
            treatment_plant=treatment_plant,
        )
        self.fields["batch"].queryset = batches
        self.fields["analysis_request"].queryset = ChemicalAnalysisRequest.objects.filter(
            batch__in=batches
        ).select_related("batch", "batch__chemical")
        users = scoped_user_queryset(
            treatment_plant=treatment_plant,
            user=user,
        )
        self.fields["initiated_by"].queryset = users
        self.fields["assigned_analyst"].queryset = users
        self.fields["qa_officer"].queryset = qa_user_queryset(self.treatment_plant)
        self.fields["approver"].queryset = permission_user_queryset(
            "ca.approve_chemical_batch",
            self.treatment_plant,
        )
        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()
        batch = cleaned.get("batch")
        request = cleaned.get("analysis_request")
        if batch is not None:
            _validate_plant_access(self.user, batch.treatment_plant)
        if request is not None and batch is not None and request.batch_id != batch.pk:
            self.add_error(
                "analysis_request",
                "The analysis request must belong to the selected batch.",
            )
        return cleaned

class ChemicalCoAForm(forms.ModelForm):
    class Meta:
        model = ChemicalCoA
        fields = (
            "batch",
            "title",
            "coa_header",
            "coa_footer",
            "footer_image",
            "packaging_status",
            "remarks",
        )
        widgets = {
            "coa_header": forms.Textarea(
                attrs={
                    "rows": 3,
                    "class": "form-control",
                }
            ),
            "coa_footer": forms.Textarea(
                attrs={
                    "rows": 3,
                    "class": "form-control",
                }
            ),
            "remarks": forms.Textarea(
                attrs={
                    "rows": 4,
                    "class": "form-control",
                }
            ),
        }

    def __init__(
        self,
        *args,
        user=None,
        treatment_plant=None,
        **kwargs,
    ):
        self.user = user
        self.treatment_plant = _effective_plant(
            user,
            treatment_plant,
        )

        super().__init__(*args, **kwargs)

        batches = (
            ChemicalBatch.objects
            .select_related(
                "chemical",
                "supplier",
                "treatment_plant",
            )
            .order_by("-created_at")
        )

        batches = _scope_plant_queryset(
            batches,
            user=user,
            treatment_plant=treatment_plant,
        )

        self.fields["batch"].queryset = batches
        self.fields["batch"].empty_label = "Select chemical batch"

        self.fields["packaging_status"].queryset = (
            PackagingStatus.objects
            .order_by("name")
        )
        self.fields["packaging_status"].empty_label = "Select packaging status"

        apply_field_classes(self)

    def clean_batch(self):
        batch = self.cleaned_data.get("batch")

        if batch is None:
            return batch

        _validate_plant_access(
            self.user,
            batch.treatment_plant,
        )

        return batch

    def clean(self):
        cleaned = super().clean()

        batch = cleaned.get("batch")

        if batch is not None:
            _validate_plant_access(
                self.user,
                batch.treatment_plant,
            )

        return cleaned

class ChemicalRemarkForm(forms.Form):
    remark = forms.CharField(
        min_length=2,
        widget=forms.Textarea(
            attrs={"rows": 4}
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_field_classes(self)

    def clean_remark(self):
        value = (
            self.cleaned_data.get(
                "remark"
            )
            or ""
        ).strip()

        if not value:
            raise ValidationError(
                "A remark is required."
            )

        return value


class PackagingForm(forms.Form):
    confirm = forms.BooleanField(
        required=True,
        label="Confirm packaging completion",
    )
    remarks = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={"rows": 3}
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        apply_field_classes(self)


class PlantScopedLocationSelect(forms.Select):
    """Expose each location's plant so batch intake can filter options live."""

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(
            name, value, label, selected, index, subindex=subindex, attrs=attrs
        )
        location = getattr(value, "instance", None)
        if location is not None:
            option["attrs"]["data-plant-id"] = str(
                location.treatment_plant_id or ""
            )
        return option


class ChemicalBatchForm(forms.ModelForm):
    intake_discrepancies = forms.MultipleChoiceField(
        required=False,
        choices=ChemicalBatchHold.Issue.choices,
        widget=forms.CheckboxSelectMultiple,
        label="Intake discrepancies",
    )
    hold_reason = forms.CharField(
        required=False,
        max_length=3000,
        widget=forms.Textarea(attrs={"rows": 2}),
        label="Hold details",
    )
    location = forms.ModelChoiceField(
        queryset=ChemicalLocation.objects.none(),
        required=True,
        label="Chemical Storage Location",
        widget=PlantScopedLocationSelect(),
    )
    delivery_date = forms.DateField(
        required=True,
        label="Date Received",
        initial=timezone.localdate,
        widget=forms.DateInput(
            attrs={
                "type": "date",
                "class": "ca-compact-input ca-date-picker",
            }
        ),
        help_text="Required. Defaults to today's date.",
    )

    class Meta:
        model = ChemicalBatch
        fields = (
            "delivery_note_number",
            "batch_number",
            "chemical",
            "supplier",
            "treatment_plant",
            "location",
            "delivery_date",
            "quantity_received",
            "unit",
            "package_count",
            "remarks",
        )
        widgets = {
            "quantity_received": forms.NumberInput(
                attrs={
                    "min": "0.0001",
                    "step": "any",
                    "placeholder": "e.g. 5000",
                }
            ),
            "package_count": forms.NumberInput(
                attrs={
                    "min": "1",
                    "step": "1",
                    "placeholder": "e.g. 100 bags / 20 drums",
                }
            ),
            "remarks": forms.Textarea(
                attrs={
                    "rows": 2,
                    "placeholder": "Delivery notes, vehicle reg, seal numbers, physical condition...",
                }
            ),
            "delivery_note_number": forms.TextInput(
                attrs={"placeholder": "e.g. DN-2026-9842"}
            ),
            "batch_number": forms.TextInput(
                attrs={"placeholder": "e.g. BATCH-ALUM-2026-04"}
            ),
        }

    def __init__(
        self,
        *args,
        user=None,
        treatment_plant=None,
        is_superuser=None,
        **kwargs,
    ):
        self.user = user
        self.is_superuser = (
            _user_is_superuser(user)
            if is_superuser is None
            else bool(is_superuser)
        )
        assigned = getattr(user, "assigned_treatment_plant", None) if user else None
        self.treatment_plant = treatment_plant or assigned or _effective_plant(
            user,
            treatment_plant,
        )

        super().__init__(
            *args,
            **kwargs,
        )

        self.fields[
            "chemical"
        ].queryset = Chemical.objects.order_by(
            "name"
        )

        self.fields[
            "supplier"
        ].queryset = Supplier.objects.order_by(
            "name"
        )

        plants = TreatmentPlant.objects.order_by(
            "name"
        )

        if self.treatment_plant is not None:
            if user and not self.is_superuser:
                plants = plants.filter(
                    pk=_plant_id(
                        self.treatment_plant
                    )
                )
                self.fields["treatment_plant"].empty_label = None
            self.fields[
                "treatment_plant"
            ].initial = self.treatment_plant
        elif assigned is not None:
            self.fields["treatment_plant"].initial = assigned

        self.fields[
            "treatment_plant"
        ].queryset = plants

        locations = ChemicalLocation.objects.select_related(
            "treatment_plant"
        ).order_by("treatment_plant__name", "name")
        if user and not self.is_superuser:
            assigned_id = _user_plant_id(user)
            if assigned_id:
                locations = locations.filter(treatment_plant_id=assigned_id)
            else:
                locations = locations.none()
        self.fields["location"].queryset = locations

        if not self.initial.get("unit"):
            self.fields["unit"].initial = "kg"

        apply_field_classes(self)

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.user and not instance.received_by_id:
            instance.received_by = self.user

        if self.user and not self.is_superuser:
            assigned_id = getattr(self.user, "assigned_treatment_plant_id", None)
            if assigned_id:
                instance.treatment_plant_id = assigned_id
        elif not instance.treatment_plant_id and self.treatment_plant:
            instance.treatment_plant_id = _plant_id(self.treatment_plant)

        instance.received_date = self.cleaned_data["delivery_date"]

        if commit:
            instance.save()
            # ``save_m2m`` is only installed by ModelForm.save(commit=False).
            # On the normal commit=True path, use Django's m2m persistence
            # hook directly (the current batch form has no m2m fields, but
            # retaining this keeps the override correct if one is added).
            self._save_m2m()
        return instance

    def clean_delivery_note_number(self):
        value = (
            self.cleaned_data.get(
                "delivery_note_number"
            )
            or ""
        ).strip()

        if not value:
            raise ValidationError(
                "Delivery note number is required."
            )

        queryset = ChemicalBatch.objects.filter(
            delivery_note_number__iexact=value
        )

        if self.instance.pk:
            queryset = queryset.exclude(
                pk=self.instance.pk
            )

        if queryset.exists():
            raise ValidationError(
                "This delivery note number is already registered."
            )

        return value

    def clean_batch_number(self):
        value = (
            self.cleaned_data.get(
                "batch_number"
            )
            or ""
        ).strip()

        if not value:
            raise ValidationError(
                "Supplier/manufacturer batch number is required."
            )

        queryset = ChemicalBatch.objects.filter(
            batch_number__iexact=value
        )

        if self.instance.pk:
            queryset = queryset.exclude(
                pk=self.instance.pk
            )

        if queryset.exists():
            raise ValidationError(
                "This batch number is already registered."
            )

        return value

    def clean_quantity_received(self):
        value = self.cleaned_data.get(
            "quantity_received"
        )

        if value is None or value <= 0:
            raise ValidationError(
                "Quantity received must be greater than zero."
            )

        return value

    def clean_package_count(self):
        value = self.cleaned_data.get(
            "package_count"
        )

        if value is None or value < 1:
            raise ValidationError(
                "Package count must be at least one."
            )

        return value

    def clean(self):
        cleaned = super().clean()

        plant = cleaned.get(
            "treatment_plant"
        )
        location = cleaned.get("location")

        if plant:
            _validate_plant_access(
                self.user,
                plant,
            )

        if location and plant and location.treatment_plant_id != plant.pk:
            self.add_error(
                "location",
                "The storage location must belong to the selected treatment plant.",
            )

        chemical = cleaned.get(
            "chemical"
        )

        if cleaned.get("intake_discrepancies") and not (cleaned.get("hold_reason") or "").strip():
            self.add_error("hold_reason", "Describe the discrepancy before placing the batch on hold.")

        if (
            chemical
            and plant
            and not ChemicalSpecification.current_for(ChemicalSpecification.objects.filter(
                chemical=chemical,
                treatment_plant=plant,
                is_required=True,
            )).exists()
        ):
            self._form_warnings = [
                "No required analytical specifications are configured "
                "for this chemical at the selected treatment plant."
            ]

        return cleaned


class ReceiveChemicalBatchForm(forms.Form):
    confirm = forms.BooleanField(
        required=True,
        label="Confirm receipt of chemical batch",
    )
    remarks = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={"rows": 3}
        ),
    )

    def __init__(
        self,
        *args,
        batch=None,
        process=None,
        user=None,
        **kwargs,
    ):
        self.batch = batch
        self.user = user
        super().__init__(*args, **kwargs)
        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        _require_active_user(
            self.user,
            "An authenticated user is required to receive the batch.",
        )

        _require_permission(
            self.user,
            "ca.receive_chemical_batch",
            "You do not have permission to receive chemical batches.",
        )

        if self.batch is None:
            raise ValidationError(
                "A chemical batch is required."
            )

        _validate_plant_access(
            self.user,
            self.batch.treatment_plant,
        )

        if (
            self.batch.status
            != ChemicalBatch.Status.RECEIVED
        ):
            raise ValidationError(
                "This batch is not in a receivable state."
            )

        return cleaned


class ChemicalAnalysisRequestForm(forms.ModelForm):
    class Meta:
        model = ChemicalAnalysisRequest
        fields = (
            "batch",
            "delivery_date",
        )
        widgets = {
            "delivery_date": forms.DateInput(
                attrs={"type": "date"}
            )
        }

    def __init__(
        self,
        *args,
        user=None,
        treatment_plant=None,
        batch=None,
        received_by=None,
        **kwargs,
    ):
        self.user = user
        self.received_by = received_by
        self.treatment_plant = _effective_plant(
            user,
            treatment_plant,
        )
        self.selected_batch = batch

        super().__init__(
            *args,
            **kwargs,
        )

        batches = (
            ChemicalBatch.objects
            .select_related(
                "chemical",
                "supplier",
                "treatment_plant",
            )
            .filter(
                status=ChemicalBatch.Status.RECEIVED
            )
            .order_by(
                "-received_date",
                "-created_at",
            )
        )

        if self.treatment_plant is not None:
            batches = batches.filter(
                treatment_plant=self.treatment_plant
            )

        if batch is not None:
            batches = batches.filter(
                pk=_pk(batch)
            )

        self.fields[
            "batch"
        ].queryset = batches

        if not self.instance.pk:
            self.fields[
                "delivery_date"
            ].initial = timezone.localdate()

        apply_field_classes(self)

    def clean_delivery_date(self):
        value = self.cleaned_data.get(
            "delivery_date"
        )

        if (
            value
            and value > timezone.localdate()
        ):
            raise ValidationError(
                "Delivery date cannot be in the future."
            )

        return value

    def clean(self):
        cleaned = super().clean()

        batch = cleaned.get(
            "batch"
        )

        if batch is None:
            return cleaned

        _validate_plant_access(
            self.user,
            batch.treatment_plant,
        )

        if (
            batch.status
            != ChemicalBatch.Status.RECEIVED
        ):
            self.add_error(
                "batch",
                "Only received chemical batches can be submitted for analysis.",
            )

        if (
            batch.package_count is None
            or batch.package_count < 1
        ):
            self.add_error(
                "batch",
                "The batch must have a valid package count before analysis is requested.",
            )

        existing = ChemicalAnalysisRequest.objects.filter(
            batch=batch
        )

        if self.instance.pk:
            existing = existing.exclude(
                pk=self.instance.pk
            )

        if existing.exists():
            self.add_error(
                "batch",
                "An analysis request already exists for this batch.",
            )

        return cleaned

    def save(self, commit=True):
        instance = super().save(
            commit=False
        )

        if self.user is not None:
            if not instance.requested_by_id:
                instance.requested_by = self.user

        if self.received_by is not None:
            instance.received_by = self.received_by

        if commit:
            instance.save()

        return instance


class ChecklistForm(forms.ModelForm):
    class Meta:
        model = ChecklistForWaterTreatmentChemicalAnalysis
        fields = (
            "chemical",
            "manufacturer_name_indicated",
            "chemical_name_on_label",
            "min_mass_indicated",
            "grade",
            "manufacture_date",
            "expiry_date",
            "chemical_label_photo",
            "storage_conditions",
            "net_weight",
            "country_of_origin",
            "handling_instructions",
            "is_oxidizing",
            "is_corrosive",
            "may_cause_fire",
            "contact_with_acids_liberates_toxic_gas",
            "causes_burns",
            "keep_out_of_reach_of_children",
            "contact_with_eyes_action",
            "fire_action_requires_respiratory_equipment",
        )
        widgets = {
            "manufacture_date": forms.DateInput(
                attrs={"type": "date"}
            ),
            "expiry_date": forms.DateInput(
                attrs={"type": "date"}
            ),
            "net_weight": forms.NumberInput(
                attrs={
                    "min": "0",
                    "step": "any",
                }
            ),
            "handling_instructions": forms.Textarea(
                attrs={"rows": 3}
            ),
        }

    def __init__(
        self,
        *args,
        request=None,
        user=None,
        process=None,
        batch=None,
        **kwargs,
    ):
        self.analysis_request = request
        self.user = user
        self.process = process
        self.batch = batch or getattr(request, "batch", None)

        super().__init__(
            *args,
            **kwargs,
        )

        self.fields["chemical_label_photo"].required = True
        self.fields["chemical_label_photo"].help_text = (
            "Upload a clear photo of the container label with the expiry date visible."
        )

        # The analysis request is workflow context, not a user-editable field.
        # Set it on the model instance before ModelForm validation so the
        # model's clean() method can validate the checklist against its batch.
        # Otherwise it raises a field error for this excluded FK and Django
        # raises ValueError because the form has no such field.
        if request is not None:
            self.instance.chemical_analysis_request = request

        queryset = Chemical.objects.order_by(
            "name"
        )

        if request is not None and request.batch_id:
            queryset = queryset.filter(
                pk=request.batch.chemical_id
            )
            self.initial[
                "chemical"
            ] = request.batch.chemical_id

        self.fields[
            "chemical"
        ].queryset = queryset

        if not self.instance.pk:
            self.fields[
                "country_of_origin"
            ].initial = "KE"

        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        manufacture = cleaned.get(
            "manufacture_date"
        )
        expiry = cleaned.get(
            "expiry_date"
        )
        weight = cleaned.get(
            "net_weight"
        )

        if (
            manufacture
            and manufacture > timezone.localdate()
        ):
            self.add_error(
                "manufacture_date",
                "Manufacture date cannot be in the future.",
            )

        if (
            manufacture
            and expiry
            and expiry <= manufacture
        ):
            self.add_error(
                "expiry_date",
                "Expiry date must be later than manufacture date.",
            )

        if weight is None or weight <= 0:
            self.add_error(
                "net_weight",
                "Net weight must be greater than zero.",
            )

        if self.analysis_request is not None:
            chemical = cleaned.get(
                "chemical"
            )

            if (
                chemical
                and chemical.pk
                != self.analysis_request.batch.chemical_id
            ):
                self.add_error(
                    "chemical",
                    "Checklist chemical must match the batch chemical.",
                )

        return cleaned

    def save(self, commit=True):
        instance = super().save(
            commit=False
        )

        if self.analysis_request is not None:
            instance.chemical_analysis_request = (
                self.analysis_request
            )

        if self.user is not None:
            if not instance.sampled_by_id:
                instance.sampled_by = self.user

            if not instance.requested_by_id:
                instance.requested_by = (
                    self.analysis_request.requested_by
                    if (
                        self.analysis_request
                        and self.analysis_request.requested_by_id
                    )
                    else self.user
                )

        if commit:
            instance.save()

        return instance


class CompleteChecklistForm(forms.Form):
    confirm = forms.BooleanField(
        required=True,
        label="Confirm that the receiving checklist is complete",
    )

    def __init__(
        self,
        *args,
        process=None,
        batch=None,
        user=None,
        **kwargs,
    ):
        self.process = process
        self.batch = batch
        self.user = user
        super().__init__(*args, **kwargs)
        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        _require_active_user(
            self.user,
            "An authenticated user is required to complete the checklist.",
        )

        if self.process is None:
            raise ValidationError(
                "The chemical analysis process is required."
            )

        _validate_plant_access(
            self.user,
            self.process.batch.treatment_plant,
        )

        if self.process.status not in {
            ChemicalAnalysisProcess.WorkflowStatus.CHECKLIST,
            ChemicalAnalysisProcess.WorkflowStatus.RECEIVED,
            ChemicalAnalysisProcess.WorkflowStatus.REQUESTED,
        }:
            raise ValidationError(
                "The receiving checklist cannot be completed in the current workflow state."
            )

        return cleaned
















class BatchTestResultForm(forms.ModelForm):
    class Meta:
        model = BatchTestResult
        fields = (
            "parameter",
            "value",
            "textual_result",
            "status",
            "inventory_equipment",
            "reference_standard_name",
            "reference_standard_lot",
            "reagent_lot_numbers",
            "expanded_uncertainty",
            "uncertainty_coverage_factor",
            "revision_reason",
        )
        widgets = {
            "value": forms.NumberInput(
                attrs={"step": "any"}
            ),
            "textual_result": forms.TextInput(),
        }

    def __init__(
        self,
        *args,
        process=None,
        batch=None,
        analyst=None,
        parameter=None,
        **kwargs,
    ):
        self.process = process
        self.batch = batch or getattr(
            process,
            "batch",
            None,
        )
        self.analyst = analyst
        self.selected_parameter = parameter

        super().__init__(
            *args,
            **kwargs,
        )

        if self.batch is not None and self.process is not None:
            queryset = self.process.required_specifications()
        elif self.batch is not None:
            analysis_request = getattr(self.batch, "analysis_request", None)
            if analysis_request is not None:
                queryset = analysis_request.get_required_specifications()
            else:
                queryset = ChemicalSpecification.current_for(
                    ChemicalSpecification.objects.filter(
                        chemical_id=self.batch.chemical_id,
                        treatment_plant_id=self.batch.treatment_plant_id,
                        is_required=True,
                    ).select_related("chemical", "method")
                )
        else:
            queryset = ChemicalSpecification.objects.none()

        from .inventory_instruments import inventory_equipment_queryset

        self.fields["inventory_equipment"] = InventoryEquipmentChoiceField(
            queryset=inventory_equipment_queryset(
                self.batch.treatment_plant_id if self.batch is not None else None
            ),
            required=bool(parameter is not None and parameter.requires_instrument),
            label="Inventory laboratory equipment",
            help_text=(
                "Equipment is plant scoped. Calibration state and serial number are checked and saved with the result."
            ),
        )

        self.fields[
            "parameter"
        ].queryset = queryset

        if parameter is not None:
            self.fields[
                "parameter"
            ].initial = parameter

        apply_field_classes(self)

    def clean_parameter(self):
        parameter = self.cleaned_data.get(
            "parameter"
        )

        if parameter is None:
            raise ValidationError(
                "An analytical parameter is required."
            )

        if self.batch is not None:
            if parameter.chemical_id != self.batch.chemical_id:
                raise ValidationError(
                    "The parameter does not belong to this chemical."
                )

            if (
                self.batch.treatment_plant_id
                and parameter.treatment_plant_id
                != self.batch.treatment_plant_id
            ):
                raise ValidationError(
                    "The parameter is not configured for this treatment plant."
                )

        return parameter

    def clean(self):
        cleaned = super().clean()

        parameter = cleaned.get(
            "parameter"
        )
        value = cleaned.get(
            "value"
        )
        textual = (
            cleaned.get(
                "textual_result"
            )
            or ""
        ).strip()
        status = cleaned.get(
            "status"
        )

        _require_active_user(
            self.analyst,
            "An active analyst is required to record results.",
        )

        _require_permission(
            self.analyst,
            "ca.record_chemical_result",
            "You do not have permission to record chemical analysis results.",
        )

        if self.process is not None:
            if (
                self.process.status
                != ChemicalAnalysisProcess.WorkflowStatus.ANALYSIS
            ):
                raise ValidationError(
                    "Results can only be recorded while analysis is active."
                )

            self.process.refresh_assigned_analysts()

            if not self.process.is_authorized_analyst(
                self.analyst
            ):
                raise ValidationError(
                    "The analyst is not assigned to this chemical analysis."
                )

        if (
            status in {
                BatchTestResult.Status.PASS,
                BatchTestResult.Status.FAIL,
            }
            and value is None
            and not textual
        ):
            raise ValidationError(
                "A numerical or textual analytical result is required."
            )

        if (
            status == BatchTestResult.Status.NA
            and value is not None
        ):
            raise ValidationError(
                "A Not Applicable result cannot contain a numerical value."
            )

        if parameter and value is not None:
            minimum = parameter.min_limit
            maximum = parameter.max_limit

            if (
                minimum is not None
                and value < minimum
                and status == BatchTestResult.Status.PASS
            ):
                cleaned["status"] = BatchTestResult.Status.FAIL

        if parameter and parameter.requires_instrument and not cleaned.get("inventory_equipment"):
            self.add_error("inventory_equipment", "Select the laboratory equipment used for this parameter.")

            if (
                maximum is not None
                and value > maximum
                and status == BatchTestResult.Status.PASS
            ):
                cleaned["status"] = BatchTestResult.Status.FAIL

            changed = self.instance.pk and any(
                self.has_changed() and field in self.changed_data
                for field in ("value", "textual_result", "status", "instrument")
            )
            if changed and not (cleaned.get("revision_reason") or "").strip():
                self.add_error("revision_reason", "Explain why this result is being amended or retested.")

            duplicates = BatchTestResult.objects.filter(
                batch=self.batch,
                parameter=parameter,
            )

            if self.instance.pk:
                duplicates = duplicates.exclude(
                    pk=self.instance.pk
                )

            if duplicates.exists():
                self.add_error(
                    "parameter",
                    "A result already exists for this parameter.",
                )

        return cleaned

    def save(self, commit=True):
        instance = super().save(
            commit=False
        )

        if self.batch is not None:
            instance.batch = self.batch

        if self.analyst is not None:
            instance.analyst = self.analyst

        instance.tested_at = timezone.now()

        if commit:
            instance.save()

        return instance


class ChemicalAnalysisInputForm(forms.ModelForm):
    class Meta:
        model = ChemicalAnalysisInput
        fields = (
            "test_result",
            "inputs",
        )
        widgets = {
            "inputs": forms.Textarea(
                attrs={"rows": 7}
            )
        }

    def __init__(
        self,
        *args,
        test_result=None,
        process=None,
        **kwargs,
    ):
        self.selected_test_result = test_result
        self.process = process

        super().__init__(
            *args,
            **kwargs,
        )

        queryset = BatchTestResult.objects.all()

        if process is not None:
            queryset = queryset.filter(
                batch=process.batch
            )
            queryset = queryset.filter(
                parameter__in=process.required_specifications()
            )

        if test_result is not None:
            queryset = queryset.filter(
                pk=test_result.pk
            )
            self.initial[
                "test_result"
            ] = test_result.pk

        self.fields[
            "test_result"
        ].queryset = queryset

        apply_field_classes(self)

    def clean_inputs(self):
        value = self.cleaned_data.get(
            "inputs"
        )

        if isinstance(value, dict):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = json.loads(
                    value
                )
            except (
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                raise ValidationError(
                    "Enter valid JSON containing the analytical inputs."
                ) from exc
        else:
            raise ValidationError(
                "Analytical inputs must be supplied as a JSON object."
            )

        if not isinstance(parsed, dict):
            raise ValidationError(
                "Analytical inputs must be a JSON object."
            )

        if not parsed:
            raise ValidationError(
                "At least one analytical input is required."
            )

        return parsed

    def clean(self):
        cleaned = super().clean()

        result = cleaned.get(
            "test_result"
        )
        inputs = cleaned.get(
            "inputs"
        ) or {}

        if result is None:
            return cleaned

        if self.process is not None:
            if result.batch_id != self.process.batch_id:
                raise ValidationError(
                    "The analytical result does not belong to this process."
                )

        method = getattr(
            result.parameter,
            "method",
            None,
        )

        if method is None:
            return cleaned

        definitions = _normalise_input_definitions(
            method.get_input_fields()
            if hasattr(
                method,
                "get_input_fields",
            )
            else []
        )

        names = {
            item["name"]
            for item in definitions
        }

        missing = [
            item["name"]
            for item in definitions
            if item["required"]
            and item["name"] not in inputs
        ]

        if missing:
            raise ValidationError(
                "Missing required analytical inputs: "
                + ", ".join(missing)
            )

        unexpected = (
            set(inputs) - names
        )

        if unexpected and names:
            raise ValidationError(
                "Unknown analytical inputs: "
                + ", ".join(
                    sorted(
                        unexpected
                    )
                )
            )

        for definition in definitions:
            name = definition["name"]

            if name not in inputs:
                continue

            value = inputs[name]
            field_type = definition["type"]

            if field_type in {
                "number",
                "decimal",
                "float",
                "integer",
                "int",
            }:
                numeric = _decimal(
                    value
                )

                if numeric is None:
                    raise ValidationError(
                        f"{definition['label']} must be numeric."
                    )

                minimum = definition[
                    "min_value"
                ]
                maximum = definition[
                    "max_value"
                ]

                if (
                    minimum is not None
                    and numeric
                    < Decimal(
                        str(minimum)
                    )
                ):
                    raise ValidationError(
                        f"{definition['label']} is below the allowed minimum."
                    )

                if (
                    maximum is not None
                    and numeric
                    > Decimal(
                        str(maximum)
                    )
                ):
                    raise ValidationError(
                        f"{definition['label']} exceeds the allowed maximum."
                    )

        return cleaned

    def save(self, commit=True):
        instance = super().save(
            commit=False
        )

        if self.selected_test_result is not None:
            instance.test_result = (
                self.selected_test_result
            )

        if commit:
            instance.save()

        return instance


class DynamicChemicalAnalysisInputForm(forms.Form):
    def __init__(
        self,
        *args,
        test_result=None,
        **kwargs,
    ):
        self.test_result = test_result

        super().__init__(
            *args,
            **kwargs,
        )

        method = getattr(
            getattr(
                test_result,
                "parameter",
                None,
            ),
            "method",
            None,
        )

        if method is not None:
            definitions = _normalise_input_definitions(
                method.inputs_structure
            )

            for definition in definitions:
                name = definition["name"]
                unit = definition["unit"]
                label = definition["label"] + (f" ({unit})" if unit else "")
                required = definition["required"]
                field_type = definition["type"]
                help_text = definition["help_text"]

                minimum = definition[
                    "min_value"
                ]
                maximum = definition[
                    "max_value"
                ]

                if field_type in {
                    "text",
                    "string",
                }:
                    field = forms.CharField(
                        required=required,
                        label=label,
                        help_text=help_text,
                    )
                elif field_type in {
                    "integer",
                    "int",
                }:
                    field = forms.IntegerField(
                        required=required,
                        label=label,
                        help_text=help_text,
                    )
                elif field_type in {
                    "boolean",
                    "bool",
                }:
                    field = forms.BooleanField(
                        required=required,
                        label=label,
                        help_text=help_text,
                    )
                elif field_type in {
                    "choice",
                    "select",
                }:
                    field = forms.ChoiceField(
                        required=required,
                        label=label,
                        choices=definition[
                            "choices"
                        ],
                        help_text=help_text,
                    )
                else:
                    field = forms.DecimalField(
                        required=required,
                        label=label,
                        max_digits=16,
                        decimal_places=6,
                        min_value=(
                            Decimal(
                                str(minimum)
                            )
                            if minimum is not None
                            else None
                        ),
                        max_value=(
                            Decimal(
                                str(maximum)
                            )
                            if maximum is not None
                            else None
                        ),
                        help_text=help_text,
                    )

                self.fields[name] = field

        apply_field_classes(self)

    def clean(self):
        return super().clean()

    def as_input_dictionary(self):
        return {
            key: value
            for key, value in self.cleaned_data.items()
            if value not in (
                None,
                "",
            )
        }


class FormulaDrivenChemicalAnalysisForm(forms.Form):
    """Collect method measurements and leave result/status calculation to the system."""

    def __init__(
        self,
        *args,
        parameter=None,
        batch=None,
        process=None,
        analyst=None,
        **kwargs,
    ):
        self.parameter = parameter
        self.batch = batch
        self.process = process
        self.analyst = analyst
        super().__init__(*args, **kwargs)

        method = getattr(parameter, "method", None)
        self.setup_errors = []
        if method is None:
            self.setup_errors.append(
                "No analytical method is assigned to this specification. Configure its method before recording results."
            )
        definitions = _normalise_input_definitions(
            getattr(method, "inputs_structure", None)
        )
        self.input_names = []

        for definition in definitions:
            name = definition["name"]
            if not name.isidentifier() or name in {"math", "__builtins__"}:
                self.setup_errors.append(
                    f"The analysis method has an invalid input name: {name}."
                )
                continue
            unit = definition["unit"]
            label = definition["label"] + (f" ({unit})" if unit else "")
            help_text = definition["help_text"]
            minimum = definition["min_value"]
            maximum = definition["max_value"]
            self.fields[name] = forms.DecimalField(
                required=True,
                label=label,
                help_text=help_text,
                max_digits=20,
                decimal_places=8,
                min_value=Decimal(str(minimum)) if minimum is not None else None,
                max_value=Decimal(str(maximum)) if maximum is not None else None,
                widget=forms.NumberInput(attrs={"step": "any"}),
            )
            self.input_names.append(name)

        self.fields["reference_standard_name"] = forms.CharField(required=False, max_length=180, label="Reference standard")
        self.fields["reference_standard_lot"] = forms.CharField(required=False, max_length=120, label="Reference standard lot")
        self.fields["reagent_lot_numbers"] = forms.CharField(required=False, max_length=500, label="Reagent lot numbers")
        self.fields["expanded_uncertainty"] = forms.DecimalField(required=False, min_value=0, max_digits=24, decimal_places=12, label="Expanded measurement uncertainty")
        self.fields["uncertainty_coverage_factor"] = forms.DecimalField(required=False, min_value=Decimal("0.0001"), max_digits=8, decimal_places=4, initial=Decimal("2"), label="Uncertainty coverage factor (k)")
        quality_plan = (
            ChemicalMethodQualityPlan.current_for(method, batch.treatment_plant if batch else None)
            if method is not None else None
        )
        if quality_plan:
            self.fields["reference_standard_lot"].required = quality_plan.require_reference_standard_lot
            self.fields["reagent_lot_numbers"].required = quality_plan.require_reagent_lot_numbers
            self.fields["expanded_uncertainty"].required = quality_plan.require_measurement_uncertainty

        if parameter is not None and parameter.requires_instrument:
            try:
                from .inventory_instruments import inventory_equipment_queryset

                equipment = inventory_equipment_queryset(
                    batch.treatment_plant_id if batch is not None else None
                )
                self.fields["inventory_equipment"] = InventoryEquipmentChoiceField(
                    queryset=equipment,
                    empty_label="Select Inventory equipment",
                    required=True,
                    label="Inventory laboratory equipment",
                    help_text="Select the equipment used. Calibration and availability are checked against Inventory and recorded with the result.",
                )
            except Exception:
                self.setup_errors.append(
                    "The Inventory equipment register is unavailable, so required laboratory equipment cannot be selected."
                )

        if not self.input_names:
            self.setup_errors.append(
                "This formula based method has no input measurements configured. "
                "Configure its input fields before recording results."
            )
        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()
        if self.setup_errors:
            raise ValidationError(self.setup_errors)
        return cleaned

    def as_input_dictionary(self):
        return {
            name: float(self.cleaned_data[name])
            for name in self.input_names
        }


class StartAnalysisForm(forms.Form):
    confirm = forms.BooleanField(
        required=True,
        label="Confirm that analysis is ready to start",
    )

    def __init__(
        self,
        *args,
        process=None,
        user=None,
        batch=None,
        **kwargs,
    ):
        self.process = process
        self.user = user
        self.batch = batch or getattr(process, "batch", None)

        super().__init__(
            *args,
            **kwargs,
        )

        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        _require_active_user(
            self.user,
            "An authenticated analyst is required to start analysis.",
        )

        _require_permission(
            self.user,
            "ca.start_chemical_analysis",
            "You do not have permission to start chemical analysis.",
        )

        if self.process is None:
            raise ValidationError(
                "The chemical analysis process is required."
            )

        _validate_plant_access(
            self.user,
            self.process.batch.treatment_plant,
        )

        self.process.refresh_assigned_analysts()

        if not self.process.is_authorized_analyst(
            self.user
        ):
            raise ValidationError(
                "You are not assigned to this chemical analysis."
            )

        if not self.process.analysis_ready:
            raise ValidationError(
                "The analysis is not ready to start."
            )

        return cleaned


class CompleteAnalysisForm(forms.Form):
    confirm = forms.BooleanField(
        required=True,
        label="Confirm that all required analytical tests are complete",
    )
    remarks = forms.CharField(
        required=False,
        max_length=1000,
        widget=forms.Textarea(
            attrs={"rows": 3}
        ),
    )

    def __init__(
        self,
        *args,
        process=None,
        user=None,
        batch=None,
        **kwargs,
    ):
        self.process = process
        self.user = user
        self.batch = batch or getattr(process, "batch", None)

        super().__init__(
            *args,
            **kwargs,
        )

        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        _require_active_user(
            self.user,
            "An authenticated analyst is required.",
        )

        _require_permission(
            self.user,
            "ca.complete_chemical_analysis",
            "You do not have permission to complete chemical analysis.",
        )

        if self.process is None:
            raise ValidationError(
                "The chemical analysis process is required."
            )

        self.process.refresh_assigned_analysts()

        if not self.process.is_authorized_analyst(
            self.user
        ):
            raise ValidationError(
                "You are not assigned to this chemical analysis."
            )

        if not self.process.is_analysis_complete:
            raise ValidationError(
                "All required analytical tests must be completed first."
            )

        return cleaned


class QAReviewForm(forms.Form):
    decision = forms.ChoiceField(
        choices=(
            ("PASS", "Accept analysis"),
            ("REWORK", "Return for rework"),
            ("CAPA", "Require CAPA"),
            ("REJECT", "Reject analysis"),
        )
    )
    remarks = forms.CharField(
        required=False,
        max_length=3000,
        widget=forms.Textarea(
            attrs={"rows": 5}
        ),
    )
    assignable_cause = forms.CharField(
        required=False,
        max_length=3000,
        label="Potential assignable cause",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    corrective_action = forms.CharField(
        required=False,
        max_length=3000,
        label="Immediate corrective action / containment",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    confirm = forms.BooleanField(
        required=True,
        label="Confirm chemist review decision",
    )

    def __init__(
        self,
        *args,
        user=None,
        process=None,
        batch=None,
        **kwargs,
    ):
        self.user = user
        self.process = process
        self.batch = batch or getattr(process, "batch", None)

        super().__init__(
            *args,
            **kwargs,
        )

        self.requires_local_repeat = False
        self.requires_interlaboratory_review = False
        if self.process is not None and self.process.failed_tests.exists():
            failed_parameter_ids = list(self.process.failed_tests.values_list("parameter_id", flat=True))
            comparisons = ChemicalLabComparisonResult.objects.filter(
                process=self.process,
                treatment_plant_id=self.process.batch.treatment_plant_id,
                parameter_id__in=failed_parameter_ids,
            )
            latest_qc_status = {}
            for qc in self.process.qc_checks.order_by("control_id", "parameter_id", "-run_number", "-pk"):
                latest_qc_status.setdefault((qc.control_id, qc.parameter_id), qc.status)
            failed_qc_parameter_ids = {
                parameter_id
                for (_, parameter_id), status in latest_qc_status.items()
                if status != ChemicalQCCheckResult.Status.PASS
            }
            self.requires_local_repeat = bool(set(failed_parameter_ids) & failed_qc_parameter_ids) or any(
                not comparisons.filter(parameter_id=parameter_id, source=ChemicalLabComparisonResult.Source.ORIGINAL).exists()
                or not comparisons.filter(parameter_id=parameter_id, source=ChemicalLabComparisonResult.Source.REASSIGNMENT).exists()
                for parameter_id in failed_parameter_ids
            )
            if self.requires_local_repeat:
                analysts = chemical_analysis_analyst_queryset(
                    treatment_plant=self.process.batch.treatment_plant,
                    batch=self.process.batch,
                    analysis_request=self.process.analysis_request if self.process.analysis_request_id else None,
                )
                retest_analysts = permission_user_queryset(
                    "ca.perform_chemical_retest",
                    treatment_plant=self.process.batch.treatment_plant,
                )
                failed_analyst_ids = self.process.failed_tests.values_list("analyst_id", flat=True)
                analysts = analysts.filter(
                    assigned_treatment_plant_id=self.process.batch.treatment_plant_id
                ).filter(
                    pk__in=retest_analysts.values("pk")
                ).exclude(is_superuser=True).exclude(pk__in=failed_analyst_ids).exclude(pk=getattr(self.user, "pk", None))
                self.fields["replacement_analyst"] = forms.ModelChoiceField(
                    queryset=analysts,
                    required=False,
                    label="Replacement analyst",
                    help_text="The selected qualified analyst at this plant must differ from every analyst who recorded a current failed result.",
                )
                self.fields["replacement_analyst"].label_from_instance = lambda analyst: (
                    f"{analyst.full_name} · @{analyst.get_username()}" if analyst.full_name else analyst.get_username()
                )
                if analysts.exists():
                    self.fields["decision"].choices = [
                        ("REASSIGN", "Reassign failed parameters to another analyst"),
                    ]
                else:
                    self.fields["decision"].choices = [("REJECT", "Reject the batch")]
            else:
                self.requires_interlaboratory_review = True
                target_plants = (
                    TreatmentPlant.objects.filter(laboratory__isnull=False)
                    .exclude(pk=self.process.batch.treatment_plant_id)
                    .exclude(pk__in=self.process.interlaboratory_referrals.values("treatment_plant_id"))
                    .order_by("name")
                )
                eligible_plant_ids = [
                    plant.pk
                    for plant in target_plants
                    if permission_user_queryset(
                        "ca.record_chemical_result",
                        treatment_plant=plant,
                    ).filter(assigned_treatment_plant_id=plant.pk).exclude(is_superuser=True).exists()
                ]
                target_plants = target_plants.filter(pk__in=eligible_plant_ids)
                if target_plants.exists():
                    self.fields["decision"].choices = [
                        ("REFER_INTERLAB", "Forward persistent failures to other plant laboratories"),
                    ]
                else:
                    self.fields["decision"].choices = [("REJECT", "Reject the batch")]
                self.fields["laboratories"] = forms.ModelMultipleChoiceField(
                    queryset=target_plants,
                    required=False,
                    label="Independent laboratories",
                    help_text="Select other treatment-plant laboratories to independently test every persistent failure.",
                    widget=forms.CheckboxSelectMultiple(),
                )
        elif self.process is not None:
            self.fields["decision"].choices = [
                ("PASS", "Accept analysis"),
                ("CAPA", "Require CAPA"),
                ("REJECT", "Reject analysis"),
            ]

        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        _require_active_user(
            self.user,
            "An active QA officer is required.",
        )

        _require_permission(
            self.user,
            "ca.review_chemical_analysis",
            "You do not have permission to perform chemist review.",
        )

        if self.process is None:
            raise ValidationError(
                "The chemical analysis process is required."
            )

        if self.process.status != (
            ChemicalAnalysisProcess.WorkflowStatus.QA
        ):
            raise ValidationError(
                "This analysis is not currently awaiting chemist review."
            )

        decision = cleaned.get(
            "decision"
        )

        if decision == "PASS":
            if not self.process.is_analysis_complete:
                raise ValidationError(
                    "The analysis cannot pass QA while required tests remain incomplete."
                )

        if decision == "REASSIGN" and not self.requires_local_repeat:
            raise ValidationError("Every persistent failed parameter already has an independent local repeat.")
        if decision == "REASSIGN" and not cleaned.get("replacement_analyst"):
            self.add_error("replacement_analyst", "Choose a different qualified analyst for the repeat analysis.")
        if decision == "REFER_INTERLAB" and not self.requires_interlaboratory_review:
            raise ValidationError("Repeat each failed parameter with another local analyst before interlaboratory referral.")
        if decision == "REFER_INTERLAB" and not cleaned.get("laboratories"):
            self.add_error("laboratories", "Select at least one other treatment-plant laboratory.")

        if self.process is not None and self.process.failed_tests.exists():
            if not (cleaned.get("assignable_cause") or "").strip():
                self.add_error("assignable_cause", "Record the potential assignable cause for every failing analysis before disposition.")
            if not (cleaned.get("corrective_action") or "").strip():
                self.add_error("corrective_action", "Record the containment or corrective action taken for the failed analysis.")

        if (
            decision
            in {
                "REWORK",
                "CAPA",
                "REJECT",
                "REASSIGN",
                "REFER_INTERLAB",
            }
            and not (
                cleaned.get(
                    "remarks"
                )
                or ""
            ).strip()
        ):
            self.add_error(
                "remarks",
                "Remarks are required for this chemist review decision.",
            )

        return cleaned


class ApprovalForm(forms.Form):
    decision = forms.ChoiceField(
        choices=(
            ("APPROVE", "Approve"),
        )
    )
    remarks = forms.CharField(
        required=False,
        max_length=3000,
        widget=forms.Textarea(
            attrs={"rows": 4}
        ),
    )
    confirm = forms.BooleanField(
        required=True,
        label="Confirm this approval decision",
    )

    def __init__(
        self,
        *args,
        user=None,
        process=None,
        batch=None,
        **kwargs,
    ):
        self.user = user
        self.process = process
        self.batch = batch or getattr(process, "batch", None)

        super().__init__(
            *args,
            **kwargs,
        )

        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        _require_active_user(
            self.user,
            "An active approver is required.",
        )

        _require_permission(
            self.user,
            "ca.approve_chemical_batch",
            "You do not have permission to approve the chemical batch.",
        )

        if self.process is None:
            raise ValidationError(
                "The chemical analysis process is required."
            )

        if self.process.status != (
            ChemicalAnalysisProcess.WorkflowStatus.QA
        ):
            raise ValidationError(
                "This process is not ready for final approval."
            )

        if not self.process.is_analysis_complete:
            raise ValidationError(
                "The analysis must be complete before approval."
            )

        if (
            cleaned.get("decision")
            == "REJECT"
            and not (
                cleaned.get(
                    "remarks"
                )
                or ""
            ).strip()
        ):
            self.add_error(
                "remarks",
                "A rejection reason is required.",
            )

        return cleaned


class RejectAnalysisForm(forms.Form):
    reason = forms.CharField(
        required=True,
        max_length=3000,
        widget=forms.Textarea(
            attrs={"rows": 4}
        ),
    )
    confirm = forms.BooleanField(
        required=True,
        label="Confirm rejection",
    )

    def __init__(
        self,
        *args,
        user=None,
        process=None,
        batch=None,
        **kwargs,
    ):
        self.user = user
        self.process = process
        self.batch = batch or getattr(process, "batch", None)

        super().__init__(
            *args,
            **kwargs,
        )

        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        _require_active_user(
            self.user,
            "An active user is required.",
        )

        _require_permission(
            self.user,
            "ca.reject_chemical_batch",
            "You do not have permission to reject this analysis.",
        )

        if not (
            cleaned.get("reason")
            or ""
        ).strip():
            self.add_error(
                "reason",
                "A rejection reason is required.",
            )

        return cleaned


class RetestForm(forms.Form):
    parameter = forms.ModelChoiceField(
        queryset=ChemicalSpecification.objects.none(),
        empty_label="Select parameter",
    )
    reason = forms.CharField(
        required=True,
        max_length=3000,
        widget=forms.Textarea(
            attrs={"rows": 4}
        ),
    )
    confirm = forms.BooleanField(
        required=True,
        label="Confirm retest request",
    )

    def __init__(
        self,
        *args,
        process=None,
        user=None,
        batch=None,
        **kwargs,
    ):
        self.process = process
        self.user = user
        self.batch = batch or getattr(process, "batch", None)

        super().__init__(
            *args,
            **kwargs,
        )

        if process is not None:
            failed_ids = process.failed_tests.values_list("parameter_id", flat=True)
            self.fields["parameter"].queryset = (
                process.required_specifications()
                .filter(pk__in=failed_ids)
                .select_related("chemical", "method")
            )

        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        _require_active_user(
            self.user,
            "An active analyst is required.",
        )

        _require_permission(
            self.user,
            "ca.perform_chemical_retest",
            "You do not have permission to request a chemical retest.",
        )

        if self.process is None:
            raise ValidationError(
                "The chemical analysis process is required."
            )

        parameter = cleaned.get(
            "parameter"
        )

        if parameter is not None:
            if not self.process.required_specifications().filter(
                pk=parameter.pk
            ).exists():
                raise ValidationError(
                    "The selected parameter is not required for this analysis."
                )

            failed_result = self.process.failed_tests.filter(parameter_id=parameter.pk).first()
            if failed_result and failed_result.analyst_id == getattr(self.user, "pk", None):
                self.add_error("parameter", "The failed result must be repeated by the newly assigned analyst, not its original analyst.")

        reason = (
            cleaned.get("reason")
            or ""
        ).strip()

        if not reason:
            self.add_error(
                "reason",
                "A retest reason is required.",
            )

        return cleaned


class ChemicalInterlaboratorySubmissionForm(forms.Form):
    submission_remarks = forms.CharField(
        required=False,
        max_length=3000,
        label="Laboratory notes",
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, referral=None, user=None, **kwargs):
        self.referral = referral
        self.user = user
        super().__init__(*args, **kwargs)
        self.parameters = []
        if referral is not None:
            parameter_ids = [int(value) for value in referral.parameter_ids]
            self.parameters = list(
                ChemicalSpecification.objects.filter(pk__in=parameter_ids)
                .select_related("chemical")
                .order_by("parameter_name", "pk")
            )
        from .inventory_instruments import inventory_equipment_queryset
        equipment = inventory_equipment_queryset(getattr(referral, "treatment_plant_id", None))

        for parameter in self.parameters:
            suffix = str(parameter.pk)
            self.fields[f"value_{suffix}"] = forms.DecimalField(
                required=False,
                max_digits=26,
                decimal_places=12,
                label=f"{parameter.parameter_name} — reading ({parameter.unit})",
            )
            self.fields[f"text_{suffix}"] = forms.CharField(
                required=False,
                max_length=255,
                label=f"{parameter.parameter_name} — textual result",
            )
            self.fields[f"status_{suffix}"] = forms.ChoiceField(
                required=False,
                choices=(("", "Select outcome"), ("PASS", "Pass"), ("FAIL", "Fail"), ("NA", "Not applicable")),
                label=f"{parameter.parameter_name} — outcome when numerical limits do not apply",
            )
            quality_plan = (
                ChemicalMethodQualityPlan.current_for(parameter.method, referral.treatment_plant)
                if parameter.method_id else None
            )
            comparison_rule = (referral.comparison_rules_snapshot or {}).get(suffix, {})
            self.fields[f"standard_{suffix}"] = forms.CharField(required=False, max_length=180, label=f"{parameter.parameter_name} — reference standard")
            self.fields[f"standard_lot_{suffix}"] = forms.CharField(
                required=bool(comparison_rule.get("require_reference_standard_lot", quality_plan and quality_plan.require_reference_standard_lot)),
                max_length=120,
                label=f"{parameter.parameter_name} — reference-standard lot",
            )
            self.fields[f"reagent_lots_{suffix}"] = forms.CharField(
                required=bool(comparison_rule.get("require_reagent_lot_numbers", quality_plan and quality_plan.require_reagent_lot_numbers)),
                max_length=500,
                label=f"{parameter.parameter_name} — reagent lot numbers",
            )
            self.fields[f"uncertainty_{suffix}"] = forms.DecimalField(
                required=bool(comparison_rule.get("require_measurement_uncertainty", quality_plan and quality_plan.require_measurement_uncertainty)),
                min_value=0,
                max_digits=24,
                decimal_places=12,
                label=f"{parameter.parameter_name} — expanded uncertainty",
            )
            self.fields[f"coverage_{suffix}"] = forms.DecimalField(
                required=False,
                min_value=Decimal("0.0001"),
                max_digits=8,
                decimal_places=4,
                initial=comparison_rule.get("uncertainty_coverage_factor") or (quality_plan.uncertainty_coverage_factor if quality_plan else Decimal("2")),
                label=f"{parameter.parameter_name} — coverage factor (k)",
            )
            if parameter.requires_instrument:
                self.fields[f"inventory_equipment_{suffix}"] = InventoryEquipmentChoiceField(
                    queryset=equipment,
                    required=True,
                    label=f"{parameter.parameter_name} — Inventory laboratory equipment",
                )
        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()
        _require_active_user(self.user, "An active analyst at the receiving laboratory is required.")
        _require_permission(self.user, "ca.record_chemical_result", "You do not have permission to submit laboratory results.")
        if self.referral is None:
            raise ValidationError("A laboratory referral is required.")
        _validate_plant_access(self.user, self.referral.treatment_plant)
        if self.referral.status != ChemicalLabReferral.Status.PENDING:
            raise ValidationError("This referral has already been submitted or closed.")
        for parameter in self.parameters:
            suffix = str(parameter.pk)
            value = cleaned.get(f"value_{suffix}")
            text = (cleaned.get(f"text_{suffix}") or "").strip()
            status = cleaned.get(f"status_{suffix}") or ""
            has_limits = parameter.min_limit is not None or parameter.max_limit is not None
            if value is None and not text:
                self.add_error(f"value_{suffix}", "Enter a numerical or textual result for this parameter.")
            if (value is None or not has_limits) and not status:
                self.add_error(f"status_{suffix}", "Select the laboratory outcome for this parameter.")
            if parameter.requires_instrument and not cleaned.get(f"inventory_equipment_{suffix}"):
                self.add_error(f"inventory_equipment_{suffix}", "Select the required Inventory laboratory equipment.")
        return cleaned

    def result_entries(self):
        return [
            {
                "parameter": parameter,
                "value": self.cleaned_data.get(f"value_{parameter.pk}"),
                "textual_result": (self.cleaned_data.get(f"text_{parameter.pk}") or "").strip(),
                "status": self.cleaned_data.get(f"status_{parameter.pk}") or "",
                "inventory_equipment": self.cleaned_data.get(f"inventory_equipment_{parameter.pk}"),
                "reference_standard_name": (self.cleaned_data.get(f"standard_{parameter.pk}") or "").strip(),
                "reference_standard_lot": (self.cleaned_data.get(f"standard_lot_{parameter.pk}") or "").strip(),
                "reagent_lot_numbers": (self.cleaned_data.get(f"reagent_lots_{parameter.pk}") or "").strip(),
                "expanded_uncertainty": self.cleaned_data.get(f"uncertainty_{parameter.pk}"),
                "uncertainty_coverage_factor": self.cleaned_data.get(f"coverage_{parameter.pk}"),
            }
            for parameter in self.parameters
        ]


class InterlaboratoryReviewForm(forms.Form):
    remarks = forms.CharField(
        required=True,
        min_length=5,
        max_length=3000,
        label="Chemist comparison remarks",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    confirm = forms.BooleanField(required=True, label="Confirm the interlaboratory comparison and finalize its average")
    acknowledge_disagreement = forms.BooleanField(
        required=False,
        label="I reviewed the laboratory spread, result-status differences, and uncertainty flags and will explain them in my remarks",
    )

    def __init__(self, *args, process=None, user=None, requires_disagreement_ack=False, **kwargs):
        self.process = process
        self.user = user
        self.requires_disagreement_ack = requires_disagreement_ack
        super().__init__(*args, **kwargs)
        self.fields["acknowledge_disagreement"].required = self.requires_disagreement_ack
        if not self.requires_disagreement_ack:
            self.fields.pop("acknowledge_disagreement")
        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()
        _require_active_user(self.user, "An active chemist reviewer is required.")
        _require_permission(self.user, "ca.review_chemical_analysis", "You do not have permission to review the laboratory comparison.")
        if self.process is None:
            raise ValidationError("The chemical analysis process is required.")
        if self.process.status != ChemicalAnalysisProcess.WorkflowStatus.QA:
            raise ValidationError("The process must remain in chemist review during laboratory comparison.")
        if self.process.is_analysis_performer(self.user):
            raise ValidationError("The analyst who performed the analysis cannot finalize its chemist comparison.")
        referrals = self.process.interlaboratory_referrals.all()
        if not referrals.exists():
            raise ValidationError("No independent laboratory referrals were recorded for this process.")
        if referrals.exclude(status=ChemicalLabReferral.Status.COMPLETED).exists():
            raise ValidationError("Wait for every referred laboratory to submit its complete result set.")
        if self.process.workflow_events.filter(event_type="interlaboratory_consensus_recorded").exists():
            raise ValidationError("The interlaboratory comparison has already been finalized.")
        if self.requires_disagreement_ack and not cleaned.get("acknowledge_disagreement"):
            self.add_error("acknowledge_disagreement", "Acknowledge the configured between-laboratory tolerance alert before finalizing.")
        return cleaned


class ChemicalMethodQCForm(forms.Form):
    """Collect method-configured QC checks before chemist review."""

    def __init__(self, *args, process=None, user=None, **kwargs):
        self.process = process
        self.user = user
        self.controls = []
        super().__init__(*args, **kwargs)
        if process is not None:
            specifications = list(process.required_specifications().select_related("method"))
            seen = set()
            for parameter in specifications:
                if not parameter.method_id:
                    continue
                plan = ChemicalMethodQualityPlan.current_for(parameter.method, process.batch.treatment_plant)
                if not plan:
                    continue
                for control in plan.controls.filter(is_active=True).order_by("pk"):
                    identity = (control.pk, parameter.pk)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    suffix = f"{control.pk}_{parameter.pk}"
                    self.fields[f"observed_{suffix}"] = forms.DecimalField(
                        required=control.required,
                        max_digits=24,
                        decimal_places=12,
                        label=f"{parameter.parameter_name} · {control.label} ({parameter.unit})",
                        help_text=(
                            f"Acceptance: {control.acceptance_min if control.acceptance_min is not None else '—'} to "
                            f"{control.acceptance_max if control.acceptance_max is not None else '—'}; "
                            f"{control.corrective_instruction or 'Investigate any failed control before releasing results.'}"
                        ),
                    )
                    self.fields[f"remarks_{suffix}"] = forms.CharField(
                        required=False,
                        max_length=1000,
                        label=f"{control.label} notes",
                    )
                    if control.check_type == ChemicalMethodQCControl.CheckType.INSTRUMENT:
                        try:
                            from .inventory_instruments import inventory_equipment_queryset
                            equipment = inventory_equipment_queryset(process.batch.treatment_plant_id)
                        except Exception:
                            equipment = None
                        self.fields[f"inventory_equipment_{suffix}"] = InventoryEquipmentChoiceField(
                            queryset=equipment if equipment is not None else BatchTestResult._meta.get_field("inventory_equipment").remote_field.model.objects.none(),
                            required=True,
                            label=f"{control.label} Inventory equipment",
                        )
                    self.controls.append((control, parameter, suffix))
        self.fields["confirm"] = forms.BooleanField(required=True, label="Confirm that all configured QC checks are recorded")
        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()
        _require_active_user(self.user, "An active analyst is required to record method QC.")
        _require_permission(self.user, "ca.record_chemical_result", "You do not have permission to record analytical QC.")
        if self.process is None or self.process.status not in {ChemicalAnalysisProcess.WorkflowStatus.QA, ChemicalAnalysisProcess.WorkflowStatus.RETEST}:
            raise ValidationError("Method QC can only be recorded after analysis or a retest is complete.")
        if not self.process.is_authorized_analyst(self.user):
            raise ValidationError("Only an assigned analyst may record QC for this analysis.")
        entries = []
        for control, parameter, suffix in self.controls:
            observed = cleaned.get(f"observed_{suffix}")
            if observed is None:
                if control.required:
                    self.add_error(f"observed_{suffix}", "This configured QC check is required.")
                continue
            result = BatchTestResult.objects.filter(batch=self.process.batch, parameter=parameter).first()
            comparison = result.value if result else None
            if control.evaluation == ChemicalMethodQCControl.Evaluation.RECOVERY_PERCENT:
                if control.check_type == ChemicalMethodQCControl.CheckType.REFERENCE:
                    denominator = control.target_value
                    comparison = denominator
                else:
                    denominator = control.spike_amount
                    comparison = result.value if result else None
                    if comparison is None:
                        self.add_error(f"observed_{suffix}", "The matrix spike needs a recorded base result for this parameter.")
                        continue
                    observed = observed - comparison
                if not denominator:
                    self.add_error(f"observed_{suffix}", "A configured positive QC target or spike amount is required.")
                    continue
                evaluated = observed / denominator * Decimal("100")
            elif control.evaluation == ChemicalMethodQCControl.Evaluation.RPD_PERCENT:
                if comparison is None:
                    self.add_error(f"observed_{suffix}", "The duplicate check needs a recorded base result for this parameter.")
                    continue
                mean = (abs(observed) + abs(comparison)) / Decimal("2")
                evaluated = Decimal("0") if mean == 0 else abs(observed - comparison) / mean * Decimal("100")
            else:
                evaluated = observed
            passed = (control.acceptance_min is None or evaluated >= control.acceptance_min) and (control.acceptance_max is None or evaluated <= control.acceptance_max)
            entries.append({
                "control": control,
                "parameter": parameter,
                "observed_value": cleaned.get(f"observed_{suffix}"),
                "evaluated_value": evaluated,
                "comparison_value": comparison,
                "status": ChemicalQCCheckResult.Status.PASS if passed else ChemicalQCCheckResult.Status.FAIL,
                "remarks": (cleaned.get(f"remarks_{suffix}") or "").strip(),
                "inventory_equipment": cleaned.get(f"inventory_equipment_{suffix}"),
            })
        self.qc_entries = entries
        return cleaned


class CAPAForm(forms.Form):
    corrective_action = forms.CharField(
        required=True,
        max_length=5000,
        widget=forms.Textarea(
            attrs={"rows": 5}
        ),
    )
    preventive_action = forms.CharField(
        required=True,
        max_length=5000,
        widget=forms.Textarea(
            attrs={"rows": 5}
        ),
    )
    confirm = forms.BooleanField(
        required=True,
        label="Confirm CAPA submission",
    )

    def __init__(
        self,
        *args,
        user=None,
        process=None,
        batch=None,
        **kwargs,
    ):
        self.user = user
        self.process = process
        self.batch = batch or getattr(process, "batch", None)

        super().__init__(
            *args,
            **kwargs,
        )

        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        _require_active_user(
            self.user,
            "An active user is required to submit CAPA.",
        )

        _require_permission(
            self.user,
            "ca.review_chemical_analysis",
            "You do not have permission to submit CAPA.",
        )

        for field in (
            "corrective_action",
            "preventive_action",
        ):
            value = (
                cleaned.get(field)
                or ""
            ).strip()

            if not value:
                self.add_error(
                    field,
                    "This field is required.",
                )

        return cleaned


class CoAGenerationForm(forms.Form):
    confirm = forms.BooleanField(
        required=True,
        label="Confirm generation of the Certificate of Analysis",
    )

    def __init__(
        self,
        *args,
        process=None,
        user=None,
        batch=None,
        **kwargs,
    ):
        self.process = process
        self.user = user
        self.batch = batch or getattr(process, "batch", None)

        super().__init__(
            *args,
            **kwargs,
        )

        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        _require_active_user(
            self.user,
            "An authenticated user is required.",
        )

        _require_permission(
            self.user,
            "ca.approve_chemical_coa",
            "You do not have permission to generate a Certificate of Analysis.",
        )

        if self.process is not None:
            if not self.process.is_analysis_complete:
                raise ValidationError(
                    "The analysis must be complete before the CoA is generated."
                )

        return cleaned


class KEBSReferralForm(forms.Form):
    confirm_dispatch = forms.BooleanField(required=True, label="Confirm that the failed chemical and its Certificate of Analysis are being forwarded to KEBS")
    kebs_reference = forms.CharField(required=False, max_length=120, label="KEBS case / reference number")
    dispatch_method = forms.CharField(required=True, max_length=80, label="Dispatch method")
    shipment_reference = forms.CharField(required=False, max_length=120, label="Courier, consignment, or handover reference")
    assignable_cause = forms.CharField(required=True, max_length=3000, label="Summary of investigation and failure cause", widget=forms.Textarea(attrs={"rows": 3}))
    remarks = forms.CharField(required=True, min_length=5, max_length=3000, label="KEBS referral remarks and requested assessment", widget=forms.Textarea(attrs={"rows": 3}))
    evidence = forms.FileField(required=False, label="Dispatch or handover evidence")

    def __init__(self, *args, process=None, user=None, batch=None, **kwargs):
        self.process = process
        self.user = user
        self.batch = batch or getattr(process, "batch", None)
        super().__init__(*args, **kwargs)
        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()
        _require_active_user(self.user, "An authenticated user is required to forward the batch to KEBS.")
        _require_permission(self.user, "ca.reject_chemical_batch", "You do not have permission to record the failed-batch KEBS handoff.")
        if self.process is None or self.batch is None or not self.process.failed_tests.exists():
            raise ValidationError("A failed final analysis is required before forwarding a chemical batch to KEBS.")
        _validate_plant_access(self.user, self.batch.treatment_plant)
        coa = ChemicalCoA.objects.filter(batch=self.batch, pdf_file__isnull=False).first()
        if coa is None or not coa.pdf_file:
            raise ValidationError("Generate the failed-result Certificate of Analysis before forwarding this chemical to KEBS.")
        if not (cleaned.get("assignable_cause") or "").strip():
            self.add_error("assignable_cause", "Summarize the known or suspected cause of the failure.")
        return cleaned


class CoAApprovalForm(forms.Form):
    confirm = forms.BooleanField(
        required=True,
        label="Confirm approval of the Certificate of Analysis",
    )
    remarks = forms.CharField(
        required=False,
        max_length=3000,
        widget=forms.Textarea(
            attrs={"rows": 4}
        ),
    )

    def __init__(
        self,
        *args,
        user=None,
        process=None,
        batch=None,
        **kwargs,
    ):
        self.user = user
        self.process = process
        self.batch = batch or getattr(process, "batch", None)

        super().__init__(
            *args,
            **kwargs,
        )

        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        _require_active_user(
            self.user,
            "An active certificate approver is required.",
        )

        _require_permission(
            self.user,
            "ca.approve_chemical_coa",
            "You do not have permission to approve the CoA.",
        )

        if self.process is not None:
            _validate_plant_access(
                self.user,
                self.process.batch.treatment_plant,
            )

        return cleaned


class ConfirmActionForm(forms.Form):
    action = forms.ChoiceField(
        choices=(
            ("UTILIZE", "Utilize batch"),
            ("RETURN", "Return to supplier"),
        )
    )
    confirm = forms.BooleanField(
        required=True,
        label="Confirm this workflow action",
    )
    remarks = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(
            attrs={"rows": 3}
        ),
    )

    def __init__(
        self,
        *args,
        batch=None,
        user=None,
        process=None,
        **kwargs,
    ):
        self.batch = batch or getattr(process, "batch", None)
        self.user = user
        self.process = process

        super().__init__(
            *args,
            **kwargs,
        )

        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        _require_active_user(
            self.user,
            "An authenticated user is required.",
        )

        if self.batch is None:
            raise ValidationError(
                "A chemical batch is required."
            )

        _validate_plant_access(
            self.user,
            self.batch.treatment_plant,
        )

        if self.batch.status != (
            ChemicalBatch.Status.APPROVED
        ):
            raise ValidationError(
                "Only approved chemical batches can be utilized or returned."
            )

        action = cleaned.get(
            "action"
        )

        permission = {
            "UTILIZE": "ca.utilize_chemical_batch",
            "RETURN": "ca.return_chemical_batch",
        }.get(action)

        if permission:
            _require_permission(
                self.user,
                permission,
            )

        return cleaned


class BatchStatusTransitionForm(forms.Form):
    target_status = forms.ChoiceField(
        choices=()
    )
    remarks = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(
            attrs={"rows": 3}
        ),
    )
    confirm = forms.BooleanField(
        required=True,
        label="Confirm status transition",
    )

    def __init__(
        self,
        *args,
        batch=None,
        user=None,
        **kwargs,
    ):
        self.batch = batch
        self.user = user

        super().__init__(
            *args,
            **kwargs,
        )

        if batch is not None:
            labels = dict(
                ChemicalBatch.Status.choices
            )

            self.fields[
                "target_status"
            ].choices = [
                (
                    status,
                    labels.get(
                        status,
                        status,
                    ),
                )
                for status in batch.batch_transitions.get(
                    batch.status,
                    [],
                )
            ]

        apply_field_classes(self)

    def clean_target_status(self):
        target = self.cleaned_data.get(
            "target_status"
        )

        if self.batch is None:
            raise ValidationError(
                "A chemical batch is required."
            )

        if not self.batch.can_transition_to(
            target
        ):
            raise ValidationError(
                f"The batch cannot transition from "
                f"{self.batch.status} to {target}."
            )

        permission_map = {
            ChemicalBatch.Status.ANALYSED:
                "ca.complete_chemical_analysis",
            ChemicalBatch.Status.APPROVED:
                "ca.approve_chemical_batch",
            ChemicalBatch.Status.REJECTED:
                "ca.reject_chemical_batch",
            ChemicalBatch.Status.UTILIZED:
                "ca.utilize_chemical_batch",
            ChemicalBatch.Status.RETURNED:
                "ca.return_chemical_batch",
        }

        permission = permission_map.get(
            target
        )

        if permission:
            _require_permission(
                self.user,
                permission,
            )

        return target


class RequestStatusTransitionForm(forms.Form):
    target_status = forms.ChoiceField(
        choices=()
    )
    confirm = forms.BooleanField(
        required=True,
        label="Confirm analysis request transition",
    )

    def __init__(
        self,
        *args,
        request=None,
        user=None,
        **kwargs,
    ):
        self.analysis_request = request
        self.user = user

        super().__init__(
            *args,
            **kwargs,
        )

        if request is not None:
            labels = dict(
                ChemicalAnalysisRequest.Status.choices
            )

            self.fields[
                "target_status"
            ].choices = [
                (
                    status,
                    labels.get(
                        status,
                        status,
                    ),
                )
                for status in request.request_transitions.get(
                    request.status,
                    (),
                )
            ]

        apply_field_classes(self)

    def clean_target_status(self):
        target = self.cleaned_data.get(
            "target_status"
        )

        if self.analysis_request is None:
            raise ValidationError(
                "An analysis request is required."
            )

        if not self.analysis_request.can_transition_to(
            target
        ):
            raise ValidationError(
                f"The analysis request cannot transition from "
                f"{self.analysis_request.status} to {target}."
            )

        return target


class ChemicalProcessActionForm(forms.Form):
    ACTION_CHOICES = (
        (
            "START_ANALYSIS",
            "Start analysis",
        ),
        (
            "COMPLETE_ANALYSIS",
            "Complete analysis",
        ),
        (
            "APPROVE",
            "Approve",
        ),
        (
            "REJECT",
            "Reject",
        ),
        (
            "RETEST",
            "Request retest",
        ),
        (
            "CAPA",
            "Initiate CAPA",
        ),
    )

    action = forms.ChoiceField(
        choices=ACTION_CHOICES
    )
    remarks = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(
            attrs={"rows": 3}
        ),
    )
    confirm = forms.BooleanField(
        required=True,
        label="Confirm workflow action",
    )

    def __init__(
        self,
        *args,
        process=None,
        available_actions=None,
        user=None,
        **kwargs,
    ):
        self.process = process
        self.user = user

        super().__init__(
            *args,
            **kwargs,
        )

        labels = dict(
            self.ACTION_CHOICES
        )

        if available_actions is None:
            available_actions = []

            if process is not None:
                status = process.status

                if status == ChemicalAnalysisProcess.WorkflowStatus.CHECKLIST:
                    if process.analysis_ready and _has_perm(user, "ca.start_chemical_analysis"):
                        available_actions.append("START_ANALYSIS")

                if status == (
                    ChemicalAnalysisProcess.WorkflowStatus.ANALYSIS
                ):
                    if _has_perm(
                        user,
                        "ca.complete_chemical_analysis",
                    ):
                        available_actions.append(
                            "COMPLETE_ANALYSIS"
                        )

                if status == (
                    ChemicalAnalysisProcess.WorkflowStatus.QA
                ):
                    if _has_perm(
                        user,
                        "ca.approve_chemical_batch",
                    ):
                        available_actions.append(
                            "APPROVE"
                        )

                    if _has_perm(
                        user,
                        "ca.reject_chemical_batch",
                    ):
                        available_actions.append(
                            "REJECT"
                        )

                    if _has_perm(
                        user,
                        "ca.perform_chemical_retest",
                    ):
                        available_actions.append(
                            "RETEST"
                        )

                    if _has_perm(
                        user,
                        "ca.review_chemical_analysis",
                    ):
                        available_actions.append(
                            "CAPA"
                        )

        self.fields[
            "action"
        ].choices = [
            (
                action,
                labels.get(
                    action,
                    action.replace(
                        "_",
                        " ",
                    ).title(),
                ),
            )
            for action in available_actions
        ]

        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        _require_active_user(
            self.user,
            "An authenticated workflow user is required.",
        )

        if self.process is None:
            raise ValidationError(
                "The chemical analysis process is required."
            )

        _validate_plant_access(
            self.user,
            self.process.batch.treatment_plant,
        )

        if self.process.status == (
            ChemicalAnalysisProcess.WorkflowStatus.COMPLETED
        ):
            raise ValidationError(
                "No workflow actions are available after completion."
            )

        action = cleaned.get(
            "action"
        )

        if not action:
            return cleaned

        permission_map = {
            "START_ANALYSIS":
                "ca.start_chemical_analysis",
            "COMPLETE_ANALYSIS":
                "ca.complete_chemical_analysis",
            "APPROVE":
                "ca.approve_chemical_batch",
            "REJECT":
                "ca.reject_chemical_batch",
            "RETEST":
                "ca.perform_chemical_retest",
            "CAPA":
                "ca.review_chemical_analysis",
        }

        permission = permission_map.get(
            action
        )

        if permission:
            _require_permission(
                self.user,
                permission,
            )

        return cleaned


class ChemicalDashboardActionForm(forms.Form):
    ACTION_CHOICES = (
        (
            "start_analysis",
            "Start analysis",
        ),
        (
            "complete_analysis",
            "Complete analysis",
        ),
        (
            "complete_checklist",
            "Complete checklist",
        ),
        (
            "complete_sampling",
            "Complete sampling",
        ),
        (
            "request_retest",
            "Request retest",
        ),
        (
            "qa_review",
            "Chemist review",
        ),
        (
            "capa",
            "CAPA",
        ),
        (
            "reject",
            "Reject",
        ),
        (
            "utilize",
            "Utilize",
        ),
        (
            "return",
            "Return to supplier",
        ),
    )

    action = forms.ChoiceField(
        choices=ACTION_CHOICES
    )
    process_id = forms.IntegerField(
        min_value=1,
        widget=forms.HiddenInput()
    )
    confirm = forms.BooleanField(
        required=True,
        label="Confirm action",
    )
    remarks = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(
            attrs={"rows": 3}
        ),
    )

    def __init__(
        self,
        *args,
        available_actions=None,
        process=None,
        user=None,
        **kwargs,
    ):
        self.process = process
        self.user = user

        super().__init__(
            *args,
            **kwargs,
        )

        if available_actions is not None:
            available = set(
                available_actions
            )

            self.fields[
                "action"
            ].choices = [
                choice
                for choice in self.ACTION_CHOICES
                if choice[0] in available
            ]

        if process is not None:
            self.initial.setdefault(
                "process_id",
                process.pk,
            )

        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        _require_active_user(
            self.user,
            "An authenticated user is required.",
        )

        if self.process is None:
            raise ValidationError(
                "The chemical analysis process is required."
            )

        if (
            cleaned.get("process_id")
            != self.process.pk
        ):
            raise ValidationError(
                "The selected workflow process is invalid."
            )

        _validate_plant_access(
            self.user,
            self.process.batch.treatment_plant,
        )

        return cleaned


class UtilizationForm(forms.Form):
    confirm = forms.BooleanField(
        required=True,
        label="Confirm utilization of the approved chemical batch",
    )
    quantity_used = forms.DecimalField(
        required=False,
        min_value=Decimal("0.000001"),
        max_digits=16,
        decimal_places=6,
        widget=forms.NumberInput(
            attrs={"step": "any"}
        ),
    )
    remarks = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(
            attrs={"rows": 3}
        ),
    )

    def __init__(
        self,
        *args,
        batch=None,
        user=None,
        **kwargs,
    ):
        self.batch = batch
        self.user = user

        super().__init__(
            *args,
            **kwargs,
        )

        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        _require_active_user(
            self.user,
            "An authenticated user is required.",
        )

        _require_permission(
            self.user,
            "ca.utilize_chemical_batch",
            "You do not have permission to utilize chemical batches.",
        )

        if self.batch is None:
            raise ValidationError(
                "A chemical batch is required."
            )

        _validate_plant_access(
            self.user,
            self.batch.treatment_plant,
        )

        if self.batch.status != (
            ChemicalBatch.Status.APPROVED
        ):
            raise ValidationError(
                "Only approved batches can be utilized."
            )

        quantity = cleaned.get(
            "quantity_used"
        )

        if (
            quantity is not None
            and quantity
            > Decimal(
                str(
                    self.batch.quantity_received
                )
            )
        ):
            self.add_error(
                "quantity_used",
                "Quantity utilized cannot exceed quantity received.",
            )

        return cleaned


class ReturnToSupplierForm(forms.Form):
    confirm = forms.BooleanField(
        required=True,
        label="Confirm return of the chemical batch to supplier",
    )
    quantity_returned = forms.DecimalField(
        required=False,
        min_value=Decimal("0.000001"),
        max_digits=16,
        decimal_places=6,
        widget=forms.NumberInput(
            attrs={"step": "any"}
        ),
    )
    reason = forms.CharField(
        required=True,
        max_length=3000,
        widget=forms.Textarea(
            attrs={"rows": 4}
        ),
    )

    def __init__(
        self,
        *args,
        batch=None,
        user=None,
        **kwargs,
    ):
        self.batch = batch
        self.user = user

        super().__init__(
            *args,
            **kwargs,
        )

        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()

        _require_active_user(
            self.user,
            "An authenticated user is required.",
        )

        _require_permission(
            self.user,
            "ca.return_chemical_batch",
            "You do not have permission to return chemical batches.",
        )

        if self.batch is None:
            raise ValidationError(
                "A chemical batch is required."
            )

        _validate_plant_access(
            self.user,
            self.batch.treatment_plant,
        )

        if self.batch.status not in {
            ChemicalBatch.Status.APPROVED,
            ChemicalBatch.Status.REJECTED,
        }:
            raise ValidationError(
                "This batch cannot currently be returned to the supplier."
            )

        if not (
            cleaned.get("reason")
            or ""
        ).strip():
            self.add_error(
                "reason",
                "A return reason is required.",
            )

        quantity = cleaned.get(
            "quantity_returned"
        )

        if (
            quantity is not None
            and quantity
            > Decimal(
                str(
                    self.batch.quantity_received
                )
            )
        ):
            self.add_error(
                "quantity_returned",
                "Quantity returned cannot exceed quantity received.",
            )

        return cleaned


class ChemicalSpecificationFilterForm(forms.Form):
    chemical = forms.ModelChoiceField(
        queryset=Chemical.objects.none(),
        required=False,
        empty_label="All chemicals",
    )
    treatment_plant = forms.ModelChoiceField(
        queryset=TreatmentPlant.objects.none(),
        required=False,
        empty_label="All treatment plants",
    )
    required_only = forms.BooleanField(
        required=False,
        initial=True,
        label="Required specifications only",
    )

    def __init__(
        self,
        *args,
        user=None,
        treatment_plant=None,
        **kwargs,
    ):
        self.user = user
        self.treatment_plant = _effective_plant(
            user,
            treatment_plant,
        )

        super().__init__(
            *args,
            **kwargs,
        )

        self.fields[
            "chemical"
        ].queryset = Chemical.objects.order_by(
            "name"
        )

        plants = TreatmentPlant.objects.order_by(
            "name"
        )

        if self.treatment_plant is not None:
            plants = plants.filter(
                pk=_plant_id(
                    self.treatment_plant
                )
            )

        self.fields[
            "treatment_plant"
        ].queryset = plants

        apply_field_classes(self)


class TestResultFilterForm(forms.Form):
    status = forms.ChoiceField(
        required=False,
        choices=(
            ("", "All statuses"),
            *BatchTestResult.Status.choices,
        ),
    )
    parameter = forms.ModelChoiceField(
        queryset=ChemicalSpecification.objects.none(),
        required=False,
        empty_label="All parameters",
    )
    analyst = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        empty_label="All analysts",
    )

    def __init__(
        self,
        *args,
        user=None,
        treatment_plant=None,
        section=None,
        **kwargs,
    ):
        super().__init__(
            *args,
            **kwargs,
        )

        self.fields[
            "analyst"
        ].queryset = scoped_user_queryset(
            treatment_plant,
            section=section,
            user=user,
        )

        self.fields["parameter"].queryset = ChemicalSpecification.current_for(
            _scope_plant_queryset(
                ChemicalSpecification.objects.select_related("chemical", "treatment_plant"),
                user=user,
                treatment_plant=treatment_plant,
            )
        ).order_by(
            "chemical__name",
            "parameter_name",
            "-version",
        )

        apply_field_classes(self)


class ProcessFilterForm(forms.Form):
    status = forms.ChoiceField(
        required=False,
        choices=(
            ("", "All workflow states"),
            *ChemicalAnalysisProcess.WorkflowStatus.choices,
        ),
    )
    analyst = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        empty_label="All analysts",
    )
    qa_officer = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        empty_label="All QA officers",
    )
    overdue_only = forms.BooleanField(
        required=False,
        label="Overdue only",
    )

    def __init__(
        self,
        *args,
        user=None,
        treatment_plant=None,
        section=None,
        **kwargs,
    ):
        super().__init__(
            *args,
            **kwargs,
        )

        users = scoped_user_queryset(
            treatment_plant,
            section=section,
            user=user,
        )

        self.fields[
            "analyst"
        ].queryset = users

        self.fields[
            "qa_officer"
        ].queryset = users

        apply_field_classes(self)


class ChemicalCertApproverForm(forms.Form):
    approver = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=True,
        empty_label="Select certificate approver",
    )
    confirm = forms.BooleanField(
        required=True,
        label="Confirm certificate approval assignment",
    )

    def __init__(
        self,
        *args,
        user=None,
        treatment_plant=None,
        **kwargs,
    ):
        self.user = user

        super().__init__(
            *args,
            **kwargs,
        )

        plant = _effective_plant(
            user,
            treatment_plant,
        )

        self.fields[
            "approver"
        ].queryset = approver_queryset(
            plant
        )

        apply_field_classes(self)

    def clean_approver(self):
        approver = self.cleaned_data.get(
            "approver"
        )

        if approver is None:
            raise ValidationError(
                "A certificate approver is required."
            )

        if not approver.is_active:
            raise ValidationError(
                "An inactive user cannot approve a certificate."
            )

        if not _has_perm(
            approver,
            "ca.approve_chemical_coa",
        ):
            raise ValidationError(
                "The selected user does not have the required CoA approval permission."
            )

        return approver


class AnalystAssignmentForm(forms.Form):
    analysts = forms.ModelMultipleChoiceField(
        queryset=User.objects.none(),
        required=True,
        widget=forms.CheckboxSelectMultiple(
            attrs={
                "aria-label": "Select analysts permitted to perform chemical analysis",
            }
        ),
    )
    confirm = forms.BooleanField(required=True, label="Confirm analyst assignment")

    def __init__(self, *args, process=None, user=None, **kwargs):
        self.process = process
        self.user = user
        super().__init__(*args, **kwargs)
        plant = getattr(process, "treatment_plant", None)
        if plant is None:
            plant = getattr(getattr(process, "batch", None), "treatment_plant", None)
        batch = getattr(process, "batch", None)
        analyst_queryset = (
            chemical_analysis_analyst_queryset(
                treatment_plant=plant,
                batch=batch,
                analysis_request=process if isinstance(process, ChemicalAnalysisRequest) else None,
            )
            if plant is not None else User.objects.none()
        )
        analyst_field = self.fields["analysts"]
        analyst_field.queryset = analyst_queryset
        analyst_field.label_from_instance = lambda analyst: (
            f"{analyst.full_name} · @{analyst.get_username()}"
            if analyst.full_name
            else analyst.get_username()
        )
        analyst_candidates = list(analyst_queryset)
        self.analyst_count = len(analyst_candidates)
        analyst_field.choices = [
            (
                str(analyst.pk),
                analyst_field.label_from_instance(analyst),
            )
            for analyst in analyst_candidates
        ]
        if process is not None:
            self.initial["analysts"] = process.assigned_analysts.values_list("pk", flat=True)
        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()
        _require_active_user(self.user)
        _require_permission(self.user, "ca.create_analysis_request", "You do not have permission to assign analysts.")
        if self.process is None:
            raise ValidationError("The chemical analysis process is required.")
        _validate_plant_access(self.user, self.process.treatment_plant)
        if not cleaned.get("analysts"):
            raise ValidationError("At least one analyst must be assigned.")
        return cleaned


class ChemicalBatchHoldResolutionForm(forms.Form):
    resolution = forms.CharField(
        label="Resolution / corrective action",
        min_length=5,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    reject_batch = forms.BooleanField(
        required=False,
        label="Reject the batch and stop the workflow",
    )

    def __init__(self, *args, user=None, hold=None, **kwargs):
        self.user = user
        self.hold = hold
        super().__init__(*args, **kwargs)
        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()
        _require_active_user(self.user)
        _require_permission(self.user, "ca.resolve_chemical_batch_hold")
        if self.hold is None:
            raise ValidationError("A chemical batch hold is required.")
        _validate_plant_access(self.user, self.hold.batch.treatment_plant)
        if self.hold.status != ChemicalBatchHold.Status.OPEN:
            raise ValidationError("This hold has already been closed.")
        return cleaned


class ChemicalCustodyRecordForm(forms.Form):
    from_location = forms.CharField(label="From location", max_length=255, required=False)
    to_location = forms.CharField(label="To location", max_length=255, required=True)
    seal_number = forms.CharField(label="Seal / container identifier", max_length=100, required=False)
    evidence = forms.FileField(label="Evidence attachment", required=False)
    remarks = forms.CharField(label="Transfer notes", widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, user=None, batch=None, **kwargs):
        self.user = user
        self.batch = batch
        super().__init__(*args, **kwargs)
        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()
        _require_active_user(self.user)
        _require_permission(self.user, "ca.record_chemical_custody")
        if self.batch is None:
            raise ValidationError("A chemical batch is required.")
        _validate_plant_access(self.user, self.batch.treatment_plant)
        return cleaned


class AnalystAutoAssignmentForm(forms.Form):
    confirm = forms.BooleanField(required=True, label="Confirm automatic analyst assignment")

    def __init__(self, *args, process=None, user=None, **kwargs):
        self.process = process
        self.user = user
        super().__init__(*args, **kwargs)
        apply_field_classes(self)

    def clean(self):
        cleaned = super().clean()
        _require_active_user(self.user)
        _require_permission(self.user, "ca.create_analysis_request", "You do not have permission to trigger analyst assignment.")
        if self.process is None or self.process.analysis_request_id is None:
            raise ValidationError("An analysis request is required before analyst assignment.")
        request = self.process.analysis_request
        if not request.assignment_overdue:
            raise ValidationError("An analyst is already assigned to this request.")
        _validate_plant_access(self.user, self.process.treatment_plant)
        return cleaned
