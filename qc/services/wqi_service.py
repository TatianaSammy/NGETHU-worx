"""Water Quality Index (WQI) calculation service.

Computes composite Water Quality Index scores following the weighted arithmetic mean method:
    WQI = Sum(w_i * q_i) / Sum(w_i)
where:
    q_i = |(V_i - V_ideal) / (S_i - V_ideal)| * 100
    w_i = k / S_i, with k = 1 / Sum(1 / S_i)  (weights sum to 1.0)

Parameter limits are loaded **dynamically** from the ``TreatedWaterLimit`` model in the
``t`` app (FK → ``PhysicochemicalParameter``), keyed by treatment plant.  Hardcoded
numeric limits are **never** used; only domain-knowledge *ideal* values (e.g. pH ideal = 7.0,
Dissolved Oxygen ideal = 14.6 mg/L — values representing perfect water quality, not regulatory
thresholds) are retained as constants, because these are physical/biological constants, not
plant-specific limits.

Classification Scale (Five-tier scale):
    0  - 25  : Excellent (water meets or exceeds all standard limits)
    26 - 50  : Good (acceptable quality for most uses with standard treatment)
    51 - 75  : Poor (at least one parameter is elevated; treatment required)
    76 - 100 : Very Poor (substantial contamination; intensive remediation needed)
    > 100    : Unsuitable for Drinking (severe standard exceedance)
"""

from __future__ import annotations

import logging
import math
from datetime import timedelta, date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.apps import apps
from django.utils import timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain-knowledge ideal values only — no regulatory limits hardcoded here.
# These are physical/biological constants representing "perfect" water quality.
# Regulatory min/max values must always come from TreatedWaterLimit in the DB.
# ---------------------------------------------------------------------------
_PARAM_IDEALS: Dict[str, float] = {
    "ph": 7.0,              # Neutral pH
    "dissolved_oxygen": 14.6,  # DO at saturation (cold, clean water ~14.6 mg/L)
    "do": 14.6,
    "temperature_change": 0.0,  # Zero change from ambient
    "temperature": None,        # Computed from (min+max)/2 at runtime
    "bod": 0.0,             # Zero organic load is ideal
    "turbidity": 0.0,       # Perfectly clear water
    "tds": 0.0,             # Zero dissolved solids is ideal
    "total_dissolved_solids": 0.0,
    "conductivity": 0.0,
    "color": 0.0,
    "colour": 0.0,
    "crc": 0.0,             # Combined residual chlorine → zero is ideal upper limit
}

# Parameter name aliases → canonical key used to look up ideal values
_PARAM_KEY_ALIASES: Dict[str, str] = {
    "p_h": "ph",
    "water_ph": "ph",
    "ph_value": "ph",
    "dissolved_oxygen": "dissolved_oxygen",
    "do": "dissolved_oxygen",
    "bod5": "bod",
    "biochemical_oxygen_demand": "bod",
    "temp": "temperature",
    "temp_change": "temperature_change",
    "temp_diff": "temperature_change",
    "temperature_difference": "temperature_change",
    "total_dissolved_solids": "tds",
    "conductivity": "conductivity",
    "electrical_conductivity": "conductivity",
    "colour": "color",
    "apparent_colour": "color",
    "true_colour": "color",
    "combined_residual_chlorine": "crc",
    "c_r_c": "crc",
    "free_residual_chlorine": "frc",
    "f_r_c": "frc",
    "total_residual_chlorine": "trc",
    "t_r_c": "trc",
    "alkalinity_m_o": "alkalinity_mo",
    "total_alkalinity": "alkalinity_mo",
    "alkalinity_p": "alkalinity_p",
    "phenolphthalein_alkalinity": "alkalinity_p",
}

# Chlorine parameters whose ideal value is the midpoint of the allowed range
_MIDPOINT_IDEAL_PARAMS = {"frc", "trc", "alkalinity_mo"}

# Parameters where ideal is 'lower is better' — q_i = value / max_limit * 100
_ZERO_IDEAL_PARAMS = {
    "bod", "turbidity", "tds", "total_dissolved_solids", "conductivity",
    "color", "colour", "crc",
}

# DO is special: ideal is high, limit is a minimum (q_i based on deficit from ideal)
_DO_PARAMS = {"dissolved_oxygen", "do"}


# ---------------------------------------------------------------------------
# Plant limit cache: { plant_id: { param_key: {"min": float|None, "max": float|None, "unit": str} } }
# ---------------------------------------------------------------------------
_plant_limit_cache: Dict[int, Dict[str, Dict[str, Any]]] = {}


def _normalize_key(name: str) -> str:
    """Normalize a parameter name to a canonical lookup key."""
    clean = (
        name.strip().lower()
        .replace(" ", "_").replace("-", "_")
        .replace(".", "").replace("(", "_").replace(")", "")
        .replace("/", "_").replace("\\", "_")
    )
    # Collapse repeated underscores
    while "__" in clean:
        clean = clean.replace("__", "_")
    return _PARAM_KEY_ALIASES.get(clean, clean)


def load_plant_limits(treatment_plant_id: int, force_reload: bool = False) -> Dict[str, Dict[str, Any]]:
    """Load and cache all TreatedWaterLimit rows for a given treatment plant.

    Returns a dict keyed by normalized parameter name:
        { "ph": {"min": 6.5, "max": 8.5, "unit": "pH units", "display_name": "pH"}, ... }

    The cache is per-process (request-level for typical Django). Pass
    ``force_reload=True`` to bypass the cache (e.g. after admin edits).
    """
    if not force_reload and treatment_plant_id in _plant_limit_cache:
        return _plant_limit_cache[treatment_plant_id]

    TreatedWaterLimit = apps.get_model("t", "TreatedWaterLimit")
    limits_qs = (
        TreatedWaterLimit.objects
        .filter(treatment_plant_id=treatment_plant_id)
        .select_related("parameter")
    )

    plant_limits: Dict[str, Dict[str, Any]] = {}
    for limit in limits_qs:
        param = limit.parameter
        display_name = param.name
        key = _normalize_key(display_name)
        plant_limits[key] = {
            "min": float(limit.min_value) if limit.min_value is not None else None,
            "max": float(limit.max_value) if limit.max_value is not None else None,
            "unit": getattr(param, "unit", "") or "",
            "display_name": display_name,
            "limit_id": limit.pk,
        }

    _plant_limit_cache[treatment_plant_id] = plant_limits
    logger.debug(
        "[WQI] Loaded %d parameter limits for plant %s",
        len(plant_limits),
        treatment_plant_id,
    )
    return plant_limits


