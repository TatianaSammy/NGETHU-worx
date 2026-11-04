from django.apps import apps
from django.db.models import Q

from .config import RESULT_GENERATOR_SPECS


class ResultGenerator:
    @classmethod
    def generate_missing_metadata(cls, batch_size=500):
        stats = {"repaired": 0, "scanned": 0, "errors": 0}
        for spec in RESULT_GENERATOR_SPECS:
            Result = apps.get_model("central_lab", spec["result_model"])
            fields = {field.name for field in Result._meta.fields}
            if not {"kebs_reference", "quality_compliance"}.issubset(fields):
                continue
            pending = Result.objects.filter(
                Q(kebs_reference__isnull=True)
                | Q(kebs_reference="")
                | Q(quality_compliance__isnull=True)
                | Q(quality_compliance="")
            ).order_by("created_at")[:batch_size]
            for result in pending:
                stats["scanned"] += 1
                try:
                    result.save(update_fields=("kebs_reference", "quality_compliance", "updated_at"))
                    stats["repaired"] += 1
                except Exception:
                    stats["errors"] += 1
        return stats
