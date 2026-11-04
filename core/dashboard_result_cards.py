from collections import Counter, defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.apps import apps
from django.db.models import Count, F, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from core.registry import registry


STATUS_ORDER = ("NEW", "ANALYSED", "VERIFIED", "CONFIRMED", "APPROVED")
PERIODS = (7, 30, 90, 365)
FAILURE_FILTER = Q(result__lt=F("parameter__min_standard")) | Q(
    result__gt=F("parameter__max_standard")
)


def _value_at(obj, path):
    for part in path.split("__"):
        obj = getattr(obj, part, None) if obj is not None else None
    return obj


def _format_value(value):
    if value is None or value == "":
        return "—"
    if isinstance(value, Decimal):
        return f"{value:,.3f}".rstrip("0").rstrip(".")
    if hasattr(value, "hour") and hasattr(value, "minute"):
        return value.strftime("%H:%M")
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        return value.strftime("%d %b %Y")
    return str(value)


def _can_view(user, model):
    return bool(
        user.is_superuser
        or user.has_perm(f"{model._meta.app_label}.view_{model._meta.model_name}")
    )


def _can_enter_results(user, model):
    return bool(
        user.is_superuser
        or user.has_perm(f"{model._meta.app_label}.add_{model._meta.model_name}")
        or user.has_perm(f"{model._meta.app_label}.change_{model._meta.model_name}")
    )


def permission_scoped_sidebar(request, model_registry, app_label):
    user = request.user
    grouped = model_registry.sidebar(app_label)
    if user.is_superuser:
        return grouped
    visible = {}
    for group, items in grouped.items():
        allowed = [
            item
            for item in items
            if user.has_perm(f"{app_label}.view_{item.get('model', '').lower()}")
        ]
        if allowed:
            visible[group] = allowed
    return visible


def _resolve_plant_scope(request):
    Plant = apps.get_model("t", "TreatmentPlant")
    user = request.user
    available_plants = []
    selected_plant = None

    if user.is_superuser:
        plants = Plant.objects.order_by("name")
        available_plants = list(plants.values("id", "name"))
        requested = request.GET.get("plant", "all")
        if requested.isdigit():
            selected_plant = plants.filter(pk=int(requested)).first()
    else:
        plant_id = getattr(user, "assigned_treatment_plant_id", None)
        if not plant_id and getattr(user, "section_id", None):
            Section = apps.get_model("core", "Section")
            plant_id = Section.objects.filter(pk=user.section_id).values_list(
                "treatment_plant_id", flat=True
            ).first()
        if plant_id:
            selected_plant = Plant.objects.filter(pk=plant_id).first()

    return {
        "selected_plant": selected_plant,
        "available_plants": available_plants,
        "selected_plant_id": str(selected_plant.pk) if selected_plant else "all",
        "can_select_all": bool(user.is_superuser),
        "has_plant_scope": bool(user.is_superuser or selected_plant),
    }


def _model_cards(app_label, user):
    cards = []
    for entry in registry.for_app(app_label) or []:
        meta = entry.meta
        model_name = meta.get("model_name")
        if not model_name or not _can_view(user, entry.model):
            continue
        cards.append({
            "key": meta.get("key"),
            "label": meta.get("verbose_name_plural") or meta.get("label_plural") or model_name,
            "group": meta.get("group", app_label),
            "icon": meta.get("icon", "flask"),
            "model_name": model_name,
            "urls": {
                "list": registry.url(model_name, "list", app_label),
                "create": registry.url(model_name, "create", app_label),
                "export": registry.url(model_name, "export", app_label),
            },
            "capabilities": {
                "create": bool(meta.get("has_create")),
                "export": bool(meta.get("has_export")),
            },
        })
    return cards


def _parameter_compliance(result):
    parameter = result.parameter
    value = result.result
    minimum = parameter.min_standard
    maximum = parameter.max_standard
    unit = getattr(parameter, "unit", "") or ""
    failed = value < minimum or value > maximum
    method = result.analysis_method
    return {
        "name": parameter.name,
        "value": _format_value(value),
        "unit": unit,
        "minimum": _format_value(minimum),
        "maximum": _format_value(maximum),
        "limit": f"{_format_value(minimum)} – {_format_value(maximum)}{f' {unit}' if unit else ''}",
        "compliance": "Not Complying" if failed else "Complying",
        "failed": failed,
        "method": getattr(method, "name", "—"),
        "reference": getattr(method, "reference", "—"),
    }


