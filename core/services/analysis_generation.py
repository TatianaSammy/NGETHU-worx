from uuid import uuid4

from django.apps import apps
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone


def generate_missing_analyses(specs, *, lock_name, plant_ids=None, batch_size=500):
    token = str(uuid4())
    try:
        acquired = cache.add(lock_name, token, timeout=240)
    except Exception:
        acquired = True
    if not acquired:
        return {"created": 0, "existing": 0, "errors": 0, "skipped": True}

    totals = {"created": 0, "existing": 0, "errors": 0, "skipped": False}
    now = timezone.localtime()
    due_samples = Q(sampling_date__lt=now.date()) | Q(
        sampling_date=now.date(),
        sampling_time__lte=now.time().replace(tzinfo=None),
    )

    try:
        for spec in specs:
            Sample = apps.get_model(spec["app_label"], spec["sample_model"])
            Analysis = apps.get_model(spec["app_label"], spec["analysis_model"])
            queryset = Sample.objects.filter(due_samples, is_deleted=False).filter(
                **{f"{spec['analysis_relation']}__isnull": True}
            )
            if plant_ids:
                queryset = queryset.filter(**{f"{spec['plant_path']}__in": plant_ids})
            queryset = queryset.order_by("sampling_date", "sampling_time", "pk")[:batch_size]

            for sample in queryset.iterator(chunk_size=min(batch_size, 250)):
                try:
                    with transaction.atomic():
                        _, created = Analysis.objects.get_or_create(sample_id=sample.pk)
                    totals["created" if created else "existing"] += 1
                except IntegrityError:
                    totals["existing"] += 1
                except Exception:
                    totals["errors"] += 1
    finally:
        try:
            if cache.get(lock_name) == token:
                cache.delete(lock_name)
        except Exception:
            pass

    return totals
