# jartest/views_dashboard.py
from __future__ import annotations

import logging
from datetime import timedelta, date as dt_date
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Dict, Any, List, Optional

from django.contrib import messages
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views import View

from core.mixins import WanjalaMixin
from t.models import TreatmentPlant, CoagulatedWaterLimit
from ca.models import Chemical
from o.models import ChemicalCategory
import json
from django.core.serializers.json import DjangoJSONEncoder
from parameters.models import PhysicochemicalParameter
from jartest.models import (
    JarTest,
    JarResult,
    JarMeasurement,
    RawWater,
    RawWaterAnalysis,
    get_latest_dosage,
)
from qc.models import RawWaterResult, SamplingSchedule, WaterAnalysis
from jartest.services.dosage_prediction import build_dashboard_analytics, parameter_catalog, predict_dosage
from jartest.services.reports import build_report_payload, serialize_report_test

logger = logging.getLogger(__name__)


class JarTestDashboardView(WanjalaMixin, View):
    """
    State-of-the-art interactive Jar Test & Coagulation Optimization Dashboard.
    Provides a real-time, zero-scrollbar, PWA-enabled analytical bench for 6-jar
    rapid testing, flocculation observation, dosage-turbidity breakpoint curve,
    and multi-tier review approvals.
    """
    template_name = "jartest/results/dashboard.html"
    model = JarTest
    export_fields = [
        "Test date", "Treatment plant", "Coagulant", "Analyst",
        "Established dose (mg/L)", "Raw-water source", "Turbidity", "Color",
        "pH", "Selected raw-water value", "Jar count", "Review stage", "Remarks",
    ]
    export_field_map = {
        "Test date": "date",
        "Treatment plant": "plant",
        "Coagulant": "coagulant",
        "Analyst": "analyst",
        "Established dose (mg/L)": "dose",
        "Raw-water source": "raw_source",
        "Turbidity": "turbidity",
        "Color": "color",
        "pH": "ph",
        "Selected raw-water value": "selected_parameter_value",
        "Jar count": "jar_count",
        "Review stage": "stage",
        "Remarks": "remarks",
    }
    export_file_name = "jar_test_reports"
    partial_matrix_template = "jartest/results/partials/_jar_matrix.html"
    partial_topbar_template = "jartest/results/partials/_topbar.html"
    partial_chart_template = "jartest/results/partials/_coagulation_curve.html"

    def get_queryset(self):
        """Build the plant-scoped, user-filtered dataset used by reports and exports."""
        from django.db.models import Case, F, IntegerField, Q, Value, When
        from django.utils.dateparse import parse_date

        request = getattr(self, "request", None)
        plant = self.resolve_plant(request) if request else None
        if not plant:
            return JarTest.objects.none()

        queryset = JarTest.objects.filter(treatment_plant=plant)
        params = request.GET

        coagulant_id = params.get("report_coagulant")
        if coagulant_id and coagulant_id.isdigit():
            queryset = queryset.filter(coagulant_id=int(coagulant_id))

        for key, lookup in (("report_from", "test_date__gte"), ("report_to", "test_date__lte")):
            parsed = parse_date(params.get(key, ""))
            if parsed:
                queryset = queryset.filter(**{lookup: parsed})

        for key, lookup in (
            ("report_min_dose", "established_dosage_mg_per_l__gte"),
            ("report_max_dose", "established_dosage_mg_per_l__lte"),
        ):
            raw_value = params.get(key, "").strip()
            if raw_value:
                try:
                    value = Decimal(raw_value)
                    if value.is_finite() and value >= 0:
                        queryset = queryset.filter(**{lookup: value})
                except (InvalidOperation, ValueError):
                    pass

        stage = params.get("report_status", "").casefold()
        if stage == "in_progress":
            queryset = queryset.filter(verified_at__isnull=True, needs_review=False)
        elif stage == "verified":
            queryset = queryset.filter(verified_at__isnull=False, confirmed_at__isnull=True, needs_review=False)
        elif stage == "confirmed":
            queryset = queryset.filter(confirmed_at__isnull=False, approved_at__isnull=True, needs_review=False)
        elif stage == "approved":
            queryset = queryset.filter(approved_at__isnull=False, needs_review=False)
        elif stage == "needs_review":
            queryset = queryset.filter(needs_review=True)

        term = params.get("report_q", "").strip()
        if term:
            queryset = queryset.filter(
                Q(coagulant__name__icontains=term)
                | Q(analyst__username__icontains=term)
                | Q(analyst__first_name__icontains=term)
                | Q(analyst__last_name__icontains=term)
                | Q(raw_water__source_name__icontains=term)
                | Q(raw_water__analyses__parameter__parameter__name__icontains=term)
                | Q(remarks__icontains=term)
            )

        parameter_id = params.get("report_parameter")
        if parameter_id and parameter_id.isdigit() and CoagulatedWaterLimit.objects.filter(
            treatment_plant=plant, pk=int(parameter_id)
        ).exists():
            queryset = queryset.filter(raw_water__analyses__parameter_id=int(parameter_id))
            for key, lookup in (
                ("report_min_parameter", "raw_water__analyses__value__gte"),
                ("report_max_parameter", "raw_water__analyses__value__lte"),
            ):
                raw_value = params.get(key, "").strip()
                if raw_value:
                    try:
                        value = Decimal(raw_value)
                        if value.is_finite() and value >= 0:
                            queryset = queryset.filter(**{lookup: value})
                    except (InvalidOperation, ValueError):
                        pass

        sort = params.get("report_sort")
        if sort == "status":
            queryset = queryset.annotate(
                _report_stage_rank=Case(
                    When(needs_review=True, then=Value(0)),
                    When(approved_at__isnull=False, then=Value(4)),
                    When(confirmed_at__isnull=False, then=Value(3)),
                    When(verified_at__isnull=False, then=Value(2)),
                    default=Value(1),
                    output_field=IntegerField(),
                )
            )
        ordering = {
            "oldest": ("test_date", "pk"),
            "dose_high": (F("established_dosage_mg_per_l").desc(nulls_last=True), "-test_date"),
            "dose_low": (F("established_dosage_mg_per_l").asc(nulls_last=True), "-test_date"),
            "coagulant": ("coagulant__name", "-test_date"),
            "analyst": ("analyst__username", "-test_date"),
            "status": ("_report_stage_rank", "-test_date"),
        }.get(sort, ("-test_date", "-pk"))
        return queryset.distinct().select_related(
            "treatment_plant", "raw_water", "coagulant", "analyst",
            "verified_by", "confirmed_by", "approved_by",
        ).prefetch_related(
            "raw_water__analyses__parameter__parameter", "jar_results",
        ).order_by(*ordering)

    def format_export_value(self, obj, field):
        """Export the same linked/raw-water fields that appear in the report table."""
        row = getattr(obj, "_jt_report_export_row", None)
        if row is None:
            parameter_id = self.request.GET.get("report_parameter")
            parameter_id = int(parameter_id) if parameter_id and parameter_id.isdigit() else None
            row = serialize_report_test(obj, parameter_id)
            obj._jt_report_export_row = row
        value = row.get(self.export_field_map.get(field, field))
        return "" if value is None else str(value)

    def check_idle_timeout(self):
        return None

    def resolve_plant(self, request) -> Optional[TreatmentPlant]:
        user = request.user
        plant_id = request.GET.get("plant") or request.POST.get("plant")

        if user.is_superuser:
            if not plant_id and hasattr(request, "session"):
                plant_id = request.session.get("qc_selected_plant")
            if plant_id:
                try:
                    plant = TreatmentPlant.objects.filter(pk=int(plant_id)).first()
                    if plant:
                        request.session["qc_selected_plant"] = plant.pk
                        return plant
                except (ValueError, TypeError):
                    pass
            plant = TreatmentPlant.objects.order_by("name").first()
            if plant and hasattr(request, "session"):
                request.session["qc_selected_plant"] = plant.pk
            return plant

        # Non-superuser
        plant = getattr(user, "assigned_treatment_plant", None)
        if plant and plant_id:
            try:
                if int(plant_id) != plant.pk:
                    return plant
            except (ValueError, TypeError):
                return plant
        return plant

    def resolve_date(self, request) -> dt_date:
        raw = request.GET.get("date") or request.POST.get("date")
        if raw:
            try:
                return timezone.datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                pass
        return timezone.localdate()

    def get_available_plants(self, request):
        """Return TreatmentPlant queryset so templates can use p.pk / p.name."""
        if request.user.is_superuser:
            return TreatmentPlant.objects.order_by("name")
        plant = self.resolve_plant(request)
        if plant:
            return TreatmentPlant.objects.filter(pk=plant.pk)
        return TreatmentPlant.objects.none()

    def get_available_coagulants(self) -> List[Chemical]:
        coagulants = list(
            Chemical.objects.filter(
                categories__category=ChemicalCategory.Category.COAGULANT
            ).distinct().order_by("name")
        )
        if not coagulants:
            # Fallback to any chemical if category not strictly tagged
            coagulants = list(Chemical.objects.order_by("name")[:10])
        return coagulants

    def get_current_qc_raw_water_analysis(self, plant: TreatmentPlant, test_date: dt_date):
        """Return the newest raw-water QC sample for this plant, no later than the test date."""
        if not plant:
            return None
        return (
            WaterAnalysis.objects.filter(
                treatment_plant=plant,
                analysis_date__lte=test_date,
                analysis_time__schedule__water_type=SamplingSchedule.RAW,
                analysis_time__schedule__treatment_plant=plant,
                rawwaterresults__isnull=False,
            )
            .select_related("analysis_time", "analysis_time__schedule")
            .order_by("-analysis_date", "-analysis_time__time", "-pk")
            .first()
        )

    def get_or_create_raw_water(self, plant: TreatmentPlant, test_date: dt_date, coagulant: Optional[Chemical]) -> Optional[RawWater]:
        """Bind the jar test to the selected plant's current QC sample when available."""
        qc_analysis = self.get_current_qc_raw_water_analysis(plant, test_date)
        if qc_analysis:
            sampled_at = timezone.make_aware(
                timezone.datetime.combine(
                    qc_analysis.analysis_date,
                    qc_analysis.analysis_time.time,
                )
            )
            rw = RawWater.objects.filter(
                treatment_plant=plant,
                sampled_at=sampled_at,
            ).first()
            if rw:
                return rw

            if not coagulant:
                coagulant = next(iter(self.get_available_coagulants()), None)
            if not coagulant:
                coagulant, _ = Chemical.objects.get_or_create(
                    name="Alum (Aluminium Sulphate)",
                    defaults={"code": "ALUM", "description": "Aluminium Sulphate Coagulant"}
                )

            return RawWater.objects.create(
                treatment_plant=plant,
                sampled_at=sampled_at,
                source_name=f"{plant.name} Raw Water · {qc_analysis.reference_id}",
                location_description=f"QC sample {qc_analysis.reference_id}",
                coagulant=coagulant,
                coagulant_concentration_ppm=Decimal("18.50"),
                remarks=f"Linked to current plant raw-water QC sample {qc_analysis.reference_id}.",
            )

        # A manually captured raw-water record is still valid when QC has no
        # sample for this plant and date. Never borrow another plant's source.
        return (
            RawWater.objects.filter(
                treatment_plant=plant,
                sampled_at__date__lte=test_date,
            )
            .order_by("-sampled_at", "-pk")
            .first()
        )

    def get_or_create_jartest(self, plant: TreatmentPlant, test_date: dt_date, user) -> Optional[JarTest]:
        if not plant:
            return None

        coagulants = self.get_available_coagulants()
        default_coagulant = coagulants[0] if coagulants else None
        if not default_coagulant:
            default_coagulant, _ = Chemical.objects.get_or_create(
                name="Alum (Aluminium Sulphate)",
                defaults={"code": "ALUM", "description": "Aluminium Sulphate Coagulant"}
            )

        raw_water = self.get_or_create_raw_water(plant, test_date, default_coagulant)
        if not raw_water:
            return None

        jartest, created = JarTest.objects.get_or_create(
            treatment_plant=plant,
            test_date=test_date,
            defaults={
                "raw_water": raw_water,
                "coagulant": default_coagulant,
                "analyst": user if user.is_authenticated else None,
            }
        )

        # Keep an in-progress test attached to the latest source sample for its
        # plant/date. Preserve the source once review has started.
        if (
            not created
            and jartest.raw_water_id != raw_water.pk
            and not (jartest.verified_at or jartest.confirmed_at or jartest.approved_at)
        ):
            jartest.raw_water = raw_water
            jartest.save(update_fields=["raw_water"])
        if jartest.raw_water.treatment_plant_id != plant.pk:
            logger.error(
                "Jar test source plant mismatch test=%s selected_plant=%s source_plant=%s",
                jartest.pk,
                plant.pk,
                jartest.raw_water.treatment_plant_id,
            )
            return None

        # Ensure all 6 Jars exist with stepped progressive dosages
        default_dosages = [
            Decimal("10.00"), Decimal("15.00"), Decimal("20.00"),
            Decimal("25.00"), Decimal("30.00"), Decimal("35.00")
        ]
        existing_jars = {j.jar_number: j for j in jartest.jar_results.all()}
        new_jars = []

        for jar_num in range(1, 7):
            if jar_num not in existing_jars:
                new_jars.append(JarResult(
                    jar_test=jartest,
                    jar_number=jar_num,
                    dosage_mg_per_l=default_dosages[jar_num - 1]
                ))

        if new_jars:
            JarResult.objects.bulk_create(new_jars)

        # Ensure parameters are mapped to jar measurements
        limits = list(CoagulatedWaterLimit.objects.filter(treatment_plant=plant).select_related("parameter"))
        if not limits:
            # Autocreate standard Coagulated limits if missing
            turbidity_param, _ = PhysicochemicalParameter.objects.get_or_create(
                name="Turbidity", defaults={"unit": "NTU"}
            )
            color_param, _ = PhysicochemicalParameter.objects.get_or_create(
                name="Color", defaults={"unit": "mg Pt/L"}
            )
            ph_param, _ = PhysicochemicalParameter.objects.get_or_create(
                name="pH", defaults={"unit": "pH units"}
            )
            for p, mn, mx in [(turbidity_param, Decimal("0.1"), Decimal("5.0")),
                              (color_param, Decimal("0.0"), Decimal("15.0")),
                              (ph_param, Decimal("6.5"), Decimal("8.5"))]:
                lim, _ = CoagulatedWaterLimit.objects.get_or_create(
                    treatment_plant=plant,
                    parameter=p,
                    defaults={"min_value": mn, "max_value": mx}
                )
                limits.append(lim)

        for jar in jartest.jar_results.all():
            existing_measurements = {m.parameter_id for m in jar.measurements.all()}
            new_measurements = []
            for lim in limits:
                if lim.pk not in existing_measurements:
                    new_measurements.append(JarMeasurement(
                        jar_result=jar,
                        parameter=lim,
                        value=Decimal("0.000")
                    ))
            if new_measurements:
                JarMeasurement.objects.bulk_create(new_measurements)

        return jartest

    def get_qc_raw_water_telemetry(self, plant, test_date):
        """
        Extract live raw water telemetry from qc.models.RawWaterResult for the given plant and date.
        If no results for test_date, fallback to the latest available WaterAnalysis for the plant.
        """
        ticker = []
        metrics = {
            "turbidity": Decimal("48.5"),
            "color": Decimal("120.0"),
            "ph": Decimal("7.20"),
            "temp": Decimal("21.5"),
            "coagulant_ppm": Decimal("18.5"),
        }
        meta = {
            "source": "QC Baseline Telemetry",
            "is_live": False,
            "analysis_id": None,
            "timestamp": None,
            "count": 0,
        }

        if not plant:
            return metrics, ticker, meta

        try:
            latest_analysis = self.get_current_qc_raw_water_analysis(plant, test_date)
            qc_results = list(
                RawWaterResult.objects.filter(analysis=latest_analysis)
                .select_related("parameter__parameter", "analysis")
                .order_by("-id")
            ) if latest_analysis else []

            if qc_results:
                meta["is_live"] = True
                meta["count"] = len(qc_results)
                first_item = qc_results[0]
                meta["analysis_id"] = getattr(first_item.analysis, "reference_id", f"WA-{first_item.analysis_id}")
                meta["source"] = f"QC Analysis #{meta['analysis_id']} ({first_item.analysis.analysis_date})"
                meta["timestamp"] = str(first_item.analysis.analysis_date)

                for r in qc_results:
                    try:
                        param_obj = r.parameter.parameter
                        p_name = param_obj.name
                        p_unit = param_obj.unit or "mg/L"
                        p_val = r.value
                        min_l = getattr(r.parameter, "min_value", None)
                        max_l = getattr(r.parameter, "max_value", None)

                        is_abnormal = False
                        if p_val is not None:
                            if min_l is not None and p_val < min_l:
                                is_abnormal = True
                            if max_l is not None and p_val > max_l:
                                is_abnormal = True

                        ticker.append({
                            "name": p_name,
                            "short_name": p_name[:14],
                            "display_value": f"{p_val:.2f}" if p_val is not None else "--",
                            "value": p_val,
                            "unit": p_unit,
                            "abnormal": is_abnormal,
                            "min_value": min_l,
                            "max_value": max_l,
                        })

                        if p_val is not None:
                            p_low = p_name.lower()
                            if "turb" in p_low:
                                metrics["turbidity"] = p_val
                            elif "color" in p_low or "colour" in p_low:
                                metrics["color"] = p_val
                            elif "ph" in p_low and "alpha" not in p_low:
                                metrics["ph"] = p_val
                            elif "temp" in p_low:
                                metrics["temp"] = p_val
                    except Exception as err:
                        logger.debug(f"Error parsing raw water result {r.id}: {err}")
        except Exception as e:
            logger.warning(f"QC Raw water telemetry fetch failed: {e}")

        return metrics, ticker, meta

    def sync_qc_raw_water_to_jartest(self, jartest, plant, test_date) -> int:
        """
        Copies/updates parameter values from QC RawWaterResult into jartest.raw_water.analyses
        """
        if (
            not jartest
            or not plant
            or not jartest.raw_water
            or jartest.raw_water.treatment_plant_id != plant.pk
        ):
            return 0

        synced = 0
        try:
            latest_analysis = self.get_current_qc_raw_water_analysis(plant, test_date)
            qc_results = list(
                RawWaterResult.objects.filter(analysis=latest_analysis)
                .select_related("parameter__parameter")
            ) if latest_analysis else []

            coag_limits = {
                l.parameter_id: l
                for l in CoagulatedWaterLimit.objects.filter(treatment_plant=plant)
            }

            for r in qc_results:
                if r.value is not None and r.parameter and r.parameter.parameter_id in coag_limits:
                    limit_obj = coag_limits[r.parameter.parameter_id]
                    RawWaterAnalysis.objects.update_or_create(
                        raw_water=jartest.raw_water,
                        parameter=limit_obj,
                        defaults={"value": r.value}
                    )
                    synced += 1
        except Exception as e:
            logger.error(f"Error syncing QC raw water to jartest: {e}")

        return synced

    def build_context(self, request) -> Dict[str, Any]:
        self.request = request
        plant = self.resolve_plant(request)
        test_date = self.resolve_date(request)
        jartest = self.get_or_create_jartest(plant, test_date, request.user) if plant else None

        # Fetch limits for column headers
        limits = list(
            CoagulatedWaterLimit.objects.filter(treatment_plant=plant)
            .select_related("parameter")
            .order_by("parameter__name")
        ) if plant else []

        # Live Raw Water telemetry from QC module
        qc_metrics, raw_water_ticker, telemetry_meta = self.get_qc_raw_water_telemetry(plant, test_date)

        # The active jar test follows the latest raw-water sample for this
        # plant/date. Keep its local measurement snapshot aligned with QC.
        if (
            jartest
            and telemetry_meta.get("is_live")
            and not (jartest.verified_at or jartest.confirmed_at or jartest.approved_at)
        ):
            self.sync_qc_raw_water_to_jartest(jartest, plant, test_date)

        # Baseline metrics (default or imported from QC)
        raw_metrics = {
            "turbidity": qc_metrics.get("turbidity", Decimal("48.5")),
            "color": qc_metrics.get("color", Decimal("120.0")),
            "ph": qc_metrics.get("ph", Decimal("7.20")),
            "temp": qc_metrics.get("temp", Decimal("21.5")),
            "coagulant_ppm": qc_metrics.get("coagulant_ppm", Decimal("18.5")),
        }

        # Override with jartest.raw_water analyses if explicitly recorded
        if jartest and jartest.raw_water:
            rw = jartest.raw_water
            if rw.coagulant_concentration_ppm:
                raw_metrics["coagulant_ppm"] = rw.coagulant_concentration_ppm
            for a in rw.analyses.select_related("parameter__parameter").all():
                pname = a.parameter.parameter.name.lower()
                if "turb" in pname:
                    raw_metrics["turbidity"] = a.value
                elif "color" in pname or "colour" in pname:
                    raw_metrics["color"] = a.value
                elif "ph" in pname and "alpha" not in pname:
                    raw_metrics["ph"] = a.value

        # Process each of the 6 jars
        jars = []
        chart_points = []
        optimal_jar = None

        if jartest:
            raw_turb = raw_metrics["turbidity"] or Decimal("1")
            raw_col = raw_metrics["color"] or Decimal("1")

        for jar in (
            jartest.jar_results.prefetch_related("measurements__parameter__parameter").order_by("jar_number")
            if jartest else []
        ):
                measurements_dict = {}
                turbidity_val = None
                color_val = None
                ph_val = None

                for m in jar.measurements.all():
                    param_name = m.parameter.parameter.name
                    measurements_dict[m.parameter.pk] = {
                        "obj": m,
                        "value": m.value,
                        "param_name": param_name,
                        "unit": m.parameter.parameter.unit,
                        "min": m.parameter.min_value,
                        "max": m.parameter.max_value,
                        "in_spec": (
                            (m.parameter.min_value is None or m.value >= m.parameter.min_value) and
                            (m.parameter.max_value is None or m.value <= m.parameter.max_value)
                        ) if m.value > 0 else None
                    }
                    pname_lower = param_name.lower()
                    if "turb" in pname_lower:
                        turbidity_val = m.value
                    elif "color" in pname_lower:
                        color_val = m.value
                    elif "ph" in pname_lower:
                        ph_val = m.value

                # Removal efficiencies
                turb_removal = None
                if turbidity_val is not None and raw_turb > 0:
                    turb_removal = max(Decimal("0"), min(Decimal("100"), ((raw_turb - turbidity_val) / raw_turb) * Decimal("100")))

                col_removal = None
                if color_val is not None and raw_col > 0:
                    col_removal = max(Decimal("0"), min(Decimal("100"), ((raw_col - color_val) / raw_col) * Decimal("100")))

                # Floc observation time formatting (synced to JarResult.floc_observation_time)
                floc_str = "00:00"
                if jar.floc_observation_time:
                    secs = int(jar.floc_observation_time.total_seconds())
                    mins, rem = divmod(max(secs, 0), 60)
                    floc_str = f"{mins:02d}:{rem:02d}"
                opt_dosage_ref = jartest.established_dosage_mg_per_l if jartest else None
                if jar.is_optimal:
                    status_class = "optimal"
                    status_label = "Optimal"
                elif jar.dosage_mg_per_l and opt_dosage_ref and jar.dosage_mg_per_l < opt_dosage_ref:
                    status_class = "under"
                    status_label = "Under-Dose"
                elif jar.dosage_mg_per_l and opt_dosage_ref and jar.dosage_mg_per_l > opt_dosage_ref:
                    status_class = "over"
                    status_label = "Over-Dose"
                else:
                    status_class = "standard"
                    status_label = "Awaiting readings"

                jar_data = {
                    "obj": jar,
                    "jar_number": jar.jar_number,
                    "dosage": jar.dosage_mg_per_l,
                    "floc_time_str": floc_str,
                    "floc_seconds": jar.floc_observation_seconds or 0,
                    "is_optimal": jar.is_optimal,
                    "rank": jar.performance_rank,
                    "status_class": status_class,
                    "status_label": status_label,
                    "measurements": measurements_dict,
                    "turbidity": turbidity_val,
                    "turbidity_removal": turb_removal,
                    "color": color_val,
                    "color_removal": col_removal,
                    "ph": ph_val,
                }
                jars.append(jar_data)

                if jar.is_optimal:
                    optimal_jar = jar_data

                chart_points.append({
                    "jar": jar.jar_number,
                    "dosage": float(jar.dosage_mg_per_l),
                    "turbidity": float(turbidity_val) if turbidity_val is not None else 0.0,
                    "color": float(color_val) if color_val is not None else 0.0,
                    "ph": float(ph_val) if ph_val is not None else 7.0,
                    "is_optimal": jar.is_optimal,
                    "rank": jar.performance_rank or jar.jar_number,
                })

        # Compute operational dosage benchmarking & latest approved dosage
        latest_approved_dosage = None
        if jartest and jartest.coagulant and plant:
            try:
                latest_approved_dosage = get_latest_dosage(jartest.coagulant, plant)
            except Exception as e:
                logger.warning(f"Failed to fetch latest approved dosage: {e}")

        # Dosage variance calculation
        dosage_delta = None
        dosage_savings_type = "optimal"
        plant_ppm = raw_metrics.get("coagulant_ppm")
        opt_dosage = jartest.established_dosage_mg_per_l if jartest else None

        if opt_dosage is not None and plant_ppm is not None:
            dosage_delta = opt_dosage - plant_ppm
            if dosage_delta < 0:
                dosage_savings_type = "decrease"  # Saving coagulant!
            elif dosage_delta > 0:
                dosage_savings_type = "increase"  # Adjustment required
            else:
                dosage_savings_type = "optimal"

        chart_payload = {
            "points": chart_points,
            "raw_turbidity": float(raw_metrics.get("turbidity") or 48.5),
            "raw_color": float(raw_metrics.get("color") or 120.0),
            "raw_ph": float(raw_metrics.get("ph") or 7.2),
            "plant_dosage": float(plant_ppm or 18.5),
            "optimal_dosage": float(opt_dosage) if opt_dosage else None,
            "latest_approved": float(latest_approved_dosage) if latest_approved_dosage else None,
        }

        available_coagulants = self.get_available_coagulants()
        selected_coagulant = (
            jartest.coagulant if jartest and jartest.coagulant_id
            else available_coagulants[0] if available_coagulants else None
        )
        sample_cutoff = jartest.raw_water.sampled_at if jartest and jartest.raw_water_id else None
        include_intelligence = not request.headers.get("HX-Request") and not request.GET.get("partial")
        if plant and include_intelligence:
            cache_key_intel = f"jt_intel_{plant.pk}_{selected_coagulant.pk if selected_coagulant else 0}_{sample_cutoff}"
            dosage_analytics = cache.get(cache_key_intel)
            if dosage_analytics is None:
                dosage_analytics = build_dashboard_analytics(plant, selected_coagulant, sample_at=sample_cutoff)
                cache.set(cache_key_intel, dosage_analytics, 180)
        else:
            dosage_analytics = {
                "catalog": [], "history": [], "correlations": [], "features": [],
                "summary": {"count": 0, "median": None, "mean": None, "stdev": None, "q1": None, "q3": None, "min": None, "max": None},
                "prior_sample": None, "has_model": False,
            }

        report_parameter_id = request.GET.get("report_parameter")
        report_parameter_id = int(report_parameter_id) if report_parameter_id and report_parameter_id.isdigit() else None
        if report_parameter_id and not any(item.pk == report_parameter_id for item in limits):
            report_parameter_id = None
        selected_report_parameter = next(
            (item for item in limits if item.pk == report_parameter_id), None
        )

        if plant and include_intelligence:
            has_report_filters = any(k.startswith("report_") for k in request.GET)
            if not has_report_filters:
                cache_key_reports = f"jt_reports_{plant.pk}_{report_parameter_id}"
                reports = cache.get(cache_key_reports)
                if reports is None:
                    reports = build_report_payload(self.get_queryset(), report_parameter_id)
                    cache.set(cache_key_reports, reports, 180)
            else:
                reports = build_report_payload(self.get_queryset(), report_parameter_id)
        else:
            reports = {
                "rows": [],
                "summary": {"records": 0, "with_dose": 0, "approved": 0, "median": None, "mean": None, "stdev": None, "q1": None, "q3": None, "min": None, "max": None, "trend_per_day": None, "turbidity_rho": None, "turbidity_p": None},
                "timeline": [], "histogram": {"centers": [], "counts": []},
                "control": {"center": None, "lower": None, "upper": None},
                "associations": [], "groups": [], "group_test": None,
            }
        report_page_obj = Paginator(reports["rows"], 50).get_page(request.GET.get("report_page", 1))
        reports["rows"] = list(report_page_obj.object_list)
        report_page_query = request.GET.copy()
        report_page_query.pop("report_page", None)
        default_association = selected_report_parameter
        if not default_association and reports["associations"]:
            preferred = next(
                (item for item in reports["associations"] if "turb" in item["label"].casefold()),
                reports["associations"][0],
            )
            try:
                default_id = int(preferred["parameter"].partition(":")[2])
                default_association = next((item for item in limits if item.pk == default_id), None)
            except (TypeError, ValueError):
                default_association = None
        default_association = default_association or next(
            (item for item in limits if "turb" in item.parameter.name.casefold()),
            limits[0] if limits else None,
        )
        current_raw_values = {}
        if include_intelligence and jartest and jartest.raw_water:
            current_raw_values = {
                str(item.parameter.parameter_id): float(item.value)
                for item in jartest.raw_water.analyses.select_related("parameter__parameter")
            }
        prior_values = (dosage_analytics.get("prior_sample") or {}).get("values", {})
        feature_parameter_ids = {
            item["parameter_id"] for item in dosage_analytics.get("features", [])
            if item["kind"] == "current"
        }
        predictor_parameters = [
            {
                **item,
                "value": current_raw_values.get(str(item["id"]), prior_values.get(str(item["id"]), "")),
                "prior_value": prior_values.get(str(item["id"])),
                "modeled": item["id"] in feature_parameter_ids,
                "value_from_prior": str(item["id"]) not in current_raw_values and str(item["id"]) in prior_values,
            }
            for item in dosage_analytics.get("catalog", [])
        ]

        return {
            "show_jartest_workspace_tabs": bool(plant),
            "plant": plant,
            "test_date": test_date,
            "jartest": jartest,
            "raw_water": jartest.raw_water if jartest else None,
            "jars": jars,
            "limits": limits,
            "optimal_jar": optimal_jar,
            "raw_metrics": raw_metrics,
            "chart_points": chart_points,
            "chart_json": json.dumps(chart_payload, cls=DjangoJSONEncoder),
            "raw_water_ticker": raw_water_ticker,
            "telemetry_meta": telemetry_meta,
            "latest_approved_dosage": latest_approved_dosage,
            "dosage_delta": dosage_delta,
            "dosage_savings_type": dosage_savings_type,
            "needs_review": getattr(jartest, "needs_review", False),
            "available_plants": self.get_available_plants(request),
            "available_coagulants": available_coagulants,
            "report_parameters": limits,
            "selected_coagulant": selected_coagulant,
            "dosage_analytics": dosage_analytics,
            "jar_test_reports": reports,
            "report_page_obj": report_page_obj,
            "report_page_query": report_page_query.urlencode(),
            "selected_report_parameter": selected_report_parameter,
            "selected_report_association_id": default_association.pk if default_association else None,
            "predictor_parameters": predictor_parameters,
            "laboratory": getattr(request.user, "assigned_lab", None),
            "current_user": request.user,
        }

    def get(self, request, *args, **kwargs):
        self.check_idle_timeout()
        context = self.build_context(request)

        partial = request.GET.get("partial")
        if partial == "matrix":
            return render(request, self.partial_matrix_template, context)
        if partial == "topbar":
            return render(request, self.partial_topbar_template, context)
        if partial == "chart":
            return render(request, self.partial_chart_template, context)

        return render(request, self.template_name, context)

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        self.check_idle_timeout()
        plant = self.resolve_plant(request)
        test_date = self.resolve_date(request)
        jartest = self.get_or_create_jartest(plant, test_date, request.user)

        action = request.POST.get("action", "save_jars")

        if action == "save_jars" and jartest:
            # 1. Update test header fields
            coagulant_id = request.POST.get("coagulant")
            if coagulant_id:
                try:
                    c = Chemical.objects.filter(pk=int(coagulant_id)).first()
                    if c:
                        jartest.coagulant = c
                except (ValueError, TypeError):
                    pass

            remarks = request.POST.get("remarks")
            if remarks is not None:
                jartest.remarks = remarks
            if request.user.is_authenticated:
                jartest.analyst = request.user

            # Update raw water properties if provided (RawWater.source_name / coagulant_concentration_ppm)
            if jartest.raw_water:
                rw_ppm = request.POST.get("coagulant_concentration_ppm")
                if rw_ppm:
                    try:
                        jartest.raw_water.coagulant_concentration_ppm = Decimal(str(rw_ppm).strip())
                    except (InvalidOperation, ValueError):
                        pass
                jartest.raw_water.save()

            jartest.save()

            # 2. Update each of the 6 Jars
            for jar in jartest.jar_results.all():
                prefix = f"jar_{jar.jar_number}_"
                dosage_raw = request.POST.get(f"{prefix}dosage")
                if dosage_raw:
                    try:
                        jar.dosage_mg_per_l = Decimal(str(dosage_raw).strip())
                    except (InvalidOperation, ValueError):
                        pass

                # Floc observation time (MM:SS or total seconds)
                floc_time_raw = request.POST.get(f"{prefix}floc_time")
                if floc_time_raw:
                    try:
                        if ":" in str(floc_time_raw):
                            parts = str(floc_time_raw).split(":")
                            mins = int(parts[0])
                            secs = int(parts[1])
                            jar.floc_observation_time = timedelta(minutes=mins, seconds=secs)
                        else:
                            jar.floc_observation_time = timedelta(seconds=int(floc_time_raw))
                    except (ValueError, TypeError):
                        pass

                jar.save()

                # Update parameter measurements
                for m in jar.measurements.all():
                    val_raw = request.POST.get(f"{prefix}param_{m.parameter.pk}")
                    if val_raw is not None and str(val_raw).strip() != "":
                        try:
                            m.value = Decimal(str(val_raw).strip())
                            m.save()
                        except (InvalidOperation, ValueError):
                            pass

            # 3. Calculate ranks and optimal dosage
            jartest.rank_jar_results()
            messages.success(request, "Jar test analytical data updated, ranked, and breakpoint dosage computed.")

        elif action == "seed_ladder" and jartest:
            ladder_dosages = [
                Decimal("10.00"),
                Decimal("15.00"),
                Decimal("20.00"),
                Decimal("25.00"),
                Decimal("30.00"),
                Decimal("35.00"),
            ]
            for jar in jartest.jar_results.all().order_by("jar_number"):
                idx = jar.jar_number - 1
                if 0 <= idx < len(ladder_dosages):
                    jar.dosage_mg_per_l = ladder_dosages[idx]
                    jar.save(update_fields=["dosage_mg_per_l"])
            messages.success(request, "Applied standard coagulation dosage ladder (10.00 to 35.00 mg/L).")

        elif action == "verify" and jartest:
            try:
                jartest.verify(request.user)
                jartest.save()
                messages.success(request, f"Jar test verified by {request.user.username}.")
            except Exception as e:
                messages.error(request, str(e))

        elif action == "confirm" and jartest:
            try:
                jartest.confirm(request.user)
                jartest.save()
                messages.success(request, f"Jar test confirmed by {request.user.username}.")
            except Exception as e:
                messages.error(request, str(e))

        elif action == "approve" and jartest:
            try:
                jartest.approve(request.user)
                jartest.save()
                messages.success(request, f"Jar test officially approved by {request.user.username}. Recommended dosage locked.")
            except Exception as e:
                messages.error(request, str(e))

        elif action == "sync_qc_telemetry" and jartest:
            try:
                synced_count = self.sync_qc_raw_water_to_jartest(jartest, plant, test_date)
                if synced_count > 0:
                    messages.success(request, f"Imported {synced_count} live Raw Water parameters from QC app into Jar Test baseline.")
                else:
                    messages.info(request, "Synced latest QC live raw water telemetry values into session dashboard.")
            except Exception as e:
                messages.error(request, f"Telemetry sync error: {e}")

        elif action == "revert_verification" and jartest:
            try:
                jartest.revert_verification()
                jartest.save()
                messages.info(request, "Verification reverted to in-progress status.")
            except Exception as e:
                messages.error(request, str(e))

        elif action == "revert_confirmation" and jartest:
            try:
                jartest.revert_confirmation()
                jartest.save()
                messages.info(request, "Confirmation reverted.")
            except Exception as e:
                messages.error(request, str(e))

        elif action == "revert_approval" and jartest:
            try:
                jartest.revert_approval()
                jartest.save()
                messages.info(request, "Approval reverted.")
            except Exception as e:
                messages.error(request, str(e))

        context = self.build_context(request)

        # Return partial for HTMX requests
        if request.headers.get("HX-Request"):
            return render(request, self.partial_matrix_template, context)

        return render(request, self.template_name, context)


