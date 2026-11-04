from collections import Counter, defaultdict
from datetime import timedelta

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.views import View

from core.domain_dashboard import dashboard_context, model_list_url, period_days, render_domain_dashboard
from core.mixins import WanjalaMixin
from .models import (
    CatchmentParameter_qad,
    CWQParameter_qad,
    DistributionParameter_qad,
    GigiriParameter_qad,
    HeavyMetalParameter,
    KabeteParameter_qad,
    MicrobialParameter,
    MicrobialParameter_qad,
    PhysicochemicalParameter,
    WWQParameter_qad,
)


PARAMETER_MODELS = (
    ("Physicochemical", "Core water quality", PhysicochemicalParameter, "flask"),
    ("Microbial", "Core microbiology", MicrobialParameter, "bacteria"),
    ("Heavy metals", "Core metals", HeavyMetalParameter, "atom"),
    ("Wastewater standards", "NEMA standards", WWQParameter_qad, "industry"),
    ("Portable water standards", "KEBS standards", CWQParameter_qad, "droplet"),
    ("Distribution standards", "KEBS standards", DistributionParameter_qad, "diagram-project"),
    ("Gigiri standards", "Internal standards", GigiriParameter_qad, "city"),
    ("Kabete standards", "Internal standards", KabeteParameter_qad, "shield-halved"),
    ("Microbial standards", "Microbiology standards", MicrobialParameter_qad, "virus"),
    ("Catchment parameters", "Catchment monitoring", CatchmentParameter_qad, "mountain-sun"),
)


