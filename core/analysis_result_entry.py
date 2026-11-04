from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from core.dashboard_result_cards import _can_view, _resolve_plant_scope
from core.mixins import WanjalaMixin


LOCKED_ANALYSIS_STATES = {"verified", "confirmed", "approved"}


def _analysis_specs(specs):
    return list(specs.values()) if isinstance(specs, dict) else list(specs)


def _path_value(instance, path):
    value = instance
    for part in (path or "").split("__"):
        value = getattr(value, part, None) if value is not None else None
    return value


def _has_permission(user, model, action):
    return bool(user.is_superuser or user.has_perm(
        f"{model._meta.app_label}.{action}_{model._meta.model_name}"
    ))


def _display(value):
    if value is None or value == "":
        return "—"
    if isinstance(value, Decimal):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return str(value)


def _result_specs(spec):
    configured = spec.get("result_specs")
    if configured:
        return configured
    if spec.get("result_model"):
        return [{
            "model": spec["result_model"],
            "value_field": "result",
            "parameter_name_path": "parameter__name",
            "unit_path": "parameter__unit",
        }]
    return []


def build_analysis_entry_groups(request, app_label, spec, analysis, plant=None):
    view = AnalysisResultEntryView()
    view.app_label = app_label
    fields = {field.name for field in analysis._meta.fields}
    status_field = spec.get("status_field") or ("status" if "status" in fields else "state")
    status_value = str(getattr(analysis, status_field, "NEW") or "NEW")
    locked = status_value.casefold() in LOCKED_ANALYSIS_STATES
    groups, can_save = view._build_groups(request, spec, analysis, plant, status_value, locked)
    return groups, bool(can_save and not locked)


def _parameter_details(parameter, spec):
    configured_name = _path_value(parameter, spec.get("parameter_name_path"))
    configured_unit = _path_value(parameter, spec.get("unit_path"))
    nested = getattr(parameter, "parameter", None)
    display_parameter = nested or parameter
    name = configured_name or getattr(display_parameter, "name", None) or str(parameter)
    unit = configured_unit or getattr(display_parameter, "unit", "") or ""
    minimum = _path_value(parameter, spec.get("minimum_path")) if spec.get("minimum_path") else None
    maximum = _path_value(parameter, spec.get("maximum_path")) if spec.get("maximum_path") else None
    minimum = minimum if minimum is not None else getattr(parameter, "min_value", None)
    maximum = maximum if maximum is not None else getattr(parameter, "max_value", None)
    if minimum is None:
        minimum = getattr(display_parameter, "min_standard", None)
    if maximum is None:
        maximum = getattr(display_parameter, "max_standard", None)
    if minimum is not None and maximum is not None:
        limit = f"{_display(minimum)} – {_display(maximum)}{f' {unit}' if unit else ''}"
    elif minimum is not None:
        limit = f"≥ {_display(minimum)}{f' {unit}' if unit else ''}"
    elif maximum is not None:
        limit = f"≤ {_display(maximum)}{f' {unit}' if unit else ''}"
    else:
        limit = "No configured range"
    return str(name), str(unit), limit


