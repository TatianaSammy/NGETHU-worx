from datetime import date, datetime, time, timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.views import View
from django.utils import timezone
from django.utils.dateparse import parse_datetime


class DashboardNotificationsView(LoginRequiredMixin, View):
    """Shared dashboard notification feed with enforced plant scope."""

    def get(self, request, *args, **kwargs):
        user = request.user
        from core.engine.super_user import SuperUserEngine
        is_superuser = SuperUserEngine.is_superuser(user)
        plant = self._resolve_plant(request, is_superuser)
        if isinstance(plant, JsonResponse):
            return plant

        alerts = []
        if plant is not None:
            from ca.services.alerts_service import ChemicalAlertsService

            alerts = ChemicalAlertsService.get_user_alerts(
                user=user,
                plant=plant,
                is_superuser=is_superuser,
                limit=25,
            )

        now = timezone.now()
        for alert in alerts:
            alert["urgency"] = self._urgency(alert, now)
            alert_type = str(alert.get("type", ""))

            # Read already-persisted audio only. The feed endpoint must not
            # create voice jobs on every poll; alert lifecycle hooks queue
            # idempotent audio generation when the event is first created.
            alert["voice_url"] = None
            alert["voice_profile"] = ""
            alert["auto_voice"] = False
            try:
                from alerts.models import VoiceAlert

                voices = VoiceAlert.objects.select_related(
                    "voice_over",
                    "alert_config",
                    "alert_config__template",
                )
                if alert_type == "alert_state" and alert.get("db_id"):
                    voice = voices.filter(
                        alert_state_id=alert["db_id"],
                    ).exclude(audio_file="").order_by("-created_at").first()
                elif alert_type == "ca_workflow_alert":
                    alert_id = str(alert.get("id", ""))
                    source_id = alert_id.removeprefix("ca_workflow_alert_")
                    voice = voices.filter(
                        source_key=f"ca-workflow:{source_id}",
                    ).exclude(audio_file="").first()
                else:
                    voice = None
                if voice and voice.profile_audio_ready:
                    alert["voice_url"] = voice.audio_file.url
                    alert["voice_profile"] = voice.voice_over.name
                    config = voice.alert_config
                    alert["auto_voice"] = bool(
                        not config
                        or (
                            config.active
                            and (
                                not config.template_id
                                or config.template.active and config.template.auto_voice_enabled
                            )
                        )
                    )
            except Exception:
                pass

            if alert_type == "ca_workflow_alert":
                # Durable CA workflow alerts carry a per-recipient decision
                # from the domain service; keep it so only assigned analysts
                # receive an acknowledgement action.
                alert["can_acknowledge"] = bool(alert.get("can_acknowledge"))
            else:
                alert["can_acknowledge"] = alert_type in {
                    "alert_state",
                    "qc_nonconformity",
                    "ca_batch_received",
                    "ca_sla",
                    "ca_out_of_spec",
                    "ca_coa",
                }

        unread = sum(not bool(alert.get("acknowledged")) for alert in alerts)
        response = JsonResponse({
            "success": True,
            "alerts": alerts,
            "unread_count": unread,
            "plant_id": getattr(plant, "pk", None),
            "timestamp": now.isoformat(),
        })
        response["Cache-Control"] = "no-store, max-age=0"
        return response

    @staticmethod
    def _resolve_plant(request, is_superuser):
        from t.models import TreatmentPlant

        if not is_superuser:
            plant = getattr(request.user, "assigned_treatment_plant", None)
            requested = request.GET.get("plant")
            if requested and str(getattr(plant, "pk", "")) != str(requested):
                return JsonResponse({"success": False, "error": "Notification scope is outside your assigned plant."}, status=403)
            return plant or TreatmentPlant.objects.first()

        requested = request.GET.get("plant") or next(
            (
                request.session.get(key)
                for key in (
                    "ca_selected_plant",
                    "qc_selected_plant",
                    "selected_plant_id",
                    "selected_treatment_plant_id",
                    "active_plant_id",
                    "plant_id",
                    "treatment_plant_id",
                )
                if request.session.get(key)
            ),
            None,
        )
        if not requested:
            return getattr(request.user, "assigned_treatment_plant", None) or TreatmentPlant.objects.first()
        if str(requested).casefold() in {"all", "none"}:
            return TreatmentPlant.objects.first()
        if not str(requested).isdigit():
            return TreatmentPlant.objects.first()
        plant = TreatmentPlant.objects.filter(pk=int(requested)).first()
        if plant is None:
            return TreatmentPlant.objects.first()
        return plant

    @staticmethod
    def _urgency(alert, now):
        text = " ".join(str(alert.get(key, "")) for key in ("title", "message", "type")).casefold()
        if any(word in text for word in ("expired", "overdue", "missed", "nonconformity", "out-of-spec", "out of spec", "failed", "rejected")):
            return "expired"
        if any(word in text for word in ("expiring", "due soon")):
            return "almost-expiring"

        severity = str(alert.get("severity", alert.get("priority", ""))).casefold()
        if severity == "critical":
            return "expired"
        if severity == "high":
            return "almost-expiring"

        timestamp = alert.get("timestamp")
        if isinstance(timestamp, str):
            timestamp = parse_datetime(timestamp)
        if isinstance(timestamp, date) and not isinstance(timestamp, datetime):
            timestamp = datetime.combine(timestamp, time.min)
        if timestamp:
            if timezone.is_naive(timestamp):
                timestamp = timezone.make_aware(timestamp, timezone.get_current_timezone())
            if timestamp >= now - timedelta(minutes=60) and not alert.get("acknowledged"):
                return "new"
        if severity == "low":
            return "new"
        return "average"


class DashboardNotificationAckView(LoginRequiredMixin, View):
    """Acknowledge a shared notification after rechecking its scope and access."""

    def post(self, request, alert_id, *args, **kwargs):
        from ca.services.alerts_service import ChemicalAlertsService
        from core.engine.super_user import SuperUserEngine

        is_superuser = SuperUserEngine.is_superuser(request.user)
        plant = DashboardNotificationsView._resolve_plant(request, is_superuser)
        if isinstance(plant, JsonResponse) or plant is None:
            return JsonResponse({"success": False, "error": "Notification not found."}, status=404)

        scoped_alerts = ChemicalAlertsService.get_user_alerts(
            user=request.user,
            plant=plant,
            is_superuser=is_superuser,
            limit=250,
        )
        if not any(str(item.get("id")) == str(alert_id) for item in scoped_alerts):
            return JsonResponse({"success": False, "error": "Notification not found."}, status=404)

        result = ChemicalAlertsService.acknowledge(
            request.user,
            str(alert_id),
            is_superuser=is_superuser,
        )
        response = JsonResponse(result, status=200 if result.get("success") else 404)
        response["Cache-Control"] = "no-store, max-age=0"
        return response
