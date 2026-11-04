from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from alerts.engine import AlertEngine
from alerts.models import AlertConfig, AlertState
from instruments.models import OnlineMeasurement, QualityFlag, TreatmentStage
from qc.models import (
    ClarifiedWaterResult,
    CoagulatedWaterResult,
    RawWaterResult,
    TreatedWaterResult,
)


ONLINE_STAGE_RESULT_MODELS = {
    TreatmentStage.RAW_WATER: RawWaterResult,
    TreatmentStage.COAGULATED: CoagulatedWaterResult,
    TreatmentStage.CLARIFIED: ClarifiedWaterResult,
    TreatmentStage.TREATED: TreatedWaterResult,
}


@receiver(post_save, sender=RawWaterResult, dispatch_uid="o.raw_water_limit_alert")
@receiver(post_save, sender=CoagulatedWaterResult, dispatch_uid="o.coagulated_water_limit_alert")
@receiver(post_save, sender=ClarifiedWaterResult, dispatch_uid="o.clarified_water_limit_alert")
@receiver(post_save, sender=TreatedWaterResult, dispatch_uid="o.treated_water_limit_alert")
def sync_water_quality_alert(sender, instance, **kwargs):
    if instance.value is None:
        return

    parameter_limit = instance.parameter
    minimum = parameter_limit.min_value
    maximum = parameter_limit.max_value
    if minimum is None and maximum is None:
        return

    value = float(instance.value)
    below = minimum is not None and value < float(minimum)
    above = maximum is not None and value > float(maximum)
    instance_ref = f"{instance._meta.label_lower}:{instance.pk}"
    active_states = AlertState.objects.filter(
        instance_ref=instance_ref,
        alert_config__alert_type=AlertConfig.AlertType.PERFORMANCE_DEVIATION,
        state__in=("queued", "sent", "escalated"),
    )

    if not below and not above:
        def close_recovered_alerts():
            for state in active_states.select_related("alert_config"):
                state.state = AlertState.StateChoices.CLOSED
                state.save(update_fields=["state", "updated_at"])

        transaction.on_commit(close_recovered_alerts)
        return

    context = {
        "is_out_of_range": True,
        "stage": sender.__name__.replace("WaterResult", " water").strip(),
        "parameter_name": parameter_limit.parameter.name,
        "observed_value": value,
        "limit_min": float(minimum) if minimum is not None else None,
        "limit_max": float(maximum) if maximum is not None else None,
        "sample_reference": instance.analysis.reference_id,
        "plant_id": instance.analysis.treatment_plant_id,
    }
    transaction.on_commit(
        lambda: AlertEngine.trigger_alert(
            instance,
            alert_type="performance_deviation",
            context_data=context,
            audit_notes="QC result outside the treatment plant's configured water-quality limits.",
        )
    )


@receiver(post_save, sender=OnlineMeasurement, dispatch_uid="o.online_water_quality_limit_alert")
def sync_online_water_quality_limit_alert(sender, instance, created, **kwargs):
    if not created or instance.quality_flag in (QualityFlag.INVALID, QualityFlag.STALE):
        return

    sensor = instance.sensor
    freshness_limit = max(sensor.instrument.expected_heartbeat_sec * 3, 180)
    measurement_age = max(0, (timezone.now() - instance.measured_at).total_seconds())
    if measurement_age > freshness_limit:
        return
    result_model = ONLINE_STAGE_RESULT_MODELS.get(sensor.stage)
    if not result_model:
        return

    parameter_limit_model = result_model._meta.get_field("parameter").remote_field.model
    parameter_limit = parameter_limit_model.objects.filter(
        treatment_plant_id=instance.treatment_plant_id,
        parameter_id=sensor.parameter_id,
    ).first()
    if not parameter_limit:
        return

    minimum = parameter_limit.min_value
    maximum = parameter_limit.max_value
    if minimum is None and maximum is None:
        return

    value = float(instance.value)
    below = minimum is not None and value < float(minimum)
    above = maximum is not None and value > float(maximum)
    instance_ref = f"instruments.sensor:{sensor.pk}"
    related_states = AlertState.objects.filter(
        instance_ref=instance_ref,
        alert_config__alert_type=AlertConfig.AlertType.WATER_QUALITY_LIMIT,
    )
    active_states = related_states.filter(
        state__in=(
            AlertState.StateChoices.QUEUED,
            AlertState.StateChoices.SENT,
            AlertState.StateChoices.ESCALATED,
            AlertState.StateChoices.ACKNOWLEDGED,
            AlertState.StateChoices.VERIFIED,
        ),
    )

    if not below and not above:
        if instance.quality_flag == QualityFlag.OK:
            def close_recovered_alerts():
                for state in active_states.select_related("alert_config"):
                    state.state = AlertState.StateChoices.CLOSED
                    state.save(update_fields=["state", "updated_at"])

            transaction.on_commit(close_recovered_alerts)
        return

    if related_states.exclude(state=AlertState.StateChoices.CLOSED).exists():
        return

    context = {
        "is_out_of_range": True,
        "stage": sensor.get_stage_display(),
        "parameter_name": parameter_limit.parameter.name,
        "observed_value": value,
        "limit_min": float(minimum) if minimum is not None else None,
        "limit_max": float(maximum) if maximum is not None else None,
        "sensor_name": sensor.name,
        "instrument_name": sensor.instrument.name,
        "quality_flag": instance.get_quality_flag_display(),
        "measured_at": instance.measured_at.isoformat(),
        "plant_id": instance.treatment_plant_id,
    }
    transaction.on_commit(
        lambda: AlertEngine.trigger_alert(
            sensor,
            alert_type=AlertConfig.AlertType.WATER_QUALITY_LIMIT,
            context_data=context,
            audit_notes="Online water-quality reading outside the treatment plant's configured stage limit.",
        )
    )