class AnalysisResultEntryView(WanjalaMixin, View):
    app_label = None
    analysis_specs = ()
    template_name = "core/components/analysis_result_entry.html"

    def get(self, request, analysis_model, analysis_id):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        return self._render(request, analysis_model, analysis_id)

    def post(self, request, analysis_model, analysis_id):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        return self._render(request, analysis_model, analysis_id, save=True)

    def _resolve_analysis(self, request, analysis_model, analysis_id):
        spec = next((item for item in _analysis_specs(self.analysis_specs)
                     if item.get("model", "").lower() == analysis_model.lower()), None)
        if not spec:
            raise PermissionDenied
        Analysis = apps.get_model(self.app_label, spec["model"])
        if not _can_view(request.user, Analysis):
            raise PermissionDenied
        queryset = Analysis.objects.all()
        field_names = {field.name for field in Analysis._meta.fields}
        if "is_deleted" in field_names:
            queryset = queryset.filter(is_deleted=False)
        plant_path = spec.get("plant_path")
        plant_scope = _resolve_plant_scope(request)
        plants = plant_scope["available_plants"]
        selected_plant = plant_scope["selected_plant"]
        if plant_path:
            if request.user.is_superuser:
                requested_plant = request.GET.get("plant") or request.POST.get("plant")
                if requested_plant and requested_plant.isdigit():
                    queryset = queryset.filter(**{plant_path: int(requested_plant)})
            elif selected_plant:
                queryset = queryset.filter(**{plant_path: selected_plant.pk})
            else:
                queryset = queryset.none()
        analysis = get_object_or_404(queryset, pk=analysis_id)
        plant = _path_value(analysis, plant_path) if plant_path else None
        status_field = spec.get("status_field") or ("status" if "status" in field_names else "state")
        status_value = str(getattr(analysis, status_field, "NEW") or "NEW")
        locked = status_value.casefold() in LOCKED_ANALYSIS_STATES
        return spec, Analysis, analysis, plant, status_field, status_value, locked, plants

    def _method_options(self, Result, row_spec, current_method=None):
        method_field = row_spec.get("method_field", "analysis_method")
        if method_field not in {field.name for field in Result._meta.fields}:
            return [], None
        Method = Result._meta.get_field(method_field).related_model
        queryset = Method.objects.all()
        method_fields = {field.name for field in Method._meta.fields}
        if "is_active" in method_fields:
            queryset = queryset.filter(is_active=True)
        ordering = ["-updated_at", "pk"] if "updated_at" in method_fields else ["pk"]
        options = list(queryset.order_by(*ordering))
        if current_method and current_method.pk not in {option.pk for option in options}:
            options.append(current_method)
        default = current_method or (options[0] if options else None)
        return options, default

    def _parameter_options(self, Result, row_spec, plant):
        parameter_field = row_spec.get("parameter_field", "parameter")
        Parameter = Result._meta.get_field(parameter_field).related_model
        queryset = Parameter.objects.all()
        parameter_fields = {field.name for field in Parameter._meta.fields}
        if "is_active" in parameter_fields:
            queryset = queryset.filter(is_active=True)
        if plant and "treatment_plant" in parameter_fields:
            queryset = queryset.filter(treatment_plant_id=plant.pk)
        if "parameter" in parameter_fields:
            queryset = queryset.select_related("parameter")
        return list(queryset.order_by("pk"))

    def _build_groups(self, request, spec, analysis, plant, status_value, locked, post_values=None, errors=None):
        groups = []
        can_save = False
        for row_spec in _result_specs(spec):
            Result = apps.get_model(row_spec.get("app_label", self.app_label), row_spec["model"])
            fields = {field.name for field in Result._meta.fields}
            value_field = row_spec.get("value_field", "result" if "result" in fields else "value")
            can_add = _has_permission(request.user, Result, "add")
            can_change = _has_permission(request.user, Result, "change")
            parameter_field = row_spec.get("parameter_field", "parameter")
            related_fields = [parameter_field]
            if parameter_field in fields and "parameter" in {
                field.name for field in Result._meta.get_field(parameter_field).related_model._meta.fields
            }:
                related_fields.append(f"{parameter_field}__parameter")
            method_field = row_spec.get("method_field", "analysis_method")
            if method_field in fields:
                related_fields.append(method_field)
            related_fields = [name for name in related_fields if name in fields or "__" in name]
            result_queryset = Result.objects.filter(analysis_id=analysis.pk)
            if row_spec.get("read_only") and row_spec.get("include_filter") is not None:
                result_queryset = result_queryset.filter(row_spec["include_filter"])
            result_rows = list(result_queryset.select_related(*related_fields).order_by(parameter_field, "pk"))
            by_parameter = {getattr(item, f"{parameter_field}_id"): item for item in result_rows}
            parameters = self._parameter_options(Result, row_spec, plant)
            for item in result_rows:
                parameter_id = getattr(item, f"{parameter_field}_id")
                if parameter_id not in {parameter.pk for parameter in parameters}:
                    parameters.append(getattr(item, parameter_field))
            group_rows = []
            parameter_by_id = {str(parameter.pk): parameter for parameter in parameters}
            for parameter in parameters:
                item = by_parameter.get(parameter.pk)
                is_deleted = bool(getattr(item, "is_deleted", False)) if item else False
                manually_editable = getattr(item, "manually_editable", True) if item else True
                is_new = item is None
                allowed = (can_add if is_new else can_change) and not locked and not is_deleted
                if is_new and not row_spec.get("allow_create", True):
                    allowed = False
                if row_spec.get("read_only", False):
                    allowed = False
                if not manually_editable:
                    allowed = False
                current_method = getattr(item, method_field, None) if item and method_field in fields else None
                methods, selected_method = self._method_options(Result, row_spec, current_method)
                if method_field in fields and not selected_method:
                    allowed = False
                name, unit, limit = _parameter_details(parameter, row_spec)
                input_key = f"{Result._meta.model_name}_{parameter.pk}"
                value = getattr(item, value_field, None) if item else None
                entry_status = str(getattr(item, "entry_status", "") or "") if item else ""
                if entry_status == "pending":
                    value = None
                group_rows.append({
                    "input_key": input_key,
                    "parameter_id": str(parameter.pk),
                    "parameter": name,
                    "unit": unit,
                    "limit": limit,
                    "value": (post_values or {}).get(input_key, value),
                    "value_display": _display(value),
                    "entry_status": entry_status,
                    "status_label": item.get_entry_status_display() if item and hasattr(item, "get_entry_status_display") else (entry_status.replace("_", " ").title() if entry_status else "Not recorded"),
                    "editable": allowed,
                    "is_new": is_new,
                    "is_deleted": is_deleted,
                    "can_save": allowed,
                    "error": (errors or {}).get(input_key),
                    "method_field": method_field if method_field in fields else "",
                    "method_options": [{"id": str(method.pk), "label": str(method)} for method in methods],
                    "selected_method_id": str(selected_method.pk) if selected_method else "",
                    "has_method": method_field in fields,
                    "result_model": Result._meta.model_name,
                    "value_field": value_field,
                    "_result_model_class": Result,
                    "_parameter_object": parameter_by_id.get(str(parameter.pk), parameter),
                    "_existing_object": item,
                    "_row_spec": row_spec,
                })
                can_save = can_save or allowed
            label = row_spec.get("title", row_spec.get("title_prefix", "").strip(" ·") or Result._meta.verbose_name_plural.title())
            groups.append({"label": label, "rows": group_rows, "model": Result._meta.model_name})
        return groups, can_save

    def _save_results(self, request, analysis, groups):
        pending = []
        errors = {}
        for group in groups:
            for row in group["rows"]:
                raw_value = request.POST.get(f"result_{row['input_key']}", "").strip()
                if not raw_value:
                    continue
                if not row["editable"]:
                    errors[row["input_key"]] = "This result is read-only for the current workflow state or permissions."
                    continue
                Result = row["_result_model_class"]
                value_field = Result._meta.get_field(row["value_field"])
                try:
                    cleaned_value = value_field.clean(raw_value, None)
                    if isinstance(cleaned_value, float) and (cleaned_value != cleaned_value or abs(cleaned_value) == float("inf")):
                        raise ValidationError("Enter a finite numeric result.")
                    if isinstance(cleaned_value, Decimal) and not cleaned_value.is_finite():
                        raise ValidationError("Enter a finite numeric result.")
                except (ValidationError, InvalidOperation, TypeError, ValueError):
                    errors[row["input_key"]] = "Enter a valid numeric result."
                    continue
                method = None
                if row["has_method"]:
                    method_field = row["method_field"]
                    Method = Result._meta.get_field(method_field).related_model
                    method_id = request.POST.get(f"method_{row['input_key']}")
                    existing = row["_existing_object"]
                    existing_method = getattr(existing, method_field, None) if existing else None
                    if existing_method and (not method_id or str(existing_method.pk) == method_id):
                        method = existing_method
                    elif method_id:
                        methods, _default = self._method_options(Result, row["_row_spec"])
                        method = next((option for option in methods if str(option.pk) == method_id), None)
                    if not method:
                        errors[row["input_key"]] = "Choose an active analysis method before saving."
                        continue
                pending.append((row, cleaned_value, method))
        if errors:
            return 0, errors
        if not pending:
            return 0, {}

        saved = 0
        with transaction.atomic():
            for row, value, method in pending:
                Result = row["_result_model_class"]
                row_spec = row["_row_spec"]
                parameter_field = row_spec.get("parameter_field", "parameter")
                parameter = row["_parameter_object"]
                existing = row["_existing_object"]
                if existing:
                    item = Result.objects.select_for_update().get(pk=existing.pk, analysis_id=analysis.pk)
                    setattr(item, row["value_field"], value)
                    if method and row["has_method"]:
                        setattr(item, row["method_field"], method)
                    if "entry_status" in {field.name for field in Result._meta.fields}:
                        setattr(item, "entry_status", "recorded")
                        item.save(update_fields=(row["value_field"], "entry_status"))
                    else:
                        item.save()
                else:
                    create_values = {
                        "analysis": analysis,
                        parameter_field: parameter,
                        row["value_field"]: value,
                    }
                    if method and row["has_method"]:
                        create_values[row["method_field"]] = method
                    if "entry_status" in {field.name for field in Result._meta.fields}:
                        create_values["entry_status"] = "recorded"
                    if "manually_editable" in {field.name for field in Result._meta.fields}:
                        create_values["manually_editable"] = True
                    item = Result(**create_values)
                    item.save()
                saved += 1

            analysis_fields = {field.name for field in analysis._meta.fields}
            update_fields = []
            if "analysed_by" in analysis_fields and getattr(analysis, "analysed_by_id", None) is None:
                analysis.analysed_by = request.user
                update_fields.append("analysed_by")
            if "analysed_at" in analysis_fields and getattr(analysis, "analysed_at", None) is None:
                analysis.analysed_at = timezone.now()
                update_fields.append("analysed_at")
            if update_fields:
                analysis.save(update_fields=update_fields)
        return saved, {}

    def _render(self, request, analysis_model, analysis_id, *, save=False):
        spec, Analysis, analysis, plant, status_field, status_value, locked, plants = self._resolve_analysis(
            request, analysis_model, analysis_id
        )
        groups, can_save = self._build_groups(request, spec, analysis, plant, status_value, locked)
        post_values = {}
        errors = {}
        if save:
            post_values = {
                key.removeprefix("result_"): value
                for key, value in request.POST.items()
                if key.startswith("result_")
            }
            saved, errors = self._save_results(request, analysis, groups)
            if errors:
                groups, can_save = self._build_groups(request, spec, analysis, plant, status_value, locked, post_values, errors)
                messages.error(request, "Review the highlighted result entries and try again.")
            elif saved:
                messages.success(request, f"{saved} result{'s' if saved != 1 else ''} saved for {analysis}.")
                return_to = request.POST.get("return_to", "").strip()
                if return_to and url_has_allowed_host_and_scheme(
                    return_to,
                    allowed_hosts={request.get_host()},
                    require_https=request.is_secure(),
                ):
                    return redirect(return_to)
                return redirect(request.get_full_path())
            else:
                messages.info(request, "Enter at least one result value to save.")
                groups, can_save = self._build_groups(request, spec, analysis, plant, status_value, locked, post_values)

        sample = getattr(analysis, "sample", None)
        reference = getattr(sample, "reference_id", None) or getattr(analysis, "reference_id", None) or str(analysis.pk)
        query = request.GET.copy()
        if plant and "plant" not in query:
            query["plant"] = str(plant.pk)
        dashboard_url = f"/{self.app_label}/"
        cards_url = f"/{self.app_label}/results/cards/"
        return render(request, self.template_name, {
            "base_template": f"{self.app_label}/base.html",
            "app_label": self.app_label,
            "dashboard_title": spec.get("title", self.app_label.replace("_", " ").title()),
            "analysis": analysis,
            "analysis_reference": reference,
            "analysis_status": analysis.get_status_display() if hasattr(analysis, "get_status_display") else analysis.get_state_display() if hasattr(analysis, "get_state_display") else status_value.replace("_", " ").title(),
            "analysis_locked": locked,
            "result_groups": groups,
            "can_save_results": can_save and not locked,
            "selected_plant": plant,
            "show_plant_selector": request.user.is_superuser,
            "plant_options": plants,
            "dashboard_url": dashboard_url,
            "cards_url": f"{cards_url}?{query.urlencode()}" if query else cards_url,
        })
