"""
wsp/ViewDashboard.py — Water Safety Plan (WSP) Telemetry Cockpit
Data-driven risk assessment, thematic compliance scoring, survey completion metrics, and WHO/NCWSC risk benchmarking.
"""
import json
import logging
from datetime import timedelta

from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Count, Avg, Sum, Max, Min, Q
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.utils import timezone
from django.views import View

from core.mixins import WanjalaMixin
from core.registry import registry

from .models import (
    Survey,
    Category,
    ThematicArea,
    Question,
    Response,
    OperationalUnit,
)

logger = logging.getLogger(__name__)


class DashboardView(WanjalaMixin, View):
    template_name = "wsp/dashboard.html"
    app_label = "wsp"

    def get(self, request):
        if (r := self.process_request(request)):
            return r
        self.check_idle_timeout()

        wants_json = (
            "application/json" in request.headers.get("Accept", "")
            or request.GET.get("format") == "json"
        )
        context = self.get_context(request)
        if wants_json:
            return JsonResponse(context.get("dashboard_json_payload", {}), safe=False)

        return HttpResponse(
            render_to_string(self.template_name, context, request=request)
        )

    def get_context(self, request):
        surveys = Survey.objects.all().select_related("unit")
        total_surveys = surveys.count()
        categories = Category.objects.all()
        thematic_areas = ThematicArea.objects.all()
        units = OperationalUnit.objects.all()
        responses = Response.objects.all()
        total_responses = responses.count()

        # Overall average score
        avg_score_agg = surveys.aggregate(avg=Avg("average_score"))["avg"] or 0.0
        avg_wsp_index = round(float(avg_score_agg), 1)

        # Average completion rate across surveys
        comp_rates = [s.completion_rate for s in surveys[:50]]
        avg_comp_rate = round(sum(comp_rates) / len(comp_rates), 1) if comp_rates else 0.0

        kpis = [
            {"label": "WSP Surveys", "value": total_surveys, "icon": "fa-clipboard-list", "color": "cyan"},
            {"label": "Safety Index", "value": f"{avg_wsp_index}%", "icon": "fa-shield-halved", "color": "emerald" if avg_wsp_index >= 80 else "amber"},
            {"label": "Thematic Areas", "value": thematic_areas.count(), "icon": "fa-layer-group", "color": "purple"},
            {"label": "Audit Responses", "value": total_responses, "icon": "fa-list-check", "color": "blue"},
            {"label": "Mean Completion", "value": f"{avg_comp_rate}%", "icon": "fa-chart-pie", "color": "emerald"},
            {"label": "Operational Units", "value": units.count(), "icon": "fa-building", "color": "amber"},
        ]

        # Category scores
        cat_labels = []
        cat_scores = []
        for cat in categories[:8]:
            cat_labels.append(cat.name)
            cat_scores.append(round(float(cat.average_score() or 0.0), 1))

        # Recent surveys feed
        recent_surveys = []
        for s in surveys.order_by("-created_at")[:10]:
            recent_surveys.append({
                "name": s.name,
                "unit": str(s.unit),
                "score": f"{float(s.average_score):.1f}%",
                "completion": f"{s.completion_rate:.0f}%",
                "date": s.created_at.strftime("%Y-%m-%d"),
            })

        charts_payload = {
            "categories": {
                "labels": cat_labels,
                "data": cat_scores,
            },
            "survey_scores": {
                "labels": [s["name"][:16] for s in recent_surveys[:6]],
                "data": [float(s["score"].replace("%", "")) for s in recent_surveys[:6]],
            },
        }

        try:
            entries = registry.for_app(self.app_label) or []
        except Exception:
            entries = []

        return {
            "dashboard_title": "Water Safety Plan (WSP)",
            "dashboard_subtitle": "WHO/NCWSC Risk Audits & Compliance Cockpit",
            "dashboard_icon": "shield-halved",
            "app_groups": registry.sidebar(self.app_label) if entries else [],
            "kpis": kpis,
            "recent_surveys": recent_surveys,
            "charts_json": json.dumps(charts_payload, cls=DjangoJSONEncoder, default=str),
            "dashboard_json_payload": charts_payload,
        }
