from django.apps import apps
from django.db.models import Count
from django.utils import timezone

from core.dashboard_result_cards import _can_view, permission_scoped_sidebar
from core.registry import registry

from .config import APP_LABEL
from .result_card_service import ResultCardService


class DashboardService:
    @classmethod
    def get_context(cls, request):
        context = ResultCardService.get_context(request)
        Analysis = apps.get_model(APP_LABEL, "MicrobialAnalysis")
        NonConformity = apps.get_model(APP_LABEL, "MicrobialNonConformity")
        queryset = Analysis.objects.filter(analysis_date__gte=context["period_start"], analysis_date__lte=context["period_end"])
        if not _can_view(request.user, Analysis):
            queryset = queryset.none()
        if context["plant"]:
            queryset = queryset.filter(treatment_plant=context["plant"])
        if not request.user.is_superuser and not context["plant"]:
            queryset = queryset.none()
        analysis_ids = queryset.values("pk")
        nc = NonConformity.objects.filter(analysis_id__in=analysis_ids).exclude(status="closed")
        if not _can_view(request.user, NonConformity):
            nc = NonConformity.objects.none()
        open_nc = nc.count()
        high_risk = nc.filter(risk_score__gte=7).count()
        incubating = queryset.filter(incubation_end__gt=timezone.now()).count()
        context["analysis_type_series"] = [
            {"label": row["method__name"] or "Unspecified method", "value": row["total"]}
            for row in queryset.values("method__name").annotate(total=Count("pk")).order_by("-total")
        ]
        context["composition_title"] = "Assay volume by method"
        context["workflow_description"] = "Scheduled assays use each active method and plant phase; only recorded microbial readings and entered treated-water values are counted."
        context["summary_cards"].extend([
            {"label": "Active incubations", "value": incubating, "caption": "Incubation windows still in progress", "icon": "fa-temperature-half", "tone": "amber", "tab": "workflow"},
            {"label": "Open nonconformities", "value": open_nc, "caption": "Microbial investigations awaiting closure", "icon": "fa-triangle-exclamation", "tone": "rose", "tab": "workflow"},
            {"label": "High-risk cases", "value": high_risk, "caption": "Risk score ≥ 7 · prioritize review", "icon": "fa-shield-halved", "tone": "violet", "tab": "workflow"},
        ])
        context.update({
            "app": {"name": APP_LABEL, "label": "Microbial Analytics"},
            "app_groups": permission_scoped_sidebar(request, registry, APP_LABEL),
            "live_url": f"/{APP_LABEL}/results/cards/",
        })
        return context
