"""Authenticated, per-user alert delivery over WebSockets."""

from __future__ import annotations

import json
import logging

from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.utils import timezone

logger = logging.getLogger(__name__)


class AlertConsumer(AsyncJsonWebsocketConsumer):
    """Deliver only to the authenticated recipient's private alert group.

    Acknowledgment remains on the HTTP endpoint, where the request can be
    revalidated against the user's plant and alert permissions.
    """

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4001)
            return

        route = self.scope.get("url_route") or {}
        route_kwargs = route.get("kwargs") or {}
        requested_user_id = route_kwargs.get("user_id")
        if requested_user_id and str(requested_user_id) != str(user.pk):
            await self.close(code=4003)
            return

        self.user = user
        self.user_group = f"user_{user.pk}_alerts"
        await self.channel_layer.group_add(self.user_group, self.channel_name)
        await self.accept()
        await self.send_json({
            "type": "connection.ready",
            "channel": "user-alerts",
            "timestamp": timezone.now().isoformat(),
        })

    async def disconnect(self, close_code):
        group = getattr(self, "user_group", None)
        if group:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            payload = json.loads(text_data)
        except (TypeError, json.JSONDecodeError):
            await self.send_json({"type": "error", "error": "Invalid JSON."})
            return

        if payload.get("type") == "ping":
            await self.send_json({"type": "pong", "timestamp": timezone.now().isoformat()})
        elif payload.get("type") == "acknowledge":
            alert_id = payload.get("id") or payload.get("alert_id")
            if alert_id:
                from asgiref.sync import sync_to_async
                from alerts.engine import AlertEngine
                raw_id = str(alert_id).replace("state_", "")
                res = await sync_to_async(AlertEngine.acknowledge_alert)(self.user, raw_id)
                await self.send_json({
                    "type": "acknowledge_result",
                    "id": alert_id,
                    "success": res.get("success", False),
                    "message": res.get("message", res.get("error", "")),
                })
            else:
                await self.send_json({"type": "error", "error": "Missing alert id to acknowledge."})

    async def alert_message(self, event):
        """Send a private alert hint; the client fetches its scoped feed."""
        await self.send_json({
            "type": "alert",
            "id": event.get("id"),
            "title": event.get("title") or "System Notification",
            "message": event.get("message"),
            "voice_url": event.get("voice_url"),
            "voice_profile": event.get("voice_profile"),
            "priority": event.get("priority", "medium"),
            "alert_type": event.get("alert_type", "general"),
            "instance_ref": event.get("instance_ref"),
            "auto_voice": event.get("auto_voice", True),
            "repeat_voice": event.get("repeat_voice", False),
            "timestamp": event.get("timestamp") or timezone.now().isoformat(),
        })

    async def alert_state(self, event):
        """Send alert event to client so bell badge updates and voice plays immediately."""
        await self.send_json({
            "type": "alert",
            "id": event.get("id"),
            "title": event.get("title") or "System Alert",
            "message": event.get("message"),
            "voice_url": event.get("voice_url"),
            "priority": event.get("priority", "medium"),
            "alert_type": event.get("alert_type", "state_change"),
            "instance_ref": event.get("instance_ref"),
            "state": event.get("state"),
            "auto_voice": event.get("auto_voice", True),
            "repeat_voice": event.get("repeat_voice", False),
            "voice_profile": event.get("voice_profile"),
            "timestamp": event.get("timestamp") or timezone.now().isoformat(),
        })

    async def voice_alert(self, event):
        """Send persistent voice alert with audio URL for audio playback."""
        audio_url = event.get("audio_url") or event.get("voice_url")
        await self.send_json({
            "type": "voice_alert",
            "id": event.get("id"),
            "audio_url": audio_url,
            "voice_url": audio_url,
            "message": event.get("message"),
            "title": event.get("title") or "Voice Alert",
            "speaker": event.get("speaker"),
            "voice_profile": event.get("voice_profile"),
            "auto_voice": event.get("auto_voice", True),
            "repeat_voice": event.get("repeat_voice", False),
            "timestamp": event.get("timestamp") or timezone.now().isoformat(),
        })

    async def alert_acknowledged(self, event):
        """Send acknowledgment signal so clients remove the badge and silence repeating audio."""
        await self.send_json({
            "type": "alert_acknowledged",
            "id": event.get("id"),
            "alert_state_id": event.get("alert_state_id"),
            "timestamp": event.get("timestamp") or timezone.now().isoformat(),
        })

