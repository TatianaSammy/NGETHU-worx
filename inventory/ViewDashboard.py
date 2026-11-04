from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db.models import (
    Case,
    Count,
    DecimalField,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, TruncDate
from django.shortcuts import render
from django.utils import timezone
from django.views import View

from core.mixins import WanjalaMixin
from core.models import Section
from core.registry import registry
from t.models import TreatmentPlant

from .models import (
    AlertConfig,
    AuditLog,
    Category,
    Equipment,
    InventoryItem,
    InventoryTransaction,
    ItemType,
    LaboratoryReagent,
    Status,
    StockLedger,
    WorkMaterialReservation,
)


class DashboardView(WanjalaMixin, View):
    template_name = "inventory/dashboard.html"
    app_label = "inventory"
    periods = (7, 30, 90, 365)
    quantity_field = DecimalField(max_digits=14, decimal_places=2)

    def get(self, request):
        response = self.process_request(request)
        if response:
            return response

        self.check_idle_timeout()
        return render(request, self.template_name, self.get_context(request))

    def _scope(self, request):
        user = request.user
        is_superuser = bool(user.is_superuser)
        if is_superuser:
            plants = TreatmentPlant.objects.all().order_by("name")
        else:
            plants = TreatmentPlant.objects.filter(assigned_users=user).order_by("name")
            assigned_id = getattr(user, "assigned_treatment_plant_id", None)
            if assigned_id and not plants.filter(pk=assigned_id).exists():
                plants = TreatmentPlant.objects.filter(pk=assigned_id)

        plant_options = list(plants.values("id", "name"))
        requested_plant = request.GET.get("plant", "")
        selected_plant = None
        section_scope = Section.objects.none()
        scope_label = "No inventory scope assigned"

        if is_superuser:
            if requested_plant and requested_plant != "all" and requested_plant.isdigit():
                selected_plant = plants.filter(pk=int(requested_plant)).first()
            if selected_plant:
                section_scope = Section.objects.filter(treatment_plant=selected_plant)
                scope_label = selected_plant.name
            else:
                section_scope = Section.objects.all()
                scope_label = "All treatment plants"
        elif plant_options:
            if requested_plant.isdigit():
                selected_plant = plants.filter(pk=int(requested_plant)).first()
            if selected_plant is None:
                assigned_id = getattr(user, "assigned_treatment_plant_id", None)
                selected_plant = plants.filter(pk=assigned_id).first() if assigned_id else None
            if selected_plant is None:
                selected_plant = plants.first()
            section_scope = Section.objects.filter(treatment_plant=selected_plant)
            scope_label = selected_plant.name if selected_plant else "No inventory scope assigned"
        elif getattr(user, "section_id", None):
            section = Section.objects.filter(pk=user.section_id).select_related("treatment_plant").first()
            if section:
                section_scope = Section.objects.filter(pk=section.pk)
                selected_plant = section.treatment_plant
                scope_label = f"{section.name} · {section.treatment_plant.name}" if section.treatment_plant_id else section.name

        return {
            "plants": plant_options,
            "selected_plant": selected_plant,
            "selected_plant_id": str(selected_plant.pk) if selected_plant else "all",
            "section_scope": section_scope,
            "scope_label": scope_label,
            "can_select_all": is_superuser,
            "has_scope": is_superuser or section_scope.exists(),
        }

    def _configured_rules(self, alert_type):
        matching_rules = AlertConfig.objects.filter(
            active=True,
            alert_type=alert_type,
            section_id=OuterRef("section_id"),
        ).filter(
            Q(category_id=OuterRef("category_id")) | Q(category__isnull=True)
        )
        matching_rules = matching_rules.annotate(
            _match_priority=Case(
                When(category__isnull=False, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        ).order_by("_match_priority", "pk")
        return matching_rules

    def _model_cards(self):
        cards = []
        for entry in registry.for_app(self.app_label) or []:
            meta = entry.meta
            model_name = meta.get("model_name")
            model_label = meta.get("verbose_name_plural") or meta.get("verbose_name") or model_name
            if not model_name:
                continue
            cards.append({
                "key": meta.get("key"),
                "label": str(model_label).replace("_", " ").title(),
                "model_name": model_name,
                "icon": meta.get("icon", "cube"),
                "group": meta.get("group", "inventory"),
                "capabilities": {
                    "create": bool(meta.get("has_create")),
                    "export": bool(meta.get("has_export")),
                },
                "urls": {
                    "list": registry.url(model_name, "list", self.app_label),
                    "create": registry.url(model_name, "create", self.app_label),
                    "export": registry.url(model_name, "export", self.app_label),
                },
            })
        return cards

    def get_context(self, request):
        scope = self._scope(request)
        today = timezone.localdate()
        try:
            period_days = int(request.GET.get("period", 30))
        except (TypeError, ValueError):
            period_days = 30
        if period_days not in self.periods:
            period_days = 30

        start_date = today - timedelta(days=period_days - 1)
        due_through = today + timedelta(days=period_days)
        sections = scope["section_scope"]

        items = InventoryItem.objects.filter(section__in=sections).select_related(
            "item_type", "category", "section", "supplier"
        )
        restock_rules = self._configured_rules("restock")
        configured_restock_limit = Subquery(
            restock_rules.values("min_quantity")[:1],
            output_field=self.quantity_field,
        )
        available_stock = Coalesce(
            F("ledger__available"), F("quantity"), Value(Decimal("0.00")),
            output_field=self.quantity_field,
        )
        annotated_items = items.annotate(
            available_stock=available_stock,
            restock_rule_configured=Exists(restock_rules.values("pk")[:1]),
            restock_limit=Coalesce(
                configured_restock_limit,
                Value(Decimal("0.00"), output_field=self.quantity_field),
                output_field=self.quantity_field,
            ),
        )
        stock_summary = annotated_items.aggregate(
            available=Sum("available_stock"),
            item_count=Count("pk"),
            locked_items=Count("pk", filter=Q(is_locked=True)),
            low_stock=Count("pk", filter=Q(restock_rule_configured=True, available_stock__lte=F("restock_limit"))),
            unconfigured=Count("pk", filter=Q(restock_rule_configured=False)),
        )
        item_count = stock_summary["item_count"] or 0
        available_total = stock_summary["available"] or Decimal("0.00")
        low_stock_count = stock_summary["low_stock"] or 0
        unconfigured_restock_count = stock_summary["unconfigured"] or 0
        locked_item_count = stock_summary["locked_items"] or 0
        low_stock_items = annotated_items.filter(
            restock_rule_configured=True,
            available_stock__lte=F("restock_limit"),
        )

        expiry_horizon = today + timedelta(days=30)
        expiry_summary = items.aggregate(
            expired=Count("pk", filter=Q(expiry_date__lt=today)),
            expiring=Count("pk", filter=Q(expiry_date__gte=today, expiry_date__lte=expiry_horizon)),
        )
        expired_count = expiry_summary["expired"] or 0
        expiring_count = expiry_summary["expiring"] or 0

        equipment = Equipment.objects.filter(section__in=sections).select_related(
            "category", "section"
        )
        maintenance_overdue_filter = Q(next_maintenance_date__lt=today)
        calibration_overdue_filter = Q(next_calibration_date__lt=today)
        maintenance_due_filter = Q(next_maintenance_date__gte=today, next_maintenance_date__lte=due_through)
        calibration_due_filter = Q(next_calibration_date__gte=today, next_calibration_date__lte=due_through)
        tracked_equipment_filter = Q(next_maintenance_date__isnull=False) | Q(next_calibration_date__isnull=False)
        equipment_summary = equipment.aggregate(
            total=Count("pk"),
            maintenance_overdue=Count("pk", filter=maintenance_overdue_filter),
            calibration_overdue=Count("pk", filter=calibration_overdue_filter),
            maintenance_due=Count("pk", filter=maintenance_due_filter),
            calibration_due=Count("pk", filter=calibration_due_filter),
            current=Count("pk", filter=tracked_equipment_filter & ~(maintenance_overdue_filter | calibration_overdue_filter)),
            attention=Count("pk", filter=maintenance_overdue_filter | calibration_overdue_filter | maintenance_due_filter | calibration_due_filter),
        )
        equipment_count = equipment_summary["total"] or 0
        maintenance_overdue = equipment_summary["maintenance_overdue"] or 0
        calibration_overdue = equipment_summary["calibration_overdue"] or 0
        maintenance_due = equipment_summary["maintenance_due"] or 0
        calibration_due = equipment_summary["calibration_due"] or 0
        equipment_current = equipment_summary["current"] or 0
        equipment_attention = equipment_summary["attention"] or 0

        transactions = InventoryTransaction.objects.filter(item__section__in=sections)
        timezone_now = timezone.get_current_timezone()
        period_start = timezone.make_aware(datetime.combine(start_date, time.min), timezone_now)
        period_end = timezone.make_aware(datetime.combine(today + timedelta(days=1), time.min), timezone_now)
        period_transactions = transactions.filter(created_at__gte=period_start, created_at__lt=period_end)
        movement_rows = period_transactions.annotate(
            movement_day=TruncDate("created_at")
        ).values("movement_day", "transaction_type").annotate(
            events=Count("pk"),
        ).order_by("movement_day")
        movement_lookup = defaultdict(lambda: {"addition": 0, "removal": 0, "adjustment": 0})
        movement_totals = defaultdict(int)
        for row in movement_rows:
            movement_lookup[row["movement_day"]][row["transaction_type"]] = row["events"]
            movement_totals[row["transaction_type"]] += row["events"]
        movement_series = []
        for offset in range(period_days):
            day = start_date + timedelta(days=offset)
            counts = movement_lookup[day]
            movement_series.append({
                "date": day.isoformat(),
                "label": day.strftime("%b %d"),
                "addition": counts["addition"],
                "removal": counts["removal"],
                "adjustment": counts["adjustment"],
            })

        allocation = StockLedger.objects.filter(item__section__in=sections).aggregate(
            available=Sum("available"),
            reserved=Sum("reserved"),
            issued=Sum("issued"),
        )
        allocation_series = [
            {"label": "Available", "value": float(allocation["available"] or 0), "color": "#26d9c2"},
            {"label": "Reserved", "value": float(allocation["reserved"] or 0), "color": "#62a5ff"},
            {"label": "Issued", "value": float(allocation["issued"] or 0), "color": "#f3b54a"},
        ]

        category_series = list(
            items.values("category__name").annotate(item_count=Count("pk"))
            .order_by("-item_count", "category__name")[:8]
        )
        category_series = [
            {"label": row["category__name"] or "Uncategorized", "value": row["item_count"]}
            for row in category_series
        ]

        reservations = WorkMaterialReservation.objects.filter(
            inventory_item__section__in=sections
        ).select_related("inventory_item", "inventory_item__category", "inventory_item__section")
        reservation_counts = {}
        reserved_quantity = Decimal("0.00")
        for row in reservations.values("status").annotate(count=Count("pk"), quantity=Sum("quantity")):
            reservation_counts[row["status"]] = row["count"]
            if row["status"] == "reserved":
                reserved_quantity = row["quantity"] or Decimal("0.00")
        reagent_is_unscoped = scope["can_select_all"] and scope["selected_plant"] is None
        reagent_records = LaboratoryReagent.objects.all() if reagent_is_unscoped else LaboratoryReagent.objects.none()
        reagent_summary = reagent_records.aggregate(records=Count("pk"), quantity=Sum("quantity"))
        alert_configs = AlertConfig.objects.filter(section__in=sections).select_related(
            "section", "category"
        )
        alert_type_counts = {}
        for row in alert_configs.filter(active=True).values("alert_type").annotate(count=Count("pk")):
            alert_type_counts[row["alert_type"]] = row["count"]
        active_alert_count = sum(alert_type_counts.values())

        item_type_count = ItemType.objects.count() if scope["has_scope"] else 0
        supplier_count = items.exclude(supplier__isnull=True).values("supplier_id").distinct().count()
        category_count = Category.objects.count() if scope["has_scope"] else 0
        status_count = Status.objects.count() if scope["has_scope"] else 0
        recent_transactions = transactions.select_related(
            "item", "item__category", "item__section", "performed_by"
        ).order_by("-created_at")[:14]
        low_stock_rows = low_stock_items.select_related(
            "category", "section", "supplier"
        ).order_by("available_stock", "name")[:12]
        expiring_rows = items.filter(
            expiry_date__lte=expiry_horizon,
        ).select_related("category", "section", "supplier").order_by("expiry_date")[:12]
        equipment_rows = equipment.order_by(
            F("next_maintenance_date").asc(nulls_last=True),
            F("next_calibration_date").asc(nulls_last=True),
            "name",
        )[:16]
        for record in equipment_rows:
            deadlines = [date for date in (record.next_maintenance_date, record.next_calibration_date) if date]
            if any(date < today for date in deadlines):
                record.dashboard_state = "overdue"
                record.dashboard_state_label = "Overdue"
            elif any(date <= due_through for date in deadlines):
                record.dashboard_state = "due"
                record.dashboard_state_label = "Due soon"
            elif deadlines:
                record.dashboard_state = "current"
                record.dashboard_state_label = "On schedule"
            else:
                record.dashboard_state = "untracked"
                record.dashboard_state_label = "No date set"

        reagent_rows = reagent_records.select_related("category").order_by("rname")[:12]
        reservation_rows = reservations.order_by("-created_at")[:12]
        config_rows = alert_configs.prefetch_related("users_to_notify").order_by("-active", "alert_type", "name")[:12]
        item_content_type = ContentType.objects.get_for_model(InventoryItem)
        audit_rows = AuditLog.objects.filter(
            content_type=item_content_type,
            object_id__in=items.values("pk"),
        ).select_related("user").order_by("-timestamp")[:10]

        movement_totals = dict(movement_totals)
        period_transactions_count = sum(movement_totals.values())
        inventory_rows = list(annotated_items.order_by("name")[:40])
        for item in inventory_rows:
            item.dashboard_low_stock = bool(
                item.restock_rule_configured and item.available_stock <= item.restock_limit
            )
            item.dashboard_expiry_watch = bool(item.expiry_date and item.expiry_date <= expiry_horizon)
        equipment_ready_percent = round(equipment_current * 100 / equipment_count) if equipment_count else 0
        kpis = [
            {
                "label": "Item records",
                "value": item_count,
                "detail": f"Across {scope['scope_label']}",
                "icon": "boxes-stacked",
                "tone": "cyan",
                "tab": "stock",
            },
            {
                "label": "Available quantity",
                "value": f"{available_total:,.2f}",
                "detail": "Recorded totals · model stores no unit",
                "icon": "warehouse",
                "tone": "blue",
                "tab": "stock",
            },
            {
                "label": "Restock watch",
                "value": low_stock_count,
                "detail": f"{unconfigured_restock_count} items have no active threshold",
                "icon": "triangle-exclamation",
                "tone": "amber",
                "tab": "stock",
                "filter": "low-stock",
            },
            {
                "label": "Expiry watch",
                "value": expiring_count + expired_count,
                "detail": f"{expired_count} expired · {expiring_count} due within 30 days",
                "icon": "hourglass-half",
                "tone": "rose",
                "tab": "stock",
                "filter": "expiry",
            },
            {
                "label": "Equipment attention",
                "value": equipment_attention,
                "detail": f"{maintenance_overdue + calibration_overdue} checks overdue · {maintenance_due + calibration_due} due soon",
                "icon": "screwdriver-wrench",
                "tone": "violet",
                "tab": "assets",
            },
            {
                "label": "Open reservations",
                "value": reservation_counts.get("reserved", 0),
                "detail": f"{reserved_quantity:,.2f} recorded quantity reserved",
                "icon": "cart-flatbed",
                "tone": "green",
                "tab": "supplies",
            },
        ]

        period_label = f"{start_date:%d %b %Y} – {today:%d %b %Y}"
        active_tab = request.GET.get("tab", "overview")
        valid_tabs = {"overview", "stock", "movements", "assets", "supplies", "controls"}
        if active_tab not in valid_tabs:
            active_tab = "overview"

        return {
            "app": {"name": "Inventory", "label": "Operations dashboard"},
            "app_groups": registry.sidebar(self.app_label),
            "models": self._model_cards(),
            "scope": scope,
            "kpis": kpis,
            "period_days": period_days,
            "period_choices": self.periods,
            "period_label": period_label,
            "active_tab": active_tab,
            "today": today,
            "item_count": item_count,
            "available_total": available_total,
            "locked_item_count": locked_item_count,
            "low_stock_count": low_stock_count,
            "unconfigured_restock_count": unconfigured_restock_count,
            "expired_count": expired_count,
            "expiring_count": expiring_count,
            "equipment_count": equipment_count,
            "equipment_current": equipment_current,
            "maintenance_overdue": maintenance_overdue,
            "calibration_overdue": calibration_overdue,
            "maintenance_due": maintenance_due,
            "calibration_due": calibration_due,
            "equipment_attention": equipment_attention,
            "equipment_ready_percent": equipment_ready_percent,
            "item_type_count": item_type_count,
            "supplier_count": supplier_count,
            "category_count": category_count,
            "status_count": status_count,
            "active_alert_count": active_alert_count,
            "alert_type_counts": alert_type_counts,
            "reservation_counts": reservation_counts,
            "reserved_quantity": reserved_quantity,
            "reagent_summary": reagent_summary,
            "reagent_is_unscoped": reagent_is_unscoped,
            "movement_totals": movement_totals,
            "period_transactions_count": period_transactions_count,
            "expiry_horizon": expiry_horizon,
            "movement_series": movement_series,
            "allocation_series": allocation_series,
            "category_series": category_series,
            "low_stock_rows": low_stock_rows,
            "expiring_rows": expiring_rows,
            "inventory_rows": inventory_rows,
            "recent_transactions": recent_transactions,
            "equipment_rows": equipment_rows,
            "reagent_rows": reagent_rows,
            "reservation_rows": reservation_rows,
            "config_rows": config_rows,
            "audit_rows": audit_rows,
            "sections_have_scope": scope["has_scope"],
        }
