# alerts/tasks.py
import logging
from datetime import date, datetime, timedelta
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.core.mail import send_mail
from celery import shared_task
from celery.utils.log import get_task_logger
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.apps import apps
import asyncio

from .utils import (
    SMSSender,
    WhatsAppSender,
    SlackNotifier,
    SafeConditionEvaluator,
)

logger = get_task_logger(__name__)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]

# =========================================
# 🔹 Helper: Filter recipients by preferences & scope
# =========================================
def filter_recipients(recipients, treatment_plant=None, group=None, section=None):
    """
    Filter recipient scope before dispatch. Per-channel preferences are applied
    during delivery so an email opt-out does not suppress inbox or push alerts.
    """
    filtered = []
    for user in recipients:
        # Scope checks
        target_plant_id = getattr(treatment_plant, "pk", treatment_plant)
        user_plant_id = getattr(user, "assigned_treatment_plant_id", None)
        if target_plant_id and str(user_plant_id) != str(target_plant_id) and not user.is_superuser:
            continue
        if group and not user.groups.filter(pk=getattr(group, "pk", group)).exists():
            continue
        if section and str(getattr(user, "section_id", "")) != str(getattr(section, "pk", section)):
            continue
        # Preferences are applied per channel at delivery time. An email opt
        # out must not suppress the persistent inbox or a permitted push.
        filtered.append(user)
    return filtered

# =========================================
# 🔹 Async multi-channel alert dispatcher
# =========================================
async def _dispatch_alerts_async(subject, body, filtered_recipients, context=None, voice_url=None):
    """
    Asynchronously dispatch alerts across multiple channels.
    Recipients must already be pre-filtered (synchronously) before calling this.
    """
    channel_layer = get_channel_layer()
    tasks = []

    for user in filtered_recipients:
        # Optional safe condition check (pure in-memory, no ORM)
        if context and not SafeConditionEvaluator.evaluate(user, context):
            continue

        # Email
        preference = getattr(user, "alert_preferences", None)
        if getattr(user, "email", None) and (not preference or preference.receive_email):
            tasks.append(asyncio.to_thread(send_mail, subject, body, settings.DEFAULT_FROM_EMAIL, [user.email]))

        # SMS
        mobile = getattr(user, "mobile_number", None) or getattr(user, "phone_number", None)
        if mobile and (not preference or preference.receive_sms):
            tasks.append(SMSSender.send_sms(mobile, body))

        # WhatsApp
        if getattr(user, "whatsapp_number", None) and (not preference or preference.receive_whatsapp):
            tasks.append(WhatsAppSender.send_message(user.whatsapp_number, body))

        # Slack
        if getattr(user, "slack_channel", None) and (not preference or preference.receive_slack):
            tasks.append(SlackNotifier.send_message(user.slack_channel, body))

        # Push/WebSocket — use native async group_send
        if channel_layer and (not preference or preference.receive_push):
            tasks.append(channel_layer.group_send(
                f"user_{user.pk}_alerts",
                {
                    "type": "alert.message",
                    "title": subject,
                    "message": body,
                    "voice_url": voice_url,
                }
            ))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            logger.error(f"Alert dispatch exception: {r}")



# =========================================
# 🔹 WebSocket Alerts
# =========================================
@shared_task
def send_websocket_notification(
    message,
    group=None,
    title="System Notification",
    voice_url=None,
    user_ids=None,
    priority="medium",
    alert_type="general",
    instance_ref=None,
    notification_id=None,
    auto_voice=True,
    repeat_voice=False,
    voice_profile=None,
):
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        payload = {
            "type": "alert.message",
            "title": title,
            "message": message,
            "voice_url": voice_url,
            "voice_profile": voice_profile,
            "priority": priority,
            "alert_type": alert_type,
            "instance_ref": instance_ref,
            "id": notification_id,
            "auto_voice": auto_voice,
            "repeat_voice": repeat_voice,
            "timestamp": timezone.now().isoformat(),
        }
        groups = {f"user_{user_id}_alerts" for user_id in (user_ids or [])}
        if not groups and group and group.startswith("user_"):
            groups.add(group)
        for target_group in groups:
            async_to_sync(channel_layer.group_send)(target_group, payload)
        logger.info("WebSocket alert sent to %s private target(s)", len(groups))
    except Exception as e:
        logger.error(f"Failed to send WebSocket alert: {e}")


# =========================================
# 🔹 Celery wrapper for async dispatcher
# =========================================
@shared_task
def dispatch_alerts(subject=None, body=None, recipients=None,
                    treatment_plant=None, group=None, section=None, context=None):
    """
    Celery entrypoint for dispatching alerts.
    Compatibility entry point for older callers. New alert events use the
    persisted AlertNotification outbox below.
    """
    if not subject or not body or not recipients:
        logger.warning("dispatch_alerts called without required arguments: "
                       f"subject={subject}, body={body}, recipients={recipients}")
        return

    try:
        # Resolve model objects, IDs, or contact dictionaries in the worker;
        # Celery's JSON serializer cannot persist ORM instances.
        from accounts.models import CustomUser
        recipient_ids = []
        for recipient in recipients:
            recipient_id = recipient.get("id") if isinstance(recipient, dict) else getattr(recipient, "pk", recipient)
            if recipient_id:
                recipient_ids.append(recipient_id)
        users = (
            CustomUser.objects
            .filter(pk__in=recipient_ids, is_active=True, is_locked=False)
            .select_related("alert_preferences")
        )
        filtered = filter_recipients(users, treatment_plant, group, section)
        if not filtered:
            logger.info("dispatch_alerts: no recipients passed filter; skipping.")
            return

        # Run legacy callers without creating duplicate voice records.
        asyncio.run(
            _dispatch_alerts_async(subject, body, filtered, context, voice_url=None)
        )
        logger.info(f"Dispatched alerts to {len(filtered)} recipients "
                    f"(plant={treatment_plant}, group={group}, section={section})")
    except Exception as e:
        logger.exception(f"Failed to dispatch alerts: {e}")


