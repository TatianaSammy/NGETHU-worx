from core.dashboard_result_cards import _can_view, permission_scoped_sidebar
from core.registry import registry
from django.apps import apps
from django.db.models import Count

from .config import APP_LABEL
from .result_card_service import ResultCardService


class DashboardService:
    @classmethod
    def get_context(cls, request):
        context = ResultCardService.get_context(request)
        Analysis = apps.get_model(APP_LABEL, "HeavyMetalAnalysis")
        NonConformity = apps.get_model(APP_LABEL, "HeavyMetalNonConformity")
        analyses = Analysis.objects.filter(analysis_date__gte=context["period_start"], analysis_date__lte=context["period_end"])
        if not _can_view(request.user, Analysis):
            analyses = analyses.none()
        if context["plant"]:
            analyses = analyses.filter(treatment_plant=context["plant"])
        elif not request.user.is_superuser:
            analyses = analyses.none()
        analysis_ids = analyses.values("pk")
        nc = NonConformity.objects.filter(analysis_id__in=analysis_ids) if _can_view(request.user, NonConformity) else NonConformity.objects.none()
        open_nc = nc.exclude(status="closed").count()
        high_risk = nc.filter(risk_score__gte=7).exclude(status="closed").count()
        context["analysis_type_series"] = [
            {"label": row["treatment_plant_phase__name"] or "Unspecified phase", "value": row["total"]}
            for row in analyses.values("treatment_plant_phase__name").annotate(total=Count("pk")).order_by("-total")
        ]
        context["composition_title"] = "Analysis volume by treatment phase"
        context["workflow_description"] = "Scheduled assays are generated for configured plant phases; parameter rows with pending entry status are excluded from reading analytics."
        context["summary_cards"].extend([
            {"label": "Open nonconformities", "value": open_nc, "caption": "Trace-metal investigations in the selected window", "icon": "fa-triangle-exclamation", "tone": "rose", "tab": "workflow"},
            {"label": "High-risk cases", "value": high_risk, "caption": "Risk score ≥ 7 · prioritize review", "icon": "fa-shield-halved", "tone": "amber", "tab": "workflow"},
        ])
        context.update({
            "app": {"name": APP_LABEL, "label": "Heavy Metal Analytics"},
            "app_groups": permission_scoped_sidebar(request, registry, APP_LABEL),
            "live_url": f"/{APP_LABEL}/results/cards/",
        })
        return context
