# alerts/admin.py
import os
from django.contrib import admin
from django.utils.html import format_html
from django.urls import path, reverse
from django.shortcuts import redirect, get_object_or_404
from django.contrib import messages
from .models import (
    VoiceAlert,
    AlertTemplate,
    AlertCategory,
    AlertConfig,
    AlertState,
    AlertNotification,
    AlertLog,
    UserAlertPreference,
)


def get_audio_mime_types(file_field):
    """Return list of appropriate MIME types for the audio element source tags."""
    if not file_field or not getattr(file_field, "name", None):
        return ["audio/mpeg"]
    ext = os.path.splitext(file_field.name.lower())[1]
    if ext in [".m4a", ".mp4"]:
        return ["audio/mp4", "audio/x-m4a", "audio/aac"]
    elif ext == ".wav":
        return ["audio/wav", "audio/x-wav"]
    elif ext == ".ogg":
        return ["audio/ogg"]
    elif ext == ".aac":
        return ["audio/aac"]
    return ["audio/mpeg"]


@admin.register(VoiceAlert)
class VoiceAlertAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "speaker_display",
        "duration_seconds",
        "repeat_count",
        "repeat_due_at",
        "audio_player",
        "created_at",
    )
    list_filter = ("created_at",)
    search_fields = (
        "title",
        "message",
        "speaker_user__username",
        "speaker_user__first_name",
        "speaker_user__last_name",
    )
    readonly_fields = (
        "uuid",
        "audio_player",
        "repeat_due_at",
        "repeat_count",
        "created_at",
    )
    fieldsets = (
        ("Alert Details", {
            "fields": ("uuid", "title", "message", "alert_config", "alert_state"),
        }),
        ("Voice Identity & Playback", {
            "fields": ("speaker_user", "audio_file", "audio_player", "duration_seconds"),
        }),
        ("Analytics", {
            "fields": ("is_played", "play_count", "repeat_due_at", "repeat_count", "created_at"),
        }),
    )

    def speaker_display(self, obj):
        if obj.speaker_user:
            return getattr(obj.speaker_user, "full_name", None) or obj.speaker_user.get_username()
        return "System Voice"
    speaker_display.short_description = "Speaker"

    def audio_player(self, obj):
        if obj.audio_file:
            url = obj.audio_file.url
            mime_types = get_audio_mime_types(obj.audio_file)
            sources_html = "".join([f'<source src="{url}" type="{m}">' for m in mime_types])
            sources_html += f'<source src="{url}">'
            return format_html(
                '<div style="display:inline-flex;align-items:center;gap:6px;">'
                '<audio controls preload="metadata" style="height:32px;width:220px;vertical-align:middle;" src="{}">'
                '{}'
                'Your browser does not support audio element.'
                '</audio>'
                '<a href="{}" target="_blank" download style="display:inline-flex;align-items:center;padding:3px 7px;border-radius:4px;background:#f3f4f6;color:#1f2937;font-size:11px;font-weight:600;text-decoration:none;border:1px solid #d1d5db;" title="Open or download audio file">'
                '&#128266; Open'
                '</a>'
                '</div>',
                url,
                format_html(sources_html),
                url,
            )
        return "No audio generated"
    audio_player.short_description = "Voice Alert Audio"


@admin.register(AlertTemplate)
class AlertTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "version", "active", "auto_voice_enabled")
    list_filter = ("active", "auto_voice_enabled")
    search_fields = ("name", "subject_template", "body_template")


@admin.register(AlertCategory)
class AlertCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "parent", "ai_cluster_code")
    search_fields = ("name", "ai_cluster_code")


@admin.register(AlertConfig)
class AlertConfigAdmin(admin.ModelAdmin):
    list_display = (
        "alert_type",
        "priority",
        "voice_persistence_mode",
        "voice_repeat_interval_seconds",
        "voice_repeat_max_count",
        "active",
        "last_triggered",
    )
    list_filter = ("alert_type", "priority", "voice_persistence_mode", "active")
    search_fields = ("alert_type",)
    filter_horizontal = ("recipients", "target_sections", "target_groups", "target_treatment_plants")


@admin.register(AlertState)
class AlertStateAdmin(admin.ModelAdmin):
    list_display = (
        "alert_config", "instance_ref", "priority", "state", "escalation_count",
        "repeat_count", "repeat_due_at", "escalation_due_at", "updated_at",
    )
    list_filter = ("state",)
    search_fields = ("instance_ref",)
    readonly_fields = ("repeat_due_at", "repeat_count", "created_at", "updated_at")


@admin.register(AlertNotification)
class AlertNotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "recipient", "alert_state", "sequence", "delivery_status", "attempts", "delivered_at", "acknowledged_at")
    list_filter = ("delivery_status", "is_escalation", "priority", "created_at")
    search_fields = ("title", "message", "recipient__username", "recipient__email", "alert_state__instance_ref")
    readonly_fields = ("created_at", "updated_at", "last_attempt_at", "delivered_at", "delivery_results")
    raw_id_fields = ("alert_state", "recipient")


@admin.register(AlertLog)
class AlertLogAdmin(admin.ModelAdmin):
    list_display = ("alert", "content_type", "object_id", "timestamp")
    list_filter = ("timestamp",)


@admin.register(UserAlertPreference)
class UserAlertPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "receive_email", "receive_sms", "receive_push", "receive_whatsapp", "receive_slack")
    list_filter = ("receive_email", "receive_sms", "receive_push", "receive_whatsapp", "receive_slack")
    search_fields = ("user__username", "user__email")
