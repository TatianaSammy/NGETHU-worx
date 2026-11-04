from __future__ import annotations

from decimal import Decimal

from django.db.models import Q
from ca.models import ChemicalQCCheckResult


def control_chart_series(control, treatment_plant_id=None, limit=30):
    queryset = ChemicalQCCheckResult.objects.filter(control=control).select_related(
        "batch", "analyst", "parameter", "control__plan__method"
    ).order_by("recorded_at", "pk")
    if treatment_plant_id is not None:
        queryset = queryset.filter(batch__treatment_plant_id=treatment_plant_id)
    rows = list(queryset.order_by("-recorded_at", "-pk")[:limit])
    rows.reverse()
    values = [item.evaluated_value for item in rows]
    baseline = values[:-1]
    chart = {
        "rows": rows,
        "mean": None,
        "sd": None,
        "warning_low": None,
        "warning_high": None,
        "control_low": None,
        "control_high": None,
        "signal": "INSUFFICIENT_BASELINE",
    }
    if len(baseline) >= 8:
        mean = sum(baseline, Decimal("0")) / Decimal(len(baseline))
        variance = sum(((value - mean) ** 2 for value in baseline), Decimal("0")) / Decimal(len(baseline) - 1)
        sd = variance.sqrt()
        warning_sigma = control.warning_sigma
        chart.update(
            mean=mean,
            sd=sd,
            warning_low=mean - warning_sigma * sd,
            warning_high=mean + warning_sigma * sd,
            control_low=mean - Decimal("3") * sd,
            control_high=mean + Decimal("3") * sd,
        )
        current = values[-1]
        distance = abs(current - mean)
        if sd == 0 and distance > 0:
            chart["signal"] = "OUT_OF_CONTROL"
        elif sd and distance >= Decimal("3") * sd:
            chart["signal"] = "OUT_OF_CONTROL"
        elif sd and distance >= warning_sigma * sd:
            chart["signal"] = "WARNING"
        else:
            chart["signal"] = "IN_CONTROL"
    return chart


def evaluate_control_chart_and_alert(qc_result):
    chart = control_chart_series(
        qc_result.control,
        treatment_plant_id=qc_result.batch.treatment_plant_id,
        limit=31,
    )
    signal = chart["signal"]
    if signal not in {"OUT_OF_CONTROL", "WARNING"}:
        return chart

    warning_repeat = False
    if signal == "WARNING":
        previous = [
            item for item in chart["rows"]
            if item.pk != qc_result.pk
        ]
        if previous:
            prior_chart = control_chart_series(
                qc_result.control,
                treatment_plant_id=qc_result.batch.treatment_plant_id,
                limit=31,
            )
            rows = [item for item in prior_chart["rows"] if item.pk != qc_result.pk]
            values = [item.evaluated_value for item in rows]
            baseline = values[:-1]
            if len(baseline) >= 8:
                mean = sum(baseline, Decimal("0")) / Decimal(len(baseline))
                sd = (sum(((value - mean) ** 2 for value in baseline), Decimal("0")) / Decimal(len(baseline) - 1)).sqrt()
                warning_repeat = bool(sd and abs(values[-1] - mean) >= qc_result.control.warning_sigma * sd)
        if not warning_repeat:
            return chart

    title = "Method QC control chart signal"
    message = (
        f"{qc_result.control.label} ({qc_result.control.plan.method}) has a {signal.lower().replace('_', ' ')} "
        f"signal at {qc_result.batch.treatment_plant}. Current evaluated value: {qc_result.evaluated_value}. "
        "Review the method QC trend and investigate possible analytical drift."
    )
    from ca.inventory_instruments import trigger_qc_alert

    trigger_qc_alert(
        qc_result,
        alert_type="ca_qc_drift",
        title=title,
        message=message,
        severity="high" if signal == "OUT_OF_CONTROL" else "medium",
        context_data={
            "qc_result_id": qc_result.pk,
            "qc_run_number": qc_result.run_number,
            "control_chart_signal": signal,
            "baseline_mean": str(chart["mean"]) if chart["mean"] is not None else None,
            "baseline_standard_deviation": str(chart["sd"]) if chart["sd"] is not None else None,
            "warning_limit_low": str(chart["warning_low"]) if chart["warning_low"] is not None else None,
            "warning_limit_high": str(chart["warning_high"]) if chart["warning_high"] is not None else None,
            "control_limit_low": str(chart["control_low"]) if chart["control_low"] is not None else None,
            "control_limit_high": str(chart["control_high"]) if chart["control_high"] is not None else None,
        },
    )
    chart["alert_created"] = True
    return chart
