# Maintenance_Incident/models.py
import hashlib
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone


class AuditModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="%(class)s_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="%(class)s_updated",
    )
    class Meta:
        abstract = True


class IncidentSeverity(models.TextChoices):
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"
    CRITICAL = "critical", "Critical"


class IncidentStatus(models.TextChoices):
    REPORTED = "reported", "Reported"
    UNDER_REVIEW = "under_review", "Under Review"
    CONFIRMED = "confirmed", "Confirmed"
    ESCALATED = "escalated", "Escalated"
    CONVERTED = "converted", "Converted"
    AUTO_DISPATCHED = "auto_dispatched", "Auto Dispatched"
    RESOLVED = "resolved", "Resolved"
    CLOSED = "closed", "Closed"


class IncidentType(models.TextChoices):
    BREAKDOWN = "breakdown", "Breakdown"
    LEAKAGE = "leakage", "Leakage"
    ELECTRICAL = "electrical", "Electrical"
    STRUCTURAL = "structural", "Structural"
    MECHANICAL = "mechanical", "Mechanical"
    SAFETY = "safety", "Safety"
    VANDALISM = "vandalism", "Vandalism"
    FIRE = "fire", "Fire"
    FLOODING = "flooding", "Flooding"
    CALIBRATION = "calibration", "Calibration"
    OTHER = "other", "Other"


# =========================================================
# ESCALATION RULES
# =========================================================

class IncidentEscalationRule(AuditModel):
    name = models.CharField(
        max_length=120,
        unique=True,
    )

    severity = models.CharField(
        max_length=20,
        choices=IncidentSeverity.choices,
        db_index=True,
    )

    incident_type = models.CharField(
        max_length=30,
        choices=IncidentType.choices,
        null=True,
        blank=True,
        db_index=True,
    )

    escalate_after_minutes = models.PositiveIntegerField(
        default=30,
    )

    auto_assign_supervisor = models.BooleanField(
        default=True,
    )

    auto_create_workorder = models.BooleanField(
        default=False,
    )

    is_active = models.BooleanField(
        default=True,
        db_index=True,
    )

    class Meta:
        indexes = [
            models.Index(
                fields=[
                    "severity",
                    "incident_type",
                    "is_active",
                ]
            )
        ]

    def __str__(self):
        return self.name


# =========================================================
# CLUSTER
# =========================================================

class IncidentCluster(AuditModel):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    cluster_key = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
    )

    title = models.CharField(max_length=255)

    incident_type = models.CharField(
        max_length=30,
        choices=IncidentType.choices,
    )

    frequency_score = models.FloatField(default=0)

    predicted_risk_score = models.FloatField(default=0)

    last_seen_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    def update_risk(self):
        self.predicted_risk_score = min(
            1.0,
            self.frequency_score * 0.1,
        )

        self.save(
            update_fields=[
                "predicted_risk_score",
                "frequency_score",
                "last_seen_at",
            ]
        )

    def __str__(self):
        return self.title


# =========================================================
# EQUIPMENT HEALTH
# =========================================================

class EquipmentHealthScore(AuditModel):
    equipment = models.OneToOneField(
        "inventory.Equipment",
        on_delete=models.CASCADE,
        related_name="health_score",
    )

    score = models.FloatField(default=100)

    failure_risk = models.FloatField(default=0)

    last_updated = models.DateTimeField(
        auto_now=True,
    )

    def degrade(self, weight):
        self.score = max(0, self.score - weight)
        self.failure_risk = 1 - (self.score / 100)

        self.save(
            update_fields=[
                "score",
                "failure_risk",
                "last_updated",
            ]
        )


# =========================================================
# INCIDENT
# =========================================================

