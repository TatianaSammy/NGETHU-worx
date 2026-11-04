import logging
from datetime import datetime, timedelta

from django.apps import apps
from django.db import IntegrityError, transaction
from django.utils import timezone

from .config import ANALYSIS_GENERATOR_SPECS


logger = logging.getLogger(__name__)


class AnalysisGenerator:
    @classmethod
    def generate_due_analyses(cls, target_datetime=None, backfill_days=30, batch_size=500):
        now = timezone.localtime(target_datetime or timezone.now())
        first_date = now.date() - timedelta(days=backfill_days)
        stats = {"created": 0, "existing": 0, "skipped": 0, "errors": 0}
        for spec in ANALYSIS_GENERATOR_SPECS:
            Sample = apps.get_model("central_lab", spec["sample_model"])
            Analysis = apps.get_model("central_lab", spec["analysis_model"])
            samples = Sample.objects.filter(
                is_deleted=False,
                sampling_date__gte=first_date,
                sampling_date__lte=now.date(),
                analysis__isnull=True,
            ).order_by("sampling_date", "sampling_time")[:batch_size]
            for sample in samples:
                if sample.sampling_date == now.date() and sample.sampling_time > now.time():
                    stats["skipped"] += 1
                    continue
                sample_datetime = timezone.make_aware(
                    datetime.combine(sample.sampling_date, sample.sampling_time),
                    timezone.get_current_timezone(),
                )
                try:
                    with transaction.atomic():
                        try:
                            _, created = Analysis.objects.get_or_create(
                                sample=sample,
                                defaults={
                                    "analysis_datetime": sample_datetime,
                                    "remarks": "Automatically generated from the existing central laboratory sample record.",
                                },
                            )
                        except IntegrityError:
                            created = False
                    stats["created" if created else "existing"] += 1
                except Exception:
                    stats["errors"] += 1
                    logger.exception("Central laboratory analysis generation failed for sample=%s", sample.pk)
        return stats
