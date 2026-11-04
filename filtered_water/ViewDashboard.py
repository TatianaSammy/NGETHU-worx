"""Plant-scoped filter fleet dashboard and filter performance workspaces."""

from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.utils import timezone
from django.views import View

from core.mixins import WanjalaMixin
from core.registry import registry
from t.models import TreatmentPlant, WaterFilter, FilteredWaterLimit

from .models import (
    FilteredWaterAnalysis,
    FilteredWaterAnalysisState,
    FilteredWaterResults,
)

ALLOWED_PERIODS = (7, 30, 90, 365)
NEAR_LIMIT_FRACTION = Decimal("0.10")


def _period_days(request):
    try:
        value = int(request.GET.get("days", 30))
    except (TypeError, ValueError):
        return 30
    return value if value in ALLOWED_PERIODS else 30


def _plants_for_user(user):
    if user.is_superuser:
        return TreatmentPlant.objects.all().order_by("name")
    return TreatmentPlant.objects.filter(assigned_users=user).order_by("name")


def _select_plant(request, plants):
    requested = request.GET.get("plant")
    if requested:
        try:
            selected = plants.filter(pk=int(requested)).first()
            if selected:
                return selected
        except (TypeError, ValueError):
            pass
    assigned_id = getattr(request.user, "assigned_treatment_plant_id", None)
    return plants.filter(pk=assigned_id).first() if assigned_id else plants.first()


def _filters_for_plant(plant):
    if not plant:
        return WaterFilter.objects.none()
    return (
        WaterFilter.objects.filter(
            Q(treatment_plant=plant, phase__isnull=True)
            | Q(treatment_plant=plant, phase__treatment_plant=plant)
            | Q(treatment_plant__isnull=True, phase__treatment_plant=plant)
        )
        .select_related("filter_type", "treatment_plant", "phase", "phase__treatment_plant", "filter_status")
        .order_by("name", "pk")
    )


def _display_user(user):
    if not user:
        return "Not recorded"
    return user.get_full_name().strip() or user.get_username()


