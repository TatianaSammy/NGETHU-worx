from django.db.models.signals import post_save
from django.db.models import Q
from django.dispatch import receiver
from .models import FilteredWaterAnalysis, FilteredWaterResults, FilteredWaterLimit, WaterFilter, WaterFilterStatus

@receiver(post_save, sender=FilteredWaterAnalysis)
def create_default_results(sender, instance, created, **kwargs):
    if not created:
        return

    # Get filters in WORKING state only
    working_filters = WaterFilter.objects.filter(
        phase=instance.treatment_plant_phase,
        filter_status__status=WaterFilterStatus.WORKING.value
    ).filter(
        Q(treatment_plant=instance.treatment_plant)
        | Q(treatment_plant__isnull=True)
    )

    # Create default results for each limit associated with working filters
    limits = FilteredWaterLimit.objects.filter(
        treatment_plant=instance.treatment_plant,
        water_filter__in=working_filters
    )

    for limit in limits:
        FilteredWaterResults.objects.get_or_create(
            analysis=instance,
            water_filter=limit.water_filter,
            parameter=limit,
            defaults={
                'result_value': None
            }
        )
