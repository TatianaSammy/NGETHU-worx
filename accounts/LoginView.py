import logging
from django.shortcuts import render, redirect
from django.conf import settings
from django.contrib.auth import authenticate, login as auth_login, get_user_model
from django.utils import timezone
from django.middleware.csrf import get_token
from django.views import View
from django.http import JsonResponse
from .utils import get_user_landing_url


logger = logging.getLogger(__name__)
User = get_user_model()


class LoginView(View):
    template_name = "accounts/login.html"

    max_failed_attempts = getattr(settings, "MAX_FAILED_LOGIN_ATTEMPTS", 5)

    def dispatch(self, request, *args, **kwargs):
        self.group_map = self._load_group_map()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        get_token(request)  # 🔥 FORCE CSRF COOKIE

        if request.user.is_authenticated:
            return redirect(get_user_landing_url(request.user))

        reason = request.GET.get("reason", "")
        error_msg = ""
        feedback_level = "info"
        if reason == "logged_out":
            error_msg = "Your session has ended. Sign in again to continue."

        response = render(request, self.template_name, {
            "error": error_msg,
            "feedback_level": feedback_level,
        })
        return self._disable_login_caching(response)

    def post(self, request):
        is_ajax = self._is_ajax(request)

        username = (request.POST.get("username") or "").strip().upper()
        password = request.POST.get("password") or ""

        if not username or not password:
            return self._fail(
                request,
                username,
                "Username and password required",
                is_ajax,
                severity="warning",
            )

        user = authenticate(request, username=username, password=password)

        if user is None:
            return self._handle_failed_login(request, username, is_ajax)

        validation_error = self._validate_user(user)
        if validation_error:
            return self._fail(
                request, username, validation_error, is_ajax, severity="warning"
            )

        auth_login(request, user)
        self._initialize_session(request, user)

        request.session.save()

        redirect_url = get_user_landing_url(user)

        logger.info(
            "LOGIN_SUCCESS user=%s ip=%s groups=%s",
            username,
            self._ip(request),
            self._validated_group_names,
        )

        if is_ajax:
            response = JsonResponse({
                "success": True,
                "redirect": redirect_url
            })
            return self._disable_login_caching(response)

        return self._disable_login_caching(redirect(redirect_url))
    def _initialize_session(self, request, user):
        request.session.set_expiry(settings.SESSION_COOKIE_AGE)
        request.session.update({
            "user_id": user.pk,
            "username": user.username,
            "login_ts": timezone.now().timestamp(),
            "primary_group": self._primary_group(user),
        })
        user.failed_login_attempts = 0
        user.is_locked = False
        user.save(update_fields=["failed_login_attempts", "is_locked"])


    def _handle_failed_login(self, request, username, is_ajax):
        error = "Invalid credentials"

        try:
            user = User.objects.get(username=username)

            user.increment_failed_login_attempts()

            if user.failed_login_attempts >= self.max_failed_attempts:
                user.is_locked = True
                user.save(update_fields=["is_locked"])
                error = "Account locked due to repeated attempts"

        except User.DoesNotExist:
            pass

        logger.warning("LOGIN_FAILED user=%s ip=%s", username, self._ip(request))

        return self._fail(request, username, error, is_ajax)

    def _validate_user(self, user):
        if user is None:
            return "Authentication failed"

        try:
            if getattr(user, "is_locked", False):
                logger.warning(
                    "VALIDATION_LOCKED user=%s",
                    user.username,
                )
                return "Account locked"

            if not user.is_active:
                logger.warning(
                    "VALIDATION_INACTIVE user=%s",
                    user.username,
                )
                return "Account inactive"

            if hasattr(user, "is_password_expired"):
                try:
                    if user.is_password_expired():
                        logger.warning(
                            "VALIDATION_PASSWORD_EXPIRED user=%s",
                            user.username,
                        )
                        return "Password expired"
                except Exception as exc:
                    logger.exception(
                        "PASSWORD_EXPIRY_CHECK_ERROR user=%s error=%s",
                        user.username,
                        exc,
                    )

            groups = self._groups(user)
            self._validated_group_names = groups

            if user.is_superuser:
                logger.info(
                    "VALIDATION_SUPERUSER user=%s",
                    user.username,
                )
                return None

            if not groups:
                logger.warning(
                    "VALIDATION_NO_GROUPS user=%s",
                    user.username,
                )
                return "No group assigned"

            matched_groups = [
                g for g in groups
                if g in self.group_map
            ]

            if not matched_groups:
                logger.warning(
                    "VALIDATION_NO_MAPPING user=%s groups=%s",
                    user.username,
                    groups,
                )
                return "No dashboard mapping found"

            logger.info(
                "VALIDATION_SUCCESS user=%s groups=%s",
                user.username,
                matched_groups,
            )

            return None

        except Exception as exc:
            logger.exception(
                "VALIDATION_ERROR user=%s error=%s",
                getattr(user, "username", "unknown"),
                exc,
            )
            return "Unable to validate account"

    def _groups(self, user):
        try:
            groups = [
                str(name).strip().upper()
                for name in user.groups.values_list(
                    "name",
                    flat=True,
                )
            ]

            return sorted(
                list(set(groups))
            )

        except Exception as exc:
            logger.exception(
                "GROUP_FETCH_ERROR user=%s error=%s",
                getattr(
                    user,
                    "username",
                    "unknown",
                ),
                exc,
            )
            return []

    def _primary_group(self, user):
        groups = getattr(self, "_validated_group_names", None)
        if groups is None:
            groups = self._groups(user)

        return (
            groups[0]
            if groups
            else "GENERAL"
        )

    def _load_group_map(self):
        raw = getattr(
            settings,
            "LOGIN_GROUP_REDIRECT_MAP",
            {},
        ) or {}

        normalized = {}

        for group, target in raw.items():
            group_key = (
                str(group)
                .strip()
                .upper()
            )

            target_value = (
                str(target).strip()
                if target
                else ""
            )

            if not target_value:
                logger.warning(
                    "GROUP_MAPPING_SKIPPED group=%s",
                    group_key,
                )
                continue

            normalized[group_key] = target_value

        logger.info(
            "GROUP_MAPPINGS_LOADED count=%s",
            len(normalized),
        )

        return normalized

    def _fail(self, request, username, error, is_ajax, severity="error"):
        error_text = str(error)
        severity = severity if severity in {"error", "warning", "info"} else "error"

        logger.warning(
            "LOGIN_ERROR user=%s ip=%s error=%s",
            username,
            self._ip(request),
            error_text,
        )

        payload = {
            "success": False,
            "error": error_text,
            "severity": severity,
            "timestamp": timezone.now().isoformat(),
        }

        if is_ajax:
            response = JsonResponse(
                payload,
                status=403,
            )
            return self._disable_login_caching(response)

        response = render(
            request,
            self.template_name,
            {
                "username": "",
                "error": error_text,
                "feedback_level": severity,
            },
            status=403,
        )
        return self._disable_login_caching(response)

    @staticmethod
    def _disable_login_caching(response):
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response

    def _is_ajax(self, request):
        return (
                request.headers.get(
                    "X-Requested-With"
                )
                == "XMLHttpRequest"
        )

    def _ip(self, request):
        xff = request.META.get(
            "HTTP_X_FORWARDED_FOR"
        )

        if xff:
            return (
                xff.split(",")[0]
                .strip()
            )

        x_real_ip = request.META.get(
            "HTTP_X_REAL_IP"
        )

        if x_real_ip:
            return x_real_ip.strip()

        return (
                request.META.get(
                    "REMOTE_ADDR"
                )
                or "unknown"
        )
