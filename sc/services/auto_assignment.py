import logging

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from core.models import Section
from sc.models import MonthlyDutyRoster


logger = logging.getLogger(__name__)
User = get_user_model()


def assign_roster(roster_id, section_id=None, start_date=None):
    roster = MonthlyDutyRoster.objects.filter(pk=roster_id).select_related("treatment_plant").first()
    if not roster:
        return 0

    sections = Section.objects.filter(
        treatment_plant_id=roster.treatment_plant_id,
        is_active=True,
        shift_requirements__shift__treatment_plant_id=roster.treatment_plant_id,
    ).distinct().order_by("pk")
    if section_id:
        sections = sections.filter(pk=section_id)

    assigned = 0
    for section in sections:
        if not User.objects.filter(
            is_active=True,
            assigned_treatment_plant_id=roster.treatment_plant_id,
            section_id=section.pk,
        ).exists():
            continue
        try:
            MonthlyDutyRoster.assign_monthly_shifts_bulk(
                roster,
                section,
                start_date=start_date,
            )
            assigned += 1
        except Exception:
            logger.exception(
                "Automatic shift allocation failed for roster %s, plant %s, section %s",
                roster.pk,
                roster.treatment_plant_id,
                section.pk,
            )
    return assigned


def assign_for_user(user_id):
    user = User.objects.filter(pk=user_id, is_active=True).select_related("section").first()
    if not user or not user.assigned_treatment_plant_id or not user.section_id:
        return 0

    if (
        user.section.treatment_plant_id != user.assigned_treatment_plant_id
        or not user.section.is_active
    ):
        logger.info(
            "Skipping automatic duty allocation for user %s because the plant and section do not match",
            user.pk,
        )
        return 0

    today = timezone.localdate()
    rosters = MonthlyDutyRoster.objects.filter(
        treatment_plant_id=user.assigned_treatment_plant_id,
    ).filter(
        Q(year__gt=today.year) | Q(year=today.year, month__gte=today.month)
    ).order_by("year", "month")

    total = 0
    for roster_id in rosters.values_list("pk", flat=True):
        total += assign_roster(roster_id, user.section_id, start_date=today)
    return total


def assign_for_shift_scope(plant_id, section_id):
    if not plant_id or not section_id:
        return 0
    today = timezone.localdate()
    rosters = MonthlyDutyRoster.objects.filter(
        treatment_plant_id=plant_id,
    ).filter(
        Q(year__gt=today.year) | Q(year=today.year, month__gte=today.month)
    ).order_by("year", "month")

    total = 0
    for roster_id in rosters.values_list("pk", flat=True):
        total += assign_roster(roster_id, section_id, start_date=today)
    return total


def assign_for_new_roster(roster_id):
    return assign_roster(roster_id, start_date=timezone.localdate())
