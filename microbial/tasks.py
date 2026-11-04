import logging

from celery import shared_task

from .services.analysis_generator import AnalysisGenerator
from .services.result_generator import ResultGenerator


logger = logging.getLogger(__name__)


@shared_task(name="microbial.refresh_analysis_workflow", ignore_result=True)
def refresh_analysis_workflow(plant_ids=None):
    analyses = AnalysisGenerator.generate_due_analyses(plant_ids=plant_ids)
    results = ResultGenerator.generate_missing_results()
    logger.info("[Microbial] scheduled refresh created=%s existing=%s skipped=%s errors=%s results_repaired=%s pending=%s", analyses["created"], analyses["existing"], analyses["skipped"], analyses["errors"], results["repaired"], results["pending"])
    return {"analyses": analyses, "results": results}
