"""
Shift Control (SC) Operational AI Intelligence & Anomaly Detection Utilities.
Provides automated operational shift briefings, apparatus condition risk scoring,
and coverage anomaly detection.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def generate_shift_intelligence_summary(
    total_assignments: int,
    waiting_handovers: int,
    total_handovers: int,
    apparatus_attention: int,
    total_apparatus: int,
    attendance_stats: Dict[str, int],
    leave_count: int,
    plant_name: str = "Water Treatment Operations",
) -> Dict[str, Any]:
    """
    Computes a futuristic operational briefing and health index for shift control.
    """
    # 1. Calculate operational readiness index (0 - 100)
    readiness_score = 100
    deductions = []

    if apparatus_attention > 0 and total_apparatus > 0:
        apparatus_penalty = min(30, int((apparatus_attention / total_apparatus) * 50))
        readiness_score -= apparatus_penalty
        deductions.append(f"{apparatus_attention} apparatus require urgent maintenance/inspection (-{apparatus_penalty}%)")

    if waiting_handovers > 0:
        handover_penalty = min(25, waiting_handovers * 10)
        readiness_score -= handover_penalty
        deductions.append(f"{waiting_handovers} shift handover(s) pending takeover confirmation (-{handover_penalty}%)")

    absent_count = attendance_stats.get("absent", 0)
    if absent_count > 0:
        absent_penalty = min(20, absent_count * 5)
        readiness_score -= absent_penalty
        deductions.append(f"{absent_count} unscheduled absence(s) detected (-{absent_penalty}%)")

    readiness_score = max(10, min(100, readiness_score))

    # Health status tone
    if readiness_score >= 88:
        status_label = "Optimal / Nominal"
        status_tone = "green"
        indicator_class = "hud-status-nominal"
    elif readiness_score >= 70:
        status_label = "Guarded / Attention Needed"
        status_tone = "amber"
        indicator_class = "hud-status-guarded"
    else:
        status_label = "Critical / Action Required"
        status_tone = "rose"
        indicator_class = "hud-status-critical"

    # Executive neural narrative
    observations = []
    if waiting_handovers == 0:
        observations.append("Shift transitions are fully synchronized with no pending supervisory takeovers.")
    else:
        observations.append(f"{waiting_handovers} transition(s) require immediate inbound supervisor acknowledgment.")

    if apparatus_attention == 0:
        observations.append("All inspected equipment & apparatus operating within nominal tolerances.")
    else:
        observations.append(f"Attention flagged on {apparatus_attention} apparatus units across active operational sections.")

    if leave_count > 0:
        observations.append(f"{leave_count} staff personnel on scheduled off-duty or annual leave.")

    executive_brief = (
        f"Operational status for {plant_name} is evaluated at {readiness_score}% readiness ({status_label}). "
        + " ".join(observations)
    )

    action_items = []
    if waiting_handovers > 0:
        action_items.append("Complete pending shift handovers in the Transitions tab.")
    if apparatus_attention > 0:
        action_items.append("Dispatch maintenance or log updated conditions for flagged apparatus.")
    if absent_count > 0:
        action_items.append("Review unmanned slots and trigger standby staff re-allocation.")
    if not action_items:
        action_items.append("Maintain continuous process monitoring and scheduled log intervals.")

    return {
        "readiness_score": readiness_score,
        "status_label": status_label,
        "status_tone": status_tone,
        "indicator_class": indicator_class,
        "executive_brief": executive_brief,
        "deductions": deductions,
        "action_items": action_items,
        "total_audited_slots": total_assignments,
        "apparatus_health_ratio": f"{total_apparatus - apparatus_attention}/{total_apparatus}" if total_apparatus else "N/A",
    }


def detect_apparatus_anomalies(apparatus_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Scans apparatus rows for condition anomalies and maintenance flags.
    """
    anomalies = []
    for item in apparatus_rows:
        status = str(item.get("status", "")).lower()
        name = item.get("name", "Unknown Apparatus")
        section = item.get("section", "—")
        remarks = item.get("remarks", "")
        
        is_anomaly = False
        severity = "info"
        reason = ""

        if status in ("out_of_service", "broken", "critical", "damaged"):
            is_anomaly = True
            severity = "critical"
            reason = f"Apparatus condition is marked '{status.replace('_', ' ').title()}'. Requires urgent work order."
        elif status in ("repair", "needs_repair", "maintenance_required"):
            is_anomaly = True
            severity = "warning"
            reason = "Scheduled or preventative maintenance required before operational degradation."
        elif status in ("unreported", "unknown"):
            is_anomaly = True
            severity = "advisory"
            reason = "No recent condition inspection logged. Verification recommended."
        
        if is_anomaly:
            anomalies.append({
                "name": name,
                "section": section,
                "status": status,
                "severity": severity,
                "reason": reason,
                "remarks": remarks,
            })
    return anomalies
