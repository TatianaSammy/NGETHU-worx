from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Set, Tuple

from django.utils import timezone
from django.db.models import Max

from t.models import TreatmentPlant, TreatmentPlantPhase
from instruments.models import (
    Instrument,
    InstrumentStatus,
    Sensor,
    OnlineMeasurement,
    QualityFlag,
    TreatmentStage,
)
from parameters.models import PhysicochemicalParameter
from qc.models import (
    WaterAnalysis,
    RawWaterResult,
    CoagulatedWaterResult,
    ClarifiedWaterResult,
    TreatedWaterResult,
)
from qc.services.result_card_service import ResultCardService
from qc.services.operational_summary import get_operational_summary, get_filter_sidebar_groups
from instruments.services.telemetry_dashboard_service import (
    TelemetryDashboardService,
    DashboardKPIs,
    TelemetryStageGroup,
)

logger = logging.getLogger(__name__)


# Tolerance thresholds for key water parameters to trigger anomaly alerts
ANOMALY_TOLERANCES = {
    "ph": {"abs_diff": Decimal("0.35"), "pct_diff": 5.0, "unit": "pH units"},
    "turbidity": {"abs_diff": Decimal("1.50"), "pct_diff": 15.0, "unit": "NTU"},
    "residual chlorine": {"abs_diff": Decimal("0.25"), "pct_diff": 15.0, "unit": "mg/L"},
    "free chlorine": {"abs_diff": Decimal("0.25"), "pct_diff": 15.0, "unit": "mg/L"},
    "conductivity": {"abs_diff": Decimal("30.0"), "pct_diff": 10.0, "unit": "µS/cm"},
    "color": {"abs_diff": Decimal("5.0"), "pct_diff": 20.0, "unit": "HU"},
    "temperature": {"abs_diff": Decimal("2.0"), "pct_diff": 10.0, "unit": "°C"},
}


@dataclass(slots=True)
class ParameterComparisonCell:
    parameter_name: str
    stage_key: str
    phase_id: Optional[int]
    phase_name: str
    unit: str

    # Sensor Telemetry
    sensor_id: Optional[str] = None
    sensor_name: str = ""
    instrument_name: str = ""
    instrument_status: str = "offline"
    sensor_value: Optional[Decimal] = None
    sensor_display: str = "—"
    sensor_measured_at: Optional[datetime] = None
    sensor_time_ago: str = "—"
    sensor_quality: str = "ok"
    sensor_stale: bool = False

    # Manual Lab Entry
    manual_value: Optional[Decimal] = None
    manual_display: str = "—"
    manual_entered: bool = False
    manual_abnormal: bool = False

    # Reconciliation & Divergence
    delta_abs: Optional[Decimal] = None
    delta_display: str = "—"
    pct_variance: Optional[float] = None
    variance_display: str = "—"
    reconciliation_status: str = "unmonitored"  # in_synch, minor_drift, critical_mismatch, sensor_offline, lab_pending, unmonitored
    status_label: str = "Unmonitored"
    severity_level: str = "info"  # success, warning, danger, info, muted
    advisory: str = ""
    is_anomaly: bool = False


@dataclass(slots=True)
class SCADAStageSummary:
    stage_key: str
    stage_title: str
    order: int
    health_score: int = 100
    status_badge: str = "NOMINAL"
    status_color: str = "#10b981"
    total_parameters: int = 0
    in_synch_count: int = 0
    drift_count: int = 0
    critical_count: int = 0
    sensor_coverage_pct: int = 0
    manual_completion_pct: int = 0
    comparisons: List[ParameterComparisonCell] = field(default_factory=list)


@dataclass(slots=True)
class SCADAAlarmItem:
    alarm_id: str
    stage_title: str
    phase_name: str
    parameter_name: str
    severity: str  # CRITICAL, WARNING, ADVISORY
    badge_class: str
    title: str
    description: str
    sensor_value_display: str
    manual_value_display: str
    delta_display: str
    advisory: str
    detected_at: datetime = field(default_factory=timezone.now)


