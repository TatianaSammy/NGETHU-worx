from core.advanced_analysis_dashboard import build_advanced_analysis_dashboard

from .config import APP_LABEL, DASHBOARD_ANALYSIS_SPECS


class ResultCardService:
    @classmethod
    def get_context(cls, request):
        return build_advanced_analysis_dashboard(request, APP_LABEL, DASHBOARD_ANALYSIS_SPECS)
