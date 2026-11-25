import io
import os
import uuid
import logging
import shutil
import tempfile
import threading
from contextlib import contextmanager
from typing import Optional
from django.db import IntegrityError, models, transaction
from django.utils import timezone
from django.db.models import JSONField, F
from django.contrib.postgres.indexes import GinIndex
from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.dispatch import receiver
from django.db.models.signals import post_save
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from accounts.models import CustomUser
from .utils import (
    SafeConditionEvaluator,
    InstanceSerializer,
    AIAlertPriorityPredictor,
    GeoContextAnalyzer,
    AIEscalationForecaster,
)

User = CustomUser
logger = logging.getLogger(__name__)


# ============================================================
# 🧠 ALERT TEMPLATE SYSTEM
# ============================================================
class AlertTemplate(models.Model):
    name = models.CharField(max_length=120, unique=True)
    subject_template = models.TextField()
    body_template = models.TextField()
    version = models.IntegerField(default=1)
    active = models.BooleanField(default=True)
    metadata = JSONField(default=dict)
    auto_voice_enabled = models.BooleanField(default=True, editable=False)

    class Meta:
        unique_together = ('name', 'version')

    def __str__(self):
        return f"{self.name} v{self.version}"

    def save(self, *args, **kwargs):
        self.auto_voice_enabled = True
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"auto_voice_enabled"}
        return super().save(*args, **kwargs)


# ============================================================
# ⚙️ ALERT CATEGORY
# ============================================================
class AlertCategory(models.Model):
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    parent = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.CASCADE, related_name='subcategories'
    )
    ai_cluster_code = models.CharField(max_length=32, blank=True)

    def __str__(self):
        return self.name


