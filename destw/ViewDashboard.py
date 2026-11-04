from django.shortcuts import render
from django.views import View

from core.mixins import WanjalaMixin
from destw.services.dashboard_service import DashboardService


class DashboardView(WanjalaMixin, View):
    template_name = "destw/dashboard.html"
    app_label = "destw"

    def get(self, request):
        response = self.process_request(request)
        if response:
            return response
        self.check_idle_timeout()
        return render(request, self.template_name, DashboardService.get_context(request))


class DashboardCardsView(WanjalaMixin, View):
    app_label = "destw"
    template_name = "core/components/analysis_workflow_dashboard.html"

    def get(self, request):
        response = self.process_request(request)
        if response:
            return response
        self.check_idle_timeout()
        return render(request, self.template_name, DashboardService.get_context(request))
