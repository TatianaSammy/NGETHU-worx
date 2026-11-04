from __future__ import annotations

import logging

from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View

from core.mixins import WanjalaMixin

try:
    from sc.models import ShiftAssignment
    _HAS_SC = True
except ImportError:
    _HAS_SC = False

from t.models import TreatmentPlant

from qc.models import (
    RawWaterResult,
    TreatedWaterResult,
    CoagulatedWaterResult,
    ClarifiedWaterResult,
    WaterAnalysisLog,
)

from qc.services.result_card_service import ResultCardService
from qc.services.operational_summary import (
    get_filter_sidebar_groups,
    get_operational_summary,
)


logger = logging.getLogger(__name__)


class ResultCardEntryView(
    WanjalaMixin,
    View,
):

    template_name = "qc/results/result_card.html"

    partial_template_name = (
        "qc/results/_result_cards.html"
    )

    topbar_template_name = (
        "qc/results/_result_topbar.html"
    )

    permission_required_view = (
        "qc.view_wateranalysis"
    )
    permission_required_update = "qc.change_wateranalysis"


    RESULT_MODELS = {

        "raw":
            RawWaterResult,

        "treated":
            TreatedWaterResult,

        "coagulated":
            CoagulatedWaterResult,

        "clarified":
            ClarifiedWaterResult,

    }


    def dispatch(
        self,
        request,
        *args,
        **kwargs,
    ):

        self.action = (
            "update"
            if request.method == "POST"
            else "view"
        )

        if not self.check_permission(self.action):
            raise PermissionDenied("You do not have permission to access QC results.")

        return super().dispatch(
            request,
            *args,
            **kwargs
        )

    def check_idle_timeout(self):
        return None


    def get(
        self,
        request,
        *args,
        **kwargs,
    ):

        assignment = (
            self.resolve_user_assignment()
        )

        plant = assignment["plant"]

        laboratory = assignment["laboratory"]

        if not plant:

            empty = {

                "cards": [],

                "plant": None,

                "laboratory":
                    laboratory,

                "entry_mode": True,

                "auto_refresh": True,

                "message":
                    "No treatment plant assigned.",

                "analysis_watermark": 0,

                "available_plants":
                    self.get_available_plants(),

                "current_dosages": get_operational_summary(None)[0],

                "phase_concentrations": [],

                "filter_phase_groups": [],

                "raw_water_snapshot": {
                    "available": False,
                    "parameters": [],
                    "abnormal_count": 0,
                    "entered_count": 0,
                    "total_count": 0,
                },

                "completion": {
                    "total": 0,
                    "entered": 0,
                    "pending": 0,
                    "abnormal": 0,
                    "percent": 0,
                },

            }

            if self.wants_json():
                return JsonResponse(
                    ResultCardService.serialize_live_payload(empty)
                )

            return self.render_context(empty)


        context = (
            ResultCardService
            .get_context(
                treatment_plant=plant,
                selected_date=self.resolve_date(),
            )
        )


        current_dosages, phase_concentrations = get_operational_summary(plant)
        needs_sidebar = not self.wants_json() and request.GET.get("partial") not in {
            "topbar",
            "cards",
        }
        filter_phase_groups = (
            get_filter_sidebar_groups(plant, request.user) if needs_sidebar else []
        )

        # Resolve current shift & supervisor for topbar display.
        # The sc.context_processors.shift_context processor already
        # supplies current_shift / current_shift_supervisor to every
        # template.  We only override those values here when the view
        # can resolve them successfully; otherwise we leave the
        # context-processor values untouched.
        shift_ctx: dict = {}
        if _HAS_SC:
            try:
                from sc.services.shift_service import get_current_shift_data
                shift_data = get_current_shift_data(request.user, request=request)
                cs = shift_data.get("current_shift")
                sup = shift_data.get("supervisor")
                if cs:
                    shift_ctx["current_shift"] = cs
                if sup:
                    shift_ctx["current_shift_supervisor"] = sup
                    shift_ctx["shift_supervisor"] = (
                        sup.get_full_name()
                        or getattr(sup, "username", str(sup))
                    )
            except Exception as e:
                logger.debug("[ResultCard] Shift resolution notice: %s", e)

        context.update({

            "entry_mode": True,

            "auto_refresh": True,

            "current_time":
                timezone.localtime(),

            "plant":
                plant,

            "laboratory":
                laboratory,

            "available_plants":
                self.get_available_plants(),

            "live_url":
                self.build_live_url(plant),

            "current_dosages": current_dosages,

            "phase_concentrations": phase_concentrations,

            "filter_phase_groups": filter_phase_groups,
        })

        # Merge shift data only if resolved — preserves context-processor values
        if shift_ctx:
            context.update(shift_ctx)

        if self.wants_json():
            return JsonResponse(
                ResultCardService.serialize_live_payload(context)
            )

        return self.render_context(
            context
        )

    def wants_json(self):
        accept = self.request.headers.get("Accept", "")
        return (
            self.request.GET.get("format") == "json"
            or self.request.GET.get("partial") == "live"
            or "application/json" in accept
        )

    def get_available_plants(self):
        user = self.request.user
        if user.is_superuser:
            return list(
                TreatmentPlant.objects
                .order_by("name")
                .values("id", "name")
            )
        plant = getattr(user, "assigned_treatment_plant", None)
        if not plant:
            return []
        return [{"id": plant.pk, "name": plant.name}]

    def build_live_url(self, plant):
        url = f"{self.request.path}?partial=live&format=json"
        if plant:
            url += f"&plant={plant.pk}"
        date = self.request.GET.get("date")
        if date:
            url += f"&date={date}"
        return url


    def resolve_user_assignment(
        self,
    ):

        user = self.request.user


        if user.is_superuser:
            plant_id = self.request.GET.get("plant") or self.request.POST.get("plant")
            if not plant_id and hasattr(self.request, "session"):
                plant_id = self.request.session.get("qc_selected_plant")

            plant = None
            if plant_id:
                try:
                    plant = TreatmentPlant.objects.filter(pk=int(plant_id)).first()
                except (ValueError, TypeError):
                    plant = None

            if plant is None:
                plant = TreatmentPlant.objects.filter(code="NG001").first() or TreatmentPlant.objects.order_by("name").first()

            if (
                plant
                and hasattr(self.request, "session")
                and self.request.session.get("qc_selected_plant") != plant.pk
            ):
                self.request.session["qc_selected_plant"] = plant.pk

            return {
                "plant": plant,
                "laboratory": getattr(user, "assigned_lab", None),
            }

        # An unassigned operator must not inherit the first plant's data.
        plant = getattr(user, "assigned_treatment_plant", None)
        return {
            "plant": plant,
            "laboratory": getattr(user, "assigned_lab", None),
        }
    @transaction.atomic
    def post(
        self,
        request,
        *args,
        **kwargs,
    ):

        submitted = (
            self.extract_submitted_values(
                request
            )
        )


        if not submitted:
            if self.is_htmx():
                return self.render_htmx_partial_response(0)

            messages.warning(
                request,
                "No results entered."
            )

            return self.redirect_back()



        updated = 0

        analysis_ids = set()


        for result_id, raw_value in submitted.items():

            result = (
                self.get_result_locked(
                    result_id
                )
            )


            if result is None:

                continue



            value = (
                self.parse_value(
                    raw_value
                )
            )


            if value is None:

                continue



            result.value = value


            result.save(
                update_fields=[
                    "value",
                    "updated_at",
                ]
            )


            analysis_ids.add(
                result.analysis_id
            )


            updated += 1



        if updated:

            self.create_audit_logs(
                analysis_ids=analysis_ids,
                count=updated,
                user=request.user,
            )


            messages.success(
                request,
                f"{updated} results saved successfully."
            )


        else:

            messages.warning(
                request,
                "No valid values were saved."
            )



        if self.is_htmx():
            return self.render_htmx_partial_response(updated)

        return self.redirect_back()

    def render_htmx_partial_response(self, updated=0):
        assignments = self.resolve_user_assignment()
        plant = assignments["plant"]
        laboratory = assignments["laboratory"]
        if plant is None:
            context = ResultCardService.get_empty_context()
        else:
            context = ResultCardService.get_context(
                treatment_plant=plant,
                selected_date=self.resolve_date(),
            )
        context.update({
            "entry_mode": True,
            "auto_refresh": True,
            "current_time": timezone.localtime(),
            "plant": plant,
            "laboratory": laboratory,
            "available_plants": self.get_available_plants(),
            "live_url": self.build_live_url(plant),
        })
        resp = render(self.request, self.partial_template_name, context)
        resp["HX-Trigger"] = '{"resultsSaved": true, "updated": %d}' % updated
        return resp



    def redirect_back(
        self,
    ):

        url = self.request.path


        params = []


        plant = (
            self.request.GET.get(
                "plant"
            )
        )

        date = (
            self.request.GET.get(
                "date"
            )
        )


        if not plant and hasattr(self.request, "session"):
            plant = self.request.session.get("qc_selected_plant")

        if plant:
            params.append(
                f"plant={plant}"
            )


        if date:

            params.append(
                f"date={date}"
            )


        if params:

            url += (
                "?"
                +
                "&".join(params)
            )


        return redirect(
            url
        )



    @staticmethod
    def extract_submitted_values(
        request,
    ):

        values = {}


        for key, value in request.POST.items():

            if not key.startswith(
                "result_"
            ):

                continue


            result_id = (
                key.replace(
                    "result_",
                    "",
                )
            )


            if result_id.isdigit():

                values[
                    int(result_id)
                ] = value.strip()


        return values



    def get_result_locked(
        self,
        result_id,
    ):

        plant = self.resolve_user_assignment().get("plant")
        if plant is None:
            return None

        for model in (
            self.RESULT_MODELS.values()
        ):

            try:

                return (
                    model.objects
                    .select_for_update()
                    .get(
                        pk=result_id,
                        analysis__treatment_plant=plant,
                    )
                )


            except ObjectDoesNotExist:

                continue


        return None



    @staticmethod
    def parse_value(
        value,
    ):

        if not value:

            return None


        try:

            return Decimal(
                value
            )


        except InvalidOperation:

            return None



    @staticmethod
    def create_audit_logs(
        analysis_ids,
        count,
        user,
    ):

        for analysis_id in analysis_ids:

            WaterAnalysisLog.objects.create(

                analysis_id=
                    analysis_id,

                action=
                    "results_entered",

                remarks=
                    f"{count} results entered manually.",

                actor=
                    user,

                auto_generated=
                    False,

            )



    def resolve_date(
        self,
    ):

        value = (
            self.request.GET.get(
                "date"
            )
        )


        if not value:

            return timezone.localdate()



        try:

            return datetime.strptime(
                value,
                "%Y-%m-%d",
            ).date()


        except ValueError:

            return timezone.localdate()


    def render_context(
        self,
        context,
    ):

        partial = self.request.GET.get(
            "partial"
        )

        # Only serve card/topbar fragments when explicitly requested.
        # Treating every HTMX request as a cards partial caused the matrix
        # to flash empty / disappear on navigation boosts.
        if partial == "topbar":
            template = self.topbar_template_name
        elif partial == "cards":
            template = self.partial_template_name
        else:
            template = self.template_name


        response = render(
            self.request,
            template,
            context,
        )


        response[
            "X-Render-Mode"
        ] = (

            "partial"

            if template ==
            self.partial_template_name

            else

            "full"

        )


        response[
            "X-Generated-At"
        ] = (
            timezone.now()
            .isoformat()
        )


        response[
            "Cache-Control"
        ] = (
            "no-store, "
            "no-cache, "
            "must-revalidate, "
            "max-age=0"
        )


        if context.get(
            "plant"
        ):

            response[
                "X-Plant-ID"
            ] = str(
                context["plant"].pk
            )


        if context.get(
            "laboratory"
        ):

            response[
                "X-Laboratory-ID"
            ] = str(
                context["laboratory"].pk
            )


        return response