# =========================================
# Durable alert outbox, retries, and escalation
# =========================================
@shared_task
def deliver_alert_notification(notification_id):
    """Deliver one persisted inbox event, resuming only failed channels."""
    from alerts.models import AlertNotification, AlertState, VoiceAlert

    now = timezone.now()
    with transaction.atomic():
        notification = (
            AlertNotification.objects.select_for_update()
            .select_related(
                "recipient", "recipient__alert_preferences", "alert_state",
                "alert_state__alert_config", "alert_state__alert_config__template",
            )
            .filter(pk=notification_id)
            .first()
        )
        if not notification or notification.delivery_status in {
            AlertNotification.DeliveryStatus.SENT,
            AlertNotification.DeliveryStatus.STORED,
            AlertNotification.DeliveryStatus.FAILED,
        }:
            return
        if notification.next_attempt_at and notification.next_attempt_at > now:
            return
        notification.attempts += 1
        notification.last_attempt_at = now
        # Claim the row before making network calls so duplicate queue entries
        # cannot send the same email/SMS concurrently.
        notification.next_attempt_at = now + timedelta(minutes=10)
        notification.save(update_fields=["attempts", "last_attempt_at", "next_attempt_at", "updated_at"])
    results = dict(notification.delivery_results or {})
    user = notification.recipient
    state = notification.alert_state
    if state.state in {
        AlertState.StateChoices.ACKNOWLEDGED,
        AlertState.StateChoices.VERIFIED,
        AlertState.StateChoices.CLOSED,
    }:
        notification.delivery_status = AlertNotification.DeliveryStatus.STORED
        notification.next_attempt_at = None
        notification.save(update_fields=["delivery_status", "next_attempt_at", "updated_at"])
        return notification.delivery_status
    voice_alert = VoiceAlert.objects.filter(alert_state=state).order_by("-created_at").first()
    voice_url = voice_alert.audio_file.url if voice_alert and voice_alert.profile_audio_ready else None
    voice_profile = voice_alert.voice_over.name if voice_url else ""
    channel_layer = get_channel_layer()

    for channel in notification.channels or []:
        if results.get(channel) in {"sent", "skipped"}:
            continue
        preference = getattr(user, "alert_preferences", None)
        preference_field = {
            "push": "receive_push",
            "email": "receive_email",
            "sms": "receive_sms",
            "whatsapp": "receive_whatsapp",
            "slack": "receive_slack",
        }.get(channel)
        if preference and preference_field and not getattr(preference, preference_field):
            results[channel] = "skipped"
            notification.delivery_results = results
            notification.save(update_fields=["delivery_results", "updated_at"])
            continue
        try:
            if channel == "email":
                if not user.email:
                    results[channel] = "skipped"
                elif send_mail(notification.title, notification.message, settings.DEFAULT_FROM_EMAIL, [user.email]):
                    results[channel] = "sent"
                else:
                    raise RuntimeError("Email backend did not accept the message.")
            elif channel == "sms":
                mobile = getattr(user, "mobile_number", None) or getattr(user, "phone_number", None)
                if not mobile or not SMSSender.SMS_GATEWAY_URL:
                    results[channel] = "skipped"
                else:
                    asyncio.run(SMSSender.send_sms(mobile, notification.message))
                    results[channel] = "sent"
            elif channel == "whatsapp":
                number = getattr(user, "whatsapp_number", None)
                if not number or not WhatsAppSender.SENDER_NUMBER:
                    results[channel] = "skipped"
                else:
                    asyncio.run(WhatsAppSender.send_message(number, notification.message))
                    results[channel] = "sent"
            elif channel == "slack":
                slack_channel = getattr(user, "slack_channel", None)
                if not slack_channel:
                    results[channel] = "skipped"
                else:
                    asyncio.run(SlackNotifier.send_message(slack_channel, notification.message))
                    results[channel] = "sent"
            elif channel == "push":
                if not channel_layer:
                    # Push is a best-effort live hint; the database inbox is
                    # authoritative and remains available on the next refresh.
                    results[channel] = "skipped"
                else:
                    async_to_sync(channel_layer.group_send)(
                        f"user_{user.pk}_alerts",
                        {
                            "type": "alert.message",
                            "id": f"state_{state.pk}",
                            "title": notification.title,
                            "message": notification.message,
                            "priority": notification.priority,
                            "alert_type": state.alert_config.alert_type,
                            "instance_ref": state.instance_ref,
                            "voice_url": voice_url,
                            "voice_profile": voice_profile,
                            "auto_voice": bool(
                                state.alert_config.active
                                and state.alert_config.template.active
                                and state.alert_config.template.auto_voice_enabled
                            ),
                            "timestamp": notification.created_at.isoformat(),
                        },
                    )
                    results[channel] = "sent"
            else:
                results[channel] = "skipped"
        except Exception as exc:
            results[channel] = "failed"
            notification.last_error = f"{channel}: {exc}"[:2000]
            logger.warning(
                "Alert notification %s channel %s failed: %s",
                notification.pk,
                channel,
                exc,
            )
        notification.delivery_results = results
        notification.save(update_fields=["delivery_results", "last_error", "updated_at"])

    failed_channels = [channel for channel in notification.channels if results.get(channel) == "failed"]
    if failed_channels:
        if notification.attempts >= 6:
            notification.delivery_status = AlertNotification.DeliveryStatus.FAILED
            notification.next_attempt_at = None
        else:
            notification.delivery_status = (
                AlertNotification.DeliveryStatus.PARTIAL
                if any(result == "sent" for result in results.values())
                else AlertNotification.DeliveryStatus.PENDING
            )
            notification.next_attempt_at = now + timedelta(seconds=min(30 * (2 ** (notification.attempts - 1)), 1800))
    else:
        notification.delivery_status = (
            AlertNotification.DeliveryStatus.SENT
            if any(result == "sent" for result in results.values())
            else AlertNotification.DeliveryStatus.STORED
        )
        notification.delivered_at = now
        notification.next_attempt_at = None
        notification.last_error = ""
        if not notification.is_escalation and state.state == AlertState.StateChoices.QUEUED:
            state.state = AlertState.StateChoices.SENT
            state.save(update_fields=["state", "updated_at"])

    notification.save(update_fields=[
        "delivery_status", "delivered_at", "next_attempt_at", "last_error", "updated_at",
    ])
    return notification.delivery_status


