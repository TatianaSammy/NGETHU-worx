"""Plant-scoped committee presence and LAN verification."""

from __future__ import annotations

import ipaddress

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from ca.models import (
    ChemicalInspectionAttendance,
    ChemicalInspectionCommitteeMember,
    ChemicalInspectionPlantNetwork,
    ChemicalWorkflowAlert,
)


def inspection_committee_setup_errors(plant_id):
    errors = []
    active_committee = ChemicalInspectionCommitteeMember.objects.filter(
        treatment_plant_id=plant_id,
        is_active=True,
        user__is_active=True,
    ) if plant_id else ChemicalInspectionCommitteeMember.objects.none()
    members_exist = active_committee.exists()
    if not members_exist:
        errors.append("Appoint at least one active Inspection Committee member before receiving a consignment.")
    if members_exist and not active_committee.filter(
        role=ChemicalInspectionCommitteeMember.Role.SECRETARY,
    ).exists():
        errors.append("Appoint an active Inspection Committee secretary before receiving a consignment.")

    network = ChemicalInspectionPlantNetwork.objects.filter(
        treatment_plant_id=plant_id
    ).only("lan_cidr", "ip_restriction_enabled").first() if plant_id else None
    if network is None:
        errors.append("Configure this treatment plant's committee attendance policy before receiving a consignment.")
    elif not network.ip_restriction_enabled:
        pass
    else:
        try:
            if not network.lan_cidr:
                raise ValueError("LAN CIDR is required while IP restriction is enabled.")
            ipaddress.ip_network(str(network.lan_cidr).strip(), strict=False)
        except ValueError:
            errors.append("The treatment plant inspection LAN must be a valid IPv4 or IPv6 CIDR network.")
    return errors


def request_client_ip(request):
    """Read the client address set by the configured reverse proxy.

    The deployment's Nginx configuration overwrites X-Real-IP after resolving
    its trusted forwarded chain. X-Forwarded-For is deliberately not consumed
    here because clients can supply that header themselves.
    """
    if request is None:
        return None
    raw_ip = request.META.get("HTTP_X_REAL_IP") or request.META.get("REMOTE_ADDR")
    try:
        return ipaddress.ip_address(str(raw_ip).strip()) if raw_ip else None
    except ValueError:
        return None


def committee_attendance_status(batch):
    members = list(
        ChemicalInspectionCommitteeMember.objects.filter(
            treatment_plant_id=batch.treatment_plant_id,
            is_active=True,
            user__is_active=True,
        ).select_related("user")
    )
    required = (2 * len(members) + 2) // 3
    network = ChemicalInspectionPlantNetwork.objects.filter(
        treatment_plant_id=batch.treatment_plant_id
    ).only("lan_cidr", "ip_restriction_enabled").first()
    lan = None
    restriction_enabled = network is None or network.ip_restriction_enabled
    if network and restriction_enabled and network.lan_cidr:
        try:
            lan = ipaddress.ip_network(str(network.lan_cidr).strip(), strict=False)
        except ValueError:
            lan = None

    attendance = list(
        ChemicalInspectionAttendance.objects.filter(
            batch=batch,
            committee_member_id__in=[member.pk for member in members],
        ).select_related("committee_member", "committee_member__user")
    )
    acknowledged_user_ids = set(
        ChemicalWorkflowAlert.objects.filter(
            batch=batch,
            alert_type="ca_inspection_committee_receipt",
            recipient_id__in=[member.user_id for member in members],
            acknowledged_at__isnull=False,
            acknowledged_by_id=F("recipient_id"),
        ).values_list("recipient_id", flat=True)
    )
    present = []
    if not restriction_enabled:
        present = [
            item.committee_member
            for item in attendance
            if item.committee_member.user_id in acknowledged_user_ids
        ]
    elif lan is not None:
        present = [
            item.committee_member
            for item in attendance
            if item.committee_member.user_id in acknowledged_user_ids
            and item.client_ip
            and ipaddress.ip_address(item.client_ip) in lan
        ]
    present_by_id = {member.pk: member for member in present}
    present_members = [member for member in members if member.pk in present_by_id]
    return {
        "total_members": len(members),
        "present_count": len(present_members),
        "required_count": required,
        "quorum_met": bool(members) and len(present_members) >= required,
        "network_configured": bool(network and (not restriction_enabled or lan is not None)),
        "ip_restriction_enabled": restriction_enabled,
        "present_members": present_members,
    }


@transaction.atomic
def acknowledge_committee_presence(*, alert, user, request):
    if (
        getattr(alert, "alert_type", None) != "ca_inspection_committee_receipt"
        or getattr(alert, "recipient_id", None) != getattr(user, "pk", None)
    ):
        raise PermissionDenied("Only the committee member's own receipt alert can record attendance.")
    batch = alert.batch
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        raise PermissionDenied("Sign in with an active committee account to acknowledge attendance.")
    if not batch.treatment_plant_id:
        raise PermissionDenied("This batch has no treatment plant scope.")

    member = ChemicalInspectionCommitteeMember.objects.select_for_update().filter(
        treatment_plant_id=batch.treatment_plant_id,
        user=user,
        is_active=True,
    ).first()
    if member is None:
        raise PermissionDenied("Only an active member of this plant's Inspection Committee may acknowledge attendance.")

    network = ChemicalInspectionPlantNetwork.objects.filter(
        treatment_plant_id=batch.treatment_plant_id
    ).only("lan_cidr", "ip_restriction_enabled").first()
    if network is None:
        raise PermissionDenied("The treatment plant committee attendance policy has not been configured.")
    client_ip = request_client_ip(request)
    if network.ip_restriction_enabled:
        if not network.lan_cidr:
            raise PermissionDenied("The treatment plant inspection LAN has not been configured.")
        try:
            lan = ipaddress.ip_network(str(network.lan_cidr).strip(), strict=False)
        except ValueError as exc:
            raise PermissionDenied("The treatment plant inspection LAN configuration is invalid.") from exc
        if client_ip is None or client_ip not in lan:
            raise PermissionDenied("Attendance can only be acknowledged from the treatment plant LAN.")

    attendance, _created = ChemicalInspectionAttendance.objects.update_or_create(
        batch=batch,
        committee_member=member,
        defaults={"client_ip": str(client_ip) if client_ip is not None else None, "acknowledged_at": timezone.now()},
    )
    return attendance


def is_active_committee_member(user, plant_id):
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and ChemicalInspectionCommitteeMember.objects.filter(
            treatment_plant_id=plant_id,
            user=user,
            is_active=True,
        ).exists()
    )


def is_active_committee_secretary(user, plant_id):
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and ChemicalInspectionCommitteeMember.objects.filter(
            treatment_plant_id=plant_id,
            user=user,
            role=ChemicalInspectionCommitteeMember.Role.SECRETARY,
            is_active=True,
        ).exists()
    )


def can_access_committee_inspection_form(user, batch, attendance=None):
    """Only the present committee secretary may proceed after alert-based quorum."""
    if not is_active_committee_secretary(user, batch.treatment_plant_id):
        return False
    attendance = attendance or committee_attendance_status(batch)
    return bool(
        attendance["network_configured"]
        and attendance["quorum_met"]
        and any(
            member.user_id == user.pk
            and member.role == ChemicalInspectionCommitteeMember.Role.SECRETARY
            for member in attendance["present_members"]
        )
    )
