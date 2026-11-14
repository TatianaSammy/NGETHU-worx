import logging
from django.views import View
from django.contrib.auth import logout as auth_logout
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.utils import timezone

logger = logging.getLogger(__name__)


class LogoutView(View):

    def post(self, request):
        if request.POST.get("action") == "window_close":
            return HttpResponse(status=204)

        user = request.user
        session_key = request.session.session_key
        username = getattr(user, "username", "unknown")
        ip = self._ip(request)
        ts = timezone.now().isoformat()

        if not user or not user.is_authenticated:
            return redirect(self._safe_redirect())

        # 1. Audit
        self._audit_logout(user, request, session_key, ip, ts)

        # End only this browser session. Other sessions for the same user
        # remain active on their own devices.
        auth_logout(request)

        logger.info(
            "LOGOUT_SUCCESS user=%s ip=%s session=%s time=%s",
            username,
            ip,
            session_key,
            ts,
        )

        return redirect(self._safe_redirect())

    def _audit_logout(self, user, request, session_key, ip, ts):
        try:
            logger.info(
                "AUDIT_LOGOUT | user=%s | id=%s | ip=%s | session=%s | time=%s | ua=%s",
                getattr(user, "username", "unknown"),
                getattr(user, "id", None),
                ip,
                session_key,
                ts,
                request.META.get("HTTP_USER_AGENT", ""),
            )
        except Exception:
            pass

    def _safe_redirect(self):
        return reverse_lazy("accounts:login")

    def _ip(self, request):
        xff = request.META.get("HTTP_X_FORWARDED_FOR")
        if xff:
            return xff.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "")

    def _invalidate_jwt_tokens(self, user):
        pass

    def _notify_websocket_clients(self, user):
        pass
