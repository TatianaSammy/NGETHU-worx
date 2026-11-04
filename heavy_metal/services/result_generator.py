from datetime import timedelta

from django.apps import apps
from django.db.models import Count
from django.utils import timezone


class ResultGenerator:
    @classmethod
    def generate_for_analysis(cls, analysis):
        Result = apps.get_model("heavy_metal", "HeavyMetalParameter")
        Limit = apps.get_model("t", "HeavyMetalLimit")
        expected = Limit.objects.filter(treatment_plant_id=analysis.treatment_plant_id).count()
        current = Result.objects.filter(analysis_id=analysis.pk).count()
        if current < expected:
            analysis._auto_create_expected_results()
        return {"expected": expected, "existing": Result.objects.filter(analysis_id=analysis.pk).count(), "pending": Result.objects.filter(analysis_id=analysis.pk, entry_status="pending").count()}

    @classmethod
    def generate_missing_results(cls, batch_size=500, lookback_days=30):
        Analysis = apps.get_model("heavy_metal", "HeavyMetalAnalysis")
        Result = apps.get_model("heavy_metal", "HeavyMetalParameter")
        Limit = apps.get_model("t", "HeavyMetalLimit")
        since = timezone.localdate() - timedelta(days=lookback_days)
        limits = dict(Limit.objects.values("treatment_plant_id").annotate(total=Count("pk")).values_list("treatment_plant_id", "total"))
        actual = dict(Result.objects.filter(analysis__analysis_date__gte=since).values("analysis_id").annotate(total=Count("pk")).values_list("analysis_id", "total"))
        analyses = Analysis.objects.filter(analysis_date__gte=since).select_related("treatment_plant")[:batch_size]
        repaired = 0
        pending = 0
        for analysis in analyses:
            expected = limits.get(analysis.treatment_plant_id, 0)
            if actual.get(analysis.pk, 0) < expected:
                cls.generate_for_analysis(analysis)
                repaired += 1
            pending += max(expected - actual.get(analysis.pk, 0), 0)
        return {"repaired": repaired, "pending": pending, "scanned": len(list(analyses))}
