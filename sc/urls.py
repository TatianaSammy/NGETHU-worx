from django.urls import path
from .ViewDashboard import DashboardView
from .dashboard_actions import roster_assignment_action, roster_assignment_options
from .views_handover import (
    shift_handover_modal_data,
    shift_handover_submit,
    shift_takeover_confirm,
)
from core.views import (NAMUBUYA,fields_schema,registry_router,)

app_name = "sc"

urlpatterns = [
    path("roster/assignment-options/", roster_assignment_options, name="roster-assignment-options"),
    path("roster/assignment-action/", roster_assignment_action, name="roster-assignment-action"),
    # ---------------- Shift Handover & Takeover ----------------
    path("handover/modal-data/", shift_handover_modal_data, name="handover-modal-data"),
    path("handover/submit/", shift_handover_submit, name="handover-submit"),
    path("handover/confirm-takeover/", shift_takeover_confirm, name="takeover-confirm"),

    # ---------------- Dashboard ----------------
    path("", DashboardView.as_view(), name="dashboard"),
    path( "<str:model>/<str:action>/", NAMUBUYA.as_view(),name="registry-list",),
    path( "<str:model>/<str:pk>/<str:action>/",NAMUBUYA.as_view(),name="registry-object",),
    path( "<str:app_label>/<str:model_name>/schema/",fields_schema,name="fields_schema", ),
    path( "<str:app_label>/<str:model>/<str:action>/",registry_router,name="registry_router",),
    path( "<str:app_label>/<str:model>/<str:pk>/<str:action>/", registry_router,name="registry_router_object", ),
]