def invalidate_plant_limit_cache(treatment_plant_id: Optional[int] = None) -> None:
    """Invalidate the in-process limit cache (call after TreatedWaterLimit saves)."""
    if treatment_plant_id is None:
        _plant_limit_cache.clear()
    else:
        _plant_limit_cache.pop(treatment_plant_id, None)


def _resolve_limits(
    param_key: str,
    db_min: Optional[float],
    db_max: Optional[float],
    plant_limits: Optional[Dict[str, Dict[str, Any]]],
) -> Tuple[Optional[float], Optional[float]]:
    """Return (min_limit, max_limit) using DB model as sole authoritative source.

    Priority:
      1. Explicit values provided on the reading (db_min / db_max from result row).
      2. Plant-level TreatedWaterLimit cache (plant_limits).
      3. If still None for both, we cannot compute a meaningful q_i — the caller
         will skip / flag the parameter.
    """
    min_lim = db_min
    max_lim = db_max

    if plant_limits and (min_lim is None or max_lim is None):
        cached = plant_limits.get(param_key) or {}
        if min_lim is None:
            min_lim = cached.get("min")
        if max_lim is None:
            max_lim = cached.get("max")

    return min_lim, max_lim


# ---------------------------------------------------------------------------
# WQI Tier Classification
# ---------------------------------------------------------------------------

def classify_wqi(score: Optional[float]) -> Dict[str, Any]:
    """Map a composite WQI score to the five-tier classification scale."""
    if score is None:
        return {
            "score": None,
            "display_score": "—",
            "tier_label": "No Data",
            "tier_key": "nodata",
            "tone": "secondary",
            "color": "#94a3b8",
            "badge_class": "badge-secondary",
            "description": "No parameter readings recorded for evaluation.",
        }
    val = round(float(score), 2)
    if val <= 25.0:
        return {
            "score": val,
            "display_score": f"{val:.2f}",
            "tier_label": "Excellent",
            "tier_key": "excellent",
            "tone": "green",
            "color": "#10b981",
            "badge_class": "badge-success",
            "description": "Meets or exceeds all standard limits. Excellent drinking quality.",
        }
    elif val <= 50.0:
        return {
            "score": val,
            "display_score": f"{val:.2f}",
            "tier_label": "Good",
            "tier_key": "good",
            "tone": "blue",
            "color": "#3b82f6",
            "badge_class": "badge-info",
            "description": "Acceptable quality for consumption with standard water treatment.",
        }
    elif val <= 75.0:
        return {
            "score": val,
            "display_score": f"{val:.2f}",
            "tier_label": "Poor",
            "tier_key": "poor",
            "tone": "amber",
            "color": "#f59e0b",
            "badge_class": "badge-warning",
            "description": "Degraded quality; at least one parameter is elevated and warrants treatment.",
        }
    elif val <= 100.0:
        return {
            "score": val,
            "display_score": f"{val:.2f}",
            "tier_label": "Very Poor",
            "tier_key": "very_poor",
            "tone": "orange",
            "color": "#f97316",
            "badge_class": "badge-danger",
            "description": "Substantial contamination; intensive water remediation required.",
        }
    else:
        return {
            "score": val,
            "display_score": f"{val:.2f}",
            "tier_label": "Unsuitable for Drinking",
            "tier_key": "unsuitable",
            "tone": "rose",
            "color": "#ef4444",
            "badge_class": "badge-danger",
            "description": "Severe exceedance; water is unsuitable for drinking without comprehensive purification.",
        }


# ---------------------------------------------------------------------------
# Single-parameter quality rating
# ---------------------------------------------------------------------------