@shared_task
def deliver_due_alert_notifications(batch_size=200):
    """Recover pending/partial deliveries if a worker or broker restarted."""
    from alerts.models import AlertNotification

    now = timezone.now()
    ids = list(
        AlertNotification.objects.filter(
            delivery_status__in=(AlertNotification.DeliveryStatus.PENDING, AlertNotification.DeliveryStatus.PARTIAL),
            next_attempt_at__lte=now,
        ).order_by("next_attempt_at", "pk").values_list("pk", flat=True)[:batch_size]
    )
    for notification_id in ids:
        deliver_alert_notification.delay(notification_id)
    return len(ids)


@shared_task
def process_due_alert_escalations(batch_size=100):
    """Queue due alert events; the handler rechecks state under a row lock."""
    from alerts.models import AlertState

    now = timezone.now()
    ids = list(
        AlertState.objects.filter(
            state__in=(AlertState.StateChoices.QUEUED, AlertState.StateChoices.SENT, AlertState.StateChoices.ESCALATED),
            escalation_due_at__lte=now,
        ).order_by("escalation_due_at", "pk").values_list("pk", flat=True)[:batch_size]
    )
    for state_id in ids:
        escalate_alert.delay(state_id)
    return len(ids)


@shared_task
def process_due_alert_repeats(batch_size=100):
    """Queue due voice reminders; each worker rechecks its persisted lifecycle."""
    from alerts.models import AlertState, VoiceAlert

    now = timezone.now()
    ids = list(
        AlertState.objects.filter(
            state__in=(AlertState.StateChoices.QUEUED, AlertState.StateChoices.SENT, AlertState.StateChoices.ESCALATED),
            repeat_due_at__lte=now,
        ).order_by("repeat_due_at", "pk").values_list("pk", flat=True)[:batch_size]
    )
    for state_id in ids:
        repeat_alarm_until_acknowledged.delay(state_id)
    voice_ids = list(
        VoiceAlert.objects.filter(
            source_key__startswith="ca-workflow:",
            repeat_due_at__lte=now,
        ).order_by("repeat_due_at", "pk").values_list("pk", flat=True)[:batch_size]
    )
    for voice_alert_id in voice_ids:
        repeat_workflow_voice_alert.delay(voice_alert_id)
    return len(ids) + len(voice_ids)


