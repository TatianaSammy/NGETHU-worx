# chlorinedemand/views_dashboard.py
from __future__ import annotations

import logging
import re
from datetime import timedelta, date as dt_date
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
from t.models import TreatedWaterLimit, TreatmentPlant
from ca.models import Chemical
from o.models import ChemicalCategory
from chlorinedemand.models import ChlorineDemandTest, ChlorineJarResult
from chlorinedemand.services.reports import build_report_payload, serialize_chlorine_test
from qc.models import RawWaterResult, SamplingSchedule, WaterAnalysis, TreatedWaterResult

logger = logging.getLogger(__name__)

_FRC_PARAMETER_NAMES = (
    "free residual chlorine",
    "free chlorine residual",
    "residual free chlorine",
    "free available chlorine",
)


def is_free_residual_chlorine_parameter(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", (name or "").casefold()).strip()
    compact = normalized.replace(" ", "")
    return (
        compact == "frc"
        or any(alias in normalized for alias in _FRC_PARAMETER_NAMES)
        or bool(re.search(r"\bf[\W_]*r[\W_]*c\b", (name or "").casefold()))
    )


class ChlorineDemandDashboardView(WanjalaMixin, View):
    """
    Advanced Breakpoint Chlorination & Chlorine Demand Analytical Dashboard.
    Provides real-time 6-jar testing, breakpoint curve visualization,
    contact-time incubation stopwatch, and plant chemical dosage scaling.
    """
    template_name = "chlorinedemand/results/dashboard.html"
    partial_matrix_template = "chlorinedemand/results/partials/_chlorine_matrix.html"
    partial_topbar_template = "chlorinedemand/results/partials/_topbar.html"
    partial_curve_template = "chlorinedemand/results/partials/_breakpoint_curve.html"
    model = ChlorineDemandTest
    export_fields = ["Test date", "Treatment plant", "Analyst", "Established dose (mg/L)", "Optimal residual (mg/L)", "Consumed chlorine (mg/L)", "Optimal jar", "Jar count", "Review stage", "Remarks"]
    export_file_name = "chlorine_demand_reports"
    export_field_map = {"Test date": "date", "Treatment plant": "plant", "Analyst": "analyst", "Established dose (mg/L)": "dose", "Optimal residual (mg/L)": "residual", "Consumed chlorine (mg/L)": "consumed", "Optimal jar": "optimal_jar", "Jar count": "jar_count", "Review stage": "stage", "Remarks": "remarks"}

    def get_queryset(self):
        request = self.request
        plant = self.resolve_plant(request)
        if not plant:
            return ChlorineDemandTest.objects.none()
        queryset = ChlorineDemandTest.objects.filter(treatment_plant=plant).annotate(
            _report_residual=Subquery(
                ChlorineJarResult.objects.filter(test_id=OuterRef("pk"))
                .order_by("-is_optimal", "performance_rank", "jar_number")
                .values("residual_chlorine")[:1]
            )
        )
        params = request.GET
        for key, lookup in (("report_from", "test_date__gte"), ("report_to", "test_date__lte")):
            if parsed := parse_date(params.get(key, "")):
                queryset = queryset.filter(**{lookup: parsed})
        for key, lookup in (("report_min_dose", "established_dosage_mg_per_l__gte"), ("report_max_dose", "established_dosage_mg_per_l__lte"), ("report_min_residual", "_report_residual__gte"), ("report_max_residual", "_report_residual__lte")):
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
            queryset = queryset.filter(Q(analyst__username__icontains=term) | Q(analyst__first_name__icontains=term) | Q(analyst__last_name__icontains=term) | Q(remarks__icontains=term))
        ordering = {
            "oldest": ("test_date", "pk"),
            "dose_high": (F("established_dosage_mg_per_l").desc(nulls_last=True), "-test_date"),
            "dose_low": (F("established_dosage_mg_per_l").asc(nulls_last=True), "-test_date"),
            "residual_high": (F("_report_residual").desc(nulls_last=True), "-test_date"),
            "residual_low": (F("_report_residual").asc(nulls_last=True), "-test_date"),
            "analyst": ("analyst__username", "-test_date"),
            "status": (Case(When(needs_review=True, then=Value(0)), When(approved_at__isnull=False, then=Value(4)), When(confirmed_at__isnull=False, then=Value(3)), When(verified_at__isnull=False, then=Value(2)), default=Value(1), output_field=IntegerField()), "-test_date"),
        }.get(params.get("report_sort"), ("-test_date", "-pk"))
        return queryset.distinct().select_related("treatment_plant", "analyst", "verified_by", "confirmed_by", "approved_by").prefetch_related("jar_results").order_by(*ordering)

    def format_export_value(self, obj, field):
        row = getattr(obj, "_cd_report_export_row", None)
        if row is None:
            row = serialize_chlorine_test(obj)
            obj._cd_report_export_row = row
        value = row.get(self.export_field_map.get(field, field))
        return "" if value is None else str(value)

    def _reports_context(self, request):
        payload = build_report_payload(self.get_queryset())
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

    # =====================================================
    # ORM INITIALIZATION
    # =====================================================
    def get_or_create_chlorine_test(self, plant: TreatmentPlant, test_date: dt_date, user) -> ChlorineDemandTest:
        test, created = ChlorineDemandTest.objects.get_or_create(
            treatment_plant=plant,
            test_date=test_date,
            defaults={
                "analyst": user if user and user.is_authenticated else None,
                "remarks": "Standard 10-minute breakpoint chlorination demand test.",
            }
        )

        # Initialize exactly 6 Jars if missing
        existing_jars = {j.jar_number: j for j in test.jar_results.all()}
        default_dosages = [
            Decimal("1.00"),
            Decimal("1.50"),
            Decimal("2.00"),
            Decimal("2.50"),
            Decimal("3.00"),
            Decimal("3.50"),
        ]

        jars_to_create = []
        for i in range(1, 7):
            if i not in existing_jars:
                jars_to_create.append(ChlorineJarResult(
                    test=test,
                    jar_number=i,
                    dosage_mg_per_l=default_dosages[i - 1],
                    residual_chlorine=Decimal("0.000"),
                    performance_rank=i,
                    is_optimal=(i == 3),
                ))

        if jars_to_create:
            ChlorineJarResult.objects.bulk_create(jars_to_create)

        return test

    # =====================================================
    # QC TELEMETRY INTEGRATION
    # =====================================================
    def get_qc_telemetry(self, plant: TreatmentPlant, test_date: dt_date):
        """
        Extracts live QC telemetry relevant to chlorination (ammonia, pH, turbidity,
        water temperature, and latest treated residual chlorine).
        """
        telemetry = {
            "turbidity": "--",
            "ph": "7.20",
            "temp": "22.0 °C",
            "ammonia": "--",
            "treated_residual": "--",
            "is_live": False,
            "source": "QC Baseline",
            "ticker_items": [],
        }

        if not plant:
            return telemetry

        try:
            # Query Raw Water parameters
            latest_raw_analysis = (
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
            qc_raw = list(
                RawWaterResult.objects.filter(analysis=latest_raw_analysis)
                .select_related("parameter__parameter", "analysis")
                .order_by("-id")
            ) if latest_raw_analysis else []

            if qc_raw:
                telemetry["is_live"] = True
                telemetry["source"] = f"QC #{getattr(qc_raw[0].analysis, 'reference_id', qc_raw[0].analysis_id)}"
                for r in qc_raw:
                    pname = r.parameter.parameter.name.lower()
                    punit = r.parameter.parameter.unit or ""
                    val = r.value
                    if val is not None:
                        if "turb" in pname:
                            telemetry["turbidity"] = f"{val:.2f} NTU"
                        elif "ph" in pname and "alpha" not in pname:
                            telemetry["ph"] = f"{val:.2f}"
                        elif "temp" in pname:
                            telemetry["temp"] = f"{val:.1f} °C"
                        elif "ammon" in pname or "nh" in pname:
                            telemetry["ammonia"] = f"{val:.3f} mg/L"

                        telemetry["ticker_items"].append({
                            "name": r.parameter.parameter.name,
                            "val": f"{val:.2f} {punit}",
                        })

            # Check treated water residual chlorine from QC
            treated_res = (
                TreatedWaterResult.objects
                .filter(analysis__treatment_plant=plant)
                .filter(
                    analysis__analysis_date__lte=test_date,
                    analysis__analysis_time__schedule__treatment_plant=plant,
                    analysis__analysis_time__schedule__water_type=SamplingSchedule.TREATED,
                )
                .filter(
                    Q(parameter__parameter__name__icontains="free residual chlorine")
                    | Q(parameter__parameter__name__icontains="free chlorine residual")
                    | Q(parameter__parameter__name__icontains="residual free chlorine")
                    | Q(parameter__parameter__name__icontains="free available chlorine")
                    | Q(parameter__parameter__name__iexact="FRC")
                    | Q(parameter__parameter__name__iexact="F.R.C")
                    | Q(parameter__parameter__name__iexact="F.R.C.")
                    | Q(parameter__parameter__name__icontains="F.R.C")
                )
                .select_related("parameter__parameter")
                .order_by("-analysis__analysis_date", "-analysis__analysis_time__time", "-pk")
                .first()
            )
            if treated_res and treated_res.value is not None:
                telemetry["treated_residual"] = f"{treated_res.value:.2f} mg/L"

        except Exception as e:
            logger.debug(f"Chlorine telemetry fetch note: {e}")

        return telemetry

    @staticmethod
    def get_plant_frc_limits(
        plant: Optional[TreatmentPlant],
    ) -> Optional[tuple[Decimal, Decimal]]:
        """Return this plant's treated-water free-chlorine lower and upper limits."""
        if not plant:
            return None

        limits = (
            TreatedWaterLimit.objects
            .filter(treatment_plant=plant)
            .exclude(min_value__isnull=True)
            .exclude(max_value__isnull=True)
            .select_related("parameter")
            .only("min_value", "max_value", "parameter__name")
        )
        for limit in limits:
            if not is_free_residual_chlorine_parameter(limit.parameter.name):
                continue
            lower = Decimal(limit.min_value)
            upper = Decimal(limit.max_value)
            if lower < 0 or upper < lower:
                logger.warning(
                    "Invalid treated-water FRC limits plant=%s min=%s max=%s",
                    plant.pk,
                    lower,
                    upper,
                )
                return None
            return lower, upper
        return None

    # =====================================================
    # CONTEXT BUILDER
    # =====================================================
    def build_context(self, request) -> Dict[str, Any]:
        plant = self.resolve_plant(request)
        test_date = self.resolve_date(request)
        report_view = request.method == "GET" and request.GET.get("tab") == "reports"
        chlorine_test = self.get_or_create_chlorine_test(plant, test_date, request.user) if plant and not report_view else None
        telemetry = self.get_qc_telemetry(plant, test_date)

        frc_limits = self.get_plant_frc_limits(plant)
        target_residual = (
            (frc_limits[0] + frc_limits[1]) / Decimal("2")
            if frc_limits is not None
            else None
        )
        jars = []
        chart_points = []
        optimal_jar = None

        if chlorine_test:
            all_jars = list(chlorine_test.jar_results.all().order_by("jar_number"))
            for jar in all_jars:
                dose = jar.dosage_mg_per_l or Decimal("0.00")
                residual = jar.residual_chlorine or Decimal("0.000")
                consumed = max(Decimal("0.00"), dose - residual)
                retention_pct = (residual / dose * Decimal("100")) if dose > Decimal("0") else Decimal("0")

                # Diagnostic status
                diff = abs(residual - target_residual) if target_residual is not None else None
                if target_residual is None:
                    status_class = "unconfigured"
                    status_label = "Treated F.R.C. Limit Missing"
                elif diff <= Decimal("0.05"):
                    status_class = "optimal"
                    status_label = "Optimal Target"
                elif residual < target_residual:
                    status_class = "under"
                    status_label = "Sub-Breakpoint"
                else:
                    status_class = "over"
                    status_label = "Excess Residual"

                gauge_range = max(
                    target_residual * Decimal("2") if target_residual is not None else Decimal("0"),
                    Decimal("0.4"),
                )
                gauge_pct = min(100, max(0, int((residual / gauge_range) * 100)))

                jar_data = {
                    "obj": jar,
                    "jar_number": jar.jar_number,
                    "dosage": dose,
                    "residual": residual,
                    "consumed": consumed,
                    "retention_pct": round(retention_pct, 1),
                    "is_optimal": jar.is_optimal,
                    "rank": jar.performance_rank,
                    "status_class": status_class,
                    "status_label": status_label,
                    "target_deviation": diff,
                    "gauge_pct": gauge_pct,
                }
                jars.append(jar_data)

                if jar.is_optimal:
                    optimal_jar = jar_data

                chart_points.append({
                    "jar": jar.jar_number,
                    "dosage": float(dose),
                    "residual": float(residual),
                    "consumed": float(consumed),
                    "is_optimal": jar.is_optimal,
                    "rank": jar.performance_rank or jar.jar_number,
                })

        return {
            "plant": plant,
            "test_date": test_date,
            "chlorine_test": chlorine_test,
            "jars": jars,
            "optimal_jar": optimal_jar,
            "target_residual": target_residual,
            "frc_limits": frc_limits,
            "chart_points": chart_points,
            "telemetry": telemetry,
            "available_plants": self.get_available_plants(request),
            "current_user": request.user,
            "show_chlorine_workspace_tabs": bool(plant),
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
            cache_key = f"cd_reports_{context['plant'].pk}"
            if not has_report_filters:
                cached = cache.get(cache_key)
                if cached:
                    reports, page, page_query = cached
                else:
                    reports, page, page_query = self._reports_context(request)
                    cache.set(cache_key, (reports, page, page_query), 180)
            else:
                reports, page, page_query = self._reports_context(request)
            context.update({"chlorine_reports": reports, "report_page_obj": page, "report_page_query": page_query})

        return render(request, self.template_name, context)

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        self.check_idle_timeout()
        plant = self.resolve_plant(request)
        if not plant:
            return render(request, self.template_name, self.build_context(request))
        test_date = self.resolve_date(request)
        chlorine_test = self.get_or_create_chlorine_test(plant, test_date, request.user)

        action = request.POST.get("action", "save_jars")

        if action == "save_jars" and chlorine_test:
            # Update remarks + ensure analyst is stamped (ChlorineDemandTest.analyst)
            remarks = request.POST.get("remarks")
            if remarks is not None:
                chlorine_test.remarks = remarks
            if request.user.is_authenticated:
                chlorine_test.analyst = request.user

            # Update jars → ChlorineJarResult.dosage_mg_per_l / residual_chlorine
            for jar in chlorine_test.jar_results.all():
                prefix = f"jar_{jar.jar_number}_"
                dosage_raw = request.POST.get(f"{prefix}dosage")
                residual_raw = request.POST.get(f"{prefix}residual")

                if dosage_raw is not None and str(dosage_raw).strip() != "":
                    try:
                        jar.dosage_mg_per_l = Decimal(str(dosage_raw).strip())
                    except (InvalidOperation, ValueError):
                        pass

                if residual_raw is not None and str(residual_raw).strip() != "":
                    try:
                        jar.residual_chlorine = Decimal(str(residual_raw).strip())
                    except (InvalidOperation, ValueError):
                        pass

                jar.save()

            # Rank jars and determine optimal breakpoint dosage
            chlorine_test.rank_results()
            messages.success(request, "Chlorine demand observations saved, breakpoint curve re-plotted, and optimal dose established.")

        elif action == "seed_ladder" and chlorine_test:
            # Seed stepped dosage ladder around base (e.g. 1.0 to 3.5 mg/L)
            base_str = request.POST.get("base_dosage", "1.0")
            step_str = request.POST.get("step_dosage", "0.5")
            try:
                base = Decimal(base_str)
                step = Decimal(step_str)
            except (InvalidOperation, ValueError):
                base = Decimal("1.0")
                step = Decimal("0.5")

            for idx, jar in enumerate(chlorine_test.jar_results.all().order_by("jar_number"), start=0):
                jar.dosage_mg_per_l = max(Decimal("0.1"), base + (step * Decimal(idx)))
                jar.save(update_fields=["dosage_mg_per_l"])

            messages.info(request, f"Applied chemical dosage ladder: {base} mg/L in steps of +{step} mg/L.")

        elif action == "verify" and chlorine_test:
            try:
                chlorine_test.verify(request.user)
                chlorine_test.save()
                messages.success(request, f"Chlorine demand test verified by {request.user.username}.")
            except Exception as e:
                messages.error(request, str(e))

        elif action == "confirm" and chlorine_test:
            try:
                chlorine_test.confirm(request.user)
                chlorine_test.save()
                messages.success(request, f"Chlorine demand test confirmed by {request.user.username}.")
            except Exception as e:
                messages.error(request, str(e))

        elif action == "approve" and chlorine_test:
            try:
                chlorine_test.approve(request.user)
                chlorine_test.save()
                messages.success(request, f"Chlorine dosage officially authorized by {request.user.username}.")
            except Exception as e:
                messages.error(request, str(e))

        elif action == "revert_verification" and chlorine_test:
            try:
                chlorine_test.revert_verification()
                chlorine_test.save()
                messages.info(request, "Verification reverted.")
            except Exception as e:
                messages.error(request, str(e))

        context = self.build_context(request)

        if request.headers.get("HX-Request"):
            return render(request, self.partial_matrix_template, context)

        return render(request, self.template_name, context)
