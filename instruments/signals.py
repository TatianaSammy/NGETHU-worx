# instruments/signals.py
import logging
from datetime import timedelta
from django.db.models import Avg
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone
from django.apps import apps
from celery import shared_task
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from .models import Sensor, OnlineMeasurement, DerivedMetric, SensorHealthSnapshot
from alerts.engine import AlertEngine

logger = logging.getLogger("instruments.signals")

# ============================================================
# 🔔 ASYNC TASKS
# ============================================================

@shared_task(bind=True)
def async_process_measurement(measurement_id):
    """Process a single OnlineMeasurement asynchronously."""
    try:
        engine = AlertEngine()
        measurement = OnlineMeasurement.objects.get(id=measurement_id)

        # 1️⃣ Threshold alerts
        engine.trigger_for_sensor_measurement(measurement)

        # 2️⃣ Derived metrics (hourly average as example)
        recent = OnlineMeasurement.objects.filter(
            sensor=measurement.sensor,
            measured_at__gte=timezone.now() - timedelta(hours=1)
        )
        avg_val = recent.aggregate(avg_val=Avg('value'))['avg_val'] or measurement.value
        derived_metric = DerivedMetric.objects.create(
            sensor=measurement.sensor,
            value=avg_val,
            metric_type='hourly_average',
            computed_at=timezone.now()
        )

        # Derived metric alerts
        engine.trigger_for_derived_metric(derived_metric)

        # 3️⃣ Sensor health snapshot
        health_score = compute_sensor_health(measurement.sensor)
        SensorHealthSnapshot.objects.update_or_create(
            sensor=measurement.sensor,
            defaults={"health_score": health_score, "assessed_at": timezone.now(), "reason": "async ingestion"}
        )

        # Alert if health below threshold
        if health_score < 0.7:
            engine.trigger_alert(measurement.sensor, alert_types=['sensor_health'])

        # 4️⃣ Push WebSocket alerts in real-time
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"sensor_{measurement.sensor.uuid}",
            {
                "type": "sensor.alert",
                "message": {
                    "sensor_uuid": str(measurement.sensor.uuid),
                    "value": str(measurement.value),
                    "quality_flag": measurement.quality_flag,
                    "measured_at": measurement.measured_at.isoformat()
                }
            }
        )

        logger.info(f"[async] Processed measurement {measurement_id} successfully")

    except Exception as e:
        logger.exception(f"[async] Failed to process measurement {measurement_id}: {e}")


@shared_task(bind=True)
def async_trigger_derived_metric_alert(metric_id):
    """Trigger alerts for derived metrics asynchronously."""
    try:
        metric = DerivedMetric.objects.get(id=metric_id)
        engine = AlertEngine()
        engine.trigger_for_derived_metric(metric)
        logger.info(f"[async] Triggered derived metric alert {metric_id}")
    except Exception as e:
        logger.exception(f"[async] Failed derived metric alert {metric_id}: {e}")


@shared_task(bind=True)
def async_trigger_sensor_health_alert(snapshot_id):
    """Trigger sensor health alerts asynchronously."""
    try:
        snapshot = SensorHealthSnapshot.objects.get(id=snapshot_id)
        engine = AlertEngine()
        if snapshot.health_score < 0.7:
            engine.trigger_alert(snapshot.sensor, alert_types=['sensor_health'])
        logger.info(f"[async] Sensor health alert processed for snapshot {snapshot_id}")
    except Exception as e:
        logger.exception(f"[async] Failed sensor health alert {snapshot_id}: {e}")


@shared_task(bind=True)
def async_escalate_unacknowledged_alerts():
    """Periodic task to auto-escalate unacknowledged alerts."""
    try:
        engine = AlertEngine()
        engine.auto_escalate_unacknowledged()
        logger.info("[async] Auto-escalated unacknowledged alerts")
    except Exception as e:
        logger.exception(f"[async] Auto-escalation failed: {e}")


# ============================================================
# ⚡ SIGNALS (LIGHTWEIGHT TRIGGERS)
# ============================================================

@receiver(post_save, sender=OnlineMeasurement)
def handle_sensor_reading(sender, instance: OnlineMeasurement, created, **kwargs):
    """Trigger Celery async task instead of heavy processing in sync signal."""
    if created:
        async_process_measurement.delay(instance.id)


@receiver(post_save, sender=DerivedMetric)
def handle_derived_metric(sender, instance: DerivedMetric, created, **kwargs):
    """Trigger derived metric alerts asynchronously."""
    if created:
        async_trigger_derived_metric_alert.delay(instance.id)


@receiver(post_save, sender=SensorHealthSnapshot)
def audit_sensor_health_snapshot(sender, instance: SensorHealthSnapshot, created, **kwargs):
    """Trigger sensor health alert asynchronously."""
    if instance.health_score < 0.7:
        async_trigger_sensor_health_alert.delay(instance.id)


# ============================================================
# 🧩 DERIVED METRIC AUTO-VALIDATION (SYNC)
# ============================================================

@receiver(pre_save, sender=DerivedMetric)
def validate_derived_metric(sender, instance: DerivedMetric, **kwargs):
    """Mark derived metrics as invalid/stale if thresholds exceeded."""
    try:
        sensor = instance.sensor
        if sensor.min_detectable and instance.value < sensor.min_detectable:
            instance.quality_flag = 'invalid'
        elif sensor.max_detectable and instance.value > sensor.max_detectable:
            instance.quality_flag = 'invalid'
        else:
            instance.quality_flag = 'ok'
    except Exception as e:
        logger.exception(f"[signals] DerivedMetric validation failed for {instance.id}: {e}")


# ============================================================
# 🔮 HELPER FUNCTION — SENSOR HEALTH COMPUTATION
# ============================================================

def compute_sensor_health(sensor: Sensor) -> float:
    """
    Compute sensor health score based on last 24h measurements.
    Returns float between 0.0 and 1.0
    """
    recent_measurements = OnlineMeasurement.objects.filter(
        sensor=sensor,
        measured_at__gte=timezone.now() - timedelta(hours=24)
    )
    if not recent_measurements.exists():
        return 0.5  # unknown health

    stale_count = recent_measurements.filter(quality_flag='stale').count()
    invalid_count = recent_measurements.filter(quality_flag='invalid').count()
    total = recent_measurements.count()
    score = 1.0 - (stale_count + invalid_count) / total
    return max(0.0, min(1.0, score))