@shared_task
def escalate_alert(alert_state_id, *legacy_args):
    """Escalate one unacknowledged event and create durable delivery records."""
    from django.contrib.contenttypes.models import ContentType
    from alerts.models import AlertNotification, AlertState, AlarmAcknowledgment
    from accounts.utils import get_contacts_for_alert
    from accounts.models import CustomUser

    if legacy_args:
        # Resolve already-queued calls from the former (config, model, object)
        # task signature into the corresponding durable state row.
        legacy_ref = str(legacy_args[-1])[:255]
        legacy_state = AlertState.objects.filter(
            pk=legacy_ref,
            alert_config_id=alert_state_id,
            state__in=(AlertState.StateChoices.QUEUED, AlertState.StateChoices.SENT, AlertState.StateChoices.ESCALATED),
        ).first()
        if not legacy_state:
            legacy_state = AlertState.objects.filter(
                alert_config_id=alert_state_id,
                instance_ref=legacy_ref,
                state__in=(AlertState.StateChoices.QUEUED, AlertState.StateChoices.SENT, AlertState.StateChoices.ESCALATED),
            ).first()
        return escalate_alert.run(legacy_state.pk) if legacy_state else "legacy_state_not_found"

    queued_ids = []
    voice_payload = None
    with transaction.atomic():
        state = (
            AlertState.objects.select_for_update()
            .select_related("alert_config")
            .filter(pk=alert_state_id)
            .first()
        )
        if not state or state.state not in {
            AlertState.StateChoices.QUEUED,
            AlertState.StateChoices.SENT,
            AlertState.StateChoices.ESCALATED,
        }:
            return "closed"
        now = timezone.now()
        if not state.escalation_due_at or state.escalation_due_at > now:
            return "not_due"

        state_type = ContentType.objects.get_for_model(AlertState)
        already_acknowledged = AlarmAcknowledgment.objects.filter(
            user__is_active=True,
            content_type=state_type,
            object_id=state.pk,
            acknowledged=True,
        ).exists()
        if already_acknowledged:
            state.state = AlertState.StateChoices.ACKNOWLEDGED
            state.escalation_due_at = None
            state.save(update_fields=["state", "escalation_due_at", "updated_at"])
            return "acknowledged"

        config = state.alert_config
        policy = config.escalation_policy if isinstance(config.escalation_policy, dict) else {}
        try:
            max_escalations = max(1, int(policy.get("max_escalations", 3)))
        except (TypeError, ValueError):
            max_escalations = 3
        if state.escalation_count >= max_escalations:
            state.escalation_due_at = None
            state.save(update_fields=["escalation_due_at", "updated_at"])
            return "max_escalations_reached"

        sequence = state.escalation_count + 1
        stages = policy.get("stages") if isinstance(policy.get("stages"), list) else []
        stage = stages[min(sequence - 1, len(stages) - 1)] if stages else {}
        if not isinstance(stage, dict):
            stage = {}

        user_ids = []
        if config.escalate_to_id:
            user_ids.append(config.escalate_to_id)
        if config.allow_ai_recipients:
            user_ids.extend(_as_list(config.ai_recommended_recipients))
        user_ids.extend(_as_list(stage.get("user_ids", stage.get("recipients"))))
        group_ids = _as_list(stage.get("group_ids", stage.get("groups")))
        section_codes = _as_list(stage.get("sections"))
        plant_ids = _as_list(stage.get("treatment_plant_ids", stage.get("treatment_plants")))

        if not any((user_ids, group_ids, section_codes, plant_ids)):
            user_ids.extend(config.recipients.values_list("pk", flat=True))
            group_ids.extend(config.target_groups.values_list("pk", flat=True))
            section_codes.extend(config.target_sections.values_list("code", flat=True))
            plant_ids.extend(config.target_treatment_plants.values_list("pk", flat=True))
        if not any((user_ids, group_ids, section_codes, plant_ids)):
            state.escalation_due_at = None
            state.save(update_fields=["escalation_due_at", "updated_at"])
            return "no_escalation_audience"
        plant_id = (state.geo_context or {}).get("plant_id")
        if plant_id and plant_ids:
            if str(plant_id) not in {str(value) for value in plant_ids}:
                state.escalation_due_at = None
                state.save(update_fields=["escalation_due_at", "updated_at"])
                return "out_of_scope"
            plant_ids = [plant_id]
        elif plant_id:
            plant_ids = [plant_id]
        contacts = get_contacts_for_alert(
            user_ids=list(set(user_ids)) or None,
            groups=list(set(group_ids)) or None,
            sections=list(set(section_codes)) or None,
            treatment_plants=list(set(plant_ids)) or None,
        )
        recipient_ids = sorted({int(contact["id"]) for contact in contacts if contact.get("id")})
        if plant_id and recipient_ids:
            directly_named = set(user_ids)
            recipient_ids = list(CustomUser.objects.filter(pk__in=recipient_ids).filter(
                Q(assigned_treatment_plant_id=plant_id)
                | Q(pk__in=directly_named)
                | Q(is_superuser=True)
            ).values_list("pk", flat=True))
        if not recipient_ids:
            state.escalation_due_at = None
            state.save(update_fields=["escalation_due_at", "updated_at"])
            return "no_escalation_recipients"
        users = {
            user.pk: user
            for user in CustomUser.objects.filter(pk__in=recipient_ids, is_active=True, is_locked=False)
            .select_related("alert_preferences")
        }

        title = f"Escalated: {state.title or config.alert_type}"[:255]
        message = (
            f"This alert is still unacknowledged after {config.tat_for_escalation}. "
            f"{state.message}"
        ).strip()
        for user_id in recipient_ids:
            user = users.get(user_id)
            if not user:
                continue
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
            notification, created = AlertNotification.objects.get_or_create(
                alert_state=state,
                recipient=user,
                sequence=sequence,
                defaults={
                    "title": title,
                    "message": message,
                    "priority": state.priority,
                    "is_escalation": True,
                    "channels": channels,
                    "delivery_status": AlertNotification.DeliveryStatus.PENDING if channels else AlertNotification.DeliveryStatus.STORED,
                    "next_attempt_at": now,
                },
            )
            if created and channels:
                queued_ids.append(notification.pk)

        state.state = AlertState.StateChoices.ESCALATED
        state.escalation_count = sequence
        state.last_escalated_at = now
        state.escalation_due_at = (
            now + config.escalation_timeout
            if config.escalation_timeout and sequence < max_escalations
            else None
        )
        state.save(update_fields=[
            "state", "escalation_count", "last_escalated_at", "escalation_due_at", "updated_at",
        ])
        config.last_escalation = now
        config.save(update_fields=["last_escalation", "updated_at"])
        voice_payload = {
            "message": message,
            "title": title,
            "alert_config_id": config.pk,
            "alert_state_id": state.pk,
            "source_key": f"alert-state:{state.pk}:escalation:{sequence}",
            "recipient_ids": recipient_ids,
        }

    for notification_id in queued_ids:
        deliver_alert_notification.delay(notification_id)
    if voice_payload and recipient_ids:
        generate_voice_notification.delay(**voice_payload)
    return "escalated"


