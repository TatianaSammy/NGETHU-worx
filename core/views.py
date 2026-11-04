#core/views.py
import logging
import json
from typing import Callable, Optional, Any, Dict
from django.apps import apps
from django.db import models, transaction
from django.views import View
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from core.engine.export_engine import export_engine
from django.views.generic import TemplateView
from django.shortcuts import get_object_or_404, render, redirect
from django.db.models import Q
from django.db.models.fields.related import ForeignKey, OneToOneField, ManyToManyField
from core.mixins import WanjalaMixin
from core.registry import registry
from core.plant_section_scope import object_in_scope, scoped_queryset
from django.template.loader import render_to_string
from django.http import (JsonResponse,HttpResponse,Http404,)

logger = logging.getLogger(__name__)

def registry_router(request, app_label, model, action, pk=None):
    view = resolve_registry_view(app_label, model)
    view_instance = view()

    # attach request early (IMPORTANT)
    view_instance.request = request

    # export shortcut
    if action == "export":
        return view_instance.handle_export(request)

    return view_instance.dispatch(request, action=action, pk=pk)




def fields_schema(request, app_label, model_name):

    entry = None

    # resolve model from registry
    for e in registry.all():
        m = e.model
        if m._meta.app_label == app_label and m._meta.model_name == model_name:
            entry = e
            break

    if not entry:
        return JsonResponse({"error": "Model not found"}, status=404)

    model = entry.model

    schema = [
        registry.inspect_field(model, f)
        for f in model._meta.fields
    ]

    return JsonResponse({
        "model": model_name,
        "app": app_label,
        "fields": schema
    })

LAUNCHER_APPS = [
    ("Core", "/core/"),
    ("Admin", "/admin/"),
    ("Accounts", "/accounts/"),
    ("CA", "/ca/"),
    ("QCA", "/qca/results/cards/"),
    ("Treatment", "/t/"),
    ("Inventory", "/inventory/"),
    ("SC", "/sc/"),
    ("QC", "/qc/"),
    ("WC", "/wc/"),
    ("Parameters", "/parameters/"),
    ("Microbial", "/microbial/"),
    ("Heavy Metal", "/heavy_metal/"),
    ("Instruments", "/instruments/"),
    ("Operations", "/o/"),
    ("PH Adjustment", "/PHadjustment/"),
    ("Duty House", "/duty_house/"),
    ("WSP", "/wsp/"),
    ("Maintenance", "/maintenance/"),
    ("Self Monitoring", "/monitoring/"),
    ("Chlorine Demand", "/chlorinedemand/"),
    ("Jar Test", "/jartest/"),
    ("DESTW", "/destw/"),
    ("KSTW", "/kstw/"),
    ("Backwashing", "/backwashing/"),
    ("Weekly Tasks", "/weeklytasks/"),
    ("XMical Concentration", "/xmicalconcentration/"),
    ("Itinerary", "/itinerary/"),
    ("Filtered Water", "/filtered_water/"),
    ("Company", "/CompanyInfo/"),
    ("Alerts", "/alerts/"),
    ("Central Lab", "/central_lab/"),
]

class HomeView(WanjalaMixin, TemplateView):
    template_name = "core/home/index.html"

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        if not request.user.is_superuser:
            from accounts.utils import get_user_landing_url
            target = get_user_landing_url(request.user)
            if target and target != "/" and target != request.path:
                return redirect(target)
            raise PermissionDenied("The Home View is restricted to system administrators.")

        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return self.get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        entries_by_app = {}
        for entry in registry.all():
            model = entry.model
            entries_by_app.setdefault(model._meta.app_label.lower(), []).append(model)

        visible_apps = []
        for name, url in LAUNCHER_APPS:
            if url == "/admin/":
                if user.is_staff or user.is_superuser:
                    visible_apps.append((name, url))
                continue
            if url == "/accounts/":
                visible_apps.append((name, url))
                continue

            app_label = url.strip("/").split("/", 1)[0].lower()
            models_for_app = entries_by_app.get(app_label, [])
            if user.is_superuser or any(
                user.has_perm(f"{model._meta.app_label}.view_{model._meta.model_name}")
                for model in models_for_app
            ):
                visible_apps.append((name, url))

        context["launcher_apps"] = visible_apps
        return context


