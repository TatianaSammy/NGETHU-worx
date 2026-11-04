from django.shortcuts import render
from django.views import View

from core.mixins import WanjalaMixin

from .services.dashboard_service import DashboardService


class DashboardView(WanjalaMixin, View):
    app_label = "central_lab"
    template_name = "central_lab/dashboard.html"

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        return render(request, self.template_name, DashboardService.get_context(request))


class DashboardCardsView(WanjalaMixin, View):
    app_label = "central_lab"
    template_name = "core/components/analysis_workflow_dashboard.html"

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        return render(request, self.template_name, DashboardService.get_context(request))
