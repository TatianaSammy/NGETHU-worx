"""
Heavy Metal Signals — fully integrated with centralized AlertEngine
Features:
- Smart initialization of HeavyMetalSamplingSchedule
- Unique HeavyMetalAnalysis reference IDs
- Auto-creation of expected parameter results
- NonConformity auto-detection triggers centralized alerts
- HeavyMetalAnalysis state transitions trigger alerts
- Adaptive sampling recalibration (optional)
- Auto-escalation and audit logging
"""

import logging
from datetime import datetime, timedelta
from django.db import transaction, connection
from django.db.models.signals import pre_save, post_save, post_migrate
from django.dispatch import receiver
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.apps import apps
from core.services.periodic_tasks import register_minute_task

logger = logging.getLogger("heavymetal.signals")


@receiver(post_migrate, dispatch_uid="heavy_metal.register_workflow_refresh_schedule")
def register_workflow_refresh_schedule(sender, **kwargs):
    register_minute_task(sender, "heavy_metal", "heavy_metal.refresh_analysis_workflow")


# ============================================================
# ⚡ Deferred import of centralized alert engine
# ============================================================
def get_alert_engine():
    """Deferred import to avoid circular imports."""
    from alerts.engine import AlertEngine
    return AlertEngine()


# ============================================================
# ⚙️ SMART INITIALIZATION OF HEAVY METAL SCHEDULES
# ============================================================
@receiver(post_migrate)
def bootstrap_heavymetal_schedule(sender, **kwargs):
    app_label = "heavy_metal"
    if sender.name != app_label:
        return

    try:
        Schedule = apps.get_model(app_label, "HeavyMetalSamplingSchedule")
        TimeModel = apps.get_model(app_label, "HeavyMetalSamplingTime")
    except LookupError as e:
        logger.warning(f"[bootstrap] Model not found: {e}")
        return

    # Skip if table does not exist yet
    with connection.cursor() as cursor:
        tables = connection.introspection.table_names(cursor)
    if Schedule._meta.db_table not in tables:
        return

    defaults = [
        {"start_time": datetime.strptime("06:00", "%H:%M").time(), "interval_minutes": 240}
    ]
    for data in defaults:
        obj, created = Schedule.objects.get_or_create(defaults=data)
        if created:
            logger.info(f"[bootstrap] Created default HeavyMetalSamplingSchedule")

    # Generate sampling times
    for schedule in Schedule.objects.filter(enabled=True):
        start = datetime.combine(timezone.localdate(), schedule.start_time)
        end = datetime.combine(timezone.localdate(), datetime.strptime("23:59", "%H:%M").time())
        delta = timedelta(minutes=schedule.interval_minutes)
        while start <= end:
            TimeModel.objects.get_or_create(schedule=schedule, time=start.time())
            start += delta

    logger.info("[bootstrap] HeavyMetal sampling schedule initialized")


# ============================================================
# 🧾 HEAVY METAL ANALYSIS REFERENCE ID
# ============================================================
@receiver(pre_save)
def ensure_hm_reference_id(sender, instance, **kwargs):
    if sender._meta.app_label != "heavy_metal" or sender.__name__ != "HeavyMetalAnalysis":
        return

    if instance.reference_id:
        return

    Model = apps.get_model("heavy_metal", "HeavyMetalAnalysis")
    for _ in range(50):
        ref = Model.generate_reference_id()
        if not Model.objects.filter(reference_id=ref).exists():
            instance.reference_id = ref
            return
    raise ValueError("Unable to generate unique HeavyMetalAnalysis reference ID.")


