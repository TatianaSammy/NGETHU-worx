from django.urls import path
from .ViewDashboard import DashboardCardsView, DashboardView
from core.views import (NAMUBUYA,fields_schema,registry_router,)
from core.analysis_result_entry import AnalysisResultEntryView
from .services.config import APP_LABEL, DASHBOARD_ANALYSIS_SPECS

app_name = "destw"

urlpatterns = [
    # ---------------- Dashboard ----------------
    path("", DashboardView.as_view(), name="dashboard"),
    path("results/cards/", DashboardCardsView.as_view(), name="result-cards"),
    path("results/entry/<str:analysis_model>/<str:analysis_id>/", AnalysisResultEntryView.as_view(app_label=APP_LABEL, analysis_specs=DASHBOARD_ANALYSIS_SPECS), name="result-entry"),
    path( "<str:model>/<str:action>/", NAMUBUYA.as_view(),name="registry-list",),
    path( "<str:model>/<str:pk>/<str:action>/",NAMUBUYA.as_view(),name="registry-object",),
    path( "<str:app_label>/<str:model_name>/schema/",fields_schema,name="fields_schema", ),
    path( "<str:app_label>/<str:model>/<str:action>/",registry_router,name="registry_router",),
    path( "<str:app_label>/<str:model>/<str:pk>/<str:action>/", registry_router,name="registry_router_object", ),
]
