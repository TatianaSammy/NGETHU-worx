from collections import Counter
from datetime import timedelta

from django.db.models import Avg, Count, OuterRef, Q, Subquery
from django.utils import timezone
from django.views import View

from core.domain_dashboard import dashboard_context, model_list_url, period_days, render_domain_dashboard, time_series
from core.mixins import WanjalaMixin
from t.models import TreatmentPlant
from .models import (
    DerivedMetric,
    Instrument,
    InstrumentCalibration,
    OnlineMeasurement,
    QualityFlag,
    Sensor,
    SensorHealthSnapshot,
)


class DashboardView(WanjalaMixin, View):
    template_name = "instruments/dashboard.html"
    app_label = "instruments"

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        return render_domain_dashboard(request, self.template_name, self.app_label, self.get_context(request))

    def get_context(self, request):
        days = period_days(request)
        now = timezone.now()
        today = timezone.localdate()
        start_at = now - timedelta(days=days)
        plants, selected_plant, is_superuser = self._plants(request)
        instruments = Instrument.objects.select_related("treatment_plant", "phase")
        sensors = Sensor.objects.select_related("instrument", "parameter", "phase", "treatment_plant")
        measurements = OnlineMeasurement.objects.select_related("sensor", "sensor__parameter", "sensor__instrument", "phase")
        metrics = DerivedMetric.objects.select_related("sensor", "sensor__parameter")
        calibrations = InstrumentCalibration.objects.select_related("instrument", "performed_by")
        health = SensorHealthSnapshot.objects.select_related("sensor", "sensor__parameter")
        if selected_plant:
            instruments = instruments.filter(treatment_plant=selected_plant)
            sensors = sensors.filter(treatment_plant=selected_plant)
            measurements = measurements.filter(treatment_plant=selected_plant)
            metrics = metrics.filter(treatment_plant=selected_plant)
            calibrations = calibrations.filter(instrument__treatment_plant=selected_plant)
            health = health.filter(sensor__treatment_plant=selected_plant)
        elif not is_superuser:
            instruments = instruments.none()
            sensors = sensors.none()
            measurements = measurements.none()
            metrics = metrics.none()
            calibrations = calibrations.none()
            health = health.none()

        active_instruments = instruments.filter(is_active=True)
        instrument_rows = list(active_instruments)
        stale_count = sum(item.is_stale() or item.status != "online" for item in instrument_rows)
        online_count = sum(not item.is_stale() and item.status == "online" for item in instrument_rows)
        sensors_active = sensors.filter(is_active=True)
        window_measurements = measurements.filter(measured_at__gte=start_at, measured_at__lte=now)
        measurement_count = window_measurements.count()
        quality_counts = {row["quality_flag"]: row["total"] for row in window_measurements.values("quality_flag").annotate(total=Count("pk"))}
        flagged_count = sum(quality_counts.get(flag, 0) for flag in [QualityFlag.SUSPECT, QualityFlag.INVALID, QualityFlag.STALE, QualityFlag.CALIBRATION])
        invalid_count = quality_counts.get(QualityFlag.INVALID, 0)
        suspect_count = quality_counts.get(QualityFlag.SUSPECT, 0)
        latest_calibration = InstrumentCalibration.objects.filter(instrument_id=OuterRef("pk")).order_by("-performed_at")
        calibration_assets = active_instruments.annotate(last_calibration_date=Subquery(latest_calibration.values("valid_until")[:1]))
        calibration_due = calibration_assets.filter(Q(last_calibration_date__lt=today) | Q(last_calibration_date__isnull=True)).count()
        latest_health = SensorHealthSnapshot.objects.filter(sensor_id=OuterRef("pk")).order_by("-assessed_at")
        health_average = sensors_active.annotate(last_health=Subquery(latest_health.values("health_score")[:1])).aggregate(value=Avg("last_health"))["value"]
        trend = time_series(window_measurements, "measured_at", days)
        status_counts = Counter(item.status for item in instrument_rows)

        recent_measurements = list(window_measurements.order_by("-measured_at")[:60])
        recent_records = [{
            "title": f"{measurement.sensor.name} · {measurement.sensor.parameter.name}",
            "detail": f"{measurement.value} {measurement.sensor.unit} · {measurement.quality_flag.upper()} · {measurement.sensor.instrument.name}",
            "timestamp": timezone.localtime(measurement.measured_at).strftime("%d %b %H:%M:%S"),
            "icon": "wave-square",
            "color": "emerald" if measurement.quality_flag == QualityFlag.OK else "rose" if measurement.quality_flag == QualityFlag.INVALID else "amber",
        } for measurement in recent_measurements[:12]]
        table_rows = [{"cells": [
            item.sensor.name,
            item.sensor.parameter.name,
            f"{item.value} {item.sensor.unit}",
            item.sensor.stage.replace("_", " ").title(),
            item.quality_flag.upper(),
            f"{item.anomaly_score:.2f} / {item.drift_score:.2f}",
            timezone.localtime(item.measured_at).strftime("%d %b %Y %H:%M:%S"),
        ]} for item in recent_measurements]
        model_specs = [
            ("Instruments", instruments, "microchip", "Gateways & controllers"),
            ("Sensors", sensors, "wave-square", "Measurement points"),
            ("Online measurements", window_measurements, "chart-line", "Telemetry events"),
            ("Derived metrics", metrics, "calculator", "Computed windows"),
            ("Instrument calibrations", calibrations, "certificate", "Metrology traceability"),
            ("Sensor health snapshots", health, "heart-pulse", "Health history"),
        ]
        model_rows = [{"label": label, "count": qs.count(), "url": model_list_url(self.app_label, qs.model._meta.model_name), "icon": icon, "group": group} for label, qs, icon, group in model_specs]
        status_labels = ["online", "offline", "degraded", "fault", "maintenance"]
        sensor_stage_rows = list(sensors_active.values("stage").annotate(total=Count("pk")).order_by("stage"))
        flagged_sensors = list(sensors_active.annotate(last_measurement=Subquery(
            OnlineMeasurement.objects.filter(sensor_id=OuterRef("pk")).order_by("-measured_at").values("anomaly_score")[:1]
        )).order_by("-last_measurement")[:10])
        total_sensors = sensors_active.count()
        ranked_rows = [{
            "label": f"{sensor.name} · {sensor.parameter.name}",
            "detail": f"{sensor.get_stage_display()} · {sensor.instrument.name}",
            "value": f"{float(sensor.last_measurement or 0):.2f} anomaly",
            "percent": min(100, round(float(sensor.last_measurement or 0) * 100)),
            "color": "rose" if float(sensor.last_measurement or 0) >= 0.8 else "amber",
        } for sensor in flagged_sensors]
        kpis = [
            {"label": "Online instruments", "value": online_count, "detail": f"{len(instrument_rows)} active in scope", "icon": "tower-broadcast", "color": "emerald", "tab": "overview"},
            {"label": "Stale / degraded", "value": stale_count, "detail": "Heartbeat gap or non-online status", "icon": "signal", "color": "amber", "tab": "analytics"},
            {"label": "Active sensors", "value": total_sensors, "detail": f"{len(sensor_stage_rows)} treatment stages represented", "icon": "wave-square", "color": "blue", "tab": "models"},
            {"label": "Telemetry events", "value": measurement_count, "detail": f"Received in the last {days} days", "icon": "chart-line", "color": "cyan", "tab": "records"},
            {"label": "Quality flags", "value": flagged_count, "detail": f"{invalid_count} invalid · {suspect_count} suspect", "icon": "triangle-exclamation", "color": "rose", "tab": "analytics"},
            {"label": "Calibration due", "value": calibration_due, "detail": "No valid latest calibration on record", "icon": "screwdriver-wrench", "color": "amber", "tab": "records"},
        ]
        context = dashboard_context(
            request,
            title="Instrumentation & Telemetry",
            subtitle="Live sensor quality, gateway health, signal integrity, and calibration readiness.",
            kicker="Plant telemetry network",
            kpis=kpis,
            charts=[
                {"id": "instrument-measurements", "title": "Telemetry ingestion", "caption": "Stored measurement events by observation time.", "type": "line", "labels": trend["labels"], "values": trend["values"], "color": "cyan"},
                {"id": "instrument-health", "title": "Instrument network state", "caption": "Current status field for active instrument records.", "type": "doughnut", "labels": [value.title() for value in status_labels], "values": [status_counts.get(value, 0) for value in status_labels], "colors": ["emerald", "slate", "amber", "rose", "purple"]},
                {"id": "instrument-quality", "title": "Measurement quality flags", "caption": "Recorded telemetry flags in the selected window.", "type": "bar", "labels": [flag.label for flag in QualityFlag], "values": [quality_counts.get(flag.value, 0) for flag in QualityFlag], "color": "blue"},
                {"id": "instrument-stages", "title": "Sensor coverage by treatment stage", "caption": "Enabled sensors grouped by their configured water stage.", "type": "horizontal", "labels": [row["stage"].replace("_", " ").title() for row in sensor_stage_rows], "values": [row["total"] for row in sensor_stage_rows], "color": "purple"},
            ],
            signals=[
                {"label": "Network heartbeat", "value": f"{online_count}/{len(instrument_rows)}", "detail": "Active instruments seen within their expected heartbeat", "icon": "satellite-dish", "color": "emerald"},
                {"label": "Average sensor health", "value": f"{float(health_average):.1f}%" if health_average is not None else "—", "detail": "Latest health snapshot per active sensor", "icon": "heart-pulse", "color": "blue"},
                {"label": "Invalid measurements", "value": invalid_count, "detail": "Outside the sensor's detectable range", "icon": "triangle-exclamation", "color": "rose"},
                {"label": "Suspect measurements", "value": suspect_count, "detail": "Weak signal or high anomaly score", "icon": "circle-exclamation", "color": "amber"},
            ],
            recent_records=recent_records,
            model_rows=model_rows,
            table_headings=["Sensor", "Parameter", "Value", "Stage", "Quality", "Anomaly / drift", "Measured at"],
            table_rows=table_rows,
            ranked_rows=ranked_rows,
            actions=[{"label": "Live telemetry", "url": "/instruments/telemetry/", "icon": "wave-square"}],
            overview_caption="Measurements are immutable telemetry events; health metrics are derived only from configured thresholds and saved snapshots.",
            use_plant_scope=True,
            model_count=len(model_specs),
        )
        context.update({"plant_options": plants, "selected_plant": selected_plant, "show_plant_selector": is_superuser})
        return context

    @staticmethod
    def _plants(request):
        user = request.user
        if user.is_superuser:
            options = TreatmentPlant.objects.order_by("name")
            requested = request.GET.get("plant")
            return options, options.filter(pk=requested).first() if requested and requested.isdigit() else None, True
        plant_id = getattr(user, "assigned_treatment_plant_id", None)
        options = TreatmentPlant.objects.filter(pk=plant_id) if plant_id else TreatmentPlant.objects.none()
        return options.order_by("name"), options.first(), False
