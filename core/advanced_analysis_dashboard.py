from collections import Counter, defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.apps import apps
from django.db.models import Count, DecimalField, F, Q, Value, CharField
from django.db.models.functions import TruncDate
from django.utils import timezone

from core.dashboard_result_cards import _can_enter_results, _can_view, _model_cards, _parse_period, _resolve_plant_scope, permission_scoped_sidebar
from core.registry import registry


STATUS_ORDER = ("NEW", "ANALYSED", "VERIFIED", "CONFIRMED", "APPROVED")
PERIODS = (7, 30, 90, 365)


def _get_path(instance, path):
    current = instance
    for part in path.split("__") if path else ():
        current = getattr(current, part, None) if current is not None else None
    return current


def _display(value):
    if value is None or value == "":
        return "—"
    if isinstance(value, Decimal):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if hasattr(value, "hour") and hasattr(value, "minute"):
        return value.strftime("%H:%M")
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        return value.strftime("%d %b %Y")
    return str(value)


def _empty_scope():
    return {
        "selected_plant": None,
        "available_plants": [],
        "selected_plant_id": "all",
        "can_select_all": False,
        "has_plant_scope": True,
    }


def _analysis_queryset(model, spec, scope, user, start_date, end_date):
    queryset = model.objects.all()
    fields = {field.name for field in model._meta.fields}
    if "is_deleted" in fields:
        queryset = queryset.filter(is_deleted=False)
    if spec.get("date_path"):
        queryset = queryset.filter(**{f"{spec['date_path']}__gte": start_date, f"{spec['date_path']}__lte": end_date})
    plant_path = spec.get("plant_path")
    if plant_path:
        if scope["selected_plant"]:
            queryset = queryset.filter(**{plant_path: scope["selected_plant"]})
        elif not user.is_superuser:
            queryset = queryset.none()
    sample_path = spec.get("sample_path")
    if sample_path:
        sample_model = model._meta.get_field(sample_path).related_model
        if "is_deleted" in {field.name for field in sample_model._meta.fields}:
            queryset = queryset.filter(**{f"{sample_path}__is_deleted": False})
    return queryset


def _result_queryset(result_model, result_spec, analysis_ids):
    queryset = result_model.objects.filter(**{result_spec.get("analysis_id_path", "analysis_id") + "__in": analysis_ids})
    fields = {field.name for field in result_model._meta.fields}
    if "is_deleted" in fields:
        queryset = queryset.filter(is_deleted=False)
    if result_spec.get("filters"):
        queryset = queryset.filter(**result_spec["filters"])
    if result_spec.get("include_filter") is not None:
        queryset = queryset.filter(result_spec["include_filter"])
    return queryset


def _result_rows(queryset, spec):
    annotations = {
        "_dash_value": F(spec.get("value_field", "result")),
        "_dash_name": F(spec["parameter_name_path"]),
        "_dash_unit": F(spec["unit_path"]) if spec.get("unit_path") else Value("", output_field=CharField()),
        "_dash_method": F(spec["method_path"]) if spec.get("method_path") else Value("—", output_field=CharField()),
        "_dash_reference": F(spec["reference_path"]) if spec.get("reference_path") else Value("—", output_field=CharField()),
        "_dash_compliance": F(spec["compliance_path"]) if spec.get("compliance_path") else Value("", output_field=CharField()),
    }
    if spec.get("minimum_path"):
        annotations["_dash_minimum"] = F(spec["minimum_path"])
    if spec.get("maximum_path"):
        annotations["_dash_maximum"] = F(spec["maximum_path"])
    else:
        annotations["_dash_maximum"] = Value(None, output_field=DecimalField(max_digits=18, decimal_places=6))
    if not spec.get("minimum_path"):
        annotations["_dash_minimum"] = Value(None, output_field=DecimalField(max_digits=18, decimal_places=6))
    return queryset.annotate(**annotations).values(
        "_dash_value", "_dash_name", "_dash_unit", "_dash_method", "_dash_reference",
        "_dash_compliance", "_dash_minimum", "_dash_maximum", "created_at",
        spec.get("analysis_id_path", "analysis_id"),
    )


