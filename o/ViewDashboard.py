"""Plant-scoped operations cockpit for the Chemical Operations app."""
from collections import defaultdict
from datetime import datetime, time, timedelta
from math import sqrt

from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection
from django.db.models import Avg, Count, F, Q, Sum, Window
from django.db.models.functions import RowNumber, TruncDate
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views import View

from alerts.models import AlertState
from core.engine.super_user import SuperUserEngine
from core.mixins import WanjalaMixin
from chlorinedemand.models import ChlorineDemandTest
from instruments.models import (
    InstrumentCalibration,
    OnlineMeasurement,
    QualityFlag,
    Sensor,
    SensorHealthSnapshot,
    TreatmentStage,
)
from jartest.models import JarTest
from PHadjustment.models import PHAdjustmentTest
from qc.models import (
    ClarifiedWaterResult,
    CoagulatedWaterResult,
    RawWaterResult,
    SamplingSchedule,
    TreatedWaterResult,
    WaterAnalysis,
)
from t.models import (
    Clarifier,
    DisludgeValve,
    FilterType,
    Status,
    TreatmentPlant,
    TreatmentPlantPhase,
    WaterFilter,
)

from .models import (
    ChemicalCategory,
    ChemicalPump,
    ChemicalStock,
    ChemicalTank,
    ChemicalUsageRecord,
    CoagulantUsageRecord,
    DisinfectantUsageRecord,
    FlocculantUsageRecord,
    MonitoringTime,
    PHAdjusterUsageRecord,
    PumpStatus,
    Reservoir,
    ReservoirLevel,
)


DOSING_MODELS = (
    ("Coagulant", CoagulantUsageRecord, "blue"),
    ("pH adjuster", PHAdjusterUsageRecord, "violet"),
    ("Disinfectant", DisinfectantUsageRecord, "green"),
    ("Flocculant", FlocculantUsageRecord, "amber"),
)
ALLOWED_PERIODS = (7, 14, 30, 90)
ALLOWED_DASHBOARD_TABS = ("overview", "process", "dosing", "assets", "storage", "reference", "reports")


def _number(value, places=1):
    try:
        return round(float(value or 0), places)
    except (TypeError, ValueError):
        return 0


def _trend_projection(values, horizons=(1, 3), window=14):
    points = [(index, float(value)) for index, value in enumerate(values) if value is not None]
    points = points[-window:]
    if len(points) < 5:
        return None

    count = len(points)
    x_mean = sum(point[0] for point in points) / count
    y_mean = sum(point[1] for point in points) / count
    x_sum_squares = sum((point[0] - x_mean) ** 2 for point in points)
    if not x_sum_squares:
        return None

    slope = sum((x - x_mean) * (y - y_mean) for x, y in points) / x_sum_squares
    intercept = y_mean - slope * x_mean
    residual_sum_squares = sum((y - (intercept + slope * x)) ** 2 for x, y in points)
    residual_error = sqrt(residual_sum_squares / max(count - 2, 1))
    latest_index, latest_value = points[-1]
    projections = {}
    for horizon in horizons:
        x_value = latest_index + horizon
        value = intercept + slope * x_value
        interval = 1.96 * residual_error * sqrt(1 + (1 / count) + ((x_value - x_mean) ** 2 / x_sum_squares))
        projections[horizon] = {
            "value": round(value, 3),
            "low": round(value - interval, 3),
            "high": round(value + interval, 3),
        }

    return {
        "observations": count,
        "latest_index": latest_index,
        "latest_value": round(latest_value, 3),
        "daily_slope": round(slope, 4),
        "direction": "Rising" if slope > 0 else "Falling" if slope < 0 else "Stable",
        "projections": projections,
    }


def _latest_per_group(queryset, group_field, order_fields):
    if connection.vendor == "postgresql":
        return queryset.order_by(group_field, *[f"-{field}" for field in order_fields]).distinct(group_field)
    return queryset.annotate(
        group_rank=Window(
            expression=RowNumber(),
            partition_by=[F(group_field)],
            order_by=[F(field).desc() for field in order_fields],
        ),
    ).filter(group_rank=1)


