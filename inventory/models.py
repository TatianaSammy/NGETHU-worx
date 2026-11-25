#inventory/models.py
import uuid
import random
import string
from decimal import Decimal

from django.db import models, transaction
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from django.core.exceptions import ValidationError

from core.models import Section
from accounts.models import CustomUser

User = CustomUser


# =========================================================
# UTILS
# =========================================================

def generate_uuid(length=10):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


# =========================================================
# CORE CLASSIFICATION
# =========================================================

class Status(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.name


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.name


class Supplier(models.Model):
    name = models.CharField(max_length=100, unique=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.name


class ItemType(models.Model):
    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


# =========================================================
# STOCK LEDGER (NEW CORE ENGINE)
# =========================================================

class StockLedger(models.Model):
    """
    Single source of truth for stock integrity.
    Tracks:
    - available stock
    - reserved stock (for maintenance work orders)
    - issued stock (consumed)
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    item = models.OneToOneField(
        "InventoryItem",
        on_delete=models.CASCADE,
        related_name="ledger"
    )

    available = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reserved = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    issued = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    updated_at = models.DateTimeField(auto_now=True)

    # -----------------------------
    # CORE OPERATIONS
    # -----------------------------

    def reserve(self, qty: Decimal):
        if self.available < qty:
            raise ValidationError("Insufficient stock to reserve")

        self.available -= qty
        self.reserved += qty
        self.save(update_fields=["available", "reserved"])

    def release_reservation(self, qty: Decimal):
        self.reserved -= qty
        self.available += qty
        self.save(update_fields=["available", "reserved"])

    def issue(self, qty: Decimal):
        if self.reserved < qty:
            raise ValidationError("Not enough reserved stock")

        self.reserved -= qty
        self.issued += qty
        self.save(update_fields=["reserved", "issued"])


# =========================================================
# ALERT CONFIGURATION (IMPROVED)
# =========================================================

class AlertConfig(models.Model):
    ALERT_TYPE_CHOICES = [
        ('restock', 'Low Stock / Restock'),
        ('maintenance_due', 'Equipment Maintenance Due'),
        ('calibration_due', 'Equipment Calibration Due'),
        ('expiry', 'Expiry Alert'),
        ('custom', 'Custom Alert'),
    ]

    name = models.CharField(max_length=100, unique=True)
    alert_type = models.CharField(max_length=50, choices=ALERT_TYPE_CHOICES)

    active = models.BooleanField(default=True)
    threshold_days = models.PositiveIntegerField(default=7)
    min_quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="alert_configs")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, null=True, blank=True)

    users_to_notify = models.ManyToManyField(User, blank=True)

    def __str__(self):
        return f"{self.name} ({self.alert_type})"

    def is_triggered_for(self, obj):
        today = timezone.now().date()

        if self.alert_type == "restock":
            return getattr(obj, "quantity", 0) <= (self.min_quantity or 0)

        if self.alert_type == "expiry":
            if hasattr(obj, "expiry_date") and obj.expiry_date:
                return (obj.expiry_date - today).days <= self.threshold_days

        if self.alert_type == "maintenance_due":
            return getattr(obj, "next_maintenance_date", None) and \
                   (obj.next_maintenance_date - today).days <= self.threshold_days

        if self.alert_type == "calibration_due":
            return getattr(obj, "next_calibration_date", None) and \
                   (obj.next_calibration_date - today).days <= self.threshold_days

        return False

    def send_alert(self, obj):
        from .signals import send_alert
        send_alert(self, obj, f"{self.alert_type} triggered for {obj}")


# =========================================================
# INVENTORY ITEM (ENHANCED WITH LEDGER LINK)
# =========================================================

class InventoryItem(models.Model):
    name = models.CharField(max_length=100)

    item_type = models.ForeignKey(ItemType, on_delete=models.CASCADE, related_name="items")
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="items")
    section = models.ForeignKey(Section, on_delete=models.CASCADE, related_name="items")

    supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, blank=True)

    purchase_date = models.DateField()
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    expiry_date = models.DateField(null=True, blank=True)
    storage_conditions = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)

    is_locked = models.BooleanField(default=False)  # reservation lock

    def __str__(self):
        return f"{self.name} ({self.section})"

    # -----------------------------
    # LEDGER ACCESS
    # -----------------------------
    @property
    def stock(self):
        ledger, _ = StockLedger.objects.get_or_create(item=self)
        return ledger

    # -----------------------------
    # RESERVATION SYSTEM
    # -----------------------------
    @transaction.atomic
    def reserve_stock(self, qty: Decimal):
        self.stock.reserve(qty)

    @transaction.atomic
    def release_stock(self, qty: Decimal):
        self.stock.release_reservation(qty)

    @transaction.atomic
    def issue_stock(self, qty: Decimal):
        self.stock.issue(qty)


# =========================================================
# INVENTORY TRANSACTIONS (SYNCED WITH LEDGER)
# =========================================================

class InventoryTransaction(models.Model):
    TRANSACTION_TYPE_CHOICES = [
        ('addition', 'Addition'),
        ('removal', 'Removal'),
        ('adjustment', 'Adjustment'),
    ]

    item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name="transactions")
    performed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPE_CHOICES)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)

    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        is_new = self._state.adding

        if is_new:
            ledger = self.item.stock

            if self.transaction_type == "addition":
                ledger.available += self.quantity

            elif self.transaction_type == "removal":
                if ledger.available < self.quantity:
                    raise ValidationError("Insufficient stock")
                ledger.available -= self.quantity

            elif self.transaction_type == "adjustment":
                ledger.available = self.quantity

            ledger.save()

        super().save(*args, **kwargs)


# =========================================================
# WORK MATERIAL INTEGRATION (MAINTENANCE SYNC)
# =========================================================

class WorkMaterialReservation(models.Model):
    """
    Bridges Maintenance WorkOrder ↔ Inventory system
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    work_order_id = models.UUIDField(db_index=True)

    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE)

    quantity = models.DecimalField(max_digits=12, decimal_places=2)

    status = models.CharField(
        max_length=20,
        choices=[
            ("reserved", "Reserved"),
            ("issued", "Issued"),
            ("released", "Released"),
        ],
        default="reserved"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    # -----------------------------
    # RESERVATION FLOW
    # -----------------------------
    @transaction.atomic
    def reserve(self):
        self.inventory_item.reserve_stock(self.quantity)
        self.status = "reserved"
        self.save()

    @transaction.atomic
    def issue(self):
        self.inventory_item.issue_stock(self.quantity)
        self.status = "issued"
        self.save()

    @transaction.atomic
    def release(self):
        self.inventory_item.release_stock(self.quantity)
        self.status = "released"
        self.save()


class LaboratoryReagent(models.Model):
    rname = models.CharField(max_length=100)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    category = models.ForeignKey(Category, on_delete=models.CASCADE)

    def __str__(self):
        return self.rname


# =========================================================
# EQUIPMENT (MINIMAL SYNC HOOK)
# =========================================================

class Equipment(models.Model):
    name = models.CharField(max_length=100)
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    section = models.ForeignKey(Section, on_delete=models.CASCADE)

    serial_number = models.CharField(max_length=50, unique=True)

    next_maintenance_date = models.DateField(null=True, blank=True)
    next_calibration_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return self.name


# =========================================================
# AUDIT LOG (CLEANED)
# =========================================================

class AuditLog(models.Model):
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()

    content_object = GenericForeignKey("content_type", "object_id")

    action = models.CharField(max_length=50)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    notes = models.TextField(blank=True, null=True)
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.action} - {self.content_type}"