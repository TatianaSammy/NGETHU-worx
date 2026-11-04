# alerts/signals.py
import logging
from django.apps import apps
from django.db import transaction
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from django.utils import timezone

from .engine import AlertEngine  # centralized alert system

logger = logging.getLogger(__name__)


# ============================================================
# ⚡ Utility: Store previous state before save
# ============================================================
def store_previous_state(instance):
    """
    Attach previous state to instance to detect state transitions.
    """
    model_class = type(instance)
    if instance.pk:
        try:
            instance._previous_state = model_class.objects.get(pk=instance.pk).state
        except model_class.DoesNotExist:
            instance._previous_state = None
    else:
        instance._previous_state = None


# ============================================================
# 🚨 Generic NonConformity Alert Registration
# ============================================================
def register_nc_alerts(app_label, model_name):
    """
    Dynamically register post_save signal for NonConformity-like models.
    Avoids circular imports.
    """
    model = apps.get_model(app_label, model_name)

    @receiver(post_save, sender=model)
    def _handle_nc_save(sender, instance, created, **kwargs):
        if created:
            AlertEngine.trigger_alert(instance, alert_type='nonconformity')
            AlertEngine.auto_escalate_unacknowledged(instance=instance)


# Register all NonConformity models dynamically
for app_model in [
    ('qc', 'NonConformity'),
    ('heavy_metal', 'HeavyMetalNonConformity'),
    ('microbial', 'MicrobialNonConformity'),
]:
    register_nc_alerts(*app_model)


# ============================================================
# 🔹 WaterAnalysis Auto-Transition Alerts
# ============================================================
WaterAnalysis = apps.get_model('qc', 'WaterAnalysis')


@receiver(pre_save, sender=WaterAnalysis)
def store_water_analysis_state(sender, instance, **kwargs):
    store_previous_state(instance)


@receiver(post_save, sender=WaterAnalysis)
def handle_water_analysis_save(sender, instance, created, **kwargs):
    """
    Trigger alerts when WaterAnalysis transitions to VERIFIED or CONFIRMED.
    """
    if created:
        return

    state_transitions_for_alert = ['verified', 'confirmed']
    previous = getattr(instance, "_previous_state", None)

    if previous != instance.state and instance.state.lower() in state_transitions_for_alert:
        AlertEngine.trigger_alert(instance, alert_type='auto_verification')
        AlertEngine.auto_escalate_unacknowledged(instance=instance)


# ============================================================
# 🛠️ Equipment Maintenance & Calibration Alerts
# ============================================================
Equipment = apps.get_model('inventory', 'Equipment')


@receiver(post_save, sender=Equipment)
def handle_equipment_save(sender, instance, created, **kwargs):
    """Check maintenance and calibration due dates, trigger alerts, auto-escalate."""
    check_and_trigger_equipment_alerts(instance)


def check_and_trigger_equipment_alerts(instance):
    """Trigger maintenance/calibration alerts if due."""
    today = timezone.localdate()
    alert_types = []
    context = {
        "source": "inventory_equipment",
        "plant_id": getattr(getattr(instance, "section", None), "treatment_plant_id", None),
        "instrument_name": getattr(instance, "name", ""),
        "equipment_name": getattr(instance, "name", ""),
        "serial_number": getattr(instance, "serial_number", ""),
        "category": str(getattr(instance, "category", "") or ""),
    }

    maint_date = getattr(instance, 'next_maintenance_date', None)
    if maint_date:
        if hasattr(maint_date, 'date'):
            maint_date = maint_date.date()
        if maint_date <= today:
            alert_types.append('maintenance_due')
        context["maintenance_due_date"] = maint_date.isoformat()
        context["days_to_maintenance"] = (maint_date - today).days

    calib_date = getattr(instance, 'next_calibration_date', None)
    if calib_date:
        if hasattr(calib_date, 'date'):
            calib_date = calib_date.date()
        context["calibration_due_date"] = calib_date.isoformat()
        context["days_to_calibration"] = (calib_date - today).days
        context["calibration_status"] = "EXPIRED" if calib_date < today else "DUE" if calib_date == today else "CURRENT"
        if calib_date <= today:
            alert_types.append('verification_pending')
    else:
        context.update(calibration_due_date="", days_to_calibration=None, calibration_status="MISSING")

    for alert_type in alert_types:
        AlertEngine.trigger_alert(instance, alert_type=alert_type, context_data=context)
        AlertEngine.auto_escalate_unacknowledged(alert_type=alert_type, instance=instance)