# ============================================================
# 🧪 AUTO-CREATE EXPECTED PARAMETER RESULTS
# ============================================================
@receiver(post_save)
def auto_create_hm_parameters(sender, instance, created, **kwargs):
    if sender._meta.app_label != "heavy_metal" or sender.__name__ != "HeavyMetalAnalysis":
        return
    if not created:
        return

    try:
        with transaction.atomic():
            LimitModel = apps.get_model("t", "HeavyMetalLimit")
            ParameterModel = apps.get_model("heavy_metal", "HeavyMetalParameter")

            limits = LimitModel.objects.filter(treatment_plant=instance.treatment_plant)
            existing = set(ParameterModel.objects.filter(analysis=instance)
                           .values_list("parameter_id", flat=True))

            placeholders = [
                ParameterModel(
                    analysis=instance,
                    parameter=limit,
                    value=0.0,
                    manually_editable=True
                )
                for limit in limits if limit.id not in existing
            ]

            if placeholders:
                ParameterModel.objects.bulk_create(placeholders)
                engine = get_alert_engine()
                engine.trigger_alert(instance, alert_type="heavymetal_creation")

            logger.info(f"[hm] Auto-created {len(placeholders)} parameters for {instance.reference_id}")

    except Exception as e:
        logger.exception(f"[hm] Failed to auto-create parameters: {e}")


# ============================================================
# 🚨 HEAVY METAL NONCONFORMITY ALERTS
# ============================================================
@receiver(post_save)
def handle_hm_nonconformity(sender, instance, created, **kwargs):
    if sender._meta.app_label != "heavy_metal" or sender.__name__ != "HeavyMetalNonConformity":
        return
    if not created and not getattr(instance, "auto_detected", False):
        return

    engine = get_alert_engine()
    engine.trigger_alert(instance, alert_type="heavymetal_nc")
    engine.auto_escalate_unacknowledged(alert_type="heavymetal_nc", instance=instance)
    logger.info(f"[hm] Triggered NC alert for {instance.parameter_name} ({instance.analysis.reference_id})")


# ============================================================
# 🔮 ADAPTIVE SAMPLING (OPTIONAL)
# ============================================================
@receiver(post_save)
def adaptive_heavymetal_sampling(sender, instance, **kwargs):
    if sender._meta.app_label != "heavy_metal" or sender.__name__ != "HeavyMetalAnalysis":
        return
    if not getattr(instance, "anomaly_flag", False):
        return

    try:
        Schedule = apps.get_model("heavy_metal", "HeavyMetalSamplingSchedule")
        schedule = Schedule.objects.select_for_update().get(
            id=instance.sampling_schedule_id
        )

        score = max(0.0, min(1.0, getattr(instance, "ai_quality_score", 0.5)))
        delta_factor = max(0.5, 1 - score)
        old = schedule.interval_minutes
        new = max(5, int(old * delta_factor))

        schedule.interval_minutes = new
        schedule.last_auto_update = timezone.now()
        schedule.save(update_fields=["interval_minutes", "last_auto_update"])

        engine = get_alert_engine()
        engine.trigger_alert(instance, alert_type="sampling_anomaly")
        engine.auto_escalate_unacknowledged(alert_type="sampling_anomaly", instance=instance)

        logger.info(f"[adaptive] Schedule {schedule.id} adjusted: {old} → {new} min (AI={score})")

    except Exception as e:
        logger.error(f"[adaptive] Failed recalibration for analysis {instance.reference_id}: {e}")


# ============================================================
# 🔗 AUTO CONNECT SIGNALS FOR HEAVY METAL PARAMETERS
# ============================================================
PARAM_MODELS = ["HeavyMetalParameter"]

def auto_verify_hm_parameter(sender, instance, **kwargs):
    from .utils import detect_heavymetal_nonconformity
    detect_heavymetal_nonconformity(instance.parameter, instance.value, instance.analysis, instance.analysis.verified_by)

    engine = get_alert_engine()
    engine.trigger_alert(instance, alert_type="parameter_deviation")
    engine.auto_escalate_unacknowledged(alert_type="parameter_deviation", instance=instance)


for model_name in PARAM_MODELS:
    try:
        Model = apps.get_model("heavy_metal", model_name)
        post_save.connect(auto_verify_hm_parameter, sender=Model, weak=False,
                          dispatch_uid=f"hm_auto_verify_{model_name}")
        logger.info(f"[signals] Connected auto-verification handler for {model_name}")
    except LookupError:
        logger.warning(f"[signals] Missing model {model_name}; skipped connection.")