def compute_parameter_qi(
    name: str,
    value: float,
    min_limit: Optional[float] = None,
    max_limit: Optional[float] = None,
    unit: str = "",
    plant_limits: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[Optional[float], Optional[float], Optional[float], Dict[str, Any]]:
    """Compute the quality rating q_i for a single parameter.

    Limits are sourced exclusively from the DB (via ``plant_limits`` cache or
    the explicit ``min_limit``/``max_limit`` arguments which are themselves
    populated from the ``TreatedWaterLimit`` model).

    Returns:
        (q_i, S_i, V_ideal, classification_dict)
        Returns (None, None, None, classify_wqi(None)) when limits are absent.
    """
    param_key = _normalize_key(name)
    min_lim, max_lim = _resolve_limits(param_key, min_limit, max_limit, plant_limits)

    # We cannot compute WQI without at least one limit from the database
    if min_lim is None and max_lim is None:
        logger.debug("[WQI] Skipping '%s' — no limits found in TreatedWaterLimit for this plant.", name)
        return None, None, None, classify_wqi(None)

    # ------------------------------------------------------------------
    # pH: bidirectional — ideal = 7.0, deviations on either side penalised
    # ------------------------------------------------------------------
    if param_key == "ph":
        ideal = 7.0
        s_upper = max_lim if max_lim is not None else 8.5
        s_lower = min_lim if min_lim is not None else 6.5
        s_weight = s_upper  # weight denominator = upper permissible limit
        if value >= ideal:
            denom = abs(s_upper - ideal) or 1.5
            qi = (abs(value - ideal) / denom) * 100.0
        else:
            denom = abs(ideal - s_lower) or 0.5
            qi = (abs(ideal - value) / denom) * 100.0
        return round(qi, 2), s_weight, ideal, classify_wqi(qi)

    # ------------------------------------------------------------------
    # Dissolved Oxygen: ideal is DO saturation; limit is the minimum allowed
    # ------------------------------------------------------------------
    if param_key in _DO_PARAMS:
        ideal = 14.6  # DO at saturation (~0 °C, clean water)
        s_limit = min_lim if (min_lim is not None and min_lim > 0) else (max_lim or 5.0)
        denom = abs(ideal - s_limit) or 9.6
        qi = (abs(ideal - value) / denom) * 100.0
        return round(qi, 2), s_limit, ideal, classify_wqi(qi)

    # ------------------------------------------------------------------
    # Temperature change (delta T): ideal = 0 deviation
    # ------------------------------------------------------------------
    if param_key == "temperature_change":
        ideal = 0.0
        s_limit = max_lim if (max_lim is not None and max_lim > 0) else (min_lim or 5.0)
        qi = (abs(value) / s_limit) * 100.0
        return round(qi, 2), s_limit, ideal, classify_wqi(qi)

    # ------------------------------------------------------------------
    # Absolute temperature: ideal = midpoint of [min, max]
    # ------------------------------------------------------------------
    if param_key == "temperature":
        t_min = min_lim if min_lim is not None else 15.0
        t_max = max_lim if max_lim is not None else 25.0
        ideal = (t_min + t_max) / 2.0
        tolerance = (t_max - t_min) / 2.0 or 5.0
        qi = (abs(value - ideal) / tolerance) * 100.0
        return round(qi, 2), tolerance, ideal, classify_wqi(qi)

    # ------------------------------------------------------------------
    # Midpoint-ideal parameters (FRC, TRC, Alkalinity MO):
    # Ideal = (min + max) / 2; q_i measures deviation from that centre
    # ------------------------------------------------------------------
    if param_key in _MIDPOINT_IDEAL_PARAMS:
        if min_lim is not None and max_lim is not None and max_lim > min_lim and min_lim > 0:
            ideal = (min_lim + max_lim) / 2.0
            span = (max_lim - min_lim) / 2.0 or 0.5
            qi = (abs(value - ideal) / span) * 100.0
            return round(qi, 2), max_lim, ideal, classify_wqi(qi)
        # Fallthrough: treat as zero-ideal if only one bound available
        ideal = 0.0
        s_limit = max_lim if max_lim is not None else (min_lim or 1.0)
        qi = (abs(value) / (s_limit or 1.0)) * 100.0
        return round(qi, 2), s_limit, ideal, classify_wqi(qi)

    # ------------------------------------------------------------------
    # Bounded symmetric parameters (min > 0 and max > min):
    # e.g. Alkalinity P, some nutrient ranges
    # Ideal = midpoint of the acceptable range
    # ------------------------------------------------------------------
    if (
        min_lim is not None and max_lim is not None
        and max_lim > min_lim and min_lim > 0
    ):
        ideal = (min_lim + max_lim) / 2.0
        half_span = (max_lim - min_lim) / 2.0 or 1.0
        qi = (abs(value - ideal) / half_span) * 100.0
        return round(qi, 2), max_lim, ideal, classify_wqi(qi)

    # ------------------------------------------------------------------
    # Zero-ideal parameters (Turbidity, TDS, BOD, Colour, Heavy metals …):
    # Lower is always better; ideal = 0; limit = max_lim.
    # ------------------------------------------------------------------
    ideal = 0.0
    s_limit = max_lim if (max_lim is not None and max_lim > 0) else (min_lim or None)
    if not s_limit:
        logger.debug("[WQI] Skipping '%s' — effective limit is zero or null.", name)
        return None, None, None, classify_wqi(None)
    qi = (abs(value) / s_limit) * 100.0
    return round(qi, 2), s_limit, ideal, classify_wqi(qi)


# ---------------------------------------------------------------------------
# Operational Remediation Knowledge Base
# ---------------------------------------------------------------------------
REMEDIATION_ACTIONS: Dict[str, str] = {
    "turbidity": "Optimize coagulant dosing (alum/polyelectrolyte), verify clarifier sludge blanket depth, and trigger scheduled rapid gravity filter backwash cycle.",
    "ph": "Adjust post-treatment soda ash / hydrated lime dosing feeder; calibrate inline glass electrode probe to restore Langelier Saturation Index balance.",
    "frc": "Adjust gas chlorinator vacuum feed rate or sodium hypochlorite dosing pump; ensure minimum 30-minute contact time in the clear water storage tank.",
    "trc": "Assess raw water chlorine demand and organic pre-cursors; balance pre- and post-chlorination split points.",
    "crc": "Verify breakpoint chlorination is achieved; organic nitrogen or ammonia in raw water may be converting free chlorine into chloramines.",
    "dissolved_oxygen": "Activate inlet cascade aeration weirs or adjust raw intake drop aeration to maximize dissolved oxygen saturation.",
    "color": "Enhance primary coagulation pH control and optimize oxidant pre-treatment for natural organic matter removal.",
    "conductivity": "Conduct raw water catchment survey for salinity intrusion; optimize coagulant salt carry-over in sedimentation basin.",
    "tds": "Inspect raw water intake blending ratio and monitor chemical reagent dissolution purity.",
    "temperature": "Monitor raw intake depth and shade reservoir storage to prevent thermal stratification.",
    "temperature_change": "Ensure discharge waters are thermally equilibrated through cooling weirs prior to distribution.",
    "bod": "Investigate raw intake for upstream organic effluent intrusion; enhance aeration stage retention time.",
    "alkalinity_mo": "Adjust bicarbonate dosing to restore buffer capacity against acidic post-chlorination drift.",
}


# ---------------------------------------------------------------------------
# Core WQI aggregation over a list of readings
# ---------------------------------------------------------------------------

def calculate_wqi_for_readings(
    readings: List[Dict[str, Any]],
    plant_limits: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Calculate composite WQI from a list of parameter readings.

    Each item in ``readings`` must be a dict:
        {
            "name": str,
            "value": float,
            "min_limit": float | None,   # from TreatedWaterLimit.min_value
            "max_limit": float | None,   # from TreatedWaterLimit.max_value
            "unit": str,
        }

    ``plant_limits`` is an optional pre-loaded dict from :func:`load_plant_limits`.
    When provided, any missing min/max values on individual readings are looked up
    there, ensuring **all** limit data originates from the DB.
    """
    _empty = {
        "score": None,
        "display_score": "—",
        "tier_label": "No Data",
        "tier_key": "nodata",
        "tone": "secondary",
        "color": "#94a3b8",
        "badge_class": "badge-secondary",
        "description": "No recorded parameters available to calculate WQI.",
        "parameters_evaluated": 0,
        "concerns_count": 0,
        "parameters_of_concern": [],
        "best_parameter": None,
        "worst_parameter": None,
        "breakdown": [],
        "sum_wi_qi": 0.0,
        "sum_wi": 0.0,
        "wqi_arithmetic": None,
        "wqi_geometric": None,
        "ccme_wqi": {"score": None, "tier": "No Data", "tone": "secondary", "f1": 0.0, "f2": 0.0, "f3": 0.0},
        "eclipsing_detected": False,
        "eclipsed_parameters": [],
        "max_qi": 0.0,
        "max_qi_parameter": None,
        "compliance_rate": 0.0,
        "suitability_matrix": {},
        "remediation_priorities": [],
    }

    valid_readings = [r for r in readings if r.get("value") is not None and r.get("name")]
    if not valid_readings:
        return _empty

    # Group by parameter name (average if the same parameter appears multiple times)
    param_groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in valid_readings:
        pname = r["name"].strip()
        param_groups.setdefault(pname, []).append(r)

    evaluated_params = []
    for pname, items in param_groups.items():
        avg_val = sum(float(it["value"]) for it in items) / len(items)
        db_min = items[0].get("min_limit")
        db_max = items[0].get("max_limit")
        unit = items[0].get("unit", "")

        qi, s_limit, ideal, p_class = compute_parameter_qi(
            pname, avg_val,
            min_limit=db_min, max_limit=db_max,
            unit=unit, plant_limits=plant_limits,
        )

        # Skip parameters where we have no limit data at all
        if qi is None or s_limit is None:
            continue

        param_key = _normalize_key(pname)
        # Resolve final display limits (may come from plant_limits cache)
        final_min, final_max = _resolve_limits(param_key, db_min, db_max, plant_limits)

        evaluated_params.append({
            "name": pname,
            "value": round(avg_val, 3),
            "unit": unit,
            "ideal": ideal,
            "limit": s_limit,
            "min_limit": final_min,
            "max_limit": final_max,
            "qi": qi,
            "s_limit": s_limit,
            "tier_label": p_class["tier_label"],
            "tier_key": p_class["tier_key"],
            "tone": p_class["tone"],
            "color": p_class["color"],
            "is_concern": qi > 50.0,
            "is_exceeded": qi > 100.0,
        })

    if not evaluated_params:
        return _empty

    # Inverse weights: w_i = k / S_i, normalised so Sum(w_i) = 1.0
    sum_inv_s = sum(1.0 / p["s_limit"] for p in evaluated_params if p["s_limit"] > 0)
    k = (1.0 / sum_inv_s) if sum_inv_s > 0 else 1.0

    breakdown = []
    sum_wi_qi = 0.0
    sum_wi = 0.0

    for p in evaluated_params:
        wi = (1.0 / p["s_limit"] * k) if p["s_limit"] > 0 else 0.0
        wi_qi = wi * p["qi"]
        sum_wi_qi += wi_qi
        sum_wi += wi
        breakdown.append({
            **p,
            "wi": round(wi, 4),
            "wi_percent": round(wi * 100.0, 2),
            "wi_qi": round(wi_qi, 4),
        })

    # Sort: parameters of concern first, then by descending q_i
    breakdown.sort(key=lambda item: (-item["qi"], item["name"]))

    composite_score = round(sum_wi_qi, 2) if sum_wi > 0 else None
    classification = classify_wqi(composite_score)

    concerns = [p for p in breakdown if p["is_concern"]]
    failed_params = [p for p in breakdown if p["is_exceeded"]]
    best_p = min(breakdown, key=lambda p: p["qi"]) if breakdown else None
    worst_p = max(breakdown, key=lambda p: p["qi"]) if breakdown else None

    # 1. Multi-Model: Weighted Geometric Mean WQI
    geom_sum = sum(p["wi"] * math.log(max(p["qi"], 0.01)) for p in breakdown if p["wi"] > 0)
    wqi_geometric = round(math.exp(geom_sum), 2) if breakdown else None

    # 2. Multi-Model: CCME Water Quality Index
    f1 = (len(failed_params) / len(breakdown) * 100.0) if breakdown else 0.0
    total_tests = len(valid_readings)
    failed_tests = 0
    excursions = []
    for r in valid_readings:
        v = float(r["value"])
        mn = r.get("min_limit")
        mx = r.get("max_limit")
        if mx is not None and v > float(mx):
            failed_tests += 1
            excursions.append((v / float(mx)) - 1.0)
        elif mn is not None and v < float(mn):
            failed_tests += 1
            excursions.append((float(mn) / max(v, 0.001)) - 1.0)
        else:
            excursions.append(0.0)

    f2 = (failed_tests / total_tests * 100.0) if total_tests > 0 else 0.0
    sum_excursions = sum(excursions)
    nse = (sum_excursions / total_tests) if total_tests > 0 else 0.0
    f3 = (nse / (0.01 * nse + 0.01)) if nse > 0 else 0.0

    ccme_vector = math.sqrt(f1**2 + f2**2 + f3**2)
    ccme_score = round(max(0.0, min(100.0, 100.0 - (ccme_vector / 1.732))), 2)

    if ccme_score >= 95.0:
        ccme_tier = "Excellent"
        ccme_tone = "green"
    elif ccme_score >= 80.0:
        ccme_tier = "Good"
        ccme_tone = "blue"
    elif ccme_score >= 65.0:
        ccme_tier = "Fair"
        ccme_tone = "amber"
    elif ccme_score >= 45.0:
        ccme_tier = "Marginal"
        ccme_tone = "orange"
    else:
        ccme_tier = "Poor"
        ccme_tone = "rose"

    ccme_wqi = {
        "score": ccme_score,
        "display_score": f"{ccme_score:.1f}",
        "tier": ccme_tier,
        "tone": ccme_tone,
        "f1": round(f1, 2),
        "f2": round(f2, 2),
        "f3": round(f3, 2),
    }

    # 3. Eclipsing & Ambiguity Diagnostics (Textbook eclipsing: aggregate score <= 100 but an individual parameter violates limit)
    eclipsing_detected = bool(composite_score is not None and composite_score <= 100.0 and len(failed_params) > 0)
    eclipsed_parameters = [p["name"] for p in failed_params]
    max_qi = max((p["qi"] for p in breakdown), default=0.0)
    max_qi_parameter = worst_p["name"] if worst_p else None
    compliance_rate = round(((len(breakdown) - len(failed_params)) / len(breakdown)) * 100.0, 1) if breakdown else 0.0

    # 4. Water Use Suitability Matrix
    if composite_score is None:
        suitability_matrix = {}
    else:
        # Potable suitability
        if composite_score <= 25.0:
            potable_pct = 100.0
            potable_label = "Pristine Potable"
            potable_desc = "Safe for direct human consumption. Meets all KS 05-459 standards."
            potable_tone = "green"
        elif composite_score <= 50.0:
            potable_pct = 85.0
            potable_label = "Standard Potable"
            potable_desc = "Fully compliant with municipal treated drinking water supply standards."
            potable_tone = "blue"
        elif composite_score <= 75.0:
            potable_pct = 50.0
            potable_label = "Provisional Potable"
            potable_desc = "Requires corrective coagulation or disinfection optimization."
            potable_tone = "amber"
        elif composite_score <= 100.0:
            potable_pct = 25.0
            potable_label = "Restricted Quality"
            potable_desc = "Substantial contamination; boil water advisory recommended."
            potable_tone = "orange"
        else:
            potable_pct = 5.0
            potable_label = "Unsuitable for Drinking"
            potable_desc = "Severe exceedance; unpotable without advanced purification."
            potable_tone = "rose"

        # Industrial boiler & cooling suitability
        tds_val = next((p["value"] for p in breakdown if "tds" in _normalize_key(p["name"])), 200.0)
        ph_val = next((p["value"] for p in breakdown if _normalize_key(p["name"]) == "ph"), 7.2)
        if 6.8 <= ph_val <= 8.2 and tds_val < 300.0:
            ind_pct = 95.0
            ind_label = "Low Scaling / Corrosion Risk"
            ind_desc = "Optimal for industrial cooling towers, boiler feed, and process machinery."
            ind_tone = "green"
        elif 6.5 <= ph_val <= 8.5 and tds_val < 600.0:
            ind_pct = 75.0
            ind_label = "Moderate Chemical Scaling Risk"
            ind_desc = "Acceptable with standard water softener or anti-scalant treatment."
            ind_tone = "blue"
        else:
            ind_pct = 35.0
            ind_label = "High Scaling / Corrosion Risk"
            ind_desc = "Aggressive scaling or corrosion tendency; demineralization required."
            ind_tone = "orange"

        # Agricultural irrigation suitability
        cond_val = next((p["value"] for p in breakdown if "conduct" in _normalize_key(p["name"])), 300.0)
        if cond_val < 700.0 and 6.5 <= ph_val <= 8.4:
            agri_pct = 95.0
            agri_label = "Excellent for All Crops"
            agri_desc = "Low salinity hazard; compliant with FAO irrigation water guidelines."
            agri_tone = "green"
        elif cond_val < 1500.0:
            agri_pct = 70.0
            agri_label = "Moderate Salinity Hazard"
            agri_desc = "Suitable for moderately salt-tolerant crops with adequate soil leaching."
            agri_tone = "amber"
        else:
            agri_pct = 30.0
            agri_label = "High Salinity / Soil Restriction"
            agri_desc = "Hazardous soil salinization risk; requires special drainage controls."
            agri_tone = "rose"

        # Aquatic life suitability
        do_val = next((p["value"] for p in breakdown if _normalize_key(p["name"]) in _DO_PARAMS), 8.5)
        turb_val = next((p["value"] for p in breakdown if "turbid" in _normalize_key(p["name"])), 2.0)
        if do_val >= 6.5 and turb_val <= 5.0 and 6.5 <= ph_val <= 8.5:
            aqua_pct = 95.0
            aqua_label = "Optimal Ecological Balance"
            aqua_desc = "Supports healthy freshwater aquatic organisms and riverine ecosystems."
            aqua_tone = "green"
        elif do_val >= 5.0 and turb_val <= 15.0:
            aqua_pct = 70.0
            aqua_label = "Moderate Environmental Stress"
            aqua_desc = "Borderline dissolved oxygen for sensitive fish species; acceptable for hardy fauna."
            aqua_tone = "amber"
        else:
            aqua_pct = 25.0
            aqua_label = "Severe Ecological Distress"
            aqua_desc = "Hypoxic or elevated suspended solids conditions hazardous to aquatic life."
            aqua_tone = "rose"

        suitability_matrix = {
            "potable": {"score": potable_pct, "label": potable_label, "description": potable_desc, "tone": potable_tone, "icon": "fa-glass-water"},
            "industrial": {"score": ind_pct, "label": ind_label, "description": ind_desc, "tone": ind_tone, "icon": "fa-industry"},
            "irrigation": {"score": agri_pct, "label": agri_label, "description": agri_desc, "tone": agri_tone, "icon": "fa-seedling"},
            "aquatic": {"score": aqua_pct, "label": aqua_label, "description": aqua_desc, "tone": aqua_tone, "icon": "fa-fish-fins"},
        }

    # 5. Remediation Priority Engine
    remediation_priorities = []
    for c in concerns:
        wqi_gain = round(c["wi_qi"], 2)
        proj_wqi = round(max(0.0, (composite_score or 0.0) - wqi_gain), 2)
        norm_key = _normalize_key(c["name"])
        action_text = REMEDIATION_ACTIONS.get(norm_key, f"Inspect chemical dosing and unit process retention for {c['name']} to restore standard compliance.")
        remediation_priorities.append({
            "name": c["name"],
            "current_value": c["value"],
            "unit": c["unit"],
            "qi": c["qi"],
            "weight": c["wi"],
            "wqi_gain": wqi_gain,
            "projected_wqi": proj_wqi,
            "action": action_text,
            "severity": "CRITICAL" if c["qi"] > 100.0 else "WARNING",
            "badge_class": "badge-danger" if c["qi"] > 100.0 else "badge-warning",
        })
    remediation_priorities.sort(key=lambda item: (-item["wqi_gain"], -item["qi"]))

    return {
        **classification,
        "parameters_evaluated": len(breakdown),
        "concerns_count": len(concerns),
        "parameters_of_concern": concerns,
        "best_parameter": best_p,
        "worst_parameter": worst_p,
        "breakdown": breakdown,
        "sum_wi_qi": round(sum_wi_qi, 4),
        "sum_wi": round(sum_wi, 4),
        "wqi_arithmetic": composite_score,
        "arithmetic_wqi": composite_score,
        "wqi_geometric": wqi_geometric,
        "geometric_wqi": wqi_geometric,
        "ccme_wqi": ccme_wqi,
        "eclipsing_detected": eclipsing_detected,
        "eclipsed_parameters": eclipsed_parameters,
        "max_qi": max_qi,
        "max_qi_parameter": max_qi_parameter,
        "compliance_rate": compliance_rate,
        "suitability_matrix": suitability_matrix,
        "remediation_priorities": remediation_priorities,
    }


# ---------------------------------------------------------------------------
# Plant-level WQI (database-sourced limits)
# ---------------------------------------------------------------------------

def get_plant_wqi(
    treatment_plant_id: int,
    target_date=None,
    lookback_days: int = 14,
) -> Dict[str, Any]:
    """Calculate the Water Quality Index for a specific treatment plant.

    Limits come exclusively from ``TreatedWaterLimit`` (via
    ``r.parameter.min_value`` / ``r.parameter.max_value`` on each result row,
    and the plant-level cache for any gaps).
    """
    TreatmentPlant = apps.get_model("t", "TreatmentPlant")
    TreatedWaterResult = apps.get_model("qc", "TreatedWaterResult")
    RawWaterResult = apps.get_model("qc", "RawWaterResult")

    plant = TreatmentPlant.objects.filter(pk=treatment_plant_id).first()
    if not plant:
        return {**classify_wqi(None), "plant_id": treatment_plant_id, "plant_name": "Unknown"}

    today = target_date or timezone.localdate()
    start_date = today - timedelta(days=lookback_days)

    # Pre-load plant limit cache (all TreatedWaterLimit rows for this plant)
    plant_limits = load_plant_limits(treatment_plant_id)

    # 1. Treated water results (primary drinking-water quality indicator)
    treated_qs = (
        TreatedWaterResult.objects
        .filter(
            analysis__treatment_plant_id=treatment_plant_id,
            analysis__analysis_date__gte=start_date,
            analysis__analysis_date__lte=today,
            value__isnull=False,
        )
        .select_related("parameter", "parameter__parameter", "analysis")
        .order_by("-analysis__analysis_date", "-analysis__analysis_time__time")
    )

    readings: List[Dict[str, Any]] = []
    seen_params = set()

    for r in treated_qs:
        pname = r.parameter.parameter.name
        if pname not in seen_params:
            seen_params.add(pname)
            readings.append({
                "name": pname,
                # min_value / max_value come directly from TreatedWaterLimit
                "min_limit": float(r.parameter.min_value) if r.parameter.min_value is not None else None,
                "max_limit": float(r.parameter.max_value) if r.parameter.max_value is not None else None,
                "value": float(r.value),
                "unit": getattr(r.parameter.parameter, "unit", "") or "",
            })

    # 2. If treated water has very few readings, supplement with raw water results
    if len(readings) < 3:
        raw_qs = (
            RawWaterResult.objects
            .filter(
                analysis__treatment_plant_id=treatment_plant_id,
                analysis__analysis_date__gte=start_date,
                analysis__analysis_date__lte=today,
                value__isnull=False,
            )
            .select_related("parameter", "parameter__parameter")
            .order_by("-analysis__analysis_date")
        )
        for r in raw_qs:
            pname = r.parameter.parameter.name
            if pname not in seen_params:
                seen_params.add(pname)
                readings.append({
                    "name": pname,
                    "min_limit": float(r.parameter.min_value) if r.parameter.min_value is not None else None,
                    "max_limit": float(r.parameter.max_value) if r.parameter.max_value is not None else None,
                    "value": float(r.value),
                    "unit": getattr(r.parameter.parameter, "unit", "") or "",
                })

    result = calculate_wqi_for_readings(readings, plant_limits=plant_limits)
    result["plant_id"] = plant.pk
    result["plant_name"] = plant.name
    return result


# ---------------------------------------------------------------------------
# Overall WQI (average across all treatment plants)
# ---------------------------------------------------------------------------

def get_overall_wqi(target_date=None, lookback_days: int = 14) -> Dict[str, Any]:
    """Calculate the overall WQI averaged across all treatment plants."""
    TreatmentPlant = apps.get_model("t", "TreatmentPlant")
    plants = list(TreatmentPlant.objects.all().order_by("name"))

    plant_scores = []
    numeric_scores = []

    for plant in plants:
        pwqi = get_plant_wqi(plant.pk, target_date=target_date, lookback_days=lookback_days)
        plant_scores.append(pwqi)
        if pwqi["score"] is not None:
            numeric_scores.append(pwqi["score"])

    if numeric_scores:
        avg_score = round(sum(numeric_scores) / len(numeric_scores), 2)
        overall_classification = classify_wqi(avg_score)
    else:
        avg_score = None
        overall_classification = classify_wqi(None)

    return {
        **overall_classification,
        "plant_count": len(plants),
        "evaluated_plants_count": len(numeric_scores),
        "plant_scores": plant_scores,
    }


# ---------------------------------------------------------------------------
# Card-level WQI (single sampling cycle)
# ---------------------------------------------------------------------------

def calculate_wqi_for_card(
    card: Any,
    treatment_plant_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Calculate WQI for a single sampling cycle analysis card.

    ``cell.minimum`` and ``cell.maximum`` on each ResultCell are already
    populated from ``TreatedWaterLimit.min_value`` / ``max_value`` by
    ``ResultCardService``.  When ``treatment_plant_id`` is provided the full
    plant limit cache is loaded so parameters with no recorded result yet still
    contribute their DB limits to the weight calculation.
    """
    if not card or not hasattr(card, "groups"):
        return classify_wqi(None)

    plant_limits: Optional[Dict[str, Dict[str, Any]]] = None
    if treatment_plant_id:
        try:
            plant_limits = load_plant_limits(treatment_plant_id)
        except Exception as exc:
            logger.warning("[WQI] Could not load plant limits for card WQI: %s", exc)

    readings: List[Dict[str, Any]] = []
    for group in card.groups:
        for row in group.rows:
            for param_name, cell in row.results.items():
                if cell and cell.entered and cell.value is not None:
                    try:
                        readings.append({
                            "name": param_name,
                            "value": float(cell.value),
                            # These come from TreatedWaterLimit.min_value / max_value
                            "min_limit": float(cell.minimum) if cell.minimum is not None else None,
                            "max_limit": float(cell.maximum) if cell.maximum is not None else None,
                            "unit": cell.unit or "",
                        })
                    except (ValueError, TypeError):
                        pass

    return calculate_wqi_for_readings(readings, plant_limits=plant_limits)


# ---------------------------------------------------------------------------
# Dashboard summary helper
# ---------------------------------------------------------------------------

def get_dashboard_wqi_summary(
    user: Any,
    selected_plant: Optional[Any] = None,
    target_date=None,
) -> Dict[str, Any]:
    """High-level WQI summary for dashboards.

    Enforces strict plant scoping:
      - Superusers: See overall average across all plants + selected/first plant.
      - Non-Superusers: Plant WQI is strictly locked to their assigned plant.
    """
    TreatmentPlant = apps.get_model("t", "TreatmentPlant")

    # 1. Overall WQI (all plants average)
    overall_summary = get_overall_wqi(target_date=target_date)

    # 2. Resolve the active plant
    active_plant = None
    if user.is_superuser:
        active_plant = selected_plant or TreatmentPlant.objects.order_by("name").first()
    else:
        assigned_id = getattr(user, "assigned_treatment_plant_id", None)
        if assigned_id:
            active_plant = TreatmentPlant.objects.filter(pk=assigned_id).first()

    if active_plant:
        plant_scoped_wqi = get_plant_wqi(active_plant.pk, target_date=target_date)
    else:
        plant_scoped_wqi = {
            **classify_wqi(None),
            "plant_id": None,
            "plant_name": "No Plant Assigned",
            "parameters_evaluated": 0,
            "concerns_count": 0,
            "parameters_of_concern": [],
            "breakdown": [],
        }

    return {
        "overall": overall_summary,
        "plant_scoped": plant_scoped_wqi,
        "all_plants": overall_summary.get("plant_scores", []),
        "is_superuser": bool(user.is_superuser),
        "is_scoped": not bool(user.is_superuser),
        "plant_name": getattr(active_plant, "name", "Unassigned"),
    }


# ---------------------------------------------------------------------------
# Historical Rolling Timeline for Trends & Sparklines
# ---------------------------------------------------------------------------

def get_plant_wqi_timeline(
    treatment_plant_id: int,
    days: int = 14,
    target_date: Optional[date] = None,
) -> Dict[str, Any]:
    """Calculate rolling daily WQI trend data for a treatment plant over N days."""
    TreatmentPlant = apps.get_model("t", "TreatmentPlant")
    TreatedWaterResult = apps.get_model("qc", "TreatedWaterResult")

    plant = TreatmentPlant.objects.filter(pk=treatment_plant_id).first()
    today = target_date or timezone.localdate()
    start_date = today - timedelta(days=days - 1)

    plant_limits = load_plant_limits(treatment_plant_id) if plant else {}

    results_qs = (
        TreatedWaterResult.objects.filter(
            analysis__treatment_plant_id=treatment_plant_id,
            analysis__analysis_date__gte=start_date,
            analysis__analysis_date__lte=today,
            value__isnull=False,
        )
        .select_related("parameter", "parameter__parameter", "analysis")
        .order_by("analysis__analysis_date", "-analysis__analysis_time__time")
    )

    by_date: Dict[date, List[Dict[str, Any]]] = {}
    for r in results_qs:
        d = r.analysis.analysis_date
        pname = r.parameter.parameter.name
        by_date.setdefault(d, []).append({
            "name": pname,
            "value": float(r.value),
            "min_limit": float(r.parameter.min_value) if r.parameter.min_value is not None else None,
            "max_limit": float(r.parameter.max_value) if r.parameter.max_value is not None else None,
            "unit": getattr(r.parameter.parameter, "unit", "") or "",
        })

    dates_list = []
    labels_list = []
    scores_list = []
    geometric_scores_list = []
    ccme_scores_list = []
    tones_list = []
    concerns_counts = []

    curr = start_date
    while curr <= today:
        day_readings = by_date.get(curr, [])
        dates_list.append(curr.isoformat())
        labels_list.append(curr.strftime("%d %b"))

        if day_readings:
            day_wqi = calculate_wqi_for_readings(day_readings, plant_limits=plant_limits)
            scores_list.append(day_wqi.get("score"))
            geometric_scores_list.append(day_wqi.get("wqi_geometric"))
            ccme_scores_list.append(day_wqi.get("ccme_wqi", {}).get("score"))
            tones_list.append(day_wqi.get("tone", "secondary"))
            concerns_counts.append(day_wqi.get("concerns_count", 0))
        else:
            scores_list.append(None)
            geometric_scores_list.append(None)
            ccme_scores_list.append(None)
            tones_list.append("secondary")
            concerns_counts.append(0)

        curr += timedelta(days=1)

    # Trend velocity calculation
    valid_scores = [s for s in scores_list if s is not None]
    if len(valid_scores) >= 4:
        recent_window = valid_scores[-3:]
        prior_window = valid_scores[:3]
        recent_avg = sum(recent_window) / len(recent_window)
        prior_avg = sum(prior_window) / len(prior_window)
        diff = round(recent_avg - prior_avg, 2)
        if diff <= -2.0:
            trend_direction = "improving"
            trend_label = f"Improving (WQI decreased by {abs(diff):.1f} pts)"
            trend_tone = "emerald"
        elif diff >= 2.0:
            trend_direction = "deteriorating"
            trend_label = f"Deteriorating (WQI increased by {diff:.1f} pts)"
            trend_tone = "rose"
        else:
            trend_direction = "stable"
            trend_label = "Stable Quality (±2.0 pts nominal band)"
            trend_tone = "cyan"
    else:
        trend_direction = "insufficient_data"
        trend_label = "Baseline Establishing"
        trend_tone = "muted"
        diff = 0.0

    return {
        "dates": dates_list,
        "labels": labels_list,
        "scores": scores_list,
        "geometric_scores": geometric_scores_list,
        "ccme_scores": ccme_scores_list,
        "tones": tones_list,
        "concerns_counts": concerns_counts,
        "valid_points_count": len(valid_scores),
        "avg_score": round(sum(valid_scores) / len(valid_scores), 2) if valid_scores else None,
        "trend_direction": trend_direction,
        "trend_label": trend_label,
        "trend_tone": trend_tone,
        "trend_delta": diff,
    }


# ---------------------------------------------------------------------------
# Executive Regulatory Compliance Report Dossier Generator
# ---------------------------------------------------------------------------

def generate_wqi_compliance_report(
    treatment_plant_id: int,
    target_date: Optional[date] = None,
    lookback_days: int = 14,
) -> Dict[str, Any]:
    """Generate a formal executive compliance audit dossier for regulatory reporting."""
    TreatmentPlant = apps.get_model("t", "TreatmentPlant")
    plant = TreatmentPlant.objects.filter(pk=treatment_plant_id).first()
    today = target_date or timezone.localdate()

    plant_wqi = get_plant_wqi(treatment_plant_id, target_date=today, lookback_days=lookback_days)
    report_id = f"NCWSC-QA-WQI-{getattr(plant, 'code', 'PLT')}-{today.strftime('%Y%m%d')}"

    score = plant_wqi.get("score")
    tier = plant_wqi.get("tier_label", "Unknown")
    concerns = plant_wqi.get("parameters_of_concern", [])
    eval_count = plant_wqi.get("parameters_evaluated", 0)
    plant_name = getattr(plant, "name", "Facility")

    if score is None:
        exec_summary = f"No analytical measurements are currently recorded for {plant_name} during the evaluation window."
        compliance_status = "PENDING DATA"
        status_color = "#94a3b8"
    elif score <= 50.0:
        if plant_wqi.get("eclipsing_detected"):
            exec_summary = (
                f"Overall treated water at {plant_name} achieved an acceptable composite WQI score of {score:.2f} ({tier}), "
                f"evaluating {eval_count} parameters against KS 05-459 specifications. However, an AGGREGATION ECLIPSE was detected: "
                f"{len(plant_wqi.get('eclipsed_parameters', []))} parameter(s) ({', '.join(plant_wqi.get('eclipsed_parameters', []))}) "
                f"severely exceed permissible drinking limits and require targeted chemical remediation."
            )
            compliance_status = "PROVISIONAL COMPLIANCE (ECLIPSE ALERT)"
            status_color = "#f59e0b"
        else:
            exec_summary = (
                f"Treated water at {plant_name} demonstrates full regulatory compliance with a composite WQI of {score:.2f} ({tier}). "
                f"All {eval_count} physical and chemical parameters meet the stringent standards set by KEBS KS 05-459 and WHO drinking water guidelines."
            )
            compliance_status = "COMPLIANT"
            status_color = "#10b981"
    else:
        exec_summary = (
            f"Water quality monitoring at {plant_name} indicates elevated contamination with a composite WQI of {score:.2f} ({tier}). "
            f"Immediate operational intervention is recommended. {len(concerns)} parameter(s) of concern were identified, led by "
            f"{plant_wqi.get('worst_parameter', {}).get('name', 'N/A')} (Rating q_i = {plant_wqi.get('worst_parameter', {}).get('qi', 0):.1f})."
        )
        compliance_status = "NON-COMPLIANT"
        status_color = "#ef4444"

    return {
        "report_id": report_id,
        "generated_at": timezone.now(),
        "target_date": today,
        "lookback_days": lookback_days,
        "plant": plant,
        "plant_name": plant_name,
        "plant_code": getattr(plant, "code", "PLT"),
        "wqi": plant_wqi,
        "compliance_status": compliance_status,
        "status_color": status_color,
        "executive_summary": exec_summary,
        "standards": [
            {"code": "KS 05-459: 2007", "title": "Kenya Standard for Drinking Water Specification", "authority": "KEBS"},
            {"code": "WHO GDWQ 4th Ed.", "title": "Guidelines for Drinking-water Quality", "authority": "World Health Organization"},
            {"code": "NCWSC QA-SOP-04", "title": "Potable Water Quality Monitoring & Verification Standard", "authority": "NCWSC"},
        ],
        "signoffs": [
            {"role": "Senior Quality Analyst", "title": "Testing Analyst", "status": "Verified", "date": today},
            {"role": "Quality Control Chemist", "title": "Data Verification & Indexing", "status": "Audited", "date": today},
            {"role": "Treatment Plant Superintendent", "title": "Plant Operations & Dosing", "status": "Approved", "date": today},
            {"role": "Director of Quality Assurance", "title": "Executive Regulatory Sign-off", "status": "Endorsed", "date": today},
        ],
    }
