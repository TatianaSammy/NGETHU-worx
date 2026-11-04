#core/registry.py
import logging
import copy
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from collections import OrderedDict, defaultdict
from typing import (Dict, Any, Type, Optional, Callable, Protocol, runtime_checkable, Tuple, List, Iterable)
from django.shortcuts import render
from django import forms
from django.db import models
from django.core.exceptions import ImproperlyConfigured
logger = logging.getLogger(__name__)
MODEL_FORM_MAP = {
    models.CharField: forms.CharField,
    models.TextField: forms.CharField,
    models.IntegerField: forms.IntegerField,
    models.BigIntegerField: forms.IntegerField,
    models.BooleanField: forms.BooleanField,
    models.DateField: forms.DateField,
    models.DateTimeField: forms.DateTimeField,
    models.DecimalField: forms.DecimalField,
    models.FloatField: forms.FloatField,
    models.EmailField: forms.EmailField,
    models.JSONField: forms.JSONField,
}
@runtime_checkable
class ActionHandler(Protocol):
    def __call__(self, context: dict) -> Any: ...
@runtime_checkable
class LifecycleHook(Protocol):
    def __call__(self, context: dict, instance=None) -> Any: ...
@runtime_checkable
class PolicyResolver(Protocol):
    def __call__(self, request, action: str, entry: "RegistryEntry") -> bool: ...
@runtime_checkable
class QueryResolver(Protocol):
    def __call__(self, model, request, config: dict) -> Any: ...
@dataclass
class FieldSchema:
    name: str
    verbose: str
    kind: str                  # text | fk | date | datetime | bool | choices | m2m
    type: str                  # django type string
    ui: Dict[str, Any] = field(default_factory=dict)
@dataclass
class ActionSchema:
    view: Optional[str] = None
    create: Optional[str] = None
    update: Optional[str] = None
    delete: Optional[str] = None
    export: Optional[str] = None
    custom: Dict[str, str] = field(default_factory=dict)
@dataclass
class RowSchema:
    id: Any
    repr: str
    fields: Dict[str, Dict[str, Any]]
    state: str = "view"
    selectable: bool = True
    editable: bool = True
    actions: List[str] = field(default_factory=lambda: ["view", "edit", "delete"])
    relations: Dict[str, Any] = field(default_factory=lambda: {
        "fk": [],
        "reverse": []
    })
    ui: Dict[str, Any] = field(default_factory=lambda: {
        "inline_edit": True,
        "hydrated": False,
        "row_template": "core/row/dynamic_row.html"
    })
@dataclass
class TableSchema:
    model: str
    app: str
    fields: List[FieldSchema]
    rows: List[RowSchema]
    meta: Dict[str, Any] = field(default_factory=dict)
    ui: Dict[str, Any] = field(default_factory=lambda: {
        "search": True,
        "sort": True,
        "bulk_actions": True,
        "export": True,
        "lazy_fk": True
    })
@runtime_checkable
class RenderResolver(Protocol):
    def __call__(self, request, context: dict, entry: "RegistryEntry") -> Any: ...
from dataclasses import dataclass, field
@dataclass(slots=True, frozen=True)
class RegistryEntry:
    model: Type[models.Model]
    config: MappingProxyType
    meta: MappingProxyType
    custom_actions: MappingProxyType
    export_handler: Optional[ActionHandler] = None
    inline_edit_handler: Optional[ActionHandler] = None
    transition_handler: Optional[ActionHandler] = None
    before_create: Optional[LifecycleHook] = None
    after_create: Optional[LifecycleHook] = None
    before_update: Optional[LifecycleHook] = None
    after_update: Optional[LifecycleHook] = None
    before_delete: Optional[LifecycleHook] = None
    after_delete: Optional[LifecycleHook] = None
    policy: Optional[PolicyResolver] = None
    queryset: Optional[QueryResolver] = None
    renderer: Optional[RenderResolver] = None
    middleware: Tuple = field(default_factory=tuple)
    validators: Tuple = field(default_factory=tuple)
    tags: frozenset = field(default_factory=frozenset)
    row_schema: Optional[RowSchema] = None
    def __post_init__(self):
        if not isinstance(self.config, MappingProxyType):
            raise TypeError("config must be MappingProxyType")
        if not isinstance(self.meta, MappingProxyType):
            raise TypeError("meta must be MappingProxyType")
        if not isinstance(self.custom_actions, MappingProxyType):
            raise TypeError("custom_actions must be MappingProxyType")
        for name, handler in [
            ("export_handler", self.export_handler),
            ("inline_edit_handler", self.inline_edit_handler),
            ("transition_handler", self.transition_handler),
        ]:
            if handler and not callable(handler):
                raise TypeError(f"{name} must be callable")

        if self.policy and not callable(self.policy):
            raise TypeError("policy must be callable")

        if self.queryset and not callable(self.queryset):
            raise TypeError("queryset must be callable")

        if self.renderer and not callable(self.renderer):
            raise TypeError("renderer must be callable")