# ============================================================
# 🚨 ALERT CONFIG
# ============================================================
class AlertConfig(models.Model):
    class AlertType(models.TextChoices):
        NONCONFORMITY = 'nonconformity', 'Nonconformity'
        AUTO_VERIFICATION = 'auto_verification', 'Automatic Verification'
        SAMPLING_DELAY = 'sampling_delay', 'Sampling Delay'
        ANALYSIS_MISSED = 'analysis_missed', 'Analysis Missed'
        MAINTENANCE_DUE = 'maintenance_due', 'Maintenance Due'
        VERIFICATION_PENDING = 'verification_pending', 'Verification Pending'
        PERFORMANCE_DEVIATION = 'performance_deviation', 'Performance Deviation'
        WATER_QUALITY_LIMIT = 'water_quality_limit', 'Water Quality Treatment Limit'
        SENSOR_THRESHOLD = 'sensor_threshold', 'Instrument Sensor Threshold'
        SENSOR_HEALTH = 'sensor_health', 'Instrument Sensor Health'
        DERIVED_METRIC = 'derived_metric', 'Instrument Derived Metric'
        PHASE_ANOMALY = 'phase_anomaly', 'Treatment Phase Anomaly'
        CA_ASSIGNMENT_PENDING = 'ca_assignment_pending', 'CA analyst assignment pending'
        CA_ASSIGNMENT_ESCALATION = 'ca_assignment_escalation', 'CA analyst assignment escalation'
        CA_QC_FAILURE = 'ca_qc_failure', 'Chemical analysis method QC failure'
        CA_QC_DRIFT = 'ca_qc_drift', 'Chemical analysis QC drift'
        CA_INTERLAB_DISAGREEMENT = 'ca_interlab_disagreement', 'Chemical interlaboratory disagreement'
        CA_INTERLAB_RESULTS_READY = 'ca_interlab_results_ready', 'Chemical interlaboratory results ready'
        CA_OOS_RESULT = 'ca_oos_result', 'Chemical analysis out-of-specification result'
        CA_INSTRUMENT_UNAVAILABLE = 'ca_instrument_unavailable', 'Chemical laboratory instrument unavailable'

    class VoicePersistenceMode(models.TextChoices):
        ONCE = 'once', 'Play once'
        UNTIL_ACKNOWLEDGED = 'until_acknowledged', 'Repeat until acknowledged'
        UNTIL_ANALYST_ASSIGNED = 'until_analyst_assigned', 'Repeat until an analyst is assigned'

    class PriorityLevel(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'
        CRITICAL = 'critical', 'Critical'

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    alert_type = models.CharField(max_length=100, choices=AlertType.choices)
    category = models.ForeignKey(AlertCategory, null=True, blank=True, on_delete=models.SET_NULL)
    priority = models.CharField(max_length=20, choices=PriorityLevel.choices, default=PriorityLevel.MEDIUM)
    template = models.ForeignKey(AlertTemplate, on_delete=models.PROTECT)
    recipients = models.ManyToManyField(User, related_name='alert_configs')

    # Customizable targeting fields
    target_sections = models.ManyToManyField("core.Section", blank=True, help_text="Send alert only to users in these sections")
    target_groups = models.ManyToManyField("auth.Group", blank=True, help_text="Send alert only to users in these groups")
    target_treatment_plants = models.ManyToManyField("t.TreatmentPlant", blank=True, help_text="Send alert only to users linked to these treatment plants")
    allow_ai_recipients = models.BooleanField(default=True, help_text="Include AI-prioritized recipients")

    # Alert timing & escalation
    verify_timeout = models.DurationField()
    verify_before_subsequent = models.DurationField()
    approve_timeout = models.DurationField(default=timezone.timedelta(minutes=30))
    tat_for_escalation = models.DurationField(default=timezone.timedelta(hours=1))
    escalation_timeout = models.DurationField(default=timezone.timedelta(hours=2))
    escalate_to = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='escalated_alerts')
    voice_persistence_mode = models.CharField(
        max_length=32,
        choices=VoicePersistenceMode.choices,
        default=VoicePersistenceMode.UNTIL_ACKNOWLEDGED,
        help_text="Controls how long voiced notifications repeat until their configured completion condition.",
    )
    voice_repeat_interval_seconds = models.PositiveIntegerField(
        default=60,
        help_text="Seconds between voice reminders. Minimum effective interval is 5 seconds.",
    )
    voice_repeat_max_count = models.PositiveIntegerField(
        default=0,
        help_text="Maximum repeat count; 0 repeats until the selected completion condition is met.",
    )

    filter_conditions = JSONField(default=dict)
    trigger_action = JSONField(default=dict)
    escalation_policy = JSONField(default=dict)
    adaptive_retry_policy = JSONField(default=dict)
    ai_recommended_recipients = JSONField(default=list, blank=True)

    custom_fields = JSONField(default=dict)
    active = models.BooleanField(default=True)
    retry_count = models.PositiveIntegerField(default=0)

    last_triggered = models.DateTimeField(null=True, blank=True)
    last_escalation = models.DateTimeField(null=True, blank=True)
    geo_context = JSONField(default=dict)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    activated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='activated_alerts')

    class Meta:
        indexes = [
            GinIndex(fields=['filter_conditions']),
            GinIndex(fields=['trigger_action']),
            GinIndex(fields=['custom_fields']),
        ]

    def __str__(self):
        return f"{self.alert_type} [{self.priority}]"

    def clean(self):
        SafeConditionEvaluator.validate_structure(self.filter_conditions, 'filter_conditions')
        SafeConditionEvaluator.validate_structure(self.trigger_action, 'trigger_action')

    # ============================================================
    # ✅ FULL ALERT LIFECYCLE
    # ============================================================
    def send_alert(self, instance):
        """Route legacy callers through the single durable alert lifecycle."""
        from .engine import AlertEngine

        return AlertEngine.trigger_alert(
            instance,
            alert_type=self.alert_type,
            alert_config_id=self.pk,
        )

    # ============================================================
    # ✅ GENERIC AUTO-ESCALATION
    # ============================================================
    def auto_escalate_if_unacknowledged(self):
        """Schedule eligible states; never send an escalation before its TAT."""
        from .engine import AlertEngine

        return AlertEngine.auto_escalate_unacknowledged(alert_type=self.alert_type)


# ============================================================
# 🔁 ALERT STATE
# ============================================================
class AlertState(models.Model):
    class StateChoices(models.TextChoices):
        QUEUED = 'queued'
        SENT = 'sent'
        ESCALATED = 'escalated'
        ACKNOWLEDGED = 'acknowledged'
        VERIFIED = 'verified'
        CLOSED = 'closed'

    alert_config = models.ForeignKey(AlertConfig, on_delete=models.CASCADE)
    instance_ref = models.CharField(max_length=255)
    title = models.CharField(max_length=255, blank=True)
    message = models.TextField(blank=True)
    priority = models.CharField(max_length=20, default="medium")
    state = models.CharField(max_length=20, choices=StateChoices.choices, default=StateChoices.QUEUED)
    geo_context = JSONField(default=dict)
    escalation_due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_escalated_at = models.DateTimeField(null=True, blank=True)
    escalation_count = models.PositiveSmallIntegerField(default=0)
    repeat_due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    repeat_count = models.PositiveIntegerField(default=0)
    # Explicit default keeps the field upgradeable for existing alert rows.
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("alert_config", "instance_ref"),
                condition=models.Q(state__in=("queued", "sent", "escalated")),
                name="alerts_open_state_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.alert_config.alert_type} → {self.state}"