class DosagePredictionView(WanjalaMixin, View):
    """AJAX endpoint for evidence-backed, plant-scoped dosage estimates."""

    def post(self, request, *args, **kwargs):
        dashboard = JarTestDashboardView()
        plant = dashboard.resolve_plant(request)
        if not plant:
            return JsonResponse({"ok": False, "status": "forbidden", "message": "Your account is not assigned to a treatment plant."}, status=403)

        try:
            coagulant_id = int(request.POST.get("coagulant", ""))
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "status": "invalid_coagulant", "message": "Select a valid coagulant."}, status=400)

        allowed_coagulant_ids = {item.pk for item in dashboard.get_available_coagulants()}
        if coagulant_id not in allowed_coagulant_ids:
            return JsonResponse({"ok": False, "status": "invalid_coagulant", "message": "Select a coagulant available to the jar-test dashboard."}, status=400)
        coagulant = Chemical.objects.filter(pk=coagulant_id).first()
        if not coagulant:
            return JsonResponse({"ok": False, "status": "invalid_coagulant", "message": "The selected coagulant could not be found."}, status=400)

        catalog = {str(item["id"]): item for item in parameter_catalog(plant)}
        inputs = {}
        for name, raw_value in request.POST.items():
            if not name.startswith("raw_") or not str(raw_value).strip():
                continue
            parameter_id = name.removeprefix("raw_")
            parameter = catalog.get(parameter_id)
            if not parameter:
                return JsonResponse({"ok": False, "status": "invalid_parameter", "message": "A submitted raw-water parameter is not configured for this plant."}, status=400)
            try:
                value = float(raw_value)
            except (TypeError, ValueError, OverflowError):
                return JsonResponse({"ok": False, "status": "invalid_value", "message": f"Enter a numeric value for {parameter['name']}."}, status=400)
            if not isfinite(value) or value < 0:
                return JsonResponse({"ok": False, "status": "invalid_value", "message": f"Enter a finite, non-negative value for {parameter['name']}."}, status=400)
            if "ph" in parameter["name"].casefold() and value > 14:
                return JsonResponse({"ok": False, "status": "invalid_value", "message": "pH must be between 0 and 14."}, status=400)
            inputs[int(parameter_id)] = value

        raw_sample_date = request.POST.get("sample_date")
        sample_at = None
        if raw_sample_date:
            try:
                sample_day = timezone.datetime.strptime(raw_sample_date, "%Y-%m-%d").date()
                sample_at = timezone.make_aware(
                    timezone.datetime.combine(sample_day, timezone.datetime.min.time()),
                    timezone.get_current_timezone(),
                )
            except (TypeError, ValueError):
                return JsonResponse({"ok": False, "status": "invalid_date", "message": "Choose a valid raw-water sample date."}, status=400)
        return JsonResponse(predict_dosage(plant, coagulant, inputs, sample_at=sample_at))
