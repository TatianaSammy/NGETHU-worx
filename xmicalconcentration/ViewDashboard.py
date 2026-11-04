from collections import defaultdict
from datetime import timedelta

from django.db.models import Avg, Count, Max, Min
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.views import View

from core.domain_dashboard import dashboard_context, model_list_url, period_days, render_domain_dashboard
from core.mixins import WanjalaMixin
from t.models import TreatmentPlant
from .models import XmicalConcentration


class DashboardView(WanjalaMixin, View):
    template_name = "xmicalconcentration/dashboard.html"
    app_label = "xmicalconcentration"

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        return render_domain_dashboard(request, self.template_name, self.app_label, self.get_context(request))

    def get_context(self, request):
        days = period_days(request)
        now = timezone.now()
        start = now - timedelta(days=days)
        today = timezone.localdate()
        plants, selected, is_superuser = self._plants(request)
        queryset = XmicalConcentration.objects.select_related("treatment_plant", "phase", "chemical", "tank", "measured_by", "created_by").filter(measurement_date__gte=start, measurement_date__lte=now)
        all_records = XmicalConcentration.objects.select_related("treatment_plant", "phase", "chemical", "tank", "measured_by", "created_by")
        if selected:
            queryset = queryset.filter(treatment_plant=selected)
            all_records = all_records.filter(treatment_plant=selected)
        elif not is_superuser:
            queryset = queryset.none()
            all_records = all_records.none()
        aggregate = queryset.aggregate(average=Avg("concentration_mg_per_l"), minimum=Min("concentration_mg_per_l"), maximum=Max("concentration_mg_per_l"), average_reading=Avg("hydrometer_reading"))
        today_count = queryset.filter(measurement_date__date=today).count()
        daily_rows = list(queryset.annotate(day=TruncDate("measurement_date")).values("day").annotate(average=Avg("concentration_mg_per_l"), count=Count("pk"), result_count=Count("concentration_mg_per_l")).order_by("day"))
        chemical_rows = list(queryset.values("chemical_name").annotate(average=Avg("concentration_mg_per_l"), count=Count("pk")).order_by("-count")[:12])
        tank_rows = list(queryset.values("tank__name").annotate(average=Avg("concentration_mg_per_l"), count=Count("pk")).order_by("-count")[:12])
        concentration_by_day = {row["day"]: row for row in daily_rows}
        dates = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
        labels = [day.strftime("%d %b") for day in dates]
        concentration_values = [float(concentration_by_day[day]["average"]) if day in concentration_by_day and concentration_by_day[day]["average"] is not None else None for day in dates]
        measurement_counts = [concentration_by_day[day]["count"] if day in concentration_by_day else 0 for day in dates]
        if days > 90:
            weekly = defaultdict(list)
            for day, row in concentration_by_day.items():
                weekly[day - timedelta(days=day.weekday())].append(row)
            dates = sorted(weekly)
            labels = [day.strftime("%d %b") for day in dates]
            concentration_values = [sum(float(row["average"]) * row["result_count"] for row in weekly[day] if row["average"] is not None) / max(sum(row["result_count"] for row in weekly[day]), 1) for day in dates]
            measurement_counts = [sum(row["count"] for row in weekly[day]) for day in dates]

        recent = list(queryset.order_by("-measurement_date")[:40])
        recent_records = [{
            "title": f"{item.chemical_name} · {float(item.concentration_mg_per_l):,.2f} mg/L" if item.concentration_mg_per_l is not None else f"{item.chemical_name} · concentration missing",
            "detail": f"{item.tank or 'Tank not set'} · {item.treatment_plant.name if item.treatment_plant else 'Plant not set'} · {item.measured_by.get_full_name() if item.measured_by else 'Analyst not recorded'}",
            "timestamp": timezone.localtime(item.measurement_date).strftime("%d %b %H:%M"),
            "icon": "vial-circle-check",
            "color": "cyan",
        } for item in recent[:12]]
        table_rows = [{"cells": [
            item.chemical_name,
            str(item.chemical or "—"),
            str(item.tank or "—"),
            str(item.treatment_plant or "—"),
            str(item.phase or "—"),
            f"{float(item.hydrometer_reading):.3f}" if item.hydrometer_reading is not None else "—",
            f"{float(item.concentration_mg_per_l):,.2f}" if item.concentration_mg_per_l is not None else "—",
            item.measured_by.get_full_name() if item.measured_by else "—",
            timezone.localtime(item.measurement_date).strftime("%d %b %Y %H:%M"),
        ]} for item in recent]
        model_rows = [{"label": "Chemical concentration measurements", "count": all_records.count(), "url": model_list_url(self.app_label, XmicalConcentration._meta.model_name), "icon": "vial-circle-check", "group": "Hydrometer analysis"}]
        count = queryset.count()
        concentration_values_only = queryset.exclude(concentration_mg_per_l__isnull=True)
        missing_values = count - concentration_values_only.count()
        kpis = [
            {"label": "Measurements", "value": count, "detail": f"Last {days} days", "icon": "vial-circle-check", "color": "cyan", "tab": "records"},
            {"label": "Today", "value": today_count, "detail": "Recorded today", "icon": "calendar-day", "color": "emerald", "tab": "records"},
            {"label": "Mean concentration", "value": f"{float(aggregate['average']):,.1f} mg/L" if aggregate["average"] is not None else "—", "detail": "Across measurements with a result", "icon": "gauge-high", "color": "blue", "tab": "analytics"},
            {"label": "Concentration range", "value": f"{float(aggregate['minimum']):,.1f}–{float(aggregate['maximum']):,.1f}" if aggregate["minimum"] is not None else "—", "detail": "Minimum to maximum mg/L", "icon": "arrows-left-right", "color": "purple", "tab": "analytics"},
            {"label": "Chemicals", "value": queryset.values("chemical_name").distinct().count(), "detail": "Distinct names in the selected window", "icon": "flask", "color": "amber", "tab": "models"},
            {"label": "Incomplete readings", "value": missing_values, "detail": "Missing calculated concentration", "icon": "circle-exclamation", "color": "rose", "tab": "records"},
        ]
        concentration_chart = [
            {"id": "concentration-trend", "title": "Concentration trend", "caption": "Daily mean concentration from saved measurement values.", "type": "line", "labels": labels, "values": concentration_values, "color": "cyan"},
            {"id": "concentration-chemical", "title": "Chemical profile", "caption": "Mean concentration by chemical name.", "type": "bar", "labels": [row["chemical_name"] for row in chemical_rows], "values": [float(row["average"] or 0) for row in chemical_rows], "color": "blue"},
            {"id": "concentration-tank", "title": "Tank profile", "caption": "Measurement count by tank.", "type": "horizontal", "labels": [row["tank__name"] or "Tank not set" for row in tank_rows], "values": [row["count"] for row in tank_rows], "color": "purple"},
        ]
        context = dashboard_context(
            request,
            title="Chemical Concentration Control",
            subtitle="Hydrometer readings, calculated concentration, tank profile, and traceable measurement history.",
            kicker="Chemical preparation analytics",
            kpis=kpis,
            charts=concentration_chart,
            signals=[
                {"label": "Mean hydrometer reading", "value": f"{float(aggregate['average_reading']):.3f}" if aggregate["average_reading"] is not None else "—", "detail": "Selected measurement window", "icon": "ruler-vertical", "color": "blue"},
                {"label": "Recorded chemical/tank pairs", "value": queryset.values("chemical_name", "tank_id").distinct().count(), "detail": "Distinct operating combinations", "icon": "layer-group", "color": "purple"},
                {"label": "Concentration values", "value": concentration_values_only.count(), "detail": "Measurements with calculated mg/L", "icon": "circle-check", "color": "emerald"},
                {"label": "Missing concentration", "value": missing_values, "detail": "Needs completion before use", "icon": "triangle-exclamation", "color": "amber"},
            ],
            recent_records=recent_records,
            model_rows=model_rows,
            table_headings=["Chemical", "Catalog item", "Tank", "Plant", "Phase", "Hydrometer", "Concentration mg/L", "Analyst", "Recorded"],
            table_rows=table_rows,
            ranked_rows=[{"label": row["chemical_name"], "detail": f"{row['count']} measurements", "value": f"{float(row['average'] or 0):,.1f} mg/L", "percent": round(row["count"] * 100 / max(count, 1)), "color": "cyan"} for row in chemical_rows],
            actions=[{"label": "Measurement records", "url": "/xmicalconcentration/", "icon": "vial-circle-check"}],
            overview_caption="Dashboard aggregates saved hydrometer readings and concentration values. It does not infer operational limits where none are configured.",
            use_plant_scope=True,
            model_count=1,
        )
        context.update({"plant_options": plants, "selected_plant": selected, "show_plant_selector": is_superuser})
        return context

    @staticmethod
    def _plants(request):
        user = request.user
        if user.is_superuser:
            plants = TreatmentPlant.objects.order_by("name")
            requested = request.GET.get("plant")
            return plants, plants.filter(pk=requested).first() if requested and requested.isdigit() else None, True
        plant_id = getattr(user, "assigned_treatment_plant_id", None)
        plants = TreatmentPlant.objects.filter(pk=plant_id) if plant_id else TreatmentPlant.objects.none()
        return plants.order_by("name"), plants.first(), False