class DashboardView(WanjalaMixin, View):
    """Advanced chemical operations dashboard, backed by O domain records."""

    template_name = "o/dashboard.html"

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        context = self.get_context(request)
        if "application/json" in request.headers.get("Accept", "") or request.GET.get("format") == "json":
            return JsonResponse(context["dashboard_json_payload"], encoder=DjangoJSONEncoder)
        return render(request, self.template_name, context)

    def get_context(self, request):
        is_superuser = SuperUserEngine.is_superuser(request.user)
        plant_options = TreatmentPlant.objects.order_by("name") if is_superuser else TreatmentPlant.objects.none()
        plant = None
        if is_superuser:
            requested_plant = request.GET.get("plant", "")
            if requested_plant.isdigit():
                plant = plant_options.filter(pk=int(requested_plant)).first()
        else:
            plant = getattr(request.user, "assigned_treatment_plant", None)

        # Non-superusers without an assigned plant see an empty, safe dashboard.
        can_view_unscoped = is_superuser
        has_plant_scope = plant is not None or can_view_unscoped
        today = timezone.localdate()
        try:
            requested_days = int(request.GET.get("days", 30))
        except (TypeError, ValueError):
            requested_days = 30
        period_days = requested_days if requested_days in ALLOWED_PERIODS else 30
        requested_tab = request.GET.get("tab", "overview")
        active_tab = requested_tab if requested_tab in ALLOWED_DASHBOARD_TABS else "overview"
        start_date = today - timedelta(days=period_days - 1)

        def scope(queryset, field="treatment_plant"):
            if plant:
                return queryset.filter(**{field: plant})
            return queryset if can_view_unscoped else queryset.none()

        def phase_scope(queryset):
            if plant:
                return queryset.filter(phase__treatment_plant=plant)
            return queryset if can_view_unscoped else queryset.none()

        # QC owns water quality readings at each treatment stage. Inflow is
        # the raw-water feed already recorded on O dosing entries; do not
        # present it as a separate treatment stage.
        analysis_scope = scope(WaterAnalysis.objects.filter(analysis_date__range=(start_date, today)))
        qc_sample_counts = {
            row["analysis_time__schedule__water_type"]: row["total"]
            for row in analysis_scope.values("analysis_time__schedule__water_type").annotate(total=Count("pk"))
        }
        quality_stage_definitions = (
            (SamplingSchedule.RAW, "Raw water / inflow", RawWaterResult, "#57e0ed"),
            (SamplingSchedule.COAGULATED, "Coagulated", CoagulatedWaterResult, "#ffc363"),
            (SamplingSchedule.CLARIFIED, "Clarified", ClarifiedWaterResult, "#b4a0ff"),
            (SamplingSchedule.TREATED, "Treated", TreatedWaterResult, "#4ee1ae"),
        )
        quality_stats = {}
        quality_daily_values = defaultdict(list)
        quality_parameters = {}
        quality_limit_ranges = {}
        quality_stage_measurements = {
            key: {"count": 0, "compared": 0, "within": 0, "latest": None}
            for key, _label, _model, _color in quality_stage_definitions
        }
        quality_plant_required = bool(is_superuser and plant is None)
        for stage_key, _stage_label, result_model, _color in quality_stage_definitions:
            results = scope(
                result_model.objects.filter(analysis__analysis_date__range=(start_date, today)),
                field="analysis__treatment_plant",
            ).select_related(
                "analysis", "analysis__analysis_time", "phase", "parameter", "parameter__parameter"
            ).order_by("analysis__analysis_date", "analysis__analysis_time__time", "pk")
            if quality_plant_required:
                quality_stage_measurements[stage_key]["count"] = results.filter(value__isnull=False).count()
                results = results.none()
            for result in results.iterator(chunk_size=2000):
                if result.value is None:
                    continue
                parameter = result.parameter.parameter
                parameter_id = parameter.pk
                value = float(result.value)
                limit_min = result.parameter.min_value
                limit_max = result.parameter.max_value
                has_limit = limit_min is not None or limit_max is not None
                within = (limit_min is None or value >= float(limit_min)) and (limit_max is None or value <= float(limit_max))
                stage_parameter_key = (stage_key, parameter_id)
                stats = quality_stats.setdefault(stage_parameter_key, {
                    "sum": 0.0, "count": 0, "minimum": None, "maximum": None,
                    "within": 0, "compared": 0, "latest": None,
                })
                stats["sum"] += value
                stats["count"] += 1
                stats["minimum"] = value if stats["minimum"] is None else min(stats["minimum"], value)
                stats["maximum"] = value if stats["maximum"] is None else max(stats["maximum"], value)
                if has_limit:
                    stats["compared"] += 1
                    stats["within"] += int(within)
                quality_stage_measurements[stage_key]["count"] += 1
                if has_limit:
                    quality_stage_measurements[stage_key]["compared"] += 1
                    quality_stage_measurements[stage_key]["within"] += int(within)
                quality_parameters[parameter_id] = {"name": parameter.name, "unit": parameter.unit}
                sample_date = result.analysis.analysis_date
                sample_time = result.analysis.analysis_time.time
                phase_name = result.phase.name if result.phase_id else "—"
                stamp_key = (sample_date, sample_time, result.pk)
                latest = stats["latest"]
                if latest is None or stamp_key > latest["stamp"]:
                    stats["latest"] = {
                        "stamp": stamp_key,
                        "value": value,
                        "date": sample_date,
                        "time": sample_time,
                        "reference": result.analysis.reference_id,
                        "phase": phase_name,
                        "limit_min": _number(limit_min, 3) if limit_min is not None else None,
                        "limit_max": _number(limit_max, 3) if limit_max is not None else None,
                        "within": within if has_limit else None,
                    }
                stage_latest = quality_stage_measurements[stage_key]["latest"]
                if stage_latest is None or stamp_key > stage_latest["stamp"]:
                    quality_stage_measurements[stage_key]["latest"] = {
                        "stamp": stamp_key,
                        "date": sample_date,
                        "time": sample_time,
                        "reference": result.analysis.reference_id,
                    }
                quality_daily_values[(stage_key, parameter_id, sample_date)].append(value)

        if plant:
            for stage_key, _stage_label, result_model, _color in quality_stage_definitions:
                limit_model = result_model._meta.get_field("parameter").remote_field.model
                for limit in limit_model.objects.filter(treatment_plant=plant).select_related("parameter"):
                    parameter_id = limit.parameter_id
                    quality_parameters.setdefault(parameter_id, {
                        "name": limit.parameter.name,
                        "unit": limit.parameter.unit,
                    })
                    quality_limit_ranges[(stage_key, parameter_id)] = {
                        "minimum": _number(limit.min_value, 3) if limit.min_value is not None else None,
                        "maximum": _number(limit.max_value, 3) if limit.max_value is not None else None,
                    }

        current_qc_by_stage_parameter = {}
        current_qc_stage_latest = {key: None for key, _label, _model, _color in quality_stage_definitions}
        if plant:
            for stage_key, _stage_label, result_model, _color in quality_stage_definitions:
                current_results = scope(
                    result_model.objects.filter(value__isnull=False),
                    field="analysis__treatment_plant",
                )
                current_results = _latest_per_group(
                    current_results,
                    "parameter_id",
                    ("analysis__analysis_date", "analysis__analysis_time__time", "pk"),
                ).select_related(
                    "analysis", "analysis__analysis_time", "parameter", "parameter__parameter", "phase"
                )
                if quality_plant_required:
                    current_results = current_results.none()
                for result in current_results:
                    limit_min = result.parameter.min_value
                    limit_max = result.parameter.max_value
                    value = float(result.value)
                    has_limit = limit_min is not None or limit_max is not None
                    within = (limit_min is None or value >= float(limit_min)) and (limit_max is None or value <= float(limit_max))
                    current_row = {
                        "value": _number(value, 3),
                        "date": result.analysis.analysis_date,
                        "time": result.analysis.analysis_time.time,
                        "reference": result.analysis.reference_id,
                        "phase": result.phase.name if result.phase_id else "—",
                        "limit_min": _number(limit_min, 3) if limit_min is not None else None,
                        "limit_max": _number(limit_max, 3) if limit_max is not None else None,
                        "within": within if has_limit else None,
                    }
                    current_qc_by_stage_parameter[(stage_key, result.parameter.parameter_id)] = current_row
                    stage_latest = current_qc_stage_latest[stage_key]
                    stamp = (current_row["date"], current_row["time"], result.pk)
                    if stage_latest is None or stamp > stage_latest["stamp"]:
                        current_qc_stage_latest[stage_key] = {"stamp": stamp, **current_row}

        instrument_stage_map = {
            TreatmentStage.RAW_WATER: SamplingSchedule.RAW,
            TreatmentStage.COAGULATED: SamplingSchedule.COAGULATED,
            TreatmentStage.CLARIFIED: SamplingSchedule.CLARIFIED,
            TreatmentStage.TREATED: SamplingSchedule.TREATED,
        }
        instrument_sensors = []
        if plant:
            instrument_sensors = list(
                Sensor.objects.filter(
                    treatment_plant=plant,
                    instrument__treatment_plant=plant,
                    instrument__is_active=True,
                    is_active=True,
                    stage__in=instrument_stage_map,
                ).select_related("parameter", "instrument", "instrument__phase")
                .order_by("stage", "parameter__name", "-is_primary", "name")
            )
        sensor_ids = [sensor.pk for sensor in instrument_sensors]
        instrument_ids = {sensor.instrument_id for sensor in instrument_sensors}
        sensor_stage_pairs = {
            (instrument_stage_map[sensor.stage], sensor.parameter_id)
            for sensor in instrument_sensors
        }
        sensor_counts_by_pair = defaultdict(int)
        for sensor in instrument_sensors:
            sensor_counts_by_pair[(instrument_stage_map[sensor.stage], sensor.parameter_id)] += 1
            quality_parameters.setdefault(sensor.parameter_id, {
                "name": sensor.parameter.name,
                "unit": sensor.unit,
            })

        latest_measurements = {}
        if sensor_ids:
            ranked_measurements = _latest_per_group(
                OnlineMeasurement.objects.filter(sensor_id__in=sensor_ids),
                "sensor_id",
                ("measured_at", "pk"),
            ).select_related("sensor", "sensor__instrument", "sensor__parameter")
            latest_measurements = {
                item.sensor_id: item for item in ranked_measurements
            }

        latest_health = {}
        if sensor_ids:
            ranked_health = _latest_per_group(
                SensorHealthSnapshot.objects.filter(sensor_id__in=sensor_ids),
                "sensor_id",
                ("assessed_at", "pk"),
            )
            latest_health = {item.sensor_id: item for item in ranked_health}

        latest_calibrations = {}
        if instrument_ids:
            ranked_calibrations = _latest_per_group(
                InstrumentCalibration.objects.filter(instrument_id__in=instrument_ids),
                "instrument_id",
                ("performed_at", "pk"),
            )
            latest_calibrations = {item.instrument_id: item for item in ranked_calibrations}

        current_time = timezone.now()
        current_date = timezone.localdate()
        online_by_stage_parameter = defaultdict(list)
        online_stage_summaries = {
            key: {"sensors": 0, "fresh": 0, "issues": 0, "latest": None}
            for key, _label, _model, _color in quality_stage_definitions
        }
        online_sensor_rows = []
        for sensor in instrument_sensors:
            stage_key = instrument_stage_map[sensor.stage]
            stage_summary = online_stage_summaries[stage_key]
            stage_summary["sensors"] += 1
            measurement = latest_measurements.get(sensor.pk)
            calibration = latest_calibrations.get(sensor.instrument_id)
            health = latest_health.get(sensor.pk)
            if measurement:
                age_seconds = max(0, (current_time - measurement.measured_at).total_seconds())
                freshness_limit = max(sensor.instrument.expected_heartbeat_sec * 3, 180)
                is_fresh = age_seconds <= freshness_limit
                flag = measurement.quality_flag
                health_percent = None
                if health:
                    health_value = float(health.health_score)
                    health_percent = health_value * 100 if health_value <= 1 else health_value
                    health_percent = max(0.0, min(100.0, health_percent))
                calibration_expired = bool(calibration and calibration.valid_until < current_date)
                limit_range = quality_limit_ranges.get((stage_key, sensor.parameter_id), {})
                limit_min = limit_range.get("minimum")
                limit_max = limit_range.get("maximum")
                has_treatment_limit = limit_min is not None or limit_max is not None
                reading_value = float(measurement.value)
                within_treatment_limits = (
                    (limit_min is None or reading_value >= limit_min)
                    and (limit_max is None or reading_value <= limit_max)
                ) if has_treatment_limit and is_fresh and flag not in (QualityFlag.INVALID, QualityFlag.STALE) else None
                if not is_fresh:
                    state_key, state_label = "unknown", "Stale"
                elif calibration_expired or flag == QualityFlag.CALIBRATION:
                    state_key, state_label = "maintenance", "Calibration due"
                elif calibration is None:
                    state_key, state_label = "maintenance", "Calibration untracked"
                elif flag == QualityFlag.INVALID:
                    state_key, state_label = "faulty", "Invalid"
                elif flag == QualityFlag.SUSPECT:
                    state_key, state_label = "maintenance", "Suspect"
                elif health_percent is not None and health_percent < 70:
                    state_key, state_label = "maintenance", "Low sensor health"
                elif sensor.instrument.status != "online":
                    state_key, state_label = "unknown", sensor.instrument.get_status_display()
                else:
                    state_key, state_label = "working", "Current"
                if within_treatment_limits is False:
                    state_key, state_label = "faulty", "Outside plant limit"
                if is_fresh:
                    stage_summary["fresh"] += 1
                if state_key != "working":
                    stage_summary["issues"] += 1
                if stage_summary["latest"] is None or measurement.measured_at > stage_summary["latest"]:
                    stage_summary["latest"] = measurement.measured_at
                pair = (stage_key, sensor.parameter_id)
                online_by_stage_parameter[pair].append({
                    "value": _number(measurement.value, 3),
                    "limit_min": limit_min,
                    "limit_max": limit_max,
                    "within_treatment_limits": within_treatment_limits,
                    "measured_at": measurement.measured_at,
                    "received_at": measurement.received_at,
                    "quality_flag": flag,
                    "quality_label": measurement.get_quality_flag_display(),
                    "state_key": state_key,
                    "state_label": state_label,
                    "is_fresh": is_fresh,
                    "is_primary": sensor.is_primary,
                    "sensor": sensor.name,
                    "instrument": sensor.instrument.name,
                    "instrument_status": sensor.instrument.get_status_display(),
                    "source": measurement.source,
                    "signal_strength": measurement.signal_strength,
                    "latency_ms": measurement.latency_ms,
                    "anomaly_score": _number(measurement.anomaly_score, 3),
                    "drift_score": _number(measurement.drift_score, 3),
                    "health_score": _number(health_percent, 1) if health_percent is not None else None,
                    "health_assessed_at": health.assessed_at if health else None,
                    "calibration_valid_until": calibration.valid_until if calibration else None,
                    "calibration_status": "Expired" if calibration_expired else "Valid" if calibration else "No calibration record",
                    "stage_key": stage_key,
                    "parameter_id": sensor.parameter_id,
                })
            else:
                stage_summary["issues"] += 1
                online_sensor_rows.append({
                    "stage_key": stage_key,
                    "parameter_id": sensor.parameter_id,
                    "parameter": sensor.parameter.name,
                    "sensor": sensor.name,
                    "instrument": sensor.instrument.name,
                    "status": "No measurement",
                })

        selected_online_by_stage_parameter = {}
        for pair, candidates in online_by_stage_parameter.items():
            candidates.sort(key=lambda item: (
                not item["is_fresh"],
                item["quality_flag"] != QualityFlag.OK,
                not item["is_primary"],
                -item["measured_at"].timestamp(),
            ))
            selected_online_by_stage_parameter[pair] = candidates[0]

        online_daily_values = defaultdict(list)
        if sensor_ids:
            window_start = timezone.make_aware(datetime.combine(start_date, time.min), timezone.get_current_timezone())
            window_end = timezone.make_aware(datetime.combine(today + timedelta(days=1), time.min), timezone.get_current_timezone())
            online_daily_rows = OnlineMeasurement.objects.filter(
                sensor_id__in=sensor_ids,
                quality_flag=QualityFlag.OK,
                measured_at__gte=window_start,
                measured_at__lt=window_end,
            ).annotate(day=TruncDate("measured_at", tzinfo=timezone.get_current_timezone())).values(
                "sensor__stage", "sensor__parameter_id", "day",
            ).annotate(mean=Avg("value"), readings=Count("pk")).order_by("day")
            for row in online_daily_rows.iterator(chunk_size=1000):
                stage_key = instrument_stage_map.get(row["sensor__stage"])
                if stage_key:
                    online_daily_values[(stage_key, row["sensor__parameter_id"], row["day"])].append(float(row["mean"]))

        quality_parameter_options = sorted(
            ({"id": parameter_id, **parameter} for parameter_id, parameter in quality_parameters.items()),
            key=lambda item: (item["name"].casefold(), item["id"]),
        )
        selected_quality_parameter = next(
            (item for item in quality_parameter_options if item["name"].casefold() == "turbidity"),
            quality_parameter_options[0] if quality_parameter_options else None,
        )
        quality_stage_rows = []
        quality_rows = []
        stage_order = {key: index for index, (key, _label, _model, _color) in enumerate(quality_stage_definitions)}
        for stage_key, stage_label, _result_model, stage_color in quality_stage_definitions:
            stage_measurement = quality_stage_measurements[stage_key]
            compared = stage_measurement["compared"]
            latest = current_qc_stage_latest[stage_key] or stage_measurement["latest"]
            stage_parameter_ids = sorted(
                {parameter_id for (candidate_stage, parameter_id) in quality_stats if candidate_stage == stage_key}
                | {parameter_id for candidate_stage, parameter_id in current_qc_by_stage_parameter if candidate_stage == stage_key}
                | {parameter_id for candidate_stage, parameter_id in quality_limit_ranges if candidate_stage == stage_key}
                | {parameter_id for candidate_stage, parameter_id in sensor_stage_pairs if candidate_stage == stage_key}
            )
            online_summary = online_stage_summaries[stage_key]
            quality_stage_rows.append({
                "key": stage_key,
                "label": stage_label,
                "color": stage_color,
                "samples": qc_sample_counts.get(stage_key, 0),
                "measurements": stage_measurement["count"],
                "parameter_count": len(stage_parameter_ids),
                "compared": compared,
                "within": stage_measurement["within"],
                "compliance": round(stage_measurement["within"] * 100 / compared, 1) if compared else None,
                "latest_date": latest["date"] if latest else None,
                "latest_time": latest["time"].strftime("%H:%M") if latest else None,
                "latest_reference": latest["reference"] if latest else None,
                "online_sensors": online_summary["sensors"],
                "online_fresh": online_summary["fresh"],
                "online_issues": online_summary["issues"],
                "latest_online": online_summary["latest"],
            })
            for parameter_id in stage_parameter_ids:
                stats = quality_stats.get((stage_key, parameter_id), {
                    "sum": 0.0, "count": 0, "minimum": None, "maximum": None,
                    "within": 0, "compared": 0, "latest": None,
                })
                parameter = quality_parameters[parameter_id]
                latest_value = stats["latest"]
                current_qc = current_qc_by_stage_parameter.get((stage_key, parameter_id)) or latest_value
                online_value = selected_online_by_stage_parameter.get((stage_key, parameter_id))
                online_sensor_count = sensor_counts_by_pair[(stage_key, parameter_id)]
                quality_rows.append({
                    "stage": stage_label,
                    "stage_key": stage_key,
                    "parameter_id": parameter_id,
                    "parameter": parameter["name"],
                    "unit": parameter["unit"],
                    "phase": current_qc["phase"] if current_qc else "—",
                    "mean": round(stats["sum"] / stats["count"], 3) if stats["count"] else None,
                    "minimum": round(stats["minimum"], 3) if stats["minimum"] is not None else None,
                    "maximum": round(stats["maximum"], 3) if stats["maximum"] is not None else None,
                    "latest": current_qc["value"] if current_qc else None,
                    "latest_date": current_qc["date"] if current_qc else None,
                    "latest_time": current_qc["time"].strftime("%H:%M") if current_qc else None,
                    "latest_reference": current_qc["reference"] if current_qc else None,
                    "latest_age_days": (today - current_qc["date"]).days if current_qc else None,
                    "limit_min": current_qc["limit_min"] if current_qc else None,
                    "limit_max": current_qc["limit_max"] if current_qc else None,
                    "latest_within": current_qc["within"] if current_qc else None,
                    "samples": stats["count"],
                    "compared": stats["compared"],
                    "within": stats["within"],
                    "compliance": round(stats["within"] * 100 / stats["compared"], 1) if stats["compared"] else None,
                    "online": online_value,
                    "online_sensor_count": online_sensor_count,
                })
                current_limits = quality_limit_ranges.get((stage_key, parameter_id), {})
                quality_rows[-1]["limit_min"] = current_limits.get("minimum", quality_rows[-1]["limit_min"])
                quality_rows[-1]["limit_max"] = current_limits.get("maximum", quality_rows[-1]["limit_max"])
        quality_rows.sort(key=lambda row: (stage_order[row["stage_key"]], row["parameter"].casefold(), row["phase"].casefold()))
        forecast_labels = [(today + timedelta(days=offset)).isoformat() for offset in range(1, 4)]
        chart_date_axis = [start_date + timedelta(days=offset) for offset in range(period_days + 3)]
        quality_chart = {
            "labels": [day.isoformat() for day in chart_date_axis],
            "stages": [
                {"key": key, "label": label, "color": color}
                for key, label, _model, color in quality_stage_definitions
            ],
            "parameters": quality_parameter_options,
            "series": {},
            "online_series": {},
            "projection_series": {},
            "projections": {},
            "limits": {},
            "forecast_labels": forecast_labels,
            "plant_required": quality_plant_required,
        }
        for option in quality_parameter_options:
            parameter_id = option["id"]
            quality_chart["series"][str(parameter_id)] = {
                key: [
                    round(sum(values) / len(values), 3) if values else None
                    for values in (
                        quality_daily_values.get((key, parameter_id, start_date + timedelta(days=offset)), [])
                        for offset in range(period_days)
                    )
                ] + [None, None, None]
                for key, _label, _model, _color in quality_stage_definitions
            }
            quality_chart["online_series"][str(parameter_id)] = {
                key: [
                    round(sum(values) / len(values), 3) if values else None
                    for values in (
                        online_daily_values.get((key, parameter_id, start_date + timedelta(days=offset)), [])
                        for offset in range(period_days)
                    )
                ] + [None, None, None]
                for key, _label, _model, _color in quality_stage_definitions
            }
            quality_chart["projection_series"][str(parameter_id)] = {}
            quality_chart["projections"][str(parameter_id)] = {}
            quality_chart["limits"][str(parameter_id)] = {
                stage_key: quality_limit_ranges.get((stage_key, parameter_id), {"minimum": None, "maximum": None})
                for stage_key, _stage_label, _model, _color in quality_stage_definitions
            }
            for stage_key, _stage_label, _model, _color in quality_stage_definitions:
                daily_means = [
                    sum(values) / len(values) if values else None
                    for values in (
                        quality_daily_values.get((stage_key, parameter_id, start_date + timedelta(days=offset)), [])
                        for offset in range(period_days)
                    )
                ]
                forecast = _trend_projection(daily_means)
                populated = [(index, value) for index, value in enumerate(daily_means) if value is not None]
                latest_qc_date = start_date + timedelta(days=populated[-1][0]) if populated else None
                forecast_is_current = bool(latest_qc_date and latest_qc_date >= today - timedelta(days=2))
                projection_row = {
                    "available": bool(forecast and forecast_is_current and plant),
                    "observations": forecast["observations"] if forecast else len(populated),
                    "as_of": latest_qc_date.isoformat() if latest_qc_date else None,
                    "direction": forecast["direction"] if forecast else "Insufficient history",
                    "daily_slope": forecast["daily_slope"] if forecast else None,
                    "horizons": forecast["projections"] if forecast and forecast_is_current and plant else {},
                    "reason": "Select a treatment plant to view isolated projections." if quality_plant_required else "Latest QC observation is older than 48 hours." if forecast and not forecast_is_current else "At least five daily QC observations are required." if not forecast else "",
                }
                quality_chart["projections"][str(parameter_id)][stage_key] = projection_row
                forecast_values = [None] * len(chart_date_axis)
                if projection_row["available"] and latest_qc_date:
                    origin_index = (latest_qc_date - start_date).days
                    forecast_values[origin_index] = round(daily_means[origin_index], 3)
                    for horizon, result in forecast["projections"].items():
                        forecast_day = latest_qc_date + timedelta(days=int(horizon))
                        forecast_index = (forecast_day - start_date).days
                        if 0 <= forecast_index < len(forecast_values):
                            forecast_values[forecast_index] = result["value"]
                quality_chart["projection_series"][str(parameter_id)][stage_key] = forecast_values

        # The dashboard shows only approved, established values as the current
        # operating recommendation while retaining each app's own source record.
        approved_dosages = []
        if has_plant_scope:
            dosage_sources = (
                ("Coagulant", JarTest, "established_dosage_mg_per_l", "mg/L", "coagulant__name", "jartest:dashboard"),
                ("pH adjustment", PHAdjustmentTest, "established_dosage_ml", "mL", "source_jar__jar_test__coagulant__name", "PHadjustment:dashboard"),
                ("Chlorine", ChlorineDemandTest, "established_dosage_mg_per_l", "mg/L", None, "chlorinedemand:dashboard"),
            )
            for label, model, dosage_field, unit, name_field, source_app in dosage_sources:
                filters = {"approved_at__isnull": False, f"{dosage_field}__isnull": False, "test_date__lte": today}
                if plant:
                    filters["treatment_plant"] = plant
                history = model.objects.filter(**filters).order_by("test_date", "pk")
                latest_query = model.objects.filter(**filters).order_by("-test_date", "-pk")
                if name_field:
                    latest_query = latest_query.select_related("__".join(name_field.split("__")[:-1]))
                latest_record = latest_query.first()
                trend = defaultdict(list)
                for dosage_date, dosage in history.filter(test_date__gte=start_date, test_date__lte=today).values_list("test_date", dosage_field).iterator(chunk_size=1000):
                    if dosage is not None:
                        trend[dosage_date].append(_number(dosage, 3))
                if latest_record:
                    chemical_name = ""
                    if name_field:
                        relation = latest_record
                        for part in name_field.split("__"):
                            relation = getattr(relation, part, None)
                            if relation is None:
                                break
                        chemical_name = str(relation or "")
                    if label == "Coagulant":
                        detail = chemical_name or "Jar Test established recommendation"
                    elif label == "pH adjustment":
                        detail = f"Source coagulant · {chemical_name}" if chemical_name else "Linked Jar Test recommendation"
                    else:
                        detail = "Breakpoint chlorine recommendation"
                    approved_dosages.append({
                        "label": label,
                        "value": _number(getattr(latest_record, dosage_field), 3),
                        "unit": unit,
                        "chemical": chemical_name,
                        "detail": detail,
                        "plant": latest_record.treatment_plant.name,
                        "date": latest_record.test_date,
                        "source": source_app,
                        "trend": [
                            round(sum(values) / len(values), 3) if values else None
                            for values in (trend.get(start_date + timedelta(days=offset), []) for offset in range(period_days))
                        ],
                    })
                else:
                    approved_dosages.append({"label": label, "value": None, "unit": unit, "chemical": "", "detail": "No approved established value", "plant": plant.name if plant else "All plants", "date": None, "source": source_app, "trend": [None for _ in range(period_days)]})

        dosing = {}
        dosing_rows = []
        daily_flow = defaultdict(list)
        phase_flow_values = defaultdict(list)
        today_phase_flow = defaultdict(list)
        today_flow_values = []
        compliance_by_date = defaultdict(lambda: {"ok": 0, "total": 0})
        dosing_counts = {}
        dosing_quality = {}
        all_flow_values = []
        compliance_total = compliance_ok = 0
        latest_activity = None

        for label, model, color in DOSING_MODELS:
            queryset = scope(model.objects.filter(date__gte=start_date, date__lte=today))
            dosing[label] = queryset
            dosing_counts[label] = queryset.count()
            dosing_quality[label] = {"type": label, "records": dosing_counts[label], "compared": 0, "within": 0, "rate": None, "color": color}
            for date_value, phase_id, flow_value in queryset.values_list("date", "phase_id", "raw_water_flow_rate").iterator(chunk_size=2000):
                flow = _number(flow_value, 2)
                daily_flow[date_value].append(flow)
                all_flow_values.append(flow)
                phase_flow_values[phase_id].append(flow)
                if date_value == today:
                    today_phase_flow[phase_id].append(flow)
                    today_flow_values.append(flow)
            expected_field = "expected_reading" if model is DisinfectantUsageRecord else "expected_dosage"
            actual_field = "actual_reading" if model is DisinfectantUsageRecord else "actual_dosage"
            for record_date, actual, expected in queryset.values_list("date", actual_field, expected_field).iterator(chunk_size=2000):
                if expected is None or _number(expected, 4) <= 0 or actual is None:
                    continue
                compliance_total += 1
                dosing_quality[label]["compared"] += 1
                compliance_by_date[record_date]["total"] += 1
                if abs(float(actual) - float(expected)) / float(expected) <= 0.15:
                    compliance_ok += 1
                    dosing_quality[label]["within"] += 1
                    compliance_by_date[record_date]["ok"] += 1
            if dosing_quality[label]["compared"]:
                dosing_quality[label]["rate"] = round(
                    dosing_quality[label]["within"] * 100 / dosing_quality[label]["compared"], 1
                )
            records = queryset.select_related(
                "chemical", "phase", "phase__treatment_plant", "pump_used", "tank_used",
                "monitoring_time", "recorded_by", "treatment_plant",
            ).order_by("-date", "-created_at")[:40]
            for record in records:
                expected = getattr(record, expected_field, None)
                actual = getattr(record, actual_field, None)
                dosing_rows.append({
                    "type": label,
                    "color": color,
                    "chemical": str(record.chemical),
                    "phase": record.phase.name,
                    "plant": record.treatment_plant.name,
                    "flow": _number(record.raw_water_flow_rate, 1),
                    "actual": _number(record.actual_dosage, 2),
                    "expected": _number(record.expected_dosage, 2) if record.expected_dosage is not None else None,
                    "concentration": _number(record.concentration_ppm, 2) if record.concentration_ppm is not None else None,
                    "date": record.date,
                    "time": str(record.monitoring_time),
                    "operator": str(record.recorded_by or "—"),
                    "pump": record.pump_used.name if record.pump_used else "—",
                    "tank": record.tank_used.name if record.tank_used else "—",
                    "remarks": record.remarks or "",
                    "batch": getattr(record, "batch_number", "") or "",
                    "flocculant_type": getattr(record, "flocculant_type", "") or "",
                    "shift": str(record.shift_assignment) if record.shift_assignment_id else "—",
                    "specific_reading": _number(getattr(record, "actual_reading", None), 2) if hasattr(record, "actual_reading") else None,
                    "expected_reading": _number(getattr(record, "expected_reading", None), 2) if getattr(record, "expected_reading", None) is not None else None,
                    "quantity": _number(getattr(record, "quantity_used_kg", 0), 2) if hasattr(record, "quantity_used_kg") else None,
                })
                stamp = record.updated_at or record.created_at
                latest_activity = max(latest_activity, stamp) if latest_activity else stamp

        dosing_rows.sort(key=lambda row: (row["date"], row["time"]), reverse=True)
        dosing_rows = dosing_rows[:40]

        usage_records = scope(
            ChemicalUsageRecord.objects.filter(date_used__gte=start_date, date_used__lte=today)
        ).select_related("chemical", "treatment_plant", "recorded_by").order_by("-date_used", "-updated_at")
        usage_total = usage_records.aggregate(total=Sum("quantity_used_kg"))["total"] or 0
        usage_trend_rows = usage_records.values("date_used").annotate(total=Sum("quantity_used_kg")).order_by("date_used")
        daily_usage = {row["date_used"]: _number(row["total"], 2) for row in usage_trend_rows}
        chemical_usage_totals = {
            (row["treatment_plant_id"], row["chemical_id"]): {
                "plant": row["treatment_plant__name"], "chemical": row["chemical__name"], "quantity": _number(row["total"], 2)
            }
            for row in usage_records.values("treatment_plant_id", "treatment_plant__name", "chemical_id", "chemical__name")
            .annotate(total=Sum("quantity_used_kg")).order_by("-total")
        }
        chemical_usage_rankings = sorted(
            ({"plant": row["plant"], "chemical": row["chemical"], "quantity": row["quantity"]}
             for row in chemical_usage_totals.values()),
            key=lambda row: (-row["quantity"], row["plant"].casefold(), row["chemical"].casefold()),
        )[:15]
        chart_labels = [(start_date + timedelta(days=offset)).isoformat() for offset in range(period_days)]
        usage_trend = [daily_usage.get(start_date + timedelta(days=offset)) for offset in range(period_days)]
        compliance_trend = [
            round(values["ok"] * 100 / values["total"], 1) if values["total"] else None
            for values in (compliance_by_date.get(start_date + timedelta(days=offset), {"ok": 0, "total": 0}) for offset in range(period_days))
        ]
        flow_trend = [
            _number(sum(daily_flow.get(start_date + timedelta(days=offset), [])) / len(daily_flow[start_date + timedelta(days=offset)]), 2)
            if daily_flow.get(start_date + timedelta(days=offset)) else None
            for offset in range(period_days)
        ]
        flow_daily_min = [
            _number(min(values), 2) if (values := daily_flow.get(start_date + timedelta(days=offset))) else None
            for offset in range(period_days)
        ]
        flow_daily_max = [
            _number(max(values), 2) if (values := daily_flow.get(start_date + timedelta(days=offset))) else None
            for offset in range(period_days)
        ]
        flow_rolling_average = []
        for index in range(period_days):
            rolling_values = [value for value in flow_trend[max(0, index - 6):index + 1] if value is not None]
            flow_rolling_average.append(_number(sum(rolling_values) / len(rolling_values), 2) if rolling_values else None)

        pumps = list(phase_scope(ChemicalPump.objects.select_related("phase", "phase__treatment_plant")).order_by("phase__name", "name"))
        pump_ids = [pump.pk for pump in pumps]
        statuses = scope(PumpStatus.objects.filter(pump_id__in=pump_ids)).select_related("pump", "updated_by").order_by("-created_at")
        latest_status_by_pump = {}
        for status in statuses.iterator(chunk_size=200):
            latest_status_by_pump.setdefault(status.pump_id, status)
            stamp = status.updated_at or status.created_at
            latest_activity = max(latest_activity, stamp) if latest_activity else stamp
            if len(latest_status_by_pump) == len(pump_ids):
                break
        status_history_rows = [{
            "pump": status.pump.name,
            "plant": status.treatment_plant.name,
            "phase": status.pump.phase.name,
            "status": status.get_status_display(),
            "status_key": status.status,
            "remarks": status.remarks or "—",
            "updated_by": str(status.updated_by or "—"),
            "updated_at": status.updated_at,
        } for status in statuses.order_by("-created_at")[:50]]
        pump_rows = [{
            "name": pump.name,
            "category": pump.get_category_display(),
            "phase": pump.phase.name,
            "plant": pump.phase.treatment_plant.name,
            "status": latest_status_by_pump[pump.pk].status if pump.pk in latest_status_by_pump else "unknown",
            "status_label": latest_status_by_pump[pump.pk].get_status_display() if pump.pk in latest_status_by_pump else "No status recorded",
            "remarks": latest_status_by_pump[pump.pk].remarks or "" if pump.pk in latest_status_by_pump else "",
            "updated_by": str(latest_status_by_pump[pump.pk].updated_by or "—") if pump.pk in latest_status_by_pump else "—",
            "updated_at": latest_status_by_pump[pump.pk].updated_at if pump.pk in latest_status_by_pump else None,
        } for pump in pumps]
        pump_summary = {status: sum(1 for row in pump_rows if row["status"] == status) for status in ("working", "faulty", "maintenance", "decommissioned", "unknown")}

        tanks = list(phase_scope(ChemicalTank.objects.select_related("phase", "phase__treatment_plant")).order_by("phase__name", "name"))
        tank_rows = [{"name": tank.name, "phase": tank.phase.name, "plant": tank.phase.treatment_plant.name,
                      "category": tank.get_category_display(), "capacity": _number(tank.capacity_liters, 0)} for tank in tanks]
        tank_capacity = sum(row["capacity"] for row in tank_rows)

        stocks = list(scope(ChemicalStock.objects.select_related("chemical", "treatment_plant")).order_by("quantity_kg", "chemical__name"))
        stock_rows = [{"chemical_id": stock.chemical_id, "treatment_plant_id": stock.treatment_plant_id, "chemical": stock.chemical.name, "plant": stock.treatment_plant.name,
                       "quantity": _number(stock.quantity_kg, 2), "updated_at": stock.updated_at} for stock in stocks]
        total_stock = sum(row["quantity"] for row in stock_rows)
        low_stock_rows = [row for row in stock_rows if row["quantity"] < 50]
        stock_by_chemical = {(row["treatment_plant_id"], row["chemical_id"]): row for row in stock_rows}
        coverage_ids = set(stock_by_chemical) | set(chemical_usage_totals)
        inventory_coverage_rows = []
        for plant_chemical_key in coverage_ids:
            stock = stock_by_chemical.get(plant_chemical_key)
            consumption = chemical_usage_totals.get(plant_chemical_key, {
                "plant": stock["plant"] if stock else "—",
                "chemical": stock["chemical"] if stock else "Unknown chemical",
                "quantity": 0,
            })
            daily_average = consumption["quantity"] / period_days
            coverage_days = round(stock["quantity"] / daily_average, 1) if stock and daily_average > 0 else None
            inventory_coverage_rows.append({
                "chemical": consumption["chemical"],
                "plant": stock["plant"] if stock else consumption["plant"],
                "stock": stock["quantity"] if stock else None,
                "usage_total": consumption["quantity"],
                "daily_average": _number(daily_average, 2),
                "coverage_days": coverage_days,
                "state": "No stock record" if stock is None else "No usage in period" if daily_average <= 0 else "Under 7 days" if coverage_days < 7 else "Projected coverage",
            })
        inventory_coverage_rows.sort(key=lambda row: (row["coverage_days"] is None, row["coverage_days"] if row["coverage_days"] is not None else float("inf"), row["chemical"].casefold()))
        dosing_quality_rows = list(dosing_quality.values())

        phases = list(scope(TreatmentPlantPhase.objects.select_related("treatment_plant"), field="treatment_plant").order_by("name"))
        phase_rows = []
        for phase in phases:
            period_values = phase_flow_values.get(phase.pk, [])
            today_values = today_phase_flow.get(phase.pk, [])
            observed_values = today_values or period_values
            observed_flow = sum(observed_values) / len(observed_values) if observed_values else None
            flow_load = round(observed_flow * 100 / phase.operational_capacity, 1) if observed_flow is not None and phase.operational_capacity else None
            phase_rows.append({
                "name": phase.name,
                "plant": phase.treatment_plant.name,
                "design_capacity": _number(phase.design_capacity, 1),
                "operational_capacity": _number(phase.operational_capacity, 1),
                "recorded_flow": _number(observed_flow, 1) if observed_flow is not None else None,
                "recorded_count": len(observed_values),
                "flow_load": flow_load,
                "flow_meter": max(0, min(100, round(flow_load))) if flow_load is not None else 0,
                "flow_source": "today" if today_values else "period mean",
            })

        filter_queryset = WaterFilter.objects.select_related(
            "filter_type", "treatment_plant", "phase", "phase__treatment_plant", "filter_status"
        )
        if plant:
            filter_queryset = filter_queryset.filter(
                Q(treatment_plant_id=plant.pk)
                | Q(treatment_plant__isnull=True, phase__treatment_plant_id=plant.pk)
            )
        elif not can_view_unscoped:
            filter_queryset = filter_queryset.none()
        filters = list(filter_queryset.order_by("phase__name", "name"))
        filter_status_counts = {key: 0 for key, _label in WaterFilter.STATUS_CHOICES}
        filter_rows = []
        filter_flow_total = filter_capacity_total = 0.0
        filter_maintenance_due = 0
        for water_filter in filters:
            plant_for_filter = water_filter.treatment_plant or (water_filter.phase.treatment_plant if water_filter.phase_id else None)
            filter_status = getattr(water_filter, "filter_status", None)
            capacity = float(water_filter.capacity) if water_filter.capacity is not None else 0
            utilization = round(water_filter.flow_rate * 100 / capacity, 1) if capacity > 0 and water_filter.flow_rate is not None else None
            last_maintenance_date = filter_status.last_maintenance_date if filter_status else None
            overdue = bool(filter_status and filter_status.maintenance_due)
            filter_status_counts[water_filter.status] = filter_status_counts.get(water_filter.status, 0) + 1
            filter_flow_total += max(0, _number(water_filter.flow_rate, 2))
            filter_capacity_total += max(0, _number(capacity, 2))
            filter_maintenance_due += int(overdue)
            filter_rows.append({
                "name": water_filter.name,
                "plant": plant_for_filter.name if plant_for_filter else "Unassigned",
                "phase": water_filter.phase.name if water_filter.phase_id else "—",
                "type": water_filter.filter_type.name,
                "status": water_filter.status,
                "status_label": water_filter.get_status_display(),
                "flow_rate": _number(water_filter.flow_rate, 2),
                "capacity": _number(capacity, 2),
                "utilization": utilization,
                "location": water_filter.location,
                "installed": filter_status.installation_date if filter_status else None,
                "last_maintenance": last_maintenance_date,
                "next_maintenance": filter_status.effective_next_maintenance_date if filter_status else None,
                "maintenance_due": overdue,
                "days_since_maintenance": max(0, (today - last_maintenance_date).days) if last_maintenance_date else None,
            })
        filter_overall_utilization = round(filter_flow_total * 100 / filter_capacity_total, 1) if filter_capacity_total else None
        filter_status_rows = [{
            "key": key,
            "label": label,
            "count": filter_status_counts.get(key, 0),
        } for key, label in WaterFilter.STATUS_CHOICES]
        known_filter_statuses = dict(WaterFilter.STATUS_CHOICES)
        filter_status_rows.extend({
            "key": key,
            "label": key.replace("_", " ").title(),
            "count": count,
        } for key, count in filter_status_counts.items() if key not in known_filter_statuses)
        filter_types = list(FilterType.objects.filter(pk__in=[item.filter_type_id for item in filters]).order_by("name"))
        filter_type_rows = [{
            "name": filter_type.name,
            "description": filter_type.description or "—",
            "units": sum(1 for row in filters if row.filter_type_id == filter_type.pk),
        } for filter_type in filter_types]

        clarifiers = list(scope(
            Clarifier.objects.select_related("treatment_plant", "phase", "phase__treatment_plant"),
            field="treatment_plant",
        ).order_by("name"))
        clarifier_ids = [clarifier.pk for clarifier in clarifiers]
        valves = list(DisludgeValve.objects.filter(clarifier_id__in=clarifier_ids).select_related(
            "clarifier", "clarifier__treatment_plant", "clarifier__phase", "status"
        ).order_by("clarifier__name", "name"))
        valve_count_by_clarifier = defaultdict(int)
        valve_status_counts = defaultdict(int)
        valve_rows = []
        for valve in valves:
            valve_count_by_clarifier[valve.clarifier_id] += 1
            valve_status_counts[valve.status.name] += 1
            valve_rows.append({
                "name": valve.name,
                "clarifier": valve.clarifier.name,
                "plant": valve.clarifier.treatment_plant.name,
                "status": valve.status.name,
            })
        status_catalog = list(Status.objects.filter(pk__in=[valve.status_id for valve in valves]).order_by("name"))
        valve_status_rows = [{"name": status.name, "count": valve_status_counts.get(status.name, 0)} for status in status_catalog]
        clarifier_rows = []
        clarifier_design_total = clarifier_volume_total = 0.0
        clarifier_maintenance_due = 0
        for clarifier in clarifiers:
            overdue = (today - clarifier.last_maintenance_date).days >= 180
            clarifier_design_total += max(0, _number(clarifier.design_capacity, 2))
            clarifier_volume_total += max(0, _number(clarifier.clarifier_capacity, 2))
            clarifier_maintenance_due += int(overdue)
            clarifier_dimensions = (
                f"{_number(clarifier.depth, 1)} m deep · {_number(clarifier.diameter, 1)} m dia"
                if clarifier.diameter
                else f"{_number(clarifier.depth, 1)} m deep · {_number(clarifier.length, 1)} × {_number(clarifier.width, 1)} m"
            )
            clarifier_rows.append({
                "name": clarifier.name,
                "plant": clarifier.treatment_plant.name,
                "phase": clarifier.phase.name if clarifier.phase_id else "—",
                "design_capacity": _number(clarifier.design_capacity, 2),
                "volume_capacity": _number(clarifier.clarifier_capacity, 2),
                "surface_area": _number(clarifier.surface_area, 2),
                "surface_loading": _number(clarifier.surface_loading, 2),
                "flow_type": clarifier.get_flow_type_display(),
                "design": clarifier.get_design_display(),
                "dimensions": clarifier_dimensions,
                "installed": clarifier.installation_date,
                "last_maintenance": clarifier.last_maintenance_date,
                "maintenance_due": overdue,
                "days_since_maintenance": max(0, (today - clarifier.last_maintenance_date).days),
                "valve_count": valve_count_by_clarifier.get(clarifier.pk, 0),
            })

        reservoirs = list(phase_scope(Reservoir.objects.select_related("phase", "phase__treatment_plant")).order_by("phase__name", "name"))
        reservoir_ids = [reservoir.pk for reservoir in reservoirs]
        levels = ReservoirLevel.objects.filter(reservoir_id__in=reservoir_ids, date__lte=today).select_related("reservoir", "monitoring_time").order_by(
            "reservoir_id", "-date", "-monitoring_time__time", "-created_at"
        )
        latest_levels = {}
        for level in levels.iterator(chunk_size=200):
            latest_levels.setdefault(level.reservoir_id, level)
            stamp = level.updated_at or level.created_at
            latest_activity = max(latest_activity, stamp) if latest_activity else stamp
            if len(latest_levels) == len(reservoir_ids):
                break
        reservoir_rows = []
        for reservoir in reservoirs:
            level = latest_levels.get(reservoir.pk)
            capacity = _number(reservoir.volume_m3(), 2)
            fill = max(0, min(100, round(float(level.level_m) * 100 / float(reservoir.depth_m), 1))) if level and reservoir.depth_m else None
            reservoir_rows.append({
                "name": reservoir.name,
                "phase": reservoir.phase.name,
                "plant": reservoir.phase.treatment_plant.name,
                "capacity": capacity,
                "dimensions": f"{_number(reservoir.length_m, 2)} × {_number(reservoir.width_m, 2)} × {_number(reservoir.depth_m, 2)} m",
                "depth": _number(reservoir.depth_m, 2),
                "level": _number(level.level_m, 2) if level else None,
                "volume": _number(level.volume_m3(), 2) if level else None,
                "fill": fill,
                "date": level.date if level else None,
                "time": str(level.monitoring_time) if level else "—",
                "state": "low" if fill is not None and fill < 20 else "normal" if fill is not None else "unmonitored",
            })
        total_reservoir_capacity = sum(row["capacity"] for row in reservoir_rows)
        measured_storage = sum(row["volume"] or 0 for row in reservoir_rows)
        measured_capacity = sum(row["capacity"] for row in reservoir_rows if row["volume"] is not None)
        monitored_reservoir_count = sum(1 for row in reservoir_rows if row["volume"] is not None)
        unmonitored_reservoir_count = len(reservoir_rows) - monitored_reservoir_count
        capacity_fill = round(measured_storage * 100 / measured_capacity, 1) if measured_capacity else 0
        low_reservoirs = [row for row in reservoir_rows if row["state"] == "low"]

        reservoir_trend_by_day = defaultdict(list)
        recent_level_observations = ReservoirLevel.objects.filter(
            reservoir_id__in=reservoir_ids,
            date__gte=start_date,
            date__lte=today,
        ).values_list(
            "date", "reservoir_id", "level_m", "reservoir__depth_m", "monitoring_time__time", "created_at"
        ).order_by("reservoir_id", "date", "monitoring_time__time", "created_at")
        daily_reservoir_latest = {}
        for date_value, reservoir_id, level_value, depth_value, _time_value, _created_at in recent_level_observations.iterator(chunk_size=2000):
            daily_reservoir_latest[(date_value, reservoir_id)] = (float(level_value), float(depth_value))
        for (date_value, _reservoir_id), (level_value, depth_value) in daily_reservoir_latest.items():
            if depth_value > 0:
                reservoir_trend_by_day[date_value].append(max(0, min(100, level_value * 100 / depth_value)))
        reservoir_fill_trend = [
            _number(sum(values) / len(values), 1) if values else None
            for values in (reservoir_trend_by_day.get(start_date + timedelta(days=offset), []) for offset in range(period_days))
        ]

        latest_level_rows = []
        for level in levels.order_by("-date", "-monitoring_time__time", "-created_at")[:60]:
            latest_level_rows.append({
                "reservoir": level.reservoir.name,
                "phase": level.reservoir.phase.name,
                "plant": level.reservoir.phase.treatment_plant.name,
                "level": _number(level.level_m, 2),
                "volume": _number(level.volume_m3(), 2),
                "date": level.date,
                "time": str(level.monitoring_time),
                "recorded_at": level.created_at,
            })

        categories = list(ChemicalCategory.objects.select_related("chemical").order_by("category", "chemical__name"))
        category_rows = [{"chemical": str(row.chemical), "category": row.get_category_display(), "key": row.category} for row in categories]
        monitoring_times = list(MonitoringTime.objects.order_by("time"))
        monitoring_rows = [{"time": str(value), "time_value": value.time.strftime("%H:%M")} for value in monitoring_times]

        category_labels = [label for label, _model, _color in DOSING_MODELS]
        category_data = [dosing_counts[label] for label in category_labels]
        latest_usage_rows = []
        for row in usage_records[:35]:
            latest_usage_rows.append({
                "chemical": row.chemical.name,
                "plant": row.treatment_plant.name,
                "quantity": _number(row.quantity_used_kg, 2),
                "date": row.date_used,
                "operator": str(row.recorded_by or "—"),
                "remarks": row.remarks or "",
            })
            stamp = row.updated_at or row.created_at
            latest_activity = max(latest_activity, stamp) if latest_activity else stamp

        avg_flow = sum(all_flow_values) / len(all_flow_values) if all_flow_values else None
        avg_flow_today = sum(today_flow_values) / len(today_flow_values) if today_flow_values else None
        flow_standard_deviation = sqrt(sum((value - avg_flow) ** 2 for value in all_flow_values) / len(all_flow_values)) if all_flow_values and avg_flow is not None else None
        flow_coefficient_variation = round(flow_standard_deviation * 100 / avg_flow, 1) if flow_standard_deviation is not None and avg_flow and avg_flow > 0 else None
        compliance_rate = round(compliance_ok * 100 / compliance_total, 1) if compliance_total else None
        total_dosing = sum(dosing_counts.values())
        qc_sample_total = sum(row["samples"] for row in quality_stage_rows)
        qc_measurement_total = sum(row["measurements"] for row in quality_stage_rows)
        online_sensor_total = sum(row["online_sensors"] for row in quality_stage_rows)
        online_sensor_fresh = sum(row["online_fresh"] for row in quality_stage_rows)
        online_sensor_issues = sum(row["online_issues"] for row in quality_stage_rows)
        treated_quality = next((row for row in quality_stage_rows if row["key"] == SamplingSchedule.TREATED), None)
        kpis = [
            {"label": "Raw water inflow", "value": f"{avg_flow_today if avg_flow_today is not None else avg_flow:,.1f}" if avg_flow_today is not None or avg_flow is not None else "—", "unit": "m³/h avg", "icon": "fa-water", "tone": "cyan", "target": "Raw-water feed · O dosing records" if avg_flow_today is not None else f"{period_days}-day mean · raw-water feed", "href": "#water-quality-telemetry"},
            {"label": "QC water samples", "value": f"{qc_sample_total:,}", "unit": "stage samples", "icon": "fa-flask-vial", "tone": "blue", "target": f"{qc_measurement_total:,} measured parameter results · {period_days} days", "href": "#water-quality-telemetry"},
            {"label": "Treated water compliance", "value": f"{treated_quality['compliance']:.1f}%" if treated_quality and treated_quality["compliance"] is not None else "—", "unit": "of scored results", "icon": "fa-shield-halved", "tone": "green" if treated_quality and treated_quality["compliance"] is not None and treated_quality["compliance"] >= 95 else "amber" if treated_quality and treated_quality["compliance"] is not None and treated_quality["compliance"] >= 80 else "red" if treated_quality and treated_quality["compliance"] is not None else "blue", "target": f"{treated_quality['within']:,} of {treated_quality['compared']:,} results within plant limits" if treated_quality else "No treated-water QC results", "href": "#water-quality-telemetry"},
            {"label": "Online probe freshness", "value": f"{online_sensor_fresh}/{online_sensor_total}" if online_sensor_total else "—", "unit": "fresh / mapped", "icon": "fa-satellite-dish", "tone": "green" if online_sensor_total and not online_sensor_issues else "amber", "target": f"{online_sensor_issues} stale, flagged or untracked · plant scoped", "href": "#water-quality-telemetry"},
            {"label": "Chemical dosing runs", "value": f"{total_dosing:,}", "unit": "records", "icon": "fa-vials", "tone": "violet", "target": f"Last {period_days} days", "href": "#dosing-records"},
            {"label": "Within ±15% expected", "value": f"{compliance_rate:.1f}%" if compliance_rate is not None else "—", "unit": "of comparable runs", "icon": "fa-bullseye", "tone": "green" if compliance_rate is not None and compliance_rate >= 85 else "amber", "target": f"{compliance_ok:,} of {compliance_total:,} comparable runs", "href": "#dosing-records"},
            {"label": "Pump availability", "value": f"{pump_summary['working']}/{len(pump_rows)}", "unit": "working / total", "icon": "fa-pump-soap", "tone": "green" if not pump_summary["faulty"] else "red", "target": f"{pump_summary['faulty']} faulty · {pump_summary['maintenance']} in maintenance", "href": "#chemical-pumps"},
            {"label": "Reservoir storage", "value": f"{capacity_fill:.1f}%" if measured_capacity else "—", "unit": f"{measured_storage:,.0f} / {measured_capacity:,.0f} m³ monitored", "icon": "fa-water-ladder", "tone": "amber" if low_reservoirs else "blue", "target": f"{monitored_reservoir_count}/{len(reservoir_rows)} monitored · {len(low_reservoirs)} below 20%", "href": "#reservoirs"},
            {"label": "Chemical stock", "value": f"{total_stock:,.0f}", "unit": "kg on record", "icon": "fa-boxes-stacked", "tone": "amber" if low_stock_rows else "green", "target": f"{len(low_stock_rows)} stock lines below 50 kg", "href": "#chemical-stocks"},
            {"label": "Filter readiness", "value": f"{filter_status_counts.get('working', 0)}/{len(filter_rows)}", "unit": "working / total", "icon": "fa-filter", "tone": "green" if not filter_status_counts.get("isolated", 0) and not filter_status_counts.get("maintenance", 0) else "amber", "target": f"{filter_flow_total:,.1f} / {filter_capacity_total:,.1f} m³/h flow / capacity", "href": "#t-filters"},
            {"label": "Clarifiers due", "value": f"{clarifier_maintenance_due}", "unit": f"of {len(clarifier_rows)} assets", "icon": "fa-industry", "tone": "amber" if clarifier_maintenance_due else "blue", "target": f"{len(valves)} disludge valves · {len(valve_status_counts)} status types", "href": "#t-clarifiers"},
        ]

        alerts = []
        for row in low_stock_rows[:8]:
            alerts.append({"level": "warning", "title": f"Low chemical stock · {row['chemical']}", "detail": f"{row['quantity']:,.1f} kg recorded at {row['plant']}.", "target": "#chemical-stocks"})
        for row in inventory_coverage_rows:
            if row["stock"] is not None and row["stock"] >= 50 and row["coverage_days"] is not None and row["coverage_days"] < 7:
                alerts.append({
                    "level": "warning",
                    "title": f"Fast-use stock cover · {row['chemical']}",
                    "detail": f"About {row['coverage_days']:.1f} days at the selected period's average usage rate.",
                    "target": "#inventory-coverage",
                })
        for row in low_reservoirs[:8]:
            alerts.append({"level": "warning", "title": f"Low reservoir level · {row['name']}", "detail": f"Latest recorded level is {row['fill']:.1f}% of depth.", "target": "#reservoirs"})
        for row in quality_rows:
            if row["latest_within"] is False and row["latest_age_days"] is not None and row["latest_age_days"] <= 2:
                limit_text = "configured limits"
                if row["limit_min"] is not None and row["limit_max"] is not None:
                    limit_text = f"{row['limit_min']:.3f}–{row['limit_max']:.3f} {row['unit']}"
                elif row["limit_min"] is not None:
                    limit_text = f"minimum {row['limit_min']:.3f} {row['unit']}"
                elif row["limit_max"] is not None:
                    limit_text = f"maximum {row['limit_max']:.3f} {row['unit']}"
                alerts.append({
                    "level": "critical",
                    "title": f"Latest QC result outside limits · {row['stage']} {row['parameter']}",
                    "detail": f"{row['latest']:.3f} {row['unit']} · allowed {limit_text} · {row['latest_reference'] or 'QC sample'}.",
                    "target": "#water-quality-telemetry",
                })
            online = row["online"]
            if online and online["within_treatment_limits"] is False:
                limit_text = "configured stage limits"
                if online["limit_min"] is not None and online["limit_max"] is not None:
                    limit_text = f"{online['limit_min']:.3f}–{online['limit_max']:.3f} {row['unit']}"
                elif online["limit_min"] is not None:
                    limit_text = f"minimum {online['limit_min']:.3f} {row['unit']}"
                elif online["limit_max"] is not None:
                    limit_text = f"maximum {online['limit_max']:.3f} {row['unit']}"
                alerts.append({
                    "level": "critical",
                    "title": f"Online reading outside plant limits · {row['stage']} {row['parameter']}",
                    "detail": f"{online['value']:.3f} {row['unit']} · allowed {limit_text} · {online['instrument']} / {online['sensor']}.",
                    "target": "#water-quality-telemetry",
                })
            if online and online["state_key"] != "working":
                alerts.append({
                    "level": "critical" if online["state_key"] == "faulty" else "warning",
                    "title": f"Water quality probe {online['state_label'].lower()} · {row['stage']} {row['parameter']}",
                    "detail": f"{online['value']:.3f} {row['unit']} from {online['instrument']} / {online['sensor']} · {online['measured_at']:%d %b %Y %H:%M}.",
                    "target": "#water-quality-telemetry",
                })
        for row in pump_rows:
            if row["status"] in ("faulty", "maintenance"):
                alerts.append({"level": "critical" if row["status"] == "faulty" else "warning", "title": f"Pump {row['status']} · {row['name']}", "detail": row["remarks"] or f"{row['phase']} · {row['category']}.", "target": "#chemical-pumps"})
        if pump_summary["unknown"]:
            alerts.append({
                "level": "warning",
                "title": "Pump status coverage incomplete",
                "detail": f"{pump_summary['unknown']} of {len(pump_rows)} configured pumps have no status event.",
                "target": "#chemical-pumps",
            })
        for phase in phase_rows:
            if phase["flow_load"] is not None and phase["flow_load"] > 100:
                alerts.append({
                    "level": "warning",
                    "title": f"Recorded flow above operational capacity · {phase['name']}",
                    "detail": f"{phase['recorded_flow']:.1f} m³/h average ({phase['flow_source']}) against {phase['operational_capacity']:.1f} m³/h operational capacity.",
                    "target": "#process-dynamics",
                })
        for filter_row in filter_rows:
            if filter_row["utilization"] is not None and filter_row["utilization"] > 100:
                alerts.append({
                    "level": "warning",
                    "title": f"Filter flow exceeds design · {filter_row['name']}",
                    "detail": f"Recorded model flow is {filter_row['flow_rate']:.1f} m³/h against {filter_row['capacity']:.1f} m³/h capacity.",
                    "target": "#t-filters",
                })
        filter_interventions = [row["name"] for row in filter_rows if row["status"] in ("isolated", "maintenance")]
        if filter_interventions:
            alerts.append({"level": "warning", "title": f"Filters out of service or in maintenance · {len(filter_interventions)} units", "detail": ", ".join(filter_interventions[:4]), "target": "#t-filters"})
        overdue_filter_names = [row["name"] for row in filter_rows if row["maintenance_due"]]
        if overdue_filter_names:
            alerts.append({"level": "warning", "title": f"Filter maintenance due · {len(overdue_filter_names)} units", "detail": ", ".join(overdue_filter_names[:4]), "target": "#t-filters"})
        overdue_clarifier_names = [row["name"] for row in clarifier_rows if row["maintenance_due"]]
        if overdue_clarifier_names:
            alerts.append({"level": "warning", "title": f"Clarifier maintenance due · {len(overdue_clarifier_names)} units", "detail": ", ".join(overdue_clarifier_names[:4]), "target": "#t-clarifiers"})

        persistent_alerts = AlertState.objects.filter(
            state__in=(AlertState.StateChoices.QUEUED, AlertState.StateChoices.SENT, AlertState.StateChoices.ESCALATED),
            alert_config__alert_type__in=(
                "sensor_threshold", "sensor_health", "derived_metric", "phase_anomaly",
                "performance_deviation", "water_quality_limit", "maintenance_due",
            ),
        ).select_related("alert_config").order_by("-created_at")
        if plant:
            persistent_alerts = persistent_alerts.filter(geo_context__plant_id=str(plant.pk))
        elif can_view_unscoped:
            persistent_alerts = persistent_alerts.exclude(geo_context__plant_id__isnull=True)
        else:
            persistent_alerts = persistent_alerts.none()
        for state in persistent_alerts[:50]:
            level = "critical" if state.priority in ("critical", "high") else "warning"
            state_label = state.get_state_display() if hasattr(state, "get_state_display") else state.state.replace("_", " ").title()
            alerts.append({
                "level": level,
                "title": state.title or state.alert_config.get_alert_type_display(),
                "detail": f"{state.message} · {state_label} · {state.priority.title()} priority · opened {timezone.localtime(state.created_at):%d %b %Y %H:%M}",
                "target": "/alerts/",
                "persistent": True,
                "state": state.state,
                "priority": state.priority,
                "created_at": state.created_at,
            })
        alerts.sort(key=lambda alert: alert["level"] != "critical")
        alerts = alerts[:12]
        if not has_plant_scope:
            operational_state, operational_tone = "Plant scope required", "amber"
        elif any(alert["level"] == "critical" for alert in alerts):
            operational_state, operational_tone = "Critical signal recorded", "red"
        elif alerts:
            operational_state, operational_tone = "Attention in latest records", "amber"
        elif total_dosing or pump_rows or reservoir_rows or filters or clarifiers or valves:
            operational_state, operational_tone = "No exceptions recorded", "green"
        else:
            operational_state, operational_tone = "Awaiting plant records", "blue"

        plant_name = plant.name if plant else "All treatment plants" if is_superuser else "Plant not assigned"
        nav_items = [
            ("overview", "Command overview", "fa-gauge-high", ""),
            ("process-dynamics", "Phase dynamics", "fa-water", ""),
            ("dosing-records", "All dosing records", "fa-vials", "all"),
            ("dosing-records", "Coagulant usage", "fa-vials", "coagulant"),
            ("dosing-records", "pH adjuster usage", "fa-flask", "ph-adjuster"),
            ("dosing-records", "Disinfectant usage", "fa-shield-virus", "disinfectant"),
            ("dosing-records", "Flocculant usage", "fa-atom", "flocculant"),
            ("chemical-pumps", "Chemical pumps", "fa-pump-soap", ""),
            ("pump-status-history", "PumpStatus history", "fa-clock-rotate-left", ""),
            ("chemical-tanks", "Chemical tanks", "fa-database", ""),
            ("chemical-stocks", "Chemical stock", "fa-boxes-stacked", ""),
            ("chemical-usage", "Chemical usage records", "fa-arrow-trend-down", ""),
            ("reservoirs", "Reservoirs", "fa-water-ladder", ""),
            ("reservoir-levels", "ReservoirLevel history", "fa-chart-area", ""),
            ("chemical-categories", "Chemical categories", "fa-layer-group", ""),
            ("monitoring-times", "Monitoring schedule", "fa-clock", ""),
            ("t-clarifiers", "Clarifier telemetry", "fa-industry", ""),
            ("t-filters", "Water filter telemetry", "fa-filter", ""),
            ("t-valves", "Disludge valve status", "fa-toggle-on", ""),
            ("inventory-coverage", "Stock coverage forecast", "fa-chart-line", ""),
            ("analytics-reports", "Analytics & reports", "fa-file-chart-column", ""),
        ]

        chart_payload = {
            "labels": chart_labels,
            "usage": usage_trend,
            "flow": flow_trend,
            "flow_daily_min": flow_daily_min,
            "flow_daily_max": flow_daily_max,
            "flow_rolling_average": flow_rolling_average,
            "reservoir_fill": reservoir_fill_trend,
            "reservoir_low_threshold": [20 for _ in chart_labels],
            "compliance": compliance_trend,
            "water_quality": quality_chart,
            "water_quality_stages": {
                "labels": [row["label"] for row in quality_stage_rows],
                "samples": [row["samples"] for row in quality_stage_rows],
                "measurements": [row["measurements"] for row in quality_stage_rows],
                "compliance": [row["compliance"] for row in quality_stage_rows],
            },
            "established_dosage_trends": {
                "labels": chart_labels,
                "datasets": [
                    {"label": row["label"], "values": row["trend"], "unit": row["unit"]}
                    for row in approved_dosages
                ],
            },
            "pump_status": pump_summary,
            "categories": {"labels": category_labels, "data": category_data},
            "dosing_quality": {
                "labels": [row["type"] for row in dosing_quality_rows],
                "rates": [row["rate"] for row in dosing_quality_rows],
                "records": [row["records"] for row in dosing_quality_rows],
                "compared": [row["compared"] for row in dosing_quality_rows],
            },
            "phase_performance": {
                "labels": [row["name"] for row in phase_rows],
                "flow": [row["recorded_flow"] for row in phase_rows],
                "capacity": [row["operational_capacity"] for row in phase_rows],
            },
            "inventory_coverage": {
                "labels": [row["chemical"] for row in inventory_coverage_rows[:12]],
                "days": [row["coverage_days"] for row in inventory_coverage_rows[:12]],
            },
            "chemical_usage_rankings": {
                "labels": [f"{row['plant']} · {row['chemical']}" for row in chemical_usage_rankings],
                "quantities": [row["quantity"] for row in chemical_usage_rankings],
            },
            "filter_train": {
                "labels": [row["name"] for row in filter_rows],
                "flow": [row["flow_rate"] for row in filter_rows],
                "capacity": [row["capacity"] for row in filter_rows],
            },
            "filter_status": {
                "labels": [label for _key, label in WaterFilter.STATUS_CHOICES] + [key for key in filter_status_counts if key not in dict(WaterFilter.STATUS_CHOICES)],
                "data": [filter_status_counts.get(key, 0) for key, _label in WaterFilter.STATUS_CHOICES] + [filter_status_counts[key] for key in filter_status_counts if key not in dict(WaterFilter.STATUS_CHOICES)],
            },
            "clarifier_capacity": {
                "labels": [row["name"] for row in clarifier_rows],
                "capacity": [row["design_capacity"] for row in clarifier_rows],
                "volume": [row["volume_capacity"] for row in clarifier_rows],
            },
            "treatment_capacity": {
                "labels": [f"Filter · {row['name']}" for row in filter_rows]
                    + [f"Clarifier · {row['name']}" for row in clarifier_rows],
                "filter_flow": [row["flow_rate"] for row in filter_rows]
                    + [None for _row in clarifier_rows],
                "clarifier_capacity": [None for _row in filter_rows]
                    + [row["design_capacity"] for row in clarifier_rows],
            },
            "valve_status": {
                "labels": [row["name"] for row in valve_status_rows],
                "data": [row["count"] for row in valve_status_rows],
            },
            "stocks": {"labels": [row["chemical"] for row in stock_rows[:14]], "data": [row["quantity"] for row in stock_rows[:14]]},
            "reservoirs": {"labels": [row["name"] for row in reservoir_rows], "data": [row["fill"] for row in reservoir_rows]},
        }
        report_payload = {
            "plant": plant_name,
            "generated_on": today,
            "period_days": period_days,
            "start_date": start_date,
            "end_date": today,
            "summary": {
                "recorded_flow_average_m3h": _number(avg_flow_today if avg_flow_today is not None else avg_flow, 2) if avg_flow_today is not None or avg_flow is not None else None,
                "flow_observations": len(all_flow_values),
                "flow_standard_deviation_m3h": _number(flow_standard_deviation, 2) if flow_standard_deviation is not None else None,
                "flow_coefficient_variation_percent": flow_coefficient_variation,
                "qc_samples": qc_sample_total,
                "qc_measurements": qc_measurement_total,
                "online_sensors_mapped": online_sensor_total,
                "online_sensors_fresh": online_sensor_fresh,
                "online_sensor_issues": online_sensor_issues,
                "quality_plant_required": quality_plant_required,
                "qc_stage_compliance": quality_stage_rows,
                "established_dosages": approved_dosages,
                "dosing_records": total_dosing,
                "dosing_alignment_percent": compliance_rate,
                "dosing_comparisons": compliance_total,
                "chemical_usage_kg": _number(usage_total, 2),
                "stock_on_record_kg": _number(total_stock, 2),
                "reservoir_storage_m3": _number(measured_storage, 2),
                "reservoir_capacity_monitored_m3": _number(measured_capacity, 2),
                "pumps_working": pump_summary["working"],
                "pumps_total": len(pump_rows),
                "filters_working": filter_status_counts.get("working", 0),
                "filters_total": len(filter_rows),
                "filter_flow_m3h": _number(filter_flow_total, 2),
                "filter_capacity_m3h": _number(filter_capacity_total, 2),
                "filter_utilization_percent": filter_overall_utilization,
                "filters_maintenance_due": filter_maintenance_due,
                "filter_status_counts": filter_status_counts,
                "clarifiers_total": len(clarifier_rows),
                "clarifier_design_capacity_m3h": _number(clarifier_design_total, 2),
                "clarifier_basin_volume_m3": _number(clarifier_volume_total, 2),
                "clarifiers_maintenance_due": clarifier_maintenance_due,
                "disludge_valves": len(valves),
                "disludge_valve_statuses": valve_status_rows,
                "alerts": len(alerts),
            },
            "daily_trends": [
                {"date": chart_labels[index], "flow_m3h": flow_trend[index], "chemical_usage_kg": usage_trend[index],
                 "flow_min_m3h": flow_daily_min[index], "flow_max_m3h": flow_daily_max[index],
                 "flow_7day_average_m3h": flow_rolling_average[index], "reservoir_fill_percent": reservoir_fill_trend[index],
                 "dosing_alignment_percent": compliance_trend[index]}
                for index in range(period_days)
            ],
            "chemical_usage_rankings": chemical_usage_rankings,
            "dosing_quality": dosing_quality_rows,
            "water_quality_stages": quality_stage_rows,
            "water_quality_parameters": quality_rows,
            "water_quality_daily_trends": quality_chart,
            "water_quality_current": quality_rows,
            "online_sensor_readings_without_data": online_sensor_rows,
            "established_dosages": approved_dosages,
            "established_dosage_trends": {
                "labels": chart_labels,
                "datasets": [
                    {"label": row["label"], "values": row["trend"], "unit": row["unit"]}
                    for row in approved_dosages
                ],
            },
            "phase_performance": phase_rows,
            "inventory_coverage": inventory_coverage_rows,
            "reservoirs": reservoir_rows,
            "pump_status": pump_rows,
            "filters": filter_rows,
            "filter_types": filter_type_rows,
            "clarifiers": clarifier_rows,
            "disludge_valves": valve_rows,
            "valve_statuses": valve_status_rows,
            "alerts": alerts,
        }
        payload = {
            "plant": {"id": plant.pk, "name": plant.name, "code": plant.code} if plant else None,
            "period_days": period_days,
            "active_tab": active_tab,
            "kpis": kpis,
            "pump_status": pump_summary,
            "dosing_counts": dosing_counts,
            "usage_total_kg": _number(usage_total, 2),
            "reservoir_storage_m3": _number(measured_storage, 2),
            "reservoir_capacity_m3": _number(total_reservoir_capacity, 2),
            "reservoir_monitored_capacity_m3": _number(measured_capacity, 2),
            "alerts": alerts,
            "charts": chart_payload,
            "report": report_payload,
            "latest_activity": latest_activity,
            "operational_state": operational_state,
            "operational_tone": operational_tone,
            "flow_observation_count": len(all_flow_values),
            "qc_sample_count": qc_sample_total,
            "qc_measurement_count": qc_measurement_total,
            "water_quality_stages": quality_stage_rows,
            "water_quality_rows": quality_rows,
            "online_sensor_rows": online_sensor_rows,
            "online_sensor_total": online_sensor_total,
            "online_sensor_fresh": online_sensor_fresh,
            "online_sensor_issues": online_sensor_issues,
            "quality_plant_required": quality_plant_required,
            "water_quality_parameters": quality_parameter_options,
            "selected_quality_parameter": selected_quality_parameter,
            "water_quality_chart": quality_chart,
            "established_dosages": approved_dosages,
            "monitored_reservoir_count": monitored_reservoir_count,
            "stock_alert_count": len(low_stock_rows),
            "flow_display": _number(avg_flow_today if avg_flow_today is not None else avg_flow, 1) if avg_flow_today is not None or avg_flow is not None else None,
            "flow_standard_deviation": _number(flow_standard_deviation, 2) if flow_standard_deviation is not None else None,
            "flow_coefficient_variation": flow_coefficient_variation,
            "flow_metric_source": "today's entries" if avg_flow_today is not None else f"{period_days}-day mean" if avg_flow is not None else "awaiting readings",
        }
        return {
            "dashboard_title": "Water Treatment Operations",
            "dashboard_subtitle": "Chemical process control · operational records",
            "plant": plant,
            "plant_name": plant_name,
            "plant_options": plant_options,
            "is_superuser": is_superuser,
            "has_plant_scope": has_plant_scope,
            "today": today,
            "period_days": period_days,
            "active_tab": active_tab,
            "allowed_periods": ALLOWED_PERIODS,
            "start_date": start_date,
            "kpis": kpis,
            "dosing_counts": dosing_counts,
            "dosing_quality_rows": dosing_quality_rows,
            "dosing_records": dosing_rows,
            "dosing_total": total_dosing,
            "compliance_rate": compliance_rate,
            "compliance_total": compliance_total,
            "usage_total": _number(usage_total, 2),
            "usage_records": latest_usage_rows,
            "pump_rows": pump_rows,
            "pump_status_history": status_history_rows,
            "pump_summary": pump_summary,
            "tank_rows": tank_rows,
            "tank_capacity": tank_capacity,
            "stock_rows": stock_rows,
            "inventory_coverage_rows": inventory_coverage_rows,
            "low_stock_count": len(low_stock_rows),
            "phase_rows": phase_rows,
            "reservoir_rows": reservoir_rows,
            "filter_rows": filter_rows,
            "filter_status_counts": filter_status_counts,
            "filter_status_rows": filter_status_rows,
            "filter_flow_total": _number(filter_flow_total, 2),
            "filter_capacity_total": _number(filter_capacity_total, 2),
            "filter_overall_utilization": filter_overall_utilization,
            "filter_maintenance_due": filter_maintenance_due,
            "filter_type_rows": filter_type_rows,
            "clarifier_rows": clarifier_rows,
            "clarifier_design_total": _number(clarifier_design_total, 2),
            "clarifier_volume_total": _number(clarifier_volume_total, 2),
            "clarifier_maintenance_due": clarifier_maintenance_due,
            "valve_rows": valve_rows,
            "valve_status_rows": valve_status_rows,
            "status_catalog": status_catalog,
            "reservoir_level_rows": latest_level_rows,
            "reservoir_capacity": total_reservoir_capacity,
            "reservoir_measured_capacity": measured_capacity,
            "reservoir_storage": measured_storage,
            "reservoir_fill": capacity_fill,
            "monitored_reservoir_count": monitored_reservoir_count,
            "unmonitored_reservoir_count": unmonitored_reservoir_count,
            "category_rows": category_rows,
            "monitoring_rows": monitoring_rows,
            "alerts": alerts,
            "nav_items": nav_items,
            "latest_activity": latest_activity,
            "operational_state": operational_state,
            "operational_tone": operational_tone,
            "flow_display": _number(avg_flow_today if avg_flow_today is not None else avg_flow, 1) if avg_flow_today is not None or avg_flow is not None else None,
            "flow_standard_deviation": _number(flow_standard_deviation, 2) if flow_standard_deviation is not None else None,
            "flow_coefficient_variation": flow_coefficient_variation,
            "flow_metric_source": "today's entries" if avg_flow_today is not None else f"{period_days}-day mean" if avg_flow is not None else "awaiting readings",
            "flow_observation_count": len(all_flow_values),
            "qc_sample_count": qc_sample_total,
            "qc_measurement_count": qc_measurement_total,
            "water_quality_stages": quality_stage_rows,
            "water_quality_rows": quality_rows,
            "online_sensor_rows": online_sensor_rows,
            "online_sensor_total": online_sensor_total,
            "online_sensor_fresh": online_sensor_fresh,
            "online_sensor_issues": online_sensor_issues,
            "quality_plant_required": quality_plant_required,
            "water_quality_parameters": quality_parameter_options,
            "selected_quality_parameter": selected_quality_parameter,
            "water_quality_chart": quality_chart,
            "established_dosages": approved_dosages,
            "total_dosing": total_dosing,
            "chart_json": chart_payload,
            "report_json": report_payload,
            "dashboard_json_payload": payload,
        }