class MaintenanceIncident(AuditModel):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    incident_no = models.CharField(
        max_length=30,
        unique=True,
        db_index=True,
    )

    title = models.CharField(
        max_length=255,
    )

    description = models.TextField()

    incident_type = models.CharField(
        max_length=30,
        choices=IncidentType.choices,
        db_index=True,
    )

    severity = models.CharField(
        max_length=20,
        choices=IncidentSeverity.choices,
        db_index=True,
    )

    status = models.CharField(
        max_length=30,
        choices=IncidentStatus.choices,
        default=IncidentStatus.REPORTED,
        db_index=True,
    )

    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reported_incidents",
    )

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_maintenance_incidents",
    )

    target_type = models.CharField(
        max_length=20,
        db_index=True,
    )

    duty_house = models.ForeignKey(
        "duty_house.DutyHouse",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="incidents",
    )

    equipment = models.ForeignKey(
        "inventory.Equipment",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="incidents",
    )

    plant = models.ForeignKey(
        "t.TreatmentPlant",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="maintenance_incidents",
    )

    occurred_at = models.DateTimeField(
        default=timezone.now,
        db_index=True,
    )

    reported_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    cluster = models.ForeignKey(
        "IncidentCluster",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="incidents",
    )

    fingerprint = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
    )

    estimated_impact_score = models.FloatField(
        default=0,
    )

    sla_breach_time = models.DateTimeField(
        null=True,
        blank=True,
    )

    resolved_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    closed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    root_cause = models.TextField(
        blank=True,
        default="",
    )

    resolution_summary = models.TextField(
        blank=True,
        default="",
    )

    work_request = models.OneToOneField(
        "maintenance.WorkRequest",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="source_incident",
    )

    work_order = models.OneToOneField(
        "maintenance.WorkOrder",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="source_incident",
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
    )

    class Meta:
        ordering = ["-reported_at"]

        indexes = [
            models.Index(
                fields=["status", "severity"]
            ),
            models.Index(
                fields=["fingerprint"]
            ),
            models.Index(
                fields=["incident_type"]
            ),
            models.Index(
                fields=["occurred_at"]
            ),
        ]

    def generate_fingerprint(self):
        base = (
            f"{self.incident_type}:"
            f"{self.target_type}:"
            f"{self.equipment_id or 'NA'}:"
            f"{self.title}"
        )

        self.fingerprint = hashlib.sha256(
            base.encode()
        ).hexdigest()

        return self.fingerprint

    def stream_event(
        self,
        event_type,
        payload=None,
    ):
        IncidentStream.objects.create(
            incident=self,
            event_type=event_type,
            payload=payload or {},
            severity_snapshot=self.severity,
        )

    def cluster_incident(self):
        cluster, created = (
            IncidentCluster.objects.get_or_create(
                cluster_key=self.fingerprint,
                defaults={
                    "title": self.title,
                    "incident_type": self.incident_type,
                    "frequency_score": 1,
                },
            )
        )

        if not created:
            cluster.frequency_score += 1
            cluster.last_seen_at = timezone.now()
            cluster.update_risk()

        self.cluster = cluster

    def update_equipment_health(self):
        if not self.equipment:
            return

        health, _ = (
            EquipmentHealthScore.objects.get_or_create(
                equipment=self.equipment
            )
        )

        weights = {
            IncidentSeverity.LOW: 1,
            IncidentSeverity.MEDIUM: 5,
            IncidentSeverity.HIGH: 10,
            IncidentSeverity.CRITICAL: 25,
        }

        health.degrade(
            weights.get(self.severity, 5)
        )

    def escalate_if_needed(self):
        rule = (
            IncidentEscalationRule.objects.filter(
                severity=self.severity,
                is_active=True,
            )
            .order_by("-incident_type")
            .first()
        )

        if not rule:
            return

        elapsed = timezone.now() - self.occurred_at

        if elapsed > timedelta(
            minutes=rule.escalate_after_minutes
        ):
            self.status = (
                IncidentStatus.ESCALATED
            )

            self.save(
                update_fields=["status"]
            )

            self.stream_event(
                "auto_escalation",
                {
                    "elapsed_minutes":
                    elapsed.total_seconds() / 60
                },
            )

    @transaction.atomic
    def auto_dispatch(self):
        if self.severity != IncidentSeverity.CRITICAL:
            return None

        from maintenance.models import WorkRequest

        request = WorkRequest.objects.create(
            title=self.title,
            description=self.description,
            priority="critical",
            target_type="equipment"
            if self.equipment
            else "duty_house",
            duty_house=self.duty_house,
            reported_by=self.reported_by,
            status="approved",
        )

        order = request.convert_to_order()

        self.work_request = request
        self.work_order = order
        self.status = (
            IncidentStatus.AUTO_DISPATCHED
        )

        self.save(
            update_fields=[
                "work_request",
                "work_order",
                "status",
            ]
        )

        self.stream_event(
            "auto_dispatch",
            {
                "work_order": str(order.id)
            },
        )

        return order

    def process_incident(self):
        self.generate_fingerprint()
        self.cluster_incident()
        self.update_equipment_health()
        self.escalate_if_needed()

        if self.severity == IncidentSeverity.CRITICAL:
            return self.auto_dispatch()

        self.stream_event(
            "ingested",
            {
                "severity": self.severity,
                "status": self.status,
            },
        )

    def __str__(self):
        return self.incident_no


# =========================================================
# EVENT STREAM
# =========================================================

class IncidentStream(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    incident = models.ForeignKey(
        "MaintenanceIncident",
        on_delete=models.CASCADE,
        related_name="stream_events",
    )

    event_type = models.CharField(
        max_length=50,
        db_index=True,
    )

    payload = models.JSONField(
        default=dict,
        blank=True,
    )

    severity_snapshot = models.CharField(
        max_length=20,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    class Meta:
        ordering = ["-created_at"]
