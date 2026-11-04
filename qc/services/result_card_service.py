from __future__ import annotations

import logging

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Type

from django.apps import apps
from django.db.models import QuerySet
from django.utils import timezone

from qc.models import (
    SamplingSchedule,
    SamplingTime,
    WaterAnalysis,
)


logger = logging.getLogger(__name__)


WATER_CONFIG = {
    SamplingSchedule.RAW: {
        "model": "RawWaterResult",
        "title": "RAW WATER",
        "order": 1,
    },
    SamplingSchedule.COAGULATED: {
        "model": "CoagulatedWaterResult",
        "title": "COAGULATED WATER",
        "order": 2,
    },
    SamplingSchedule.CLARIFIED: {
        "model": "ClarifiedWaterResult",
        "title": "CLARIFIED WATER",
        "order": 3,
    },
    SamplingSchedule.TREATED: {
        "model": "TreatedWaterResult",
        "title": "TREATED WATER",
        "order": 4,
    },
}


@dataclass(slots=True)
class ResultCell:

    result: Any

    parameter: str

    unit: str

    value: Optional[Decimal]

    minimum: Optional[Decimal]

    maximum: Optional[Decimal]

    entered: bool

    abnormal: bool

    display_value: str

    display_limit: str


@dataclass(slots=True)
class ResultRow:

    name: str

    results: Dict[str, ResultCell] = field(
        default_factory=dict
    )


@dataclass(slots=True)
class ResultGroup:

    title: str

    water_type: str

    rows: List[ResultRow] = field(
        default_factory=list
    )

    parameters: List[dict] = field(
        default_factory=list
    )

    total_cells: int = 0

    entered_cells: int = 0

    is_completed: bool = False


@dataclass(slots=True)
class AnalysisCard:

    sampling_time: SamplingTime

    time_label: str

    groups: List[ResultGroup]


