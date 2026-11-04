from __future__ import annotations

from collections import OrderedDict

from django.db.models import Q


def _display_number(value):
    return f"{value:f}" if value is not None else None


def get_operational_summary(treatment_plant):
    """Return the latest approved treatment dosages and phase concentrations."""
    dosage_rows = [
        {"label": "Jar Test", "value_display": None, "unit": "mg/L", "detail": "No approved dosage"},
        {"label": "pH Adjustment", "value_display": None, "unit": "mL", "detail": "No approved dosage"},
        {"label": "Chlorine", "value_display": None, "unit": "mg/L", "detail": "No approved dosage"},
    ]
    phases = []
    if not treatment_plant:
        return dosage_rows, phases

    from jartest.models import JarTest
    from PHadjustment.models import PHAdjustmentTest
    from chlorinedemand.models import ChlorineDemandTest
    from t.models import TreatmentPlantPhase
    from xmicalconcentration.models import XmicalConcentration

    jar_test = (
        JarTest.objects.filter(
            treatment_plant=treatment_plant,
            approved_at__isnull=False,
            established_dosage_mg_per_l__isnull=False,
        )
        .select_related("coagulant")
        .order_by("-test_date", "-pk")
        .first()
    )
    if jar_test:
        dosage_rows[0].update(
            value_display=_display_number(jar_test.established_dosage_mg_per_l),
            detail=f"{jar_test.coagulant.name} · {jar_test.test_date:%d %b %Y}",
        )

    ph_test = (
        PHAdjustmentTest.objects.filter(
            treatment_plant=treatment_plant,
            approved_at__isnull=False,
            established_dosage_ml__isnull=False,
        )
        .order_by("-test_date", "-pk")
        .first()
    )
    if ph_test:
        dosage_rows[1].update(
            value_display=_display_number(ph_test.established_dosage_ml),
            detail=f"Approved · {ph_test.test_date:%d %b %Y}",
        )

    chlorine_test = (
        ChlorineDemandTest.objects.filter(
            treatment_plant=treatment_plant,
            approved_at__isnull=False,
            established_dosage_mg_per_l__isnull=False,
        )
        .order_by("-test_date", "-pk")
        .first()
    )
    if chlorine_test:
        dosage_rows[2].update(
            value_display=_display_number(chlorine_test.established_dosage_mg_per_l),
            detail=f"Approved · {chlorine_test.test_date:%d %b %Y}",
        )

    phase_map = OrderedDict(
        (phase.pk, {"id": phase.pk, "name": phase.name, "items": []})
        for phase in TreatmentPlantPhase.objects.filter(treatment_plant=treatment_plant).order_by("name")
    )
    seen = set()
    readings = (
        XmicalConcentration.objects.filter(
            treatment_plant=treatment_plant,
            phase__treatment_plant=treatment_plant,
        )
        .select_related("phase", "chemical", "tank")
        .order_by("-measurement_date", "-pk")
    )
    for reading in readings:
        if reading.phase_id not in phase_map:
            continue
        key = (reading.phase_id, reading.chemical_id, reading.tank_id)
        if key in seen:
            continue
        seen.add(key)
        phase_map[reading.phase_id]["items"].append({
            "chemical": reading.chemical_name or reading.chemical.name,
            "tank": reading.tank.name,
            "value_display": _display_number(reading.concentration_mg_per_l),
            "unit": "mg/L",
            "measured_at": reading.measurement_date,
        })

    phases = list(phase_map.values())
    return dosage_rows, phases


