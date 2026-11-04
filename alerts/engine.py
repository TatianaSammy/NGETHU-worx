"""Central alert creation, durable delivery, and escalation scheduling."""

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import AlertConfig, AlertLog, AlertNotification, AlertState, AlarmAcknowledgment
from .utils import (
    AIAlertPriorityPredictor,
    GeoContextAnalyzer,
    InstanceSerializer,
    SafeConditionEvaluator,
)

logger = logging.getLogger(__name__)

_OPEN_STATES = (
    AlertState.StateChoices.QUEUED,
    AlertState.StateChoices.SENT,
    AlertState.StateChoices.ESCALATED,
)


def _render(template, values, fallback):
    if not template:
        return str(fallback)
    try:
        return template.format_map(_MissingValueMap(values))
    except (ValueError, AttributeError, TypeError):
        return str(template)


class _MissingValueMap(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _instance_reference(instance):
    meta = getattr(instance, "_meta", None)
    pk = getattr(instance, "pk", None)
    if meta and pk is not None:
        return f"{meta.label_lower}:{pk}"
    return str(instance)[:255]


def _plant_id(instance):
    for candidate in (
        getattr(instance, "treatment_plant_id", None),
        getattr(getattr(instance, "section", None), "treatment_plant_id", None),
        getattr(getattr(instance, "analysis", None), "treatment_plant_id", None),
        getattr(getattr(instance, "batch", None), "treatment_plant_id", None),
        getattr(getattr(instance, "treatment_plant", None), "pk", None),
    ):
        if candidate:
            return candidate
    return None


def _target_users(config, plant_id):
    """Resolve direct, AI, section, group, and plant audiences into user IDs."""
    from accounts.utils import get_contacts_for_alert
    from accounts.models import CustomUser

    user_ids = list(config.recipients.values_list("pk", flat=True))
    ai_ids = list(config.ai_recommended_recipients or []) if config.allow_ai_recipients else []
    section_codes = list(config.target_sections.values_list("code", flat=True))
    group_ids = list(config.target_groups.values_list("pk", flat=True))
    plant_ids = list(config.target_treatment_plants.values_list("pk", flat=True))

    # Do not let an empty audience configuration resolve to every account.
    if not any((user_ids, ai_ids, section_codes, group_ids, plant_ids, plant_id)):
        return []

    # An inferred plant narrows the configured audience when the rule itself
    # has no explicit plant target.
    if plant_id and not plant_ids:
        plant_ids = [plant_id]
    elif plant_id and str(plant_id) not in {str(value) for value in plant_ids}:
        # A rule explicitly scoped to other plants must not fire for this one.
        return []

    contacts = get_contacts_for_alert(
        user_ids=user_ids,
        ai_recommended_user_ids=ai_ids,
        sections=section_codes or None,
        groups=group_ids or None,
        treatment_plants=plant_ids or None,
    )
    candidate_ids = {int(contact["id"]) for contact in contacts if contact.get("id")}
    if plant_id and candidate_ids:
        # Direct-user/group/section matches still honor the event's plant scope.
        explicitly_named = set(user_ids + ai_ids)
        in_scope = CustomUser.objects.filter(pk__in=candidate_ids).filter(
            Q(assigned_treatment_plant_id=plant_id)
            | Q(pk__in=explicitly_named)
            | Q(is_superuser=True)
        )
        candidate_ids = set(in_scope.values_list("pk", flat=True))
    return sorted(candidate_ids)


def _channels_for_user(user):
    preference = getattr(user, "alert_preferences", None)
    channels = []
    if not preference or preference.receive_push:
        channels.append("push")
    if (not preference or preference.receive_email) and user.email:
        channels.append("email")
    mobile = getattr(user, "mobile_number", None) or getattr(user, "phone_number", None)
    if (not preference or preference.receive_sms) and mobile:
        channels.append("sms")
    whatsapp = getattr(user, "whatsapp_number", None)
    if (not preference or preference.receive_whatsapp) and whatsapp:
        channels.append("whatsapp")
    if (not preference or preference.receive_slack) and getattr(user, "slack_channel", None):
        channels.append("slack")
    return channels


class AlertEngine:
    """Create each event once, persist its recipients, then deliver asynchronously."""

    @staticmethod
    def trigger_alert(
        instance,
        alert_types=None,
        alert_type=None,
        audit_notes=None,
        alert_config_id=None,
        context_data=None,
    ):
        if alert_type and not alert_types:
            alert_types = [alert_type]

        serialized = InstanceSerializer.serialize(instance)
        if isinstance(serialized, dict) and isinstance(context_data, dict):
            serialized.update(context_data)
        plant_id = _plant_id(instance)
        geo_context = GeoContextAnalyzer.detect(serialized) or {}
        if plant_id:
            geo_context["plant_id"] = str(plant_id)

        configs = AlertConfig.objects.filter(active=True).select_related("template")
        if alert_config_id:
            configs = configs.filter(pk=alert_config_id)
        if alert_types:
            configs = configs.filter(alert_type__in=alert_types)

        reference = _instance_reference(instance)
        legacy_references = {reference, str(instance)[:255]}
        if getattr(instance, "pk", None) is not None:
            legacy_references.add(str(instance.pk)[:255])
        triggered = []
        for config in configs:
            try:
                if not SafeConditionEvaluator.evaluate(config.filter_conditions, serialized):
                    continue

                predicted_priority = AIAlertPriorityPredictor.predict(serialized)
                valid_priorities = {choice for choice, _label in AlertConfig.PriorityLevel.choices}
                if predicted_priority in valid_priorities and predicted_priority != config.priority:
                    config.priority = predicted_priority
                    config.save(update_fields=["priority", "updated_at"])

                title = _render(
                    config.template.subject_template if config.template_id else "",
                    serialized,
                    f"Alert: {config.alert_type}",
                )
                message = _render(
                    config.template.body_template if config.template_id else "",
                    serialized,
                    str(instance),
                )
                now = timezone.now()
                escalation_delay = config.tat_for_escalation or timedelta(0)
                recipient_ids = _target_users(config, plant_id)

                with transaction.atomic():
                    state = (
                        AlertState.objects.select_for_update()
                        .filter(alert_config=config, instance_ref__in=legacy_references, state__in=_OPEN_STATES)
                        .order_by("-created_at")
                        .first()
                    )
                    if state:
                        # Signals often observe the same save through multiple
                        # code paths. Reuse the open event instead of spamming.
                        changed_fields = []
                        if not state.title:
                            state.title = title[:255]
                            changed_fields.append("title")
                        if not state.message:
                            state.message = message
                            changed_fields.append("message")
                        if changed_fields:
                            changed_fields.append("updated_at")
                            state.save(update_fields=changed_fields)
                        triggered.append(state)
                        continue

                    try:
                        with transaction.atomic():
                            state = AlertState.objects.create(
                                alert_config=config,
                                instance_ref=reference,
                                title=title[:255],
                                message=message,
                                priority=config.priority,
                                state=AlertState.StateChoices.QUEUED,
                                geo_context=geo_context,
                                escalation_due_at=now + escalation_delay,
                            )
                    except IntegrityError:
                        # A concurrent signal may have committed the same
                        # active event after our initial lookup.
                        state = AlertState.objects.filter(
                            alert_config=config,
                            instance_ref__in=legacy_references,
                            state__in=_OPEN_STATES,
                        ).order_by("-created_at").first()
                        if not state:
                            raise
                        triggered.append(state)
                        continue

                    if recipient_ids:
                        from accounts.models import CustomUser

                        users = CustomUser.objects.filter(
                            pk__in=recipient_ids, is_active=True, is_locked=False
                        ).select_related("assigned_treatment_plant", "alert_preferences")
                        notifications = []
                        for user in users:
                            channels = _channels_for_user(user)
                            notifications.append(AlertNotification(
                                alert_state=state,
                                recipient=user,
                                title=title[:255],
                                message=message,
                                priority=config.priority,
                                channels=channels,
                                delivery_status=(
                                    AlertNotification.DeliveryStatus.PENDING
                                    if channels
                                    else AlertNotification.DeliveryStatus.STORED
                                ),
                                next_attempt_at=now,
                            ))
                        AlertNotification.objects.bulk_create(notifications)
                        if (
                            config.template_id
                            and config.template.active
                            and config.template.auto_voice_enabled
                            and config.voice_persistence_mode != AlertConfig.VoicePersistenceMode.ONCE
                            and any("push" in (notification.channels or []) for notification in notifications)
                        ):
                            state.repeat_due_at = now + timedelta(
                                seconds=max(5, int(config.voice_repeat_interval_seconds or 60))
                            )
                            state.save(update_fields=["repeat_due_at", "updated_at"])

                    try:
                        from django.contrib.contenttypes.models import ContentType

                        if getattr(instance, "pk", None) is not None and getattr(instance, "_meta", None):
                            AlertLog.objects.create(
                                alert=config,
                                content_type=ContentType.objects.get_for_model(instance),
                                object_id=instance.pk,
                                details={
                                    "state_id": state.pk,
                                    "title": title,
                                    "message": message,
                                    "priority": config.priority,
                                    "recipients": recipient_ids,
                                    "notes": audit_notes or "",
                                },
                            )
                    except Exception:
                        logger.exception("Could not write alert audit log for state %s", state.pk)

                    config.last_triggered = now
                    config.save(update_fields=["last_triggered", "updated_at"])
                    notification_ids = list(state.notifications.values_list("pk", flat=True))
                    if notification_ids:
                        transaction.on_commit(
                            lambda ids=notification_ids: AlertEngine._queue_delivery(ids)
                        )
                    triggered.append(state)

            except Exception:
                logger.exception("Failed creating alert %s for %s", config.alert_type, reference)

        return triggered

    @staticmethod
    def _queue_delivery(notification_ids):
        try:
            from .tasks import deliver_alert_notification
            from .models import AlertNotification

            for notification_id in notification_ids:
                deliver_alert_notification.delay(notification_id)
        except Exception:
            logger.exception("Could not queue alert notification delivery")

    @staticmethod
    def auto_escalate_unacknowledged(alert_type=None, instance=None):
        """Backfill missing escalation deadlines without sending early alerts."""
        reference = _instance_reference(instance) if instance is not None else None
        states = AlertState.objects.filter(state__in=_OPEN_STATES, escalation_due_at__isnull=True)
        if alert_type:
            states = states.filter(alert_config__alert_type=alert_type)
        if reference:
            states = states.filter(instance_ref=reference)

        now = timezone.now()
        for state in states.select_related("alert_config").iterator():
            delay = (
                state.alert_config.escalation_timeout
                if state.state == AlertState.StateChoices.ESCALATED
                else state.alert_config.tat_for_escalation
            ) or timedelta(0)
            AlertState.objects.filter(pk=state.pk, escalation_due_at__isnull=True).update(
                escalation_due_at=state.created_at + delay,
            )

    @staticmethod
    def trigger_for_sensor_measurement(measurement):
        return AlertEngine.trigger_alert(measurement, alert_types=["sensor_threshold", "sensor_health"])

    @staticmethod
    def trigger_for_derived_metric(metric):
        return AlertEngine.trigger_alert(metric, alert_types=["derived_metric", "phase_anomaly"])

    @staticmethod
    def trigger_for_nonconformity(nonconformity):
        return AlertEngine.trigger_alert(nonconformity, alert_types=["nc_detected", "nc_escalation"])

    @staticmethod
    def acknowledge_alert(user, alert_state_id, notes=None):
        """
        Atomically acknowledges an alert event across AlertState, AlertNotification,
        and AlarmAcknowledgment, stopping active repeats and notifying connected clients.
        """
        from django.contrib.contenttypes.models import ContentType
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync

        now = timezone.now()
        with transaction.atomic():
            state = (
                AlertState.objects.select_for_update()
                .select_related("alert_config")
                .filter(pk=alert_state_id)
                .first()
            )
            if not state:
                return {"success": False, "error": "Alert state not found."}

            # Scope / authorization check
            is_superuser = getattr(user, "is_superuser", False)
            user_plant_id = getattr(user, "assigned_treatment_plant_id", None)
            plant_id = (state.geo_context or {}).get("plant_id")
            if not is_superuser and plant_id and user_plant_id and str(plant_id) != str(user_plant_id):
                return {"success": False, "error": "Alert is outside assigned plant scope."}

            state.state = AlertState.StateChoices.ACKNOWLEDGED
            state.escalation_due_at = None
            if hasattr(state, "repeat_due_at"):
                state.repeat_due_at = None
            state.save(update_fields=["state", "escalation_due_at", "repeat_due_at", "updated_at"])

            # Acknowledge recipient notifications
            notifications = AlertNotification.objects.filter(
                alert_state=state,
                acknowledged_at__isnull=True,
            )
            recipient_ids = list(notifications.values_list("recipient_id", flat=True))
            notifications.update(
                acknowledged_at=now,
                delivery_status=AlertNotification.DeliveryStatus.STORED,
            )

            # Record durable acknowledgment
            state_ct = ContentType.objects.get_for_model(AlertState)
            AlarmAcknowledgment.objects.update_or_create(
                user=user,
                content_type=state_ct,
                object_id=state.pk,
                defaults={
                    "acknowledged": True,
                    "acknowledged_at": now,
                },
            )

            # Audit log
            try:
                AlertLog.objects.create(
                    alert=state.alert_config,
                    content_type=state_ct,
                    object_id=state.pk,
                    details={
                        "action": "acknowledged",
                        "acknowledged_by": getattr(user, "username", str(user)),
                        "notes": notes or "",
                        "timestamp": now.isoformat(),
                    },
                )
            except Exception:
                logger.exception("Could not record alert acknowledgment audit log for state %s", state.pk)

        # Broadcast acknowledgment to close UI alarm & silence repeat audio
        try:
            channel_layer = get_channel_layer()
            if channel_layer:
                target_user_ids = set(recipient_ids)
                if user.pk:
                    target_user_ids.add(user.pk)
                ack_payload = {
                    "type": "alert.acknowledged",
                    "id": f"state_{state.pk}",
                    "alert_state_id": state.pk,
                    "timestamp": now.isoformat(),
                }
                for uid in target_user_ids:
                    async_to_sync(channel_layer.group_send)(f"user_{uid}_alerts", ack_payload)
        except Exception:
            logger.warning("Could not broadcast alert acknowledgment WebSocket event for state %s", state.pk)

        return {
            "success": True,
            "state_id": state.pk,
            "message": "Alert acknowledged successfully.",
        }

    @staticmethod
    def escalate_state(alert_state_id, force=False):
        """Immediately trigger or schedule escalation for a state."""
        from .tasks import escalate_alert

        state = AlertState.objects.filter(pk=alert_state_id).first()
        if not state or state.state not in _OPEN_STATES:
            return "closed"

        if force:
            state.escalation_due_at = timezone.now()
            state.save(update_fields=["escalation_due_at", "updated_at"])

        return escalate_alert.delay(state.pk)
