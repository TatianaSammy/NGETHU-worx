from datetime import timedelta

from django.apps import apps
from django.db.models import Count
from django.utils import timezone


class ResultGenerator:
    @classmethod
    def generate_for_analysis(cls, analysis):
        MicrobialLimit = apps.get_model("t", "MicrobialLimit")
        TreatedLimit = apps.get_model("t", "TreatedWaterLimit")
        MicrobialResult = apps.get_model("microbial", "MicrobialParameterResult")
        TreatedResult = apps.get_model("microbial", "TreatedWaterParameterResult")
        expected_microbial = MicrobialLimit.objects.filter(treatment_plant_id=analysis.treatment_plant_id).count()
        expected_treated = TreatedLimit.objects.filter(treatment_plant_id=analysis.treatment_plant_id).count()
        current_microbial = MicrobialResult.objects.filter(analysis_id=analysis.pk).count()
        current_treated = TreatedResult.objects.filter(analysis_id=analysis.pk).count()
        if current_microbial < expected_microbial or current_treated < expected_treated:
            analysis._auto_create_expected_results()
        return {
            "expected": expected_microbial + expected_treated,
            "existing": MicrobialResult.objects.filter(analysis_id=analysis.pk).count() + TreatedResult.objects.filter(analysis_id=analysis.pk).count(),
            "pending": MicrobialResult.objects.filter(analysis_id=analysis.pk, entry_status="pending").count(),
        }

    @classmethod
    def generate_missing_results(cls, batch_size=500, lookback_days=30):
        Analysis = apps.get_model("microbial", "MicrobialAnalysis")
        MicrobialResult = apps.get_model("microbial", "MicrobialParameterResult")
        TreatedResult = apps.get_model("microbial", "TreatedWaterParameterResult")
        MicrobialLimit = apps.get_model("t", "MicrobialLimit")
        TreatedLimit = apps.get_model("t", "TreatedWaterLimit")
        since = timezone.localdate() - timedelta(days=lookback_days)
        limits = dict(MicrobialLimit.objects.values("treatment_plant_id").annotate(total=Count("pk")).values_list("treatment_plant_id", "total"))
        treated_limits = dict(TreatedLimit.objects.values("treatment_plant_id").annotate(total=Count("pk")).values_list("treatment_plant_id", "total"))
        microbial_actual = dict(MicrobialResult.objects.filter(analysis__analysis_date__gte=since).values("analysis_id").annotate(total=Count("pk")).values_list("analysis_id", "total"))
        treated_actual = dict(TreatedResult.objects.filter(analysis__analysis_date__gte=since).values("analysis_id").annotate(total=Count("pk")).values_list("analysis_id", "total"))
        analyses = Analysis.objects.filter(analysis_date__gte=since).select_related("treatment_plant")[:batch_size]
        repaired = 0
        pending = 0
        for analysis in analyses:
            expected_microbial = limits.get(analysis.treatment_plant_id, 0)
            expected_treated = treated_limits.get(analysis.treatment_plant_id, 0)
            current_microbial = microbial_actual.get(analysis.pk, 0)
            current_treated = treated_actual.get(analysis.pk, 0)
            if current_microbial < expected_microbial or current_treated < expected_treated:
                cls.generate_for_analysis(analysis)
                repaired += 1
            pending += max(expected_microbial - current_microbial, 0)
        return {"repaired": repaired, "pending": pending, "scanned": len(list(analyses))}
