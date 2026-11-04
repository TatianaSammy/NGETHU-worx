from core.dashboard_result_cards import build_result_dashboard

from .config import APP_LABEL, DASHBOARD_ANALYSIS_SPECS


class ResultCardService:
    @classmethod
    def get_context(cls, request):
        return build_result_dashboard(request, APP_LABEL, DASHBOARD_ANALYSIS_SPECS)
