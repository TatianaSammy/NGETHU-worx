from datetime import timedelta
from decimal import Decimal

from django.apps import apps
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from core.analysis_result_entry import LOCKED_ANALYSIS_STATES, _path_value, build_analysis_entry_groups
from core.dashboard_result_cards import (
    PERIODS,
    _can_enter_results,
    _can_view,
    _resolve_plant_scope,
    _parse_period,
    permission_scoped_sidebar,
)
from core.registry import registry

from .config import APP_LABEL, DASHBOARD_ANALYSIS_SPECS


def _format_value(value):
    if value is None or value == "":
        return "—"
    if isinstance(value, Decimal):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if hasattr(value, "hour") and hasattr(value, "minute") and not hasattr(value, "year"):
        return value.strftime("%H:%M")
    if hasattr(value, "strftime"):
        return value.strftime("%d %b %Y %H:%M" if hasattr(value, "hour") else "%d %b %Y")
    return str(value)


class WorksheetDashboardService:
    @classmethod
    def get_context(cls, request):
        user = request.user
        scope = _resolve_plant_scope(request)
        today = timezone.localdate()
        period_days = _parse_period(request)
        period_start = today - timedelta(days=period_days - 1)
        period_end = today
        spec = DASHBOARD_ANALYSIS_SPECS[0]
        Analysis = apps.get_model(APP_LABEL, spec["model"])
        has_analysis_permission = _can_view(user, Analysis)

        queryset = Analysis.objects.all()
        fields = {field.name for field in Analysis._meta.fields}
        if "is_deleted" in fields:
            queryset = queryset.filter(is_deleted=False)
        queryset = queryset.filter(
            **{
                f"{spec['date_path']}__gte": period_start,
                f"{spec['date_path']}__lte": period_end,
            }
        )
        if spec.get("plant_path"):
            if scope["selected_plant"]:
                queryset = queryset.filter(**{spec["plant_path"]: scope["selected_plant"]})
            elif not user.is_superuser:
                queryset = queryset.none()
        if not has_analysis_permission:
            queryset = queryset.none()

        selected_related = [
            field
            for field in spec.get("select_related", ())
            if field.split("__", 1)[0] in fields
        ]
        analyses = list(
            queryset.select_related(*selected_related)
            .order_by(f"-{spec['date_path']}", "-created_at", "-pk")[:16]
        )

        result_specs = []
        result_models = []
        for row_spec in spec.get("result_specs", ()):
            Result = apps.get_model(row_spec.get("app_label", APP_LABEL), row_spec["model"])
            if _can_view(user, Result) or _can_enter_results(user, Result):
                result_specs.append(row_spec)
                result_models.append(Result)
        entry_spec = {**spec, "result_specs": result_specs}
        can_enter_any = any(_can_enter_results(user, model) for model in result_models)

        candidates = []
        for analysis in analyses:
            status_field = spec.get("status_field", "status")
            status = str(getattr(analysis, status_field, "NEW") or "NEW")
            can_enter = can_enter_any and status.casefold() not in LOCKED_ANALYSIS_STATES
            candidates.append((analysis, status, can_enter))
        candidates.sort(
            key=lambda item: (
                item[2],
                getattr(item[0], spec.get("analysis_datetime_path", "created_at")) or timezone.now(),
            ),
            reverse=True,
        )

        worksheet_cards = []
        for analysis, status, _can_enter in candidates:
            if len(worksheet_cards) >= 4 or not result_specs:
                break
            plant = _path_value(analysis, spec.get("plant_path"))
            groups, can_save_inline = build_analysis_entry_groups(
                request,
                APP_LABEL,
                entry_spec,
                analysis,
                plant,
            )
            if not groups:
                continue
            analyst = _path_value(analysis, spec.get("analyst_path", "analysed_by"))
            entry_url = reverse(
                f"{APP_LABEL}:result-entry",
                kwargs={
                    "analysis_model": Analysis._meta.model_name,
                    "analysis_id": analysis.pk,
                },
            )
            if plant:
                entry_url = f"{entry_url}?plant={plant.pk}"
            status_label = (
                analysis.get_state_display()
                if hasattr(analysis, "get_state_display")
                else status.replace("_", " ").title()
            )
            return_query = request.GET.copy()
            return_query["analysis"] = str(analysis.reference_id)
            return_url = request.path
            if return_query:
                return_url = f"{return_url}?{return_query.urlencode()}"
            return_url = f"{return_url}#assay-panel-{slugify(analysis.reference_id)}"
            worksheet_cards.append({
                "title": spec["title"],
                "reference": _format_value(_path_value(analysis, spec.get("reference_path", "reference_id"))),
                "plant_name": getattr(plant, "name", "—"),
                "plant_id": plant.pk if plant else "",
                "status": status,
                "status_label": status_label,
                "analysis_datetime": _path_value(analysis, spec.get("analysis_datetime_path", "created_at")),
                "analyst": (analyst.get_full_name() or analyst.get_username()) if analyst else "Unassigned",
                "sample_fields": [
                    {
                        "label": label,
                        "value": _format_value(_path_value(analysis, path)),
                    }
                    for label, path in spec.get("sample_fields", ())
                ],
                "entry_groups": groups,
                "can_save_inline": can_save_inline,
                "entry_url": entry_url if any(_can_enter_results(user, model) for model in result_models) else "",
                "detail_url": entry_url,
                "return_url": return_url,
            })

        active_analysis_id = str(request.GET.get("analysis", ""))
        if active_analysis_id not in {str(card["reference"]) for card in worksheet_cards}:
            active_analysis_id = str(worksheet_cards[0]["reference"]) if worksheet_cards else ""

        return {
            "app": {"name": APP_LABEL, "label": "Microbial Analytics"},
            "app_groups": permission_scoped_sidebar(request, registry, APP_LABEL),
            "plant": scope["selected_plant"],
            "available_plants": scope["available_plants"],
            "selected_plant_id": scope["selected_plant_id"],
            "can_select_all_plants": scope["can_select_all"],
            "has_plant_scope": scope["has_plant_scope"],
            "has_analysis_permission": has_analysis_permission,
            "has_result_permission": bool(result_specs),
            "worksheet_cards": worksheet_cards,
            "active_analysis_id": active_analysis_id,
            "period_days": period_days,
            "period_choices": PERIODS,
            "period_start": period_start,
            "period_end": period_end,
            "period_label": f"{period_start:%d %b %Y} – {period_end:%d %b %Y}",
            "dashboard_return_url": request.get_full_path(),
            "last_refreshed": timezone.localtime(),
        }