# ============================================================
# ⚡ Generic Helper for Manual Alert Trigger
# ============================================================
def dynamic_alert_trigger(instance, alert_type):
    """
    Centralized alert helper:
      - AI-based priority prediction
      - Multi-channel dispatch (email, push, voice)
      - Auto-escalation if unacknowledged
    """
    try:
        AlertEngine.trigger_alert(instance, alert_type=alert_type)
        AlertEngine.auto_escalate_unacknowledged(instance)
        logger.info(f"✅ Alert triggered for {alert_type} on {instance}")
    except Exception as e:
        logger.exception(f"❌ Failed to trigger alert for {alert_type} on {instance}: {e}")


# ============================================================
# 🎙️ UNIVERSAL VOICE MESSAGE GENERATION FOR ALL ALERTS & NOTIFICATIONS
# ============================================================

AlertState = apps.get_model('alerts', 'AlertState')


@receiver(post_save, sender=AlertState)
def handle_alert_state_voice(sender, instance, created, **kwargs):
    """Ensure every AlertState record has an asynchronously generated persistent VoiceAlert."""
    if created:
        try:
            from alerts.tasks import generate_voice_notification
            cfg = instance.alert_config
            body = instance.message or f"Alert state {instance.state} for {instance.instance_ref}"
            title = instance.title or (cfg.alert_type if cfg else "System Alert Notification")
            state_id = instance.pk
            cfg_id = cfg.pk if cfg else None
            transaction.on_commit(lambda: generate_voice_notification.delay(
                message=body,
                alert_config_id=cfg_id,
                title=title,
                alert_state_id=state_id,
                source_key=f"alert-state:{state_id}",
            ))
        except Exception as e:
            logger.warning(f"[VoiceAlert] AlertState signal voice generation notice: {e}")


try:
    ChemicalWorkflowAlert = apps.get_model('ca', 'ChemicalWorkflowAlert')

    @receiver(post_save, sender=ChemicalWorkflowAlert)
    def handle_ca_workflow_alert_voice(sender, instance, created, **kwargs):
        """Ensure every CA workflow alert generates a corresponding voice message asynchronously."""
        if created:
            try:
                from alerts.tasks import generate_voice_notification
                from alerts.models import AlertConfig

                voice_text = f"{instance.title}. {instance.message}"
                v_title = instance.title
                source_key = f"ca-workflow:{instance.pk}"
                recipient_ids = [instance.recipient_id] if getattr(instance, "recipient_id", None) else []
                config_id = AlertConfig.objects.filter(
                    alert_type=instance.alert_type,
                    active=True,
                ).values_list("pk", flat=True).first()
                batch = getattr(instance, "batch", None)
                if batch is None and instance.analysis_request_id:
                    batch = instance.analysis_request.batch
                plant_id = getattr(batch, "treatment_plant_id", None)
                transaction.on_commit(lambda: generate_voice_notification.delay(
                    message=voice_text,
                    title=v_title,
                    source_key=source_key,
                    alert_config_id=config_id,
                    recipient_ids=recipient_ids,
                    plant_id=plant_id,
                ))
            except Exception as e:
                logger.warning(f"[VoiceAlert] CA workflow alert signal voice generation notice: {e}")
except Exception:
    pass