# =========================================
# 🔹 Voice notification (Asynchronous Celery Task)
# =========================================
@shared_task(bind=True, max_retries=3)
def generate_voice_notification(
    self,
    message,
    alert_config_id=None,
    title=None,
    forced_voice_id=None,
    alert_state_id=None,
    plant_id=None,
    source_key=None,
    recipient_ids=None,
):
    try:
        from alerts.models import VoiceAlert, AlertConfig, VoiceOver, AlertState
        cfg = (
            AlertConfig.objects.select_related("template").filter(pk=alert_config_id).first()
            if alert_config_id else None
        )
        state = AlertState.objects.filter(pk=alert_state_id).first() if alert_state_id else None
        ca_alert = None
        if source_key and source_key.startswith("ca-workflow:"):
            from ca.models import ChemicalWorkflowAlert

            ca_alert_id = source_key.removeprefix("ca-workflow:")
            ca_alert = ChemicalWorkflowAlert.objects.select_related(
                "recipient", "analysis_request", "analysis_request__batch"
            ).filter(pk=ca_alert_id).first()
            if not ca_alert or ca_alert.acknowledged_at:
                return None
            if (
                ca_alert.alert_type in {"ca_assignment_pending", "ca_assignment_escalation"}
                and ca_alert.analysis_request_id
                and ca_alert.analysis_request.assigned_analysts.exists()
            ):
                from ca.services.workflow_notifications import clear_assignment_notifications
                clear_assignment_notifications(ca_alert.analysis_request)
                return None
            if not recipient_ids:
                recipient_ids = [ca_alert.recipient_id]
            if cfg is None:
                cfg = AlertConfig.objects.select_related("template").filter(
                    alert_type=ca_alert.alert_type
                ).first()
        if cfg and (
            not cfg.active
            or (cfg.template_id and (not cfg.template.active or not cfg.template.auto_voice_enabled))
        ):
            return None
        forced_voice = VoiceOver.objects.filter(pk=forced_voice_id).first() if forced_voice_id else None
        resolved_title = title or (state.title if state and state.title else (cfg.alert_type if cfg else "Alert Notification"))
        resolved_message = message or (state.message if state else "")
        if not source_key and state:
            source_key = f"alert-state:{state.pk}"

        voice_alert = VoiceAlert.objects.filter(source_key=source_key).first() if source_key else None
        if not voice_alert or not voice_alert.profile_audio_ready:
            voice_alert = VoiceAlert.generate_persistent_voice_alert(
                message=resolved_message,
                title=resolved_title,
                alert_config=cfg,
                alert_state=state,
                forced_voice=forced_voice,
                source_key=source_key,
            )
        voice_url = voice_alert.audio_file.url if voice_alert and voice_alert.profile_audio_ready else None
        if not voice_url:
            raise RuntimeError("Voice synthesis did not produce a persistent audio file.")

        mode = cfg.voice_persistence_mode if cfg else AlertConfig.VoicePersistenceMode.ONCE
        if ca_alert and not cfg:
            mode = (
                AlertConfig.VoicePersistenceMode.UNTIL_ANALYST_ASSIGNED
                if ca_alert.alert_type in {"ca_assignment_pending", "ca_assignment_escalation"}
                else AlertConfig.VoicePersistenceMode.UNTIL_ACKNOWLEDGED
            )
        repeat_interval = max(5, int(getattr(cfg, "voice_repeat_interval_seconds", 60) or 60))
        repeat_max = int(getattr(cfg, "voice_repeat_max_count", 0) or 0)
        if mode != AlertConfig.VoicePersistenceMode.ONCE:
            due_at = timezone.now() + timedelta(seconds=repeat_interval)
            if state:
                AlertState.objects.filter(
                    pk=state.pk,
                    state__in=(AlertState.StateChoices.QUEUED, AlertState.StateChoices.SENT, AlertState.StateChoices.ESCALATED),
                ).update(repeat_due_at=due_at)
            elif ca_alert:
                VoiceAlert.objects.filter(pk=voice_alert.pk).update(repeat_due_at=due_at)
        elif ca_alert:
            VoiceAlert.objects.filter(pk=voice_alert.pk).update(repeat_due_at=None)

        if not recipient_ids and state:
            recipient_ids = list(state.notifications.values_list("recipient_id", flat=True).distinct())
        channel_layer = get_channel_layer()
        if channel_layer and voice_url:
            for user_id in set(recipient_ids or []):
                preference = None
                try:
                    from alerts.models import UserAlertPreference
                    preference = UserAlertPreference.objects.filter(user_id=user_id).first()
                except Exception:
                    pass
                if preference and not preference.receive_push:
                    continue
                if ca_alert:
                    async_to_sync(channel_layer.group_send)(
                        f"user_{user_id}_alerts",
                        {
                            "type": "alert.message",
                            "id": f"ca_workflow_alert_{ca_alert.pk}",
                            "title": ca_alert.title,
                            "message": ca_alert.message,
                            "priority": ca_alert.severity,
                            "alert_type": ca_alert.alert_type,
                            "instance_ref": str(ca_alert.analysis_request_id or ca_alert.batch_id or ""),
                            "voice_url": voice_url,
                            "voice_profile": voice_alert.voice_over.name if voice_alert.voice_over_id else "",
                            "auto_voice": True,
                            "repeat_voice": False,
                            "timestamp": timezone.now().isoformat(),
                        },
                    )
                else:
                    async_to_sync(channel_layer.group_send)(
                        f"user_{user_id}_alerts",
                        {
                            "type": "voice.alert",
                            "id": f"state_{state.pk}" if state else f"voice_{voice_alert.pk}",
                            "audio_url": voice_url,
                            "voice_profile": voice_alert.voice_over.name if voice_alert.voice_over_id else "",
                            "message": resolved_message,
                            "title": resolved_title,
                            "auto_voice": True,
                            "repeat_voice": bool(source_key and ":escalation:" in source_key),
                            "timestamp": timezone.now().isoformat(),
                        },
                    )

        if voice_url:
            logger.info(f"Persistent VoiceAlert generated at {voice_url}")
            return voice_url

        return voice_url
    except Exception as e:
        logger.exception(f"Failed to generate voice notification: {e}")
        if self.request.retries < self.max_retries:
            raise self.retry(
                exc=e,
                countdown=min(30 * (2 ** self.request.retries), 300),
            )
        return None