def _parse_period(request):
    try:
        period = int(request.GET.get("period", 30))
    except (TypeError, ValueError):
        period = 30
    return period if period in PERIODS else 30


def build_result_dashboard(request, app_label, analysis_specs):
    from core.analysis_result_entry import build_analysis_entry_groups

    user = request.user
    scope = _resolve_plant_scope(request)
    today = timezone.localdate()
    period_days = _parse_period(request)
    start_date = today - timedelta(days=period_days - 1)
    current_timezone = timezone.get_current_timezone()
    result_start = timezone.make_aware(datetime.combine(start_date, time.min), current_timezone)
    result_end = timezone.make_aware(datetime.combine(today + timedelta(days=1), time.min), current_timezone)
    available_types = []
    cards = []
    status_totals = Counter()
    type_totals = Counter()
    daily_analysis = Counter()
    daily_results = Counter()
    parameter_totals = defaultdict(lambda: {"readings": 0, "failed": 0})
    total_analyses = 0
    new_analyses = 0
    total_readings = 0
    failed_readings = 0
    analyses_with_results = 0
    analysis_types_with_result_access = 0

    total_analyses_with_result_access = 0
    for spec in analysis_specs:
        Analysis = apps.get_model(app_label, spec["model"])
        Result = apps.get_model(app_label, spec["result_model"])
        if not _can_view(user, Analysis):
            continue

        available_types.append(spec["title"])
        analysis_model_fields = {field.name for field in Analysis._meta.fields}
        sample_model = Analysis._meta.get_field("sample").related_model
        sample_model_fields = {field.name for field in sample_model._meta.fields}
        queryset = Analysis.objects.all()
        if "is_deleted" in analysis_model_fields:
            queryset = queryset.filter(is_deleted=False)
        if "is_deleted" in sample_model_fields:
            queryset = queryset.filter(sample__is_deleted=False)
        if scope["selected_plant"] is not None:
            queryset = queryset.filter(**{spec["plant_path"]: scope["selected_plant"]})
        elif not user.is_superuser:
            queryset = queryset.none()
        queryset = queryset.filter(
            **{
                f"{spec['sample_date_path']}__gte": start_date,
                f"{spec['sample_date_path']}__lte": today,
            }
        )

        type_total = queryset.count()
        total_analyses += type_total
        type_totals[spec["title"]] += type_total
        for row in queryset.values("status").annotate(total=Count("pk")):
            state = row["status"] or "NEW"
            status_totals[state] += row["total"]
            if state == "NEW":
                new_analyses += row["total"]

        for row in queryset.values(spec["sample_date_path"]).annotate(total=Count("pk")):
            sample_day = row[spec["sample_date_path"]]
            if sample_day:
                daily_analysis[sample_day] += row["total"]

        result_permitted = _can_view(user, Result)
        result_queryset = Result.objects.none()
        if result_permitted:
            total_analyses_with_result_access += type_total
            analysis_ids = queryset.values("pk")
            result_model_fields = {field.name for field in Result._meta.fields}
            result_queryset = Result.objects.filter(analysis_id__in=analysis_ids)
            if "is_deleted" in result_model_fields:
                result_queryset = result_queryset.filter(is_deleted=False)
            analysis_types_with_result_access += 1
            summary = result_queryset.aggregate(
                readings=Count("pk"),
                failed=Count("pk", filter=FAILURE_FILTER),
            )
            readings = summary["readings"] or 0
            failed = summary["failed"] or 0
            total_readings += readings
            failed_readings += failed
            analyses_with_results += result_queryset.values("analysis_id").distinct().count()

            for row in result_queryset.values(
                "parameter__name", "parameter__unit"
            ).annotate(
                readings=Count("pk"),
                failed=Count("pk", filter=FAILURE_FILTER),
            ):
                key = (row["parameter__name"], row["parameter__unit"] or "")
                parameter_totals[key]["readings"] += row["readings"]
                parameter_totals[key]["failed"] += row["failed"]

            result_days = result_queryset.filter(
                created_at__gte=result_start,
                created_at__lt=result_end,
            ).annotate(recorded_day=TruncDate("created_at"))
            for row in result_days.values("recorded_day").annotate(
                readings=Count("pk"),
                failed=Count("pk", filter=FAILURE_FILTER),
            ):
                if row["recorded_day"]:
                    daily_results[row["recorded_day"]] += row["readings"]

        analyses = list(
            queryset.select_related(*spec["select_related"])
            .order_by("-analysis_datetime", "-pk")[:8]
        )
        can_enter_results = _can_enter_results(user, Result)
        result_map = defaultdict(list)
        if analyses and result_permitted:
            for result in result_queryset.filter(
                analysis_id__in=[analysis.pk for analysis in analyses]
            ).select_related("parameter", "analysis_method").order_by("parameter__name"):
                result_map[result.analysis_id].append(_parameter_compliance(result))

        for analysis in analyses:
            sample = analysis.sample
            analysis_results = result_map.get(analysis.pk, [])
            result_failures = sum(row["failed"] for row in analysis_results)
            plant = _value_at(analysis, spec["plant_path"])
            status = getattr(analysis, "status", "NEW") or "NEW"
            analyst = getattr(analysis, "analysed_by", None)
            card = {
                "title": spec["title"],
                "sample_title": str(sample),
                "reference": str(getattr(sample, "reference_id", f"#{analysis.pk}")),
                "status": status,
                "status_label": analysis.get_status_display() if hasattr(analysis, "get_status_display") else status.replace("_", " ").title(),
                "stage_index": STATUS_ORDER.index(status) if status in STATUS_ORDER else 0,
                "plant_name": getattr(plant, "name", "—") or "—",
                "sample_fields": [
                    {"label": label, "value": _format_value(_value_at(analysis, path))}
                    for label, path in spec["sample_fields"]
                ],
                "results": analysis_results[:8],
                "result_count": len(analysis_results) if result_permitted else None,
                "results_permitted": result_permitted,
                "failure_count": result_failures,
                "result_state": "Restricted" if not result_permitted else (
                    "No readings" if not analysis_results else
                    "Out of range" if result_failures else "In range"
                ),
                "analyst": analyst.get_full_name() or analyst.username if analyst else "Unassigned",
                "analysis_datetime": analysis.analysis_datetime,
                "detail_url": f"/{app_label}/{Analysis._meta.model_name}/{analysis.pk}/detail/",
                "can_enter_results": can_enter_results and status.casefold() not in {"verified", "confirmed", "approved"},
                "entry_url": f"/{app_label}/results/entry/{Analysis._meta.model_name}/{analysis.pk}/?plant={plant.pk}" if can_enter_results and plant else f"/{app_label}/results/entry/{Analysis._meta.model_name}/{analysis.pk}/" if can_enter_results else "",
                "_entry_spec": spec,
                "_analysis_object": analysis,
                "_plant_object": plant,
                "_has_entry_permission": can_enter_results,
            }
            cards.append(card)

    cards.sort(key=lambda item: item["analysis_datetime"] or timezone.now(), reverse=True)
    worksheet_cards = []
    worksheet_candidates = sorted(
        cards,
        key=lambda item: (bool(item["can_enter_results"]), item["analysis_datetime"] or timezone.now()),
        reverse=True,
    )
    for card in worksheet_candidates:
        if len(worksheet_cards) >= 4:
            break
        if card["_has_entry_permission"] or card["results_permitted"]:
            groups, can_save_inline = build_analysis_entry_groups(
                request,
                app_label,
                card["_entry_spec"],
                card["_analysis_object"],
                card["_plant_object"],
            )
            if groups:
                card["entry_groups"] = groups
                card["can_save_inline"] = can_save_inline
                card["show_worksheet"] = True
                worksheet_cards.append(card)
    for card in cards:
        card.pop("_entry_spec", None)
        card.pop("_analysis_object", None)
        card.pop("_plant_object", None)
        card.pop("_has_entry_permission", None)
    parameter_series = []
    for (name, unit), counts in parameter_totals.items():
        readings = counts["readings"]
        failed = counts["failed"]
        parameter_series.append({
            "label": name,
            "unit": unit,
            "readings": readings,
            "failed": failed,
            "compliant": readings - failed,
            "compliance_percent": round((readings - failed) * 100 / readings, 1) if readings else 0,
        })
    parameter_series.sort(key=lambda item: (-item["failed"], item["compliance_percent"], item["label"]))
    parameter_series = parameter_series[:12]

    analysis_series = []
    result_series = []
    for offset in range(period_days):
        day = start_date + timedelta(days=offset)
        analysis_series.append({
            "date": day.isoformat(),
            "label": day.strftime("%d %b"),
            "count": daily_analysis[day],
        })
        result_series.append({
            "date": day.isoformat(),
            "label": day.strftime("%d %b"),
            "count": daily_results[day],
        })

    pending_readings = max(
        total_analyses_with_result_access - analyses_with_results,
        0,
    ) if analysis_types_with_result_access else 0
    compliant_readings = max(total_readings - failed_readings, 0)
    compliance_rate = round(compliant_readings * 100 / total_readings, 1) if total_readings else 0
    stage_cards = [
        {"key": state, "label": state.replace("_", " ").title(), "count": status_totals[state]}
        for state in STATUS_ORDER
    ]
    active_tab = request.GET.get("tab", "overview")
    valid_tabs = {"overview", "results", "compliance", "trends", "workflow", "models"}
    if active_tab not in valid_tabs:
        active_tab = "overview"

    dashboard_title = {
        "destw": "DESTW · Wastewater operations",
        "kstw": "KSTW · Wastewater & effluent quality",
    }.get(app_label, f"{app_label.upper()} · Analysis operations")
    dashboard_subtitle = {
        "destw": "Plant and stabilization pond sampling, result compliance, and review workflow.",
        "kstw": "Wastewater, industrial, treatment plant, and exhauster analyses in one plant-scoped view.",
    }.get(app_label, "Sample analysis, result compliance, and review workflow.")

    return {
        "dashboard_title": dashboard_title,
        "dashboard_subtitle": dashboard_subtitle,
        "plant": scope["selected_plant"],
        "available_plants": scope["available_plants"],
        "selected_plant_id": scope["selected_plant_id"],
        "can_select_all_plants": scope["can_select_all"],
        "has_plant_scope": scope["has_plant_scope"],
        "cards": cards,
        "worksheet_cards": worksheet_cards,
        "dashboard_return_url": f"{request.get_full_path()}#analysis-worksheets",
        "stage_cards": stage_cards,
        "summary_cards": [
            {"label": "Analysis records", "value": total_analyses, "caption": f"{len(available_types)} visible analysis types", "icon": "fa-vials", "tone": "cyan", "tab": "results"},
            {"label": "Awaiting analysis", "value": new_analyses, "caption": "New workflow records", "icon": "fa-hourglass-half", "tone": "amber", "tab": "workflow", "filter": "NEW"},
            {"label": "Readings recorded", "value": total_readings, "caption": "Saved numeric results", "icon": "fa-flask-vial", "tone": "blue", "tab": "results"},
            {"label": "Out of range", "value": failed_readings, "caption": "Compared with stored parameter standards", "icon": "fa-triangle-exclamation", "tone": "rose", "tab": "compliance", "filter": "failed"},
            {"label": "In-range rate", "value": f"{compliance_rate}%", "caption": f"{compliant_readings} of {total_readings} readings", "icon": "fa-chart-line", "tone": "green", "tab": "compliance"},
            {"label": "No readings yet", "value": pending_readings, "caption": "Analyses with no saved result rows", "icon": "fa-clipboard-question", "tone": "violet", "tab": "results", "filter": "empty"},
        ],
        "available_analysis_types": available_types,
        "analysis_type_series": [
            {"label": label, "value": count}
            for label, count in type_totals.most_common()
        ],
        "analysis_series": analysis_series,
        "result_series": result_series,
        "parameter_series": parameter_series,
        "total_analyses": total_analyses,
        "new_analyses": new_analyses,
        "total_readings": total_readings,
        "failed_readings": failed_readings,
        "compliant_readings": compliant_readings,
        "compliance_rate": compliance_rate,
        "pending_readings": pending_readings,
        "analysis_per_day": round(total_analyses / period_days, 2),
        "period_days": period_days,
        "period_choices": PERIODS,
        "period_start": start_date,
        "period_end": today,
        "period_label": f"{start_date:%d %b %Y} – {today:%d %b %Y}",
        "active_tab": active_tab,
        "models": _model_cards(app_label, user),
        "last_refreshed": timezone.localtime(),
        "has_analysis_permission": bool(available_types),
        "has_result_permission": bool(analysis_types_with_result_access),
    }
