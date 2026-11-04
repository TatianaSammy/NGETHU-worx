# PHadjustment/views_dashboard.py
from __future__ import annotations

import logging
from datetime import date as dt_date
from decimal import Decimal, InvalidOperation
from typing import Dict, Any, List, Optional

from django.contrib import messages
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, F, IntegerField, OuterRef, Q, Subquery, Value, When
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views import View

from core.mixins import WanjalaMixin
from t.models import TreatmentPlant, CoagulatedWaterLimit
from jartest.models import JarTest, JarResult
from PHadjustment.models import PHAdjustmentTest, PHAdjustmentJarResult
from PHadjustment.services.reports import build_report_payload, serialize_ph_test
from qc.models import RawWaterResult, SamplingSchedule, WaterAnalysis, CoagulatedWaterResult, TreatedWaterResult

logger = logging.getLogger(__name__)


class PHAdjustmentDashboardView(WanjalaMixin, View):
    """
    Advanced pH Adjustment & Chemical Neutralization Analytical Dashboard.
    Provides real-time 6-jar titration bench linked to Jar Test coagulation,
    dynamic titration / buffer curve plotting, universal indicator color shift,
    and plant alkaline/acid chemical feed rate scaling.
    """
    template_name = "PHadjustment/results/dashboard.html"
    partial_matrix_template = "PHadjustment/results/partials/_ph_matrix.html"
    partial_topbar_template = "PHadjustment/results/partials/_topbar.html"
    partial_curve_template = "PHadjustment/results/partials/_titration_curve.html"
    model = PHAdjustmentTest
    export_fields = ["Test date", "Treatment plant", "Analyst", "Established dosage (mL)", "Resulting pH", "Deviation from pH 7", "Source coagulant", "Source jar dose (mg/L)", "Source jar", "Source water", "Linked raw-water measurements", "Optimal jar", "Jar count", "Review stage", "Remarks"]
    export_file_name = "ph_adjustment_reports"
    export_field_map = {"Test date": "date", "Treatment plant": "plant", "Analyst": "analyst", "Established dosage (mL)": "dose", "Resulting pH": "ph", "Deviation from pH 7": "deviation", "Source coagulant": "source_coagulant", "Source jar dose (mg/L)": "source_dose", "Source jar": "source_jar_number", "Source water": "raw_source", "Linked raw-water measurements": "raw_summary", "Optimal jar": "optimal_jar", "Jar count": "jar_count", "Review stage": "stage", "Remarks": "remarks"}

    def get_queryset(self):
        request = self.request
        plant = self.resolve_plant(request)
        if not plant:
            return PHAdjustmentTest.objects.none()
        queryset = PHAdjustmentTest.objects.filter(treatment_plant=plant).annotate(
            _report_ph=Subquery(
                PHAdjustmentJarResult.objects.filter(test_id=OuterRef("pk"))
                .order_by("-is_optimal", "performance_rank", "jar_number")
                .values("resulting_ph")[:1]
            )
        )
        params = request.GET
        for key, lookup in (("report_from", "test_date__gte"), ("report_to", "test_date__lte")):
            if parsed := parse_date(params.get(key, "")):
                queryset = queryset.filter(**{lookup: parsed})
        for key, lookup in (("report_min_dose", "established_dosage_ml__gte"), ("report_max_dose", "established_dosage_ml__lte"), ("report_min_ph", "_report_ph__gte"), ("report_max_ph", "_report_ph__lte")):
            raw = params.get(key, "").strip()
            if raw:
                try:
                    value = Decimal(raw)
                    if value.is_finite() and value >= 0:
                        queryset = queryset.filter(**{lookup: value})
                except (InvalidOperation, ValueError):
                    pass
        coagulant_id = params.get("report_coagulant")
        if coagulant_id and coagulant_id.isdigit():
            queryset = queryset.filter(source_jar__jar_test__coagulant_id=int(coagulant_id))
        parameter_id = params.get("report_parameter")
        if parameter_id and parameter_id.isdigit() and CoagulatedWaterLimit.objects.filter(treatment_plant=plant, pk=int(parameter_id)).exists():
            queryset = queryset.filter(source_jar__jar_test__raw_water__analyses__parameter_id=int(parameter_id))
            for key, lookup in (("report_min_parameter", "source_jar__jar_test__raw_water__analyses__value__gte"), ("report_max_parameter", "source_jar__jar_test__raw_water__analyses__value__lte")):
                raw = params.get(key, "").strip()
                if raw:
                    try:
                        value = Decimal(raw)
                        if value.is_finite() and value >= 0:
                            queryset = queryset.filter(**{lookup: value})
                    except (InvalidOperation, ValueError):
                        pass
        stage = params.get("report_status", "").casefold()
        if stage == "in_progress": queryset = queryset.filter(verified_at__isnull=True, needs_review=False)
        elif stage == "verified": queryset = queryset.filter(verified_at__isnull=False, confirmed_at__isnull=True, needs_review=False)
        elif stage == "confirmed": queryset = queryset.filter(confirmed_at__isnull=False, approved_at__isnull=True, needs_review=False)
        elif stage == "approved": queryset = queryset.filter(approved_at__isnull=False, needs_review=False)
        elif stage == "needs_review": queryset = queryset.filter(needs_review=True)
        term = params.get("report_q", "").strip()
        if term:
            queryset = queryset.filter(Q(analyst__username__icontains=term) | Q(analyst__first_name__icontains=term) | Q(analyst__last_name__icontains=term) | Q(source_jar__jar_test__coagulant__name__icontains=term) | Q(source_jar__jar_test__raw_water__source_name__icontains=term) | Q(source_jar__jar_test__raw_water__analyses__parameter__parameter__name__icontains=term) | Q(remarks__icontains=term))
        ordering = {
            "oldest": ("test_date", "pk"),
            "dose_high": (F("established_dosage_ml").desc(nulls_last=True), "-test_date"),
            "dose_low": (F("established_dosage_ml").asc(nulls_last=True), "-test_date"),
            "ph_high": (F("_report_ph").desc(nulls_last=True), "-test_date"),
            "ph_low": (F("_report_ph").asc(nulls_last=True), "-test_date"),
            "coagulant": ("source_jar__jar_test__coagulant__name", "-test_date"),
            "analyst": ("analyst__username", "-test_date"),
            "status": (Case(When(needs_review=True, then=Value(0)), When(approved_at__isnull=False, then=Value(4)), When(confirmed_at__isnull=False, then=Value(3)), When(verified_at__isnull=False, then=Value(2)), default=Value(1), output_field=IntegerField()), "-test_date"),
        }.get(params.get("report_sort"), ("-test_date", "-pk"))
        return queryset.distinct().select_related("treatment_plant", "analyst", "source_jar", "source_jar__jar_test", "source_jar__jar_test__coagulant", "source_jar__jar_test__raw_water", "verified_by", "confirmed_by", "approved_by").prefetch_related("results", "source_jar__jar_test__raw_water__analyses__parameter__parameter").order_by(*ordering)

    def format_export_value(self, obj, field):
        row = getattr(obj, "_ph_report_export_row", None)
        if row is None:
            row = serialize_ph_test(obj)
            obj._ph_report_export_row = row
        value = row.get(self.export_field_map.get(field, field))
        return "" if value is None else str(value)

    def _reports_context(self, request):
        payload = build_report_payload(self.get_queryset(), request.GET.get("report_parameter"))
        page = Paginator(payload["rows"], 50).get_page(request.GET.get("report_page", 1))
        payload["rows"] = list(page.object_list)
        page_query = request.GET.copy()
        page_query.pop("report_page", None)
        return payload, page, page_query.urlencode()

    # =====================================================
    # RESOLUTION HELPERS
    # =====================================================
    def resolve_plant(self, request) -> Optional[TreatmentPlant]:
        user = request.user
        plant_id = request.GET.get("plant") or request.POST.get("plant")
        if user.is_superuser:
            plant_id = plant_id or request.session.get("qc_selected_plant")
            if plant_id:
                try:
                    plant = TreatmentPlant.objects.filter(pk=int(plant_id)).first()
                    if plant:
                        request.session["qc_selected_plant"] = plant.pk
                        return plant
                except (ValueError, TypeError):
                    pass
            plant = TreatmentPlant.objects.order_by("name").first()
            if plant:
                request.session["qc_selected_plant"] = plant.pk
            return plant
        return getattr(user, "assigned_treatment_plant", None)

    def resolve_date(self, request) -> dt_date:
        date_str = request.GET.get("date") or request.POST.get("date")
        if date_str:
            try:
                return timezone.datetime.strptime(date_str, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                pass
        return timezone.localdate()

    def get_available_plants(self, request):
        if request.user.is_superuser:
            return TreatmentPlant.objects.order_by("name")
        plant = self.resolve_plant(request)
        return TreatmentPlant.objects.filter(pk=plant.pk) if plant else TreatmentPlant.objects.none()

    def resolve_source_jar(self, plant: TreatmentPlant, test_date: dt_date) -> Optional[JarResult]:
        """Choose a source jar backed by raw water from the selected plant."""
        today_jartest = JarTest.objects.filter(
            treatment_plant=plant,
            raw_water__treatment_plant=plant,
            test_date=test_date,
        ).first()
        if today_jartest:
            opt_jar = today_jartest.jar_results.filter(is_optimal=True).first()
            if opt_jar:
                return opt_jar
            first_jar = today_jartest.jar_results.first()
            if first_jar:
                return first_jar

        return (
            JarResult.objects.filter(
                jar_test__treatment_plant=plant,
                jar_test__raw_water__treatment_plant=plant,
                jar_test__test_date__lte=test_date,
            )
            .select_related("jar_test", "jar_test__raw_water")
            .order_by("-jar_test__test_date", "performance_rank", "jar_number")
            .first()
        )

    # =====================================================
    # ORM INITIALIZATION
    # =====================================================
    def get_or_create_ph_test(self, plant: TreatmentPlant, test_date: dt_date, user) -> Optional[PHAdjustmentTest]:
        test = PHAdjustmentTest.objects.filter(
            treatment_plant=plant,
            test_date=test_date,
        ).select_related("source_jar__jar_test__raw_water").first()
        if not test:
            source_jar = self.resolve_source_jar(plant, test_date)
            if not source_jar:
                return None
            test = PHAdjustmentTest.objects.create(
                treatment_plant=plant,
                source_jar=source_jar,
                test_date=test_date,
                analyst=user if user and user.is_authenticated else None,
                remarks="Coagulated water pH stabilization test targeting pH 7.00.",
            )
        elif (
            test.source_jar.jar_test.treatment_plant_id != plant.pk
            or test.source_jar.jar_test.raw_water.treatment_plant_id != plant.pk
        ):
            if test.verified_at or test.confirmed_at or test.approved_at:
                logger.error("pH test source plant mismatch test=%s selected_plant=%s", test.pk, plant.pk)
                return None
            source_jar = self.resolve_source_jar(plant, test_date)
            if not source_jar:
                return None
            test.source_jar = source_jar
            test.save(update_fields=["source_jar"])

        # Initialize jars if empty
        if not test.results.exists():
            base_dose = test.source_jar.dosage_mg_per_l if test.source_jar else Decimal("18.00")
            step = max(Decimal("0.5"), base_dose * Decimal("0.05"))
            dosages = [
                max(Decimal("0.5"), base_dose - 2 * step),
                max(Decimal("0.5"), base_dose - step),
                base_dose,
                base_dose + step,
                base_dose + 2 * step,
                base_dose + 3 * step,
            ]
            default_ph = [Decimal("6.40"), Decimal("6.70"), Decimal("7.00"), Decimal("7.25"), Decimal("7.50"), Decimal("7.80")]
            jars_to_create = []
            for idx, dose in enumerate(dosages, start=1):
                jars_to_create.append(PHAdjustmentJarResult(
                    test=test,
                    jar_number=idx,
                    dosage_ml=dose,
                    resulting_ph=default_ph[idx - 1],
                    performance_rank=idx,
                    is_optimal=(idx == 3),
                ))
            PHAdjustmentJarResult.objects.bulk_create(jars_to_create)
            test.rank_results()

        return test

    # =====================================================
    # QC TELEMETRY INTEGRATION
    # =====================================================
    def get_qc_ph_telemetry(self, plant: TreatmentPlant, test_date: dt_date):
        telemetry = {
            "raw_ph": "7.20",
            "coagulated_ph": "6.40",
            "treated_ph": "7.10",
            "water_temp": "21.5 °C",
            "alkalinity": "-- mg/L",
            "is_live": False,
            "source": "QC Water Quality Bench",
        }

        if not plant:
            return telemetry

        try:
            # Use the latest raw-water sample for this plant, bounded by the
            # selected test date so later samples cannot leak into old tests.
            raw_analysis = (
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
            raw_res = list(
                RawWaterResult.objects.filter(analysis=raw_analysis)
                .select_related("parameter__parameter", "analysis")
                .order_by("-id")
            ) if raw_analysis else []
            if raw_analysis:
                telemetry["source"] = f"QC {raw_analysis.reference_id} · {raw_analysis.analysis_date}"
            for r in raw_res:
                pname = r.parameter.parameter.name.lower()
                if "ph" in pname and "alpha" not in pname and r.value is not None:
                    telemetry["raw_ph"] = f"{r.value:.2f}"
                    telemetry["is_live"] = True
                elif "temp" in pname and r.value is not None:
                    telemetry["water_temp"] = f"{r.value:.1f} °C"
                elif "alkal" in pname and r.value is not None:
                    telemetry["alkalinity"] = f"{r.value:.1f} mg/L"

            # Query coagulated water pH (post-coagulation) from QC
            coag_res = (
                CoagulatedWaterResult.objects
                .filter(
                    analysis__treatment_plant=plant,
                    analysis__analysis_date__lte=test_date,
                    analysis__analysis_time__schedule__treatment_plant=plant,
                    analysis__analysis_time__schedule__water_type=SamplingSchedule.COAGULATED,
                )
                .select_related("parameter__parameter")
                .order_by("-analysis__analysis_date", "-analysis__analysis_time__time", "-pk")
            )
            for r in coag_res[:10]:
                pname = r.parameter.parameter.name.lower()
                if "ph" in pname and "alpha" not in pname and r.value is not None:
                    telemetry["coagulated_ph"] = f"{r.value:.2f}"
                    telemetry["is_live"] = True
                    break

            # Query Treated Water Result for finished water pH
            treated_res = (
                TreatedWaterResult.objects
                .filter(
                    analysis__treatment_plant=plant,
                    analysis__analysis_date__lte=test_date,
                    analysis__analysis_time__schedule__treatment_plant=plant,
                    analysis__analysis_time__schedule__water_type=SamplingSchedule.TREATED,
                )
                .select_related("parameter__parameter")
                .order_by("-analysis__analysis_date", "-analysis__analysis_time__time", "-pk")
            )
            for r in treated_res[:10]:
                pname = r.parameter.parameter.name.lower()
                if "ph" in pname and "alpha" not in pname and r.value is not None:
                    telemetry["treated_ph"] = f"{r.value:.2f}"
        except Exception as e:
            logger.debug(f"QC pH telemetry fetch notice: {e}")

        return telemetry

    # =====================================================
    # CONTEXT BUILDER
    # =====================================================
    def build_context(self, request) -> Dict[str, Any]:
        plant = self.resolve_plant(request)
        test_date = self.resolve_date(request)
        report_view = request.method == "GET" and request.GET.get("tab") == "reports"
        ph_test = self.get_or_create_ph_test(plant, test_date, request.user) if plant and not report_view else None
        telemetry = self.get_qc_ph_telemetry(plant, test_date)

        target_ph = Decimal("7.00")
        jars = []
        chart_points = []
        optimal_jar = None

        if ph_test:
            all_jars = list(ph_test.results.all().order_by("jar_number"))
            for jar in all_jars:
                dose = jar.dosage_ml or Decimal("0.00")
                res_ph = jar.resulting_ph or Decimal("7.00")
                delta_ph = res_ph - target_ph
                abs_delta = abs(delta_ph)

                # Determine Universal Indicator color & CSS class
                # pH < 5: red; 5-6: orange/yellow; 6.5-7.5: green (neutral); 7.6-8.5: cyan/teal; >8.5: violet
                if res_ph < Decimal("5.5"):
                    ph_color = "#ef4444"  # Red
                    ph_status = "Acidic"
                    status_class = "acidic"
                elif res_ph < Decimal("6.5"):
                    ph_color = "#f59e0b"  # Amber/Yellow
                    ph_status = "Slightly Acidic"
                    status_class = "sub-neutral"
                elif res_ph <= Decimal("7.5"):
                    ph_color = "#10b981"  # Emerald
                    ph_status = "Optimal Neutral"
                    status_class = "optimal"
                elif res_ph <= Decimal("8.5"):
                    ph_color = "#06b6d4"  # Cyan/Teal
                    ph_status = "Mildly Alkaline"
                    status_class = "alkaline"
                else:
                    ph_color = "#a855f7"  # Purple/Violet
                    ph_status = "High Alkaline"
                    status_class = "over-alkaline"

                jar_data = {
                    "obj": jar,
                    "jar_number": jar.jar_number,
                    "dosage_ml": dose,
                    "resulting_ph": res_ph,
                    "delta_ph": round(delta_ph, 2),
                    "abs_delta": round(abs_delta, 2),
                    "ph_color": ph_color,
                    "ph_status": ph_status,
                    "status_class": status_class,
                    "is_optimal": jar.is_optimal,
                    "rank": jar.performance_rank or jar.jar_number,
                }
                jars.append(jar_data)

                if jar.is_optimal:
                    optimal_jar = jar_data

                chart_points.append({
                    "jar": jar.jar_number,
                    "dosage": float(dose),
                    "ph": float(res_ph),
                    "delta": float(delta_ph),
                    "color": ph_color,
                    "is_optimal": jar.is_optimal,
                    "rank": jar.performance_rank or jar.jar_number,
                })

        # Available Jar Test source jars for selection
        available_source_jars = list(
            JarResult.objects
            .filter(
                jar_test__treatment_plant=plant,
                jar_test__raw_water__treatment_plant=plant,
                jar_test__test_date__lte=test_date,
            )
            .select_related("jar_test")
            .order_by("-jar_test__test_date", "performance_rank")[:10]
        ) if plant else []

        # Available pH Adjuster chemicals for plant conditioning feed
        from ca.models import Chemical
        available_ph_adjusters = list(
            Chemical.objects.filter(
                Q(categories__category="PH Adjuster")
                | Q(name__icontains="lime")
                | Q(name__icontains="soda")
                | Q(name__icontains="caustic")
                | Q(name__icontains="acid")
                | Q(code__icontains="LIME")
                | Q(code__icontains="SODA")
                | Q(code__icontains="NAOH")
            ).distinct().order_by("name")
        )

        return {
            "plant": plant,
            "test_date": test_date,
            "ph_test": ph_test,
            "jars": jars,
            "optimal_jar": optimal_jar,
            "target_ph": target_ph,
            "chart_points": chart_points,
            "telemetry": telemetry,
            "available_plants": self.get_available_plants(request),
            "available_source_jars": available_source_jars,
            "available_ph_adjusters": available_ph_adjusters,
            "current_user": request.user,
            "show_ph_workspace_tabs": bool(plant),
        }

    # =====================================================
    # HTTP HANDLERS
    # =====================================================
    def get(self, request, *args, **kwargs):
        self.check_idle_timeout()
        context = self.build_context(request)

        partial = request.GET.get("partial")
        if partial == "matrix":
            return render(request, self.partial_matrix_template, context)
        if partial == "topbar":
            return render(request, self.partial_topbar_template, context)
        if partial == "curve":
            return render(request, self.partial_curve_template, context)

        if context["plant"] and request.GET.get("tab") == "reports":
            has_report_filters = any(k.startswith("report_") for k in request.GET)
            cache_key = f"ph_reports_{context['plant'].pk}"
            if not has_report_filters:
                cached = cache.get(cache_key)
                if cached:
                    reports, page, page_query = cached
                else:
                    reports, page, page_query = self._reports_context(request)
                    cache.set(cache_key, (reports, page, page_query), 180)
            else:
                reports, page, page_query = self._reports_context(request)

            from ca.models import Chemical
            coagulant_ids = JarTest.objects.filter(treatment_plant=context["plant"]).values_list("coagulant_id", flat=True).distinct()
            report_coagulants = Chemical.objects.filter(pk__in=coagulant_ids).order_by("name")
            report_parameters = CoagulatedWaterLimit.objects.filter(treatment_plant=context["plant"]).select_related("parameter").order_by("parameter__name")
            context.update({"ph_reports": reports, "report_page_obj": page, "report_page_query": page_query, "report_coagulants": report_coagulants, "report_parameters": report_parameters})

        return render(request, self.template_name, context)

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        self.check_idle_timeout()
        plant = self.resolve_plant(request)
        if not plant:
            return render(request, self.template_name, self.build_context(request))
        test_date = self.resolve_date(request)
        ph_test = self.get_or_create_ph_test(plant, test_date, request.user)

        action = request.POST.get("action", "save_jars")

        if action == "save_jars" and ph_test:
            # Update remarks, source_jar, analyst (PHAdjustmentTest fields)
            remarks = request.POST.get("remarks")
            if remarks is not None:
                ph_test.remarks = remarks

            source_jar_id = request.POST.get("source_jar")
            if source_jar_id:
                try:
                    sj = JarResult.objects.filter(
                        pk=int(source_jar_id),
                        jar_test__treatment_plant=plant,
                        jar_test__raw_water__treatment_plant=plant,
                    ).first()
                    if sj:
                        ph_test.source_jar = sj
                except (ValueError, TypeError):
                    pass
            if request.user.is_authenticated:
                ph_test.analyst = request.user
            ph_test.save()

            # Update jars → PHAdjustmentJarResult.dosage_ml / resulting_ph
            for jar in ph_test.results.all():
                prefix = f"jar_{jar.jar_number}_"
                dosage_raw = request.POST.get(f"{prefix}dosage")
                ph_raw = request.POST.get(f"{prefix}resulting_ph")

                if dosage_raw is not None and str(dosage_raw).strip() != "":
                    try:
                        jar.dosage_ml = Decimal(str(dosage_raw).strip())
                    except (InvalidOperation, ValueError):
                        pass

                if ph_raw is not None and str(ph_raw).strip() != "":
                    try:
                        jar.resulting_ph = Decimal(str(ph_raw).strip())
                    except (InvalidOperation, ValueError):
                        pass

                jar.save()

            # Rank jars and establish optimal dosage
            ph_test.rank_results()
            messages.success(request, "pH titration observations saved, neutralization curve plotted, and optimal dose established.")

        elif action == "init_ladder" and ph_test:
            # Re-generate dosage ladder around source jar
            base_str = request.POST.get("base_dosage")
            if base_str:
                try:
                    base = Decimal(base_str)
                except (InvalidOperation, ValueError):
                    base = ph_test.source_jar.dosage_mg_per_l if ph_test.source_jar else Decimal("18.00")
            else:
                base = ph_test.source_jar.dosage_mg_per_l if ph_test.source_jar else Decimal("18.00")

            step = max(Decimal("0.5"), base * Decimal("0.05"))
            dosages = [
                max(Decimal("0.5"), base - 2 * step),
                max(Decimal("0.5"), base - step),
                base,
                base + step,
                base + 2 * step,
                base + 3 * step,
            ]

            for idx, jar in enumerate(ph_test.results.all().order_by("jar_number"), start=0):
                if idx < len(dosages):
                    jar.dosage_ml = dosages[idx]
                    jar.save(update_fields=["dosage_ml"])

            messages.info(request, f"Re-seeded pH titration ladder around base dose {base:.2f} mL.")

        elif action == "verify" and ph_test:
            try:
                ph_test.verify(request.user)
                ph_test.save()
                messages.success(request, f"pH adjustment test verified by {request.user.username}.")
            except Exception as e:
                messages.error(request, str(e))

        elif action == "confirm" and ph_test:
            try:
                ph_test.confirm(request.user)
                ph_test.save()
                messages.success(request, f"pH adjustment test confirmed by {request.user.username}.")
            except Exception as e:
                messages.error(request, str(e))

        elif action == "approve" and ph_test:
            try:
                ph_test.approve(request.user)
                ph_test.save()
                messages.success(request, f"pH adjustment dosage officially authorized by {request.user.username}.")
            except Exception as e:
                messages.error(request, str(e))

        elif action == "revert_verification" and ph_test:
            try:
                ph_test.revert_verification()
                ph_test.save()
                messages.info(request, "Verification reverted.")
            except Exception as e:
                messages.error(request, str(e))

        context = self.build_context(request)

        if request.headers.get("HX-Request"):
            return render(request, self.partial_matrix_template, context)

        return render(request, self.template_name, context)