class AlertNotification(models.Model):
    """Durable per-recipient inbox event and delivery outbox record."""

    class DeliveryStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PARTIAL = "partial", "Partially delivered"
        SENT = "sent", "Sent"
        STORED = "stored", "Stored in inbox"
        FAILED = "failed", "Failed"

    alert_state = models.ForeignKey(
        AlertState,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    recipient = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="alert_notifications",
    )
    sequence = models.PositiveSmallIntegerField(default=0)
    title = models.CharField(max_length=255)
    message = models.TextField()
    priority = models.CharField(max_length=20, default="medium")
    is_escalation = models.BooleanField(default=False)
    channels = JSONField(default=list, blank=True)
    delivery_status = models.CharField(
        max_length=20,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
    )
    delivery_results = JSONField(default=dict, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=("recipient", "acknowledged_at", "created_at"),
                name="alerts_notif_user_ack_idx",
            ),
            models.Index(
                fields=("delivery_status", "next_attempt_at"),
                name="alerts_notif_delivery_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("alert_state", "recipient", "sequence"),
                name="alerts_state_recipient_sequence_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.title} → {self.recipient} ({self.delivery_status})"


# ============================================================
# ✅ ACKNOWLEDGMENT
# ============================================================
class AlarmAcknowledgment(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")

    acknowledged = models.BooleanField(default=False)
    verified = models.BooleanField(default=False)
    sentiment_score = models.FloatField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'content_type', 'object_id')


# ============================================================
# 🧩 ALERT LOGS
# ============================================================
class AlertLog(models.Model):
    alert = models.ForeignKey(AlertConfig, on_delete=models.CASCADE)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")
    details = JSONField(default=dict)
    timestamp = models.DateTimeField(auto_now_add=True)


# ============================================================
# 🔔 LIVE STATE PUSH
# ============================================================
@receiver(post_save, sender=AlertState)
def alert_state_change_observer(sender, instance, created=False, **kwargs):
    """Publish lifecycle changes only to users with a persisted inbox row."""
    if created:
        # The alert dispatch path sends the initial event to each recipient.
        return
    if instance.state not in {
        AlertState.StateChoices.ACKNOWLEDGED,
        AlertState.StateChoices.VERIFIED,
        AlertState.StateChoices.CLOSED,
    }:
        # Sent/escalated transitions have their own per-recipient outbox rows.
        return
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    try:
        recipient_ids = set()
        for notification in instance.notifications.select_related("recipient"):
            recipient_ids.add(notification.recipient_id)
        if not recipient_ids:
            return
        message = {
            "type": "alert_state",
            "id": f"state_{instance.pk}",
            "title": instance.title or instance.alert_config.alert_type,
            "message": instance.message or f"Alert state changed to {instance.state}",
            "priority": instance.priority,
            "alert_type": instance.alert_config.alert_type,
            "instance_ref": instance.instance_ref,
            "state": instance.state,
            "auto_voice": True,
            "timestamp": timezone.now().isoformat(),
        }
        for user_id in recipient_ids:
            async_to_sync(channel_layer.group_send)(f"user_{user_id}_alerts", message)
    except Exception:
        logger.exception("Could not publish alert state update to its recipients")


# ============================================================
# 🧠 USER PREFERENCES
# ============================================================
class UserAlertPreference(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="alert_preferences")
    receive_email = models.BooleanField(default=True)
    receive_sms = models.BooleanField(default=False)
    receive_push = models.BooleanField(default=True)
    receive_whatsapp = models.BooleanField(default=False)
    receive_slack = models.BooleanField(default=False)

    preferred_time_window_start = models.TimeField(null=True, blank=True)
    preferred_time_window_end = models.TimeField(null=True, blank=True)
    geo_zone = models.CharField(max_length=120, blank=True)


# ============================================================
# 🎙️ LIGHTWEIGHT TEXT-TO-SPEECH (TTS) NOTIFICATION ENGINE
# ============================================================
def synthesize_simple_tts(message: str, lang: str = "en") -> tuple[bytes, str]:
    """
    Lightweight Text-To-Speech generator.
    Uses standard gTTS for clean, natural speech (MP3) or local pyttsx3 fallback (WAV).
    Extremely fast, zero heavy neural networks, zero voice cloning overhead.
    Returns: (audio_bytes, format_extension)
    """
    clean_text = (message or "").strip()
    if not clean_text:
        return b"", "mp3"

    # 1. Primary: gTTS (lightweight Google Text-to-Speech)
    try:
        from gtts import gTTS
        fp = io.BytesIO()
        tts = gTTS(text=clean_text, lang=lang, slow=False)
        tts.write_to_fp(fp)
        audio_bytes = fp.getvalue()
        if audio_bytes and len(audio_bytes) > 100:
            return audio_bytes, "mp3"
    except Exception as gtts_err:
        logger.debug("[TTS] gTTS skipped or unavailable: %s", gtts_err)

    # 2. Local speech-engine fallback for connected application workers
    try:
        import pyttsx3
        engine = pyttsx3.init()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            engine.save_to_file(clean_text, tmp_path)
            engine.runAndWait()
            with open(tmp_path, "rb") as f:
                wav_bytes = f.read()
            if wav_bytes.startswith(b"RIFF") and wav_bytes[8:12] == b"WAVE":
                return wav_bytes, "wav"
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
    except Exception as pyttsx_err:
        logger.debug("[TTS] Local speech-engine fallback skipped or unavailable: %s", pyttsx_err)

    return b"", "mp3"


def synthesize_alert_audio(message: str, **kwargs) -> tuple[bytes, bool]:
    """Backward-compatible helper returning (audio_bytes, is_cloned=False)."""
    audio_bytes, _ = synthesize_simple_tts(message)
    return audio_bytes, False


# ============================================================
# 🔊 PERSISTENT VOICE / TTS ALERT MODEL
# ============================================================
class VoiceAlert(models.Model):
    """
    Persistent voice alert record accompanying every notification.
    Generated using lightweight text-to-speech for fast, responsive delivery.
    """
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    source_key = models.CharField(max_length=255, unique=True, null=True, blank=True)
    speaker_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="voiced_alerts",
        help_text="The user associated with this alert."
    )
    alert_config = models.ForeignKey(
        AlertConfig,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="voice_alerts",
        help_text="Associated alert configuration rule."
    )
    alert_state = models.ForeignKey(
        AlertState,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="voice_alerts",
        help_text="Associated alert state/dispatch event."
    )
    title = models.CharField(max_length=255, blank=True)
    message = models.TextField(help_text="Full text message voiced in this alert.")
    audio_file = models.FileField(
        upload_to="voice_alerts/notifications/",
        help_text="Persistent audio file of the voiced alert."
    )
    duration_seconds = models.FloatField(default=0.0)
    is_played = models.BooleanField(default=False)
    play_count = models.PositiveIntegerField(default=0)
    repeat_due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    repeat_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Voice Alert"
        verbose_name_plural = "Voice Alerts"
        ordering = ["-created_at"]

    def __str__(self):
        speaker = getattr(self.speaker_user, "username", "System TTS")
        return f"VoiceAlert [{speaker}]: {self.title or self.message[:40]}"

    @property
    def profile_audio_ready(self):
        return bool(self.audio_file)

    @classmethod
    def generate_persistent_voice_alert(
        cls,
        message: str,
        title: str = "",
        alert_config: Optional[AlertConfig] = None,
        alert_state: Optional[AlertState] = None,
        source_key: Optional[str] = None,
        speaker_user: Optional[User] = None,
        **kwargs,
    ) -> Optional["VoiceAlert"]:
        """
        Generates and persistently saves a lightweight TTS voice alert for a notification.
        """
        clean_message = (message or "").strip()
        if not clean_message:
            return None

        existing = cls.objects.filter(source_key=source_key).first() if source_key else None
        if existing and existing.profile_audio_ready:
            return existing

        audio_bytes, ext = synthesize_simple_tts(clean_message)

        alert_uuid = existing.uuid if existing else uuid.uuid4()
        filename = f"tts_alert_{alert_uuid.hex[:12]}.{ext}"

        voice_alert = existing or cls(uuid=alert_uuid, source_key=source_key)
        previous_audio_name = voice_alert.audio_file.name if voice_alert.audio_file else ""
        voice_alert.speaker_user = speaker_user
        voice_alert.alert_config = alert_config
        voice_alert.alert_state = alert_state
        voice_alert.title = title or (alert_config.alert_type if alert_config else "Alert Notification")
        voice_alert.message = clean_message
        voice_alert.duration_seconds = round(max(1.0, len(clean_message.split()) * 0.4), 1)

        if audio_bytes:
            voice_alert.audio_file.save(filename, ContentFile(audio_bytes), save=False)

        try:
            with transaction.atomic():
                voice_alert.save()
        except IntegrityError:
            if source_key:
                if audio_bytes:
                    voice_alert.audio_file.delete(save=False)
                existing = cls.objects.filter(source_key=source_key).first()
                if existing and existing.audio_file:
                    return existing
            raise

        if previous_audio_name and previous_audio_name != voice_alert.audio_file.name:
            try:
                voice_alert.audio_file.storage.delete(previous_audio_name)
            except Exception as storage_error:
                logger.debug("[TTS] Could not remove replaced audio file: %s", storage_error)

        logger.info(f"[TTS] Created lightweight voice alert {voice_alert.uuid} (size={len(audio_bytes)} bytes)")
        return voice_alert

