import uuid
import json
import hashlib
import math
import ast
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_HALF_EVEN, ROUND_DOWN, ROUND_UP
from django.apps import apps
from django.db.models import Max
from django.db import models, transaction
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from accounts.models import CustomUser  # Imported as requested
from django.utils import timezone

# -------------------------
# Abstract Base Method Model
# -------------------------
class AbstractBaseMethod(models.Model):
    """
    Abstract base class for all analysis methods (chemical, microbial, etc.)
    with UUID, audit fields, versioning.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(CustomUser, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="created_%(class)s")
    updated_by = models.ForeignKey(CustomUser, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="updated_%(class)s")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-updated_at"]

# -------------------------
# Chemical Analysis Method
# -------------------------
class ChemicalAnalysisMethod(AbstractBaseMethod):
    method = models.CharField(max_length=255)
    kebs_reference = models.CharField(max_length=255, help_text="Official KEBS reference standard")
    formula = models.TextField(
        blank=True,
        help_text="Pythonic formula using variables from `inputs_structure`."
    )
    inputs_structure = models.JSONField(default=list, blank=True)
    unit = models.CharField(max_length=50, blank=True)
    rounding_precision = models.PositiveSmallIntegerField(default=2)
    rounding_mode = models.CharField(
        max_length=16,
        choices=(
            ("HALF_UP", "Half up"),
            ("HALF_EVEN", "Half even"),
            ("DOWN", "Toward zero"),
            ("UP", "Away from zero"),
        ),
        default="HALF_UP",
    )
    description = models.TextField(blank=True)
    category = models.CharField(max_length=100, blank=True)
    tags = models.JSONField(default=list, blank=True)
    formula_hash = models.CharField(max_length=64, editable=False, blank=True)

    class Meta:
        verbose_name = "Chemical Analysis Method"
        verbose_name_plural = "Chemical Analysis Methods"
        unique_together = ("method", "version")
        ordering = ["method", "-version"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(rounding_precision__gte=0, rounding_precision__lte=12),
                name="am_rounding_precision_range",
            ),
            models.CheckConstraint(
                condition=models.Q(rounding_mode__in=["HALF_UP", "HALF_EVEN", "DOWN", "UP"]),
                name="am_rounding_mode_valid",
            ),
        ]

    def __str__(self):
        status = "" if self.is_active else " [inactive]"
        return f"{self.method} (v{self.version}){status}"

    def clean(self):
        if self.formula:
            from ca.models import validate_formula_expression as validate_chemical_formula

            validate_chemical_formula(self.formula, allow_math_attr=True)
            input_names = self.get_input_fields()
            if not input_names:
                raise ValidationError({
                    "inputs_structure": "A formula needs at least one configured measurement input."
                })
            invalid_names = [
                name for name in input_names
                if not name.isidentifier() or name in {"math", "__builtins__"}
            ]
            if invalid_names:
                raise ValidationError({
                    "inputs_structure": "Invalid formula input name(s): " + ", ".join(invalid_names)
                })
            tree = ast.parse(self.formula, mode="eval")
            unknown_names = sorted({
                node.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Name) and node.id not in set(input_names) | {"math"}
            })
            if unknown_names:
                raise ValidationError({
                    "formula": "The formula references input(s) that are not configured: "
                    + ", ".join(unknown_names)
                })
        if self.rounding_precision < 0 or self.rounding_precision > 12:
            raise ValidationError({"rounding_precision": "Precision must be between 0 and 12 decimal places."})
        if self.rounding_mode not in {"HALF_UP", "HALF_EVEN", "DOWN", "UP"}:
            raise ValidationError({"rounding_mode": "Select a supported scientific rounding rule."})

    def get_input_fields(self):
        definitions = self.get_input_definitions()
        return list(dict.fromkeys(item["name"] for item in definitions if item.get("name")))

    def _input_definitions_from_structure(self):
        definitions = self.inputs_structure
        if isinstance(definitions, dict):
            definitions = [
                {"name": name, **(value if isinstance(value, dict) else {"label": value})}
                for name, value in definitions.items()
            ]
        elif isinstance(definitions, str):
            definitions = [{"name": name.strip()} for name in definitions.split(",") if name.strip()]
        if not isinstance(definitions, (list, tuple)):
            return []
        output = []
        for definition in definitions:
            if isinstance(definition, str):
                output.append({"name": definition.strip(), "required": True, "type": "number"})
            elif isinstance(definition, dict) and definition.get("name"):
                output.append(dict(definition, name=str(definition["name"]).strip()))
        return output

    def get_input_definitions(self):
        if self.pk:
            try:
                related = list(self.analysis_inputs.order_by("position", "pk"))
            except Exception:
                related = []
            if related:
                return [item.as_definition() for item in related]
        return self._input_definitions_from_structure()

    def _sync_analysis_inputs(self):
        InputModel = apps.get_model("ca", "ChemicalAnalysisInput")
        manager = InputModel.objects.using(self._state.db)
        if manager.filter(method_id=self.pk).exists():
            return
        definitions = self._input_definitions_from_structure()
        rows = []
        for position, definition in enumerate(definitions):
            name = str(definition.get("name") or "").strip()
            if not name:
                continue
            rows.append(InputModel(
                method_id=self.pk,
                name=name,
                label=definition.get("label") or name.replace("_", " ").title(),
                input_type=definition.get("type") or "number",
                unit=definition.get("unit") or "",
                help_text=definition.get("help_text") or "",
                required=definition.get("required", True),
                min_value=definition.get("min_value"),
                max_value=definition.get("max_value"),
                choices=definition.get("choices") or [],
                position=position,
            ))
        if rows:
            manager.bulk_create(rows, ignore_conflicts=True)

    def generate_formula_hash(self):
        raw = json.dumps({
            "method": self.method,
            "reference": self.kebs_reference,
            "formula": self.formula,
            "inputs": self.inputs_structure,
            "unit": self.unit,
            "precision": self.rounding_precision,
            "rounding_mode": self.rounding_mode,
            "version": self.version,
        }, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    @transaction.atomic
    def save(self, *args, **kwargs):
        previous = None
        previous_pk = self.pk
        versioned_fields = (
            "method", "kebs_reference", "formula", "inputs_structure", "unit",
            "rounding_precision", "rounding_mode", "description", "category", "tags", "version",
        )
        if previous_pk:
            previous = type(self).objects.filter(pk=previous_pk).first()
        if previous is not None and any(
            getattr(previous, field) != getattr(self, field)
            for field in versioned_fields
        ):
            latest_version = type(self).objects.filter(method=self.method).aggregate(value=Max("version"))["value"] or 0
            if previous.method == self.method:
                next_version = max(latest_version, previous.version) + 1
            else:
                next_version = latest_version + 1
            type(self).objects.filter(pk=previous_pk).update(is_active=False)
            self.pk = uuid.uuid4()
            self._state.adding = True
            self.version = next_version
            kwargs.pop("update_fields", None)
        elif self._state.adding:
            latest_version = type(self).objects.filter(method=self.method).aggregate(value=Max("version"))["value"] or 0
            if self.version <= latest_version:
                self.version = latest_version + 1
        self.formula_hash = self.generate_formula_hash()
        self.full_clean()
        super().save(*args, **kwargs)
        self._sync_analysis_inputs()
        if previous is not None and self.pk != previous_pk:
            # The method change created a new immutable version. Point each
            # current specification at it by creating a new specification
            # version too; requests already captured against the old
            # specification continue to use their original method snapshot.
            specification_model = apps.get_model("ca", "ChemicalSpecification")
            active_specifications = specification_model.objects.filter(
                method_id=previous_pk,
                is_active=True,
            )
            for specification in active_specifications.iterator():
                specification.method = self
                specification.save()

    def delete(self, *args, **kwargs):
        raise ValidationError("Chemical analysis method versions cannot be deleted.")

    def evaluate(self, input_data: dict) -> Decimal | None:
        from ca.services.formulas import safe_eval_formula
        from ca.models import ChemicalAnalysisInput

        self.clean()
        if not (self.formula or "").strip():
            raise ValidationError("This analysis method has no calculation formula.")
        ChemicalAnalysisInput.validate_values(self, input_data)
        formula = self.formula.strip()
        tree = ast.parse(formula, mode="eval")
        formula_fields = {
            node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id != "math"
        }
        safe_context = {"math": math}
        for key in formula_fields:
            try:
                value = Decimal(str(input_data[key]))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValidationError({key: "Enter a valid numeric measurement."}) from exc
            if not value.is_finite():
                raise ValidationError({key: "Measurements must be finite numbers."})
            safe_context[key] = value
        result = safe_eval_formula(formula, safe_context)
        rounding_modes = {
            "HALF_UP": ROUND_HALF_UP,
            "HALF_EVEN": ROUND_HALF_EVEN,
            "DOWN": ROUND_DOWN,
            "UP": ROUND_UP,
        }
        quantum = Decimal(1).scaleb(-self.rounding_precision)
        return result.quantize(quantum, rounding=rounding_modes[self.rounding_mode])

    def example_input_payload(self):
        return {name: 0.0 for name in self.get_input_fields()}

    def describe_inputs(self):
        descriptions = []
        for definition in self.get_input_definitions():
            if isinstance(definition, str):
                name = definition
                label = definition.replace("_", " ").title()
            elif isinstance(definition, dict) and definition.get("name"):
                name = definition["name"]
                label = definition.get("label") or str(name).replace("_", " ").title()
            else:
                continue
            descriptions.append(f"{label} ({name})")
        return ", ".join(descriptions)

    def as_dict(self):
        return {
            "id": str(self.id),
            "method": self.method,
            "kebs_reference": self.kebs_reference,
            "description": self.description,
            "unit": self.unit,
            "inputs_structure": self.inputs_structure,
            "formula": self.formula,
            "rounding_precision": self.rounding_precision,
            "rounding_mode": self.rounding_mode,
            "formula_hash": self.formula_hash,
            "example_inputs": self.example_input_payload(),
            "version": self.version,
            "category": self.category,
            "tags": self.tags,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

# -------------------------
# Microbial Method
# -------------------------
class MicrobialMethodStatus(models.TextChoices):
    ACTIVE = 'ACTIVE', _('Active')
    DEPRECATED = 'DEPRECATED', _('Deprecated')
    UNDER_REVIEW = 'UNDER_REVIEW', _('Under Review')

class MicrobialMethod(AbstractBaseMethod):
    name = models.CharField(max_length=150)
    kebs_reference = models.CharField(max_length=255, help_text="Official KEBS reference standard")
    description = models.TextField()
    incubation_time = models.DurationField(help_text="Incubation time (hh:mm:ss)")
    confirmatory_tests = models.TextField(help_text="Comma-separated confirmatory tests")
    status = models.CharField(
        max_length=20,
        choices=MicrobialMethodStatus.choices,
        default=MicrobialMethodStatus.ACTIVE
    )
    change_log = models.TextField(blank=True)

    class Meta:
        verbose_name = "Microbial Method"
        verbose_name_plural = "Microbial Methods"
        unique_together = ("name", "version")

    def __str__(self):
        return f"{self.name} (v{self.version})"

    def get_incubation_hours(self):
        return round(self.incubation_time.total_seconds() / 3600, 2)

    def get_confirmatory_tests_list(self):
        return [test.strip() for test in self.confirmatory_tests.split(',') if test.strip()]

    def increment_version(self, user, changelog=""):
        self.version += 1
        self.change_log = changelog
        self.updated_by = user
        self.save()

    @property
    def is_deprecated(self):
        return self.status == MicrobialMethodStatus.DEPRECATED

# -------------------------
# WasteWater Analysis Method
# -------------------------
class WWAnalysisMethod(AbstractBaseMethod):
    name = models.CharField(max_length=255, unique=True)
    reference = models.CharField(max_length=255, help_text="Official reference standard")

    class Meta:
        verbose_name = "WasteWater Analysis Method"
        verbose_name_plural = "WasteWater Analysis Methods"

    def __str__(self):
        return f"{self.name} (v{self.version})"

    def as_dict(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "reference": self.reference,
            "version": self.version,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

# -------------------------
# CleanWater Analysis Method
# -------------------------
class CWAnalysisMethod(AbstractBaseMethod):
    name = models.CharField(max_length=255, unique=True)
    reference = models.CharField(max_length=255, help_text="Official reference standard")

    class Meta:
        verbose_name = "CleanWater Analysis Method"
        verbose_name_plural = "CleanWater Analysis Methods"

    def __str__(self):
        return f"{self.name} (v{self.version})"

    def as_dict(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "reference": self.reference,
            "version": self.version,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

# -------------------------
# Utility: Formula Validator (Placeholder)
# -------------------------
def validate_formula_expression(expr):
    from ca.models import validate_formula_expression as validate_chemical_formula

    validate_chemical_formula(expr, allow_math_attr=True)
    return True
