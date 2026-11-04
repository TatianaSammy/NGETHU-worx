from collections import Counter
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone
from django.views import View

from core.domain_dashboard import dashboard_context, model_list_url, period_days, render_domain_dashboard, time_series
from core.mixins import WanjalaMixin
from .models import (
    Catchment,
    CatchmentSamplingPoint,
    DynamicSamplingPoint,
    FCARegionSamplingPoint,
    Itinerary,
    RegionSamplingPoint,
    SampleCategory,
    SamplingPlan,
)


POINT_MODELS = (
    ("Regional points", RegionSamplingPoint, "map-location-dot"),
    ("FCA points", FCARegionSamplingPoint, "water"),
    ("Catchment points", CatchmentSamplingPoint, "mountain-sun"),
)


class DashboardView(WanjalaMixin, View):
    template_name = "itinerary/dashboard.html"
    app_label = "itinerary"

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        return render_domain_dashboard(request, self.template_name, self.app_label, self.get_context(request))

    def get_context(self, request):
        days = period_days(request)
        today = timezone.localdate()
        start_date = today - timedelta(days=days - 1)
        end_date = today + timedelta(days=30)
        itineraries = Itinerary.objects.select_related("content_type").annotate(point_count=Count("sampling_points", distinct=True))
        window_itineraries = itineraries.filter(sampling_date__gte=start_date, sampling_date__lte=end_date)
        points = []
        point_type_counts = Counter()
        point_due_counts = Counter()
        all_point_rows = []
        for label, model, icon in POINT_MODELS:
            qs = model.objects.filter(is_deleted=False).order_by("name")
            rows = list(qs)
            point_type_counts[label] = len(rows)
            for point in rows:
                interval = {"Daily": 1, "Weekly": 7, "Monthly": 30}.get(point.preferred_frequency, 30)
                age = (today - point.last_sampled_date).days if point.last_sampled_date else None
                is_due = age is None or age >= interval
                if is_due:
                    point_due_counts[label] += 1
                points.append(point)
                all_point_rows.append({
                    "cells": [point.name, label, point.preferred_frequency, point.last_sampled_date.strftime("%d %b %Y") if point.last_sampled_date else "Never sampled", point.total_times_sampled, "Due" if is_due else "Current"]
                })

        dynamic_points = DynamicSamplingPoint.objects.count()
        active_itineraries = window_itineraries.exclude(status="Archived")
        status_counts = Counter(window_itineraries.values_list("status", flat=True))
        total_points = sum(point_type_counts.values())
        overdue_points = sum(point_due_counts.values())
        current_plan_month = today.replace(day=1)
        current_plan = SamplingPlan.objects.filter(month=current_plan_month).first()
        plan_target = current_plan.target_sampling_points if current_plan else 0
        plan_actual = current_plan.actual_sampled_points if current_plan else 0
        coverage = min(100, round(plan_actual * 100 / plan_target)) if plan_target else None
        sampling_trend = time_series(Itinerary.objects.filter(sampling_date__lte=today), "sampling_date", days)
        itinerary_rows = list(window_itineraries.order_by("sampling_date", "name")[:50])
        recent_records = [{
            "title": row.name,
            "detail": f"{row.status} · {row.point_count} assigned points · {row.content_type.name if row.content_type else 'Sampling route'}",
            "timestamp": row.sampling_date.strftime("%d %b %Y"),
            "icon": "route",
            "color": "emerald" if row.status == "Completed" else "amber" if row.status in {"Scheduled", "In Progress"} else "cyan",
        } for row in itinerary_rows[:12]]
        table_rows = [{"cells": [
            row.name,
            row.content_type.name if row.content_type else "—",
            row.sampling_date.strftime("%d %b %Y"),
            row.status,
            f"{row.point_count}/{row.max_sampling_points}",
            "Locked" if row.is_locked else "Open",
            row.assignment_strategy or "Default",
        ]} for row in itinerary_rows]
        model_specs = [
            ("Itineraries", Itinerary, "route", "Sampling routes"),
            ("Sampling plans", SamplingPlan, "calendar-check", "Monthly targets"),
            ("Sampling point links", DynamicSamplingPoint, "link", "Generic point mapping"),
            ("Regional points", RegionSamplingPoint, "map-location-dot", "Sampling locations"),
            ("FCA points", FCARegionSamplingPoint, "water", "Sampling locations"),
            ("Catchment points", CatchmentSamplingPoint, "mountain-sun", "Sampling locations"),
            ("Catchments", Catchment, "mountain", "Catchment registry"),
            ("Sample categories", SampleCategory, "tags", "Sample classification"),
        ]
        model_rows = [{"label": label, "count": model.objects.count(), "url": model_list_url(self.app_label, model._meta.model_name), "icon": icon, "group": group} for label, model, icon, group in model_specs]
        scheduled = status_counts.get("Scheduled", 0) + status_counts.get("In Progress", 0)
        completed = status_counts.get("Completed", 0)
        kpis = [
            {"label": "Route plans", "value": active_itineraries.count(), "detail": f"Within the selected window plus 30-day horizon", "icon": "route", "color": "cyan", "tab": "records"},
            {"label": "Scheduled / active", "value": scheduled, "detail": "Routes with fieldwork ahead or underway", "icon": "truck-fast", "color": "blue", "tab": "analytics"},
            {"label": "Completed", "value": completed, "detail": f"Of {window_itineraries.count()} itineraries in scope", "icon": "circle-check", "color": "emerald", "tab": "records"},
            {"label": "Sampling locations", "value": total_points, "detail": f"{dynamic_points} generic point links", "icon": "location-dot", "color": "purple", "tab": "models"},
            {"label": "Sampling overdue", "value": overdue_points, "detail": "Frequency-based: daily, weekly, or monthly", "icon": "triangle-exclamation", "color": "amber", "tab": "analytics"},
            {"label": "Monthly plan coverage", "value": f"{coverage}%" if coverage is not None else "—", "detail": f"{plan_actual} of {plan_target} target points sampled", "icon": "bullseye", "color": "emerald" if coverage is not None and coverage >= 80 else "amber", "tab": "analytics"},
        ]
        all_point_total = max(total_points, 1)
        ranked_rows = [{
            "label": label,
            "detail": f"{point_due_counts[label]} due from {count} registered locations",
            "value": f"{point_due_counts[label]} due",
            "percent": round(point_due_counts[label] * 100 / max(count, 1)),
            "color": "rose" if point_due_counts[label] else "emerald",
        } for label, count in point_type_counts.items()]
        context = dashboard_context(
            request,
            title="Sampling Itinerary Control",
            subtitle="Route readiness, sampling frequency coverage, monthly targets, and fieldwork status.",
            kicker="Field sampling operations",
            kpis=kpis,
            charts=[
                {"id": "itinerary-trend", "title": "Route plan cadence", "caption": "Saved itinerary sampling dates by period bucket.", "type": "line", "labels": sampling_trend["labels"], "values": sampling_trend["values"], "color": "cyan"},
                {"id": "itinerary-status", "title": "Itinerary lifecycle", "caption": "Actual saved status distribution for the selected schedule window.", "type": "doughnut", "labels": list(status_counts.keys()), "values": list(status_counts.values()), "colors": ["slate", "blue", "amber", "emerald", "purple"]},
                {"id": "itinerary-point-types", "title": "Sampling locations by domain", "caption": "Registered, non-deleted sample locations.", "type": "bar", "labels": list(point_type_counts.keys()), "values": list(point_type_counts.values()), "color": "purple"},
            ],
            signals=[
                {"label": "Locations due", "value": overdue_points, "detail": "Past their configured sampling cadence", "icon": "location-crosshairs", "color": "amber"},
                {"label": "Monthly sampled", "value": plan_actual, "detail": f"Target: {plan_target}", "icon": "vial", "color": "cyan"},
                {"label": "Available slots", "value": sum(max(row.max_sampling_points - row.point_count, 0) for row in itinerary_rows), "detail": "Remaining itinerary capacity in the window", "icon": "arrows-left-right", "color": "blue"},
                {"label": "Locked routes", "value": window_itineraries.filter(is_locked=True).count(), "detail": "Itineraries protected from changes", "icon": "lock", "color": "purple"},
            ],
            recent_records=recent_records,
            model_rows=model_rows,
            table_headings=["Itinerary", "Point domain", "Sampling date", "Status", "Assigned / max", "Edit lock", "Assignment strategy"],
            table_rows=table_rows,
            ranked_rows=ranked_rows,
            actions=[{"label": "Sampling locations", "url": "/itinerary/", "icon": "map-location-dot"}],
            overview_caption="Frequency coverage and monthly plan metrics are derived from sampling-point dates and the saved plan model.",
            model_count=len(model_specs),
        )
        return context
