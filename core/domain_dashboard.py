from datetime import date, datetime, time, timedelta

from django.db.models import Count
from django.db.models.functions import TruncDate, TruncMonth, TruncWeek
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone


PERIOD_OPTIONS = (7, 30, 90, 365)
DASHBOARD_TABS = (
    {"id": "overview", "label": "Overview", "icon": "gauge-high"},
    {"id": "analytics", "label": "Analytics", "icon": "chart-line"},
    {"id": "records", "label": "Records", "icon": "table-list"},
    {"id": "models", "label": "Model coverage", "icon": "diagram-project"},
)


def period_days(request, key="days", default=30):
    try:
        requested = int(request.GET.get(key, default))
    except (TypeError, ValueError):
        return default
    return requested if requested in PERIOD_OPTIONS else default


def plant_scope(request):
    from t.models import TreatmentPlant

    user = request.user
    if user.is_superuser:
        plants = TreatmentPlant.objects.order_by("name")
        requested = request.GET.get("plant")
        selected = plants.filter(pk=requested).first() if requested and requested.isdigit() else None
        return plants, selected, True

    plant_id = getattr(user, "assigned_treatment_plant_id", None)
    plants = TreatmentPlant.objects.filter(pk=plant_id).order_by("name") if plant_id else TreatmentPlant.objects.none()
    return plants, plants.first(), False


def time_series(queryset, field_name, days=30):
    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=days - 1)
    field = queryset.model._meta.get_field(field_name)
    if field.get_internal_type() in {"DateTimeField", "DateField"}:
        lower = start_date if field.get_internal_type() == "DateField" else timezone.make_aware(datetime.combine(start_date, time.min))
        upper = end_date if field.get_internal_type() == "DateField" else timezone.now()
        scoped = queryset.filter(**{f"{field_name}__gte": lower, f"{field_name}__lte": upper})
    else:
        return {"labels": [], "values": []}

    truncator = TruncDate if days <= 45 else TruncWeek if days <= 180 else TruncMonth
    rows = (
        scoped.annotate(bucket=truncator(field_name))
        .values("bucket")
        .annotate(total=Count("pk"))
        .order_by("bucket")
    )
    values = {row["bucket"]: row["total"] for row in rows if row["bucket"] is not None}

    if days <= 45:
        buckets = [start_date + timedelta(days=offset) for offset in range(days)]
        labels = [bucket.strftime("%d %b") for bucket in buckets]
        amounts = [values.get(bucket, 0) for bucket in buckets]
    else:
        buckets = sorted(values)
        labels = [bucket.strftime("%d %b") for bucket in buckets]
        amounts = [values[bucket] for bucket in buckets]
    return {"labels": labels, "values": amounts}


def display_user(user):
    if not user:
        return "Not recorded"
    return user.get_full_name().strip() or user.get_username()


def model_list_url(app_label, model_name):
    try:
        from core.registry import registry

        return registry.url(model=model_name, action="list", app_label=app_label)
    except Exception:
        return "#"


def render_domain_dashboard(request, template_name, app_label, context):
    try:
        from core.registry import registry

        context["app_groups"] = registry.sidebar(app_label)
    except Exception:
        context["app_groups"] = []
    return HttpResponse(render_to_string(template_name, context, request=request))


def dashboard_context(
    request,
    *,
    title,
    subtitle,
    kicker,
    kpis,
    charts,
    signals,
    recent_records,
    model_rows,
    table_headings,
    table_rows,
    ranked_rows=None,
    actions=None,
    overview_caption="Live, permission-scoped operational metrics.",
    use_plant_scope=False,
    model_count=None,
):
    days = period_days(request)
    plants, selected, can_choose_plant = plant_scope(request) if use_plant_scope else ([], None, False)
    context = {
        "dashboard_title": title,
        "dashboard_subtitle": subtitle,
        "dashboard_kicker": kicker,
        "dashboard_actions": actions or [],
        "dashboard_tabs": [dict(tab, count=None) for tab in DASHBOARD_TABS],
        "active_tab": request.GET.get("tab") if request.GET.get("tab") in {tab["id"] for tab in DASHBOARD_TABS} else "overview",
        "period_days": days,
        "period_options": PERIOD_OPTIONS,
        "plant_options": plants,
        "selected_plant": selected,
        "show_plant_selector": can_choose_plant,
        "model_count": model_count if model_count is not None else len(model_rows),
        "kpis": kpis,
        "dashboard_charts": charts,
        "dashboard_chart_payload": charts,
        "status_signals": signals,
        "recent_records": recent_records,
        "model_rows": model_rows,
        "table_headings": table_headings,
        "table_rows": table_rows,
        "ranked_rows": ranked_rows or [],
        "overview_caption": overview_caption,
    }
    return context