def get_filter_sidebar_groups(treatment_plant, user=None):
    """Build plant-scoped filter groups and their latest recorded parameters."""
    if not treatment_plant:
        return []

    if user is not None and getattr(user, "is_authenticated", False):
        is_superuser = getattr(user, "is_superuser", False)
        can_change_results = is_superuser or (
            user.has_perm("filtered_water.change_filteredwaterresults")
            and user.has_perm("qc.change_wateranalysis")
        )
        can_view_filters = is_superuser or user.has_perm("t.view_waterfilter") or can_change_results
        can_view_results = (
            is_superuser
            or user.has_perm("filtered_water.view_filteredwaterresults")
            or can_change_results
        )
        can_add_analysis = is_superuser or user.has_perm("filtered_water.add_filteredwateranalysis")
    elif user is not None and getattr(user, "is_superuser", False):
        can_change_results = True
        can_view_filters = True
        can_view_results = True
        can_add_analysis = True
    else:
        # Default for internal dashboard calls: read-only access to view filters and results
        can_change_results = False
        can_view_filters = True
        can_view_results = True
        can_add_analysis = False

    if not can_view_filters and not can_view_results:
        return []

    from filtered_water.models import FilteredWaterAnalysis, FilteredWaterResults
    from t.models import FilteredWaterLimit, TreatmentPlantPhase, WaterFilter

    phase_rows = list(
        TreatmentPlantPhase.objects.filter(treatment_plant=treatment_plant).order_by("name")
    )
    phase_ids = [phase.pk for phase in phase_rows]
    filters = list(
        WaterFilter.objects.filter(phase_id__in=phase_ids)
        .filter(Q(treatment_plant=treatment_plant) | Q(treatment_plant__isnull=True))
        .select_related("phase", "filter_type", "filter_status")
        .order_by("phase__name", "name")
    )
    filter_ids = [water_filter.pk for water_filter in filters]
    limits = list(
        FilteredWaterLimit.objects.filter(
            treatment_plant=treatment_plant,
            water_filter_id__in=filter_ids,
        )
        .select_related("water_filter", "parameter")
        .order_by("water_filter__name", "parameter__name")
    )

    latest_analyses = {}
    for phase in phase_rows:
        latest_analyses[phase.pk] = (
            FilteredWaterAnalysis.objects.filter(
                treatment_plant=treatment_plant,
                treatment_plant_phase=phase,
            )
            .order_by("-timestamp", "-pk")
            .first()
        )

    latest_values = {}
    latest_analysis_ids = [
        analysis.pk for analysis in latest_analyses.values() if analysis is not None
    ]
    if can_view_results and filter_ids and latest_analysis_ids:
        results = (
            FilteredWaterResults.objects.filter(
                analysis__treatment_plant=treatment_plant,
                analysis_id__in=latest_analysis_ids,
                water_filter_id__in=filter_ids,
            )
            .select_related("analysis", "water_filter", "parameter", "parameter__parameter")
            .order_by("-analysis__timestamp", "-pk")
        )
        for result in results:
            key = (result.water_filter_id, result.parameter_id)
            if key not in latest_values:
                latest_values[key] = result

    limits_by_filter = {}
    for limit in limits:
        parameter = limit.parameter
        unit = getattr(parameter, "unit", "") or getattr(parameter, "unit_of_measure", "")
        current = latest_values.get((limit.water_filter_id, limit.pk))
        limits_by_filter.setdefault(limit.water_filter_id, []).append({
            "id": limit.pk,
            "name": parameter.name,
            "unit": unit,
            "min_value": limit.min_value,
            "max_value": limit.max_value,
            "value": _display_number(current.result_value) if current and current.result_value is not None else "",
            "display_value": _display_number(current.result_value) if current and current.result_value is not None else None,
        })

    groups = []
    for phase in phase_rows:
        latest = latest_analyses[phase.pk]
        can_edit_phase = can_change_results and (
            (latest is not None and latest.state == "new") or can_add_analysis
        )
        phase_filters = []
        for water_filter in filters:
            if water_filter.phase_id != phase.pk:
                continue
            parameters = limits_by_filter.get(water_filter.pk, [])
            status_label = water_filter.get_status_display()
            phase_filters.append({
                "id": water_filter.pk,
                "name": water_filter.name,
                "type": water_filter.filter_type.name,
                "status": water_filter.status,
                "status_label": status_label,
                "can_edit": can_edit_phase and water_filter.status == "working" and bool(parameters),
                "parameters": parameters,
            })
        groups.append({
            "id": phase.pk,
            "name": phase.name,
            "filters": phase_filters,
            "analysis_state": latest.get_state_display() if latest else "No reading session",
            "can_edit": can_edit_phase and any(item["can_edit"] for item in phase_filters),
            "can_view_results": can_view_results,
        })
    return groups