class NAMUBUYA(WanjalaMixin, View):
    def dispatch(self, request, *args, **kwargs):
        self.request = request
        self.kwargs = kwargs

        if (r := self.process_request(request)):
            return r

        self.entry = self._resolve_entry()
        self.model = self.entry.model

        return super().dispatch(request, *args, **kwargs)


    def _build_relations(self, obj):
        model = obj.__class__
        entry = self.entry

        fk_relations = []
        reverse_relations = []

        opts = model._meta
        for field in opts.fields:
            if isinstance(field, ForeignKey) and field.name != "id":
                related_obj = getattr(obj, field.name, None)

                rel_model = field.related_model
                rel_app = rel_model._meta.app_label
                rel_name = rel_model._meta.model_name
                if (
                    related_obj is not None
                    and rel_app in {"sc", "weeklytasks"}
                    and not object_in_scope(related_obj, self.request.user, allow_global_read=True)
                ):
                    continue

                fk_relations.append({
                    "name": field.name,
                    "verbose": str(field.verbose_name).title(),
                    "value_id": getattr(related_obj, "pk", None),

                    "urls": {
                        "detail": self.registry.url(rel_name, "detail", rel_app,
                                                    getattr(related_obj, "pk", None)) if related_obj else None,
                        "create": self.registry.url(rel_name, "create", rel_app),
                    }
                })
        for rel in opts.related_objects:

            accessor = rel.get_accessor_name()

            try:
                related_manager = getattr(obj, accessor)
            except Exception:
                continue

            rel_model = rel.related_model
            rel_app = rel_model._meta.app_label
            rel_name = rel_model._meta.model_name

            qs = related_manager.all()
            if rel_app in {"sc", "weeklytasks"}:
                qs = scoped_queryset(qs, self.request.user)
            qs = qs.select_related() if hasattr(qs, "select_related") else qs

            fields = [
                         f.name for f in rel_model._meta.fields
                         if f.name != "id"
                     ][:6]  # limit UI noise

            items = list(qs[:25])  # pagination safety

            reverse_relations.append({
                "label": rel_name.replace("_", " ").title(),
                "count": qs.count() if hasattr(qs, "count") else len(items),

                "model": rel_name,
                "app": rel_app,

                "fields": [
                    {"name": f, "verbose": f.replace("_", " ").title()}
                    for f in fields
                ],

                "items": items,

                "urls": {
                    "create": self.registry.url(rel_name, "create", rel_app),
                    "detail": self.registry.url(rel_name, "detail", rel_app) + "",
                    "update": self.registry.url(rel_name, "update", rel_app) + "",
                    "delete": self.registry.url(rel_name, "delete", rel_app) + "",
                }
            })

        return fk_relations, reverse_relations

    def _build_fields(self):
        config = registry.config(self.model)
        model = self.model

        fields = []

        for f in model._meta.fields:
            if f.name in config.get("exclude", ["id"]):
                continue

            fields.append(
                registry.inspect_field(model, f)            )

        return fields
    def _map_field_type(self, f):
        from django.db import models

        if isinstance(f, models.BooleanField):
            return "bool"
        if isinstance(f, models.DateField):
            return "date"
        if isinstance(f, models.DateTimeField):
            return "datetime"

        return "text"

    def get(self, request, *args, **kwargs):
        return self._execute()

    def post(self, request, *args, **kwargs):
        return self._execute()

    def patch(self, request, *args, **kwargs):
        return self._execute(forced_action="patch")

    def _render_row(self, instance):
        row = registry.resolve_row(instance)

        return render(
            self.request,
            "core/list/dynamic_list.html",
            {
                "row": row,
                "meta": self.entry.meta,
                "custom_actions": self.entry.custom_actions,
            },
        )
    def _execute(self, forced_action: Optional[str] = None):

        action = self._resolve_action(forced_action)

        self._authorize(action)

        handler = self._get_handler(action)

        if not handler:
            return self._error(f"Action '{action}' not implemented", 404)

        ctx = self._ctx(action)

        self._hook(f"before_{action}", ctx)

        try:
            result = handler(ctx)
        except Exception as e:
            logger.exception("Execution failed: %s", e)
            return self._error(str(e), 500)

        self._hook(f"after_{action}", ctx, result)
        self._emit(action, result)

        return result
    def _resolve_action(self, forced):
        if forced:
            return forced

        if self.kwargs.get("action"):
            return self.kwargs["action"]

        if self.request.method == "POST":
            return self.request.POST.get("action", "create")

        return self.request.GET.get("action") or "list"

    def _infer_action(self):
        if self.kwargs.get("pk") or self.kwargs.get("id"):
            return "detail"
        return "list"

    def _authorize(self, action: str):
        if (
            self.model._meta.app_label == "sc"
            and self.model._meta.model_name == "monthlydutyroster"
            and action not in {"list", "detail", "export"}
            and not self.request.user.is_superuser
        ):
            raise PermissionDenied("Monthly duty rosters are plant-wide; only a superuser can change them.")
        if (
            self.model._meta.app_label == "sc"
            and self.model._meta.model_name == "shift"
            and not self.request.user.is_superuser
        ):
            if action in {"delete", "bulk_delete"}:
                raise PermissionDenied("Only a superuser can delete treatment plant shifts.")
            if action in {"update", "inline_edit"}:
                shift = self._obj(self._ctx(action))
                section_id = getattr(self.request.user, "section_id", None)
                if shift.section_requirements.exclude(section_id=section_id).exists():
                    raise PermissionDenied("Only a superuser can change a shift shared across treatment plant sections.")
            elif action not in {"list", "detail", "export", "create"}:
                raise PermissionDenied("Only a superuser can run this action on a treatment plant shift.")
        if (
            self.model._meta.app_label in {"sc", "weeklytasks"}
            and self.model._meta.model_name == "standardweeklytask"
            and action not in {"list", "detail", "export"}
            and not self.request.user.is_superuser
        ):
            raise PermissionDenied("Only a superuser can change the shared task library.")
        if (
            self.model._meta.app_label == "ca"
            and self.request.user.is_superuser
            and action in {"delete", "bulk_delete"}
        ):
            return True
        registry.check_policy(self.request, action, self.entry)

    def _get_handler(self, action: str) -> Optional[Callable]:
        row_actions = {
            "inline_edit",
            "update",
            "delete",
            *self.entry.custom_actions.keys()
        }

        if action in row_actions:
            return self._handle_row_action

        return (
                getattr(self.entry, f"{action}_handler", None)
                or getattr(self, f"_handle_{action}", None)
        )

    def _ctx(self, action: str) -> Dict[str, Any]:
        return {
            "request": self.request,
            "view": self,
            "entry": self.entry,
            "model": self.model,
            "action": action,
            "config": dict(self.entry.config),
        }

    def _handle_list(self, ctx):

        request = ctx["request"]
        qs = self._qs(ctx)
        page_result = self.paginate_queryset(qs)

        if page_result:
            paginator, page_obj, objects, is_paginated = page_result
        else:
            paginator = page_obj = None
            objects = qs
            is_paginated = False

        rows = []

        for obj in objects:
            try:
                rows.append(registry.resolve_row(obj))
            except Exception as e:
                logger.exception(f"Row build failed: {e}")
                continue

        if request.headers.get("x-requested-with") == "XMLHttpRequest" \
                or request.GET.get("format") == "json":
            return JsonResponse({
                "rows": rows,
                "pagination": {
                    "count": qs.count(),
                    "next": None,
                    "previous": None,
                }
            })

        context = {
            "object_list": [],  # 🔥 IMPORTANT: NEVER inject ORM rows
            "entry": self.entry,
            "fields": self._build_fields(),
            "row_mode": "ajax"
        }

        return registry.render_template(
            self.request,
            self.model,
            "list",
            context
        )

    def _handle_detail(self, ctx):

        obj = self._obj(ctx)
        request = ctx["request"]
        config = ctx["config"]
        entry = self.entry
        model = self.model

        fields = self._build_fields()

        def safe(v, default=None):
            try:
                return v() if callable(v) else v
            except Exception:
                return default

        def build_urls(app, model_name, pk=None):
            r = registry
            return {
                "detail": r.url(model_name, "detail", app, pk) if pk else None,
                "update": r.url(model_name, "update", app, pk) if pk else None,
                "delete": r.url(model_name, "delete", app, pk) if pk else None,
                "create": r.url(model_name, "create", app),
                "list": r.url(model_name, "list", app),
            }

        def fk_graph(instance):
            graph = []

            for f in instance._meta.concrete_fields:
                try:
                    if not getattr(f, "is_relation", False):
                        continue

                    rel_model = getattr(f, "related_model", None)
                    if not rel_model:
                        continue

                    val = getattr(instance, f.name, None)
                    if val is None:
                        continue
                    if (
                        rel_model._meta.app_label in {"sc", "weeklytasks"}
                        and not object_in_scope(val, request.user, allow_global_read=True)
                    ):
                        continue

                    graph.append({
                        "type": "fk",
                        "field": f.name,
                        "verbose": str(f.verbose_name),
                        "value_id": getattr(val, "pk", None),
                        "value": str(val),
                        "related_app": rel_model._meta.app_label,
                        "related_model": rel_model._meta.model_name,
                        "urls": build_urls(
                            rel_model._meta.app_label,
                            rel_model._meta.model_name,
                            getattr(val, "pk", None)
                        )
                    })

                except Exception:
                    continue

            return graph

        def reverse_graph(instance):
            graph = []

            for rel in instance._meta.related_objects:
                try:
                    accessor = rel.get_accessor_name()
                    rel_model = rel.related_model
                    manager = getattr(instance, accessor, None)

                    if manager is None:
                        continue

                    qs = manager.all()
                    if rel_model._meta.app_label in {"sc", "weeklytasks"}:
                        qs = scoped_queryset(qs, request.user)

                    if config.get("relation_limit"):
                        qs = qs[:config["relation_limit"]]

                    items = []
                    for o in qs:
                        row = registry.resolve_row(o)
                        items.append({
                            "id": o.pk,
                            "repr": str(o),
                            "fields": row["fields"],
                            "urls": build_urls(
                                rel_model._meta.app_label,
                                rel_model._meta.model_name,
                                o.pk
                            )
                        })

                    graph.append({
                        "type": "reverse",
                        "label": rel.get_accessor_name(),
                        "verbose": str(rel.name),
                        "count": safe(qs.count, len(items)),
                        "app": rel_model._meta.app_label,
                        "model": rel_model._meta.model_name,
                        "fields": [
                            {"name": f.name, "verbose": str(f.verbose_name)}
                            for f in rel_model._meta.fields[:6]
                        ],
                        "items": items,
                        "urls": build_urls(
                            rel_model._meta.app_label,
                            rel_model._meta.model_name
                        ),
                        "htmx": {
                            "enabled": True,
                            "url": f"/{rel_model._meta.app_label}/{rel_model._meta.model_name}/?related_to={obj.pk}",
                            "lazy": True,
                        }
                    })

                except Exception:
                    continue

            return graph

        fk = fk_graph(obj)
        reverse = reverse_graph(obj)

        relation_tree = {
            "root": {
                "model": model._meta.model_name,
                "id": obj.pk,
            },
            "fk": fk,
            "reverse": reverse,
            "stats": {
                "fk_count": len(fk),
                "reverse_count": len(reverse),
            }
        }

        context = {
            "object": obj,
            "entry": entry,
            "meta": entry.meta,
            "fields": fields,
            "fk_relations": fk,
            "reverse_relations": reverse,
            "relation_tree": relation_tree,
            "relation_engine": {
                "enabled": True,
                "lazy": True,
                "htmx": True,
                "infinite_scroll": True,
                "search": True,
                "collapse": True,
                "batch_size": config.get("relation_batch_size", 25),
            },

            "urls": {
                "detail": registry.url(model._meta.model_name, "detail", model._meta.app_label, obj.pk),
                "update": registry.url(model._meta.model_name, "update", model._meta.app_label, obj.pk),
                "delete": registry.url(model._meta.model_name, "delete", model._meta.app_label, obj.pk),
                "create": registry.url(model._meta.model_name, "create", model._meta.app_label),
                "export": registry.url(model._meta.model_name, "export", model._meta.app_label),
            },

            "performance": {
                "select_related": config.get("select_related", []),
                "prefetch_related": config.get("prefetch_related", []),
            },

            "editable": True,
            "export_enabled": bool(entry.export_handler),
        }

        return registry.render_template(
            request,
            model,
            "detail",
            context
        )

    def _handle_create(self, ctx):
        return self._form(ctx, "create")

    def _handle_update(self, ctx):
        return self._form(ctx, "update")

    def _handle_delete(self, ctx):
        obj = self._obj(ctx)

        self._hook("before_delete", ctx, obj)

        obj_id = obj.pk
        if self.request.user.is_superuser:
            from core.engine.superuser_authorization import force_delete_objects

            force_delete_objects([obj], actor=self.request.user)
        else:
            obj.delete()

        self._hook("after_delete", ctx, obj_id)
        self._emit("delete", obj_id)

        return self._success(object_id=obj_id)

    def _handle_bulk_delete(self, ctx):
        ids = ctx["request"].POST.getlist("ids")

        with transaction.atomic():
            queryset = self._qs(ctx).filter(pk__in=ids)
            if ctx["request"].user.is_superuser:
                from core.engine.superuser_authorization import force_delete_objects

                deleted = force_delete_objects(
                    queryset, actor=ctx["request"].user
                )
            else:
                deleted, _ = queryset.delete()

        self._emit("bulk_delete", deleted)
        return self._success(deleted=deleted)

    def _handle_patch(self, ctx):
        return self.post_inline_edit(ctx["request"])

    def _form(self, ctx, action: str):
        request = ctx["request"]

        instance = self._obj(ctx) if action == "update" else None

        Form = registry.build_form(
            self.model,
            self.entry.config,
            registry_instance=registry,
        )

        form = Form(
            request.POST or None,
            request.FILES or None,
            instance=instance,
        )
        if self.model._meta.app_label in {"sc", "weeklytasks"} and not request.user.is_superuser:
            for field in form.fields.values():
                queryset = getattr(field, "queryset", None)
                if queryset is not None:
                    field.queryset = scoped_queryset(queryset, request.user)

        is_htmx = request.headers.get("HX-Request") is not None
        is_json = (
                "application/json"
                in request.headers.get("Content-Type", "")
        )

        # =====================================================
        # GET
        # =====================================================
        if request.method == "GET":
            return registry.render_template(
                request,
                self.model,
                action,
                {
                    "form": form,
                    "entry": self.entry,
                    "object": instance,
                    "action": action,
                    "form_action": request.path,
                },
                mode="partial" if is_htmx else "page",
            )

        # =====================================================
        # ONLY POST ALLOWED
        # =====================================================
        if request.method != "POST":
            return self._error("Method not allowed", 405)

        self._hook(f"before_{action}", ctx, instance)

        # =====================================================
        # VALIDATION FAILED
        # =====================================================
        if not form.is_valid():
            errors = form.errors.get_json_data()

            if is_htmx:
                response = render(
                    request,
                    "core/form/dynamic_form_partial.html",
                    {
                        "form": form,
                        "entry": self.entry,
                        "object": instance,
                        "action": action,
                        "form_action": request.path,
                    },
                    status=422,
                )

                response["HX-Trigger"] = json.dumps({
                    "form:error": errors,
                })

                return response

            return JsonResponse(
                {
                    "success": False,
                    "errors": errors,
                },
                status=422,
            )

        # =====================================================
        # SAVE
        # =====================================================
        try:
            with transaction.atomic():
                if self.model._meta.app_label in {"sc", "weeklytasks"}:
                    obj = form.save(commit=False)
                    if not object_in_scope(obj, request.user):
                        raise PermissionDenied("This record must remain within your assigned treatment plant and section.")
                    obj.save()
                    form.save_m2m()
                else:
                    obj = form.save()

        except PermissionDenied as e:
            return self._error(str(e), 403)
        except Exception as e:
            logger.exception("Form save failed")

            return JsonResponse(
                {
                    "success": False,
                    "message": str(e),
                },
                status=500,
            )

        self._hook(f"after_{action}", ctx, obj)
        self._emit(action, obj)

        row = registry.resolve_row(obj)

        payload = {
            "success": True,
            "action": action,
            "id": obj.pk,
            "object_id": obj.pk,
            "row": row,
            "object": {
                "pk": obj.pk,
                "str": str(obj),
                "model": obj._meta.model_name,
                "app": obj._meta.app_label,
            },
            "message": (
                f"{self.entry.meta['verbose_name']} "
                f"{'created' if action == 'create' else 'updated'} successfully."
            ),
        }

        # =====================================================
        # HTMX RESPONSE
        # =====================================================
        if is_htmx:

            html = render_to_string(
                "core/row/dynamic_row.html",
                {
                    "obj": obj,
                    "row": row,
                    "entry": self.entry,
                    "meta": self.entry.meta,
                    "fields": row.get("fields", []),
                    "custom_actions": self.entry.custom_actions,
                },
                request=request,
            )

            response = HttpResponse(html)

            trigger = {
                "form:success": payload,
                "registry:reload": True,
                "registry:row-added": payload,
            }

            if action == "update":
                trigger["registry:row-updated"] = payload

            if action == "create":
                trigger["registry:row-created"] = payload
                trigger["form:reset"] = True

            response["HX-Trigger"] = json.dumps(trigger)

            # optional modal close
            response["HX-Trigger-After-Swap"] = json.dumps({
                "modal:close": True,
            })

            return response

        # =====================================================
        # JSON API RESPONSE
        # =====================================================
        return JsonResponse(payload, status=200)

    def _qs(self, ctx):
        request = ctx["request"]
        config = ctx["config"]

        qs = (
            self.entry.queryset(self.model, request, config)
            if self.entry.queryset
            else registry.resolve_queryset(self.model, request, config)
        )
        if self.model._meta.app_label in {"sc", "weeklytasks"}:
            qs = scoped_queryset(qs, request.user)

        search = request.GET.get("search")
        if search:
            q = Q()

            for f in self.model._meta.fields:
                if isinstance(f, (models.CharField, models.TextField)):
                    q |= Q(**{f"{f.name}__icontains": search})

            qs = qs.filter(q)

        return qs

    # =====================================================
    # OBJECT RESOLUTION (HARDENED)
    # =====================================================
    def _obj(self, ctx):
        request = ctx["request"]

        pk = (
                self.kwargs.get("pk")
                or self.kwargs.get("id")
                or request.GET.get("id")
                or request.POST.get("id")
        )

        if not pk:
            raise Http404("Object ID not provided")

        qs = self._qs(ctx)  # 🔥 CRITICAL: same queryset as list

        return get_object_or_404(qs, pk=pk)

    def _resolve_entry(self):
        model_name = self.kwargs.get("model")

        if not model_name:
            raise Http404("Missing model")

        model_name = model_name.lower().strip()

        # ✅ GET APP FROM URLCONF (not hardcoded)
        app_label = self.request.resolver_match.app_name

        if not app_label:
            raise Http404("Missing app namespace")

        key = f"{app_label}.{model_name}"

        entry = registry.get(key)

        if not entry:
            available = list(registry._registry.keys())

            logger.error("Registry miss: %s", key)
            logger.error("Available:\n%s", "\n".join(available))

            raise Http404(f"Model not registered: {key}")

        logger.info("Resolved registry entry: %s", key)

        return entry

    # =====================================================
    # EVENTS
    # =====================================================
    def _emit(self, event: str, payload: Any):
        registry.emit(event, {
            "model": self.model.__name__,
            "payload": getattr(payload, "pk", payload),
        })

    # =====================================================
    # HOOKS
    # =====================================================
    def _hook(self, name: str, ctx: dict, *args):
        hook = getattr(self.entry, name, None)
        if callable(hook):
            try:
                hook(ctx, *args)
            except Exception:
                logger.exception("Hook failed: %s", name)

    # =====================================================
    # RESPONSES
    # =====================================================
    def _success(self, obj=None, **extra):
        return JsonResponse({
            "success": True,
            "object_id": getattr(obj, "pk", None),
            **extra,
        })

    def _error(self, errors, status=400):
        return JsonResponse({
            "success": False,
            "errors": errors
        }, status=status)
    def assert_option_b(self, payload: dict):
        """
        Ensures frontend contract is RowSchema-compatible.
        Prevents accidental ORM leakage.
        """

        required_keys = {"id", "fields", "actions", "ui"}

        missing = required_keys - payload.keys()
        if missing:
            raise ImproperlyConfigured(f"RowSchema violation: missing {missing}")

        if not isinstance(payload.get("fields"), dict):
            raise ImproperlyConfigured("RowSchema violation: 'fields' must be dict")

    def _handle_row_action(self, ctx):
        request = ctx["request"]
        action = ctx["action"]
        entry = self.entry

        instance = None
        if action not in ("list", "create", "bulk_delete"):
            instance = self._obj(ctx)

        # =====================================================
        # INLINE EDIT (FIELD LEVEL) — ROBUST + TYPE SAFE
        # =====================================================
        if action == "inline_edit":

            # -------------------------
            # GET (render inline editor)
            # -------------------------
            if request.method == "GET":
                field_name = request.GET.get("field")

                if not field_name:
                    return self._error("Field is required", 400)

                row = registry.resolve_row(instance)

                if field_name not in row["fields"]:
                    return self._error("Invalid field", 400)

                field_meta = row["fields"][field_name]

                return render(
                    request,
                    "core/inline_edit/dynamic_inline_edit_partial.html",
                    {
                        "row": row,
                        "field_name": field_name,
                        "value": field_meta["value"],
                        "meta": entry.meta,
                        "field": field_meta
                    },
                )

            # -------------------------
            # POST / PATCH (persist edit)
            # -------------------------
            if request.method in ("POST", "PATCH"):

                # =========================
                # SAFE PAYLOAD PARSING
                # =========================
                try:
                    if request.content_type == "application/json":
                        payload = json.loads(request.body.decode("utf-8") or "{}")
                    else:
                        payload = request.POST.dict()
                except Exception:
                    return self._error("Invalid request payload", 400)

                field_name = payload.get("field")
                value = payload.get("value")

                if not field_name:
                    return self._error("Field is required", 400)

                try:
                    field_obj = instance._meta.get_field(field_name)
                except Exception:
                    return self._error(f"Unknown field: {field_name}", 400)

                if field_obj.primary_key:
                    return self._error("Primary key cannot be edited", 403)

                try:
                    if isinstance(field_obj, models.BooleanField):
                        value = str(value).lower() in ("1", "true", "yes", "on")

                    elif isinstance(field_obj, models.IntegerField):
                        value = int(value)

                    elif isinstance(field_obj, models.FloatField):
                        value = float(value)

                    elif isinstance(field_obj, models.DateField):
                        value = value  # optionally parse ISO if needed

                    elif isinstance(field_obj, models.ForeignKey):
                        related_queryset = field_obj.remote_field.model.objects.all()
                        if self.model._meta.app_label in {"sc", "weeklytasks"}:
                            related_queryset = scoped_queryset(related_queryset, request.user)
                        value = related_queryset.get(pk=value)

                except Exception as e:
                    return self._error(f"Invalid value type: {str(e)}", 400)

                self._hook("before_update", ctx, instance)

                try:
                    with transaction.atomic():
                        setattr(instance, field_name, value)
                        if (
                            self.model._meta.app_label in {"sc", "weeklytasks"}
                            and not object_in_scope(instance, request.user, allow_global_read=True)
                        ):
                            return self._error("This record must remain within your assigned treatment plant and section.", 403)
                        update_fields = [field_name]
                        if field_name == "value" and hasattr(instance, "entry_status"):
                            instance.entry_status = instance.EntryStatus.RECORDED
                            update_fields.append("entry_status")
                        instance.save(update_fields=update_fields)

                except Exception as e:
                    logger.exception("Inline update failed")
                    return self._error("Update failed", 500)

                # =========================
                # HOOK: AFTER UPDATE
                # =========================
                self._hook("after_update", ctx, instance)

                updated_row = registry.resolve_row(instance)

                return JsonResponse({
                    "success": True,
                    "id": instance.pk,
                    "field": field_name,
                    "value": updated_row["fields"][field_name]["value"],
                    "row": updated_row
                })

        if action == "delete":
            if not instance:
                return self._error("Object not found", 404)

            pk = instance.pk

            self._hook("before_delete", ctx, instance)

            try:
                with transaction.atomic():
                    if ctx["request"].user.is_superuser:
                        from core.engine.superuser_authorization import force_delete_objects

                        force_delete_objects([instance], actor=ctx["request"].user)
                    else:
                        instance.delete()
            except Exception as e:
                return self._error(str(e), 500)

            self._hook("after_delete", ctx, pk)
            self._emit("delete", pk)

            return JsonResponse({
                "success": True,
                "deleted": True,
                "id": pk,
                "ui": {
                    "toast": "success",
                    "remove_row": True
                }
            })


        if action == "update":

            Form = registry.build_form(self.model, entry.config)
            form = Form(request.POST or None, request.FILES or None, instance=instance)
            if self.model._meta.app_label in {"sc", "weeklytasks"} and not request.user.is_superuser:
                for field in form.fields.values():
                    queryset = getattr(field, "queryset", None)
                    if queryset is not None:
                        field.queryset = scoped_queryset(queryset, request.user)

            if request.method == "POST":

                if form.is_valid():
                    try:
                        with transaction.atomic():
                            updated = form.save(commit=False)
                            if (
                                self.model._meta.app_label in {"sc", "weeklytasks"}
                                and not object_in_scope(updated, request.user, allow_global_read=True)
                            ):
                                raise PermissionDenied("This record must remain within your assigned treatment plant and section.")
                            updated.save()
                            form.save_m2m()

                    except PermissionDenied as e:
                        return self._error(str(e), 403)
                    except Exception as e:
                        return self._error(str(e), 500)

                    self._hook("after_update", ctx, updated)

                    return JsonResponse({
                        "success": True,
                        "id": updated.pk,
                        "row": registry.resolve_row(updated)
                    })

                return render(
                    request,
                    "core/form/dynamic_form_partial.html",
                    {
                        "form": form,
                        "entry": entry
                    }
                )

            return render(
                request,
                "core/form/dynamic_form_partial.html",
                {
                    "form": form,
                    "entry": entry
                }
            )

        if action in entry.custom_actions:

            try:
                result = entry.custom_actions[action](ctx)
            except Exception as e:
                logger.exception("Custom action failed")
                return self._error(str(e), 500)

            if isinstance(result, models.Model):
                return self._render_row(result)

            return result

        return self._error("Unhandled row action", 400)

    def handle_export(self, request):
        export_type = (
                request.GET.get("export")
                or request.GET.get("type")
        )

        if not export_type:
            return self._error(
                "Export type required",
                400,
            )

        self._authorize("export")

        ctx = self._ctx("export")
        ctx["type"] = export_type

        self._hook("before_export", ctx)

        try:
            response = export_engine.run(ctx)

        except Exception as e:
            logger.exception("Export failed")

            return self._error(
                str(e),
                500,
            )

        self._hook(
            "after_export",
            ctx,
            response,
        )

        self._emit(
            "export",
            {"type": export_type},
        )

        return response