@shared_task
def repeat_workflow_voice_alert(voice_alert_id):
    """Repeat a CA workflow voice reminder until acknowledgment or assignment."""
    from alerts.models import AlertConfig, UserAlertPreference, VoiceAlert
    from accounts.models import CustomUser
    from ca.models import ChemicalWorkflowAlert

    payload = None
    with transaction.atomic():
        voice_alert = (
            VoiceAlert.objects.select_for_update()
            .select_related("alert_config", "alert_config__template")
            .filter(pk=voice_alert_id)
            .first()
        )
        if not voice_alert or not voice_alert.source_key or not voice_alert.source_key.startswith("ca-workflow:"):
            return "voice_alert_not_found"
        now = timezone.now()
        if not voice_alert.repeat_due_at or voice_alert.repeat_due_at > now:
            return "not_due"
        ca_alert = ChemicalWorkflowAlert.objects.select_for_update().select_related(
            "recipient", "analysis_request"
        ).filter(pk=voice_alert.source_key.removeprefix("ca-workflow:")).first()
        if not ca_alert or ca_alert.acknowledged_at:
            voice_alert.repeat_due_at = None
            voice_alert.save(update_fields=["repeat_due_at"])
            return "workflow_alert_closed"
        assignment_alert = ca_alert.alert_type in {"ca_assignment_pending", "ca_assignment_escalation"}
        if assignment_alert and ca_alert.analysis_request_id and ca_alert.analysis_request.assigned_analysts.exists():
            from ca.services.workflow_notifications import clear_assignment_notifications
            clear_assignment_notifications(ca_alert.analysis_request)
            voice_alert.repeat_due_at = None
            voice_alert.save(update_fields=["repeat_due_at"])
            return "analyst_assigned"

        config = voice_alert.alert_config
        if config is None:
            config = AlertConfig.objects.select_related("template").filter(alert_type=ca_alert.alert_type).first()
        mode = (
            config.voice_persistence_mode
            if config
            else (
                AlertConfig.VoicePersistenceMode.UNTIL_ANALYST_ASSIGNED
                if assignment_alert
                else AlertConfig.VoicePersistenceMode.UNTIL_ACKNOWLEDGED
            )
        )
        if (
            mode == AlertConfig.VoicePersistenceMode.ONCE
            or (config and (
                not config.active
                or (config.template_id and (not config.template.active or not config.template.auto_voice_enabled))
            ))
        ):
            voice_alert.repeat_due_at = None
            voice_alert.save(update_fields=["repeat_due_at"])
            return "voice_repeats_disabled"
        repeat_max = int(getattr(config, "voice_repeat_max_count", 0) or 0)
        if repeat_max and voice_alert.repeat_count >= repeat_max:
            voice_alert.repeat_due_at = None
            voice_alert.save(update_fields=["repeat_due_at"])
            return "repeat_limit_reached"
        if not voice_alert.profile_audio_ready:
            interval = max(5, int(getattr(config, "voice_repeat_interval_seconds", 60) or 60))
            voice_alert.repeat_due_at = now + timedelta(seconds=interval)
            voice_alert.save(update_fields=["repeat_due_at"])
            transaction.on_commit(lambda: generate_voice_notification.delay(
                message=ca_alert.message,
                title=ca_alert.title,
                alert_config_id=config.pk if config else None,
                source_key=voice_alert.source_key,
            ))
            return "profile_voice_refresh_queued"

        preference = UserAlertPreference.objects.filter(user_id=ca_alert.recipient_id).first()
        user = CustomUser.objects.filter(pk=ca_alert.recipient_id, is_active=True, is_locked=False).first()
        if not user or (preference and not preference.receive_push):
            voice_alert.repeat_due_at = None
            voice_alert.save(update_fields=["repeat_due_at"])
            return "recipient_unavailable"

        voice_alert.alert_config = config
        voice_alert.repeat_count += 1
        interval = max(5, int(getattr(config, "voice_repeat_interval_seconds", 60) or 60))
        voice_alert.repeat_due_at = (
            now + timedelta(seconds=interval)
            if not repeat_max or voice_alert.repeat_count < repeat_max
            else None
        )
        voice_alert.save(update_fields=["alert_config", "repeat_count", "repeat_due_at"])
        payload = {
            "message": ca_alert.message,
            "title": ca_alert.title,
            "voice_url": voice_alert.audio_file.url if voice_alert.profile_audio_ready else None,
            "voice_profile": voice_alert.voice_over.name if voice_alert.voice_over_id else "",
            "user_ids": [ca_alert.recipient_id],
            "priority": ca_alert.severity,
            "alert_type": ca_alert.alert_type,
            "instance_ref": str(ca_alert.analysis_request_id or ca_alert.batch_id or ""),
            "notification_id": f"ca_workflow_alert_{ca_alert.pk}",
            "auto_voice": True,
            "repeat_voice": True,
        }
    if payload and payload["voice_url"]:
        send_websocket_notification.delay(**payload)
        return "repeated"
    return "voice_audio_missing"


