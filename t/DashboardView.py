from collections import Counter
from datetime import timedelta

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.views import View

from core.domain_dashboard import dashboard_context, model_list_url, period_days, render_domain_dashboard
from core.mixins import WanjalaMixin
from .models import (
    ClarifiedWaterLimit,
    Clarifier,
    CoagulatedWaterLimit,
    DisludgeValve,
    FilterStatus,
    FilterStatusEvent,
    FilteredWaterLimit,
    HeavyMetalLimit,
    MicrobialLimit,
    RawWaterLimit,
    TreatedWaterLimit,
    TreatmentPlant,
    TreatmentPlantPhase,
    WaterFilter,
)


LIMIT_MODELS = (RawWaterLimit, CoagulatedWaterLimit, ClarifiedWaterLimit, FilteredWaterLimit, TreatedWaterLimit, MicrobialLimit, HeavyMetalLimit)


class DashboardView(WanjalaMixin, View):
    template_name = "t/dashboard.html"
    app_label = "t"

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        return render_domain_dashboard(request, self.template_name, self.app_label, self.get_context(request))

    def get_context(self, request):
        days = period_days(request)
        today = timezone.localdate()
        start_date = today - timedelta(days=days - 1)
        plant_options, selected_plant, is_superuser = self._plants(request)
        plants = TreatmentPlant.objects.all()
        phases = TreatmentPlantPhase.objects.select_related("treatment_plant")
        filters = WaterFilter.objects.select_related("filter_type", "treatment_plant", "phase", "filter_status")
        statuses = FilterStatus.objects.select_related("water_filter", "water_filter__treatment_plant", "water_filter__phase")
        clarifiers = Clarifier.objects.select_related("treatment_plant", "phase")
        valves = DisludgeValve.objects.select_related("clarifier", "status", "clarifier__treatment_plant")
        events = FilterStatusEvent.objects.select_related("filter_status", "filter_status__water_filter", "performed_by")
        if selected_plant:
            plants = plants.filter(pk=selected_plant.pk)
            phases = phases.filter(treatment_plant=selected_plant)
            filters = filters.filter(treatment_plant=selected_plant)
            statuses = statuses.filter(water_filter__treatment_plant=selected_plant)
            clarifiers = clarifiers.filter(treatment_plant=selected_plant)
            valves = valves.filter(clarifier__treatment_plant=selected_plant)
            events = events.filter(filter_status__water_filter__treatment_plant=selected_plant)
        elif not is_superuser:
            plants = plants.none()
            phases = phases.none()
            filters = filters.none()
            statuses = statuses.none()
            clarifiers = clarifiers.none()
            valves = valves.none()
            events = events.none()

        filter_rows = list(statuses)
        status_counts = Counter(row.status for row in filter_rows)
        maintenance_due = [row for row in filter_rows if row.maintenance_due]
        condition_values = [float(row.condition_percent) for row in filter_rows if row.condition_percent is not None]
        condition_average = round(sum(condition_values) / len(condition_values), 1) if condition_values else None
        operational_capacity = phases.aggregate(total=Sum("operational_capacity"))["total"] or 0
        design_capacity = phases.aggregate(total=Sum("design_capacity"))["total"] or 0
        flow_total = sum(float(row.flow_rate or 0) for row in filters)
        filter_capacity = sum(float(row.capacity or 0) for row in filters)
        capacity_utilization = round(flow_total * 100 / filter_capacity, 1) if filter_capacity else None
        limit_counts = [(model, model.objects.filter(treatment_plant__in=plants).count()) for model in LIMIT_MODELS]
        limit_total = sum(count for _model, count in limit_counts)
        event_window = events.filter(recorded_at__date__gte=start_date, recorded_at__date__lte=today)
        status_rows = list(event_window.order_by("-recorded_at")[:12])
        event_trend = self._daily_events(event_window, days)

        phase_rows = list(phases.annotate(filter_count=Count("filters", distinct=True)).order_by("treatment_plant__name", "name"))
        phase_labels = [f"{phase.treatment_plant.code} · {phase.name}" for phase in phase_rows[:12]]
        phase_values = [round(float(phase.operational_capacity or 0), 1) for phase in phase_rows[:12]]
        model_specs = [
            ("Treatment plants", plants.count(), "treatmentplant", "industry", "Network"),
            ("Treatment phases", phases.count(), "treatmentplantphase", "layer-group", "Process topology"),
            ("Water filters", filters.count(), "waterfilter", "filter", "Filter fleet"),
            ("Filter statuses", statuses.count(), "filterstatus", "circle-dot", "Asset readiness"),
            ("Clarifiers", clarifiers.count(), "clarifier", "water", "Clarification assets"),
            ("Disludge valves", valves.count(), "disludgevalve", "valve", "Clarifier assets"),
        ]
        model_rows = [{"label": label, "count": count, "url": model_list_url(self.app_label, model), "icon": icon, "group": group} for label, count, model, icon, group in model_specs]
        recent_records = [{
            "title": f"{event.filter_status.water_filter.name} · {event.get_status_display()}",
            "detail": f"{event.event_type.replace('_', ' ').title()} · {event.filter_status.water_filter.treatment_plant.name if event.filter_status.water_filter.treatment_plant else 'Plant not set'}",
            "timestamp": timezone.localtime(event.recorded_at).strftime("%d %b %H:%M"),
            "icon": "filter",
            "color": "emerald" if event.status == FilterStatus.Status.WORKING else "amber" if event.status in {FilterStatus.Status.BACKWASHING, FilterStatus.Status.MAINTENANCE} else "rose",
        } for event in status_rows]
        table_rows = [{"cells": [
            row.water_filter.name,
            row.water_filter.treatment_plant.name if row.water_filter.treatment_plant else "Unassigned",
            row.water_filter.phase.name if row.water_filter.phase else "Plant-wide",
            row.get_status_display(),
            f"{row.condition_percent}%" if row.condition_percent is not None else "Not reported",
            row.effective_next_maintenance_date.strftime("%d %b %Y") if row.effective_next_maintenance_date else "Not scheduled",
        ]} for row in sorted(filter_rows, key=lambda item: (not item.maintenance_due, item.effective_next_maintenance_date or today))[:100]]
        total_filters = len(filter_rows)
        healthy_pct = round(status_counts.get(FilterStatus.Status.WORKING, 0) * 100 / total_filters) if total_filters else 0
        kpis = [
            {"label": "Treatment plants", "value": plants.count(), "detail": "Plants within your authorized scope", "icon": "industry", "color": "blue", "tab": "models"},
            {"label": "Treatment phases", "value": phases.count(), "detail": f"{design_capacity:,.0f} m³/hr design capacity", "icon": "layer-group", "color": "cyan", "tab": "analytics"},
            {"label": "Filter fleet", "value": total_filters, "detail": f"{healthy_pct}% currently working", "icon": "filter", "color": "emerald", "tab": "records"},
            {"label": "Maintenance due", "value": len(maintenance_due), "detail": "Calculated from filter maintenance plans", "icon": "screwdriver-wrench", "color": "amber", "tab": "records"},
            {"label": "Clarifiers", "value": clarifiers.count(), "detail": f"{valves.count()} associated valves", "icon": "water", "color": "blue", "tab": "models"},
            {"label": "Configured limits", "value": limit_total, "detail": "Across raw, treated, filtered and other stages", "icon": "sliders", "color": "purple", "tab": "analytics"},
        ]
        total_capacity = sum(phase_values) or 1
        ranked_rows = [{
            "label": f"{phase.treatment_plant.code} · {phase.name}",
            "detail": f"{float(phase.operational_capacity or 0):,.1f} of {float(phase.design_capacity or 0):,.1f} m³/hr",
            "value": f"{float(phase.operational_capacity or 0):,.1f} m³/hr",
            "percent": min(100, round(float(phase.operational_capacity or 0) * 100 / total_capacity)),
            "color": "cyan",
        } for phase in sorted(phase_rows, key=lambda item: float(item.operational_capacity or 0), reverse=True)[:12]]
        context = dashboard_context(
            request,
            title="Treatment Plant Network",
            subtitle="Process capacity, filter readiness, clarifier assets, and plant-specific water-quality limits.",
            kicker="Treatment assets & process topology",
            kpis=kpis,
            charts=[
                {"id": "t-filter-status", "title": "Filter fleet status", "caption": "Current state from the FilterStatus records.", "type": "doughnut", "labels": [label.title() for label in ["working", "backwashing", "maintenance", "isolated", "fault", "out_of_service"]], "values": [status_counts.get(status, 0) for status in ["working", "backwashing", "maintenance", "isolated", "fault", "out_of_service"]], "colors": ["emerald", "cyan", "amber", "slate", "rose", "purple"]},
                {"id": "t-phase-capacity", "title": "Operational capacity by phase", "caption": "Configured operational capacity in m³/hr.", "type": "bar", "labels": phase_labels, "values": phase_values, "color": "blue"},
                {"id": "t-status-events", "title": "Filter status activity", "caption": "Recorded filter-state changes in the selected window.", "type": "line", "labels": event_trend["labels"], "values": event_trend["values"], "color": "emerald"},
            ],
            signals=[
                {"label": "Working filters", "value": status_counts.get(FilterStatus.Status.WORKING, 0), "detail": "FilterStatus = working", "icon": "circle-check", "color": "emerald"},
                {"label": "Backwashing / maintenance", "value": status_counts.get(FilterStatus.Status.BACKWASHING, 0) + status_counts.get(FilterStatus.Status.MAINTENANCE, 0), "detail": "Temporary process restrictions", "icon": "arrows-rotate", "color": "amber"},
                {"label": "Average condition", "value": f"{condition_average}%" if condition_average is not None else "—", "detail": "Mean condition score where reported", "icon": "gauge-high", "color": "cyan"},
                {"label": "Filter flow utilization", "value": f"{capacity_utilization}%" if capacity_utilization is not None else "—", "detail": f"{flow_total:,.1f} / {filter_capacity:,.1f} m³/hr", "icon": "chart-line", "color": "blue"},
                {"label": "Process capacity", "value": f"{operational_capacity:,.0f} m³/hr", "detail": f"Design: {design_capacity:,.0f} m³/hr", "icon": "water", "color": "purple"},
                {"label": "Water-quality limits", "value": limit_total, "detail": f"Across {len(LIMIT_MODELS)} limit families", "icon": "shield-check", "color": "blue"},
            ],
            recent_records=recent_records,
            model_rows=model_rows + [{"label": model._meta.verbose_name_plural.title(), "count": count, "url": model_list_url(self.app_label, model._meta.model_name), "icon": "sliders", "group": "Water quality limits"} for model, count in limit_counts],
            table_headings=["Filter", "Plant", "Phase", "Current status", "Condition", "Next maintenance"],
            table_rows=table_rows,
            ranked_rows=ranked_rows,
            actions=[{"label": "Filter performance", "url": "/filtered_water/", "icon": "filter"}],
            overview_caption=f"{len(plant_options) if is_superuser and not selected_plant else (1 if selected_plant else 0)} plants in scope · {len(phase_rows)} phases · {total_filters} filter assets.",
            use_plant_scope=True,
            model_count=len(model_rows) + len(LIMIT_MODELS),
        )
        context.update({"plant_options": plant_options, "selected_plant": selected_plant, "show_plant_selector": is_superuser})
        return context

    @staticmethod
    def _plants(request):
        user = request.user
        if user.is_superuser:
            options = TreatmentPlant.objects.order_by("name")
            requested = request.GET.get("plant")
            selected = options.filter(pk=requested).first() if requested and requested.isdigit() else None
            return options, selected, True
        plant_id = getattr(user, "assigned_treatment_plant_id", None)
        options = TreatmentPlant.objects.filter(pk=plant_id) if plant_id else TreatmentPlant.objects.none()
        return options.order_by("name"), options.first(), False

    @staticmethod
    def _daily_events(queryset, days):
        today = timezone.localdate()
        start = today - timedelta(days=days - 1)
        counts = Counter(row["day"] for row in queryset.annotate(day=TruncDate("recorded_at")).values("day"))
        buckets = [start + timedelta(days=offset) for offset in range(days)]
        if days > 90:
            weekly = Counter()
            for day, count in counts.items():
                weekly[day - timedelta(days=day.weekday())] += count
            buckets = sorted(weekly)
            counts = weekly
        return {"labels": [day.strftime("%d %b") for day in buckets], "values": [counts.get(day, 0) for day in buckets]}
