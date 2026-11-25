from django.urls import path

from .views import StandardizationDashboardView

app_name = "standardization"

urlpatterns = [
    path("", StandardizationDashboardView.as_view(), name="dashboard"),
]
