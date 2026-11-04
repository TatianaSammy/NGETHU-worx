# sc/views_handover.py
from __future__ import annotations

import json
import logging
from django.http import JsonResponse
from django.views import View
from django.utils import timezone
from django.contrib.auth import authenticate, get_user_model
from django.views.decorators.http import require_GET, require_POST
from django.utils.decorators import method_decorator
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.core.exceptions import PermissionDenied
from core.plant_section_scope import scoped_queryset, user_plant_section

from sc.models import Shift, ShiftHandover, ShiftAssignment, Apparatus, ApparatusCondition, AuditLog
from sc.services.shift_service import (
    get_current_shift_data,
    get_next_shift,
    user_belongs_to_shift,
    get_eligible_supervisors_for_shift,
    seed_default_apparatus_if_needed,
    check_handover_window
)

logger = logging.getLogger(__name__)
User = get_user_model()


@login_required
def shift_handover_modal_data(request):
    """
    Returns shift telemetry, eligible supervisors for both shifts,
    apparatus checklist, and handover window timing.
    """
    user = request.user
    shift_data = get_current_shift_data(user, request=request)
    plant = shift_data.get("plant")
    current_shift = shift_data.get("current_shift")
    next_shift = shift_data.get("next_shift")
    supervisor = shift_data.get("supervisor") or user

    if not current_shift or not plant:
        return JsonResponse({"success": False, "message": "No active shift configured for treatment plant."}, status=400)

    # Ensure baseline apparatus exists
    section_id = getattr(user, "section_id", None)
    if not user.is_superuser and not section_id:
        return JsonResponse({"success": False, "message": "A section assignment is required for shift handover access."}, status=403)
    apparatus_qs = seed_default_apparatus_if_needed(plant, None if user.is_superuser else section_id)
    apparatus_list = [
        {
            "id": a.id,
            "name": a.name,
            "description": a.description or "",
        }
        for a in apparatus_qs
    ]

    # Eligible incoming supervisors belonging to next shift
    eligible_incoming = get_eligible_supervisors_for_shift(
        next_shift or current_shift,
        timezone.localdate(),
        section_id=None if user.is_superuser else section_id,
        allow_cross_scope=user.is_superuser,
    )
    incoming_list = [
        {
            "id": u.id,
            "name": u.get_full_name() or u.username,
            "username": u.username,
            "is_superuser": u.is_superuser,
        }
        for u in eligible_incoming
    ]

    # Eligible outgoing supervisors belonging to current shift
    eligible_outgoing = get_eligible_supervisors_for_shift(
        current_shift,
        timezone.localdate(),
        section_id=None if user.is_superuser else section_id,
        allow_cross_scope=user.is_superuser,
    )
    outgoing_list = [
        {
            "id": u.id,
            "name": u.get_full_name() or u.username,
            "username": u.username,
        }
        for u in eligible_outgoing
    ]

    return JsonResponse({
        "success": True,
        "plant": {
            "id": plant.id,
            "name": plant.name,
            "code": plant.code or "NGH",
        },
        "current_shift": {
            "id": current_shift.id,
            "name": current_shift.name,
            "start_time": current_shift.start_time.strftime("%H:%M"),
            "end_time": current_shift.end_time.strftime("%H:%M"),
            "handover_time": (current_shift.handover_time or current_shift.end_time).strftime("%H:%M"),
        },
        "next_shift": {
            "id": next_shift.id if next_shift else None,
            "name": next_shift.name if next_shift else "Incoming Shift",
            "start_time": next_shift.start_time.strftime("%H:%M") if next_shift else "",
            "end_time": next_shift.end_time.strftime("%H:%M") if next_shift else "",
        } if next_shift else None,
        "current_supervisor": {
            "id": supervisor.id,
            "name": supervisor.get_full_name() or supervisor.username,
            "username": supervisor.username,
        },
        "eligible_outgoing_supervisors": outgoing_list,
        "eligible_incoming_supervisors": incoming_list,
        "apparatus_list": apparatus_list,
        "is_window_active": shift_data.get("is_handover_window_active", False),
        "handover_window_time": shift_data.get("handover_window_time", ""),
    })


