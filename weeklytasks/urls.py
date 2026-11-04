from django.urls import path
from .ViewDashboard import DashboardView
from .dashboard_actions import task_assignment_action, task_assignment_options
from core.views import (NAMUBUYA,fields_schema,registry_router,)

app_name = "weeklytasks"

urlpatterns = [
    path("assignment-options/", task_assignment_options, name="assignment-options"),
    path("assignment-action/", task_assignment_action, name="assignment-action"),
    # ---------------- Dashboard ----------------
    path("", DashboardView.as_view(), name="dashboard"),
    path( "<str:model>/<str:action>/", NAMUBUYA.as_view(),name="registry-list",),
    path( "<str:model>/<str:pk>/<str:action>/",NAMUBUYA.as_view(),name="registry-object",),
    path( "<str:app_label>/<str:model_name>/schema/",fields_schema,name="fields_schema", ),
    path( "<str:app_label>/<str:model>/<str:action>/",registry_router,name="registry_router",),
    path( "<str:app_label>/<str:model>/<str:pk>/<str:action>/", registry_router,name="registry_router_object", ),
]