def _reading_status(row):
    stored = str(row.get("_dash_compliance") or "").strip().lower()
    if stored in {"not complying", "non-compliant", "noncompliant", "failed", "out of range"}:
        return "failed"
    if stored in {"complying", "compliant", "pass", "within range"}:
        return "compliant"
    minimum = row.get("_dash_minimum")
    maximum = row.get("_dash_maximum")
    if minimum is None and maximum is None:
        return "unrated"
    value = row.get("_dash_value")
    if value is None:
        return "unrated"
    return "failed" if (minimum is not None and value < minimum) or (maximum is not None and value > maximum) else "compliant"


def _choice_stages(analysis_model, spec, status_totals):
    status_field = spec.get("status_field", "status")
    field = analysis_model._meta.get_field(status_field)
    choices = list(field.choices)
    if not choices:
        choices = [(status, status.replace("_", " ").title()) for status in STATUS_ORDER]
    return [
        {"key": str(key), "label": str(label), "count": status_totals[str(key)]}
        for key, label in choices
    ]


def build_advanced_analysis_dashboard(request, app_label, analysis_specs, *, analysis_type=None, analysis_type_tabs=None):
    from core.analysis_result_entry import _result_specs, build_analysis_entry_groups

    user = request.user
    plant_scoped = any(spec.get("plant_path") for spec in analysis_specs)
    scope = _resolve_plant_scope(request) if plant_scoped else _empty_scope()
    today = timezone.localdate()
    period_days = _parse_period(request)
    start_date = today - timedelta(days=period_days - 1)
    start_datetime = timezone.make_aware(datetime.combine(start_date, time.min))
    end_datetime = timezone.make_aware(datetime.combine(today + timedelta(days=1), time.min))
    cards = []
    available_types = []
    type_totals = Counter()
    status_totals = Counter()
    daily_analysis = Counter()
    daily_results = Counter()
    parameter_totals = defaultdict(lambda: {"readings": 0, "failed": 0, "unrated": 0, "sum": 0.0, "minimum": None, "maximum": None})
    total_analyses = 0
    new_analyses = 0
    total_readings = 0
    failed_readings = 0
    unrated_readings = 0
    analyses_with_results = 0
    analyses_with_result_access = 0
    result_type_count = 0
    all_status_cards = []

    for spec in analysis_specs:
        Analysis = apps.get_model(app_label, spec["model"])
        if not _can_view(user, Analysis):
            continue
        available_types.append(spec["title"])
        queryset = _analysis_queryset(Analysis, spec, scope, user, start_date, today)
        analysis_count = queryset.count()
        total_analyses += analysis_count
        type_totals[spec["title"]] += analysis_count
        status_field = spec.get("status_field", "status")
        for item in queryset.values(status_field).annotate(total=Count("pk")):
            state = str(item[status_field] or "NEW")
            status_totals[state] += item["total"]
            if state.lower() == "new":
                new_analyses += item["total"]
        for item in queryset.values(spec["date_path"]).annotate(total=Count("pk")):
            sample_day = item[spec["date_path"]]
            if sample_day:
                daily_analysis[sample_day] += item["total"]

        result_models = []
        result_queryset_map = []
        analysis_ids = queryset.values("pk")
        result_permitted = True
        entry_specs = _result_specs(spec)
        entry_result_models = [apps.get_model(result_spec.get("app_label", app_label), result_spec["model"]) for result_spec in entry_specs]
        has_entry_permission = any(_can_enter_results(user, Result) for Result in entry_result_models)
        for result_spec in spec.get("result_specs", []):
            Result = apps.get_model(result_spec.get("app_label", app_label), result_spec["model"])
            if not _can_view(user, Result):
                result_permitted = False
                continue
            result_models.append((Result, result_spec))
            result_queryset_map.append((Result, result_spec, _result_queryset(Result, result_spec, analysis_ids)))
        if result_models:
            result_type_count += 1
            analyses_with_result_access += analysis_count

        selected = list(queryset.select_related(*spec.get("select_related", [])).order_by(
            f"-{spec.get('analysis_datetime_path', 'created_at')}", "-pk"
        )[:12])
        selected_ids = {analysis.pk for analysis in selected}
        result_map = defaultdict(list)
        analysis_result_counts = Counter()
        analysis_result_failures = Counter()
        analysis_result_unrated = Counter()
        if result_permitted:
            for Result, result_spec, result_queryset in result_queryset_map:
                for row in _result_rows(result_queryset, result_spec).iterator(chunk_size=1500):
                    value = row["_dash_value"]
                    if value is None:
                        continue
                    state = _reading_status(row)
                    total_readings += 1
                    if state == "failed":
                        failed_readings += 1
                    elif state == "unrated":
                        unrated_readings += 1
                    key = (str(row["_dash_name"] or result_spec.get("title", "Result")), str(row["_dash_unit"] or ""))
                    totals = parameter_totals[key]
                    totals["readings"] += 1
                    totals["failed"] += state == "failed"
                    totals["unrated"] += state == "unrated"
                    try:
                        numeric_value = float(value)
                        totals["sum"] += numeric_value
                        totals["minimum"] = numeric_value if totals["minimum"] is None else min(totals["minimum"], numeric_value)
                        totals["maximum"] = numeric_value if totals["maximum"] is None else max(totals["maximum"], numeric_value)
                    except (TypeError, ValueError):
                        numeric_value = None
                    analysis_id = row.get("analysis_id")
                    if analysis_id is not None:
                        analysis_result_counts[analysis_id] += 1
                        if state == "failed":
                            analysis_result_failures[analysis_id] += 1
                        if state == "unrated":
                            analysis_result_unrated[analysis_id] += 1
                        if analysis_id in selected_ids and len(result_map[analysis_id]) < 8:
                            minimum = row.get("_dash_minimum")
                            maximum = row.get("_dash_maximum")
                            unit = str(row["_dash_unit"] or "")
                            if minimum is None and maximum is None:
                                limit = "No configured standard"
                            else:
                                limit = f"{_display(minimum) if minimum is not None else '—'} – {_display(maximum) if maximum is not None else '—'}{f' {unit}' if unit else ''}"
                            result_map[analysis_id].append({
                                "name": f"{result_spec.get('title_prefix', '')}{str(row['_dash_name'] or 'Result')}",
                                "value": _display(value),
                                "unit": unit,
                                "minimum": _display(minimum),
                                "maximum": _display(maximum),
                                "limit": limit,
                                "compliance": "Not Complying" if state == "failed" else "Complying" if state == "compliant" else "Not rated",
                                "failed": state == "failed",
                                "method": str(row["_dash_method"] or "—"),
                                "reference": str(row["_dash_reference"] or "—"),
                            })
                date_queryset = result_queryset.filter(created_at__gte=start_datetime, created_at__lt=end_datetime).annotate(recorded_day=TruncDate("created_at"))
                for item in date_queryset.values("recorded_day").annotate(readings=Count("pk")):
                    if item["recorded_day"]:
                        daily_results[item["recorded_day"]] += item["readings"]
            analyses_with_results += len([pk for pk, count in analysis_result_counts.items() if count])

        for analysis in selected:
            status = str(getattr(analysis, status_field, "NEW") or "NEW")
            plant = _get_path(analysis, spec.get("plant_path")) if spec.get("plant_path") else None
            analyst = _get_path(analysis, spec.get("analyst_path", "analysed_by"))
            analysis_datetime = _get_path(analysis, spec.get("analysis_datetime_path", "created_at"))
            result_count = analysis_result_counts.get(analysis.pk, 0) if result_permitted else None
            failures = analysis_result_failures.get(analysis.pk, 0)
            unrated = analysis_result_unrated.get(analysis.pk, 0)
            if not result_permitted:
                result_state = "Restricted"
            elif not result_count:
                result_state = "No readings"
            elif failures:
                result_state = "Out of range"
            elif unrated:
                result_state = "Needs standard review"
            else:
                result_state = "In range"
            card = {
                "title": spec["title"],
                "sample_title": str(_get_path(analysis, spec.get("sample_title_path", "reference_id")) or f"Analysis {analysis.pk}"),
                "reference": str(_get_path(analysis, spec.get("reference_path", "reference_id")) or f"#{analysis.pk}"),
                "status": status,
                "status_label": analysis.get_state_display() if hasattr(analysis, "get_state_display") else analysis.get_status_display() if hasattr(analysis, "get_status_display") else status.replace("_", " ").title(),
                "stage_index": STATUS_ORDER.index(status.upper()) if status.upper() in STATUS_ORDER else 0,
                "plant_name": getattr(plant, "name", None) or spec.get("scope_label") or (scope["selected_plant"].name if scope["selected_plant"] else "All accessible plants"),
                "sample_fields": [{"label": label, "value": _display(_get_path(analysis, path))} for label, path in spec.get("sample_fields", [])],
                "results": result_map.get(analysis.pk, []),
                "result_count": result_count,
                "results_permitted": result_permitted,
                "failure_count": failures,
                "result_state": result_state,
                "analyst": analyst.get_full_name() or analyst.username if analyst else "Unassigned",
                "analysis_datetime": analysis_datetime,
                "detail_url": f"/{app_label}/{Analysis._meta.model_name}/{analysis.pk}/detail/",
                "can_enter_results": has_entry_permission and status.casefold() not in {"verified", "confirmed", "approved"},
                "entry_url": (
                    f"/{app_label}/results/entry/{Analysis._meta.model_name}/{analysis.pk}/?plant={plant.pk}"
                    if plant and has_entry_permission
                    else f"/{app_label}/results/entry/{Analysis._meta.model_name}/{analysis.pk}/"
                    if has_entry_permission
                    else ""
                ),
                "_entry_spec": spec,
                "_analysis_object": analysis,
                "_plant_object": plant,
                "_has_entry_permission": has_entry_permission,
            }
            cards.append(card)
        all_status_cards.extend(_choice_stages(Analysis, spec, status_totals))

    cards.sort(key=lambda row: row["analysis_datetime"] or timezone.now(), reverse=True)
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
    for (name, unit), totals in parameter_totals.items():
        readings = totals["readings"]
        rated = readings - totals["unrated"]
        parameter_series.append({
            "label": name,
            "unit": unit,
            "readings": readings,
            "failed": totals["failed"],
            "unrated": totals["unrated"],
            "compliant": max(rated - totals["failed"], 0),
            "compliance_percent": round((rated - totals["failed"]) * 100 / rated, 1) if rated else 0,
            "mean": round(totals["sum"] / readings, 4) if readings else 0,
            "minimum": totals["minimum"],
            "maximum": totals["maximum"],
        })
    parameter_series.sort(key=lambda row: (-row["failed"], row["compliance_percent"], row["label"]))
    parameter_series = parameter_series[:15]

    analysis_series = []
    result_series = []
    for offset in range(period_days):
        day = start_date + timedelta(days=offset)
        analysis_series.append({"date": day.isoformat(), "label": day.strftime("%d %b"), "count": daily_analysis[day]})
        result_series.append({"date": day.isoformat(), "label": day.strftime("%d %b"), "count": daily_results[day]})

    rated_readings = max(total_readings - unrated_readings, 0)
    compliant_readings = max(rated_readings - failed_readings, 0)
    compliance_rate = round(compliant_readings * 100 / rated_readings, 1) if rated_readings else 0
    compliance_rate_display = f"{compliance_rate}%" if rated_readings else "—"
    stage_cards = []
    seen_states = set()
    for stage in all_status_cards:
        if stage["key"] in seen_states:
            continue
        seen_states.add(stage["key"])
        stage_cards.append({**stage, "count": status_totals[stage["key"]]})
    if not stage_cards:
        stage_cards = [{"key": state, "label": state.title(), "count": status_totals[state]} for state in STATUS_ORDER]

    active_tab = request.GET.get("tab", "overview")
    valid_tabs = {"overview", "results", "compliance", "trends", "workflow", "models"}
    if active_tab not in valid_tabs:
        active_tab = "overview"
    result_state_label = next((spec.get("result_description") for spec in analysis_specs if spec.get("result_description")), "stored readings compared with configured limits")
    context = {
        "dashboard_title": {"heavy_metal": "Heavy Metal · Trace Analytics", "microbial": "Microbial · Bioassay Operations", "central_lab": "Central Laboratory · Sample Intelligence"}.get(app_label, f"{app_label.upper()} · Analysis Operations"),
        "dashboard_subtitle": {"heavy_metal": "Trace element results, plant-phase monitoring, nonconformities, and verification pipeline.", "microbial": "Microbial assays, incubation windows, treated-water results, and compliance signals.", "central_lab": "Region, Catchment, and Kabete sample analyses with method traceability and result compliance."}.get(app_label, "Sample analysis, result compliance, and review workflow."),
        "domain_label": {"heavy_metal": "TRACE ELEMENT CONTROL", "microbial": "MICROBIAL QUALITY CONTROL", "central_lab": "CENTRAL LABORATORY INTELLIGENCE"}.get(app_label, app_label.upper()),
        "plant": scope["selected_plant"],
        "available_plants": scope["available_plants"],
        "selected_plant_id": scope["selected_plant_id"],
        "can_select_all_plants": scope["can_select_all"],
        "show_plant_scope": plant_scoped,
        "has_plant_scope": scope["has_plant_scope"],
        "cards": cards,
        "worksheet_cards": worksheet_cards,
        "dashboard_return_url": f"{request.get_full_path()}#analysis-worksheets",
        "stage_cards": stage_cards,
        "summary_cards": [
            {"label": "Analysis records", "value": total_analyses, "caption": f"{len(available_types)} visible analysis types", "icon": "fa-vials", "tone": "cyan", "tab": "results"},
            {"label": "Awaiting analysis", "value": new_analyses, "caption": "New workflow records", "icon": "fa-hourglass-half", "tone": "amber", "tab": "workflow", "filter": stage_cards[0]["key"] if stage_cards else "NEW"},
            {"label": "Recorded readings", "value": total_readings, "caption": "Persisted result values", "icon": "fa-flask-vial", "tone": "blue", "tab": "results"},
            {"label": "Out of range", "value": failed_readings, "caption": "Compared with configured limits or stored decisions", "icon": "fa-triangle-exclamation", "tone": "rose", "tab": "compliance", "filter": "failed"},
            {"label": "Compliance rate", "value": compliance_rate_display, "caption": f"{compliant_readings} of {rated_readings} rated readings", "icon": "fa-chart-line", "tone": "green", "tab": "compliance"},
            {"label": "No readings yet", "value": max(analyses_with_result_access - analyses_with_results, 0), "caption": "Analyses without saved result rows", "icon": "fa-clipboard-question", "tone": "violet", "tab": "results", "filter": "empty"},
            {"label": "Not rated", "value": unrated_readings, "caption": "No applicable result limits configured", "icon": "fa-scale-unbalanced", "tone": "amber", "tab": "compliance"},
        ],
        "available_analysis_types": available_types,
        "analysis_type_series": [{"label": label, "value": count} for label, count in type_totals.most_common()],
        "analysis_series": analysis_series,
        "result_series": result_series,
        "parameter_series": parameter_series,
        "total_analyses": total_analyses,
        "new_analyses": new_analyses,
        "total_readings": total_readings,
        "failed_readings": failed_readings,
        "unrated_readings": unrated_readings,
        "compliant_readings": compliant_readings,
        "compliance_rate": compliance_rate,
        "compliance_rate_display": compliance_rate_display,
        "pending_readings": max(analyses_with_result_access - analyses_with_results, 0) if result_type_count else 0,
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
        "has_result_permission": bool(result_type_count),
        "workflow_description": "Scheduled tasks create analysis records from the configured sampling slots or existing sample register. Result cards contain only stored measurement values; expected or pending entries are not counted as readings.",
        "compliance_description": f"Compliance compares {result_state_label}. Unrated results remain visible without being treated as passing.",
        "result_state_label": result_state_label,
        "analysis_type_tabs": analysis_type_tabs or [],
        "selected_analysis_type": analysis_type or "",
        "live_url": f"/{app_label}/results/cards/",
        "app": {"name": app_label, "label": app_label.replace("_", " ").title()},
        "app_groups": permission_scoped_sidebar(request, registry, app_label),
    }
    return context
