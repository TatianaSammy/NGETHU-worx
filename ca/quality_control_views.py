from __future__ import annotations

from decimal import Decimal

from django.db.models import Q
from django.views.generic import TemplateView

from .mixins import CaWanjalaMixin
from .models import ChemicalMethodQCControl, ChemicalQCCheckResult
from .services.quality_control import control_chart_series


class ChemicalQCControlChartView(CaWanjalaMixin, TemplateView):
    permission_required_view = "ca.view_batchtestresult"
    template_name = "ca/quality_control/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        plant_id = self.get_scope_plant_id()
        controls = ChemicalMethodQCControl.objects.filter(is_active=True, plan__is_active=True).select_related(
            "plan__method", "plan__treatment_plant"
        )
        if plant_id is not None:
            controls = controls.filter(Q(plan__treatment_plant_id=plant_id) | Q(plan__treatment_plant__isnull=True))
        cards = []
        for control in controls.order_by("plan__method__method", "label", "pk"):
            plant_ids = list(
                ChemicalQCCheckResult.objects.filter(control=control)
                .values_list("batch__treatment_plant_id", flat=True).distinct().order_by("batch__treatment_plant_id")
            )
            if plant_id is not None:
                plant_ids = [plant_id]
            if not plant_ids:
                plant_ids = [control.plan.treatment_plant_id]
            for scope_id in plant_ids:
                chart = control_chart_series(control, treatment_plant_id=scope_id, limit=30)
                rows = chart["rows"]
                numeric = [float(item.evaluated_value) for item in rows]
                bounds = numeric + [
                    float(value)
                    for value in (chart["control_low"], chart["control_high"])
                    if value is not None
                ]
                low = min(bounds) if bounds else 0.0
                high = max(bounds) if bounds else 1.0
                spread = high - low or 1.0
                y_for = lambda value: 110 - ((float(value) - low) / spread) * 90
                points = " ".join(
                    f"{20 + index * (560 / max(1, len(numeric) - 1)):.1f},{y_for(value):.1f}"
                    for index, value in enumerate(numeric)
                )
                recent_failures = sum(item.status == ChemicalQCCheckResult.Status.FAIL for item in rows)
                cards.append({
                    "control": control,
                    "plant": rows[-1].batch.treatment_plant if rows else (control.plan.treatment_plant or self.get_selected_plant()),
                    "chart": chart,
                    "points": points,
                    "latest": rows[-1] if rows else None,
                    "recent_failures": recent_failures,
                    "samples": len(rows),
                    "mean_y": y_for(chart["mean"]) if chart["mean"] is not None else None,
                    "warning_high_y": y_for(chart["warning_high"]) if chart["warning_high"] is not None else None,
                    "warning_low_y": y_for(chart["warning_low"]) if chart["warning_low"] is not None else None,
                    "control_high_y": y_for(chart["control_high"]) if chart["control_high"] is not None else None,
                    "control_low_y": y_for(chart["control_low"]) if chart["control_low"] is not None else None,
                })
        context.update(
            control_cards=cards,
            plant_context=self.get_selected_plant(),
            overall_checks=sum(item["samples"] for item in cards),
            control_failures=sum(item["recent_failures"] for item in cards),
            out_of_control=sum(item["chart"]["signal"] == "OUT_OF_CONTROL" for item in cards),
        )
        return context
