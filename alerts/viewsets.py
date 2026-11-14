"""DRF ViewSets for the alerts app."""

from __future__ import annotations

import logging
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.db.models import Q

from core.engine.super_user import SuperUserEngine
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
from .serializers import (
    AlertTemplateSerializer,
    AlertCategorySerializer,
    AlertConfigSerializer,
    AlertStateSerializer,
    AlertNotificationSerializer,
    VoiceAlertSerializer,
    AlarmAcknowledgmentSerializer,
    AlertLogSerializer,
    UserAlertPreferenceSerializer,
)
from .engine import AlertEngine

logger = logging.getLogger(__name__)


class AlertNotificationViewSet(viewsets.ModelViewSet):
    """Inbox and outbox records for alert notifications."""

    serializer_class = AlertNotificationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = AlertNotification.objects.select_related(
            "recipient",
            "alert_state",
            "alert_state__alert_config",
        )
        if not SuperUserEngine.is_superuser(user):
            qs = qs.filter(recipient=user)
        status_filter = self.request.query_params.get("delivery_status")
        if status_filter:
            qs = qs.filter(delivery_status=status_filter)
        unread_only = self.request.query_params.get("unread")
        if unread_only and unread_only.lower() in ("true", "1"):
            qs = qs.filter(acknowledged_at__isnull=True)
        return qs.order_by("-created_at")

    @action(detail=True, methods=["post"])
    def acknowledge(self, request, pk=None):
        notification = self.get_object()
        now = timezone.now()
        notification.acknowledged_at = now
        notification.save(update_fields=["acknowledged_at", "updated_at"])

        # Also delegate to the central engine to ensure state, logs, and repeats update
        engine_res = AlertEngine.acknowledge_alert(
            user=request.user,
            alert_state_id=notification.alert_state_id,
            notes=request.data.get("notes"),
        )
        return Response({
            "success": True,
            "notification_id": notification.pk,
            "engine_result": engine_res,
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        notification = self.get_object()
        from .tasks import deliver_alert_notification
        deliver_alert_notification.delay(notification.pk)
        return Response({
            "success": True,
            "message": f"Delivery retried for notification {notification.pk}.",
        }, status=status.HTTP_200_OK)


class AlertStateViewSet(viewsets.ModelViewSet):
    """Lifecycle and escalation states of alerts."""

    serializer_class = AlertStateSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = AlertState.objects.select_related(
            "alert_config",
            "alert_config__template",
            "alert_config__category",
        ).prefetch_related("notifications")
        if not SuperUserEngine.is_superuser(user):
            user_plant = getattr(user, "assigned_treatment_plant_id", None)
            if user_plant:
                qs = qs.filter(
                    Q(geo_context__plant_id=str(user_plant))
                    | Q(geo_context__plant_id__isnull=True)
                    | Q(notifications__recipient=user)
                ).distinct()
            else:
                qs = qs.filter(notifications__recipient=user).distinct()
        state_filter = self.request.query_params.get("state")
        if state_filter:
            qs = qs.filter(state=state_filter)
        priority_filter = self.request.query_params.get("priority")
        if priority_filter:
            qs = qs.filter(priority=priority_filter)
        return qs.order_by("-created_at")

    @action(detail=True, methods=["post"])
    def acknowledge(self, request, pk=None):
        state = self.get_object()
        result = AlertEngine.acknowledge_alert(
            user=request.user,
            alert_state_id=state.pk,
            notes=request.data.get("notes"),
        )
        status_code = status.HTTP_200_OK if result.get("success") else status.HTTP_400_BAD_REQUEST
        return Response(result, status=status_code)

    @action(detail=True, methods=["post"])
    def escalate(self, request, pk=None):
        state = self.get_object()
        task_id = AlertEngine.escalate_state(state.pk, force=True)
        return Response({
            "success": True,
            "message": f"Escalation dispatched for alert state {state.pk}.",
            "task_id": str(task_id),
        }, status=status.HTTP_200_OK)


class AlertConfigViewSet(viewsets.ModelViewSet):
    """Rules and threshold configurations for automated alerts."""

    serializer_class = AlertConfigSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = AlertConfig.objects.select_related(
        "category",
        "template",
        "escalate_to",
    ).prefetch_related(
        "recipients",
        "target_sections",
        "target_groups",
        "target_treatment_plants",
    ).order_by("alert_type")


class AlertTemplateViewSet(viewsets.ModelViewSet):
    """Templates defining alert subject, body, and voiceover settings."""

    serializer_class = AlertTemplateSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = AlertTemplate.objects.all().order_by("name")


class AlertCategoryViewSet(viewsets.ModelViewSet):
    """Classification taxonomy for alert streams."""

    serializer_class = AlertCategorySerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = AlertCategory.objects.select_related("parent").order_by("name")


class VoiceAlertViewSet(viewsets.ModelViewSet):
    """Persistent synthesized voice alerts."""

    serializer_class = VoiceAlertSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = VoiceAlert.objects.select_related(
        "alert_config",
        "alert_state",
        "speaker_user",
    ).order_by("-created_at")

    @action(detail=True, methods=["post"])
    def mark_played(self, request, pk=None):
        voice_alert = self.get_object()
        voice_alert.is_played = True
        voice_alert.play_count += 1
        voice_alert.save(update_fields=["is_played", "play_count"])
        return Response({
            "success": True,
            "is_played": True,
            "play_count": voice_alert.play_count,
        }, status=status.HTTP_200_OK)


class AlarmAcknowledgmentViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only audit trail of acknowledged alarms."""

    serializer_class = AlarmAcknowledgmentSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = AlarmAcknowledgment.objects.select_related("user", "content_type").order_by("-timestamp")


class AlertLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only audit log of alert triggers and actions."""

    serializer_class = AlertLogSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = AlertLog.objects.select_related("alert", "content_type").order_by("-timestamp")


class UserAlertPreferenceViewSet(viewsets.ModelViewSet):
    """Per-user channel routing and notification preferences."""

    serializer_class = UserAlertPreferenceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if SuperUserEngine.is_superuser(user):
            return UserAlertPreference.objects.select_related("user").order_by("user__username")
        return UserAlertPreference.objects.filter(user=user).select_related("user")

    @action(detail=False, methods=["get", "post", "patch"])
    def my_preferences(self, request):
        pref, _ = UserAlertPreference.objects.get_or_create(user=request.user)
        if request.method in ["POST", "PATCH"]:
            serializer = self.get_serializer(pref, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)
        serializer = self.get_serializer(pref)
        return Response(serializer.data, status=status.HTTP_200_OK)
