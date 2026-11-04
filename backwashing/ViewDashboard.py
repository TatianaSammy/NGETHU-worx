from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone
from django.views import View

from core.domain_dashboard import dashboard_context, model_list_url, period_days, render_domain_dashboard
from core.mixins import WanjalaMixin
from t.models import TreatmentPlant
from .models import Backwashing, Disludging, OperationStatus, PhaseLossRate, PumpFlush


OPERATION_MODELS = (
    ("Backwashing", Backwashing, "filter", "backwash"),
    ("Desludging", Disludging, "broom", "desludge"),
    ("Pump flushing", PumpFlush, "pump", "flush"),
)


class DashboardView(WanjalaMixin, View):
    template_name = "backwashing/dashboard.html"
    app_label = "backwashing"

    def get(self, request):
        if response := self.process_request(request):
            return response
        self.check_idle_timeout()
        return render_domain_dashboard(request, self.template_name, self.app_label, self.get_context(request))

    def get_context(self, request):
        days = period_days(request)
        now = timezone.now()
        today = timezone.localdate()
        start = now - timedelta(days=days)
        plants, selected, is_superuser = self._plants(request)
        operation_rows = []
        operation_counts = {}
        status_counts = Counter()
        daily_counts = defaultdict(int)
        daily_loss = defaultdict(Decimal)
        total_loss = Decimal("0")
        today_count = 0
        completed = 0
        duration_values = []
        recent_records = []
        table_rows = []
        for label, model, icon, key in OPERATION_MODELS:
            qs = model.objects.select_related("treatment_plant", "phase", "performed_by", "status")
            if selected:
                qs = qs.filter(treatment_plant=selected)
            elif not is_superuser:
                qs = qs.none()
            qs = qs.filter(start_time__gte=start, start_time__lte=now)
            operation_counts[label] = qs.count()
            for row in qs.iterator(chunk_size=1000):
                operation_rows.append((row.start_time, label, row, icon, key))
                status_name = row.status.name if row.status else "unknown"
                status_counts[status_name] += 1
                loss = row.water_lost_m3 or Decimal("0")
                total_loss += loss
                daily_counts[row.start_time.date()] += 1
                daily_loss[row.start_time.date()] += loss
                if row.start_time.date() == today:
                    today_count += 1
                if status_name.casefold() == "completed":
                    completed += 1
                duration = row.duration_minutes
                if duration is not None and duration >= 0:
                    duration_values.append(duration)
                target = getattr(row, "filter_unit", None) or getattr(row, "clarifier", None) or getattr(row, "chemical_pump", None)
                operator = row.performed_by.get_full_name() if row.performed_by else "Operator not recorded"
                recent_records.append({
                    "title": f"{label} · {target or 'Asset not recorded'}",
                    "detail": f"{status_name.replace('_', ' ').title()} · {loss:,.3f} m³ · {operator}",
                    "timestamp": timezone.localtime(row.start_time).strftime("%d %b %H:%M"),
                    "icon": icon,
                    "color": "emerald" if status_name.casefold() == "completed" else "amber" if status_name.casefold() in {"scheduled", "in_progress"} else "rose",
                })
                table_rows.append((row.start_time, {"cells": [label, str(target or "—"), str(row.treatment_plant), str(row.phase or "—"), status_name.replace("_", " ").title(), f"{duration} min" if duration is not None else "In progress", f"{loss:,.3f}", operator, timezone.localtime(row.start_time).strftime("%d %b %Y %H:%M")]}))

        operation_rows.sort(key=lambda item: item[0], reverse=True)
        recent_records = sorted(recent_records, key=lambda item: item["timestamp"], reverse=True)[:12]
        table_rows = [row for _date, row in sorted(table_rows, key=lambda row: row[0], reverse=True)[:80]]
        total_ops = sum(operation_counts.values())
        completion_rate = round(completed * 100 / total_ops, 1) if total_ops else None
        average_duration = round(sum(duration_values) / len(duration_values), 1) if duration_values else None
        labels = [(today - timedelta(days=offset)).strftime("%d %b") for offset in range(days - 1, -1, -1)]
        dates = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
        if days > 90:
            weekly_count = defaultdict(int)
            weekly_loss = defaultdict(Decimal)
            for date, count in daily_counts.items():
                bucket = date - timedelta(days=date.weekday())
                weekly_count[bucket] += count
            for date, loss in daily_loss.items():
                bucket = date - timedelta(days=date.weekday())
                weekly_loss[bucket] += loss
            dates = sorted(weekly_count.keys() | weekly_loss.keys())
            labels = [date.strftime("%d %b") for date in dates]
            activity_values = [weekly_count[date] for date in dates]
            loss_values = [float(weekly_loss[date]) for date in dates]
        else:
            activity_values = [daily_counts[date] for date in dates]
            loss_values = [float(daily_loss[date]) for date in dates]

        models_for_scope = []
        for label, model, icon, group in [*[(name, cls, icon, "Operations") for name, cls, icon, _key in OPERATION_MODELS], ("Operation statuses", OperationStatus, "circle-dot", "Workflow state"), ("Phase loss rates", PhaseLossRate, "droplet", "Water-loss factors")]:
            qs = model.objects.all()
            if model in {Backwashing, Disludging, PumpFlush}:
                if selected:
                    qs = qs.filter(treatment_plant=selected)
                elif not is_superuser:
                    qs = qs.none()
            elif model is PhaseLossRate:
                if selected:
                    qs = qs.filter(phase__treatment_plant=selected)
                elif not is_superuser:
                    qs = qs.none()
            models_for_scope.append({"label": label, "count": qs.count(), "url": model_list_url(self.app_label, model._meta.model_name), "icon": icon, "group": group})
        total_operation_volume = max(total_ops, 1)
        ranked_rows = [{"label": label, "detail": "Recorded operation events", "value": count, "percent": round(count * 100 / total_operation_volume), "color": color} for label, count, color in [("Backwashing", operation_counts.get("Backwashing", 0), "cyan"), ("Desludging", operation_counts.get("Desludging", 0), "purple"), ("Pump flushing", operation_counts.get("Pump flushing", 0), "blue")]]
        kpis = [
            {"label": "Operations", "value": total_ops, "detail": f"Last {days} days · all operation types", "icon": "arrows-rotate", "color": "cyan", "tab": "records"},
            {"label": "Water loss", "value": f"{total_loss:,.1f} m³", "detail": "Sum of saved operation loss values", "icon": "droplet", "color": "rose", "tab": "analytics"},
            {"label": "Today", "value": today_count, "detail": "Operations started today", "icon": "calendar-day", "color": "emerald", "tab": "records"},
            {"label": "Completed", "value": f"{completion_rate}%" if completion_rate is not None else "—", "detail": f"{completed} operation records completed", "icon": "circle-check", "color": "emerald", "tab": "analytics"},
            {"label": "Mean duration", "value": f"{average_duration} min" if average_duration is not None else "—", "detail": "Closed operations with usable duration", "icon": "stopwatch", "color": "blue", "tab": "analytics"},
            {"label": "Scheduled / active", "value": status_counts.get("scheduled", 0) + status_counts.get("in_progress", 0), "detail": "Operation records not yet completed", "icon": "hourglass-half", "color": "amber", "tab": "records"},
        ]
        context = dashboard_context(
            request,
            title="Backwashing & Process Maintenance",
            subtitle="Backwash, clarifier desludging, pump flushing, operation duration, and water-loss performance.",
            kicker="Process maintenance analytics",
            kpis=kpis,
            charts=[
                {"id": "backwash-activity", "title": "Operation cadence", "caption": "All maintenance operation records by start date.", "type": "line", "labels": labels, "values": activity_values, "color": "cyan"},
                {"id": "backwash-loss", "title": "Water-loss profile", "caption": "Recorded and model-calculated water loss in m³.", "type": "line", "labels": labels, "values": loss_values, "color": "rose"},
                {"id": "backwash-operation-mix", "title": "Operation mix", "caption": "Backwash, desludge, and flushing event counts.", "type": "doughnut", "labels": list(operation_counts.keys()), "values": list(operation_counts.values()), "colors": ["cyan", "purple", "blue"]},
                {"id": "backwash-status", "title": "Workflow status", "caption": "Current status distribution for records in the window.", "type": "bar", "labels": [value.replace("_", " ").title() for value in status_counts.keys()], "values": list(status_counts.values()), "color": "amber"},
            ],
            signals=[
                {"label": "Backwashing", "value": operation_counts.get("Backwashing", 0), "detail": "Filter-cleaning cycles", "icon": "filter", "color": "cyan"},
                {"label": "Desludging", "value": operation_counts.get("Desludging", 0), "detail": "Clarifier solids removal", "icon": "broom", "color": "purple"},
                {"label": "Pump flushing", "value": operation_counts.get("Pump flushing", 0), "detail": "Chemical pump flushes", "icon": "pump", "color": "blue"},
                {"label": "In progress", "value": status_counts.get("in_progress", 0), "detail": "No completed duration yet", "icon": "hourglass-half", "color": "amber"},
            ],
            recent_records=recent_records,
            model_rows=models_for_scope,
            table_headings=["Operation", "Asset", "Plant", "Phase", "Status", "Duration", "Water loss m³", "Operator", "Started"],
            table_rows=table_rows,
            ranked_rows=ranked_rows,
            actions=[{"label": "Filter fleet", "url": "/filtered_water/", "icon": "filter"}],
            overview_caption="Water-loss and duration indicators use stored operation values and configured phase loss rates.",
            use_plant_scope=True,
            model_count=len(models_for_scope),
        )
        context.update({"plant_options": plants, "selected_plant": selected, "show_plant_selector": is_superuser})
        return context

    @staticmethod
    def _plants(request):
        user = request.user
        if user.is_superuser:
            plants = TreatmentPlant.objects.order_by("name")
            requested = request.GET.get("plant")
            return plants, plants.filter(pk=requested).first() if requested and requested.isdigit() else None, True
        plant_id = getattr(user, "assigned_treatment_plant_id", None)
        plants = TreatmentPlant.objects.filter(pk=plant_id) if plant_id else TreatmentPlant.objects.none()
        return plants.order_by("name"), plants.first(), False
