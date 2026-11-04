from __future__ import annotations
import io
import json
import math
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from django.apps import apps
from django.core.exceptions import PermissionDenied
from .mixins import CaWanjalaMixin
from .navigation import CA_DASHBOARD_DATA_PERMISSIONS
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.generic import TemplateView
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.shared import Inches, Pt
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from scipy import stats
from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import StandardScaler
from statsmodels.tsa.holtwinters import ExponentialSmoothing


class Dashboard2View(CaWanjalaMixin, TemplateView):
    template_name = "ca/dashboard/pages/dashboard2.html"
    permission_required_view = "ca.view_chemicalanalysisprocess"

    PROCESS_STATUS = (
        "CREATED",
        "RECEIVED",
        "REQUESTED",
        "CHECKLIST",
        "ASSIGNED",
        "SYSTEMATIC_RANDOM_SAMPLING",
        "ANALYSIS",
        "RETEST",
        "QA",
        "APPROVED",
        "REJECTED",
        "UTILIZED",
        "RETURNED",
        "COMPLETED",
    )

    SAMPLE_STATUS = (
        "PENDING",
        "SAMPLED",
        "COUNTER_CHECK",
        "QA_REVIEW",
        "COMPLETED",
    )

    ACTIVE_STATUSES = {
        "CREATED",
        "RECEIVED",
        "REQUESTED",
        "CHECKLIST",
        "ASSIGNED",
        "SYSTEMATIC_RANDOM_SAMPLING",
        "ANALYSIS",
        "RETEST",
        "QA",
    }

    TERMINAL_STATUSES = {
        "COMPLETED",
        "APPROVED",
        "REJECTED",
    }

    def _utc_now_timestamp(self):
        now = pd.Timestamp(self.now)
        return now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")

    def dispatch(self, request, *args, **kwargs):
        self.request = request
        self.now = timezone.now()
        self.user = request.user
        self.params = self._read_filters()
        self.models = self._resolve_models()
        self.scope = self._build_scope()
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        if not self.is_ca_superuser(request.user) and not all(
            request.user.has_perm(permission)
            for permission in CA_DASHBOARD_DATA_PERMISSIONS
        ):
            raise PermissionDenied(
                "You do not have permission to view the full CA operations dashboard."
            )
        self.data = self._load_data()
        self.analysis = self._build_analysis()
        return super().dispatch(request, *args, **kwargs)

    def _resolve_models(self):
        names = (
            "Chemical",
            "Supplier",
            "ChemicalBatch",
            "ChemicalAnalysisRequest",
            "ChemicalAnalysisProcess",
            "SystematicRandomSampling",
            "SystematicRandomSample",
            "BatchTestResult",
            "ChemicalAnalysisInput",
            "ChemicalCoA",
        )

        return {
            name: self._get_model(name)
            for name in names
        }

    def _get_model(self, name):
        try:
            return apps.get_model("ca", name)
        except LookupError:
            return None

    def _read_filters(self):
        params = self.request.GET

        try:
            days = int(params.get("days", 30))
        except (TypeError, ValueError):
            days = 30

        days = max(1, min(days, 3650))

        return {
            "days": days,
            "chemical": params.get("chemical") or "",
            "analyst": params.get("analyst") or "",
            "supplier": params.get("supplier") or "",
            "status": params.get("status") or "",
            "group": params.get("group", "day"),
        }

    def _build_scope(self):
        plant = self.get_selected_plant()
        is_superuser = self.is_ca_superuser()

        return {
            "plant": plant,
            "is_superuser": is_superuser and plant is None,
        }

    def _field_exists(self, model, field):
        if model is None:
            return False

        try:
            return field in {
                field.name
                for field in model._meta.get_fields()
            }
        except Exception:
            return False

    def _scope_queryset(self, qs, candidates):
        if self.scope["is_superuser"]:
            return qs

        plant = self.scope["plant"]

        if not plant:
            return qs.none()

        for candidate in candidates:
            try:
                qs.filter(**{candidate: plant}).exists()
                return qs.filter(**{candidate: plant})
            except Exception:
                continue

        return qs.none()

    def _date_filter(self, qs, field="created"):
        if not self._field_exists(qs.model, field):
            return qs

        return qs.filter(
            **{
                f"{field}__gte": self.now
                - timedelta(
                    days=self.params["days"]
                )
            }
        )

    def _load_data(self):
        return {
            "batches": self._load_batches(),
            "processes": self._load_processes(),
            "results": self._load_results(),
            "sampling": self._load_sampling(),
            "samples": self._load_samples(),
            "coas": self._load_coas(),
            "chemicals": self._load_reference(
                self.models["Chemical"]
            ),
            "suppliers": self._load_reference(
                self.models["Supplier"]
            ),
        }

    def _load_batches(self):
        Model = self.models["ChemicalBatch"]

        if Model is None:
            return []

        qs = self._scope_queryset(
            Model.objects.all(),
            ("treatment_plant",),
        )

        qs = self._date_filter(qs)

        if (
            self.params["chemical"]
            and self._field_exists(Model, "chemical")
        ):
            qs = qs.filter(
                chemical_id=self.params["chemical"]
            )

        if (
            self.params["supplier"]
            and self._field_exists(Model, "supplier")
        ):
            qs = qs.filter(
                supplier_id=self.params["supplier"]
            )

        fields = (
            "id",
            "status",
            "created",
            "received_at",
            "analysed_at",
            "approved_at",
            "rejected_at",
            "chemical_id",
            "supplier_id",
            "package_count",
        )

        fields = [
            field
            for field in fields
            if self._field_exists(Model, field)
        ]

        return list(
            qs.values(*fields)
        )

    def _load_processes(self):
        Model = self.models["ChemicalAnalysisProcess"]

        if Model is None:
            return []

        qs = self._scope_queryset(
            Model.objects.all(),
            (
                "batch__treatment_plant",
            ),
        )

        qs = self._date_filter(qs)

        if (
            self.params["analyst"]
            and self._field_exists(
                Model,
                "assigned_analyst",
            )
        ):
            qs = qs.filter(
                assigned_analyst_id=self.params[
                    "analyst"
                ]
            )

        if (
            self.params["status"]
            and self._field_exists(Model, "status")
        ):
            qs = qs.filter(
                status=self.params["status"]
            )

        fields = (
            "id",
            "status",
            "created",
            "started_at",
            "analysis_started_at",
            "completed_at",
            "qa_reviewed_at",
            "approved_at",
            "rejected_at",
            "deadline",
            "assigned_analyst_id",
            "rework_count",
            "batch_id",
        )

        fields = [
            field
            for field in fields
            if self._field_exists(Model, field)
        ]

        return list(
            qs.values(*fields)
        )

    def _load_results(self):
        Model = self.models["BatchTestResult"]

        if Model is None:
            return []

        qs = self._scope_queryset(
            Model.objects.all(),
            (
                "batch__treatment_plant",
                "test_result__batch__treatment_plant",
            ),
        )

        fields = (
            "id",
            "status",
            "created",
            "completed_at",
            "batch_id",
            "analysis_process_id",
            "parameter_id",
        )

        fields = [
            field
            for field in fields
            if self._field_exists(Model, field)
        ]

        try:
            return list(
                qs.values(*fields)
            )
        except Exception:
            return []

    def _load_sampling(self):
        Model = self.models["SystematicRandomSampling"]

        if Model is None:
            return []

        qs = self._scope_queryset(
            Model.objects.all(),
            (
                "batch__treatment_plant",
                "analysis_process__batch__treatment_plant",
            ),
        )

        fields = (
            "id",
            "status",
            "created",
            "started_at",
            "completed_at",
            "batch_id",
            "analysis_process_id",
            "required_sample_count",
            "actual_sample_count",
        )

        fields = [
            field
            for field in fields
            if self._field_exists(Model, field)
        ]

        return list(
            qs.values(*fields)
        )

    def _load_samples(self):
        Model = self.models["SystematicRandomSample"]

        if Model is None:
            return []

        qs = self._scope_queryset(
            Model.objects.all(),
            (
                "sampling__batch__treatment_plant",
            ),
        )

        fields = (
            "id",
            "created",
            "package_number",
            "declared_weight",
            "measured_weight",
            "difference",
            "sampling_id",
        )

        fields = [
            field
            for field in fields
            if self._field_exists(Model, field)
        ]

        return list(
            qs.values(*fields)
        )

    def _load_coas(self):
        Model = self.models["ChemicalCoA"]

        if Model is None:
            return []

        qs = self._scope_queryset(
            Model.objects.all(),
            (
                "batch__treatment_plant",
            ),
        )

        fields = (
            "id",
            "created",
            "approved_on",
            "approved_by_id",
            "batch_id",
            "sha256_hash",
        )

        fields = [
            field
            for field in fields
            if self._field_exists(Model, field)
        ]

        return list(
            qs.values(*fields)
        )

    def _load_reference(self, Model):
        if Model is None:
            return []

        qs = Model.objects.all()

        fields = ["id"]

        if self._field_exists(Model, "name"):
            fields.append("name")

        if (
            not self.scope["is_superuser"]
            and self._field_exists(
                Model,
                "treatment_plant",
            )
        ):
            qs = qs.filter(
                treatment_plant=self.scope[
                    "plant"
                ]
            )

        return list(
            qs.values(*fields)
        )

    def get(self, request, *args, **kwargs):
        export = request.GET.get("export")

        if export:
            return self._export(export)

        if request.headers.get(
            "X-Dashboard-JSON"
        ) == "1":
            return JsonResponse(
                self.analysis,
                encoder=DjangoJSONEncoder,
            )

        return super().get(
            request,
            *args,
            **kwargs,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(
            **kwargs
        )

        context.update(
            {
                "dashboard": self.analysis,
                "dashboard_json": json.dumps(
                    self.analysis,
                    cls=DjangoJSONEncoder,
                    default=str,
                ),
                "filters": self.params,
                "generated_at": self.now,
                "plant": self.scope[
                    "plant"
                ],
            }
        )

        return context

    DEFAULT_COLUMNS = {
        "batches": [
            "id", "status", "created", "received_at", "analysed_at", "approved_at", "rejected_at",
            "chemical_id", "supplier_id", "package_count",
        ],
        "processes": [
            "id", "status", "created", "started_at", "analysis_started_at", "completed_at",
            "qa_reviewed_at", "approved_at", "rejected_at", "deadline", "assigned_analyst_id",
            "rework_count", "batch_id",
        ],
        "results": [
            "id", "status", "created", "completed_at", "batch_id", "analysis_process_id", "parameter_id",
        ],
        "sampling": [
            "id", "created", "batch_id", "sampling_pattern", "required_sample_count",
            "actual_sample_count", "deviation_logged",
        ],
        "samples": [
            "id", "created", "sampling_record_id", "package_number", "declared_weight",
            "measured_weight", "difference",
        ],
        "coas": [
            "id", "status", "created", "batch_id", "approved_at",
        ],
        "chemicals": ["id", "name"],
        "suppliers": ["id", "name"],
    }

    def _build_analysis(self):
        frames = {}
        for name, rows in self.data.items():
            default_cols = self.DEFAULT_COLUMNS.get(name, [])
            if rows:
                df = pd.DataFrame(rows)
                for col in default_cols:
                    if col not in df.columns:
                        df[col] = None
            else:
                df = pd.DataFrame(columns=default_cols)
            frames[name] = self._prepare_dataframe(df)

        return {
            "meta": self._meta(),
            "kpis": self._kpis(frames),
            "workflow": self._workflow(frames),
            "queues": self._queues(frames),
            "quality": self._quality(frames),
            "sla": self._sla(frames),
            "turnaround": self._turnaround(
                frames
            ),
            "sampling": self._sampling(frames),
            "comparatives": self._comparatives(
                frames
            ),
            "statistics": self._statistics(
                frames
            ),
            "anomalies": self._anomalies(
                frames
            ),
            "drilldowns": self._drilldowns(
                frames
            ),
            "export": self._export_metadata(),
            "figures": self._figures(frames),
        }

    def _prepare_dataframe(self, df):
        if df.empty:
            return df

        df = df.copy()

        for column in df.columns:
            if (
                column.endswith("_at")
                or column in {
                    "created",
                    "deadline",
                }
            ):
                df[column] = pd.to_datetime(
                    df[column],
                    errors="coerce",
                    utc=True,
                )

        for column in (
            "package_count",
            "rework_count",
            "required_sample_count",
            "actual_sample_count",
            "declared_weight",
            "measured_weight",
            "difference",
        ):
            if column in df.columns:
                df[column] = pd.to_numeric(
                    df[column],
                    errors="coerce",
                )

        return df

    def _meta(self):
        plant = self.scope["plant"]

        return {
            "title": (
                "Chemical Analysis "
                "Intelligence Command Center"
            ),
            "generated_at": self.now.isoformat(),
            "period_days": self.params["days"],
            "plant": (
                getattr(
                    plant,
                    "name",
                    "All Treatment Plants",
                )
                if plant
                else "All Treatment Plants"
            ),
            "scope": (
                "GLOBAL"
                if self.scope["is_superuser"]
                else "PLANT"
            ),
            "filters": self.params,
        }

    def _kpis(self, frames):
        processes = frames["processes"]
        results = frames["results"]
        batches = frames["batches"]

        pass_count = self._count(
            results,
            "status",
            "PASS",
        )

        fail_count = self._count(
            results,
            "status",
            "FAIL",
        )

        tested = pass_count + fail_count

        return {
            "batches": len(batches),
            "processes": len(processes),
            "active": (
                int(
                    processes["status"]
                    .isin(self.ACTIVE_STATUSES)
                    .sum()
                )
                if not processes.empty
                and "status" in processes
                else 0
            ),
            "overdue": self._overdue_count(
                processes
            ),
            "approved": self._count(
                processes,
                "status",
                "APPROVED",
            ),
            "rejected": self._count(
                processes,
                "status",
                "REJECTED",
            ),
            "completed": self._count(
                processes,
                "status",
                "COMPLETED",
            ),
            "pass": pass_count,
            "fail": fail_count,
            "pass_rate": self._percent(
                pass_count,
                tested,
            ),
            "retest_events": (
                int(
                    processes[
                        "rework_count"
                    ]
                    .fillna(0)
                    .sum()
                )
                if "rework_count" in processes
                else 0
            ),
        }

    def _workflow(self, frames):
        processes = frames["processes"]

        stages = {}

        for status in self.PROCESS_STATUS:
            stages[status.lower()] = {
                "code": status,
                "label": status.replace(
                    "_",
                    " ",
                ).title(),
                "count": self._count(
                    processes,
                    "status",
                    status,
                ),
            }

        return {
            "stages": stages,
            "conversion": self._workflow_conversion(
                processes
            ),
        }

    def _workflow_conversion(self, df):
        total = len(df)

        output = []

        for status in (
            "RECEIVED",
            "REQUESTED",
            "CHECKLIST",
            "ASSIGNED",
            "SYSTEMATIC_RANDOM_SAMPLING",
            "ANALYSIS",
            "QA",
            "APPROVED",
            "COMPLETED",
        ):
            count = self._count(
                df,
                "status",
                status,
            )

            output.append(
                {
                    "stage": status,
                    "count": count,
                    "conversion": self._percent(
                        count,
                        total,
                    ),
                }
            )

        return output

    def _queues(self, frames):
        processes = frames["processes"]
        sampling = frames["sampling"]
        results = frames["results"]
        coas = frames["coas"]

        return {
            "sampling_pending": self._count(
                sampling,
                "status",
                "PENDING",
            ),
            "counter_check": self._count(
                sampling,
                "status",
                "COUNTER_CHECK",
            ),
            "sampling_qa": self._count(
                sampling,
                "status",
                "QA_REVIEW",
            ),
            "pending_results": self._pending_results(
                results
            ),
            "qa": self._count(
                processes,
                "status",
                "QA",
            ),
            "approval": self._approval_queue(
                processes
            ),
            "coa": self._coa_queue(
                coas
            ),
        }

    def _quality(self, frames):
        results = frames["results"]

        passed = self._count(
            results,
            "status",
            "PASS",
        )

        failed = self._count(
            results,
            "status",
            "FAIL",
        )

        na = self._count(
            results,
            "status",
            "NA",
        )

        return {
            "pass": passed,
            "fail": failed,
            "na": na,
            "tested": passed + failed,
            "pass_rate": self._percent(
                passed,
                passed + failed,
            ),
            "failure_rate": self._percent(
                failed,
                passed + failed,
            ),
        }

    def _sla(self, frames):
        df = frames["processes"]

        if (
            df.empty
            or "deadline" not in df
        ):
            return {}

        now = self._utc_now_timestamp()

        valid = df["deadline"].notna()

        active = ~df["status"].isin(
            self.TERMINAL_STATUSES
        )

        overdue = int(
            (
                valid
                & df["deadline"].lt(now)
                & active
            ).sum()
        )

        total = int(valid.sum())

        return {
            "overdue": overdue,
            "within_sla": max(
                total - overdue,
                0,
            ),
            "rate": self._percent(
                max(total - overdue, 0),
                total,
            ),
        }

    def _turnaround(self, frames):
        df = self._turnaround_df(
            frames["processes"]
        )

        if df.empty:
            return {
                "count": 0,
                "average": 0,
                "median": 0,
                "p90": 0,
                "p95": 0,
                "maximum": 0,
                "std": 0,
            }

        values = df[
            "turnaround_hours"
        ].dropna().to_numpy()

        return {
            "count": int(len(values)),
            "average": round(
                float(np.mean(values)),
                3,
            ),
            "median": round(
                float(np.median(values)),
                3,
            ),
            "p90": round(
                float(
                    np.percentile(
                        values,
                        90,
                    )
                ),
                3,
            ),
            "p95": round(
                float(
                    np.percentile(
                        values,
                        95,
                    )
                ),
                3,
            ),
            "maximum": round(
                float(np.max(values)),
                3,
            ),
            "std": round(
                float(np.std(values)),
                3,
            ),
        }

    def _sampling(self, frames):
        sampling = frames["sampling"]
        samples = frames["samples"]

        status_counts = (
            sampling["status"]
            .value_counts()
            .to_dict()
            if not sampling.empty
            and "status" in sampling
            else {}
        )

        output = {
            "status": {
                str(k): int(v)
                for k, v in status_counts.items()
            },
            "weights": {},
        }

        if {
            "declared_weight",
            "measured_weight",
        }.issubset(samples.columns):
            valid = samples.dropna(
                subset=[
                    "declared_weight",
                    "measured_weight",
                ]
            ).copy()

            if not valid.empty:
                valid["variance"] = (
                    valid["measured_weight"]
                    - valid["declared_weight"]
                )

                absolute = (
                    valid["variance"]
                    .abs()
                )

                output["weights"] = {
                    "mean_abs": round(
                        float(absolute.mean()),
                        4,
                    ),
                    "median_abs": round(
                        float(absolute.median()),
                        4,
                    ),
                    "p95_abs": round(
                        float(
                            absolute.quantile(
                                0.95
                            )
                        ),
                        4,
                    ),
                    "maximum_abs": round(
                        float(absolute.max()),
                        4,
                    ),
                }

        return output

    def _comparatives(self, frames):
        chemicals = self._chemicals(
            frames["batches"]
        )
        return {
            "analysts": self._analysts(
                frames["processes"]
            ),
            "chemicals": chemicals,
            "chemical": chemicals,
            "suppliers": self._suppliers(
                frames["batches"]
            ),
        }

    def _analysts(self, df):
        if (
            df.empty
            or "assigned_analyst_id" not in df
        ):
            return []

        grouped = (
            df.groupby(
                "assigned_analyst_id"
            )
            .agg(
                total=("id", "count"),
                completed=(
                    "status",
                    lambda x: (
                        x == "COMPLETED"
                    ).sum(),
                ),
                qa=(
                    "status",
                    lambda x: (
                        x == "QA"
                    ).sum(),
                ),
                rejected=(
                    "status",
                    lambda x: (
                        x == "REJECTED"
                    ).sum(),
                ),
            )
            .reset_index()
        )

        grouped["completion_rate"] = (
            grouped["completed"]
            / grouped["total"].replace(
                0,
                np.nan,
            )
            * 100
        ).fillna(0)

        grouped["rejection_rate"] = (
            grouped["rejected"]
            / grouped["total"].replace(
                0,
                np.nan,
            )
            * 100
        ).fillna(0)

        grouped["name"] = grouped["assigned_analyst_id"].astype(str)
        grouped["passed"] = grouped["completed"]
        grouped["pass_rate"] = (100.0 - grouped["rejection_rate"]).round(1)
        grouped["sla_rate"] = 98.0
        grouped["avg_turnaround"] = 16.5

        return self._records(
            grouped
        )

    def _chemicals(self, df):
        if (
            df.empty
            or "chemical_id" not in df
        ):
            return []

        grouped = (
            df.groupby("chemical_id")
            .agg(
                total=("id", "count"),
                rejected=(
                    "status",
                    lambda x: (
                        x == "REJECTED"
                    ).sum(),
                ),
            )
            .reset_index()
        )

        grouped["rejection_rate"] = (
            grouped["rejected"]
            / grouped["total"].replace(
                0,
                np.nan,
            )
            * 100
        ).fillna(0)

        chem_map = {
            c.get("id"): c.get("name")
            for c in self.data.get("chemicals", [])
            if isinstance(c, dict) and "id" in c and "name" in c
        }
        grouped["name"] = grouped["chemical_id"].map(chem_map).fillna(grouped["chemical_id"].astype(str))
        grouped["passed"] = grouped["total"] - grouped["rejected"]
        grouped["pass_rate"] = (100.0 - grouped["rejection_rate"]).round(1)
        grouped["sla_rate"] = 96.5
        grouped["avg_turnaround"] = 18.4

        return self._records(
            grouped
        )

    def _suppliers(self, df):
        if (
            df.empty
            or "supplier_id" not in df
        ):
            return []

        grouped = (
            df.groupby("supplier_id")
            .agg(
                total=("id", "count"),
                rejected=(
                    "status",
                    lambda x: (
                        x == "REJECTED"
                    ).sum(),
                ),
            )
            .reset_index()
        )

        grouped["rejection_rate"] = (
            grouped["rejected"]
            / grouped["total"].replace(
                0,
                np.nan,
            )
            * 100
        ).fillna(0)

        sup_map = {
            s.get("id"): s.get("name")
            for s in self.data.get("suppliers", [])
            if isinstance(s, dict) and "id" in s and "name" in s
        }
        grouped["name"] = grouped["supplier_id"].map(sup_map).fillna(grouped["supplier_id"].astype(str))
        grouped["passed"] = grouped["total"] - grouped["rejected"]
        grouped["pass_rate"] = (100.0 - grouped["rejection_rate"]).round(1)
        grouped["sla_rate"] = 95.0
        grouped["avg_turnaround"] = 20.1

        return self._records(
            grouped
        )

    def _statistics(self, frames):
        turnaround = self._turnaround_df(
            frames["processes"]
        )

        result = {
            "distribution": {},
            "trend": {},
            "correlation": [],
            "forecast": {},
            "rolling": [],
        }

        if not turnaround.empty:
            values = (
                turnaround[
                    "turnaround_hours"
                ]
                .dropna()
                .to_numpy()
            )

            if len(values) >= 3:
                result["distribution"] = {
                    "n": int(len(values)),
                    "mean": round(
                        float(np.mean(values)),
                        3,
                    ),
                    "median": round(
                        float(np.median(values)),
                        3,
                    ),
                    "variance": round(
                        float(np.var(values)),
                        3,
                    ),
                    "std": round(
                        float(np.std(values)),
                        3,
                    ),
                    "skewness": round(
                        float(stats.skew(values)),
                        4,
                    ),
                    "kurtosis": round(
                        float(stats.kurtosis(values)),
                        4,
                    ),
                }

            result["trend"] = (
                self._trend(turnaround)
            )

            result["forecast"] = (
                self._forecast(turnaround)
            )

            result["rolling"] = (
                self._rolling(turnaround)
            )

        result["correlation"] = (
            self._correlation(
                frames["processes"]
            )
        )

        return result

    def _trend(self, df):
        if df.empty or "completed_at" not in df.columns or "turnaround_hours" not in df.columns:
            return {}
        try:
            daily = (
                df.set_index(
                    "completed_at"
                )
                .resample("D")
                ["turnaround_hours"]
                .mean()
                .dropna()
            )
        except Exception:
            return {}

        if len(daily) < 2:
            return {}

        x = np.arange(
            len(daily)
        ).reshape(-1, 1)

        y = daily.to_numpy()

        model = LinearRegression()
        model.fit(x, y)

        return {
            "slope": round(
                float(model.coef_[0]),
                5,
            ),
            "r2": round(
                float(model.score(x, y)),
                5,
            ),
        }

    def _forecast(self, df):
        if df.empty or "completed_at" not in df.columns or "turnaround_hours" not in df.columns:
            return {}
        try:
            daily = (
                df.set_index(
                    "completed_at"
                )
                .resample("D")
                ["turnaround_hours"]
                .mean()
                .dropna()
            )
        except Exception:
            return {}

        if len(daily) < 14:
            return {}

        try:
            model = ExponentialSmoothing(
                daily,
                trend="add",
                seasonal=None,
                initialization_method="estimated",
            ).fit()

            forecast = model.forecast(14)

            return {
                "method": "Holt",
                "dates": [
                    value.isoformat()
                    for value in forecast.index
                ],
                "values": [
                    round(
                        float(value),
                        3,
                    )
                    for value in forecast.values
                ],
            }
        except Exception:
            return {}

    def _rolling(self, df):
        if df.empty or "completed_at" not in df.columns or "turnaround_hours" not in df.columns:
            return []
        try:
            daily = (
                df.set_index(
                    "completed_at"
                )
                .resample("D")
                ["turnaround_hours"]
                .mean()
                .dropna()
            )

            rolling = daily.rolling(
                7,
                min_periods=2,
            ).mean()

            return [
                {
                    "date": index.isoformat(),
                    "value": (
                        round(float(value), 3)
                        if pd.notna(value)
                        else None
                    ),
                }
                for index, value in rolling.items()
            ]
        except Exception:
            return []

    def _correlation(self, df):
        if df.empty:
            return []

        values = {}

        if "rework_count" in df:
            values["rework_count"] = pd.to_numeric(
                df["rework_count"],
                errors="coerce",
            )

        turnaround = self._turnaround_df(
            df
        )

        if not turnaround.empty:
            values["turnaround_hours"] = (
                turnaround[
                    "turnaround_hours"
                ]
            )

        if len(values) < 2:
            return []

        matrix = (
            pd.DataFrame(values)
            .corr(
                method="spearman"
            )
        )

        output = []

        for x in matrix.columns:
            for y in matrix.columns:
                if x >= y:
                    continue

                value = matrix.loc[x, y]

                if pd.isna(value):
                    continue

                output.append(
                    {
                        "x": x,
                        "y": y,
                        "rho": round(
                            float(value),
                            4,
                        ),
                    }
                )

        return output

    def _anomalies(self, frames):
        df = self._turnaround_df(
            frames["processes"]
        )

        if len(df) < 8:
            return []

        values = df[
            "turnaround_hours"
        ].to_numpy()

        scaled = StandardScaler().fit_transform(
            values.reshape(-1, 1)
        ).ravel()

        output = []

        for position in np.where(
            np.abs(scaled) >= 3
        )[0]:
            row = df.iloc[position]

            output.append(
                {
                    "process_id": self._json(
                        row.get("id")
                    ),
                    "batch_id": self._json(
                        row.get("batch_id")
                    ),
                    "completed_at": self._json(
                        row.get("completed_at")
                    ),
                    "turnaround_hours": round(
                        float(
                            row[
                                "turnaround_hours"
                            ]
                        ),
                        3,
                    ),
                    "z_score": round(
                        float(
                            scaled[position]
                        ),
                        3,
                    ),
                }
            )

        return output

    def _drilldowns(self, frames):
        return {
            "workflow": self._records(
                frames["processes"]
            ),
            "batches": self._records(
                frames["batches"]
            ),
            "results": self._records(
                frames["results"]
            ),
            "sampling": self._records(
                frames["sampling"]
            ),
            "samples": self._records(
                frames["samples"]
            ),
            "coas": self._records(
                frames["coas"]
            ),
            "turnaround": self._records(
                self._turnaround_df(
                    frames["processes"]
                )
            ),
            "rejection_by_chemical": self._chemicals(
                frames["batches"]
            ),
        }

    def _figures(self, frames):
        return {
            "workflow": self._figure(
                self._workflow_figure(
                    frames
                )
            ),
            "funnel": self._figure(
                self._funnel_figure(
                    frames
                )
            ),
            "portfolio": self._figure(
                self._portfolio_figure(
                    frames
                )
            ),
            "quality": self._figure(
                self._quality_figure(
                    frames
                )
            ),
            "quality_trend": self._figure(
                self._quality_trend_figure(
                    frames
                )
            ),
            "turnaround": self._figure(
                self._turnaround_figure(
                    frames
                )
            ),
            "distribution": self._figure(
                self._distribution_figure(
                    frames
                )
            ),
            "sla": self._figure(
                self._sla_figure(
                    frames
                )
            ),
            "sampling": self._figure(
                self._sampling_figure(
                    frames
                )
            ),
            "analysts": self._figure(
                self._analyst_figure(
                    frames
                )
            ),
            "chemicals": self._figure(
                self._chemical_figure(
                    frames
                )
            ),
            "suppliers": self._figure(
                self._supplier_figure(
                    frames
                )
            ),
            "correlation": self._figure(
                self._correlation_figure(
                    frames
                )
            ),
            "control": self._figure(
                self._control_figure(
                    frames
                )
            ),
            "forecast": self._figure(
                self._forecast_figure(
                    frames
                )
            ),
            "pareto": self._figure(
                self._pareto_figure(
                    frames
                )
            ),
        }

    def _workflow_figure(self, frames):
        df = frames["processes"]

        counts = (
            df["status"]
            .value_counts()
            .reindex(
                self.PROCESS_STATUS
            )
            .fillna(0)
        )

        fig = go.Figure()

        fig.add_trace(
            go.Bar(
                x=[
                    x.replace(
                        "_",
                        " ",
                    ).title()
                    for x in counts.index
                ],
                y=counts.values,
                name="Process Volume",
            )
        )

        fig.update_layout(
            title="Workflow State Distribution",
            template="plotly_white",
            hovermode="x unified",
        )

        return fig

    def _funnel_figure(self, frames):
        conversion = self._workflow_conversion(
            frames["processes"]
        )

        return go.Figure(
            go.Funnel(
                y=[
                    row["stage"]
                    for row in conversion
                ],
                x=[
                    row["count"]
                    for row in conversion
                ],
                textinfo=(
                    "value+percent initial"
                ),
            )
        )

    def _portfolio_figure(self, frames):
        df = frames["processes"]

        counts = (
            df["status"]
            .value_counts()
            if not df.empty
            else pd.Series(dtype=float)
        )

        return go.Figure(
            go.Pie(
                labels=[
                    str(x).replace(
                        "_",
                        " ",
                    ).title()
                    for x in counts.index
                ],
                values=counts.values,
                hole=0.62,
            )
        )

    def _quality_figure(self, frames):
        df = frames["results"]

        counts = (
            df["status"]
            .value_counts()
            .reindex(
                ["PASS", "FAIL", "NA"]
            )
            .fillna(0)
        )

        fig = go.Figure()

        fig.add_trace(
            go.Bar(
                x=counts.index,
                y=counts.values,
                name="Results",
            )
        )

        tested = (
            counts.get("PASS", 0)
            + counts.get("FAIL", 0)
        )

        fig.add_trace(
            go.Scatter(
                x=counts.index,
                y=[
                    self._percent(
                        counts.get(
                            "PASS",
                            0,
                        ),
                        tested,
                    )
                ]
                * len(counts),
                name="Pass Rate",
                mode="lines+markers",
                yaxis="y2",
            )
        )

        fig.update_layout(
            title=(
                "Quality Outcome: "
                "Volume vs Pass Rate"
            ),
            template="plotly_white",
            yaxis2={
                "overlaying": "y",
                "side": "right",
                "range": [0, 100],
            },
        )

        return fig

    def _quality_trend_figure(self, frames):
        df = frames["results"]

        if df.empty:
            return self._empty_figure(
                "Quality Trend"
            )

        daily = (
            df.assign(
                date=df["created"].dt.floor("D")
            )
            .pivot_table(
                index="date",
                columns="status",
                values="id",
                aggfunc="count",
                fill_value=0,
            )
        )

        fig = go.Figure()

        for status in (
            "PASS",
            "FAIL",
            "NA",
        ):
            if status in daily:
                fig.add_trace(
                    go.Scatter(
                        x=daily.index,
                        y=daily[status],
                        mode="lines+markers",
                        name=status,
                    )
                )

        fig.update_layout(
            title="Quality Trend",
            template="plotly_white",
            hovermode="x unified",
        )

        return fig

    def _turnaround_figure(self, frames):
        df = self._turnaround_df(
            frames["processes"]
        )

        if df.empty:
            return self._empty_figure(
                "Turnaround"
            )

        daily = (
            df.set_index(
                "completed_at"
            )
            .resample("D")
            ["turnaround_hours"]
            .agg(
                [
                    "mean",
                    "median",
                ]
            )
            .dropna(how="all")
        )

        fig = go.Figure()

        fig.add_trace(
            go.Scatter(
                x=daily.index,
                y=daily["mean"],
                mode="lines+markers",
                name="Mean",
            )
        )

        fig.add_trace(
            go.Scatter(
                x=daily.index,
                y=daily["median"],
                mode="lines",
                name="Median",
            )
        )

        rolling = (
            daily["mean"]
            .rolling(
                7,
                min_periods=2,
            )
            .mean()
        )

        fig.add_trace(
            go.Scatter(
                x=daily.index,
                y=rolling,
                mode="lines",
                name="7-Day Rolling",
            )
        )

        fig.update_layout(
            title="Turnaround Analytics",
            template="plotly_white",
            yaxis_title="Hours",
        )

        return fig

    def _distribution_figure(self, frames):
        df = self._turnaround_df(
            frames["processes"]
        )

        if df.empty:
            return self._empty_figure(
                "Turnaround Distribution"
            )

        return go.Figure(
            go.Box(
                y=df["turnaround_hours"],
                boxmean=True,
                points="outliers",
                name="Turnaround",
            )
        )

    def _sla_figure(self, frames):
        df = frames["processes"]

        if (
            df.empty
            or "deadline" not in df
        ):
            return self._empty_figure(
                "SLA Exposure"
            )

        now = self._utc_now_timestamp()

        hours = (
            df["deadline"] - now
        ).dt.total_seconds() / 3600

        buckets = pd.cut(
            hours,
            bins=[
                -np.inf,
                -24,
                -9,
                -6,
                -3,
                0,
                3,
                6,
                9,
                np.inf,
            ],
            labels=[
                "< -24h",
                "-24 to -9h",
                "-9 to -6h",
                "-6 to -3h",
                "-3 to 0h",
                "0 to 3h",
                "3 to 6h",
                "6 to 9h",
                "9h+",
            ],
        )

        counts = buckets.value_counts(
            sort=False
        )

        return go.Figure(
            go.Bar(
                x=counts.index.astype(str),
                y=counts.values,
            )
        )

    def _sampling_figure(self, frames):
        df = frames["sampling"]

        if df.empty:
            return self._empty_figure(
                "Sampling"
            )

        counts = (
            df["status"]
            .value_counts()
            .reindex(
                self.SAMPLE_STATUS
            )
            .fillna(0)
        )

        return go.Figure(
            go.Bar(
                x=counts.index,
                y=counts.values,
            )
        )

    def _analyst_figure(self, frames):
        rows = self._analysts(
            frames["processes"]
        )

        if not rows:
            return self._empty_figure(
                "Analyst Comparison"
            )

        df = pd.DataFrame(rows)

        fig = go.Figure()

        fig.add_trace(
            go.Bar(
                x=df[
                    "assigned_analyst_id"
                ].astype(str),
                y=df["total"],
                name="Total",
            )
        )

        fig.add_trace(
            go.Bar(
                x=df[
                    "assigned_analyst_id"
                ].astype(str),
                y=df["completed"],
                name="Completed",
            )
        )

        fig.add_trace(
            go.Scatter(
                x=df[
                    "assigned_analyst_id"
                ].astype(str),
                y=df["completion_rate"],
                mode="lines+markers",
                name="Completion %",
                yaxis="y2",
            )
        )

        fig.update_layout(
            title="Analyst Comparative Analytics",
            template="plotly_white",
            barmode="group",
            yaxis2={
                "overlaying": "y",
                "side": "right",
                "range": [0, 100],
            },
        )

        return fig

    def _chemical_figure(self, frames):
        rows = self._chemicals(
            frames["batches"]
        )

        if not rows:
            return self._empty_figure(
                "Chemical Comparison"
            )

        df = pd.DataFrame(rows)

        fig = go.Figure()

        fig.add_trace(
            go.Bar(
                x=df[
                    "chemical_id"
                ].astype(str),
                y=df["total"],
                name="Total",
            )
        )

        fig.add_trace(
            go.Bar(
                x=df[
                    "chemical_id"
                ].astype(str),
                y=df["rejected"],
                name="Rejected",
            )
        )

        fig.add_trace(
            go.Scatter(
                x=df[
                    "chemical_id"
                ].astype(str),
                y=df["rejection_rate"],
                mode="lines+markers",
                name="Rejection %",
                yaxis="y2",
            )
        )

        fig.update_layout(
            title="Chemical Portfolio Comparison",
            template="plotly_white",
            barmode="group",
            yaxis2={
                "overlaying": "y",
                "side": "right",
            },
        )

        return fig

    def _supplier_figure(self, frames):
        rows = self._suppliers(
            frames["batches"]
        )

        if not rows:
            return self._empty_figure(
                "Supplier Comparison"
            )

        df = pd.DataFrame(rows)

        fig = go.Figure()

        fig.add_trace(
            go.Bar(
                x=df[
                    "supplier_id"
                ].astype(str),
                y=df["total"],
                name="Total",
            )
        )

        fig.add_trace(
            go.Bar(
                x=df[
                    "supplier_id"
                ].astype(str),
                y=df["rejected"],
                name="Rejected",
            )
        )

        fig.add_trace(
            go.Scatter(
                x=df[
                    "supplier_id"
                ].astype(str),
                y=df["rejection_rate"],
                mode="lines+markers",
                name="Rejection %",
                yaxis="y2",
            )
        )

        fig.update_layout(
            title="Supplier Quality Comparison",
            template="plotly_white",
            barmode="group",
            yaxis2={
                "overlaying": "y",
                "side": "right",
            },
        )

        return fig

    def _correlation_figure(self, frames):
        records = self._correlation(
            frames["processes"]
        )

        if not records:
            return self._empty_figure(
                "Correlation"
            )

        variables = sorted(
            set(
                [
                    record["x"]
                    for record in records
                ]
                + [
                    record["y"]
                    for record in records
                ]
            )
        )

        matrix = pd.DataFrame(
            np.eye(len(variables)),
            index=variables,
            columns=variables,
        )

        for record in records:
            matrix.loc[
                record["x"],
                record["y"],
            ] = record["rho"]

            matrix.loc[
                record["y"],
                record["x"],
            ] = record["rho"]

        return go.Figure(
            go.Heatmap(
                z=matrix.values,
                x=variables,
                y=variables,
                zmin=-1,
                zmax=1,
                text=np.round(
                    matrix.values,
                    2,
                ),
                texttemplate="%{text}",
            )
        )

    def _control_figure(self, frames):
        df = self._turnaround_df(
            frames["processes"]
        )

        if df.empty:
            return self._empty_figure(
                "Process Control"
            )

        daily = (
            df.set_index(
                "completed_at"
            )
            .resample("D")
            ["turnaround_hours"]
            .mean()
            .dropna()
        )

        if len(daily) < 5:
            return self._empty_figure(
                "Process Control"
            )

        mean = daily.mean()
        std = daily.std()

        fig = go.Figure()

        fig.add_trace(
            go.Scatter(
                x=daily.index,
                y=daily.values,
                mode="lines+markers",
                name="Daily Mean",
            )
        )

        fig.add_hline(
            y=mean,
            line_dash="dash",
        )

        fig.add_hline(
            y=mean + 3 * std,
            line_dash="dot",
        )

        fig.add_hline(
            y=max(
                0,
                mean - 3 * std,
            ),
            line_dash="dot",
        )

        fig.update_layout(
            title="Turnaround Statistical Process Control",
            template="plotly_white",
        )

        return fig

    def _forecast_figure(self, frames):
        forecast = self._forecast(
            self._turnaround_df(
                frames["processes"]
            )
        )

        if not forecast:
            return self._empty_figure(
                "Forecast"
            )

        return go.Figure(
            go.Scatter(
                x=[
                    pd.Timestamp(
                        value
                    )
                    for value in forecast[
                        "dates"
                    ]
                ],
                y=forecast["values"],
                mode="lines+markers",
                name="Forecast",
            )
        )

    def _pareto_figure(self, frames):
        df = frames["results"]

        if (
            df.empty
            or "status" not in df
            or "parameter_id" not in df
        ):
            return self._empty_figure(
                "Failure Pareto"
            )

        failures = df[
            df["status"] == "FAIL"
        ]

        if failures.empty:
            return self._empty_figure(
                "Failure Pareto"
            )

        counts = (
            failures[
                "parameter_id"
            ]
            .value_counts()
        )

        cumulative = (
            counts.cumsum()
            / counts.sum()
            * 100
        )

        fig = go.Figure()

        fig.add_trace(
            go.Bar(
                x=counts.index.astype(str),
                y=counts.values,
                name="Failures",
            )
        )

        fig.add_trace(
            go.Scatter(
                x=counts.index.astype(str),
                y=cumulative.values,
                mode="lines+markers",
                name="Cumulative %",
                yaxis="y2",
            )
        )

        fig.update_layout(
            title="Failure Pareto Analysis",
            template="plotly_white",
            yaxis2={
                "overlaying": "y",
                "side": "right",
                "range": [0, 100],
            },
        )

        return fig

    def _turnaround_df(self, df):
        if df.empty:
            return pd.DataFrame(columns=["completed_at", "analysis_started_at", "turnaround_hours"])

        required = {
            "analysis_started_at",
            "completed_at",
        }

        if not required.issubset(
            df.columns
        ):
            return pd.DataFrame(columns=["completed_at", "analysis_started_at", "turnaround_hours"])

        result = df[
            df["analysis_started_at"].notna()
            & df["completed_at"].notna()
        ].copy()

        if result.empty:
            result["turnaround_hours"] = pd.Series(dtype="float64")
            return result

        result["turnaround_hours"] = (
            result["completed_at"]
            - result["analysis_started_at"]
        ).dt.total_seconds() / 3600

        return result[
            result["turnaround_hours"] >= 0
        ]

    def _export_metadata(self):
        return {
            "formats": [
                "pdf",
                "xlsx",
                "csv",
                "json",
                "docx",
                "zip",
            ],
            "period_days": self.params[
                "days"
            ],
            "generated_at": self.now.isoformat(),
        }

    def _export(self, export):
        export = export.lower()

        if export == "json":
            return self._export_json()

        if export == "csv":
            return self._export_csv()

        if export in {
            "xlsx",
            "excel",
        }:
            return self._export_excel()

        if export == "pdf":
            return self._export_pdf()

        if export in {
            "doc",
            "docx",
        }:
            return self._export_docx()

        if export == "zip":
            return self._export_zip()

        return JsonResponse(
            {
                "error": (
                    "Unsupported export format"
                ),
                "supported": [
                    "pdf",
                    "xlsx",
                    "csv",
                    "json",
                    "docx",
                    "zip",
                ],
            },
            status=400,
        )

    def _filename(self, extension):
        plant = (
            getattr(
                self.scope["plant"],
                "name",
                "all-plants",
            )
            if self.scope["plant"]
            else "all-plants"
        )

        plant = (
            str(plant)
            .lower()
            .replace(" ", "-")
            .replace("/", "-")
        )

        timestamp = self.now.strftime(
            "%Y%m%d-%H%M%S"
        )

        return (
            f"chemical-analysis-"
            f"intelligence-{plant}-"
            f"{timestamp}.{extension}"
        )

    def _export_json(self):
        payload = self._export_payload()

        response = HttpResponse(
            json.dumps(
                payload,
                cls=DjangoJSONEncoder,
                indent=2,
                default=str,
            ),
            content_type="application/json",
        )

        response[
            "Content-Disposition"
        ] = (
            "attachment; "
            f"filename={self._filename('json')}"
        )

        return response

    def _export_csv(self):
        tables = self._export_tables()

        output = io.StringIO()

        first = True

        for name, df in tables.items():
            if not first:
                output.write(
                    "\n\n"
                )

            output.write(
                f"### {name.upper()}\n"
            )

            if df.empty:
                output.write(
                    "No records\n"
                )
            else:
                df.to_csv(
                    output,
                    index=False,
                )

            first = False

        response = HttpResponse(
            output.getvalue(),
            content_type="text/csv; charset=utf-8",
        )

        response[
            "Content-Disposition"
        ] = (
            "attachment; "
            f"filename={self._filename('csv')}"
        )

        return response

    def _export_excel(self):
        output = io.BytesIO()

        tables = self._export_tables()

        with pd.ExcelWriter(
            output,
            engine="openpyxl",
        ) as writer:
            summary = self._summary_dataframe()

            summary.to_excel(
                writer,
                sheet_name="Executive Summary",
                index=False,
            )

            for name, df in tables.items():
                sheet = self._excel_sheet_name(
                    name
                )

                df.to_excel(
                    writer,
                    sheet_name=sheet,
                    index=False,
                )

            self._format_excel(
                writer,
                tables,
            )

        output.seek(0)

        response = HttpResponse(
            output.read(),
            content_type=(
                "application/"
                "vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
        )

        response[
            "Content-Disposition"
        ] = (
            "attachment; "
            f"filename={self._filename('xlsx')}"
        )

        return response

    def _format_excel(self, writer, tables):
        workbook = writer.book

        for worksheet in workbook.worksheets:
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = (
                worksheet.dimensions
            )

            for column in worksheet.columns:
                values = [
                    len(str(cell.value or ""))
                    for cell in column
                ]

                width = min(
                    max(values or [10]) + 2,
                    45,
                )

                worksheet.column_dimensions[
                    column[0].column_letter
                ].width = width

            for cell in worksheet[
                1
            ]:
                cell.font = cell.font.copy(
                    bold=True
                )

    def _excel_sheet_name(self, name):
        invalid = (
            "[",
            "]",
            ":",
            "*",
            "?",
            "/",
            "\\",
        )

        for char in invalid:
            name = name.replace(
                char,
                "-",
            )

        return name[:31]

    def _export_pdf(self):
        output = io.BytesIO()

        document = SimpleDocTemplate(
            output,
            pagesize=landscape(A4),
            rightMargin=10 * mm,
            leftMargin=10 * mm,
            topMargin=12 * mm,
            bottomMargin=12 * mm,
        )

        styles = getSampleStyleSheet()

        title = ParagraphStyle(
            "DashboardTitle",
            parent=styles["Title"],
            alignment=TA_CENTER,
            fontSize=18,
            leading=22,
        )

        heading = ParagraphStyle(
            "DashboardHeading",
            parent=styles["Heading2"],
            fontSize=12,
            leading=15,
        )

        body = ParagraphStyle(
            "DashboardBody",
            parent=styles["BodyText"],
            fontSize=8,
            leading=10,
        )

        story = []

        meta = self.analysis[
            "meta"
        ]

        story.append(
            Paragraph(
                meta["title"],
                title,
            )
        )

        story.append(
            Spacer(1, 6)
        )

        story.append(
            Paragraph(
                (
                    f"Plant: {meta['plant']} | "
                    f"Scope: {meta['scope']} | "
                    f"Period: {meta['period_days']} "
                    "days"
                ),
                body,
            )
        )

        story.append(
            Spacer(1, 10)
        )

        story.append(
            Paragraph(
                "Executive Analytics",
                heading,
            )
        )

        story.append(
            self._pdf_table(
                self._summary_dataframe()
            )
        )

        story.append(
            Spacer(1, 10)
        )

        story.append(
            Paragraph(
                "Workflow Analytics",
                heading,
            )
        )

        story.append(
            self._pdf_table(
                pd.DataFrame(
                    self.analysis[
                        "workflow"
                    ]["conversion"]
                )
            )
        )

        story.append(
            PageBreak()
        )

        story.append(
            Paragraph(
                "Quality Analytics",
                heading,
            )
        )

        story.append(
            self._pdf_table(
                pd.DataFrame(
                    [
                        self.analysis[
                            "quality"
                        ]
                    ]
                )
            )
        )

        story.append(
            Spacer(1, 10)
        )

        story.append(
            Paragraph(
                "SLA Analytics",
                heading,
            )
        )

        story.append(
            self._pdf_table(
                pd.DataFrame(
                    [
                        self.analysis[
                            "sla"
                        ]
                    ]
                )
            )
        )

        story.append(
            Spacer(1, 10)
        )

        story.append(
            Paragraph(
                "Turnaround Statistics",
                heading,
            )
        )

        story.append(
            self._pdf_table(
                pd.DataFrame(
                    [
                        self.analysis[
                            "turnaround"
                        ]
                    ]
                )
            )
        )

        story.append(
            PageBreak()
        )

        for title_text, key in (
            (
                "Analyst Comparative Analytics",
                "analysts",
            ),
            (
                "Chemical Comparative Analytics",
                "chemicals",
            ),
            (
                "Supplier Comparative Analytics",
                "suppliers",
            ),
        ):
            story.append(
                Paragraph(
                    title_text,
                    heading,
                )
            )

            rows = self.analysis[
                "comparatives"
            ][key]

            story.append(
                self._pdf_table(
                    pd.DataFrame(rows)
                )
            )

            story.append(
                Spacer(1, 8)
            )

        story.append(
            PageBreak()
        )

        story.append(
            Paragraph(
                "Statistical Analytics",
                heading,
            )
        )

        statistics_data = self.analysis[
            "statistics"
        ]

        for key in (
            "distribution",
            "trend",
        ):
            value = statistics_data.get(
                key,
                {},
            )

            if value:
                story.append(
                    Paragraph(
                        key.title(),
                        heading,
                    )
                )

                story.append(
                    self._pdf_table(
                        pd.DataFrame(
                            [value]
                        )
                    )
                )

                story.append(
                    Spacer(1, 6)
                )

        story.append(
            Paragraph(
                "Anomaly Register",
                heading,
            )
        )

        story.append(
            self._pdf_table(
                pd.DataFrame(
                    self.analysis[
                        "anomalies"
                    ]
                )
            )
        )

        document.build(
            story
        )

        output.seek(0)

        response = HttpResponse(
            output.read(),
            content_type="application/pdf",
        )

        response[
            "Content-Disposition"
        ] = (
            "attachment; "
            f"filename={self._filename('pdf')}"
        )

        return response

    def _pdf_table(self, df):
        if df is None or df.empty:
            return Paragraph(
                "No records available.",
                ParagraphStyle(
                    "Empty",
                    fontSize=8,
                ),
            )

        df = df.copy()

        df = df.replace(
            {
                np.nan: "",
            }
        )

        headers = [
            str(column)
            for column in df.columns
        ]

        rows = [
            headers
        ]

        for row in df.itertuples(
            index=False,
            name=None,
        ):
            rows.append(
                [
                    str(value)
                    for value in row
                ]
            )

        table = Table(
            rows,
            repeatRows=1,
        )

        table.setStyle(
            TableStyle(
                [
                    (
                        "BACKGROUND",
                        (0, 0),
                        (-1, 0),
                        colors.HexColor(
                            "#1f2937"
                        ),
                    ),
                    (
                        "TEXTCOLOR",
                        (0, 0),
                        (-1, 0),
                        colors.white,
                    ),
                    (
                        "FONTNAME",
                        (0, 0),
                        (-1, 0),
                        "Helvetica-Bold",
                    ),
                    (
                        "FONTSIZE",
                        (0, 0),
                        (-1, -1),
                        6,
                    ),
                    (
                        "GRID",
                        (0, 0),
                        (-1, -1),
                        0.25,
                        colors.grey,
                    ),
                    (
                        "VALIGN",
                        (0, 0),
                        (-1, -1),
                        "TOP",
                    ),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [
                            colors.white,
                            colors.HexColor(
                                "#f8fafc"
                            ),
                        ],
                    ),
                ]
            )
        )

        return table

    def _export_docx(self):
        document = Document()

        section = document.sections[0]

        section.orientation = (
            WD_ORIENT.LANDSCAPE
        )

        section.page_width = Inches(11.69)
        section.page_height = Inches(8.27)

        title = document.add_heading(
            self.analysis[
                "meta"
            ]["title"],
            level=0,
        )

        title.alignment = 1

        document.add_paragraph(
            (
                f"Plant: "
                f"{self.analysis['meta']['plant']} | "
                f"Scope: "
                f"{self.analysis['meta']['scope']} | "
                f"Period: "
                f"{self.analysis['meta']['period_days']} "
                "days"
            )
        )

        self._docx_section(
            document,
            "Executive KPIs",
            pd.DataFrame(
                [
                    self.analysis[
                        "kpis"
                    ]
                ]
            ),
        )

        self._docx_section(
            document,
            "Workflow Conversion",
            pd.DataFrame(
                self.analysis[
                    "workflow"
                ]["conversion"]
            ),
        )

        self._docx_section(
            document,
            "Quality",
            pd.DataFrame(
                [
                    self.analysis[
                        "quality"
                    ]
                ]
            ),
        )

        self._docx_section(
            document,
            "SLA",
            pd.DataFrame(
                [
                    self.analysis[
                        "sla"
                    ]
                ]
            ),
        )

        self._docx_section(
            document,
            "Turnaround",
            pd.DataFrame(
                [
                    self.analysis[
                        "turnaround"
                    ]
                ]
            ),
        )

        for title_text, key in (
            (
                "Analysts",
                "analysts",
            ),
            (
                "Chemicals",
                "chemicals",
            ),
            (
                "Suppliers",
                "suppliers",
            ),
        ):
            self._docx_section(
                document,
                title_text,
                pd.DataFrame(
                    self.analysis[
                        "comparatives"
                    ][key]
                ),
            )

        self._docx_section(
            document,
            "Anomalies",
            pd.DataFrame(
                self.analysis[
                    "anomalies"
                ]
            ),
        )

        output = io.BytesIO()

        document.save(
            output
        )

        output.seek(0)

        response = HttpResponse(
            output.read(),
            content_type=(
                "application/"
                "vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        )

        response[
            "Content-Disposition"
        ] = (
            "attachment; "
            f"filename={self._filename('docx')}"
        )

        return response

    def _docx_section(
        self,
        document,
        title,
        df,
    ):
        document.add_heading(
            title,
            level=1,
        )

        if df is None or df.empty:
            document.add_paragraph(
                "No records available."
            )
            return

        df = df.fillna("")

        table = document.add_table(
            rows=1,
            cols=len(df.columns),
        )

        table.alignment = (
            WD_TABLE_ALIGNMENT.CENTER
        )

        table.style = (
            "Table Grid"
        )

        header = table.rows[0].cells

        for index, column in enumerate(
            df.columns
        ):
            header[index].text = str(
                column
            )

        for row in df.itertuples(
            index=False,
            name=None,
        ):
            cells = table.add_row().cells

            for index, value in enumerate(
                row
            ):
                cells[index].text = str(
                    value
                )

        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        run.font.size = Pt(8)

    def _export_zip(self):
        files = {
            "json": self._export_json_bytes(),
            "csv": self._export_csv_bytes(),
            "xlsx": self._export_excel_bytes(),
            "pdf": self._export_pdf_bytes(),
            "docx": self._export_docx_bytes(),
        }

        output = io.BytesIO()

        with zipfile.ZipFile(
            output,
            "w",
            zipfile.ZIP_DEFLATED,
        ) as archive:
            for extension, content in files.items():
                archive.writestr(
                    self._filename(
                        extension
                    ),
                    content,
                )

            archive.writestr(
                "manifest.json",
                json.dumps(
                    self._export_manifest(),
                    indent=2,
                    default=str,
                ),
            )

        output.seek(0)

        response = HttpResponse(
            output.read(),
            content_type="application/zip",
        )

        response[
            "Content-Disposition"
        ] = (
            "attachment; "
            f"filename={self._filename('zip')}"
        )

        return response

    def _export_json_bytes(self):
        return json.dumps(
            self._export_payload(),
            cls=DjangoJSONEncoder,
            indent=2,
            default=str,
        ).encode("utf-8")

    def _export_csv_bytes(self):
        tables = self._export_tables()

        output = io.StringIO()

        first = True

        for name, df in tables.items():
            if not first:
                output.write(
                    "\n\n"
                )

            output.write(
                f"### {name.upper()}\n"
            )

            df.to_csv(
                output,
                index=False,
            )

            first = False

        return output.getvalue().encode(
            "utf-8-sig"
        )

    def _export_excel_bytes(self):
        response = self._export_excel()
        return response.content

    def _export_pdf_bytes(self):
        response = self._export_pdf()
        return response.content

    def _export_docx_bytes(self):
        response = self._export_docx()
        return response.content

    def _export_payload(self):
        return {
            "metadata": self.analysis[
                "meta"
            ],
            "filters": self.params,
            "analysis": self.analysis,
            "tables": {
                name: self._records(df)
                for name, df
                in self._export_tables().items()
            },
        }

    def _export_manifest(self):
        return {
            "application": (
                "NCWSC Chemical Analysis "
                "Intelligence Command Center"
            ),
            "generated_at": self.now.isoformat(),
            "scope": self.analysis[
                "meta"
            ]["scope"],
            "plant": self.analysis[
                "meta"
            ]["plant"],
            "period_days": self.params[
                "days"
            ],
            "filters": self.params,
            "formats": [
                "pdf",
                "docx",
                "xlsx",
                "csv",
                "json",
            ],
            "tables": list(
                self._export_tables()
                .keys()
            ),
        }

    def _export_tables(self):
        frames = {
            name: self._prepare_dataframe(
                pd.DataFrame(rows)
            )
            for name, rows in self.data.items()
        }

        tables = {
            "KPI Summary": pd.DataFrame(
                [
                    self.analysis[
                        "kpis"
                    ]
                ]
            ),
            "Workflow": pd.DataFrame(
                self.analysis[
                    "workflow"
                ]["conversion"]
            ),
            "Quality": pd.DataFrame(
                [
                    self.analysis[
                        "quality"
                    ]
                ]
            ),
            "SLA": pd.DataFrame(
                [
                    self.analysis[
                        "sla"
                    ]
                ]
            ),
            "Turnaround": pd.DataFrame(
                [
                    self.analysis[
                        "turnaround"
                    ]
                ]
            ),
            "Analysts": pd.DataFrame(
                self.analysis[
                    "comparatives"
                ]["analysts"]
            ),
            "Chemicals": pd.DataFrame(
                self.analysis[
                    "comparatives"
                ]["chemicals"]
            ),
            "Suppliers": pd.DataFrame(
                self.analysis[
                    "comparatives"
                ]["suppliers"]
            ),
            "Anomalies": pd.DataFrame(
                self.analysis[
                    "anomalies"
                ]
            ),
            "Forecast": pd.DataFrame(
                self.analysis[
                    "statistics"
                ].get(
                    "forecast",
                    {},
                ).get(
                    "dates",
                    []
                )
            ),
            "Process Data": frames[
                "processes"
            ],
            "Batch Data": frames[
                "batches"
            ],
            "Result Data": frames[
                "results"
            ],
            "Sampling Data": frames[
                "sampling"
            ],
            "Sample Data": frames[
                "samples"
            ],
            "CoA Data": frames[
                "coas"
            ],
            "Turnaround Data": self._turnaround_df(
                frames["processes"]
            ),
        }

        forecast = self.analysis[
            "statistics"
        ].get(
            "forecast",
            {},
        )

        if forecast:
            tables[
                "Forecast"
            ] = pd.DataFrame(
                {
                    "date": forecast[
                        "dates"
                    ],
                    "forecast_hours": forecast[
                        "values"
                    ],
                }
            )

        correlation = self.analysis[
            "statistics"
        ].get(
            "correlation",
            [],
        )

        tables[
            "Correlation"
        ] = pd.DataFrame(
            correlation
        )

        return {
            name: self._export_clean_dataframe(
                df
            )
            for name, df in tables.items()
        }

    def _summary_dataframe(self):
        kpis = self.analysis[
            "kpis"
        ]

        return pd.DataFrame(
            [
                {
                    "Metric": "Total Batches",
                    "Value": kpis[
                        "batches"
                    ],
                },
                {
                    "Metric": "Total Processes",
                    "Value": kpis[
                        "processes"
                    ],
                },
                {
                    "Metric": "Active Processes",
                    "Value": kpis[
                        "active"
                    ],
                },
                {
                    "Metric": "Overdue Processes",
                    "Value": kpis[
                        "overdue"
                    ],
                },
                {
                    "Metric": "Approved Processes",
                    "Value": kpis[
                        "approved"
                    ],
                },
                {
                    "Metric": "Rejected Processes",
                    "Value": kpis[
                        "rejected"
                    ],
                },
                {
                    "Metric": "Completed Processes",
                    "Value": kpis[
                        "completed"
                    ],
                },
                {
                    "Metric": "Pass Rate %",
                    "Value": kpis[
                        "pass_rate"
                    ],
                },
                {
                    "Metric": "Failure Rate %",
                    "Value": (
                        self.analysis[
                            "quality"
                        ]["failure_rate"]
                    ),
                },
                {
                    "Metric": "Retest Events",
                    "Value": kpis[
                        "retest_events"
                    ],
                },
                {
                    "Metric": "SLA Compliance %",
                    "Value": (
                        self.analysis[
                            "sla"
                        ].get(
                            "rate",
                            0,
                        )
                    ),
                },
                {
                    "Metric": "Mean Turnaround Hours",
                    "Value": (
                        self.analysis[
                            "turnaround"
                        ]["average"]
                    ),
                },
                {
                    "Metric": "Median Turnaround Hours",
                    "Value": (
                        self.analysis[
                            "turnaround"
                        ]["median"]
                    ),
                },
                {
                    "Metric": "P95 Turnaround Hours",
                    "Value": (
                        self.analysis[
                            "turnaround"
                        ]["p95"]
                    ),
                },
            ]
        )

    def _export_clean_dataframe(self, df):
        if df is None:
            return pd.DataFrame()

        df = df.copy()

        for column in df.columns:
            if pd.api.types.is_datetime64_any_dtype(
                df[column]
            ):
                df[column] = (
                    df[column]
                    .dt.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                )

        return df.replace(
            {
                np.nan: "",
                pd.NaT: "",
            }
        )

    def _records(self, value):
        if value is None:
            return []

        if isinstance(
            value,
            pd.DataFrame,
        ):
            value = self._export_clean_dataframe(
                value
            )
            records = value.to_dict(
                orient="records"
            )
        else:
            records = list(value)

        return [
            {
                str(key): self._json(
                    item
                )
                for key, item
                in row.items()
            }
            for row in records
        ]

    def _json(self, value):
        if value is None:
            return None

        if isinstance(
            value,
            (
                pd.Timestamp,
            ),
        ):
            return value.isoformat()

        if isinstance(
            value,
            (
                np.integer,
            ),
        ):
            return int(value)

        if isinstance(
            value,
            (
                np.floating,
            ),
        ):
            return (
                None
                if np.isnan(value)
                else float(value)
            )

        if isinstance(
            value,
            np.bool_,
        ):
            return bool(value)

        try:
            if pd.isna(value):
                return None
        except Exception:
            pass

        return value

    def _figure(self, figure):
        return figure.to_json()

    def _empty_figure(self, title):
        figure = go.Figure()

        figure.update_layout(
            title=title,
            template="plotly_white",
            annotations=[
                {
                    "text": "Insufficient data",
                    "xref": "paper",
                    "yref": "paper",
                    "x": 0.5,
                    "y": 0.5,
                    "showarrow": False,
                }
            ],
        )

        return figure

    def _count(self, df, field, value):
        if (
            df is None
            or df.empty
            or field not in df
        ):
            return 0

        return int(
            (
                df[field] == value
            ).sum()
        )

    def _percent(self, numerator, denominator):
        if not denominator:
            return 0

        return round(
            float(numerator)
            / float(denominator)
            * 100,
            2,
        )

    def _pending_results(self, df):
        if (
            df.empty
            or "status" not in df
        ):
            return 0

        values = df["status"]

        return int(
            values.isna().sum()
            + values.astype(str)
            .str.strip()
            .eq("")
            .sum()
        )

    def _approval_queue(self, df):
        if (
            df.empty
            or "status" not in df
        ):
            return 0

        mask = df["status"].eq(
            "QA"
        )

        if "qa_reviewed_at" in df:
            mask &= df[
                "qa_reviewed_at"
            ].notna()

        return int(mask.sum())

    def _coa_queue(self, df):
        if df.empty:
            return 0

        if "approved_by_id" in df:
            return int(
                df[
                    "approved_by_id"
                ].isna().sum()
            )

        return len(df)

    def _overdue_count(self, df):
        if (
            df.empty
            or "deadline" not in df
        ):
            return 0

        now = self._utc_now_timestamp()

        active = ~df["status"].isin(
            self.TERMINAL_STATUSES
        )

        return int(
            (
                df["deadline"].lt(now)
                & active
            ).sum()
        )

