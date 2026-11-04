from django.core.serializers.json import DjangoJSONEncoder
from django.http import JsonResponse
from django.shortcuts import render
from django.views import View

from core.mixins import WanjalaMixin

from .services.dashboard_service import DashboardService


class DashboardView(WanjalaMixin, View):
    app_label = "heavy_metal"
    template_name = "heavy_metal/dashboard.html"

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        context = DashboardService.get_context(request)
        if request.GET.get("format") == "json" or "application/json" in request.headers.get("Accept", ""):
            payload = {key: context[key] for key in ("summary_cards", "analysis_series", "result_series", "analysis_type_series", "parameter_series", "stage_cards", "cards")}
            return JsonResponse(payload, encoder=DjangoJSONEncoder)
        return render(request, self.template_name, context)


class DashboardCardsView(WanjalaMixin, View):
    app_label = "heavy_metal"
    template_name = "core/components/analysis_workflow_dashboard.html"

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        return render(request, self.template_name, DashboardService.get_context(request))
