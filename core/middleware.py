from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import resolve_url
from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.utils.deprecation import MiddlewareMixin

from core.engine.superuser_authorization import (
    bind_request_user,
    reset_request_user,
)


class PersistentSessionMiddleware(MiddlewareMixin):
    """Apply the configured long session lifetime to authenticated sessions."""

    def process_request(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            request.session.set_expiry(settings.SESSION_COOKIE_AGE)


class SuperuserAuthorizationContextMiddleware:
    """Scope elevated permissions and hard-delete behavior to one request."""

    def __init__(self, get_response):
        self.get_response = get_response
        self._is_async = iscoroutinefunction(get_response)
        if self._is_async:
            markcoroutinefunction(self)

    def __call__(self, request):
        if self._is_async:
            return self.__acall__(request)
        return self.__scall__(request)

    def _bind(self, request):
        token = bind_request_user(getattr(request, "user", None))
        user = getattr(request, "user", None)
        path_parts = [
            part.casefold()
            for part in request.path_info.strip("/").split("/")
            if part
        ]
        deletion_markers = {"delete", "remove", "purge", "destroy", "erase"}
        deletion_intent = (
            request.method.upper() == "DELETE"
            or any(
                part == marker or part.startswith((marker + "-", marker + "_"))
                for part in path_parts
                for marker in deletion_markers
            )
        )
        if (
            deletion_intent
            and getattr(user, "is_authenticated", False)
            and not (
                getattr(user, "is_active", False)
                and getattr(user, "is_superuser", False)
            )
        ):
            from django.http import HttpResponseForbidden

            return token, HttpResponseForbidden(
                "Only an active superuser can delete records."
            )
        return token, None

    def __scall__(self, request):
        token, denied = self._bind(request)
        try:
            if denied is not None:
                return denied
            return self.get_response(request)
        finally:
            reset_request_user(token)

    async def __acall__(self, request):
        token, denied = self._bind(request)
        try:
            if denied is not None:
                return denied
            return await self.get_response(request)
        finally:
            reset_request_user(token)


class AnonymousForbiddenRedirectMiddleware:
    """Send anonymous users to login when a protected endpoint returns 403."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.status_code != 403:
            return response

        user = getattr(request, "user", None)
        if user is None or user.is_authenticated:
            return response

        login_url = resolve_url(settings.LOGIN_URL)
        if request.path_info in {login_url, "/login/"}:
            return response

        return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)


class SingleDeviceSessionMiddleware:
    """Deprecated compatibility middleware; concurrent sessions are allowed."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)


class NetworkOnlyResponseMiddleware:
    """Keep dynamic application responses out of browser and intermediary caches."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        static_prefix = getattr(settings, "STATIC_URL", "/static/")
        if static_prefix and request.path_info.startswith(static_prefix):
            return response

        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response

