import logging

from celery import shared_task

from kstw.services.analysis_generator import AnalysisGenerator
from kstw.services.result_generator import ResultGenerator


logger = logging.getLogger(__name__)


@shared_task(name="kstw.refresh_analysis_workflow", ignore_result=True)
def refresh_analysis_workflow(plant_ids=None):
    analyses = AnalysisGenerator.generate_due_analyses(plant_ids=plant_ids)
    results = ResultGenerator.generate_missing_results()
    logger.info(
        "[KSTW] Scheduled workflow refresh created=%s existing=%s analysis_errors=%s metadata_updated=%s metadata_errors=%s",
        analyses["created"],
        analyses["existing"],
        analyses["errors"],
        results["updated"],
        results["errors"],
    )
    return {"analyses": analyses, "results": results}