class ResultCardService:

    _result_cache: Dict[str, Type] = {}

    @classmethod
    def get_result_model(
        cls,
        water_type: str,
    ) -> Optional[Type]:

        if water_type in cls._result_cache:
            return cls._result_cache[water_type]

        config = WATER_CONFIG.get(
            water_type
        )

        if not config:
            return None

        model = apps.get_model(
            "qc",
            config["model"],
        )

        if model:
            cls._result_cache[water_type] = model

        return model


    @classmethod
    def get_active_analyses(
        cls,
        *,
        treatment_plant,
        selected_date=None,
    ) -> QuerySet:

        analysis_date = (
            selected_date
            or timezone.localdate()
        )

        return (
            WaterAnalysis.objects
            .filter(
                treatment_plant=treatment_plant,
                analysis_date=analysis_date,
            )
            .select_related(
                "analysis_time",
                "analysis_time__schedule",
                "shift",
                "treatment_plant",
            )
            .order_by(
                "-analysis_time__time",
                "analysis_time__schedule__water_type",
            )
        )


    @classmethod
    def preload_results(
        cls,
        analyses: Iterable[WaterAnalysis],
    ) -> Dict[int, list]:

        result_map = defaultdict(list)

        grouped = defaultdict(list)

        for analysis in analyses:

            model = cls.get_result_model(
                analysis.water_type
            )

            if model:
                grouped[model].append(
                    analysis.id
                )


        for model, analysis_ids in grouped.items():

            results = (
                model.objects
                .filter(
                    analysis_id__in=analysis_ids
                )
                .select_related(
                    "analysis",
                    "parameter",
                    "parameter__parameter",
                    "phase",
                )
                .order_by(
                    "phase__name",
                    "parameter__parameter__name",
                )
            )

            for result in results:

                result_map[
                    result.analysis_id
                ].append(
                    result
                )

        return result_map
    @classmethod
    def build_time_card(
        cls,
        analyses: List[WaterAnalysis],
        result_map: Dict[int, list],
        plant_has_phases: bool,
    ) -> Optional[AnalysisCard]:

        if not analyses:
            return None

        sampling_time = analyses[0].analysis_time

        groups = []

        for analysis in analyses:

            group = cls.build_group(
                analysis=analysis,
                results=result_map.get(
                    analysis.id,
                    [],
                ),
                plant_has_phases=plant_has_phases,
            )

            if group:
                groups.append(group)

        groups.sort(
            key=lambda group:
            WATER_CONFIG.get(
                group.water_type,
                {},
            ).get(
                "order",
                99,
            )
        )

        if not groups:
            return None

        return AnalysisCard(
            sampling_time=sampling_time,
            time_label=sampling_time.time.strftime(
                "%H:%M"
            ),
            groups=groups,
        )


    @classmethod
    def build_group(
        cls,
        *,
        analysis: WaterAnalysis,
        results: List[Any],
        plant_has_phases: bool,
    ) -> Optional[ResultGroup]:

        title = cls.water_title(
            analysis.water_type
        )

        rows = defaultdict(
            lambda: ResultRow(
                name=""
            )
        )

        for result in results:

            parameter = cls.resolve_parameter(
                result
            )

            if not parameter:
                continue

            row_name = cls.resolve_row_name(
                analysis,
                result,
                plant_has_phases,
            )

            row = rows[row_name]

            row.name = row_name

            row.results[
                parameter.name
            ] = cls.make_cell(
                result
            )

        if not rows:

            rows[title] = ResultRow(
                name=title
            )

        row_list = list(rows.values())
        param_list = cls.extract_parameters(results)
        total_cells = 0
        entered_cells = 0
        for r in row_list:
            for cell in r.results.values():
                total_cells += 1
                if cell.entered:
                    entered_cells += 1
        is_completed = (total_cells > 0 and entered_cells == total_cells)

        return ResultGroup(
            title=title,
            water_type=analysis.water_type,
            rows=row_list,
            parameters=param_list,
            total_cells=total_cells,
            entered_cells=entered_cells,
            is_completed=is_completed,
        )


    @staticmethod
    def water_title(
        water_type: str,
    ) -> str:

        return (
            WATER_CONFIG
            .get(
                water_type,
                {},
            )
            .get(
                "title",
                str(water_type).upper(),
            )
        )


    @staticmethod
    def resolve_parameter(
        result,
    ):

        parameter_limit = getattr(
            result,
            "parameter",
            None,
        )

        if not parameter_limit:
            return None

        return getattr(
            parameter_limit,
            "parameter",
            None,
        )


    @classmethod
    def resolve_row_name(
        cls,
        analysis,
        result,
        plant_has_phases: bool,
    ):

        if (
            plant_has_phases
            and result.phase_id
            and result.phase
        ):
            return result.phase.name

        return cls.water_title(
            analysis.water_type
        )


    @staticmethod
    def extract_parameters(
        results: List[Any],
    ) -> List[dict]:

        parameters = {}

        for result in results:

            parameter = ResultCardService.resolve_parameter(
                result
            )

            if not parameter:
                continue

            parameters[
                parameter.pk
            ] = {
                "id": parameter.pk,
                "name": parameter.name,
                "unit": getattr(
                    parameter,
                    "unit",
                    "",
                ),
                "order": getattr(
                    parameter,
                    "display_order",
                    0,
                ),
            }

        return sorted(
            parameters.values(),
            key=lambda item: item["order"],
        )


    @classmethod
    def make_cell(
        cls,
        result,
    ) -> ResultCell:

        parameter_limit = getattr(
            result,
            "parameter",
            None,
        )

        parameter = cls.resolve_parameter(
            result
        )

        value = getattr(
            result,
            "value",
            None,
        )

        minimum = getattr(
            parameter_limit,
            "min_value",
            None,
        )

        maximum = getattr(
            parameter_limit,
            "max_value",
            None,
        )

        abnormal = cls.is_abnormal(
            result=result,
            value=value,
            minimum=minimum,
            maximum=maximum,
        )

        return ResultCell(
            result=result,
            parameter=(
                parameter.name
                if parameter
                else ""
            ),
            unit=(
                getattr(
                    parameter,
                    "unit",
                    "",
                )
                if parameter
                else ""
            ),
            value=value,
            minimum=minimum,
            maximum=maximum,
            entered=value is not None,
            abnormal=abnormal,
            display_value=cls.display_value(
                value
            ),
            display_limit=cls.limit_text(
                minimum,
                maximum,
            ),
        )
    @staticmethod
    def is_abnormal(
        *,
        result,
        value,
        minimum,
        maximum,
    ) -> bool:

        stored_status = getattr(
            result,
            "is_abnormal",
            None,
        )

        if stored_status is not None:
            return stored_status

        exceeds_limit = getattr(
            result,
            "exceeds_limit",
            None,
        )

        if exceeds_limit is not None:
            return exceeds_limit

        if value is None:
            return False

        if minimum is not None and value < minimum:
            return True

        if maximum is not None and value > maximum:
            return True

        return False


    @staticmethod
    def display_value(
        value,
    ) -> str:

        if value is None:
            return "—"

        return str(value)


    @staticmethod
    def limit_text(
        minimum,
        maximum,
    ) -> str:

        if (
            minimum is None
            and maximum is None
        ):
            return ""

        if minimum is None:
            return f"≤ {maximum}"

        if maximum is None:
            return f"≥ {minimum}"

        return f"{minimum} – {maximum}"

    @classmethod
    def get_context(
            cls,
            *,
            treatment_plant,
            selected_date=None,
    ):

        generated_at = timezone.localtime()

        # --------------------------------------------------
        # No plant assigned
        # --------------------------------------------------

        if not treatment_plant:
            return {
                "cards": [],
                "plant": None,
                "generated_at": generated_at,
                "current_only": True,
                "raw_water_snapshot": {
                    "available": False,
                    "time_label": None,
                    "analysis_id": None,
                    "parameters": [],
                    "abnormal_count": 0,
                    "entered_count": 0,
                    "total_count": 0,
                },
                "current_analysis": None,
                "current_time_label": None,
                "analysis_count": 0,
                "analysis_watermark": 0,
                "completion": {
                    "total": 0,
                    "entered": 0,
                    "pending": 0,
                    "abnormal": 0,
                    "percent": 0,
                },
            }

        # --------------------------------------------------
        # Load today's analyses
        # --------------------------------------------------

        analyses = list(
            cls.get_active_analyses(
                treatment_plant=treatment_plant,
                selected_date=selected_date,
            )
        )

        # --------------------------------------------------
        # No scheduled analyses - lazy auto-generate if today
        # --------------------------------------------------

        if not analyses:
            if selected_date is None or selected_date == timezone.localdate():
                from qc.services.analysis_generator import AnalysisGenerator
                AnalysisGenerator.generate_due_analyses(
                    plant_ids=[treatment_plant.pk] if hasattr(treatment_plant, "pk") else None
                )
                analyses = list(
                    cls.get_active_analyses(
                        treatment_plant=treatment_plant,
                        selected_date=selected_date,
                    )
                )

        if not analyses:
            return {
                "cards": [],
                "plant": treatment_plant,
                "generated_at": generated_at,
                "current_only": True,
                "raw_water_snapshot": {
                    "available": False,
                    "time_label": None,
                    "analysis_id": None,
                    "parameters": [],
                    "abnormal_count": 0,
                    "entered_count": 0,
                    "total_count": 0,
                },
                "current_analysis": None,
                "current_time_label": None,
                "analysis_count": 0,
                "analysis_watermark": max(
                    (analysis.pk for analysis in analyses),
                    default=0,
                ),
                "completion": {
                    "total": 0,
                    "entered": 0,
                    "pending": 0,
                    "abnormal": 0,
                    "percent": 0,
                },
            }

        # --------------------------------------------------
        # Load all results efficiently
        # --------------------------------------------------

        result_map = cls.preload_results(
            analyses
        )

        # --------------------------------------------------
        # Determine plant phase support
        # --------------------------------------------------

        plant_has_phases = (
            treatment_plant
            .phases
            .exists()
        )

        # ==================================================
        # CURRENT ANALYSIS ONLY (Strict Active Analysis)
        # Result cards stay visible continuously until the next analysis.
        # ==================================================
        from qc.services.analysis_generator import AnalysisGenerator

        current_analysis = AnalysisGenerator.get_current_analysis(
            treatment_plant=treatment_plant
        )

        current_analyses = []
        if current_analysis and current_analysis.analysis_time:
            current_sampling_time = current_analysis.analysis_time.time
            current_analyses = [
                a for a in analyses
                if a.analysis_time.time == current_sampling_time
            ]
        elif analyses:
            latest_time = max(a.analysis_time.time for a in analyses)
            current_analyses = [
                a for a in analyses
                if a.analysis_time.time == latest_time
            ]
            current_analysis = current_analyses[0]

        card = cls.build_time_card(
            analyses=current_analyses,
            result_map=result_map,
            plant_has_phases=plant_has_phases,
        )

        cards = [card] if card else []
        completion = cls.compute_completion(cards)
        is_completely_filled = (
            completion["total"] > 0
            and completion["pending"] == 0
        )
        has_expired = False
        dismiss_countdown = None

        # --------------------------------------------------
        # Return dashboard context
        # --------------------------------------------------

        raw_snapshot = cls.get_raw_water_snapshot(
            treatment_plant=treatment_plant,
            analyses=current_analyses,
            result_map=result_map,
        )

        current_analysis = (
            current_analyses[0]
            if current_analyses
            else None
        )

        return {
            "cards": cards,
            "plant": treatment_plant,
            "generated_at": generated_at,
            "current_only": True,
            "raw_water_snapshot": raw_snapshot,
            "current_analysis": current_analysis,
            "current_time_label": (
                cards[0].time_label if cards else None
            ),
            "analysis_count": len(current_analyses),
            "analysis_watermark": max(
                (analysis.pk for analysis in analyses),
                default=0,
            ),
            "completion": completion,
            "is_completely_filled": is_completely_filled,
            "has_expired": has_expired,
            "dismiss_countdown": dismiss_countdown,
        }

    @classmethod
    def get_raw_water_snapshot(
        cls,
        *,
        treatment_plant,
        analyses=None,
        result_map=None,
    ):
        """
        Latest raw-water parameter values for the live topbar.
        Prefers the current sampling cycle; falls back to the most
        recent raw analysis for the selected day.
        """
        if analyses is None:
            analyses = list(
                cls.get_active_analyses(
                    treatment_plant=treatment_plant,
                )
            )

        raw_analyses = [
            analysis
            for analysis in analyses
            if analysis.water_type == SamplingSchedule.RAW
        ]

        if not raw_analyses:
            return {
                "available": False,
                "time_label": None,
                "analysis_id": None,
                "parameters": [],
                "abnormal_count": 0,
                "entered_count": 0,
                "total_count": 0,
            }

        latest_time = max(
            analysis.analysis_time.time
            for analysis in raw_analyses
        )

        current_raw = [
            analysis
            for analysis in raw_analyses
            if analysis.analysis_time.time == latest_time
        ]

        if result_map is None:
            result_map = cls.preload_results(current_raw)

        parameters = []
        abnormal_count = 0
        entered_count = 0

        for analysis in current_raw:
            for result in result_map.get(analysis.id, []):
                parameter = cls.resolve_parameter(result)
                if not parameter:
                    continue

                cell = cls.make_cell(result)
                if cell.entered:
                    entered_count += 1
                if cell.abnormal:
                    abnormal_count += 1

                parameters.append(
                    {
                        "id": parameter.pk,
                        "name": parameter.name,
                        "unit": cell.unit or "",
                        "value": cell.display_value,
                        "raw_value": (
                            float(cell.value)
                            if cell.value is not None
                            else None
                        ),
                        "limit": cell.display_limit,
                        "entered": cell.entered,
                        "abnormal": cell.abnormal,
                        "phase": (
                            result.phase.name
                            if getattr(result, "phase", None)
                            else None
                        ),
                    }
                )

        parameters.sort(
            key=lambda item: (
                0 if item["abnormal"] else 1,
                item["name"].lower(),
            )
        )

        return {
            "available": bool(parameters),
            "time_label": latest_time.strftime("%H:%M"),
            "analysis_id": current_raw[0].pk,
            "parameters": parameters,
            "abnormal_count": abnormal_count,
            "entered_count": entered_count,
            "total_count": len(parameters),
        }

    @staticmethod
    def compute_completion(cards):
        total = 0
        entered = 0
        abnormal = 0

        for card in cards:
            for group in card.groups:
                for row in group.rows:
                    for cell in row.results.values():
                        total += 1
                        if cell.entered:
                            entered += 1
                        if cell.abnormal:
                            abnormal += 1

        percent = (
            round((entered / total) * 100)
            if total
            else 0
        )

        return {
            "total": total,
            "entered": entered,
            "pending": max(total - entered, 0),
            "abnormal": abnormal,
            "percent": percent,
        }

    @classmethod
    def serialize_live_payload(cls, context):
        raw = context.get("raw_water_snapshot") or {}
        completion = context.get("completion") or {}
        cards = context.get("cards") or []
        plant = context.get("plant")
        current = context.get("current_analysis")

        return {
            "ok": True,
            "generated_at": (
                context.get("generated_at").isoformat()
                if context.get("generated_at")
                else timezone.localtime().isoformat()
            ),
            "plant": {
                "id": getattr(plant, "pk", None),
                "name": getattr(plant, "name", None),
            },
            "current_time_label": context.get("current_time_label"),
            "analysis_id": getattr(current, "pk", None),
            "analysis_count": context.get("analysis_count", 0),
            "analysis_watermark": context.get("analysis_watermark", 0),
            "completion": completion,
            "raw_water": raw,
            "current_dosages": context.get("current_dosages") or [],
            "phase_concentrations": context.get("phase_concentrations") or [],
            "card_count": len(cards),
            "fingerprint": cls.build_fingerprint(context),
        }

    @classmethod
    def build_fingerprint(cls, context):
        raw = context.get("raw_water_snapshot") or {}
        completion = context.get("completion") or {}
        parts = [
            str(context.get("current_time_label") or ""),
            str(getattr(context.get("current_analysis"), "pk", "") or ""),
            str(context.get("analysis_watermark", 0)),
            str(completion.get("entered", 0)),
            str(completion.get("total", 0)),
            str(completion.get("abnormal", 0)),
            str(raw.get("time_label") or ""),
            str(raw.get("entered_count", 0)),
            str(raw.get("abnormal_count", 0)),
        ]
        for item in raw.get("parameters") or []:
            parts.append(
                f"{item.get('id')}:{item.get('value')}:{int(bool(item.get('abnormal')))}"
            )
        for dosage in context.get("current_dosages") or []:
            parts.append(
                f"dose:{dosage.get('label')}:{dosage.get('value_display')}:{dosage.get('detail')}"
            )
        for phase in context.get("phase_concentrations") or []:
            parts.append(f"phase:{phase.get('id')}:{phase.get('name')}")
            for item in phase.get("items") or []:
                parts.append(
                    f"concentration:{item.get('chemical')}:{item.get('tank')}:{item.get('value_display')}"
                )
        return "|".join(parts)

    @classmethod
    def for_request(
        cls,
        request,
    ):

        plant = getattr(
            request.user,
            "assigned_treatment_plant",
            None,
        )

        return cls.get_context(
            treatment_plant=plant,
        )
