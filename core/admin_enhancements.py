from __future__ import annotations

from functools import wraps
from pathlib import Path

from django.apps import apps
from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.admin.options import ModelAdmin
from django.contrib.admin.sites import AlreadyRegistered
from django.core.exceptions import FieldDoesNotExist
from django.db import DatabaseError, transaction


SENSITIVE_FIELD_PARTS = (
    "password",
    "secret",
    "token",
    "credential",
    "salt",
    "private_key",
    "api_key",
)

IDENTIFIER_PARTS = (
    "code",
    "reference",
    "number",
    "name",
    "title",
    "batch",
    "sample",
    "plant",
    "status",
    "state",
    "result",
    "created",
    "updated",
    "date",
    "active",
)

AUDIT_FIELD_NAMES = {
    "created_at",
    "updated_at",
    "created_on",
    "updated_on",
    "modified_at",
    "modified_on",
    "date_created",
    "date_updated",
}


def _is_sensitive(field) -> bool:
    name = field.name.lower()
    return any(part in name for part in SENSITIVE_FIELD_PARTS)


def _model_fields(model):
    return [
        field
        for field in model._meta.get_fields()
        if getattr(field, "concrete", False)
        and not getattr(field, "auto_created", False)
        and not _is_sensitive(field)
    ]


def _field_score(field) -> tuple[int, int]:
    name = field.name.lower()
    priority = next(
        (index for index, part in enumerate(IDENTIFIER_PARTS) if part in name),
        len(IDENTIFIER_PARTS),
    )
    displayable = field.is_relation or field.get_internal_type() in {
        "CharField",
        "TextField",
        "BooleanField",
        "DateField",
        "DateTimeField",
        "DecimalField",
        "IntegerField",
        "PositiveIntegerField",
        "FloatField",
        "DurationField",
    }
    return (priority if displayable else len(IDENTIFIER_PARTS) + 1, field.creation_counter)


def _display_fields(model, limit=5):
    candidates = sorted(_model_fields(model), key=_field_score)
    selected = []
    for field in candidates:
        if not field.editable and not field.is_relation:
            continue
        if field.primary_key or field.name in AUDIT_FIELD_NAMES:
            continue
        if field.many_to_many or field.one_to_many:
            continue
        if field.get_internal_type() in {"BinaryField", "JSONField"}:
            continue
        selected.append(field.name)
        if len(selected) == limit:
            break
    return selected


def _default_filters(model, limit=3):
    fields = _model_fields(model)
    ranked = []
    for field in fields:
        if field.many_to_many or field.one_to_many or field.primary_key:
            continue
        kind = field.get_internal_type()
        if getattr(field, "choices", None):
            score = 0
        elif kind in {"BooleanField", "NullBooleanField"}:
            score = 1
        elif kind in {"DateField", "DateTimeField"}:
            score = 2
        else:
            continue
        ranked.append((score, field.name))
    ranked.sort()
    return [name for _, name in ranked[:limit]]


def _default_search_fields(model, limit=5):
    fields = []
    for field in _model_fields(model):
        if field.many_to_many or field.one_to_many or field.is_relation:
            continue
        if field.get_internal_type() not in {"CharField", "EmailField", "SlugField"}:
            continue
        fields.append(field)
    fields.sort(key=_field_score)
    return [f"^{field.name}" for field in fields[:limit]]


def _relation_fields(model, display):
    related = []
    for entry in display:
        if not isinstance(entry, str) or entry.startswith("__"):
            continue
        root = entry.split("__", 1)[0]
        try:
            field = model._meta.get_field(root)
        except FieldDoesNotExist:
            continue
        if field.is_relation and (field.many_to_one or field.one_to_one):
            related.append(root)
    return related[:5]


class EnhancedAdminMixin:
    def admin_record(self, obj):
        value = str(obj)
        return value if len(value) <= 96 else f"{value[:93]}…"

    admin_record.short_description = "Record"
    admin_record.admin_order_field = "pk"

    def get_list_display(self, request):
        current = tuple(super().get_list_display(request))
        if current != ("__str__",):
            return current
        return ("admin_record", *_display_fields(self.model))

    def get_list_filter(self, request):
        current = tuple(super().get_list_filter(request) or ())
        if current:
            return current
        return tuple(_default_filters(self.model))

    def get_search_fields(self, request):
        current = tuple(super().get_search_fields(request) or ())
        if current:
            return current
        return tuple(_default_search_fields(self.model))

    def get_list_select_related(self, request):
        current = super().get_list_select_related(request)
        if current is True:
            return True
        names = list(current or ())
        for name in _relation_fields(self.model, self.get_list_display(request)):
            if name not in names:
                names.append(name)
        return tuple(names)

    def get_readonly_fields(self, request, obj=None):
        current = tuple(super().get_readonly_fields(request, obj))
        fields = {
            field.name: field
            for field in self.model._meta.concrete_fields
        }
        audit_fields = [
            name
            for name in AUDIT_FIELD_NAMES
            if name in fields
            and name not in current
            and (
                getattr(fields[name], "auto_now", False)
                or getattr(fields[name], "auto_now_add", False)
                or not fields[name].editable
            )
        ]
        return (*current, *sorted(audit_fields))


