from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import redirect
from django.urls import reverse

from qc.views.result_card import ResultCardEntryView


class FilterReadingsUpdateView(ResultCardEntryView):
    """Save current parameter readings for one plant phase's water filters."""

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        from filtered_water.models import FilteredWaterAnalysis, FilteredWaterResults
        from t.models import FilteredWaterLimit, TreatmentPlantPhase

        plant = self.resolve_user_assignment().get("plant")
        user = request.user
        can_change = user.is_superuser or user.has_perm(
            "filtered_water.change_filteredwaterresults"
        )
        if not plant or not can_change:
            raise PermissionDenied("You do not have permission to update filter readings for this plant.")

        try:
            phase_id = int(request.POST.get("phase", ""))
        except (TypeError, ValueError):
            raise PermissionDenied("A valid treatment phase is required.")
        phase = TreatmentPlantPhase.objects.select_for_update().filter(
            pk=phase_id,
            treatment_plant=plant,
        ).first()
        if phase is None:
            raise PermissionDenied("The selected phase does not belong to your treatment plant.")

        submitted = {}
        for key, value in request.POST.items():
            if not key.startswith("value_"):
                continue
            raw_id = key.removeprefix("value_")
            if not raw_id.isdigit():
                continue
            try:
                parsed = Decimal(value.strip()) if value.strip() else None
            except InvalidOperation:
                messages.error(request, "Enter a valid number for every filter parameter.")
                return self.filter_readings_redirect(plant)
            if parsed is not None and (not parsed.is_finite() or parsed < 0):
                messages.error(request, "Filter readings must be finite, non-negative numbers.")
                return self.filter_readings_redirect(plant)
            submitted[int(raw_id)] = parsed

        if not submitted:
            messages.warning(request, "No filter parameter readings were submitted.")
            return self.filter_readings_redirect(plant)

        limits = list(
            FilteredWaterLimit.objects.filter(
                pk__in=submitted,
                treatment_plant=plant,
                water_filter__phase=phase,
                water_filter__filter_status__status="working",
            ).select_related("water_filter")
        )
        if len(limits) != len(submitted):
            raise PermissionDenied("One or more filter parameters are outside this plant phase.")

        latest = (
            FilteredWaterAnalysis.objects.filter(
                treatment_plant=plant,
                treatment_plant_phase=phase,
            )
            .order_by("-timestamp", "-pk")
            .first()
        )
        if latest is None or latest.state != "new":
            can_add_analysis = user.is_superuser or user.has_perm(
                "filtered_water.add_filteredwateranalysis"
            )
            if not can_add_analysis:
                raise PermissionDenied("You need permission to start a new filter reading session.")

        try:
            with transaction.atomic():
                analysis = latest
                if analysis is None or analysis.state != "new":
                    analysis = FilteredWaterAnalysis(
                        treatment_plant=plant,
                        treatment_plant_phase=phase,
                    )
                    analysis.full_clean()
                    analysis.save()

                for limit in limits:
                    result, _created = FilteredWaterResults.objects.get_or_create(
                        analysis=analysis,
                        water_filter=limit.water_filter,
                        parameter=limit,
                        defaults={"result_value": None},
                    )
                    result.result_value = submitted[limit.pk]
                    result.full_clean()
                    result.save(update_fields=("result_value", "updated_at"))
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return self.filter_readings_redirect(plant)

        messages.success(request, f"Saved {len(limits)} filter parameter reading(s) for {phase.name}.")
        return self.filter_readings_redirect(plant)

    def filter_readings_redirect(self, plant):
        url = reverse("qc:quality_control")
        params = {"plant": plant.pk}
        selected_date = self.request.POST.get("date")
        if selected_date:
            params["date"] = selected_date
        return redirect(f"{url}?{urlencode(params)}")
