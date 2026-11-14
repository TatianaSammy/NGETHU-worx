from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .ViewDashboard import DashboardView
from core.views import (NAMUBUYA, fields_schema, registry_router)
from .viewsets import (
    AlertNotificationViewSet,
    AlertStateViewSet,
    AlertConfigViewSet,
    AlertTemplateViewSet,
    AlertCategoryViewSet,
    VoiceAlertViewSet,
    AlarmAcknowledgmentViewSet,
    AlertLogViewSet,
    UserAlertPreferenceViewSet,
)

app_name = "alerts"

router = DefaultRouter()
router.register(r"notifications", AlertNotificationViewSet, basename="alert-notification")
router.register(r"states", AlertStateViewSet, basename="alert-state")
router.register(r"configs", AlertConfigViewSet, basename="alert-config")
router.register(r"templates", AlertTemplateViewSet, basename="alert-template")
router.register(r"categories", AlertCategoryViewSet, basename="alert-category")
router.register(r"voice-alerts", VoiceAlertViewSet, basename="voice-alert")
router.register(r"acknowledgments", AlarmAcknowledgmentViewSet, basename="alarm-acknowledgment")
router.register(r"logs", AlertLogViewSet, basename="alert-log")
router.register(r"preferences", UserAlertPreferenceViewSet, basename="user-alert-preference")

urlpatterns = [
    # ---------------- Dashboard ----------------
    path("", DashboardView.as_view(), name="dashboard"),
    # ---------------- REST API ----------------
    path("api/", include(router.urls)),
    # ---------------- Registry Views ----------------
    path("<str:model>/<str:action>/", NAMUBUYA.as_view(), name="registry-list"),
    path("<str:model>/<str:pk>/<str:action>/", NAMUBUYA.as_view(), name="registry-object"),
    path("<str:app_label>/<str:model_name>/schema/", fields_schema, name="fields_schema"),
    path("<str:app_label>/<str:model>/<str:action>/", registry_router, name="registry_router"),
    path("<str:app_label>/<str:model>/<str:pk>/<str:action>/", registry_router, name="registry_router_object"),
]

