from core.dashboard_result_cards import permission_scoped_sidebar
from core.registry import registry

from .config import APP_LABEL
from .result_card_service import ResultCardService


class DashboardService:
    @classmethod
    def get_context(cls, request):
        context = ResultCardService.get_context(request)
        context.update({
            "app": {"name": APP_LABEL, "label": "KSTW Wastewater & Effluent Quality"},
            "app_groups": permission_scoped_sidebar(request, registry, APP_LABEL),
            "live_url": f"/{APP_LABEL}/results/cards/",
        })
        return context
