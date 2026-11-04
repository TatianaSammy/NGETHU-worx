from django.apps import apps

from core.services.result_metadata import generate_missing_result_metadata

from .config import RESULT_GENERATOR_SPECS


class ResultGenerator:
    @classmethod
    def generate_missing_results(cls, *, batch_size=500):
        return generate_missing_result_metadata(RESULT_GENERATOR_SPECS, batch_size=batch_size)

    @classmethod
    def generate_for_analysis(cls, analysis, *, batch_size=500):
        spec = next(
            (item for item in RESULT_GENERATOR_SPECS if item["analysis_model"].lower() == analysis._meta.model_name),
            None,
        )
        if not spec:
            return {"updated": 0, "errors": 1}
        Result = apps.get_model(spec["app_label"], spec["result_model"])
        result = Result.objects.filter(analysis_id=analysis.pk, is_deleted=False).first()
        if not result:
            return {"updated": 0, "errors": 0, "pending": True}
        return generate_missing_result_metadata(
            [{**spec, "analysis_ids": [analysis.pk]}],
            batch_size=batch_size,
        )
