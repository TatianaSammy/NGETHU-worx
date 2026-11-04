import logging

from celery import shared_task

from .services.analysis_generator import AnalysisGenerator
from .services.result_generator import ResultGenerator


logger = logging.getLogger(__name__)


@shared_task(name="central_lab.refresh_analysis_workflow", ignore_result=True)
def refresh_analysis_workflow():
    analyses = AnalysisGenerator.generate_due_analyses()
    results = ResultGenerator.generate_missing_metadata()
    logger.info("[Central Lab] scheduled refresh created=%s existing=%s skipped=%s errors=%s result_metadata_repaired=%s", analyses["created"], analyses["existing"], analyses["skipped"], analyses["errors"], results["repaired"])
    return {"analyses": analyses, "results": results}
