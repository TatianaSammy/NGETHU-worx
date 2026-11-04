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

_voice_clone_lock = threading.RLock()
_voice_clone_model = None
_voice_clone_device = None


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
    auto_voice_enabled = models.BooleanField(default=True)

    class Meta:
        unique_together = ('name', 'version')

    def __str__(self):
        return f"{self.name} v{self.version}"


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
        help_text="Controls how long voiced notifications repeat. The template must also allow auto voice.",
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
            if "push" not in (notification.channels or []):
                continue
            preference = getattr(notification.recipient, "alert_preferences", None)
            if preference and not preference.receive_push:
                continue
            recipient_ids.add(notification.recipient_id)
        if not recipient_ids:
            return
        auto_voice = bool(
            instance.alert_config_id
            and instance.alert_config.active
            and instance.alert_config.template_id
            and instance.alert_config.template.active
            and instance.alert_config.template.auto_voice_enabled
        )
        message = {
            "type": "alert_state",
            "id": f"state_{instance.pk}",
            "title": instance.title or instance.alert_config.alert_type,
            "message": instance.message or f"Alert state changed to {instance.state}",
            "priority": instance.priority,
            "alert_type": instance.alert_config.alert_type,
            "instance_ref": instance.instance_ref,
            "state": instance.state,
            "auto_voice": auto_voice,
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
# 🎙️ VOICE SYNTHESIS HELPER & VOICE CLONING ENGINE
# ============================================================
def _run_coroutine_safely(coro):
    """Run an async coroutine from synchronous code without event loop conflicts."""
    import asyncio
    import concurrent.futures
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


def resolve_voice_profile(voice_over: Optional["VoiceOver"] = None, accent: str = "co.uk", speech_rate: float = 1.0):
    """Resolve a regional TTS voice using the configured VoiceOver profile."""
    rate_percent = f"{int((speech_rate - 1.0) * 100):+d}%" if speech_rate and speech_rate != 1.0 else "+0%"

    if not voice_over:
        raise ValueError("A VoiceOver profile is required for alert speech.")

    accent_voices = {
        "co.uk": {
            "female": ("en-GB-SoniaNeural", "en-GB-LibbyNeural"),
            "male": ("en-GB-RyanNeural", "en-GB-ThomasNeural", "en-GB-AlfieNeural"),
        },
        "com": {
            "female": ("en-US-AvaNeural", "en-US-EmmaNeural", "en-US-JennyNeural"),
            "male": ("en-US-AndrewNeural", "en-US-BrianNeural", "en-US-ChristopherNeural"),
        },
        "com.au": {
            "female": ("en-AU-NatashaNeural", "en-AU-FreyaNeural"),
            "male": ("en-AU-WilliamNeural", "en-AU-DuncanNeural", "en-AU-TimNeural"),
        },
        "ca": {
            "female": ("en-CA-ClaraNeural",),
            "male": ("en-CA-LiamNeural",),
        },
        "co.za": {
            "female": ("en-ZA-LeahNeural",),
            "male": ("en-ZA-LukeNeural",),
        },
        "co.in": {
            "female": ("en-IN-NeerjaNeural",),
            "male": ("en-IN-PrabhatNeural",),
        },
        "ie": {
            "female": ("en-IE-EmilyNeural",),
            "male": ("en-IE-ConnorNeural",),
        },
    }
    gender = "female" if voice_over.gender == "female" else "male"
    profile_accent = voice_over.accent_profile or accent or "co.uk"
    voices = accent_voices.get(profile_accent, accent_voices["co.uk"])[gender]
    slot = max(1, int(voice_over.slot or 1))
    neural_voice = voices[(slot - 1) % len(voices)]
    pitch_offset = "-2Hz" if slot % 2 == 0 else "+0Hz"
    return neural_voice, pitch_offset, rate_percent


@contextmanager
def _local_voice_reference(voice_over):
    """Expose a VoiceOver sample as a local path, including remote Django storage."""
    clip = getattr(voice_over, "sample_clip", None) if voice_over else None
    if not clip:
        yield None
        return

    try:
        local_path = clip.path
        if os.path.isfile(local_path):
            yield local_path
            return
    except (AttributeError, NotImplementedError, ValueError, OSError):
        pass

    suffix = os.path.splitext(getattr(clip, "name", ""))[1].lower()
    if suffix not in {".mp3", ".wav", ".ogg", ".m4a", ".flac"}:
        suffix = ".audio"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
            temp_path = temp_file.name
            with clip.open("rb") as source:
                shutil.copyfileobj(source, temp_file)
        yield temp_path
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def _synthesize_reference_voice(message: str, voice_over: Optional["VoiceOver"]) -> bytes:
    """Clone speech from the attached sample using the optional Chatterbox runtime."""
    global _voice_clone_model, _voice_clone_device
    if not voice_over or not getattr(voice_over, "sample_clip", None):
        return b""

    try:
        import torch
        import torchaudio
        from chatterbox.tts_turbo import ChatterboxTurboTTS

        with _voice_clone_lock:
            if _voice_clone_model is None:
                _voice_clone_device = "cuda" if torch.cuda.is_available() else "cpu"
                # Nano is the CPU-capable model; use Turbo when a CUDA device
                # is present. Both accept the VoiceOver sample as a reference.
                _voice_clone_model = ChatterboxTurboTTS.from_pretrained(
                    device=_voice_clone_device,
                    nano=_voice_clone_device == "cpu",
                )

            with _local_voice_reference(voice_over) as reference_path:
                if not reference_path:
                    return b""
                waveform = _voice_clone_model.generate(
                    text=message,
                    audio_prompt_path=reference_path,
                )

            audio_buffer = io.BytesIO()
            torchaudio.save(
                audio_buffer,
                waveform.detach().cpu(),
                _voice_clone_model.sr,
                format="wav",
            )
            return audio_buffer.getvalue()
    except ImportError:
        logger.info("Voice cloning runtime is not installed; using regional TTS fallback.")
    except Exception as exc:
        logger.warning("[VoiceAlert] Reference voice cloning failed: %s", exc)
    return b""


def synthesize_alert_audio(
    message: str,
    voice_over: Optional["VoiceOver"] = None,
    accent: str = "co.uk",
    speech_rate: float = 1.0,
) -> tuple[bytes, bool]:
    """Synthesize using the selected VoiceOver profile and its local settings."""
    if voice_over is None:
        logger.error("[VoiceAlert] Refusing to synthesize speech without a VoiceOver profile.")
        return b"", False

    # 1. Reference-conditioned voice cloning from the attached VoiceOver clip.
    cloned_audio = _synthesize_reference_voice(message, voice_over)
    if cloned_audio:
        return cloned_audio, True

    # 2. Local profile-matched speech works without network access.
    try:
        import pyttsx3

        engine = pyttsx3.init()
        available_voices = engine.getProperty("voices") or []
        gender = (getattr(voice_over, "gender", "") or "").casefold()
        accent_tokens = {
            "co.uk": ("en-gb", "english_rp", "british", "en_gb"),
            "com": ("en-us", "english-us", "american", "en_us"),
            "com.au": ("en-au", "australian", "en_au", "en-gb"),
            "ca": ("en-ca", "canadian", "en_ca", "en-us"),
            "co.za": ("en-za", "south african", "en_za", "english_rp", "en-gb"),
            "co.in": ("en-in", "indian", "en_in", "en-gb"),
            "ie": ("en-ie", "irish", "en_ie", "en-sc", "en-gb"),
        }
        profile_accent = getattr(voice_over, "accent_profile", None) or accent
        tokens = accent_tokens.get(profile_accent, ())
        english_voices = []
        best_voice = None
        best_score = -1
        for candidate in available_voices:
            candidate_id = str(getattr(candidate, "id", ""))
            candidate_name = str(getattr(candidate, "name", ""))
            language_text = " ".join(
                str(value.decode(errors="ignore") if isinstance(value, bytes) else value)
                for value in (getattr(candidate, "languages", []) or [])
            )
            descriptor = f"{candidate_id} {candidate_name} {language_text}".casefold()
            if "en" not in descriptor and "english" not in descriptor:
                continue
            english_voices.append(candidate)
            score = sum(3 for token in tokens if token in descriptor)
            candidate_gender = str(getattr(candidate, "gender", "") or "").casefold()
            if gender and candidate_gender == gender:
                score += 2
            if gender and gender in descriptor:
                score += 1
            if score > best_score:
                best_voice = candidate
                best_score = score
        if best_voice is None and english_voices:
            best_voice = english_voices[0]
        if best_voice is not None:
            voice_id = str(best_voice.id)
            if os.path.exists("/usr/bin/espeak-ng") and "+" not in voice_id:
                slot = int(getattr(voice_over, "slot", 1) or 1)
                variant = 2 if slot == 1 else 4
                voice_variant = "f" if gender == "female" else "m"
                voice_id = f"{voice_id}+{voice_variant}{variant}"
            engine.setProperty("voice", voice_id)
        base_rate = int(engine.getProperty("rate") or 175)
        multiplier = float(speech_rate or getattr(voice_over, "speech_rate", 1.0) or 1.0)
        engine.setProperty("rate", max(90, min(300, int(base_rate * multiplier))))

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            engine.save_to_file(message, tmp_path)
            engine.runAndWait()
            with open(tmp_path, "rb") as audio_file:
                data = audio_file.read()
            if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
                return data, False
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
    except Exception as offline_err:
        logger.warning("[VoiceAlert] Local profile speech synthesis failed: %s", offline_err)

    # 3. Network providers retain the VoiceOver profile's accent, gender, and rate.
    try:
        import edge_tts
        neural_voice, pitch_offset, rate_offset = resolve_voice_profile(
            voice_over=voice_over,
            accent=accent,
            speech_rate=speech_rate,
        )

        async def _synthesize_edge():
            communicate = edge_tts.Communicate(
                text=message,
                voice=neural_voice,
                pitch=pitch_offset,
                rate=rate_offset,
            )
            chunks = [chunk["data"] async for chunk in communicate.stream() if chunk["type"] == "audio"]
            return b"".join(chunks)

        audio_bytes = _run_coroutine_safely(_synthesize_edge())
        if audio_bytes and len(audio_bytes) > 200:
            return audio_bytes, False
    except Exception as edge_err:
        logger.warning("[VoiceAlert] Profile-matched Edge-TTS failed: %s", edge_err)

    logger.error("[VoiceAlert] No profile-matched synthesis provider could generate audio.")
    return b"", False


def optimize_m4a_faststart(file_path: str):
    """
    Rearrange MP4/M4A atoms so that 'moov' atom precedes 'mdat'.
    Enables instant web streaming and immediate browser playback without buffering delays.
    """
    import struct
    import shutil
    try:
        if not os.path.exists(file_path):
            return
        with open(file_path, "rb") as f:
            data = f.read()
        idx = 0
        atoms = []
        while idx < len(data):
            if idx + 8 > len(data):
                break
            size = struct.unpack(">I", data[idx:idx + 4])[0]
            name = data[idx + 4:idx + 8].decode("latin1", errors="ignore")
            if size == 1:
                size = struct.unpack(">Q", data[idx + 8:idx + 16])[0]
            elif size == 0:
                size = len(data) - idx
            atoms.append((name, idx, size))
            idx += size

        moov_atom = [a for a in atoms if a[0] == "moov"]
        mdat_atom = [a for a in atoms if a[0] == "mdat"]
        if not moov_atom or not mdat_atom:
            return
        moov, mdat = moov_atom[0], mdat_atom[0]
        if moov[1] < mdat[1]:
            return  # Already faststarted

        moov_data = bytearray(data[moov[1]:moov[1] + moov[2]])
        moov_len = len(moov_data)

        stco_idx = 0
        while True:
            pos = moov_data.find(b"stco", stco_idx)
            if pos == -1:
                break
            entry_count = struct.unpack(">I", moov_data[pos + 8:pos + 12])[0]
            for i in range(entry_count):
                e_pos = pos + 12 + (i * 4)
                old_offset = struct.unpack(">I", moov_data[e_pos:e_pos + 4])[0]
                moov_data[e_pos:e_pos + 4] = struct.pack(">I", old_offset + moov_len)
            stco_idx = pos + 4

        pre_mdat = b""
        for a in atoms:
            if a[0] not in ("moov", "mdat"):
                pre_mdat += data[a[1]:a[1] + a[2]]
        mdat_data = data[mdat[1]:mdat[1] + mdat[2]]

        temp_path = file_path + ".fast.tmp"
        with open(temp_path, "wb") as f:
            f.write(pre_mdat)
            f.write(moov_data)
            f.write(mdat_data)
        shutil.move(temp_path, file_path)
        logger.info(f"[VoiceOver] Successfully faststarted audio file {file_path}")
    except Exception as exc:
        logger.warning(f"[VoiceOver] Faststart optimization error: {exc}")


# ============================================================
# 🎙️ VOICE OVER MODEL (MAXIMUM OF TWO VOICES)
# ============================================================
class VoiceOver(models.Model):
    """
    Voice Over model storing sample voice clips of users from the accounts app.
    Enforces a maximum of two voices.
    Provides 1:1 interchangeable fair distribution for all generated alert notifications.
    """
    class VoiceGender(models.TextChoices):
        FEMALE = "female", "Female Voice"
        MALE = "male", "Male Voice"
        NEUTRAL = "neutral", "Neutral Voice"

    class AccentProfile(models.TextChoices):
        BRITISH = "co.uk", "British English (co.uk)"
        AMERICAN = "com", "US English (com)"
        AUSTRALIAN = "com.au", "Australian English (com.au)"
        CANADIAN = "ca", "Canadian English (ca)"
        SOUTH_AFRICAN = "co.za", "African English (co.za)"
        INDIAN = "co.in", "Indian English (co.in)"
        IRISH = "ie", "Irish English (ie)"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="voice_overs",
        help_text="User from the accounts app whose voice is used as voice over."
    )
    name = models.CharField(
        max_length=120,
        help_text="Descriptive title or label for this voice over (e.g. 'Analyst Sarah - Plant A')"
    )
    sample_clip = models.FileField(
        upload_to="voice_overs/samples/",
        help_text="Sample voice audio clip (MP3, WAV, OGG) of the accounts user to be used as voice over."
    )
    gender = models.CharField(
        max_length=20,
        choices=VoiceGender.choices,
        default=VoiceGender.NEUTRAL,
        help_text="Voice gender profile for synthesis alignment."
    )
    accent_profile = models.CharField(
        max_length=30,
        choices=AccentProfile.choices,
        default=AccentProfile.BRITISH,
        help_text="Voice dialect/accent used for voice alert synthesis."
    )
    speech_rate = models.FloatField(
        default=1.0,
        help_text="Speed multiplier for custom alert voicing (0.75 - 1.5)."
    )
    slot = models.PositiveSmallIntegerField(
        choices=[(1, "Voice Slot 1"), (2, "Voice Slot 2")],
        null=True,
        blank=True,
        unique=True,
        help_text="Designated voice slot (Slot 1 or Slot 2; maximum 2 voices)."
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Whether this voice is actively used for generating alert messages."
    )
    usage_count = models.PositiveBigIntegerField(
        default=0,
        help_text="Total alert messages voiced by this profile (used to guarantee 1:1 fair ratio)."
    )
    last_voiced_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when this voice last synthesized an alert."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Voice Over"
        verbose_name_plural = "Voice Overs"
        ordering = ["slot", "id"]

    def __str__(self):
        full_name = getattr(self.user, "full_name", None) or self.user.get_username()
        slot_str = f" [Slot {self.slot}]" if self.slot else ""
        return f"VoiceOver: {self.name} ({full_name}){slot_str} - {self.get_accent_profile_display()}"

    def clean(self):
        super().clean()
        # Verify user belongs to the accounts app
        from accounts.models import CustomUser
        if self.user_id:
            try:
                user_instance = CustomUser.objects.get(pk=self.user_id)
                if not isinstance(user_instance, CustomUser):
                    raise ValidationError({"user": "Voice clip must belong to a user from the accounts app."})
            except CustomUser.DoesNotExist:
                raise ValidationError({"user": "Specified user not found in accounts app."})

        # Enforce maximum of two active voices in the system
        qs = VoiceOver.objects.filter(is_active=True)
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        if self.is_active and qs.count() >= 2:
            raise ValidationError(
                "A maximum of two voices can be active simultaneously. "
                "Please deactivate or update an existing voice before configuring a new one."
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
        if self.sample_clip and hasattr(self.sample_clip, 'path') and os.path.exists(self.sample_clip.path):
            name = self.sample_clip.name.lower()
            if name.endswith('.m4a') or name.endswith('.mp4'):
                optimize_m4a_faststart(self.sample_clip.path)

    def generate_test_alert(self, custom_message: Optional[str] = None) -> "VoiceAlert":
        """Generate a persistent test VoiceAlert using this specific voice profile."""
        full_name = getattr(self.user, "full_name", None) or self.user.get_username()
        msg = custom_message or (
            f"Testing voice alert system. This is voice profile {self.name} for user {full_name}. "
            f"All quality control operations are functioning nominally."
        )
        return VoiceAlert.generate_persistent_voice_alert(
            message=msg,
            title=f"Voice Test - {self.name}",
            forced_voice=self,
        )

    @classmethod
    def get_next_voice(cls) -> Optional["VoiceOver"]:
        """
        Fair 1:1 ratio scheduler.
        Interchangeably alternates between the two active voices with exact 50/50 balance.
        Selects the active voice that has been used fewer times (lowest usage_count).
        """
        with transaction.atomic():
            active_voices = list(
                cls.objects.select_for_update()
                .filter(is_active=True)
                .order_by("usage_count", "id")[:2]
            )
            if not active_voices:
                return None
            # Reserve the least-used profile while holding both rows so
            # concurrent workers preserve the configured alternation.
            voice = active_voices[0]
            cls.objects.filter(pk=voice.pk).update(
                usage_count=F("usage_count") + 1,
                last_voiced_at=timezone.now(),
            )
            voice.refresh_from_db(fields=["usage_count", "last_voiced_at"])
            return voice

    def record_usage(self):
        """Atomically increment usage counter for 1:1 fairness tracking."""
        VoiceOver.objects.filter(pk=self.pk).update(
            usage_count=F("usage_count") + 1,
            last_voiced_at=timezone.now()
        )
        self.refresh_from_db(fields=["usage_count", "last_voiced_at"])


# ============================================================
# 🔊 PERSISTENT VOICE ALERT MODEL
# ============================================================
class VoiceAlert(models.Model):
    """
    Persistent voice alert record accompanying every notification.
    Generated using the configured VoiceOver profiles in a 1:1 interchangeable ratio.
    """
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    source_key = models.CharField(max_length=255, unique=True, null=True, blank=True)
    voice_over = models.ForeignKey(
        VoiceOver,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="generated_voice_alerts",
        help_text="The VoiceOver profile used to voice this alert."
    )
    speaker_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="voiced_alerts",
        help_text="The accounts user whose voice identity generated this alert."
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
    voice_cloned = models.BooleanField(
        default=False,
        help_text="True only when synthesis used this VoiceOver sample as a reference."
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
        speaker = getattr(self.speaker_user, "username", "System Voice")
        return f"VoiceAlert [{speaker}]: {self.title or self.message[:40]}"

    @property
    def profile_audio_ready(self):
        return bool(self.voice_over_id and self.audio_file)

    @classmethod
    def generate_persistent_voice_alert(
        cls,
        message: str,
        title: str = "",
        alert_config: Optional[AlertConfig] = None,
        alert_state: Optional[AlertState] = None,
        forced_voice: Optional["VoiceOver"] = None,
        source_key: Optional[str] = None,
    ) -> Optional["VoiceAlert"]:
        """
        Generates and persistently saves a custom voice alert for a notification,
        fairly selecting from the two VoiceOver profiles in a 1:1 ratio.
        """
        existing = cls.objects.filter(source_key=source_key).first() if source_key else None
        if existing and existing.profile_audio_ready:
            return existing

        voice = forced_voice or VoiceOver.get_next_voice()
        if voice is None:
            logger.error("[VoiceAlert] No active VoiceOver profile is configured; alert audio was not generated.")
            return None
        speaker = voice.user
        accent = voice.accent_profile
        rate = voice.speech_rate

        # Clone the selected VoiceOver profile and synthesize the notification text using TTS
        audio_bytes, voice_cloned = synthesize_alert_audio(
            message=message,
            voice_over=voice,
            accent=accent,
            speech_rate=rate,
        )

        if not audio_bytes:
            logger.warning("[VoiceAlert] No audio bytes generated for message.")
            return None

        # Choose the persistent file extension from the container signature.
        alert_uuid = existing.uuid if existing else uuid.uuid4()
        is_wav = audio_bytes.startswith(b"RIFF") and audio_bytes[8:12] == b"WAVE"
        extension = "wav" if is_wav else "mp3"
        filename = f"voice_alert_{alert_uuid.hex[:12]}.{extension}"

        # Create persistent VoiceAlert
        voice_alert = existing or cls(uuid=alert_uuid, source_key=source_key)
        previous_audio_name = voice_alert.audio_file.name if voice_alert.audio_file else ""
        voice_alert.voice_over = voice
        voice_alert.speaker_user = speaker
        voice_alert.alert_config = alert_config
        voice_alert.alert_state = alert_state
        voice_alert.title = title or (alert_config.alert_type if alert_config else "Alert Notification")
        voice_alert.message = message
        voice_alert.voice_cloned = voice_cloned
        voice_alert.duration_seconds = round(max(1.0, len(message.split()) * 0.4), 1)
        voice_alert.audio_file.save(filename, ContentFile(audio_bytes), save=False)
        try:
            with transaction.atomic():
                voice_alert.save()
        except IntegrityError:
            # Concurrent retries may both synthesize before either insert
            # wins the unique source_key constraint. Keep one file and row.
            if source_key:
                voice_alert.audio_file.delete(save=False)
                existing = cls.objects.filter(source_key=source_key).first()
                if existing and existing.audio_file:
                    return existing
            raise

        if previous_audio_name and previous_audio_name != voice_alert.audio_file.name:
            try:
                voice_alert.audio_file.storage.delete(previous_audio_name)
            except Exception as storage_error:
                logger.warning("[VoiceAlert] Could not remove replaced audio file: %s", storage_error)

        # Forced profiles (such as the admin test action) do not pass through
        # the round-robin reservation above.
        if voice and forced_voice:
            voice.record_usage()

        logger.info(f"[VoiceAlert] Created persistent voice alert {voice_alert.uuid} (voice={voice})")
        return voice_alert

