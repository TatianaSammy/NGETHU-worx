"""
Microbial/Water Analysis Signals
- Auto-initialize sampling schedules and times
- Generate unique WaterAnalysis reference IDs
- Auto-create expected results
- Detect nonconformities & trigger centralized alerts
- Adaptive sampling recalibration
- Audit logging for all critical actions
"""

import logging
from datetime import datetime, timedelta
from django.db import transaction
from django.db.models.signals import pre_save, post_save, post_migrate
from django.dispatch import receiver
from django.utils import timezone
from django.apps import apps

logger = logging.getLogger("microbial.signals")


# ============================================================
# ⚡ Centralized Alert Engine (deferred import to avoid circular dependencies)
# ============================================================
def get_alert_engine():
    from alerts.engine import AlertEngine
    return AlertEngine()


# ============================================================
# 🧭 SAMPLING SCHEDULE INITIALIZATION
# ============================================================
@receiver(post_migrate)
def bootstrap_sampling_schedules(sender, **kwargs):
    app_label = __name__.rsplit('.', 1)[0]
    if sender.name != app_label:
        return

    ScheduleModel = apps.get_model(sender.name, 'SamplingSchedule')
    if not ScheduleModel.objects.exists():
        ScheduleModel.objects.bulk_create([
            ScheduleModel(water_type="raw", start_time=datetime.strptime("00:00", "%H:%M").time(), interval_minutes=30),
            ScheduleModel(water_type="treated", start_time=datetime.strptime("00:00", "%H:%M").time(), interval_minutes=30),
            ScheduleModel(water_type="coagulated", start_time=datetime.strptime("02:00", "%H:%M").time(), interval_minutes=120),
            ScheduleModel(water_type="clarified", start_time=datetime.strptime("00:30", "%H:%M").time(), interval_minutes=120),
        ])
        logger.info("[microbial] Default sampling schedules created")

    for schedule in ScheduleModel.objects.filter(enabled=True):
        schedule.generate_times()


# ============================================================
# ⚙️ WATER ANALYSIS REFERENCE ID
# ============================================================
@receiver(pre_save, sender=apps.get_model("qc", "WaterAnalysis"))
def ensure_water_analysis_ref(sender, instance, **kwargs):
    if instance.reference_id:
        return

    for _ in range(50):
        ref = ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=8))
        if not sender.objects.filter(reference_id=ref).exists():
            instance.reference_id = ref
            return
    raise ValueError("Unable to generate unique WaterAnalysis reference ID.")


# ============================================================
# 🧪 AUTO-CREATE EXPECTED RESULTS
# ============================================================
@receiver(post_save, sender=apps.get_model("qc", "WaterAnalysis"))
def auto_create_water_results(sender, instance, created, **kwargs):
    if not created:
        return

    try:
        with transaction.atomic():
            from .utils.instrument_integration import import_instrument_readings, WATER_TYPE_RESULT_MODEL

            # 1️⃣ Import live readings
            import_instrument_readings(instance)

            # 2️⃣ Create placeholders for missing parameters
            water_type = instance.analysis_time.water_type
            ResultModel = WATER_TYPE_RESULT_MODEL.get(water_type)
            if not ResultModel:
                return

            limit_model_map = {
                "raw": apps.get_model("t", "RawWaterLimit"),
                "treated": apps.get_model("t", "TreatedWaterLimit"),
                "coagulated": apps.get_model("t", "CoagulatedWaterLimit"),
                "clarified": apps.get_model("t", "ClarifiedWaterLimit"),
            }
            LimitModel = limit_model_map.get(water_type)
            limits = LimitModel.objects.filter(treatment_plant=instance.treatment_plant)

            existing = set(ResultModel.objects.filter(analysis=instance).values_list("parameter_id", flat=True))
            placeholders = [
                ResultModel(analysis=instance, parameter=limit, value=0.0, manually_editable=True)
                for limit in limits if limit.id not in existing
            ]

            if placeholders:
                ResultModel.objects.bulk_create(placeholders)
                engine = get_alert_engine()
                engine.trigger_alert(instance, alert_type="water_analysis_creation")

            logger.info(f"[microbial] Auto-created {len(placeholders)} results for {instance.reference_id}")

            # 3️⃣ Log creation
            WaterAnalysisLog = apps.get_model("qc", "WaterAnalysisLog")
            WaterAnalysisLog.objects.create(
                analysis=instance,
                actor=instance.analysed_by,
                action="created",
                remarks="New WaterAnalysis created",
                auto_generated=True
            )

    except Exception as e:
        logger.exception(f"[microbial] Failed to auto-create results: {e}")


# ============================================================
# 🚨 NONCONFORMITY ALERTS
# ============================================================
@receiver(post_save, sender=apps.get_model("qc", "NonConformity"))
def handle_water_nc(sender, instance, created, **kwargs):
    if not created and not getattr(instance, "auto_detected", False):
        return

    engine = get_alert_engine()
    engine.trigger_alert(instance, alert_type="water_nc")
    engine.auto_escalate_unacknowledged(alert_type="water_nc", instance=instance)
    logger.info(f"[microbial] Triggered alert for NC {instance.parameter_name} on analysis {instance.analysis.reference_id}")


# ============================================================
# 🔬 AUTO-DETECTION FOR WATER RESULTS
# ============================================================
RESULT_MODELS = [
    "TreatedWaterResult",
    "RawWaterResult",
    "CoagulatedWaterResult",
    "ClarifiedWaterResult",
]

def auto_detect_nc(sender, instance, **kwargs):
    from .utils.instrument_integration import WATER_TYPE_RESULT_MODEL
    from .models import detect_nonconformity

    detect_nonconformity(instance.parameter, instance.value, instance.analysis, instance.analysis.verified_by)

    engine = get_alert_engine()
    engine.trigger_alert(instance, alert_type="parameter_deviation")
    engine.auto_escalate_unacknowledged(alert_type="parameter_deviation", instance=instance)

for model_name in RESULT_MODELS:
    try:
        Model = apps.get_model("qc", model_name)
        post_save.connect(auto_detect_nc, sender=Model, weak=False, dispatch_uid=f"water_auto_detect_{model_name}")
        logger.info(f"[signals] Connected auto-detection for {model_name}")
    except LookupError:
        logger.warning(f"[signals] Missing model {model_name}; skipped connection")