class EnhancedModelAdmin(EnhancedAdminMixin, admin.ModelAdmin):
    list_per_page = 50
    list_max_show_all = 200
    show_full_result_count = False
    show_facets = admin.ShowFacets.NEVER


def _enhanced_admin_class(original):
    if issubclass(original, EnhancedAdminMixin):
        return original

    attributes = {
        "__module__": original.__module__,
        "__doc__": original.__doc__,
        "_project_enhanced_admin": True,
    }
    custom_mro = [
        cls
        for cls in original.mro()
        if cls not in {admin.ModelAdmin, ModelAdmin, object}
    ]
    if not any("list_per_page" in cls.__dict__ for cls in custom_mro):
        attributes["list_per_page"] = 50
    if not any("list_max_show_all" in cls.__dict__ for cls in custom_mro):
        attributes["list_max_show_all"] = 200
    if not any("show_full_result_count" in cls.__dict__ for cls in custom_mro):
        attributes["show_full_result_count"] = False
    if hasattr(admin, "ShowFacets") and not any(
        "show_facets" in cls.__dict__ for cls in custom_mro
    ):
        attributes["show_facets"] = admin.ShowFacets.NEVER
    return type(
        f"Enhanced{original.__name__}",
        (EnhancedAdminMixin, original),
        attributes,
    )


def _configure_site(site):
    site.site_header = "NCWSC · Water Quality Management"
    site.site_title = "Water Quality Administration"
    site.index_title = "Operations & data administration"
    site.empty_value_display = "—"

    original_index = site.index
    if getattr(original_index, "_project_admin_index_handler", False):
        return

    @wraps(original_index)
    def index(request, extra_context=None):
        response = original_index(request, extra_context=extra_context)
        context = getattr(response, "context_data", None)
        if not isinstance(context, dict):
            return response
        app_list = context.get("app_list", ())
        models = [model for app in app_list for model in app.get("models", ())]
        context["admin_overview"] = {
            "apps": len(app_list),
            "models": len(models),
            "addable": sum(bool(model.get("add_url")) for model in models),
        }
        try:
            with transaction.atomic():
                recent = LogEntry.objects.select_related("user", "content_type")
                if not request.user.has_perm("admin.view_logentry"):
                    recent = recent.filter(user=request.user)
                context["recent_admin_actions"] = list(
                    recent.order_by("-action_time")[:8]
                )
        except DatabaseError:
            context["recent_admin_actions"] = ()
        return response

    index._project_admin_index_handler = True
    site.index = index


def register_app_models(package_or_label, site=None):
    """Apply the shared admin and register any remaining local app models."""
    site = site or admin.site
    _configure_site(site)
    candidate = str(package_or_label or "").strip()
    config = next(
        (
            item
            for item in apps.get_app_configs()
            if item.name == candidate
            or item.label == candidate
            or item.name.lower() == candidate.lower()
            or item.label.lower() == candidate.lower()
        ),
        None,
    )
    if config is None:
        return

    models = [
        model
        for model in config.get_models()
        if not model._meta.abstract and model._meta.managed
    ]
    _enhance_and_register(models, site)


def _enhance_and_register(models, site):
    missing = []
    for model in models:
        model_admin = site._registry.get(model)
        if model_admin is None:
            missing.append(model)
            continue
        if isinstance(model_admin, EnhancedAdminMixin):
            continue
        original = model_admin.__class__
        enhanced = _enhanced_admin_class(original)
        if enhanced is original:
            continue
        try:
            model_admin.__class__ = enhanced
        except TypeError:
            site.unregister(model)
            site.register(model, enhanced)
    if missing:
        try:
            site.register(tuple(missing), EnhancedModelAdmin)
        except AlreadyRegistered:
            _enhance_and_register(missing, site)


def install_admin_enhancements(site=None):
    _configure_site(site or admin.site)


def register_local_admin_models(base_path, site=None):
    """Enhance and register models belonging to this project."""
    base = Path(base_path).resolve()
    local_models = []
    for config in apps.get_app_configs():
        try:
            Path(config.path).resolve().relative_to(base)
        except (OSError, ValueError):
            continue
        local_models.extend(
            model
            for model in config.get_models()
            if not model._meta.abstract and model._meta.managed
        )
    _enhance_and_register(local_models, site or admin.site)
