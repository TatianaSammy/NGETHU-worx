import logging
from datetime import datetime, time, timedelta

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from sc.models import Shift

from .config import SCHEDULE_CONFIG


logger = logging.getLogger(__name__)


class AnalysisGenerator:
    @classmethod
    def generate_due_analyses(cls, target_datetime=None, plant_ids=None, lookback_minutes=None):
        target = target_datetime or timezone.now()
        now = timezone.localtime(target) if timezone.is_aware(target) else timezone.make_aware(target)
        lookback = lookback_minutes if lookback_minutes is not None else SCHEDULE_CONFIG["lookback_minutes"]
        window_start = now - timedelta(minutes=lookback)
        window_end = now + timedelta(minutes=2)
        stats = {"created": 0, "existing": 0, "skipped": 0, "errors": 0}
        from heavy_metal.models import HeavyMetalAnalysis, HeavyMetalSamplingTime
        from t.models import TreatmentPlant

        SamplingTime = HeavyMetalSamplingTime
        Analysis = HeavyMetalAnalysis
        Plant = TreatmentPlant
        schedules = SamplingTime.objects.filter(schedule__enabled=True).select_related("schedule")
        plants = Plant.objects.prefetch_related("phases").order_by("pk")
        if plant_ids:
            plants = plants.filter(pk__in=plant_ids)
        slots = []
        for sample_day in {window_start.date(), now.date(), window_end.date()}:
            lower = window_start.time() if sample_day == window_start.date() else time.min
            upper = window_end.time() if sample_day == window_end.date() else time.max
            day_slots = schedules.filter(time__gte=lower, time__lte=upper)
            for sample_time in day_slots:
                slot = timezone.make_aware(datetime.combine(sample_day, sample_time.time), timezone.get_current_timezone())
                if window_start <= slot <= window_end:
                    slots.append((sample_time, slot))
        slots.sort(key=lambda item: item[1])
        if not slots:
            return stats

        phase_lists = {plant.pk: list(plant.phases.all()) for plant in plants}
        for sample_time, slot in slots:
            existing_keys = set(Analysis.objects.filter(
                analysis_date=slot.date(), sampling_time=sample_time
            ).values_list("treatment_plant_id", "treatment_plant_phase_id", "shift_id"))
            for plant in plants:
                phases = phase_lists.get(plant.pk, [])
                if not phases:
                    stats["skipped"] += 1
                    continue
                shift = Shift.get_shift_for_time(slot, treatment_plant=plant)
                for phase in phases:
                    identity = (plant.pk, phase.pk, shift.pk if shift else None)
                    if identity in existing_keys:
                        stats["existing"] += 1
                        continue
                    lock_key = f"heavy-metal-analysis:{plant.pk}:{phase.pk}:{sample_time.pk}:{slot.date()}:{shift.pk if shift else 'no-shift'}"
                    if not cache.add(lock_key, True, timeout=180):
                        stats["skipped"] += 1
                        continue
                    try:
                        lookup = {
                            "treatment_plant": plant,
                            "treatment_plant_phase": phase,
                            "analysis_date": slot.date(),
                            "sampling_time": sample_time,
                        }
                        if shift is None:
                            lookup["shift__isnull"] = True
                        else:
                            lookup["shift"] = shift
                        with transaction.atomic():
                            analysis = Analysis.objects.filter(**lookup).first()
                            if analysis:
                                stats["existing"] += 1
                                existing_keys.add(identity)
                                continue
                            try:
                                Analysis.objects.create(
                                    treatment_plant=plant,
                                    treatment_plant_phase=phase,
                                    analysis_date=slot.date(),
                                    sampling_time=sample_time,
                                    shift=shift,
                                    remarks="Automatically generated from the configured heavy-metal sampling schedule.",
                                )
                                created = True
                                existing_keys.add(identity)
                            except IntegrityError:
                                created = False
                            stats["created" if created else "existing"] += 1
                    except Exception:
                        stats["errors"] += 1
                        logger.exception("Heavy-metal scheduled analysis generation failed for plant=%s phase=%s slot=%s", plant.pk, phase.pk, slot.isoformat())
                    finally:
                        cache.delete(lock_key)
        return stats
