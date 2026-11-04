from django.core.serializers.json import DjangoJSONEncoder
from django.http import JsonResponse
from django.shortcuts import render
from django.views import View

from core.mixins import WanjalaMixin

from .services.dashboard_service import DashboardService
from .services.worksheet_dashboard_service import WorksheetDashboardService


class DashboardView(WanjalaMixin, View):
    app_label = "microbial"
    template_name = "microbial/dashboard.html"

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        if request.GET.get("format") == "json" or "application/json" in request.headers.get("Accept", ""):
            context = DashboardService.get_context(request)
            payload = {key: context[key] for key in ("summary_cards", "analysis_series", "result_series", "analysis_type_series", "parameter_series", "stage_cards", "cards")}
            return JsonResponse(payload, encoder=DjangoJSONEncoder)
        context = WorksheetDashboardService.get_context(request)
        return render(request, self.template_name, context)


class DashboardCardsView(WanjalaMixin, View):
    app_label = "microbial"
    template_name = "microbial/dashboard.html"

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        return render(request, self.template_name, WorksheetDashboardService.get_context(request))
