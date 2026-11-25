# Maintenance command-center actions
from __future__ import annotations

import uuid
from urllib.parse import urlsplit

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from maintenance.models import (
    DutyHouse,
    DutyHouseStatus,
    DutyHouseOccupancy,
    DutyHouseUnit,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
    MaintenanceIncident,
    OrderStatus,
    OccupancyStatus,
    OccupancyTransfer,
    RequestStatus,
    WorkOrder,
    WorkRequest,
)
from t.models import TreatmentPlant


def _back(request: HttpRequest, tab: str = "overview") -> HttpResponse:
    dashboard_url = reverse("maintenance:dashboard")
    referer = request.META.get("HTTP_REFERER", "")
    if referer and url_has_allowed_host_and_scheme(
        referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        referer_path = urlsplit(referer).path
        dashboard_path = urlsplit(dashboard_url).path
        allowed_dashboard_paths = {
            dashboard_path,
            f"{dashboard_path.rstrip('/')}/dashboard/",
        }
        if referer_path in allowed_dashboard_paths:
            return redirect(referer)
    return redirect(f"{dashboard_url}?tab={tab}")


def _plant_ids(user):
    if user.is_superuser:
        return None
    return list(
        TreatmentPlant.objects.filter(assigned_users=user).values_list("pk", flat=True)
    )


def _house_scope(user):
    queryset = DutyHouse.objects.all()
    plant_ids = _plant_ids(user)
    if plant_ids is not None:
        return queryset.filter(plant_id__in=plant_ids)
    return queryset


def _incident_scope(user):
    queryset = MaintenanceIncident.objects.all()
    plant_ids = _plant_ids(user)
    if plant_ids is not None:
        if not plant_ids:
            return queryset.none()
        return queryset.filter(
            (
                Q(plant_id__in=plant_ids)
                & (Q(duty_house__isnull=True) | Q(duty_house__plant_id__in=plant_ids))
            )
            | Q(plant__isnull=True, duty_house__plant_id__in=plant_ids)
        )
    return queryset


def _request_scope(user):
    queryset = WorkRequest.objects.all()
    plant_ids = _plant_ids(user)
    if plant_ids is not None:
        return queryset.filter(duty_house__plant_id__in=plant_ids)
    return queryset


def _order_scope(user):
    queryset = WorkOrder.objects.all()
    plant_ids = _plant_ids(user)
    if plant_ids is not None:
        if not plant_ids:
            return queryset.none()
        return queryset.filter(
            Q(duty_house__plant_id__in=plant_ids)
            | Q(duty_house__isnull=True, request__duty_house__plant_id__in=plant_ids)
        )
    return queryset


def _occupancy_scope(user):
    queryset = DutyHouseOccupancy.objects.select_related(
        "duty_house", "unit", "user"
    )
    plant_ids = _plant_ids(user)
    if plant_ids is not None:
        return queryset.filter(duty_house__plant_id__in=plant_ids)
    return queryset


def _unit_scope(user):
    queryset = DutyHouseUnit.objects.select_related("duty_house", "duty_house__plant")
    plant_ids = _plant_ids(user)
    if plant_ids is not None:
        return queryset.filter(duty_house__plant_id__in=plant_ids)
    return queryset


def _report_response(request, message, status=200, success=False):
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        response = JsonResponse({"success": success, "message": message}, status=status)
        response["Cache-Control"] = "private, no-store"
        return response
    (messages.success if success else messages.error)(request, message)
    return _back(request, "incidents")


@login_required
@permission_required("Maintenance_Incident.add_maintenanceincident", raise_exception=True)
@require_POST
def report_incident(request: HttpRequest) -> HttpResponse:
    """Create an incident report submitted through the live application."""
    from maintenance.models import MaintenanceIncident

    title = (request.POST.get("title") or "").strip()
    description = (request.POST.get("description") or "").strip()
    severity = request.POST.get("severity", "")
    incident_type = request.POST.get("incident_type", "")
    if not title or not description:
        return _report_response(request, "Enter a title and description for the incident.", 400)
    if len(title) > 255:
        return _report_response(request, "Incident titles must be 255 characters or fewer.", 400)
    if severity not in {value for value, _ in IncidentSeverity.choices}:
        return _report_response(request, "Select a valid incident severity.", 400)
    if incident_type not in {value for value, _ in IncidentType.choices}:
        return _report_response(request, "Select a valid incident type.", 400)

    raw_plant_id = (request.POST.get("plant") or "").strip()
    raw_house_id = (request.POST.get("duty_house") or "").strip()
    plant = None
    house = None
    plant_ids = _plant_ids(request.user)
    if raw_plant_id:
        plants = TreatmentPlant.objects.all()
        if plant_ids is not None:
            plants = plants.filter(pk__in=plant_ids)
        plant = plants.filter(pk=raw_plant_id).first()
        if plant is None:
            return _report_response(request, "Select a treatment plant assigned to your account.", 403)
    if raw_house_id:
        house = _house_scope(request.user).select_related("plant").filter(pk=raw_house_id).first()
        if house is None:
            return _report_response(request, "Select a duty house in your assigned plant.", 403)
        if plant and house.plant_id != plant.pk:
            return _report_response(request, "The selected house does not belong to that plant.", 400)
        plant = house.plant
    if plant is None:
        return _report_response(request, "Select the treatment plant where the incident occurred.", 400)

    occurred_at = parse_datetime(request.POST.get("occurred_at", ""))
    if occurred_at is None:
        occurred_at = timezone.now()
    elif timezone.is_naive(occurred_at):
        occurred_at = timezone.make_aware(occurred_at, timezone.get_current_timezone())

    defaults = {
        "incident_no": f"MI-{timezone.localdate():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}",
        "title": title,
        "description": description,
        "incident_type": incident_type,
        "severity": severity,
        "reported_by": request.user,
        "created_by": request.user,
        "target_type": "duty_house" if house else "plant",
        "duty_house": house,
        "plant": plant,
        "occurred_at": occurred_at,
    }
    incident = MaintenanceIncident.objects.create(**defaults)
    if house and severity == IncidentSeverity.CRITICAL:
        incident.process_incident()
    else:
        incident.generate_fingerprint()
        incident.cluster_incident()
        if incident.cluster:
            incident.cluster.update_risk()
        incident.update_equipment_health()
        incident.save(
            update_fields=["fingerprint", "cluster", "updated_at"]
        )
    incident.stream_event(
        "reported",
        {"by": request.user.get_username(), "source": "dashboard"},
    )
    return _report_response(
        request,
        "Incident report submitted.",
        success=True,
    )


@login_required
@permission_required("maintenance.change_workrequest", raise_exception=True)
@require_POST
def approve_request(request: HttpRequest, pk) -> HttpResponse:
    obj = get_object_or_404(_request_scope(request.user), pk=pk)
    try:
        if obj.status == RequestStatus.DRAFT:
            obj.status = RequestStatus.PENDING
            obj.save(update_fields=["status"])
        obj.approve(request.user)
        messages.success(request, f"Approved request: {obj.title}")
    except ValidationError as exc:
        messages.error(request, str(exc))
    return _back(request, "requests")


@login_required
@permission_required("maintenance.change_workrequest", raise_exception=True)
@permission_required("maintenance.add_workorder", raise_exception=True)
@require_POST
def convert_request(request: HttpRequest, pk) -> HttpResponse:
    obj = get_object_or_404(_request_scope(request.user), pk=pk)
    try:
        if obj.status == RequestStatus.PENDING:
            obj.approve(request.user)
        order = obj.convert_to_order()
        messages.success(request, f"Created work order {order}")
    except ValidationError as exc:
        messages.error(request, str(exc))
    return _back(request, "orders")


@login_required
@permission_required("maintenance.change_workorder", raise_exception=True)
@require_POST
def assign_order(request: HttpRequest, pk) -> HttpResponse:
    obj = get_object_or_404(_order_scope(request.user), pk=pk)
    if obj.status not in {OrderStatus.QUEUED, OrderStatus.ASSIGNED}:
        messages.warning(request, "Only queued or assigned work orders can be assigned.")
        return _back(request, "orders")
    # The dashboard action is a self-assignment. It does not allow a crafted
    # POST to assign an order to an arbitrary account.
    obj.assign(request.user)
    messages.success(request, f"Assigned {obj} to you")
    return _back(request, "orders")


@login_required
@permission_required("maintenance.change_workorder", raise_exception=True)
@require_POST
def start_order(request: HttpRequest, pk) -> HttpResponse:
    obj = get_object_or_404(_order_scope(request.user), pk=pk)
    try:
        obj.start()
        messages.success(request, f"Started {obj}")
    except ValidationError as exc:
        messages.error(request, str(exc))
    return _back(request, "orders")


@login_required
@permission_required("maintenance.change_workorder", raise_exception=True)
@require_POST
def complete_order(request: HttpRequest, pk) -> HttpResponse:
    obj = get_object_or_404(_order_scope(request.user), pk=pk)
    try:
        if not obj.tasks.exists():
            obj.status = OrderStatus.COMPLETED
            obj.progress = 100
            obj.completed_at = timezone.now()
            obj.save(update_fields=["status", "progress", "completed_at"])
        else:
            obj.complete()
        messages.success(request, f"Completed {obj}")
    except ValidationError as exc:
        messages.error(request, str(exc))
    return _back(request, "orders")


@login_required
@permission_required("Maintenance_Incident.change_maintenanceincident", raise_exception=True)
@require_POST
def acknowledge_incident(request: HttpRequest, pk) -> HttpResponse:
    obj = get_object_or_404(_incident_scope(request.user), pk=pk)
    if obj.status not in {
        IncidentStatus.REPORTED,
        IncidentStatus.CONFIRMED,
        IncidentStatus.ESCALATED,
    }:
        messages.warning(request, "This incident cannot be acknowledged in its current state.")
        return _back(request, "incidents")
    obj.status = IncidentStatus.UNDER_REVIEW
    obj.updated_by = request.user
    obj.save(update_fields=["status", "updated_by", "updated_at"])
    obj.stream_event("acknowledged", {"by": request.user.get_username()})
    messages.success(request, f"Acknowledged {obj.incident_no}")
    return _back(request, "incidents")


@login_required
@permission_required("Maintenance_Incident.change_maintenanceincident", raise_exception=True)
@require_POST
def assign_incident(request: HttpRequest, pk) -> HttpResponse:
    obj = get_object_or_404(_incident_scope(request.user), pk=pk)
    if obj.status in {IncidentStatus.RESOLVED, IncidentStatus.CLOSED}:
        messages.warning(request, "Reopen this incident before assigning it.")
        return _back(request, "incidents")
    previous_assignee = obj.assigned_to.get_username() if obj.assigned_to else ""
    obj.assigned_to = request.user
    obj.updated_by = request.user
    obj.save(update_fields=["assigned_to", "updated_by", "updated_at"])
    obj.stream_event(
        "assignment_changed",
        {"from": previous_assignee, "to": request.user.get_username()},
    )
    messages.success(request, f"Assigned {obj.incident_no} to you")
    return _back(request, "incidents")


@login_required
@permission_required("Maintenance_Incident.change_maintenanceincident", raise_exception=True)
@require_POST
def escalate_incident(request: HttpRequest, pk) -> HttpResponse:
    obj = get_object_or_404(_incident_scope(request.user), pk=pk)
    if obj.status in {IncidentStatus.RESOLVED, IncidentStatus.CLOSED}:
        messages.warning(request, "Resolved incidents cannot be escalated.")
        return _back(request, "incidents")
    obj.status = IncidentStatus.ESCALATED
    obj.updated_by = request.user
    obj.save(update_fields=["status", "updated_by", "updated_at"])
    obj.stream_event("manual_escalation", {"by": request.user.get_username()})
    messages.warning(request, f"Escalated {obj.incident_no}")
    return _back(request, "incidents")


@login_required
@permission_required("Maintenance_Incident.change_maintenanceincident", raise_exception=True)
@require_POST
def resolve_incident(request: HttpRequest, pk) -> HttpResponse:
    obj = get_object_or_404(_incident_scope(request.user), pk=pk)
    if obj.status in {IncidentStatus.RESOLVED, IncidentStatus.CLOSED}:
        messages.warning(request, "This incident is already resolved.")
        return _back(request, "incidents")
    summary = (request.POST.get("resolution_summary") or "").strip()
    root_cause = (request.POST.get("root_cause") or "").strip()
    if not summary:
        messages.error(request, "Enter a resolution summary before resolving this incident.")
        return _back(request, "incidents")
    obj.status = IncidentStatus.RESOLVED
    obj.resolved_at = timezone.now()
    obj.resolution_summary = summary[:10000]
    obj.root_cause = root_cause[:10000]
    obj.updated_by = request.user
    obj.save(
        update_fields=[
            "status", "resolved_at", "resolution_summary", "root_cause",
            "updated_by", "updated_at",
        ]
    )
    obj.stream_event(
        "resolved",
        {
            "by": request.user.get_username(),
            "resolution_summary": obj.resolution_summary,
        },
    )
    messages.success(request, f"Resolved {obj.incident_no}")
    return _back(request, "incidents")


@login_required
@permission_required("Maintenance_Incident.change_maintenanceincident", raise_exception=True)
@require_POST
def close_incident(request: HttpRequest, pk) -> HttpResponse:
    obj = get_object_or_404(_incident_scope(request.user), pk=pk)
    if obj.status != IncidentStatus.RESOLVED:
        messages.error(request, "Only resolved incidents can be closed.")
        return _back(request, "incidents")
    obj.status = IncidentStatus.CLOSED
    obj.closed_at = timezone.now()
    obj.updated_by = request.user
    obj.save(update_fields=["status", "closed_at", "updated_by", "updated_at"])
    obj.stream_event("closed", {"by": request.user.get_username()})
    messages.success(request, f"Closed {obj.incident_no}")
    return _back(request, "incidents")


@login_required
@permission_required("Maintenance_Incident.change_maintenanceincident", raise_exception=True)
@require_POST
def reopen_incident(request: HttpRequest, pk) -> HttpResponse:
    obj = get_object_or_404(_incident_scope(request.user), pk=pk)
    if obj.status not in {IncidentStatus.RESOLVED, IncidentStatus.CLOSED}:
        messages.error(request, "Only resolved or closed incidents can be reopened.")
        return _back(request, "incidents")
    obj.status = IncidentStatus.UNDER_REVIEW
    obj.closed_at = None
    obj.resolved_at = None
    obj.updated_by = request.user
    obj.save(update_fields=["status", "closed_at", "resolved_at", "updated_by", "updated_at"])
    obj.stream_event("reopened", {"by": request.user.get_username()})
    messages.success(request, f"Reopened {obj.incident_no}")
    return _back(request, "incidents")


@login_required
@permission_required("duty_house.change_dutyhouse", raise_exception=True)
@require_POST
def mark_house_maintenance(request: HttpRequest, pk) -> HttpResponse:
    obj = get_object_or_404(_house_scope(request.user), pk=pk)
    if not obj.is_active or obj.status == DutyHouseStatus.DECOMMISSIONED:
        messages.error(request, "Decommissioned houses cannot be placed into maintenance.")
        return _back(request, "housing")
    obj.status = DutyHouseStatus.MAINTENANCE
    obj.updated_by = request.user
    obj.save(update_fields=["status", "updated_by", "updated_at"])
    messages.info(request, f"{obj} marked under maintenance")
    return _back(request, "housing")


@login_required
@permission_required("duty_house.change_dutyhouse", raise_exception=True)
@require_POST
def activate_house(request: HttpRequest, pk) -> HttpResponse:
    obj = get_object_or_404(_house_scope(request.user), pk=pk)
    if not obj.is_active:
        messages.error(request, "Decommissioned houses cannot be reactivated here.")
        return _back(request, "housing")
    obj.status = DutyHouseStatus.ACTIVE
    obj.updated_by = request.user
    obj.save(update_fields=["status", "updated_by", "updated_at"])
    messages.success(request, f"{obj} marked active")
    return _back(request, "housing")


@login_required
@permission_required("duty_house.add_dutyhouseoccupancy", raise_exception=True)
@require_POST
def check_in_occupant(request: HttpRequest) -> HttpResponse:
    house = get_object_or_404(_house_scope(request.user), pk=request.POST.get("house_id"))
    unit = get_object_or_404(
        _unit_scope(request.user),
        pk=request.POST.get("unit_id"),
        duty_house_id=house.pk,
    )
    User = get_user_model()
    person = get_object_or_404(
        User.objects.filter(
            assigned_treatment_plant_id=house.plant_id,
            is_active=True,
        ),
        pk=request.POST.get("user_id"),
    )
    if not house.is_active or house.status != DutyHouseStatus.ACTIVE:
        messages.error(request, "Occupants can only be assigned to an active house.")
        return _back(request, "housing")
    occupancy = DutyHouseOccupancy(
        duty_house=house,
        unit=unit,
        user=person,
        status=OccupancyStatus.ACTIVE,
        is_current=True,
        check_in_by=request.user,
        created_by=request.user,
        updated_by=request.user,
    )
    try:
        occupancy.full_clean()
        occupancy.save()
        messages.success(request, f"Checked {person} into {unit}.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    except IntegrityError:
        messages.error(
            request,
            "This person already has a current housing assignment or the unit just reached capacity.",
        )
    return _back(request, "housing")


@login_required
@permission_required("duty_house.change_dutyhouseoccupancy", raise_exception=True)
@require_POST
def check_out_occupant(request: HttpRequest, pk) -> HttpResponse:
    try:
        with transaction.atomic():
            occupancy = _occupancy_scope(request.user).select_for_update().filter(
                pk=pk, is_current=True
            ).first()
            if occupancy is None:
                messages.error(
                    request,
                    "This housing assignment is no longer current. Refresh the dashboard.",
                )
                return _back(request, "housing")
            occupancy.is_current = False
            occupancy.status = OccupancyStatus.ENDED
            occupancy.end_date = timezone.now()
            occupancy.exit_reason = (request.POST.get("exit_reason") or "").strip()[:2000]
            occupancy.check_out_by = request.user
            occupancy.updated_by = request.user
            occupancy.save(
                update_fields=[
                    "is_current", "status", "end_date", "exit_reason",
                    "check_out_by", "updated_by", "updated_at",
                ]
            )
        messages.success(request, f"Checked {occupancy.user} out of {occupancy.unit}.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    except IntegrityError:
        messages.error(
            request,
            "The assignment changed while processing. Refresh the dashboard and try again.",
        )
    return _back(request, "housing")


@login_required
@permission_required("duty_house.add_occupancytransfer", raise_exception=True)
@permission_required("duty_house.change_dutyhouseoccupancy", raise_exception=True)
@require_POST
def transfer_occupant(request: HttpRequest, pk=None) -> HttpResponse:
    occupancy_id = pk or request.POST.get("occupancy_id")
    occupancy = get_object_or_404(
        _occupancy_scope(request.user), pk=occupancy_id, is_current=True
    )
    destination = get_object_or_404(
        _unit_scope(request.user), pk=request.POST.get("to_unit_id")
    )
    if destination.duty_house.plant_id != occupancy.duty_house.plant_id:
        messages.error(request, "Transfers must stay within your assigned treatment plant.")
        return _back(request, "housing")
    transfer = OccupancyTransfer(
        from_unit=occupancy.unit,
        to_unit=destination,
        user=occupancy.user,
        transferred_by=request.user,
        created_by=request.user,
        updated_by=request.user,
    )
    try:
        with transaction.atomic():
            transfer.save()
            transfer.execute(request.user)
        messages.success(request, f"Transferred {occupancy.user} to {destination}.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    except IntegrityError:
        messages.error(
            request,
            "The housing assignment changed while processing. Refresh the dashboard and try again.",
        )
    return _back(request, "housing")