class ModelRegistry:
    def __init__(self):
        self._registry: "OrderedDict[str, RegistryEntry]" = OrderedDict()
        self._runtime: dict[str, dict] = defaultdict(dict)
        self._listeners: dict[str, list[Callable]] = defaultdict(list)
        self._cache: dict[Type[models.Model], str] = {}
    def get_fields_schema(self, model):
        entry = self.get(model)
        if not entry:
            raise ImproperlyConfigured("Model not registered")
        model_cls = entry.model
        fields = [
            self.inspect_field(model_cls, f)
            for f in model_cls._meta.fields
            if not f.primary_key
        ]

        return {
            "model": f"{model_cls._meta.app_label}.{model_cls._meta.model_name}",
            "fields": fields
        }
    def on(self, event: str, fn: Callable):
        self._listeners[event].append(fn)
    def emit(self, event: str, payload: dict):
        for fn in tuple(self._listeners.get(event, [])):
            try:
                fn(payload)
            except Exception:
                logger.exception(f"[Registry] event failed: {event}")
    def _resolve_template(self, action: str, mode: str):
        base = {
            "list": "core/list/dynamic_list",
            "detail": "core/detail/dynamic_detail",
            "create": "core/form/dynamic_form",
            "update": "core/form/dynamic_form",
            "delete": "core/confirm/dynamic_delete",
            "inline_edit": "core/row/dynamic_row",
        }.get(action, "core/fallback/dynamic_unknown")
        return f"{base}.html"
    def render_template(self, request, model, action: str, context=None, mode="page"):
        context = context or {}
        entry = self.get(model)
        if not entry:
            raise ImproperlyConfigured("Model not registered")
        context.update({
            "entry": entry,
            "meta": entry.meta,
            "config": entry.config,
            "custom_actions": entry.custom_actions,
        })
        if request.headers.get("HX-Request"):
            mode = "partial"
        if entry.renderer:
            result = entry.renderer(request, context, entry)
            if result is None:
                raise ImproperlyConfigured("Custom renderer returned None")
            return result
        template = self._resolve_template(action, mode)
        return render(request, template, context)

    def resolve_object_key(app: str, model: str, pk: Any):
        return f"/{app}/{model}/{pk}/"

    def resolve_row(self, instance):
        entry = self.get(instance.__class__)
        schema = entry.row_schema if entry else None
        fields = {}
        def safe_value(val):
            if val is None:
                return ""
            if isinstance(val, (str, int, float, bool)):
                return val
            return str(val)
        def resolve_fk(val, field):
            if val is None:
                return ""
            if hasattr(val, "__str__"):
                return str(val)
            return str(val)
        for f in instance._meta.fields:
            if f.primary_key:
                continue
            raw_value = getattr(instance, f.name, None)
            field_schema = self.inspect_field(instance.__class__, f)
            if isinstance(f, models.ForeignKey):
                value = resolve_fk(raw_value, f)
            else:
                value = safe_value(raw_value)
            fields[f.name] = {
                "value": value,
                "raw": safe_value(raw_value),
                "kind": field_schema["kind"],
                "type": field_schema["type"],
                "ui": dict(field_schema.get("ui") or {}),
                "related": field_schema.get("related"),}
        row = RowSchema(
            id=instance.pk,
            repr=str(instance),
            fields=fields
        )
        return {
            "id": row.id,
            "repr": row.repr,
            "fields": row.fields,
            "state": row.state,
            "selectable": row.selectable,
            "editable": row.editable,
            "actions": schema.actions if schema and hasattr(schema, "actions")
            else row.actions,
            "relations": row.relations,
            "ui": {
                **row.ui,
                "hydrated": True
            }
        }
    def _key(self, model: Type[models.Model]) -> str:
        if model in self._cache:
            return self._cache[model]
        key = f"{model._meta.app_label}.{model._meta.model_name}"
        self._cache[model] = key
        return key
    def inspect_field(self, model_cls, f):

        info = {
            "name": f.name,
            "verbose": str(f.verbose_name).title(),
            "type": f.get_internal_type(),
            "required": not f.blank,
            "nullable": f.null,
            "editable": getattr(f, "editable", True),
        }
        if isinstance(f, models.ForeignKey):
            rel_model = f.related_model

            info.update({
                "kind": "fk",

                "ui": {
                    "widget": "select",
                    "async": True,
                    "searchable": True,
                    "clearable": True,
                    "placeholder": f"Select {rel_model._meta.verbose_name}",
                },

                "related": {
                    "app": rel_model._meta.app_label,
                    "model": rel_model._meta.model_name,
                },

                "options_endpoint": f"/api/schema/{rel_model._meta.app_label}/{rel_model._meta.model_name}/options/",
                "options_preview": self._serialize_options(rel_model, limit=20),
                "depends_on": getattr(f, "depends_on", None),
            })
        elif isinstance(f, models.ManyToManyField):
            rel_model = f.related_model
            info.update({
                "kind": "m2m",
                "ui": {
                    "widget": "multi_select",
                    "chips": True,
                    "searchable": True,
                    "async": True,
                    "max_visible": 5,
                },
                "related": {
                    "app": rel_model._meta.app_label,
                    "model": rel_model._meta.model_name,
                },

                "options_endpoint": f"/api/schema/{rel_model._meta.app_label}/{rel_model._meta.model_name}/options/",

                "options_preview": self._serialize_options(rel_model, limit=20),
            })

        elif getattr(f, "choices", None):
            info.update({
                "kind": "choices",
                "ui": {
                    "widget": "select",
                    "async": False,
                },
                "choices": [
                    {"value": k, "label": v}
                    for k, v in f.choices
                ]
            })

        elif isinstance(f, models.BooleanField):
            info.update({
                "kind": "boolean",
                "ui": {
                    "widget": "toggle"
                }
            })

        elif isinstance(f, models.DateField):
            info.update({
                "kind": "date",
                "ui": {"widget": "date_picker"}
            })

        elif isinstance(f, models.DateTimeField):
            info.update({
                "kind": "datetime",
                "ui": {"widget": "datetime_picker"}
            })

        else:
            info["kind"] = "field"
            info["ui"] = {"widget": "text"}

        return MappingProxyType(info)
    def parse_key(self, key: str) -> Tuple[str, str]:
        try:
            return tuple(key.split(".", 1))  # type: ignore
        except Exception:
            raise ValueError(f"Invalid key: {key}")

    def register(self, model: Type[models.Model], **config):
        key = self._key(model)

        if key in self._registry:
            logger.warning("Model already registered: %s", key)
            return self._registry[key]

        app = model._meta.app_label
        model_name = model._meta.model_name
        object_name = model.__name__

        normalized = self._normalize(config)

        ui = copy.deepcopy(normalized.get("ui") or {})
        meta_cfg = copy.deepcopy(normalized.get("meta") or {})

        base_label = self._humanize(object_name)

        # ----------------------------------------------------------
        # MODEL INTROSPECTION
        # ----------------------------------------------------------

        model_fields = [
            f.name
            for f in model._meta.fields
        ]

        editable_fields = [
            f.name
            for f in model._meta.fields
            if getattr(f, "editable", True)
        ]

        relations = [
            f.name
            for f in model._meta.fields
            if f.is_relation
        ]

        m2m_fields = [
            f.name
            for f in model._meta.many_to_many
        ]

        # ----------------------------------------------------------
        # ROUTES
        # ----------------------------------------------------------

        routes = {
            "dashboard": f"/{app}/",
            "list": f"/{app}/{model_name}/list/",
            "detail": f"/{app}/{model_name}/<pk>/",
            "create": f"/{app}/{model_name}/create/",
            "update": f"/{app}/{model_name}/<pk>/update/",
            "delete": f"/{app}/{model_name}/<pk>/delete/",
            "export": f"/{app}/{model_name}/export/",
            "import": f"/{app}/{model_name}/import/",
            "history": f"/{app}/{model_name}/history/",
            "bulk_delete": f"/{app}/{model_name}/bulk-delete/",
            "schema": f"/{app}/{model_name}/schema/",
        }

        # ----------------------------------------------------------
        # META
        # ----------------------------------------------------------

        meta = {

            # Identity
            "key": key,
            "slug": key,

            "app_label": app,
            "model_name": model_name,
            "object_name": object_name,

            # Labels
            "label": meta_cfg.get(
                "label",
                base_label
            ),

            "label_plural": meta_cfg.get(
                "label_plural",
                f"{base_label}s"
            ),

            "verbose_name": str(model._meta.verbose_name),

            "verbose_name_plural": str(
                model._meta.verbose_name_plural
            ),

            # UI
            "icon": ui.get("icon", "cube"),

            "color": ui.get("color", "primary"),

            "group": ui.get("group", app),

            "order": ui.get("order", 100),

            "description": ui.get(
                "description",
                ""
            ),

            # Navigation
            "show_in_sidebar": ui.get(
                "show_in_sidebar",
                True
            ),

            "show_on_dashboard": ui.get(
                "show_on_dashboard",
                True
            ),

            "favorite": ui.get(
                "favorite",
                False
            ),

            "pinned": ui.get(
                "pinned",
                False
            ),

            # URLs
            "routes": routes,

            # ORM
            "db_table": model._meta.db_table,

            "ordering": normalized.get(
                "ordering",
                []
            ),

            # Model Structure
            "field_count": len(model_fields),

            "editable_field_count": len(editable_fields),

            "relation_count": len(relations),

            "m2m_count": len(m2m_fields),

            # Search
            "searchable": bool(
                normalized.get("search_fields")
            ),

            "filterable": bool(
                normalized.get("filter_fields")
            ),

            "sortable": bool(
                normalized.get("ordering")
            ),

            # Features
            "has_create": True,

            "has_update": True,

            "has_delete": True,

            "has_detail": True,

            "has_export": bool(
                normalized.get("export_handler")
            ),

            "has_inline": bool(
                normalized.get("inline_edit_handler")
            ),

            "has_bulk_delete": True,

            "has_history": normalized.get(
                "history",
                False
            ),

            "has_workflow": bool(
                normalized.get("transition_handler")
            ),

            # Performance
            "uses_select_related": bool(
                normalized.get("select_related")
            ),

            "uses_prefetch_related": bool(
                normalized.get("prefetch_related")
            ),

            "cached": bool(
                normalized.get("cache", {})
                .get("enabled")
            ),

            # Security
            "security_model": meta_cfg.get(
                "security_model",
                False
            ),

            "auth_required": meta_cfg.get(
                "auth_required",
                False
            ),

            "audit_enabled": meta_cfg.get(
                "audit_enabled",
                False
            ),

            "security_level": meta_cfg.get(
                "security_level",
                "normal"
            ),

            # API
            "api_enabled": meta_cfg.get(
                "api_enabled",
                True
            ),

            "schema_enabled": True,

            # Registry
            "registered": True,

            "registry_version": 1,

            # Statistics
            "custom_action_count": len(
                normalized.get(
                    "custom_actions",
                    {}
                )
            ),

            "tag_count": len(
                normalized.get(
                    "tags",
                    []
                )
            ),

            "validator_count": len(
                normalized.get(
                    "validators",
                    []
                )
            ),

            "middleware_count": len(
                normalized.get(
                    "middleware",
                    []
                )
            ),
        }

        # ----------------------------------------------------------
        # Merge custom metadata (allow overrides)
        # ----------------------------------------------------------

        meta.update(meta_cfg)

        entry = RegistryEntry(
            model=model,
            config=MappingProxyType(normalized),
            meta=MappingProxyType(meta),
            custom_actions=MappingProxyType(
                self._normalize_actions(
                    normalized.get("custom_actions", {})
                )
            ),
            export_handler=normalized.get("export_handler"),
            inline_edit_handler=normalized.get("inline_edit_handler"),
            transition_handler=normalized.get("transition_handler"),
            before_create=normalized.get("before_create"),
            after_create=normalized.get("after_create"),
            before_update=normalized.get("before_update"),
            after_update=normalized.get("after_update"),
            before_delete=normalized.get("before_delete"),
            after_delete=normalized.get("after_delete"),
            policy=normalized.get("policy"),
            queryset=normalized.get("queryset"),
            renderer=normalized.get("renderer"),
            middleware=tuple(normalized.get("middleware", [])),
            validators=tuple(normalized.get("validators", [])),
            tags=frozenset(normalized.get("tags", [])),
        )

        self._registry[key] = entry

        self.emit(
            "registered",
            {
                "key": key,
                "app": app,
                "model": model_name,
            },
        )

        logger.info("Registered %s", key)

        return entry

    def dashboard_models(self):
        """
        Enterprise registry intelligence serializer.

        Returns:
            - model metadata
            - architecture metrics
            - security posture
            - health score
            - complexity score
            - dependency graph
            - operational metrics
            - feature matrix
            - navigation metadata
        """
        cards = []

        total_models = max(len(self.all()), 1)

        for entry in self.all():
            m = entry.meta
            model = entry.model

            fields = model._meta.fields
            m2m = model._meta.many_to_many

            relations = [
                f for f in fields
                if f.is_relation
            ]

            reverse_relations = [
                r for r in model._meta.related_objects
            ]

            ##########################################
            # COMPLEXITY SCORE
            ##########################################

            complexity = (
                    len(fields) * 1 +
                    len(relations) * 3 +
                    len(m2m) * 4 +
                    len(entry.custom_actions) * 2 +
                    len(entry.validators) * 2 +
                    len(entry.middleware)
            )

            if complexity >= 60:
                complexity_grade = "Extreme"
            elif complexity >= 40:
                complexity_grade = "High"
            elif complexity >= 20:
                complexity_grade = "Medium"
            else:
                complexity_grade = "Low"

            ##########################################
            # HEALTH SCORE
            ##########################################

            score = 0

            checks = [
                m["has_create"],
                m["has_update"],
                m["has_delete"],
                m["api_enabled"],
                m["searchable"],
                m["filterable"],
                m["sortable"],
                m["audit_enabled"],
                m["cached"],
                m["uses_select_related"],
            ]

            score += sum(10 for c in checks if c)
            score = min(score, 100)

            grade = (
                "A+" if score >= 95 else
                "A" if score >= 85 else
                "B" if score >= 70 else
                "C" if score >= 55 else
                "D"
            )

            ##########################################
            # FEATURE MATRIX
            ##########################################

            features = {
                "crud":
                    m["has_create"]
                    and m["has_update"]
                    and m["has_delete"],

                "api": m["api_enabled"],
                "workflow": m["has_workflow"],
                "history": m["has_history"],
                "inline_edit": m["has_inline"],
                "export": m["has_export"],
                "bulk_delete": m["has_bulk_delete"],
                "search": m["searchable"],
                "filters": m["filterable"],
                "sorting": m["sortable"],
                "cache": m["cached"],
                "audit": m["audit_enabled"],
            }

            ##########################################
            # DEPENDENCIES
            ##########################################

            dependencies = []

            for f in relations:
                dependencies.append({
                    "app": f.related_model._meta.app_label,
                    "model": f.related_model._meta.model_name,
                    "field": f.name,
                    "type": f.get_internal_type(),
                })

            dependents = []

            for rel in reverse_relations:
                dependents.append({
                    "app": rel.related_model._meta.app_label,
                    "model": rel.related_model._meta.model_name,
                    "field": rel.field.name,
                })

            ##########################################
            # FIELD INTELLIGENCE
            ##########################################

            field_stats = {
                "total": len(fields),
                "editable": sum(
                    1 for f in fields
                    if f.editable
                ),
                "nullable": sum(
                    1 for f in fields
                    if f.null
                ),
                "indexed": sum(
                    1 for f in fields
                    if getattr(f, "db_index", False)
                ),
                "unique": sum(
                    1 for f in fields
                    if getattr(f, "unique", False)
                ),
                "choices": sum(
                    1 for f in fields
                    if getattr(f, "choices", None)
                ),
                "relations": len(relations),
                "m2m": len(m2m),
            }

            ##########################################
            # PERFORMANCE RATING
            ##########################################

            performance = {
                "cached": m["cached"],
                "select_related": m["uses_select_related"],
                "prefetch_related": m["uses_prefetch_related"],

                "rating":
                    "Excellent"
                    if (
                            m["cached"]
                            and m["uses_select_related"]
                    )
                    else
                    "Good"
                    if (
                            m["uses_select_related"]
                            or m["uses_prefetch_related"]
                    )
                    else
                    "Standard"
            }

            ##########################################
            # SECURITY
            ##########################################

            security = {
                "enabled": m["security_model"],
                "auth_required": m["auth_required"],
                "audit_enabled": m["audit_enabled"],
                "level": m["security_level"],

                "rating":
                    "High"
                    if (
                            m["security_model"]
                            and m["audit_enabled"]
                    )
                    else
                    "Medium"
                    if m["auth_required"]
                    else
                    "Low"
            }

            ##########################################
            # DASHBOARD CARD
            ##########################################

            cards.append({
                "key": m["key"],
                "slug": m["slug"],
                "label": m["label"],
                "label_plural": m["label_plural"],
                "app": m["app_label"],
                "model": m["model_name"],
                "object_name": m["object_name"],

                "icon": m["icon"],
                "color": m["color"],
                "description": m["description"],
                "group": m["group"],

                "favorite": m["favorite"],
                "pinned": m["pinned"],
                "tags": list(entry.tags),

                "class": {
                    "module": model.__module__,
                    "name": model.__name__,
                    "table": model._meta.db_table,
                },

                "features": features,

                "health": {
                    "score": score,
                    "grade": grade,
                },

                "complexity": {
                    "score": complexity,
                    "grade": complexity_grade,
                },

                "security": security,

                "performance": performance,

                "field_stats": field_stats,

                "dependencies": dependencies,

                "dependents": dependents,

                "custom_actions": {
                    "count": len(entry.custom_actions),
                    "actions": list(
                        entry.custom_actions.keys()
                    ),
                },

                "validators": {
                    "count": len(entry.validators),
                },

                "middleware": {
                    "count": len(entry.middleware),
                },

                "registry": {
                    "registered": True,
                    "version": m["registry_version"],
                },

                "navigation": {
                    "list": m["routes"]["list"],
                    "create": m["routes"]["create"],
                    "detail": m["routes"]["detail"],
                    "export": m["routes"]["export"],
                    "schema": m["routes"]["schema"],
                },

                "analytics": {
                    "model_percentage":
                        round(
                            (1 / total_models) * 100,
                            2,
                        ),
                    "dependency_count":
                        len(dependencies),
                    "dependent_count":
                        len(dependents),
                },
            })

        cards.sort(
            key=lambda x: (
                not x["pinned"],
                x["app"],
                x["label"]
            )
        )

        return cards
    def _serialize_options(self, model, limit=200):
        qs = model.objects.all()[:limit]
        return [
            {
                "id": obj.pk,
                "label": str(obj)
            }
            for obj in qs
        ]
    def get(self, model_or_key):
        if isinstance(model_or_key, str):
            return self._registry.get(model_or_key)
        return self._registry.get(self._key(model_or_key))
    def all(self):
        return tuple(self._registry.values())
    def for_app(self, app_label: str):
        return [e for e in self._registry.values() if e.meta["app_label"] == app_label]

    def by_tag(self, tag: str):
        return [e for e in self._registry.values() if tag in e.tags]

    def by_app(self):
        out = defaultdict(list)
        for e in self._registry.values():
            out[e.meta["app_label"]].append(e)
        return dict(out)
    def config(self, model_or_key, runtime_key: Optional[str] = None):
        entry = self.get(model_or_key)
        if not entry:
            return {}

        cfg = dict(entry.config)
        if runtime_key:
            cfg.update(self._runtime.get(runtime_key, {}))
        return cfg
    def set_runtime(self, key: str, patch: dict):
        self._runtime[key].update(patch)
    def clear_runtime(self, key: str):
        self._runtime.pop(key, None)
    def check_policy(self, request, action: str, entry: RegistryEntry):
        if (
            getattr(request.user, "is_active", False)
            and getattr(request.user, "is_superuser", False)
        ):
            return True
        if entry.policy:
            return entry.policy(request, action, entry)
        perm = f"{entry.meta['app_label']}.{action}_{entry.meta['model_name']}"
        if not request.user.has_perm(perm):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied(perm)
        return True
    def resolve_queryset(self, model, request, config):
        if config.get("queryset_factory"):
            return config["queryset_factory"](model)
        qs = model.objects.all()
        if config.get("select_related"):
            qs = qs.select_related(*config["select_related"])
        if config.get("prefetch_related"):
            qs = qs.prefetch_related(*config["prefetch_related"])
        if config.get("ordering"):
            qs = qs.order_by(*config["ordering"])
        return qs

    def build_form(self, model_cls, config=None, registry_instance=None):
        from django import forms
        from django.db import models

        config = config or {}
        registry_instance = registry_instance or self

        # ==============================
        # SAFE OPTIONAL IMPORTS (lazy)
        # ==============================
        try:
            import pytz
        except Exception:
            pytz = None

        try:
            from django_countries import countries
        except Exception:
            countries = None

        # ==============================
        # FIELD TYPE REGISTRY (EXTENSIBLE)
        # ==============================
        FIELD_HANDLERS = {}

        def handler(*field_types):
            def wrapper(fn):
                for t in field_types:
                    FIELD_HANDLERS[t] = fn
                return fn

            return wrapper

        def apply_attrs(form_field, attrs):
            form_field.widget.attrs.update(attrs)

        # ==============================
        # BASE HANDLER
        # ==============================
        def handle_default(field, model_field, schema, ui, attrs):
            attrs.update({
                "data-widget": "text",
                "autocomplete": "off",
            })

        # ==============================
        # HANDLERS
        # ==============================

        @handler(models.ForeignKey)
        def handle_fk(field, model_field, schema, ui, attrs):
            rel_model = model_field.remote_field.model
            field.queryset = rel_model.objects.all()

            attrs.update({
                "data-widget": ui.get("widget", "select"),
                "data-async": "true",
                "data-search": "true",
                "data-clearable": "true",
            })

        @handler(models.ManyToManyField)
        def handle_m2m(field, model_field, schema, ui, attrs):
            rel_model = model_field.remote_field.model
            field.queryset = rel_model.objects.all()

            attrs.update({
                "data-widget": "multi-select",
                "data-async": "true",
                "data-chips": "true",
            })

        @handler(models.BooleanField)
        def handle_bool(field, model_field, schema, ui, attrs):
            attrs.update({"data-widget": "toggle"})

        @handler(models.DateField)
        def handle_date(field, model_field, schema, ui, attrs):
            attrs.update({
                "data-widget": "date-picker",
                "data-calendar": "true",
            })

        @handler(models.DateTimeField)
        def handle_datetime(field, model_field, schema, ui, attrs):
            attrs.update({
                "data-widget": "datetime-picker",
                "data-timezone": str(ui.get("timezone", False)).lower(),
            })

        @handler(models.TimeField)
        def handle_time(field, model_field, schema, ui, attrs):
            attrs.update({"data-widget": "time-picker"})

        @handler(models.IntegerField)
        def handle_int(field, model_field, schema, ui, attrs):
            attrs.update({"data-widget": "number", "data-integer": "true"})

        @handler(models.DecimalField)
        def handle_decimal(field, model_field, schema, ui, attrs):
            attrs.update({
                "data-widget": "number",
                "data-decimal": "true",
                "step": str(model_field.decimal_places or 0.01),
            })

        @handler(models.EmailField)
        def handle_email(field, model_field, schema, ui, attrs):
            attrs.update({"data-widget": "email", "autocomplete": "email"})

        @handler(models.URLField)
        def handle_url(field, model_field, schema, ui, attrs):
            attrs.update({"data-widget": "url"})

        @handler(models.JSONField)
        def handle_json(field, model_field, schema, ui, attrs):
            attrs.update({"data-widget": "json-editor"})

        @handler(models.FileField)
        def handle_file(field, model_field, schema, ui, attrs):
            attrs.update({"data-widget": "file-upload"})

        @handler(models.ImageField)
        def handle_image(field, model_field, schema, ui, attrs):
            attrs.update({"data-widget": "image-upload", "data-preview": "true"})

        # ==============================
        # FORM CLASS
        # ==============================
        class DynamicForm(forms.ModelForm):

            class Meta:
                model = model_cls
                fields = config.get("fields") or "__all__"
                exclude = config.get("exclude") or None

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)

                meta = model_cls._meta

                for name, form_field in self.fields.items():

                    try:
                        model_field = meta.get_field(name)
                    except Exception:
                        continue

                    schema = registry_instance.inspect_field(model_cls, model_field) or {}
                    ui = schema.get("ui") or {}
                    related = schema.get("related") or {}

                    attrs = form_field.widget.attrs
                    widget = form_field.widget

                    attrs.update({
                        "data-widget": ui.get("widget") or widget.__class__.__name__.lower(),
                        "data-widget-class": widget.__class__.__name__,
                        "data-name": name,
                        "data-type": schema.get("type") or model_field.get_internal_type(),
                        "data-model": related.get("model", ""),
                        "data-app": related.get("app", ""),
                        "data-endpoint": schema.get("options_endpoint", ""),
                        "data-required": str(not model_field.blank).lower(),
                        "data-nullable": str(model_field.null).lower(),

                    })

                    # ==============================
                    # UI CUSTOMIZATION
                    # ==============================
                    if ui.get("placeholder"):
                        attrs["placeholder"] = ui["placeholder"]

                    if ui.get("class"):
                        attrs["class"] = ui["class"]

                    if ui.get("readonly") or not model_field.editable:
                        attrs["readonly"] = "readonly"

                    if ui.get("disabled"):
                        attrs["disabled"] = "disabled"

                    if ui.get("depends_on"):
                        attrs["data-depends-on"] = ui["depends_on"]

                    # ==============================
                    # TYPE HANDLING DISPATCH
                    # ==============================
                    handler_fn = FIELD_HANDLERS.get(type(model_field), handle_default)
                    handler_fn(form_field, model_field, schema, ui, attrs)

                    # ==============================
                    # SPECIAL CASES (DATA LIST FIELDS)
                    # ==============================

                    if "country" in name.lower() and countries:
                        form_field.choices = list(countries)
                        attrs["data-widget"] = "country-select"

                    elif "timezone" in name.lower() and pytz:
                        form_field.choices = [(t, t) for t in pytz.all_timezones]
                        attrs["data-widget"] = "timezone-select"

                    elif model_field.choices:
                        form_field.choices = model_field.choices
                        attrs["data-widget"] = "select"

        return DynamicForm

    def sidebar(self, app_label: Optional[str] = None):
        grouped = defaultdict(list)
        entries = self.for_app(app_label) if app_label else self.all()
        for e in entries:
            m = e.meta
            grouped[m["app_label"]].append({
                "key": m["key"],
                "label": m.get("label_plural") or m.get("label") or m["model_name"],
                "model": m["model_name"],
                "icon": m.get("icon", "cube"),
                "routes": {
                    "list": self.url(m["model_name"], "list", m["app_label"]),
                    "create": self.url(m["model_name"], "create", m["app_label"]),
                    "export": self.url(m["model_name"], "export", m["app_label"]),
                },
                "capabilities": {
                    "create": m["has_create"],
                    "export": m["has_export"],
                    "inline": m["has_inline"],
                },
                "actions": list(e.custom_actions.keys()),
            })
        return dict(grouped)
    def url(self, model: str, action: str, app_label: str, pk: str | None = None):
        model = str(model).lower()
        action = str(action).lower()
        collection_actions = ("list", "create", "export", "bulk_delete")
        object_actions = ("detail", "update", "delete", "patch", "inline_edit")
        if action in collection_actions:
            return f"/{app_label}/{model}/{action}/"
        if action in object_actions and pk is not None:
            return f"/{app_label}/{model}/{pk}/{action}/"
        return f"/{app_label}/{model}/"
    def _normalize(self, config: Dict[str, Any]) -> Dict[str, Any]:
        defaults = {
            "fields": None,
            "exclude": ["id"],
            "search_fields": [],
            "filter_fields": [],
            "select_related": [],
            "prefetch_related": [],
            "ordering": None,
            "htmx": True,
            "cache": {"enabled": False, "ttl": 0},
            "security": {"audit": False},
            "custom_actions": {},}
        merged = copy.deepcopy(defaults)
        for k, v in config.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        return merged
    @staticmethod
    def default_inline_edit_handler(context):
        request = context["request"]
        instance = context["instance"]
        field = context.get("field")
        value = context.get("value")
        if not field:
            return instance
        try:
            f = instance._meta.get_field(field)
        except Exception:
            return instance
        if isinstance(f, models.BooleanField):
            value = str(value).lower() in ("true", "1", "yes", "on")
        elif isinstance(f, models.IntegerField):
            try:
                value = int(value)
            except Exception:
                value = None
        elif isinstance(f, models.FloatField):
            try:
                value = float(value)
            except Exception:
                value = None
        setattr(instance, field, value)
        instance.save(update_fields=[field])
        return instance
    def get_inline_handler(self, entry):
        return entry.inline_edit_handler or self.default_inline_edit_handler
    def _normalize_actions(self, actions: dict) -> dict:
        return {k: v for k, v in actions.items() if callable(v)}
    def _humanize(self, name: str) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", " ", name).strip().title()
registry = ModelRegistry()
def register_model(**config):
    def wrapper(model_cls):
        registry.register(model_cls, **config)
        return model_cls
    return wrapper
def get_app_models(app_label: str):
    return list(registry.for_app(app_label))
def registry_health():
    return {
        "models": len(registry.all()),
        "apps": len(registry.by_app()),
        "runtime_patches": len(registry._runtime),}
def resolve_registry_request(request):
    match = request.resolver_match
    return {
        "app": match.kwargs.get("app_label"),
        "model": match.kwargs.get("model"),
        "pk": match.kwargs.get("pk"),
        "action": match.kwargs.get("action"),
    }