@login_required
@require_POST
def shift_handover_submit(request):
    """
    Validates and saves the filled Shift Handing Over form.
    Prepares the data to be immediately opened in the Shift Taking Over (read-only) view.
    Ensures both outgoing and incoming parties belong to their respective shifts.
    """
    user = request.user
    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        data = request.POST

    shift_id = data.get("shift_id")
    next_shift_id = data.get("next_shift_id")
    outgoing_sup_id = data.get("outgoing_supervisor_id") or user.id
    incoming_sup_id = data.get("incoming_supervisor_id")
    handover_notes = (data.get("handover_notes") or "").strip()
    pending_work = (data.get("pending_work") or "").strip()
    apparatus_conditions = data.get("apparatus_conditions") or []

    if not shift_id or not incoming_sup_id:
        return JsonResponse({"success": False, "message": "Shift and Incoming Supervisor are required."}, status=400)

    try:
        current_shift = Shift.objects.get(pk=shift_id)
        next_shift = Shift.objects.get(pk=next_shift_id) if next_shift_id else get_next_shift(
            current_shift,
            current_shift.treatment_plant,
            None if user.is_superuser else user.section_id,
        )
        outgoing_user = User.objects.get(pk=outgoing_sup_id)
        incoming_user = User.objects.get(pk=incoming_sup_id)
    except Exception as e:
        return JsonResponse({"success": False, "message": f"Entity not found: {str(e)}"}, status=400)

    today = timezone.localdate()

    if not user.is_superuser:
        plant_id, section_id = user_plant_section(user)
        valid_shift = bool(
            plant_id
            and section_id
            and current_shift.treatment_plant_id == plant_id
            and current_shift.section_requirements.filter(section_id=section_id).exists()
        )
        valid_next = not next_shift or bool(
            next_shift.treatment_plant_id == plant_id
            and next_shift.section_requirements.filter(section_id=section_id).exists()
        )
        assigned_parties = all(
            person.assigned_treatment_plant_id == plant_id and person.section_id == section_id and not person.is_superuser
            for person in (outgoing_user, incoming_user)
        )
        if not valid_shift or not valid_next or not assigned_parties:
            return JsonResponse({"success": False, "message": "The shift, section, and supervisors must match your assigned scope."}, status=403)

    apparatus_by_id = {}
    if isinstance(apparatus_conditions, list):
        apparatus_ids = {item.get("apparatus_id") for item in apparatus_conditions if item.get("apparatus_id")}
        apparatus_by_id = {
            item.pk: item
            for item in scoped_queryset(Apparatus.objects.select_related("equipment", "equipment__section"), user)
            .filter(pk__in=apparatus_ids)
        }
        if len(apparatus_by_id) != len(apparatus_ids):
            return JsonResponse({"success": False, "message": "An apparatus item is outside your plant or section."}, status=403)
        if any(
            not ShiftSectionRequirement.objects.filter(
                shift_id=current_shift.pk,
                section_id=apparatus.equipment.section_id,
            ).exists()
            for apparatus in apparatus_by_id.values()
        ):
            return JsonResponse({"success": False, "message": "Apparatus must belong to a section configured for this shift."}, status=403)

    # Verify both parties belong to their respective shifts
    if not user_belongs_to_shift(outgoing_user, current_shift, today):
        return JsonResponse({
            "success": False,
            "message": f"Outgoing supervisor {outgoing_user.get_full_name() or outgoing_user.username} is not assigned to {current_shift.name}."
        }, status=400)

    if next_shift and not user_belongs_to_shift(incoming_user, next_shift, today):
        return JsonResponse({
            "success": False,
            "message": f"Incoming supervisor {incoming_user.get_full_name() or incoming_user.username} is not assigned to {next_shift.name}."
        }, status=400)

    # Handover window check (superuser can bypass)
    window_data = check_handover_window(current_shift)
    if not window_data["is_active"] and not user.is_superuser:
        return JsonResponse({
            "success": False,
            "message": f"Shift handover is only permitted during handover hours ({window_data['window_display']})."
        }, status=403)

    with transaction.atomic():
        # Get or create today's handover record for this shift
        handover, created = ShiftHandover.objects.get_or_create(
            shift=current_shift,
            date=today,
            defaults={
                "outgoing_supervisor": outgoing_user,
                "incoming_supervisor": incoming_user,
                "handover_notes": handover_notes,
                "pending_work": pending_work,
                "is_taken_over": False,
            }
        )

        if not created:
            handover.outgoing_supervisor = outgoing_user
            handover.incoming_supervisor = incoming_user
            handover.handover_notes = handover_notes
            handover.pending_work = pending_work
            handover.is_taken_over = False
            handover.save()

        # Save apparatus conditions
        apparatus_summary = []
        if isinstance(apparatus_conditions, list):
            for item in apparatus_conditions:
                app_id = item.get("apparatus_id")
                status = item.get("status", "working")
                remarks = item.get("remarks", "")
                if app_id:
                    app = apparatus_by_id.get(int(app_id))
                    if app:
                        ApparatusCondition.objects.update_or_create(
                            handover=handover,
                            apparatus=app,
                            defaults={"status": status, "remarks": remarks}
                        )
                        apparatus_summary.append({
                            "apparatus_name": app.name,
                            "status": status,
                            "remarks": remarks
                        })

    return JsonResponse({
        "success": True,
        "message": "Shift handover form saved. Proceeding to Shift Taking Over.",
        "handover_id": handover.id,
        "handover_data": {
            "handover_id": handover.id,
            "plant_name": current_shift.treatment_plant.name,
            "shift_name": current_shift.name,
            "next_shift_name": next_shift.name if next_shift else "Incoming Shift",
            "date": today.strftime("%A, %d %B %Y"),
            "handed_over_at": timezone.localtime(handover.handed_over_at).strftime("%H:%M:%S"),
            "outgoing_supervisor": {
                "id": outgoing_user.id,
                "name": outgoing_user.get_full_name() or outgoing_user.username,
                "username": outgoing_user.username,
            },
            "incoming_supervisor": {
                "id": incoming_user.id,
                "name": incoming_user.get_full_name() or incoming_user.username,
                "username": incoming_user.username,
            },
            "handover_notes": handover_notes,
            "pending_work": pending_work,
            "apparatus_summary": apparatus_summary,
        }
    })


