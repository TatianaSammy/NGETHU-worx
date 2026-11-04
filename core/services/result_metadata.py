from django.apps import apps
from django.db.models import Q
from django.utils import timezone

from core.quality_results import parameter_result_metadata


def generate_missing_result_metadata(specs, *, batch_size=500):
    totals = {"updated": 0, "errors": 0}
    now = timezone.now()

    for spec in specs:
        Result = apps.get_model(spec["app_label"], spec["result_model"])
        queryset = Result.objects.filter(is_deleted=False).filter(
            Q(effluent_standard="")
            | Q(kebs_reference="")
            | Q(quality_compliance="")
        ).select_related("parameter", "analysis_method")
        if spec.get("analysis_ids"):
            queryset = queryset.filter(analysis_id__in=spec["analysis_ids"])
        pending = []

        for result in queryset.iterator(chunk_size=batch_size):
            try:
                for field, value in parameter_result_metadata(
                    result.parameter,
                    result.analysis_method,
                    result.result,
                ).items():
                    setattr(result, field, value)
                result.updated_at = now
                pending.append(result)
            except Exception:
                totals["errors"] += 1
            if len(pending) >= batch_size:
                Result.objects.bulk_update(
                    pending,
                    ["effluent_standard", "kebs_reference", "quality_compliance", "updated_at"],
                    batch_size=batch_size,
                )
                totals["updated"] += len(pending)
                pending.clear()

        if pending:
            Result.objects.bulk_update(
                pending,
                ["effluent_standard", "kebs_reference", "quality_compliance", "updated_at"],
                batch_size=batch_size,
            )
            totals["updated"] += len(pending)

    return totals