# =========================================
# 🔹 Alarm trigger
# =========================================
@shared_task
def trigger_alarm(user_id, message, alarm_sound_path=None, alert_state_id=None):
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        from accounts.models import CustomUser
        from alerts.models import VoiceAlert
        user = CustomUser.objects.filter(pk=user_id, is_active=True, is_locked=False).select_related("alert_preferences").first()
        if not user:
            return
        preference = getattr(user, "alert_preferences", None)
        if preference and not preference.receive_push:
            return
        voice_alert = VoiceAlert.objects.filter(alert_state_id=alert_state_id).order_by("-created_at").first() if alert_state_id else None
        voice_url = voice_alert.audio_file.url if voice_alert and voice_alert.profile_audio_ready else None
        voice_profile = voice_alert.voice_over.name if voice_alert and voice_alert.voice_over_id else ""
        state = voice_alert.alert_state if voice_alert else None
        async_to_sync(channel_layer.group_send)(
            f"user_{user_id}_alerts",
            {
                "type": "alert.message",
                "id": f"state_{state.pk}" if state else None,
                "title": "Critical Alarm",
                "message": message,
                "priority": "critical",
                "alert_type": "critical_alarm",
                "instance_ref": state.instance_ref if state else None,
                "voice_url": voice_url,
                "voice_profile": voice_profile,
                "auto_voice": True,
                "timestamp": timezone.now().isoformat(),
            },
        )
        logger.info("Critical alarm pushed to user %s", user_id)
    except Exception as e:
        logger.exception(f"Failed to trigger alarm for user {user_id}: {e}")

# =========================================
# 🔹 Repeat alarms until acknowledged
# =========================================
@shared_task(bind=True)
def legacy_repeat_alarm_until_acknowledged(self, user_id, alert_config_id, instance_model, instance_id,
                                    interval_sec=30, max_repeats=20):
    """Drain legacy queued repeat jobs without re-sending untracked alerts."""
    logger.info(
        "Ignoring legacy repeat alarm job user=%s config=%s instance=%s:%s; "
        "new alerts use the persisted AlertNotification lifecycle.",
        user_id,
        alert_config_id,
        instance_model,
        instance_id,
    )
    return "legacy_repeat_deprecated"

# =========================================
# 🔹 Durable critical alarm repeat
# =========================================
@shared_task(bind=True)
def repeat_alarm_until_acknowledged(self, alert_state_id, interval_sec=30, max_repeats=20, repeat_count=0, *legacy_args):
    """Repeat a configured voice reminder while its persisted alert stays open."""
    from alerts.models import AlertNotification, AlertState

    if legacy_args or not isinstance(max_repeats, (int, float)):
        # Forward legacy queued calls to the retained compatibility handler.
        legacy_repeat_alarm_until_acknowledged.delay(
            alert_state_id,
            interval_sec,
            max_repeats,
            repeat_count,
            *legacy_args,
        )
        return "legacy_repeat_forwarded"

    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    recipient_ids = []
    with transaction.atomic():
        state = (
            AlertState.objects.select_for_update()
            .select_related("alert_config", "alert_config__template")
            .filter(pk=alert_state_id)
            .first()
        )
        if not state or state.state not in {
            AlertState.StateChoices.QUEUED,
            AlertState.StateChoices.SENT,
            AlertState.StateChoices.ESCALATED,
        }:
            return
        now = timezone.now()
        config = state.alert_config
        if not state.repeat_due_at or state.repeat_due_at > now:
            return
        if (
            not config.active
            or config.voice_persistence_mode == config.VoicePersistenceMode.ONCE
            or not config.template_id
            or not config.template.active
            or not config.template.auto_voice_enabled
        ):
            state.repeat_due_at = None
            state.save(update_fields=["repeat_due_at", "updated_at"])
            return
        configured_max = int(config.voice_repeat_max_count or 0)
        if configured_max and state.repeat_count >= configured_max:
            state.repeat_due_at = None
            state.save(update_fields=["repeat_due_at", "updated_at"])
            return

        notifications = AlertNotification.objects.filter(
            alert_state=state,
            acknowledged_at__isnull=True,
            recipient__is_active=True,
            recipient__is_locked=False,
        ).select_related("recipient", "recipient__alert_preferences")
        for notification in notifications:
            preference = getattr(notification.recipient, "alert_preferences", None)
            if "push" in (notification.channels or []) and (not preference or preference.receive_push):
                recipient_ids.append(notification.recipient_id)
        recipient_ids = sorted(set(recipient_ids))
        if not recipient_ids:
            state.repeat_due_at = None
            state.save(update_fields=["repeat_due_at", "updated_at"])
            return

        from alerts.models import VoiceAlert

        voice_alert = VoiceAlert.objects.filter(alert_state=state).exclude(audio_file="").order_by("-created_at").first()
        voice_url = voice_alert.audio_file.url if voice_alert and voice_alert.profile_audio_ready else None
        voice_profile = voice_alert.voice_over.name if voice_alert and voice_alert.voice_over_id else ""
        if not voice_url:
            # TTS can finish after the initial reminder becomes due. Keep the
            # durable reminder pending and let the next scheduler pass retry.
            state.repeat_due_at = now + timedelta(seconds=max(5, int(config.voice_repeat_interval_seconds or 60)))
            state.save(update_fields=["repeat_due_at", "updated_at"])
            def enqueue_voice_generation(
                state_id=state.pk,
                config_id=config.pk,
                alert_title=state.title,
                alert_message=state.message,
            ):
                generate_voice_notification.delay(
                    message=alert_message,
                    title=alert_title,
                    alert_config_id=config_id,
                    alert_state_id=state_id,
                    source_key=f"alert-state:{state_id}",
                )

            transaction.on_commit(enqueue_voice_generation)
            return
        state.repeat_count += 1
        state.repeat_due_at = (
            now + timedelta(seconds=max(5, int(config.voice_repeat_interval_seconds or 60)))
            if not configured_max or state.repeat_count < configured_max
            else None
        )
        state.save(update_fields=["repeat_count", "repeat_due_at", "updated_at"])
        alert_message = state.message
        alert_title = state.title or "Critical Alert"
        alert_type = state.alert_config.alert_type
        instance_ref = state.instance_ref
        auto_voice = True

    send_websocket_notification.delay(
        alert_message,
        title=alert_title,
        voice_url=voice_url,
        user_ids=recipient_ids,
        priority=state.priority,
        alert_type=alert_type,
        instance_ref=instance_ref,
        notification_id=f"state_{alert_state_id}",
        auto_voice=auto_voice,
        repeat_voice=True,
        voice_profile=voice_profile,
    )