class DashboardView(WanjalaMixin, View):
    template_name = "parameters/dashboard.html"
    app_label = "parameters"

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        return render_domain_dashboard(request, self.template_name, self.app_label, self.get_context(request))

    def get_context(self, request):
        days = period_days(request)
        start = timezone.now() - timedelta(days=days)
        today = timezone.localdate()
        counts = []
        unit_counts = Counter()
        recent = []
        active_total = standardized_total = inactive_total = recent_total = 0
        daily_updates = defaultdict(int)

        for label, group, model, icon in PARAMETER_MODELS:
            queryset = model.objects.all()
            total = queryset.count()
            active = queryset.filter(is_active=True).count()
            inactive = total - active
            standardized = hasattr(model, "min_standard")
            if standardized:
                standardized_total += total
            active_total += active
            inactive_total += inactive
            counts.append({
                "label": label,
                "group": group,
                "count": total,
                "active": active,
                "inactive": inactive,
                "icon": icon,
                "url": model_list_url(self.app_label, model._meta.model_name),
                "standardized": standardized,
            })
            for unit, amount in queryset.values("unit").annotate(total=Count("pk")).values_list("unit", "total"):
                unit_counts[unit or "Unspecified"] += amount
            updates = queryset.filter(updated_at__gte=start)
            recent_total += updates.count()
            for row in updates.annotate(day=TruncDate("updated_at")).values("day").annotate(total=Count("pk")):
                daily_updates[row["day"]] += row["total"]
            for item in queryset.order_by("-updated_at")[:5]:
                recent.append((item.updated_at, {
                    "title": item.name,
                    "detail": f"{label} · {item.unit}",
                    "timestamp": timezone.localtime(item.updated_at).strftime("%d %b %H:%M"),
                    "icon": "sliders",
                    "color": "emerald" if item.is_active else "amber",
                }, {
                    "cells": [item.name, label, item.unit, f"{item.min_standard:g} – {item.max_standard:g}" if standardized else "No standard range", "Active" if item.is_active else "Inactive", timezone.localtime(item.updated_at).strftime("%d %b %Y %H:%M")]
                }))

        recent.sort(key=lambda value: value[0], reverse=True)
        recent_records = [row[1] for row in recent[:12]]
        table_rows = [row[2] for row in recent[:40]]
        chart_days = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
        if days > 90:
            week_values = defaultdict(int)
            for day, amount in daily_updates.items():
                week_values[day - timedelta(days=day.weekday())] += amount
            chart_days = sorted(week_values)
            update_values = [week_values[day] for day in chart_days]
        else:
            update_values = [daily_updates.get(day, 0) for day in chart_days]

        model_rows = [{
            "label": item["label"], "group": item["group"], "count": item["count"],
            "icon": item["icon"], "url": item["url"],
        } for item in counts]
        kpis = [
            {"label": "Active parameters", "value": active_total, "detail": "Enabled for operational entry", "icon": "circle-check", "color": "emerald", "tab": "models"},
            {"label": "Regulatory ranges", "value": standardized_total, "detail": "Parameters with configured min/max standards", "icon": "scale-balanced", "color": "blue", "tab": "analytics"},
            {"label": "Inactive records", "value": inactive_total, "detail": "Soft-disabled parameter definitions", "icon": "circle-pause", "color": "amber", "tab": "models"},
            {"label": "Updated in window", "value": recent_total, "detail": f"Configuration changes · {days} days", "icon": "clock-rotate-left", "color": "cyan", "tab": "analytics"},
            {"label": "Parameter families", "value": len(counts), "detail": "Actual parameter models in this app", "icon": "layer-group", "color": "purple", "tab": "models"},
            {"label": "Measurement units", "value": len(unit_counts), "detail": "Distinct units in the active catalog", "icon": "ruler-combined", "color": "rose", "tab": "analytics"},
        ]
        family_total = max(sum(item["count"] for item in counts), 1)
        ranked_rows = [
            {"label": item["label"], "detail": f"{item['active']} active · {item['inactive']} inactive", "value": item["count"], "percent": round(item["count"] * 100 / family_total), "color": "emerald" if item["standardized"] else "cyan"}
            for item in sorted(counts, key=lambda row: (-row["count"], row["label"]))
        ]
        context = dashboard_context(
            request,
            title="Water Quality Parameters",
            subtitle="Parameter catalog, unit coverage, and regulatory limit governance.",
            kicker="Standards & reference data",
            kpis=kpis,
            charts=[
                {"id": "parameter-families", "title": "Parameter family coverage", "caption": "Catalog volume across the live parameter models.", "type": "bar", "labels": [row["label"] for row in counts], "values": [row["count"] for row in counts], "color": "cyan"},
                {"id": "parameter-units", "title": "Unit footprint", "caption": "Stored parameter definitions grouped by measurement unit.", "type": "doughnut", "labels": list(unit_counts.keys())[:9], "values": list(unit_counts.values())[:9], "colors": ["cyan", "emerald", "amber", "rose", "purple", "blue", "slate"]},
                {"id": "parameter-updates", "title": "Configuration activity", "caption": "Saved parameter-definition edits during the selected window.", "type": "line", "labels": [day.strftime("%d %b") for day in chart_days], "values": update_values, "color": "purple"},
            ],
            signals=[
                {"label": "With numeric standards", "value": standardized_total, "detail": "Min/max regulatory fields are present", "icon": "scale-balanced", "color": "blue"},
                {"label": "Enabled for use", "value": active_total, "detail": "Active parameter records", "icon": "circle-check", "color": "emerald"},
                {"label": "Catalog disabled", "value": inactive_total, "detail": "Not available for new entries", "icon": "circle-pause", "color": "amber"},
                {"label": "Unit diversity", "value": len(unit_counts), "detail": "Distinct recorded units", "icon": "ruler-combined", "color": "cyan"},
            ],
            recent_records=recent_records,
            model_rows=model_rows,
            table_headings=["Parameter", "Family", "Unit", "Standard range", "Availability", "Last updated"],
            table_rows=table_rows,
            ranked_rows=ranked_rows,
            actions=[
                {"label": "Parameter catalog", "url": "/parameters/", "icon": "list-check"},
            ],
            overview_caption="The parameter catalog and reference ranges currently available to the water-quality workflows.",
            model_count=len(PARAMETER_MODELS),
        )
        return context
