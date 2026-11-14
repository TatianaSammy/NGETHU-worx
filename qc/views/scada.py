from __future__ import annotations

import csv
import json
import logging
from datetime import date
from decimal import Decimal
from typing import Optional

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views import View

from core.mixins import WanjalaMixin
from t.models import TreatmentPlant
from qc.services.scada_dashboard_service import SCADADashboardService
from qc.services.wqi_service import generate_wqi_compliance_report


logger = logging.getLogger(__name__)


class SCADADashboardView(WanjalaMixin, View):
    """
    Unified SCADA Quality Control & Sensor Telemetry Command Center.

    Combines automated field sensor telemetry (instruments app) with
    laboratory QC manual analytical entry worksheets (qc/results/cards)
    and Water Quality Index (WQI) multi-model intelligence into a single
    multi-tabbed, cyber-industrial monitoring dashboard.
    """

    template_name = "qc/scada_dashboard.html"
    report_template_name = "qc/wqi_compliance_report.html"

    def dispatch(self, request, *args, **kwargs):
        """Bypass generic WanjalaMixin model table export handler for custom WQI exports."""
        export_mode = request.GET.get("export")
        if export_mode in ("wqi_csv", "wqi_json", "wqi_report"):
            user = request.user
            if not user.is_authenticated:
                return self.handle_no_permission()
            return self.get(request, *args, **kwargs)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        plant = self._resolve_plant(request)
        selected_date = self._resolve_date(request)
        active_tab = request.GET.get("tab", "overview")

        # -------------------------------------------------------------
        #  Dynamic WQI Export Handlers
        # -------------------------------------------------------------
        export_mode = request.GET.get("export")
        if export_mode == "wqi_csv":
            return self._export_wqi_csv(plant, selected_date)
        elif export_mode == "wqi_json":
            return self._export_wqi_json(plant, selected_date, request.user)
        elif export_mode == "wqi_report":
            report_ctx = generate_wqi_compliance_report(
                treatment_plant_id=plant.pk if plant else 0,
                target_date=selected_date,
            )
            report_ctx["request"] = request
            return render(request, self.report_template_name, report_ctx)

        service = SCADADashboardService(plant=plant, selected_date=selected_date, user=request.user)
        ctx = service.build_unified_scada_context(active_tab=active_tab)

        # Build chart data payloads for Trend Analytics & WQI tabs
        ctx.update(self._build_chart_payloads(ctx))

        return render(request, self.template_name, ctx)

    # ------------------------------------------------------------------
    #  Plant & Date Resolution (Strictly Plant-Scoped for Non-Superusers)
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_plant(request):
        """Resolve the plant from the request GET parameters with strict scoping."""
        user = request.user
        if not user.is_superuser:
            assigned_id = getattr(user, "assigned_treatment_plant_id", None)
            if assigned_id:
                try:
                    return TreatmentPlant.objects.get(pk=assigned_id)
                except TreatmentPlant.DoesNotExist:
                    pass

        plant_pk = request.GET.get("plant")
        if plant_pk:
            try:
                return TreatmentPlant.objects.get(pk=plant_pk)
            except (TreatmentPlant.DoesNotExist, ValueError):
                pass
        # Fallback: first available plant
        return TreatmentPlant.objects.order_by("name").first()

    @staticmethod
    def _resolve_date(request):
        """Resolve the selected date from the request GET parameters."""
        date_str = request.GET.get("date")
        if date_str:
            try:
                return date.fromisoformat(date_str)
            except (ValueError, TypeError):
                pass
        return timezone.localdate()

    # ------------------------------------------------------------------
    #  Dynamic Report Exports (CSV / JSON)
    # ------------------------------------------------------------------

    def _export_wqi_csv(self, plant: Optional[TreatmentPlant], target_date: date) -> HttpResponse:
        """Stream formal CSV export of WQI parameters, weights, and ratings."""
        from qc.services.wqi_service import get_plant_wqi

        plant_id = plant.pk if plant else 0
        plant_name = plant.name if plant else "Unassigned"
        plant_code = getattr(plant, "code", "PLT")
        data = get_plant_wqi(plant_id, target_date=target_date)

        filename = f"WQI_{plant_code}_{target_date.strftime('%Y%m%d')}.csv"
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow(["NCWSC Quality Assurance Directorate - Water Quality Index Report"])
        writer.writerow(["Treatment Plant:", plant_name, "Code:", plant_code])
        writer.writerow(["Assessment Date:", target_date.strftime("%Y-%m-%d")])
        writer.writerow(["Composite WQI Score:", data.get("score"), "Classification:", data.get("tier_label")])
        writer.writerow(["Geometric WQI:", data.get("wqi_geometric"), "CCME WQI Score:", data.get("ccme_wqi", {}).get("score")])
        writer.writerow(["Compliance Rate (%):", data.get("compliance_rate"), "Eclipsing Detected:", data.get("eclipsing_detected")])
        writer.writerow([])
        writer.writerow([
            "Parameter Name",
            "Measured Value",
            "Unit",
            "Min Limit",
            "Max Limit (Si)",
            "Ideal (V_ideal)",
            "Quality Rating (qi)",
            "Unit Weight (wi)",
            "Weight %",
            "Sub-Index (wi*qi)",
            "Status",
            "Standard Exceeded",
        ])

        for p in data.get("breakdown", []):
            writer.writerow([
                p.get("name"),
                p.get("value"),
                p.get("unit"),
                p.get("min_limit") if p.get("min_limit") is not None else "—",
                p.get("limit"),
                p.get("ideal"),
                p.get("qi"),
                p.get("wi"),
                p.get("wi_percent"),
                p.get("wi_qi"),
                p.get("tier_label"),
                "YES" if p.get("is_exceeded") else "NO",
            ])

        return response

    def _export_wqi_json(self, plant: Optional[TreatmentPlant], target_date: date, user) -> JsonResponse:
        """Return complete JSON API payload for automated SCADA historians."""
        from qc.services.wqi_service import get_dashboard_wqi_summary, get_plant_wqi_timeline

        summary = get_dashboard_wqi_summary(user=user, selected_plant=plant, target_date=target_date)
        timeline = get_plant_wqi_timeline(treatment_plant_id=plant.pk if plant else 0, target_date=target_date)

        payload = {
            "status": "success",
            "plant_id": plant.pk if plant else None,
            "plant_name": getattr(plant, "name", "Unassigned"),
            "target_date": target_date.isoformat(),
            "wqi": summary.get("plant_scoped"),
            "overall_average": summary.get("overall"),
            "timeline": timeline,
            "generated_at": timezone.now().isoformat(),
        }
        return JsonResponse(payload)

    # ------------------------------------------------------------------
    #  Chart Data Construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_chart_payloads(ctx):
        """
        Construct JSON-serializable chart data payloads for the
        Trend Analytics & WQI tabs (Chart.js).
        """
        stages = ctx.get("stages", [])

        # 1. Stage Concordance Bar Chart
        concordance_data = {
            "labels": [],
            "synch": [],
            "drift": [],
            "critical": [],
        }
        for stage in stages:
            concordance_data["labels"].append(stage.stage_title)
            concordance_data["synch"].append(stage.in_synch_count)
            concordance_data["drift"].append(stage.drift_count)
            concordance_data["critical"].append(stage.critical_count)

        # 2. Variance Distribution Radar
        radar_data = {"labels": [], "values": []}
        for stage in stages:
            radar_data["labels"].append(stage.stage_title)
            variances = [
                c.pct_variance
                for c in stage.comparisons
                if c.pct_variance is not None
            ]
            avg_var = sum(variances) / len(variances) if variances else 0
            radar_data["values"].append(round(avg_var, 1))

        # 3. Health Score Doughnut
        health_data = {"score": ctx.get("scada_health_index", 100)}

        # 4. Parameter Deviation Bubble Chart
        bubble_datasets = []
        for stage in stages:
            points = []
            for comp in stage.comparisons:
                if comp.delta_abs is not None and comp.pct_variance is not None:
                    radius = 5
                    if comp.reconciliation_status == "critical_mismatch":
                        radius = 12
                    elif comp.reconciliation_status == "minor_drift":
                        radius = 8

                    points.append({
                        "x": float(comp.delta_abs),
                        "y": float(comp.pct_variance),
                        "r": radius,
                        "label": f"{comp.parameter_name} ({comp.phase_name})",
                    })

            if points:
                color = "#10b981"
                if any(p["r"] >= 12 for p in points):
                    color = "#ef4444"
                elif any(p["r"] >= 8 for p in points):
                    color = "#f59e0b"

                bubble_datasets.append({
                    "label": stage.stage_title,
                    "data": points,
                    "backgroundColor": f"{color}88",
                    "borderColor": color,
                    "borderWidth": 1,
                })

        bubble_data = {"datasets": bubble_datasets}

        # -------------------------------------------------------------
        # 5. WQI Radar Chart (Parameter Quality Rating qi vs Limit 100)
        # -------------------------------------------------------------
        plant_wqi = ctx.get("plant_wqi") or {}
        breakdown = plant_wqi.get("breakdown", [])
        wqi_radar_data = {
            "labels": [p["name"] for p in breakdown],
            "qi_values": [p["qi"] for p in breakdown],
            "weights": [p["wi_percent"] for p in breakdown],
            "limits": [100.0 for _ in breakdown],
        }

        # 6. WQI Weight Contribution Donut Chart
        wqi_weights_data = {
            "labels": [p["name"] for p in breakdown],
            "values": [p["wi_percent"] for p in breakdown],
        }

        # 7. WQI Historical Timeline (Line Chart with Benchmark Bands)
        wqi_timeline = ctx.get("wqi_timeline") or {}
        wqi_timeline_data = {
            "labels": wqi_timeline.get("labels", []),
            "scores": wqi_timeline.get("scores", []),
            "ccme_scores": wqi_timeline.get("ccme_scores", []),
            "geometric_scores": wqi_timeline.get("geometric_scores", []),
        }

        # 8. Cross-Plant WQI Fleet Benchmark (Horizontal Bar Chart)
        all_plants = ctx.get("all_plants_wqi", [])
        wqi_fleet_data = {
            "labels": [p.get("plant_name", "Plant") for p in all_plants],
            "scores": [p.get("score") if p.get("score") is not None else 0 for p in all_plants],
            "tones": [p.get("tone", "secondary") for p in all_plants],
        }

        return {
            "chart_concordance_data": json.dumps(concordance_data),
            "chart_radar_data": json.dumps(radar_data),
            "chart_health_data": json.dumps(health_data),
            "chart_bubble_data": json.dumps(bubble_data),
            "chart_wqi_radar_data": json.dumps(wqi_radar_data),
            "chart_wqi_weights_data": json.dumps(wqi_weights_data),
            "chart_wqi_timeline_data": json.dumps(wqi_timeline_data),
            "chart_wqi_fleet_data": json.dumps(wqi_fleet_data),
        }