# =========================================
# Obsolete legacy escalation handler (kept for queued-task compatibility)
# =========================================
@shared_task
def legacy_escalate_alert(alert_config_id, instance_model, instance_id):
    try:
        AlertConfig = apps.get_model("alerts", "AlertConfig")
        model_map = {
            "qc": "qc.NonConformity",
            "heavymetal": "heavy_metal.HeavyMetalNonConformity",
            "microbial": "microbial.NonConformity",
            "inventory": "inventory.NonConformity"
        }
        InstanceModel = apps.get_model(*model_map[instance_model].split("."))
        instance = InstanceModel.objects.get(id=instance_id)
        alert_config = AlertConfig.objects.get(id=alert_config_id)
        from alerts.engine import AlertEngine
        states = AlertEngine.trigger_alert(
            instance,
            alert_config_id=alert_config.pk,
            audit_notes="Legacy escalation task forwarded to the durable alert engine.",
        )
        state = states[-1] if states else None
        if state and state.escalation_due_at and state.escalation_due_at <= timezone.now():
            escalate_alert.delay(state.pk)
        return "forwarded" if state else "no_active_alert"

    except Exception as e:
        logger.exception(f"Error in escalate_alert: {e}")

# =========================================
# 🔹 QC / HeavyMetal / Microbial Auto-Generation Engine
# =========================================
from qc.models import SamplingSchedule, WaterAnalysis
from heavy_metal.models import HeavyMetalAnalysis
from microbial.models import MicrobialAnalysis

ANALYSIS_MODEL_MAP = {
    "water": WaterAnalysis,
    "heavymetal": HeavyMetalAnalysis,
    "microbial": MicrobialAnalysis,
}

def _analysis_exists(model, plant, analysis_time):
    return model.objects.filter(treatment_plant=plant, analysis_time=analysis_time).exists()

def _build_analysis_instance(schedule, analysis_time):
    model = ANALYSIS_MODEL_MAP[schedule.analysis_type]
    kwargs = {
        "treatment_plant": schedule.treatment_plant,
        "analysis_time": analysis_time,
    }
    if hasattr(model, "treatment_plant_phase"):
        kwargs["treatment_plant_phase"] = schedule.treatment_plant_phase
    if hasattr(model, "shift") and schedule.shift:
        kwargs["shift"] = schedule.shift
    if hasattr(model, "method") and hasattr(schedule, "method"):
        kwargs["method"] = schedule.method
    return model(**kwargs)

# =========================================
# 🔹 Scheduled Analyses Generator (patch)
# =========================================
@shared_task
def generate_scheduled_analyses(run_date=None):
    run_date = run_date or date.today()
    schedules = (
        SamplingSchedule.objects
        .filter(active=True)
        .select_related("treatment_plant", "treatment_plant_phase", "shift")
        .prefetch_related("times")
    )

    with transaction.atomic():
        for schedule in schedules:
            model = ANALYSIS_MODEL_MAP.get(schedule.analysis_type)
            if not model:
                logger.warning(f"Unknown analysis_type={schedule.analysis_type} in schedule {schedule.id}")
                continue

            for sampling_time in schedule.times.all():
                analysis_time = timezone.make_aware(datetime.combine(run_date, sampling_time.time))
                if _analysis_exists(model, schedule.treatment_plant, analysis_time):
                    continue

                instance = _build_analysis_instance(schedule, analysis_time)
                instance.save()
                logger.info(f"Generated analysis {model.__name__} for {schedule.treatment_plant} at {analysis_time}")

                # Persist and route only the matching scheduled-analysis rule.
                from alerts.engine import AlertEngine
                AlertEngine.trigger_alert(
                    instance,
                    alert_type="sampling_delay",
                    audit_notes=f"Scheduled for {schedule.treatment_plant} at {analysis_time}.",
                )