@login_required
@require_POST
def shift_takeover_confirm(request):
    """
    Authenticates the incoming supervisor using their username and password,
    verifies shift membership of both parties, and effects the shift transition.
    """
    try:
        data = json.loads(request.body.decode("utf-8")) if request.body else request.POST
    except Exception:
        data = request.POST

    handover_id = data.get("handover_id")
    takeover_notes = (data.get("takeover_notes") or "").strip()
    auth_username = (data.get("username") or "").strip()
    auth_password = data.get("password") or ""

    if not handover_id or not auth_username or not auth_password:
        return JsonResponse({"success": False, "message": "Handover record, username, and password are required."}, status=400)

    try:
        handover = scoped_queryset(
            ShiftHandover.objects.select_related("shift", "shift__treatment_plant", "outgoing_supervisor", "incoming_supervisor"),
            request.user,
        ).get(pk=handover_id)
    except ShiftHandover.DoesNotExist:
        return JsonResponse({"success": False, "message": "Shift Handover record not found."}, status=404)

    # Authenticate credentials
    auth_user = authenticate(request, username=auth_username.upper(), password=auth_password)
    if not auth_user:
        # Try exact username
        auth_user = authenticate(request, username=auth_username, password=auth_password)

    if not auth_user or not auth_user.is_active:
        logger.warning("Shift takeover authentication failed for username=%s", auth_username)
        return JsonResponse({"success": False, "message": "Invalid supervisor username or password. Transition rejected."}, status=401)

    if not request.user.is_superuser:
        plant_id, section_id = user_plant_section(request.user)
        if (
            not plant_id
            or not section_id
            or handover.shift.treatment_plant_id != plant_id
            or not handover.shift.section_requirements.filter(section_id=section_id).exists()
        ):
            return JsonResponse({"success": False, "message": "This handover is outside your assigned plant or section."}, status=403)
        if not auth_user.is_superuser and (
            auth_user.assigned_treatment_plant_id != plant_id or auth_user.section_id != section_id
        ):
            return JsonResponse({"success": False, "message": "The incoming supervisor belongs to another plant or section."}, status=403)

    # Verify that authenticated user matches the designated incoming supervisor (or is superuser)
    if not auth_user.is_superuser and auth_user.id != handover.incoming_supervisor_id:
        return JsonResponse({
            "success": False,
            "message": f"Authenticated user ({auth_user.username}) does not match incoming supervisor ({handover.incoming_supervisor.username})."
        }, status=403)

    today = handover.date
    next_shift = get_next_shift(
        handover.shift,
        handover.shift.treatment_plant,
        None if auth_user.is_superuser else auth_user.section_id,
    )

    # Re-verify that both parties belong to their respective shifts
    if not user_belongs_to_shift(handover.outgoing_supervisor, handover.shift, today):
        return JsonResponse({
            "success": False,
            "message": f"Outgoing supervisor {handover.outgoing_supervisor} does not belong to {handover.shift.name}."
        }, status=400)

    if next_shift and not user_belongs_to_shift(auth_user, next_shift, today):
        return JsonResponse({
            "success": False,
            "message": f"Incoming supervisor {auth_user} does not belong to incoming shift {next_shift.name}."
        }, status=400)

    with transaction.atomic():
        # Effect the shift takeover
        handover.incoming_supervisor = auth_user
        handover.takeover_notes = takeover_notes
        handover.is_taken_over = True
        handover.taken_over_at = timezone.now()
        handover.save()

        # Update active roster assignments to point to new supervisor
        assignment_updates = ShiftAssignment.objects.filter(
            shift=handover.shift,
            date=today,
            is_deleted=False
        )
        if not request.user.is_superuser:
            assignment_updates = assignment_updates.filter(staff__assigned_treatment_plant_id=plant_id, staff__section_id=section_id)
        assignment_updates.update(handover_to=auth_user)

        # Audit log entry
        from django.contrib.contenttypes.models import ContentType
        ct = ContentType.objects.get_for_model(ShiftHandover)
        AuditLog.objects.create(
            user=auth_user,
            action="update",
            content_type=ct,
            object_id=str(handover.id),
            object_repr=str(handover)[:512],
            changes={
                "event": "shift_takeover_effected",
                "notes": f"Shift transition completed. Outgoing: {handover.outgoing_supervisor} -> Incoming: {auth_user}. Next Shift: {next_shift.name if next_shift else 'N/A'}"
            }
        )

    logger.info("Shift takeover effected: Handover %d to supervisor %s", handover.id, auth_user.username)

    return JsonResponse({
        "success": True,
        "message": f"Shift transition successfully effected! {next_shift.name if next_shift else 'New Shift'} is now active under Supervisor {auth_user.get_full_name() or auth_user.username}.",
        "new_shift": {
            "id": next_shift.id if next_shift else handover.shift.id,
            "name": next_shift.name if next_shift else handover.shift.name,
            "start_time": next_shift.start_time.strftime("%H:%M") if next_shift else "",
            "end_time": next_shift.end_time.strftime("%H:%M") if next_shift else "",
        },
        "new_supervisor": {
            "id": auth_user.id,
            "name": auth_user.get_full_name() or auth_user.username,
            "username": auth_user.username,
        }
    })