class SCADADashboardService:
    """
    Unified SCADA Analytics Service.
    Orchestrates the reconciliation between automated field sensor telemetry (instruments)
    and laboratory QC manual analytical entry worksheets (qc/results/cards).
    """

    STAGE_MAPPINGS = {
        "raw": TreatmentStage.RAW_WATER,
        "coagulated": TreatmentStage.COAGULATED,
        "clarified": TreatmentStage.CLARIFIED,
        "filtered": TreatmentStage.FILTERED,
        "treated": TreatmentStage.TREATED,
    }

    REVERSE_STAGE_MAPPINGS = {v: k for k, v in STAGE_MAPPINGS.items()}

    def __init__(
        self,
        plant: Optional[TreatmentPlant],
        selected_date: Optional[date] = None,
        user: Optional[Any] = None,
    ):
        self.plant = plant
        self.selected_date = selected_date or timezone.localdate()
        self.user = user

    def build_unified_scada_context(self, active_tab: str = "overview") -> Dict[str, Any]:
        """
        Builds the complete multi-tab SCADA dashboard state.
        """
        now = timezone.now()
        plants = list(TreatmentPlant.objects.all().order_by("name"))

        if not self.plant and plants:
            self.plant = plants[0]

        # -------------------------------------------------------------
        # 1. Telemetry Dashboard (Sensors & Gateways)
        # -------------------------------------------------------------
        telemetry_service = TelemetryDashboardService(plant=self.plant)
        telemetry_matrix = telemetry_service.build_telemetry_matrix()
        telemetry_kpis = telemetry_service.get_kpis()

        # -------------------------------------------------------------
        # 2. Lab Cards Context (Manual Analytical Worksheets)
        # -------------------------------------------------------------
        cards_context = ResultCardService.get_context(
            treatment_plant=self.plant,
            selected_date=self.selected_date,
        )

        # Operational dosages and phase chemical concentrations
        current_dosages, phase_concentrations = get_operational_summary(self.plant)
        filter_phase_groups = get_filter_sidebar_groups(self.plant, self.user) if self.plant else []

        # -------------------------------------------------------------
        # 3. Comparative Reconciliation Engine (Sensor vs Lab Manual)
        # -------------------------------------------------------------
        stage_comparisons, scada_alarms = self._reconcile_streams(
            telemetry_matrix=telemetry_matrix,
            cards_context=cards_context,
        )

        # -------------------------------------------------------------
        # 4. Global SCADA Health & Agreement Metrics
        # -------------------------------------------------------------
        total_reconciled = sum(len(stage.comparisons) for stage in stage_comparisons)
        total_in_synch = sum(stage.in_synch_count for stage in stage_comparisons)
        total_drifts = sum(stage.drift_count for stage in stage_comparisons)
        total_critical = sum(stage.critical_count for stage in stage_comparisons)

        agreement_pct = int(round((total_in_synch / total_reconciled) * 100)) if total_reconciled > 0 else 100
        scada_health_index = max(0, min(100, int(round((agreement_pct * 0.6) + (telemetry_kpis.overall_health_pct * 0.4)))))

        system_status = "SYSTEM NOMINAL"
        system_status_class = "status-nominal"
        if total_critical > 0 or telemetry_kpis.danger_sensors > 0:
            system_status = f"{total_critical + telemetry_kpis.danger_sensors} CRITICAL ALARMS"
            system_status_class = "status-critical"
        elif total_drifts > 0 or telemetry_kpis.warning_sensors > 0:
            system_status = f"{total_drifts + telemetry_kpis.warning_sensors} SENSOR DRIFTS"
            system_status_class = "status-warning"

        # -------------------------------------------------------------
        # 5. Gateway / Instruments Fleet Diagnostics
        # -------------------------------------------------------------
        fleet_instruments = []
        if self.plant:
            fleet_instruments = list(
                Instrument.objects.filter(treatment_plant=self.plant, is_active=True)
                .select_related("phase")
                .prefetch_related("sensors", "calibrations")
                .order_by("name")
            )

        return {
            "plant": self.plant,
            "selected_plant": self.plant,
            "plants": plants,
            "available_plants": plants,
            "selected_date": self.selected_date,
            "active_tab": active_tab,
            "last_synced": now,

            # SCADA Meta
            "system_status": system_status,
            "system_status_class": system_status_class,
            "scada_health_index": scada_health_index,
            "agreement_pct": agreement_pct,
            "total_critical_alarms": total_critical,
            "total_drift_warnings": total_drifts,

            # Process Pipeline & Stage Reconciliation
            "stages": stage_comparisons,
            "alarms": scada_alarms,

            # Raw Telemetry Subsystem
            "telemetry_matrix": telemetry_matrix,
            "telemetry_kpis": telemetry_kpis,
            "fleet_instruments": fleet_instruments,

            # QC Lab Cards Subsystem
            "cards": cards_context.get("cards", []),
            "current_analysis": cards_context.get("current_analysis"),
            "current_time_label": cards_context.get("current_time_label"),
            "completion": cards_context.get("completion", {}),
            "raw_water_snapshot": cards_context.get("raw_water_snapshot", {}),
            "analysis_watermark": cards_context.get("analysis_watermark", 0),
            "current_dosages": current_dosages,
            "phase_concentrations": phase_concentrations,
            "filter_phase_groups": filter_phase_groups,
        }

    def _reconcile_streams(
        self,
        telemetry_matrix: List[TelemetryStageGroup],
        cards_context: Dict[str, Any],
    ) -> Tuple[List[SCADAStageSummary], List[SCADAAlarmItem]]:
        """
        Aligns field sensor telemetry with laboratory worksheet entries.
        Generates comparative variance deltas and automated SCADA advisories.
        """
        # Map manual card cells by: (stage_slug, phase_name.lower(), param_name.lower()) -> ResultCell
        manual_results_map: Dict[Tuple[str, str, str], Any] = {}
        cards = cards_context.get("cards", [])
        if cards:
            active_card = cards[0]
            for group in getattr(active_card, "groups", []):
                stage_slug = (getattr(group, "water_type", "") or "").lower()
                for row in getattr(group, "rows", []):
                    phase_name = getattr(row, "name", "").strip().lower()
                    for p_name, cell in getattr(row, "results", {}).items():
                        key = (stage_slug, phase_name, p_name.strip().lower())
                        manual_results_map[key] = cell

        # Map telemetry cells by: (stage_choice, phase_name.lower(), param_name.lower()) -> SensorCell
        telemetry_cells_map: Dict[Tuple[str, str, str], Any] = {}
        for stage_group in telemetry_matrix:
            stage_key = stage_group.stage_key
            for row in stage_group.rows:
                phase_name = row.phase_name.strip().lower()
                for param_uuid, cell in row.cells.items():
                    param_name = cell.parameter_name.strip().lower()
                    key = (stage_key, phase_name, param_name)
                    telemetry_cells_map[key] = cell

        stage_summaries: List[SCADAStageSummary] = []
        alarms: List[SCADAAlarmItem] = []
        alarm_counter = 1

        stage_definitions = [
            (TreatmentStage.RAW_WATER, "RAW WATER", "raw", 1),
            (TreatmentStage.COAGULATED, "COAGULATED WATER", "coagulated", 2),
            (TreatmentStage.CLARIFIED, "CLARIFIED WATER", "clarified", 3),
            (TreatmentStage.FILTERED, "FILTERED WATER", "filtered", 4),
            (TreatmentStage.TREATED, "TREATED WATER", "treated", 5),
        ]

        phases = list(TreatmentPlantPhase.objects.filter(treatment_plant=self.plant).order_by("name")) if self.plant else []
        parameters = list(PhysicochemicalParameter.objects.filter(is_active=True).order_by("name"))

        for stage_choice, stage_title, manual_slug, order in stage_definitions:
            summary = SCADAStageSummary(
                stage_key=stage_choice,
                stage_title=stage_title,
                order=order,
            )

            # Collect all active comparisons for this stage
            stage_cells: List[ParameterComparisonCell] = []

            # Combine phases and parameters present in this stage
            for phase in (phases or [None]):
                phase_id = phase.pk if phase else None
                phase_name = phase.name if phase else "General / Plant"
                p_norm = phase_name.strip().lower()

                for param in parameters:
                    param_norm = param.name.strip().lower()

                    t_cell = telemetry_cells_map.get((stage_choice, p_norm, param_norm))
                    m_cell = manual_results_map.get((manual_slug, p_norm, param_norm))

                    # If neither sensor nor manual entry exists for this combination, skip to avoid huge sparse grid
                    if not t_cell and not m_cell:
                        continue
                    if (t_cell and t_cell.status == "no_sensor") and (not m_cell or m_cell.value is None):
                        continue

                    comp = ParameterComparisonCell(
                        parameter_name=param.name,
                        stage_key=stage_choice,
                        phase_id=phase_id,
                        phase_name=phase_name,
                        unit=param.unit or (t_cell.unit if t_cell else ""),
                    )

                    # Populate Sensor Data
                    if t_cell and t_cell.status != "no_sensor":
                        comp.sensor_id = t_cell.sensor_id
                        comp.sensor_name = t_cell.sensor_name
                        comp.instrument_name = t_cell.instrument_name
                        comp.instrument_status = t_cell.instrument_status
                        comp.sensor_value = t_cell.value
                        comp.sensor_display = t_cell.formatted_value
                        comp.sensor_measured_at = t_cell.measured_at
                        comp.sensor_time_ago = t_cell.time_ago
                        comp.sensor_quality = t_cell.quality_flag
                        comp.sensor_stale = (t_cell.status in ["stale", "offline"])

                    # Populate Manual Lab Data
                    if m_cell and m_cell.value is not None:
                        comp.manual_value = m_cell.value
                        comp.manual_display = str(m_cell.value)
                        comp.manual_entered = True
                        comp.manual_abnormal = getattr(m_cell, "abnormal", False)

                    # Compute Divergence / Reconciliation
                    if comp.sensor_value is not None and comp.manual_value is not None:
                        s_val = comp.sensor_value
                        m_val = comp.manual_value
                        delta = abs(s_val - m_val)
                        comp.delta_abs = delta
                        comp.delta_display = f"{float(delta):.3f}"

                        # Percentage Variance
                        avg_val = (s_val + m_val) / Decimal("2.0")
                        if avg_val > Decimal("0.0001"):
                            pct = float((delta / avg_val) * Decimal("100.0"))
                            comp.pct_variance = round(pct, 1)
                            comp.variance_display = f"{pct:.1f}%"
                        else:
                            comp.pct_variance = 0.0
                            comp.variance_display = "0.0%"

                        # Evaluate Tolerance Standards
                        tol_info = self._get_tolerance_for_param(param.name)
                        max_abs = tol_info.get("abs_diff")
                        max_pct = tol_info.get("pct_diff", 15.0)

                        is_critical = (comp.pct_variance > 25.0) or (max_abs is not None and delta > max_abs * Decimal("1.8"))
                        is_warning = (comp.pct_variance > max_pct) or (max_abs is not None and delta > max_abs)

                        if is_critical:
                            comp.reconciliation_status = "critical_mismatch"
                            comp.status_label = "CRITICAL MISMATCH"
                            comp.severity_level = "danger"
                            comp.is_anomaly = True
                            comp.advisory = f"Sensor ({comp.sensor_display}) and Lab ({comp.manual_display}) differ by {comp.variance_display}. Immediate probe inspection or re-titration required."
                            summary.critical_count += 1

                            alarms.append(SCADAAlarmItem(
                                alarm_id=f"ALM-{alarm_counter:04d}",
                                stage_title=stage_title,
                                phase_name=phase_name,
                                parameter_name=param.name,
                                severity="CRITICAL",
                                badge_class="badge-danger",
                                title=f"Sensor/Lab Divergence: {param.name}",
                                description=f"Significant divergence detected at {phase_name}. Sensor reads {comp.sensor_display} {comp.unit} vs Lab {comp.manual_display} {comp.unit} (Δ = {comp.delta_display}).",
                                sensor_value_display=comp.sensor_display,
                                manual_value_display=comp.manual_display,
                                delta_display=comp.delta_display,
                                advisory=comp.advisory,
                            ))
                            alarm_counter += 1

                        elif is_warning:
                            comp.reconciliation_status = "minor_drift"
                            comp.status_label = "DRIFT DETECTED"
                            comp.severity_level = "warning"
                            comp.is_anomaly = True
                            comp.advisory = f"Moderate variance of {comp.variance_display} (Δ={comp.delta_display}). Check calibration offset and reagent batch."
                            summary.drift_count += 1

                            alarms.append(SCADAAlarmItem(
                                alarm_id=f"ALM-{alarm_counter:04d}",
                                stage_title=stage_title,
                                phase_name=phase_name,
                                parameter_name=param.name,
                                severity="WARNING",
                                badge_class="badge-warning",
                                title=f"Telemetry Drift: {param.name}",
                                description=f"Sensor and lab variance exceeds nominal band at {phase_name}. Δ = {comp.delta_display} {comp.unit} ({comp.variance_display}).",
                                sensor_value_display=comp.sensor_display,
                                manual_value_display=comp.manual_display,
                                delta_display=comp.delta_display,
                                advisory=comp.advisory,
                            ))
                            alarm_counter += 1

                        else:
                            comp.reconciliation_status = "in_synch"
                            comp.status_label = "IN SYNCH"
                            comp.severity_level = "success"
                            comp.advisory = f"Sensor and laboratory results agree within nominal variance (Δ = {comp.delta_display})."
                            summary.in_synch_count += 1

                    elif comp.sensor_value is not None and not comp.manual_entered:
                        comp.reconciliation_status = "lab_pending"
                        comp.status_label = "LAB PENDING"
                        comp.severity_level = "info"
                        comp.advisory = f"Active sensor telemetry streaming ({comp.sensor_display} {comp.unit}); manual QC cycle entry awaiting analyst input."

                    elif comp.manual_entered and comp.sensor_value is None:
                        comp.reconciliation_status = "sensor_offline"
                        comp.status_label = "SENSOR OFFLINE"
                        comp.severity_level = "muted"
                        comp.advisory = f"Laboratory recorded {comp.manual_display} {comp.unit}, but online telemetry is unavailable or offline."

                    stage_cells.append(comp)

            summary.comparisons = sorted(stage_cells, key=lambda c: (c.phase_name, c.parameter_name))
            summary.total_parameters = len(stage_cells)

            # Calculate Stage Health
            if summary.total_parameters > 0:
                bad_points = (summary.critical_count * 2) + summary.drift_count
                summary.health_score = max(10, 100 - (bad_points * 20))
                if summary.critical_count > 0:
                    summary.status_badge = "CRITICAL VARIANCE"
                    summary.status_color = "#ef4444"
                elif summary.drift_count > 0:
                    summary.status_badge = "DRIFT DETECTED"
                    summary.status_color = "#f59e0b"
                else:
                    summary.status_badge = "NOMINAL SYNCH"
                    summary.status_color = "#10b981"
            else:
                summary.health_score = 100
                summary.status_badge = "STANDBY"
                summary.status_color = "#64748b"

            stage_summaries.append(summary)

        return sorted(stage_summaries, key=lambda s: s.order), alarms

    @staticmethod
    def _get_tolerance_for_param(param_name: str) -> dict:
        norm = param_name.strip().lower()
        for key, tol in ANOMALY_TOLERANCES.items():
            if key in norm:
                return tol
        return {"abs_diff": None, "pct_diff": 15.0, "unit": ""}
