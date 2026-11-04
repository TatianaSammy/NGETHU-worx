from datetime import timedelta

from django.apps import apps
from django.utils import timezone

from core.dashboard_result_cards import permission_scoped_sidebar
from core.registry import registry

from .config import APP_LABEL, DASHBOARD_ANALYSIS_SPECS
from .result_card_service import ResultCardService


class DashboardService:
    @classmethod
    def get_context(cls, request):
        period = request.GET.get("period", "30")
        try:
            period_days = int(period)
        except (TypeError, ValueError):
            period_days = 30
        if period_days not in (7, 30, 90, 365):
            period_days = 30
        analysis_type = request.GET.get("analysis_type", "region")
        if analysis_type not in DASHBOARD_ANALYSIS_SPECS:
            analysis_type = "region"
        today = timezone.localdate()
        start_date = today - timedelta(days=period_days - 1)
        tabs = []
        for key, spec in DASHBOARD_ANALYSIS_SPECS.items():
            Analysis = apps.get_model(APP_LABEL, spec["model"])
            queryset = Analysis.objects.filter(
                **{f"{spec['date_path']}__gte": start_date, f"{spec['date_path']}__lte": today},
            )
            if "is_deleted" in {field.name for field in Analysis._meta.fields}:
                queryset = queryset.filter(is_deleted=False)
            sample_model = Analysis._meta.get_field("sample").related_model
            if "is_deleted" in {field.name for field in sample_model._meta.fields}:
                queryset = queryset.filter(sample__is_deleted=False)
            count = queryset.count() if request.user.is_superuser or request.user.has_perm(f"{APP_LABEL}.view_{Analysis._meta.model_name}") else 0
            tabs.append({"key": key, "label": spec["title"], "count": count, "active": key == analysis_type})
        context = ResultCardService.get_context(request, analysis_type)
        context.update({
            "analysis_type_tabs": tabs,
            "selected_analysis_type": analysis_type,
            "app": {"name": APP_LABEL, "label": "Central Laboratory"},
            "app_groups": permission_scoped_sidebar(request, registry, APP_LABEL),
            "live_url": f"/{APP_LABEL}/results/cards/",
        })
        return context