def _decimal(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _evaluate_value(value, minimum, maximum):
    """Return a measured value's limit condition and normalized edge proximity."""
    value = _decimal(value)
    minimum = _decimal(minimum)
    maximum = _decimal(maximum)
    if value is None or (minimum is None and maximum is None):
        return {"condition": "unknown", "within_limits": None, "near_limit": False}

    if minimum is not None and value < minimum:
        return {"condition": "out_of_range", "within_limits": False, "near_limit": False}
    if maximum is not None and value > maximum:
        return {"condition": "out_of_range", "within_limits": False, "near_limit": False}

    if minimum is not None and maximum is not None and maximum > minimum:
        margin = min(value - minimum, maximum - value)
        near_limit = margin <= (maximum - minimum) * NEAR_LIMIT_FRACTION
    else:
        boundary = minimum if minimum is not None else maximum
        scale = max(abs(boundary), abs(value), Decimal("1"))
        margin = value - boundary if minimum is not None else boundary - value
        near_limit = margin <= scale * NEAR_LIMIT_FRACTION

    return {
        "condition": "near_limit" if near_limit else "within_limits",
        "within_limits": True,
        "near_limit": near_limit,
    }


def _limit_text(minimum, maximum):
    if minimum is not None and maximum is not None:
        return f"{minimum:g} – {maximum:g}"
    if minimum is not None:
        return f"≥ {minimum:g}"
    if maximum is not None:
        return f"≤ {maximum:g}"
    return "No limit configured"


def _latest_readings(filters, plant):
    """Return the latest non-empty result for each filter parameter limit."""
    filter_ids = [item.pk for item in filters]
    if not filter_ids or not plant:
        return {}

    rows = (
        FilteredWaterResults.objects.filter(
            water_filter_id__in=filter_ids,
            analysis__treatment_plant=plant,
            result_value__isnull=False,
        )
        .select_related(
            "analysis",
            "analysis__analysed_by",
            "analysis__treatment_plant_phase",
            "water_filter",
            "parameter",
            "parameter__parameter",
        )
        .order_by("water_filter_id", "parameter_id", "-analysis__timestamp", "-created_at", "-pk")
    )

    latest = defaultdict(list)
    seen = set()
    for result in rows.iterator(chunk_size=1000):
        key = (result.water_filter_id, result.parameter_id)
        if key in seen:
            continue
        seen.add(key)
        limit = result.parameter
        evaluation = _evaluate_value(result.result_value, limit.min_value, limit.max_value)
        analyst = _display_user(result.analysis.analysed_by)
        recorded_at = timezone.localtime(result.analysis.timestamp)
        latest[result.water_filter_id].append({
            "limit_id": limit.pk,
            "name": limit.parameter.name,
            "unit": getattr(limit.parameter, "unit", "") or "",
            "value": float(result.result_value),
            "value_text": f"{result.result_value:g}",
            "minimum": float(limit.min_value) if limit.min_value is not None else None,
            "maximum": float(limit.max_value) if limit.max_value is not None else None,
            "limit_text": _limit_text(limit.min_value, limit.max_value),
            "condition": evaluation["condition"],
            "within_limits": evaluation["within_limits"],
            "near_limit": evaluation["near_limit"],
            "recorded_at": recorded_at.strftime("%d %b %Y · %H:%M"),
            "recorded_iso": recorded_at.isoformat(),
            "analyst": analyst,
            "analysis_state": result.analysis.get_state_display(),
            "analysis_uuid": str(result.analysis.uuid),
        })
    return latest


def _filter_health(water_filter, readings):
    excursions = sum(reading["condition"] == "out_of_range" for reading in readings)
    near_limits = sum(reading["condition"] == "near_limit" for reading in readings)
    unknown = sum(reading["condition"] == "unknown" for reading in readings)

    status = (water_filter.status or "unknown").lower()
    if status in {"isolated", "maintenance"}:
        tone, label, reason = "red", "OPERATIONAL HOLD", water_filter.get_status_display()
    elif excursions:
        tone, label, reason = "red", "OUT OF LIMIT", f"{excursions} parameter result(s) outside configured limits."
    elif status == "backwashing":
        tone, label, reason = "orange", "BACKWASHING", "Filter is in a backwash cycle."
    elif not readings:
        tone, label, reason = "orange", "NO CURRENT DATA", "No non-empty parameter results have been recorded."
    elif near_limits or unknown:
        tone, label = "orange", "WATCH"
        reason = f"{near_limits} near a limit · {unknown} without a configured range."
    else:
        tone, label, reason = "green", "WITHIN LIMITS", "All latest measured parameters are within a healthy margin."

    most_recent = max(readings, key=lambda reading: reading["recorded_iso"]) if readings else None
    return {
        "tone": tone,
        "label": label,
        "reason": reason,
        "excursions": excursions,
        "near_limits": near_limits,
        "unknown": unknown,
        "readings": readings,
        "reading_count": len(readings),
        "last_recorded": most_recent["recorded_at"] if most_recent else "No result recorded",
        "analyst": most_recent["analyst"] if most_recent else "Not recorded",
    }


def _filter_card(water_filter, readings):
    health = _filter_health(water_filter, readings)
    capacity = _decimal(water_filter.capacity)
    flow_rate = _decimal(water_filter.flow_rate)
    utilization = None
    if capacity and capacity > 0 and flow_rate is not None:
        utilization = float((flow_rate / capacity) * Decimal("100"))

    return {
        "id": water_filter.pk,
        "name": water_filter.name,
        "plant": str(water_filter.treatment_plant or water_filter.phase.treatment_plant),
        "type": str(water_filter.filter_type),
        "phase": str(water_filter.phase) if water_filter.phase_id else "Phase not assigned",
        "location": water_filter.location,
        "status": water_filter.get_status_display(),
        "status_key": water_filter.status,
        "flow_rate": float(flow_rate) if flow_rate is not None else None,
        "capacity": float(capacity) if capacity is not None else None,
        "utilization": utilization,
        "utilization_bar": min(max(utilization, 0), 100) if utilization is not None else 0,
        "tone": health["tone"],
        "health_label": health["label"],
        "health_reason": health["reason"],
        "excursions": health["excursions"],
        "near_limits": health["near_limits"],
        "unknown": health["unknown"],
        "readings": health["readings"],
        "reading_count": health["reading_count"],
        "last_recorded": health["last_recorded"],
        "analyst": health["analyst"],
    }


def _period_results(plant, water_filter=None, start=None):
    if not plant:
        return FilteredWaterResults.objects.none()
    results = FilteredWaterResults.objects.filter(
        analysis__treatment_plant=plant,
        result_value__isnull=False,
    )
    if water_filter is not None:
        results = results.filter(water_filter=water_filter)
    if start:
        results = results.filter(analysis__timestamp__gte=start)
    return results.select_related(
        "analysis", "analysis__analysed_by", "parameter", "parameter__parameter", "water_filter"
    )


class DashboardView(WanjalaMixin, View):
    template_name = "filtered_water/dashboard.html"
    app_label = "filtered_water"

    def get(self, request):
        if (response := self.process_request(request)):
            return response
        wants_json = "application/json" in request.headers.get("Accept", "") or request.GET.get("format") == "json"
        context = self.get_context(request)
        if wants_json:
            return JsonResponse(context.get("dashboard_json_payload", {}), safe=False)
        return HttpResponse(render_to_string(self.template_name, context, request=request))

    def get_context(self, request):
        now = timezone.now()
        period_days = _period_days(request)
        local_now = timezone.localtime(now)
        start_date = local_now.date() - timedelta(days=period_days - 1)
        start = timezone.make_aware(
            datetime.combine(start_date, time.min),
            timezone.get_current_timezone(),
        )
        plants = _plants_for_user(request.user)
        plant = _select_plant(request, plants)
        filters = list(_filters_for_plant(plant))
        latest = _latest_readings(filters, plant)
        filter_cards = [_filter_card(item, latest.get(item.pk, [])) for item in filters]

        analyses = FilteredWaterAnalysis.objects.none()
        if plant:
            analyses = FilteredWaterAnalysis.objects.filter(
                treatment_plant=plant,
                timestamp__gte=start,
            )
        total_analyses = analyses.count()
        verified_count = analyses.filter(state=FilteredWaterAnalysisState.VERIFIED.value).count()
        pending_verification = analyses.filter(state=FilteredWaterAnalysisState.ANALYSED.value).count()
        new_count = analyses.filter(state=FilteredWaterAnalysisState.NEW.value).count()

        results = _period_results(plant, start=start)
        compliant_count = 0
        non_compliant_count = 0
        near_count = 0
        unknown_count = 0
        result_count = 0
        daily_result_counts = defaultdict(lambda: {"within": 0, "near": 0, "out": 0, "unknown": 0})
        per_parameter = defaultdict(lambda: {"name": "", "within": 0, "near": 0, "out": 0, "unknown": 0})
        for result in results.iterator(chunk_size=1000):
            limit = result.parameter
            evaluation = _evaluate_value(result.result_value, limit.min_value, limit.max_value)
            recorded_date = timezone.localtime(result.analysis.timestamp).date().isoformat()
            stats = daily_result_counts[recorded_date]
            parameter_stats = per_parameter[limit.pk]
            parameter_stats["name"] = limit.parameter.name
            result_count += 1
            if evaluation["within_limits"] is False:
                non_compliant_count += 1
                stats["out"] += 1
                parameter_stats["out"] += 1
            elif evaluation["near_limit"]:
                compliant_count += 1
                near_count += 1
                stats["near"] += 1
                parameter_stats["near"] += 1
            elif evaluation["within_limits"] is None:
                unknown_count += 1
                stats["unknown"] += 1
                parameter_stats["unknown"] += 1
            else:
                compliant_count += 1
                stats["within"] += 1
                parameter_stats["within"] += 1

        evaluated_count = compliant_count + non_compliant_count
        compliance_rate = round(compliant_count * 100 / evaluated_count, 1) if evaluated_count else None
        tone_counts = {tone: sum(card["tone"] == tone for card in filter_cards) for tone in ("green", "orange", "red")}
        working = sum(card["status_key"] == "working" for card in filter_cards)
        backwashing = sum(card["status_key"] == "backwashing" for card in filter_cards)
        offline = sum(card["status_key"] in {"isolated", "maintenance"} for card in filter_cards)
        kpis = [
            {"label": "Filters in scope", "value": len(filters), "icon": "fa-filter", "color": "cyan", "detail": str(plant or "No assigned plant")},
            {"label": "Healthy signals", "value": tone_counts["green"], "icon": "fa-circle-check", "color": "emerald", "detail": "Latest results in healthy margins"},
            {"label": "Watch signals", "value": tone_counts["orange"], "icon": "fa-triangle-exclamation", "color": "amber", "detail": "Near limits, backwash or missing data"},
            {"label": "Critical signals", "value": tone_counts["red"], "icon": "fa-circle-exclamation", "color": "rose", "detail": "Excursion or operational hold"},
            {"label": "Period compliance", "value": f"{compliance_rate:.1f}%" if compliance_rate is not None else "—", "icon": "fa-shield-halved", "color": "emerald" if compliance_rate is not None and compliance_rate >= 95 else "amber", "detail": f"{evaluated_count} results · {period_days} days"},
            {"label": "Analysis workflow", "value": total_analyses, "icon": "fa-vials", "color": "blue", "detail": f"{verified_count} verified · {pending_verification} awaiting QA"},
        ]

        daily_labels = [(local_now.date() - timedelta(days=offset)).isoformat() for offset in range(period_days - 1, -1, -1)]
        parameter_rows = []
        for metrics in per_parameter.values():
            count = metrics["within"] + metrics["near"] + metrics["out"]
            parameter_rows.append({
                "name": metrics["name"],
                "compliance": round((metrics["within"] + metrics["near"]) * 100 / count, 1) if count else None,
                "readings": count + metrics["unknown"],
                "excursions": metrics["out"],
                "unbounded": metrics["unknown"],
            })
        parameter_rows.sort(key=lambda row: (row["compliance"] if row["compliance"] is not None else -1, -row["readings"], row["name"]))

        charts_payload = {
            "period_days": period_days,
            "plant": str(plant) if plant else "No plant assigned",
            "daily_results": {
                "labels": daily_labels,
                "within": [daily_result_counts[day]["within"] for day in daily_labels],
                "near": [daily_result_counts[day]["near"] for day in daily_labels],
                "out": [daily_result_counts[day]["out"] for day in daily_labels],
                "unknown": [daily_result_counts[day]["unknown"] for day in daily_labels],
            },
            "filter_health": {"labels": ["Healthy", "Watch", "Critical"], "data": [tone_counts["green"], tone_counts["orange"], tone_counts["red"]]},
            "parameter_performance": parameter_rows,
            "analysis_states": {"labels": ["Verified", "Analysed", "New"], "data": [verified_count, pending_verification, new_count]},
        }

        recent_analyses = []
        for analysis in analyses.select_related("treatment_plant", "treatment_plant_phase", "analysed_by").order_by("-timestamp")[:12]:
            recent_analyses.append({
                "uuid": str(analysis.uuid)[:8],
                "plant": str(analysis.treatment_plant),
                "phase": str(analysis.treatment_plant_phase),
                "state": analysis.state,
                "analyst": _display_user(analysis.analysed_by),
                "date": timezone.localtime(analysis.timestamp).strftime("%Y-%m-%d %H:%M"),
            })

        try:
            entries = registry.for_app(self.app_label) or []
        except Exception:
            entries = []

        return {
            "dashboard_title": "Filter Operations",
            "dashboard_subtitle": f"Plant-scoped Filter Performance · {period_days}d",
            "dashboard_icon": "filter",
            "app_groups": registry.sidebar(self.app_label) if entries else [],
            "plants": plants,
            "plant": plant,
            "filters": filter_cards,
            "kpis": kpis,
            "recent_analyses": recent_analyses,
            "period_days": period_days,
            "allowed_periods": ALLOWED_PERIODS,
            "total_analyses": total_analyses,
            "verified_count": verified_count,
            "pending_verification": pending_verification,
            "new_count": new_count,
            "working_filters": working,
            "backwashing_filters": backwashing,
            "offline_filters": offline,
            "total_filters": len(filters),
            "result_count": result_count,
            "near_count": near_count,
            "unknown_count": unknown_count,
            "non_compliant_count": non_compliant_count,
            "compliance_rate": compliance_rate,
            "parameter_rows": parameter_rows,
            "charts_payload": charts_payload,
            "dashboard_json_payload": charts_payload,
        }


class FilterDetailView(WanjalaMixin, View):
    template_name = "filtered_water/filter_detail.html"

    def get(self, request, filter_id):
        if (response := self.process_request(request)):
            return response
        plants = _plants_for_user(request.user)
        plant = _select_plant(request, plants)
        water_filter = _filters_for_plant(plant).filter(pk=filter_id).first()
        if not water_filter:
            raise Http404("Filter not found in your plant scope.")

        period_days = _period_days(request)
        now = timezone.now()
        start = now - timedelta(days=period_days)
        limits = list(
            FilteredWaterLimit.objects.filter(treatment_plant=plant, water_filter=water_filter)
            .select_related("parameter")
            .order_by("parameter__name", "pk")
        )
        all_latest = _latest_readings([water_filter], plant).get(water_filter.pk, [])
        latest_by_limit = {reading["limit_id"]: reading for reading in all_latest}
        rows_by_limit = {
            limit.pk: {
                "limit": limit,
                "name": limit.parameter.name,
                "unit": getattr(limit.parameter, "unit", "") or "",
                "minimum": float(limit.min_value) if limit.min_value is not None else None,
                "maximum": float(limit.max_value) if limit.max_value is not None else None,
                "limit_text": _limit_text(limit.min_value, limit.max_value),
                "history": [],
            }
            for limit in limits
        }

        history = []
        results = _period_results(plant, water_filter=water_filter, start=start).order_by("analysis__timestamp", "created_at", "pk")
        for result in results.iterator(chunk_size=1000):
            limit = result.parameter
            row = rows_by_limit.get(limit.pk)
            if row is None:
                continue
            recorded_at = timezone.localtime(result.analysis.timestamp)
            evaluation = _evaluate_value(result.result_value, limit.min_value, limit.max_value)
            value = float(result.result_value)
            entry = {
                "date": recorded_at.strftime("%d %b %Y · %H:%M"),
                "timestamp": recorded_at.isoformat(),
                "value": value,
                "analyst": _display_user(result.analysis.analysed_by),
                "state": result.analysis.get_state_display(),
                "uuid": str(result.analysis.uuid),
                "within_limits": evaluation["within_limits"],
                "condition": evaluation["condition"],
            }
            row["history"].append(entry)
            history.append({**entry, "parameter_id": limit.pk, "parameter": limit.parameter.name})

        matrix = []
        performance_labels = []
        performance_values = []
        for limit_id, row in rows_by_limit.items():
            samples = row["history"]
            values = [sample["value"] for sample in samples]
            compliant = sum(sample["within_limits"] is True for sample in samples)
            excursions = sum(sample["within_limits"] is False for sample in samples)
            unbounded = sum(sample["within_limits"] is None for sample in samples)
            latest = latest_by_limit.get(limit_id)
            row.update({
                "latest": latest,
                "samples": len(samples),
                "average": sum(values) / len(values) if values else None,
                "minimum_observed": min(values) if values else None,
                "maximum_observed": max(values) if values else None,
                "compliance": round(compliant * 100 / (compliant + excursions), 1) if compliant + excursions else None,
                "excursions": excursions,
                "unbounded": unbounded,
            })
            matrix.append(row)
            if row["compliance"] is not None:
                performance_labels.append(row["name"])
                performance_values.append(row["compliance"])

        health = _filter_health(water_filter, all_latest)
        tested_count = sum(row["samples"] for row in matrix)
        valid_count = sum(sum(sample["within_limits"] is not None for sample in row["history"]) for row in matrix)
        compliant_count = sum(sum(sample["within_limits"] is True for sample in row["history"]) for row in matrix)
        compliance_rate = round(compliant_count * 100 / valid_count, 1) if valid_count else None
        flow = _decimal(water_filter.flow_rate)
        capacity = _decimal(water_filter.capacity)
        utilization = float(flow / capacity * Decimal("100")) if flow is not None and capacity and capacity > 0 else None
        filter_status = getattr(water_filter, "filter_status", None)
        last_maintenance_date = filter_status.last_maintenance_date if filter_status else None
        days_since_service = (now.date() - last_maintenance_date).days if last_maintenance_date else None

        trend_payload = {
            "parameters": [
                {
                    "id": row["limit"].pk,
                    "name": row["name"],
                    "unit": row["unit"],
                    "minimum": row["minimum"],
                    "maximum": row["maximum"],
                    "labels": [sample["timestamp"] for sample in row["history"]],
                    "values": [sample["value"] for sample in row["history"]],
                }
                for row in matrix
            ],
            "performance": {"labels": performance_labels, "values": performance_values},
            "capacity": {"labels": ["Current flow", "Rated capacity"], "values": [float(flow) if flow is not None else 0, float(capacity) if capacity is not None else 0]},
        }

        context = {
            "dashboard_title": water_filter.name,
            "dashboard_subtitle": f"Filter Performance Workspace · {plant}",
            "dashboard_icon": "filter",
            "water_filter": water_filter,
            "plant": plant,
            "period_days": period_days,
            "allowed_periods": ALLOWED_PERIODS,
            "health": health,
            "matrix": matrix,
            "history": list(reversed(history[-150:])),
            "sample_count": tested_count,
            "compliance_rate": compliance_rate,
            "utilization": utilization,
            "days_since_service": days_since_service,
            "maintenance_due": bool(filter_status and filter_status.maintenance_due),
            "filter_status": filter_status,
            "last_maintenance_date": last_maintenance_date,
            "trend_payload": trend_payload,
        }
        return HttpResponse(render_to_string(self.template_name, context, request=request))
