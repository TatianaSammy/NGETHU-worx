"""DRF Serializers for the alerts app."""

from __future__ import annotations

from rest_framework import serializers
from .models import (
    AlertTemplate,
    AlertCategory,
    AlertConfig,
    AlertState,
    AlertNotification,
    VoiceAlert,
    AlarmAcknowledgment,
    AlertLog,
    UserAlertPreference,
)


class AlertTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = AlertTemplate
        fields = [
            "id",
            "name",
            "version",
            "subject_template",
            "body_template",
            "active",
            "auto_voice_enabled",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class AlertCategorySerializer(serializers.ModelSerializer):
    parent_name = serializers.CharField(source="parent.name", read_only=True)

    class Meta:
        model = AlertCategory
        fields = [
            "id",
            "name",
            "parent",
            "parent_name",
            "description",
            "ai_cluster_code",
        ]
        read_only_fields = ["id"]


class AlertConfigSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    template_name = serializers.CharField(source="template.name", read_only=True)

    class Meta:
        model = AlertConfig
        fields = [
            "id",
            "alert_type",
            "priority",
            "category",
            "category_name",
            "template",
            "template_name",
            "filter_conditions",
            "escalate_to",
            "allow_ai_recipients",
            "ai_recommended_recipients",
            "verify_timeout",
            "verify_before_subsequent",
            "approve_timeout",
            "tat_for_escalation",
            "escalation_timeout",
            "escalation_policy",
            "voice_persistence_mode",
            "voice_repeat_interval_seconds",
            "voice_repeat_max_count",
            "active",
            "last_triggered",
            "last_escalation",
            "recipients",
            "target_sections",
            "target_groups",
            "target_treatment_plants",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "last_triggered", "last_escalation", "created_at", "updated_at"]


class AlertNotificationSerializer(serializers.ModelSerializer):
    recipient_username = serializers.CharField(source="recipient.username", read_only=True)
    alert_type = serializers.CharField(source="alert_state.alert_config.alert_type", read_only=True)

    class Meta:
        model = AlertNotification
        fields = [
            "id",
            "alert_state",
            "alert_type",
            "recipient",
            "recipient_username",
            "sequence",
            "title",
            "message",
            "priority",
            "is_escalation",
            "channels",
            "delivery_status",
            "delivery_results",
            "attempts",
            "last_attempt_at",
            "next_attempt_at",
            "delivered_at",
            "acknowledged_at",
            "last_error",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "attempts",
            "last_attempt_at",
            "next_attempt_at",
            "delivered_at",
            "delivery_results",
            "last_error",
            "created_at",
            "updated_at",
        ]


class AlertStateSerializer(serializers.ModelSerializer):
    alert_type = serializers.CharField(source="alert_config.alert_type", read_only=True)
    notifications_count = serializers.IntegerField(source="notifications.count", read_only=True)
    active_notifications = AlertNotificationSerializer(source="notifications", many=True, read_only=True)

    class Meta:
        model = AlertState
        fields = [
            "id",
            "alert_config",
            "alert_type",
            "instance_ref",
            "title",
            "message",
            "priority",
            "state",
            "geo_context",
            "escalation_due_at",
            "last_escalated_at",
            "escalation_count",
            "repeat_due_at",
            "repeat_count",
            "created_at",
            "updated_at",
            "notifications_count",
            "active_notifications",
        ]
        read_only_fields = [
            "id",
            "created_at",
            "updated_at",
            "notifications_count",
            "active_notifications",
            "repeat_due_at",
            "repeat_count",
        ]


class VoiceAlertSerializer(serializers.ModelSerializer):
    audio_file_url = serializers.SerializerMethodField()
    speaker_name = serializers.SerializerMethodField()

    class Meta:
        model = VoiceAlert
        fields = [
            "id",
            "uuid",
            "title",
            "message",
            "alert_config",
            "alert_state",
            "speaker_user",
            "speaker_name",
            "audio_file",
            "audio_file_url",
            "duration_seconds",
            "source_key",
            "is_played",
            "play_count",
            "repeat_due_at",
            "repeat_count",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "uuid",
            "audio_file_url",
            "speaker_name",
            "duration_seconds",
            "repeat_due_at",
            "repeat_count",
            "created_at",
        ]

    def get_audio_file_url(self, obj):
        return obj.audio_file.url if obj.audio_file else None

    def get_speaker_name(self, obj):
        if obj.speaker_user:
            return getattr(obj.speaker_user, "full_name", None) or obj.speaker_user.get_username()
        return "System Voice"


class AlarmAcknowledgmentSerializer(serializers.ModelSerializer):
    user_username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = AlarmAcknowledgment
        fields = [
            "id",
            "user",
            "user_username",
            "content_type",
            "object_id",
            "acknowledged",
            "verified",
            "sentiment_score",
            "acknowledged_at",
            "timestamp",
        ]
        read_only_fields = ["id", "timestamp"]


class AlertLogSerializer(serializers.ModelSerializer):
    alert_type = serializers.CharField(source="alert.alert_type", read_only=True)

    class Meta:
        model = AlertLog
        fields = [
            "id",
            "alert",
            "alert_type",
            "content_type",
            "object_id",
            "details",
            "timestamp",
        ]
        read_only_fields = ["id", "timestamp"]


class UserAlertPreferenceSerializer(serializers.ModelSerializer):
    user_username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = UserAlertPreference
        fields = [
            "id",
            "user",
            "user_username",
            "receive_email",
            "receive_sms",
            "receive_push",
            "receive_whatsapp",
            "receive_slack",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
