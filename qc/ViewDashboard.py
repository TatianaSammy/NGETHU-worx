from collections import Counter
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone
from django.views import View

from core.domain_dashboard import dashboard_context, model_list_url, period_days, render_domain_dashboard, time_series
from core.mixins import WanjalaMixin
from .models import (
    ClarifiedWaterResult,
    CoagulatedWaterResult,
    NonConformity,
    RawWaterResult,
    SamplingSchedule,
    SamplingTime,
    TreatedWaterResult,
    WaterAnalysis,
    WaterAnalysisLog,
)


RESULT_MODELS = (
    ("Raw water", RawWaterResult, "cyan"),
    ("Coagulated", CoagulatedWaterResult, "blue"),
    ("Clarified", ClarifiedWaterResult, "purple"),
    ("Treated", TreatedWaterResult, "emerald"),
)


class DashboardView(WanjalaMixin, View):
    template_name = "qc/dashboard.html"
    app_label = "qc"

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
        analyses = WaterAnalysis.objects.select_related("treatment_plant", "shift", "analysis_time", "analysis_time__schedule").filter(analysis_date__gte=start_date, analysis_date__lte=today)
        schedules = SamplingSchedule.objects.filter(enabled=True)
        nonconformities = NonConformity.objects.select_related("analysis", "analysis__treatment_plant").filter(analysis__analysis_date__gte=start_date)
        logs = WaterAnalysisLog.objects.select_related("analysis", "analysis__treatment_plant", "actor").filter(created_at__gte=timezone.now() - timedelta(days=days))
        if selected_plant:
            analyses = analyses.filter(treatment_plant=selected_plant)
            schedules = schedules.filter(treatment_plant=selected_plant)
            nonconformities = nonconformities.filter(analysis__treatment_plant=selected_plant)
            logs = logs.filter(analysis__treatment_plant=selected_plant)
        elif not is_superuser:
            analyses = analyses.none()
            schedules = schedules.none()
            nonconformities = nonconformities.none()
            logs = logs.none()

        stage_counts = Counter()
        result_count = compliant = failed = unrated = 0
        daily_result_counts = Counter()
        stage_result_counts = {}
        for stage, model, _color in RESULT_MODELS:
            result_rows = model.objects.filter(analysis__in=analyses).values_list(
                "analysis__analysis_date", "value", "parameter__min_value", "parameter__max_value"
            )
            count = 0
            for result_date, value, minimum, maximum in result_rows.iterator(chunk_size=1000):
                if value is None:
                    continue
                count += 1
                result_count += 1
                if minimum is None and maximum is None:
                    unrated += 1
                    continue
                if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
                    failed += 1
                else:
                    compliant += 1
            stage_result_counts[stage] = count

        total_analyses = analyses.count()
        analysis_by_water_type = dict(analyses.values("analysis_time__schedule__water_type").annotate(total=Count("pk")).values_list("analysis_time__schedule__water_type", "total"))
        open_nc = nonconformities.filter(status__in=["open", "under_review"]).count()
        closed_nc = nonconformities.filter(status="closed").count()
        recent_logs = list(logs.order_by("-created_at")[:12])
        analysis_trend = time_series(analyses, "analysis_date", days)
        if result_count and compliant + failed:
            compliance_rate = round(compliant * 100 / (compliant + failed), 1)
        else:
            compliance_rate = None

        analysis_rows = list(analyses.order_by("-analysis_date", "-analysis_time__time")[:50])
        table_rows = []
        recent_records = []
        for analysis in analysis_rows:
            water_type = analysis.water_type
            water_type_label = str(water_type).replace("_", " ").title()
            table_rows.append({"cells": [
                analysis.reference_id,
                analysis.treatment_plant.name,
                water_type_label,
                analysis.analysis_date.strftime("%d %b %Y"),
                analysis.analysis_time.time.strftime("%H:%M"),
                analysis.shift.name if analysis.shift else "Not assigned",
                str(analysis.remarks or "—")[:70],
            ]})
            recent_records.append({
                "title": f"{water_type_label} analysis · {analysis.reference_id}",
                "detail": f"{analysis.treatment_plant.name} · {analysis.shift.name if analysis.shift else 'Shift not set'}",
                "timestamp": f"{analysis.analysis_date:%d %b} · {analysis.analysis_time.time:%H:%M}",
                "icon": "vials",
                "color": "cyan",
            })
        for log in recent_logs:
            recent_records.append({
                "title": f"{log.action.replace('_', ' ').title()} · {log.analysis.reference_id}",
                "detail": f"{log.analysis.treatment_plant.name} · {log.actor.get_full_name() if log.actor else 'System'}",
                "timestamp": timezone.localtime(log.created_at).strftime("%d %b %H:%M"),
                "icon": "clipboard-check",
                "color": "emerald",
            })
        recent_records.sort(key=lambda row: row["timestamp"], reverse=True)

        model_specs = [
            ("Water analyses", WaterAnalysis, "vials", "Scheduled sampling"),
            ("Sampling schedules", SamplingSchedule, "calendar-days", "Sampling cadence"),
            ("Sampling times", SamplingTime, "clock", "Sampling cadence"),
            ("Non-conformities", NonConformity, "triangle-exclamation", "Quality investigations"),
            ("Analysis logs", WaterAnalysisLog, "list-check", "Audit trail"),
        ]
        model_rows = []
        for label, model, icon, group in model_specs:
            qs = model.objects.all()
            if selected_plant and hasattr(model, "treatment_plant_id"):
                qs = qs.filter(treatment_plant=selected_plant)
            elif not is_superuser and hasattr(model, "treatment_plant_id"):
                qs = qs.none()
            model_rows.append({"label": label, "count": qs.count(), "url": model_list_url(self.app_label, model._meta.model_name), "icon": icon, "group": group})
        for label, model, _color in RESULT_MODELS:
            qs = model.objects.filter(analysis__in=analyses)
            model_rows.append({"label": f"{label} results", "count": qs.count(), "url": model_list_url(self.app_label, model._meta.model_name), "icon": "flask-vial", "group": "Measured water stages"})

        rated_total = compliant + failed
        kpis = [
            {"label": "Water analyses", "value": total_analyses, "detail": f"Scheduled records · {days} days", "icon": "vials", "color": "cyan", "tab": "records"},
            {"label": "Saved readings", "value": result_count, "detail": "Non-empty values across four process stages", "icon": "flask-vial", "color": "blue", "tab": "analytics"},
            {"label": "Rated compliance", "value": f"{compliance_rate}%" if compliance_rate is not None else "—", "detail": f"{compliant} within · {failed} outside limits", "icon": "shield-heart", "color": "emerald" if compliance_rate is not None and compliance_rate >= 95 else "amber", "tab": "analytics"},
            {"label": "Open investigations", "value": open_nc, "detail": "Open or under review", "icon": "triangle-exclamation", "color": "rose", "tab": "records"},
            {"label": "Active schedules", "value": schedules.count(), "detail": "Enabled plant-specific sampling plans", "icon": "calendar-check", "color": "purple", "tab": "models"},
            {"label": "Unrated readings", "value": unrated, "detail": "No configured min/max limit", "icon": "scale-unbalanced", "color": "amber", "tab": "analytics"},
        ]
        stage_total = max(sum(stage_result_counts.values()), 1)
        ranked_rows = [{"label": stage, "detail": "Recorded result values", "value": count, "percent": round(count * 100 / stage_total), "color": color} for stage, _model, color in RESULT_MODELS for count in [stage_result_counts.get(stage, 0)]]
        context = dashboard_context(
            request,
            title="Water Quality Control",
            subtitle="Plant-scoped raw, coagulated, clarified, and treated water analysis intelligence.",
            kicker="QC command overview",
            kpis=kpis,
            charts=[
                {"id": "qc-analysis-trend", "title": "Analysis throughput", "caption": "Analysis records by sampling date.", "type": "line", "labels": analysis_trend["labels"], "values": analysis_trend["values"], "color": "cyan"},
                {"id": "qc-stage-volume", "title": "Process-stage readings", "caption": "Recorded result values by water stage.", "type": "bar", "labels": list(stage_result_counts.keys()), "values": list(stage_result_counts.values()), "color": "blue"},
                {"id": "qc-compliance", "title": "Limit evaluation", "caption": "Only measurements with configured bounds are rated.", "type": "doughnut", "labels": ["Within limits", "Outside limits", "Unrated"], "values": [compliant, failed, unrated], "colors": ["emerald", "rose", "amber"]},
                {"id": "qc-water-types", "title": "Analysis mix", "caption": "Sampling volume across the configured water types.", "type": "horizontal", "labels": [str(key).replace("_", " ").title() for key in analysis_by_water_type.keys()], "values": list(analysis_by_water_type.values()), "color": "purple"},
            ],
            signals=[
                {"label": "Within limits", "value": compliant, "detail": "Rated readings within configured bounds", "icon": "circle-check", "color": "emerald"},
                {"label": "Out of limits", "value": failed, "detail": "Rated readings outside configured bounds", "icon": "triangle-exclamation", "color": "rose"},
                {"label": "Unrated", "value": unrated, "detail": "Bounds are not configured for the parameter", "icon": "scale-unbalanced", "color": "amber"},
                {"label": "Closed investigations", "value": closed_nc, "detail": "Non-conformities closed during the selected window", "icon": "circle-check", "color": "blue"},
            ],
            recent_records=recent_records[:12],
            model_rows=model_rows,
            table_headings=["Reference", "Plant", "Water stage", "Date", "Sample time", "Shift", "Remarks"],
            table_rows=table_rows,
            ranked_rows=ranked_rows,
            actions=[
                {"label": "Enter QC result cards", "url": f"/qca/results/cards/{'?plant=' + str(selected_plant.pk) if selected_plant else ''}", "icon": "table-cells-large"},
                {"label": "QC analytics", "url": "/qc/quality_control/analytics/", "icon": "chart-line"},
                {"label": "SCADA", "url": "/qc/scada/", "icon": "tower-broadcast"},
            ],
            overview_caption="Quality KPIs are computed from persisted analyses, result rows, plant limits, and investigation states.",
            use_plant_scope=True,
            model_count=len(model_specs) + len(RESULT_MODELS),
        )
        context.update({"plant_options": plant_options, "selected_plant": selected_plant, "show_plant_selector": is_superuser})
        return context

    @staticmethod
    def _plants(request):
        from t.models import TreatmentPlant

        user = request.user
        if user.is_superuser:
            options = TreatmentPlant.objects.order_by("name")
            requested = request.GET.get("plant")
            return options, options.filter(pk=requested).first() if requested and requested.isdigit() else None, True
        plant_id = getattr(user, "assigned_treatment_plant_id", None)
        options = TreatmentPlant.objects.filter(pk=plant_id) if plant_id else TreatmentPlant.objects.none()
        return options, options.first(), False
