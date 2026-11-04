from core.services.analysis_generation import generate_missing_analyses

from .config import ANALYSIS_GENERATOR_SPECS


class AnalysisGenerator:
    LOCK_NAME = "kstw-analysis-generator"

    @classmethod
    def generate_due_analyses(cls, *, plant_ids=None, batch_size=500):
        return generate_missing_analyses(
            ANALYSIS_GENERATOR_SPECS,
            lock_name=cls.LOCK_NAME,
            plant_ids=plant_ids,
            batch_size=batch_size,
        )
