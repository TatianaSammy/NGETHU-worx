# ca/services/alerts_service.py
from __future__ import annotations

import logging
from typing import Any
from datetime import date, datetime, time, timedelta
from django.utils import timezone
from django.db import transaction
from django.db.models import Q
from django.apps import apps
from django.contrib.contenttypes.models import ContentType

logger = logging.getLogger(__name__)


class ChemicalAlertsService:
    """
    Orchestrates chemical quality alerts channelled through the alerts app:
    - Queries AlertState, AlertConfig, AlertLog, AlarmAcknowledgment
    - Enforces treatment plant scoping for non-superusers
    - Aggregates chemical workflow exceptions:
      * Out-of-spec test results
      * Missed SLA / overdue chemical analysis requests
      * Expired / pending receiving checklists
      * Batches awaiting chemist review or CoA approval
      * Rejected chemical batches
    - Supports acknowledgment via AlarmAcknowledgment and AlertState
    """

    @staticmethod
    def get_user_alerts(user, plant=None, is_superuser=False, limit=20) -> list[dict[str, Any]]:
        """
        Retrieves active alerts channelled through alerts app,
        scoped to the user and their treatment plant.
        """
        alerts_list = []
        intake_alerts = []
        now = timezone.now()
        is_superuser = bool(is_superuser or user.is_superuser)

        # A shared dashboard notification feed defaults to first plant if none is active
        if plant is None:
            try:
                from t.models import TreatmentPlant
                plant = TreatmentPlant.objects.first()
            except Exception:
                plant = None
        if plant is None:
            return []

        try:
            AlertState = apps.get_model("alerts", "AlertState")
            AlertConfig = apps.get_model("alerts", "AlertConfig")
            AlertNotification = apps.get_model("alerts", "AlertNotification")
            AlarmAcknowledgment = apps.get_model("alerts", "AlarmAcknowledgment")
        except LookupError:
            AlertState = None
            AlertConfig = None
            AlertNotification = None
            AlarmAcknowledgment = None

        # 1. Fetch channelled AlertStates from alerts app
        if AlertState and AlertConfig:
            try:
                state_qs = (
                    AlertState.objects
                    .select_related("alert_config", "alert_config__template", "alert_config__category")
                    .filter(
                        state__in=[
                            AlertState.StateChoices.QUEUED,
                            AlertState.StateChoices.SENT,
                            AlertState.StateChoices.ESCALATED,
                        ]
                    )
                    .order_by("-updated_at")
                )

                recipient_state_ids = []
                if AlertNotification:
                    recipient_state_ids = AlertNotification.objects.filter(
                        recipient=user,
                        acknowledged_at__isnull=True,
                    ).values_list("alert_state_id", flat=True)
                    states_with_inbox_rows = AlertNotification.objects.values_list(
                        "alert_state_id", flat=True
                    )
                    # New events are private per persisted recipient. Legacy
                    # AlertState rows without inbox records retain config scope.
                    state_qs = state_qs.filter(
                        Q(pk__in=recipient_state_ids)
                        | ~Q(pk__in=states_with_inbox_rows)
                    )

                if plant is None:
                    state_qs = state_qs.none()
                else:
                    plant_scope = (
                        Q(geo_context__plant_id=str(plant.pk))
                        | (
                            Q(geo_context__plant_id__isnull=True)
                            & Q(alert_config__target_treatment_plants=plant)
                        )
                        | (
                            Q(geo_context__plant_id__isnull=True)
                            & Q(alert_config__target_treatment_plants__isnull=True)
                            & (
                                Q(alert_config__geo_context__plant_id=str(plant.pk))
                                | Q(alert_config__geo_context__plant_id__isnull=True)
                            )
                        )
                    )
                    state_qs = state_qs.filter(plant_scope)
                if plant is not None and not is_superuser:
                    audience_without_plant = (
                        Q(alert_config__recipients=user)
                        | Q(alert_config__target_groups__in=user.groups.all())
                    )
                    if getattr(user, "section_id", None):
                        audience_without_plant |= Q(alert_config__target_sections__pk=user.section_id)
                    audience = Q(pk__in=recipient_state_ids) | Q(alert_config__target_treatment_plants=plant) | (
                        Q(alert_config__target_treatment_plants__isnull=True)
                        & audience_without_plant
                    )
                    state_qs = state_qs.filter(audience).distinct()

                if AlarmAcknowledgment:
                    state_content_type = ContentType.objects.get_for_model(AlertState)
                    acknowledged_state_ids = AlarmAcknowledgment.objects.filter(
                        user=user,
                        content_type=state_content_type,
                        acknowledged=True,
                    ).values_list("object_id", flat=True)
                    state_qs = state_qs.exclude(pk__in=acknowledged_state_ids)

                for item in state_qs[:limit]:
                    config = item.alert_config
                    if not ChemicalAlertsService._can_view_alert_type(user, config.alert_type if config else "", is_superuser):
                        continue
                    notification = None
                    if AlertNotification:
                        notification = AlertNotification.objects.filter(
                            alert_state=item,
                            recipient=user,
                            acknowledged_at__isnull=True,
                        ).order_by("-sequence", "-created_at").first()
                    subject = (
                        notification.title if notification else
                        item.title or (config.template.subject_template if config and config.template else (config.alert_type or "System Alert"))
                    )
                    message = (
                        notification.message if notification else
                        item.message or f"Alert ref {item.instance_ref} · State: {item.state}"
                    )
                    priority = notification.priority if notification else (item.priority or (config.priority if config else "medium"))
                    alerts_list.append({
                        "id": f"state_{item.pk}",
                        "db_id": item.pk,
                        "type": "alert_state",
                        "title": subject,
                        "message": message,
                        "priority": priority,
                        "severity": (priority or "medium").lower(),
                        "timestamp": item.created_at,
                        "time_ago": ChemicalAlertsService._time_ago(item.created_at, now),
                        "acknowledged": item.state == AlertState.StateChoices.ACKNOWLEDGED,
                        "can_acknowledge": True,
                        "escalated": bool(notification.is_escalation) if notification else item.state == AlertState.StateChoices.ESCALATED,
                        "source": "alerts_app",
                        "url": f"/alerts/",
                    })
            except Exception as e:
                logger.warning(f"Error querying alerts app AlertState: {e}")

        # 2. Chemical analysis operational notifications (channelling CA domain alerts)
        try:
            # QC water-result nonconformities are durable bell notifications.
            # Only expose them to users who can view QC findings, and scope them
            # to the user's assigned plant just like the QC dashboard.
            from qc.models import NonConformity, NonConformityStatus

            can_view_qc_findings = bool(
                user.is_authenticated
                and (
                    is_superuser
                    or user.has_perm("qc.view_nonconformity")
                    or user.has_perm("qc.view_wateranalysis")
                    or user.has_perm("qc.view_rawwaterresult")
                    or user.has_perm("qc.view_treatedwaterresult")
                    or user.has_perm("qc.view_coagulatedwaterresult")
                    or user.has_perm("qc.view_clarifiedwaterresult")
                )
            )
            if AlarmAcknowledgment and can_view_qc_findings:
                nc_content_type = ContentType.objects.get_for_model(NonConformity)
                acknowledged_nc_ids = AlarmAcknowledgment.objects.filter(
                    user=user,
                    content_type=nc_content_type,
                    acknowledged=True,
                ).values_list("object_id", flat=True)
                nc_qs = (
                    NonConformity.objects
                    .filter(status__in=(NonConformityStatus.OPEN, NonConformityStatus.UNDER_REVIEW))
                    .exclude(pk__in=acknowledged_nc_ids)
                    .select_related("analysis", "analysis__treatment_plant")
                    .order_by("-created_at")
                )
                if plant is not None:
                    nc_qs = nc_qs.filter(analysis__treatment_plant=plant)
                elif not (is_superuser or user.is_superuser):
                    nc_qs = nc_qs.none()

                for nc in nc_qs[:10]:
                    alerts_list.append({
                        "id": f"qc_nonconformity_{nc.pk}",
                        "db_id": nc.pk,
                        "type": "qc_nonconformity",
                        "title": "Water quality nonconformity",
                        "message": (
                            f"{nc.parameter_name}: {nc.observed_value} "
                            f"(allowed {nc.min_limit}–{nc.max_limit}) · "
                            f"Analysis {nc.analysis.reference_id}."
                        ),
                        "priority": "high",
                        "severity": "high",
                        "timestamp": nc.created_at,
                        "time_ago": ChemicalAlertsService._time_ago(nc.created_at, now),
                        "acknowledged": False,
                        "source": "qc_nonconformity",
                        "url": "/qc/nonconformities/",
                    })

            from ca.models import (
                ChemicalAnalysisRequest,
                ChemicalBatch,
                BatchTestResult,
                ChemicalCoA,
                ChecklistForWaterTreatmentChemicalAnalysis,
            )

            # Pending, unassigned analysis requests are actionable alerts for
            # users who can assign analysts. They disappear as soon as the
            # request has an analyst, independent of the batch detail view.
            can_assign_analysts = bool(
                user.is_authenticated
                and (
                    is_superuser
                    or user.is_superuser
                    or user.has_perm("ca.create_analysis_request")
                )
            )
            if can_assign_analysts:
                from ca.models import ChemicalWorkflowAlert

                persistent_assignment_requests = ChemicalWorkflowAlert.objects.filter(
                    recipient=user,
                    alert_type="ca_assignment_pending",
                    acknowledged_at__isnull=True,
                    analysis_request__isnull=False,
                ).values_list("analysis_request_id", flat=True)
                received_batches = (
                    ChemicalBatch.objects
                    .filter(
                        status=ChemicalBatch.Status.RECEIVED,
                        created_at__gte=now - timedelta(days=7),
                        analysis_request__status=ChemicalAnalysisRequest.Status.PENDING,
                        analysis_request__assigned_analysts__isnull=True,
                    )
                    .exclude(analysis_request_id__in=persistent_assignment_requests)
                    .select_related("chemical", "analysis_request")
                    .order_by("-created_at")
                )
                if not is_superuser:
                    if plant is None:
                        received_batches = received_batches.none()
                    else:
                        received_batches = received_batches.filter(
                            treatment_plant=plant
                        )
                elif plant is not None:
                    received_batches = received_batches.filter(
                        treatment_plant=plant
                    )

                for batch in received_batches.distinct()[:10]:
                    intake_alerts.append({
                        "id": f"ca_assignment_pending_{batch.analysis_request.pk}",
                        "db_id": batch.analysis_request.pk,
                        "type": "ca_assignment_pending",
                        "title": "Analysis request needs an analyst",
                        "message": (
                            f"Assign an analyst to {batch.chemical.name} "
                            f"batch {batch.batch_number}."
                        ),
                        "priority": "medium",
                        "severity": "medium",
                        "timestamp": batch.created_at,
                        "time_ago": ChemicalAlertsService._time_ago(
                            batch.created_at, now
                        ),
                        "acknowledged": False,
                        "source": "ca_intake",
                        "url": f"/ca/requests/{batch.analysis_request.pk}/assign-analyst/",
                    })

            # A. Overdue Analysis Requests (SLA missed)
            can_view_ca_requests = bool(
                is_superuser
                or user.has_perm("ca.view_chemicalanalysisrequest")
                or user.has_perm("ca.view_chemicalbatch")
                or user.has_perm("ca.view_batchtestresult")
            )
            overdue_reqs = ChemicalAnalysisRequest.objects.filter(
                status__in=["PENDING", "IN_PROGRESS"],
                analysis_deadline__lt=now,
            ).exclude(
                assigned_analysts__isnull=True
            ).select_related(
                "batch",
                "batch__chemical",
                "batch__treatment_plant",
            )
            if AlarmAcknowledgment:
                overdue_reqs = overdue_reqs.exclude(
                    pk__in=ChemicalAlertsService._acknowledged_ids(user, ChemicalAnalysisRequest)
                )

            if not can_view_ca_requests:
                overdue_reqs = overdue_reqs.none()
            if not is_superuser and plant:
                overdue_reqs = overdue_reqs.filter(batch__treatment_plant=plant)
            elif not is_superuser:
                overdue_reqs = overdue_reqs.none()
            elif plant:
                overdue_reqs = overdue_reqs.filter(batch__treatment_plant=plant)

            for req in overdue_reqs[:10]:
                chem_name = req.batch.chemical.name if (req.batch and req.batch.chemical) else "Chemical"
                batch_no = req.batch.batch_number if req.batch else req.request_id
                alerts_list.append({
                    "id": f"ca_overdue_{req.pk}",
                    "db_id": req.pk,
                    "type": "ca_sla",
                    "title": f"Overdue SLA: {chem_name}",
                    "message": f"Analysis request #{req.request_id or req.pk} for batch {batch_no} missed deadline.",
                    "priority": "critical",
                    "severity": "critical",
                    "timestamp": req.analysis_deadline or req.created_at,
                    "time_ago": ChemicalAlertsService._time_ago(req.analysis_deadline or req.created_at, now),
                    "acknowledged": False,
                    "source": "ca_workflow",
                    "url": f"/ca/requests/{req.pk}/",
                })

            # B. Out-of-Spec Test Failures
            can_view_ca_results = bool(
                is_superuser
                or user.has_perm("ca.view_batchtestresult")
                or user.has_perm("ca.view_chemicalbatch")
            )
            failed_tests = BatchTestResult.objects.filter(
                status="FAIL"
            ).select_related("batch", "batch__chemical", "batch__treatment_plant").order_by("-tested_at")
            if AlarmAcknowledgment:
                failed_tests = failed_tests.exclude(
                    pk__in=ChemicalAlertsService._acknowledged_ids(user, BatchTestResult)
                )

            if not can_view_ca_results:
                failed_tests = failed_tests.none()
            if not is_superuser and plant:
                failed_tests = failed_tests.filter(batch__treatment_plant=plant)
            elif not is_superuser:
                failed_tests = failed_tests.none()
            elif plant:
                failed_tests = failed_tests.filter(batch__treatment_plant=plant)

            for test in failed_tests[:8]:
                batch_no = test.batch.batch_number if test.batch else f"#{test.batch_id}"
                chem_name = test.batch.chemical.name if (test.batch and test.batch.chemical) else "Batch"
                alerts_list.append({
                    "id": f"ca_test_{test.pk}",
                    "db_id": test.pk,
                    "type": "ca_out_of_spec",
                    "title": f"Test Failed: {chem_name} ({batch_no})",
                    "message": f"Parameter out-of-spec: value={test.value or test.textual_result}. Auto remark: {test.auto_remark or 'Nonconformity'}",
                    "priority": "high",
                    "severity": "high",
                    "timestamp": test.tested_at or now,
                    "time_ago": ChemicalAlertsService._time_ago(test.tested_at or now, now),
                    "acknowledged": False,
                    "source": "ca_qc",
                    "url": f"/ca/results/{test.pk}/",
                })

            # C. Pending CoA Approval
            can_view_coa = bool(is_superuser or user.has_perm("ca.view_chemicalcoa"))
            pending_coas = ChemicalCoA.objects.filter(
                approved_by__isnull=True
            ).select_related("batch", "batch__chemical", "batch__treatment_plant").order_by("-id")
            if AlarmAcknowledgment:
                pending_coas = pending_coas.exclude(
                    pk__in=ChemicalAlertsService._acknowledged_ids(user, ChemicalCoA)
                )

            if not can_view_coa:
                pending_coas = pending_coas.none()
            if not is_superuser and plant:
                pending_coas = pending_coas.filter(batch__treatment_plant=plant)
            elif not is_superuser:
                pending_coas = pending_coas.none()
            elif plant:
                pending_coas = pending_coas.filter(batch__treatment_plant=plant)

            for coa in pending_coas[:5]:
                alerts_list.append({
                    "id": f"ca_coa_{coa.pk}",
                    "db_id": coa.pk,
                    "type": "ca_coa",
                    "title": f"CoA Pending Approval: #{coa.pk}",
                    "message": f"Certificate for {coa.chemical_name or 'Chemical'} awaits sign-off.",
                    "priority": "medium",
                    "severity": "medium",
                    "timestamp": coa.delivery_date or now,
                    "time_ago": ChemicalAlertsService._time_ago(coa.delivery_date or now, now),
                    "acknowledged": False,
                    "source": "ca_approval",
                    "url": f"/ca/coa/{coa.pk}/",
                })

        except Exception as e:
            logger.warning(f"Error querying CA operational notifications: {e}")

        # Persistent CA workflow escalations are delivered only to their
        # intended recipient and remain plant-scoped even for old alerts.
        try:
            from ca.models import ChemicalWorkflowAlert

            workflow_qs = ChemicalWorkflowAlert.objects.filter(
                recipient=user,
                acknowledged_at__isnull=True,
            ).exclude(
                Q(
                    alert_type__in=(
                        "ca_analysis_assignment",
                        "ca_assignment_pending",
                        "ca_assignment_escalation",
                    ),
                    analysis_request__assigned_analysts__isnull=False,
                )
            ).select_related("batch", "analysis_request")
            interlab_alert_keys = []
            if plant is not None:
                from ca.models import ChemicalLabReferral

                interlab_alert_keys = [
                    f"ca-interlab-referral:{referral_id}"
                    for referral_id in ChemicalLabReferral.objects.filter(
                        treatment_plant_id=plant.pk,
                        status=ChemicalLabReferral.Status.PENDING,
                    ).values_list("pk", flat=True)
                ]
            if not is_superuser:
                if plant is None:
                    workflow_qs = workflow_qs.none()
                else:
                    workflow_qs = workflow_qs.filter(
                        Q(batch__treatment_plant=plant)
                        | Q(alert_type="ca_interlab_referral", dedupe_key__in=interlab_alert_keys)
                    )
            elif plant is not None:
                workflow_qs = workflow_qs.filter(
                    Q(batch__treatment_plant=plant)
                    | Q(alert_type="ca_interlab_referral", dedupe_key__in=interlab_alert_keys)
                )

            for item in workflow_qs[:limit]:
                batch = item.batch
                is_interlab_recipient = bool(
                    item.alert_type == "ca_interlab_referral"
                    and item.dedupe_key in interlab_alert_keys
                )
                if not is_superuser and batch and batch.treatment_plant_id != getattr(plant, "pk", None) and not is_interlab_recipient:
                    continue
                assigned_analyst_ack = bool(
                    is_interlab_recipient
                    or (
                        item.alert_type == "ca_analysis_assignment"
                        and item.analysis_request_id
                        and item.analysis_request.is_assigned_analyst(user)
                    )
                )
                alerts_list.append({
                    "id": f"ca_workflow_alert_{item.pk}",
                    "db_id": item.pk,
                    "type": "ca_workflow_alert",
                    "workflow_alert_type": item.alert_type,
                    "title": item.title,
                    "message": item.message,
                    "priority": item.severity,
                    "severity": item.severity,
                    "timestamp": item.created_at,
                    "time_ago": ChemicalAlertsService._time_ago(item.created_at, now),
                    "acknowledged": False,
                    "can_acknowledge": assigned_analyst_ack,
                    "source": "ca_workflow",
                    "url": item.url,
                })
        except Exception as e:
            logger.warning("Error querying persistent CA workflow notifications: %s", e)

        # One deterministic order for every alert source: severity first, then
        # newest first.  IDs also deduplicate any item emitted by two sources.
        priority_order = {"critical": 0, "high": 1, "medium": 2, "warning": 2, "low": 3, "info": 4}

        def sort_key(item):
            severity = str(item.get("severity", item.get("priority", "medium"))).casefold()
            timestamp = item.get("timestamp")
            if isinstance(timestamp, str):
                from django.utils.dateparse import parse_datetime

                timestamp = parse_datetime(timestamp)
            if isinstance(timestamp, date) and not isinstance(timestamp, datetime):
                timestamp = datetime.combine(timestamp, time.min)
            try:
                if timestamp and timezone.is_naive(timestamp):
                    timestamp = timezone.make_aware(timestamp)
                epoch = timestamp.timestamp() if timestamp else 0
            except (AttributeError, TypeError, ValueError, OverflowError):
                epoch = 0
            return (priority_order.get(severity, 2), -epoch)

        unique_alerts = {}
        for item in intake_alerts + alerts_list:
            alert_id = item.get("id")
            key = str(alert_id) if alert_id is not None else f"anonymous:{len(unique_alerts)}"
            unique_alerts.setdefault(key, item)
        ordered = sorted(unique_alerts.values(), key=sort_key)
        return ordered[:max(0, int(limit))]

    @staticmethod
    def _acknowledged_ids(user, model):
        try:
            AlarmAcknowledgment = apps.get_model("alerts", "AlarmAcknowledgment")
            content_type = ContentType.objects.get_for_model(model)
            return AlarmAcknowledgment.objects.filter(
                user=user,
                content_type=content_type,
                acknowledged=True,
            ).values_list("object_id", flat=True)
        except Exception:
            return []

    @staticmethod
    def _can_view_alert_type(user, alert_type, is_superuser=False):
        if is_superuser or user.is_superuser:
            return True
        all_permissions = user.get_all_permissions()
        alert_type = str(alert_type or "").lower()
        if alert_type in {"nonconformity", "performance_deviation"}:
            required = {
                "qc.view_nonconformity", "qc.view_wateranalysis",
                "qc.view_rawwaterresult", "qc.view_treatedwaterresult",
                "qc.view_coagulatedwaterresult", "qc.view_clarifiedwaterresult",
            }
            return bool(all_permissions.intersection(required))
        if alert_type in {"sampling_delay", "analysis_missed", "verification_pending"}:
            return any(permission in all_permissions for permission in (
                "qc.view_wateranalysis", "destw.view_wastewateranalysis",
                "destw.view_wastewaterpondanalysis", "kstw.view_wastewateranalysis",
                "kstw.view_industrialsampleanalysis", "kstw.view_treatmentplantsampleanalysis",
                "kstw.view_exhaustersampleanalysis",
            ))
        if alert_type == "maintenance_due":
            return any(permission.startswith("maintenance.view_") for permission in all_permissions)
        return user.has_perm("alerts.view_alertstate") or user.alert_configs.filter(alert_type=alert_type).exists()

    @staticmethod
    def acknowledge(user, alert_id: str, is_superuser=False) -> dict[str, Any]:
        """
        Marks an alert as acknowledged in the alerts app.
        Supports both AlertState and CA alert IDs.
        """
        now = timezone.now()
        try:
            AlertState = apps.get_model("alerts", "AlertState")
            AlertNotification = apps.get_model("alerts", "AlertNotification")
            AlarmAcknowledgment = apps.get_model("alerts", "AlarmAcknowledgment")
        except LookupError:
            AlertState = None
            AlertNotification = None
            AlarmAcknowledgment = None

        if alert_id.startswith("state_"):
            pk = alert_id.replace("state_", "")
            try:
                from alerts.engine import AlertEngine
                return AlertEngine.acknowledge_alert(user, pk)
            except Exception as e:
                return {"success": False, "error": str(e)}

        if alert_id.startswith("ca_batch_received_") and AlarmAcknowledgment:
            try:
                from ca.models import ChemicalBatch

                batch_id = alert_id.removeprefix("ca_batch_received_")
                batch = ChemicalBatch.objects.get(pk=batch_id)
                can_view_batch = (
                    is_superuser
                    or user.is_superuser
                    or user.has_perm("ca.view_chemicalbatch")
                    or user.has_perm("ca.receive_chemical_batch")
                )
                if not can_view_batch or not (
                    is_superuser
                    or user.is_superuser
                    or batch.treatment_plant_id
                    == getattr(user, "assigned_treatment_plant_id", None)
                ):
                    return {"success": False, "error": "Notification not found."}
                content_type = ContentType.objects.get_for_model(batch)
                AlarmAcknowledgment.objects.update_or_create(
                    user=user,
                    content_type=content_type,
                    object_id=batch.pk,
                    defaults={
                        "acknowledged": True,
                        "acknowledged_at": now,
                    },
                )
                return {"success": True, "message": "Batch notification acknowledged."}
            except Exception as e:
                return {"success": False, "error": str(e)}

        if alert_id.startswith("qc_nonconformity_") and AlarmAcknowledgment:
            try:
                from qc.models import NonConformity

                nc_id = alert_id.removeprefix("qc_nonconformity_")
                nc = NonConformity.objects.select_related("analysis").get(pk=nc_id)
                can_view_qc = (
                    is_superuser
                    or user.is_superuser
                    or user.has_perm("qc.view_nonconformity")
                    or user.has_perm("qc.view_wateranalysis")
                    or user.has_perm("qc.view_rawwaterresult")
                    or user.has_perm("qc.view_treatedwaterresult")
                    or user.has_perm("qc.view_coagulatedwaterresult")
                    or user.has_perm("qc.view_clarifiedwaterresult")
                )
                if not can_view_qc or not (
                    is_superuser
                    or user.is_superuser
                    or nc.analysis.treatment_plant_id
                    == getattr(user, "assigned_treatment_plant_id", None)
                ):
                    return {"success": False, "error": "Notification not found."}
                content_type = ContentType.objects.get_for_model(nc)
                AlarmAcknowledgment.objects.update_or_create(
                    user=user,
                    content_type=content_type,
                    object_id=nc.pk,
                    defaults={"acknowledged": True, "acknowledged_at": now},
                )
                return {"success": True, "message": "Water nonconformity acknowledged."}
            except Exception as e:
                return {"success": False, "error": str(e)}

        alert_model_prefixes = (
            ("ca_overdue_", "ChemicalAnalysisRequest", "ca.view_chemicalanalysisrequest"),
            ("ca_test_", "BatchTestResult", "ca.view_batchtestresult"),
            ("ca_coa_", "ChemicalCoA", "ca.view_chemicalcoa"),
        )
        for prefix, model_name, permission in alert_model_prefixes:
            if not alert_id.startswith(prefix) or not AlarmAcknowledgment:
                continue
            try:
                from ca import models as ca_models

                model = getattr(ca_models, model_name)
                can_view = is_superuser or user.is_superuser or user.has_perm(permission)
                if model_name in {"ChemicalAnalysisRequest", "BatchTestResult"}:
                    can_view = can_view or user.has_perm("ca.view_chemicalbatch")
                if not can_view:
                    return {"success": False, "error": "Notification not found."}
                obj = model.objects.select_related("batch", "batch__treatment_plant").get(pk=alert_id.removeprefix(prefix))
                batch = getattr(obj, "batch", None)
                if not (is_superuser or user.is_superuser) and (
                    batch is None or batch.treatment_plant_id != getattr(user, "assigned_treatment_plant_id", None)
                ):
                    return {"success": False, "error": "Notification not found."}
                content_type = ContentType.objects.get_for_model(model)
                AlarmAcknowledgment.objects.update_or_create(
                    user=user,
                    content_type=content_type,
                    object_id=obj.pk,
                    defaults={"acknowledged": True, "acknowledged_at": now},
                )
                return {"success": True, "message": "Notification acknowledged."}
            except Exception as e:
                return {"success": False, "error": str(e)}

        if alert_id.startswith("ca_workflow_alert_"):
            try:
                from ca.models import ChemicalWorkflowAlert

                item = ChemicalWorkflowAlert.objects.select_related(
                    "batch",
                    "analysis_request",
                ).get(
                    pk=alert_id.removeprefix("ca_workflow_alert_"),
                    recipient=user,
                    acknowledged_at__isnull=True,
                )
                if not (is_superuser or user.is_superuser):
                    if item.alert_type == "ca_interlab_referral":
                        from ca.models import ChemicalLabReferral

                        try:
                            referral_id = int(str(item.dedupe_key).rsplit(":", 1)[-1])
                            target_plant_id = ChemicalLabReferral.objects.values_list(
                                "treatment_plant_id", flat=True
                            ).get(pk=referral_id, status=ChemicalLabReferral.Status.PENDING)
                        except (TypeError, ValueError, ChemicalLabReferral.DoesNotExist):
                            return {"success": False, "error": "Notification not found."}
                        if target_plant_id != getattr(user, "assigned_treatment_plant_id", None):
                            return {"success": False, "error": "Notification not found."}
                    elif item.batch_id and item.batch.treatment_plant_id != getattr(user, "assigned_treatment_plant_id", None):
                        return {"success": False, "error": "Notification not found."}
                if item.alert_type == "ca_analysis_assignment":
                    request = item.analysis_request
                    if request is None or not request.is_assigned_analyst(user):
                        return {"success": False, "error": "Notification not found."}
                    assignment_alerts = ChemicalWorkflowAlert.objects.filter(
                        analysis_request=request,
                        alert_type="ca_analysis_assignment",
                        dedupe_key=item.dedupe_key,
                        acknowledged_at__isnull=True,
                    )
                    for assignment_alert in assignment_alerts.iterator():
                        assignment_alert.acknowledge(actor=user)
                    return {
                        "success": True,
                        "message": "Assignment acknowledged.",
                        "redirect_url": item.url,
                    }
                item.acknowledge(actor=user)
                return {"success": True, "message": "Workflow notification acknowledged."}
            except Exception:
                return {"success": False, "error": "Notification not found."}

        return {"success": False, "error": "Unsupported notification."}

    @staticmethod
    def _can_ack_alert_state(user, state, is_superuser=False):
        if is_superuser or user.is_superuser:
            return True
        plant = getattr(user, "assigned_treatment_plant", None)
        if plant is None or not ChemicalAlertsService._can_view_alert_type(user, state.alert_config.alert_type, False):
            return False
        config = state.alert_config
        targeted_plants = list(config.target_treatment_plants.values_list("pk", flat=True))
        if targeted_plants and plant.pk not in targeted_plants:
            return False
        context_plant = (state.geo_context or {}).get("plant_id")
        if context_plant and str(context_plant) != str(plant.pk):
            return False
        try:
            is_notification_recipient = state.notifications.filter(recipient=user).exists()
        except Exception:
            is_notification_recipient = False
        return bool(
            is_notification_recipient
            or
            config.recipients.filter(pk=user.pk).exists()
            or config.target_groups.filter(pk__in=user.groups.values("pk")).exists()
            or (getattr(user, "section_id", None) and config.target_sections.filter(pk=user.section_id).exists())
            or plant.pk in targeted_plants
        )

    @staticmethod
    def _time_ago(dt, now) -> str:
        if not dt:
            return "Just now"
        try:
            if hasattr(dt, "tzinfo") and dt.tzinfo is None:
                dt = timezone.make_aware(dt)
            diff = now - dt
            seconds = int(diff.total_seconds())
            if seconds < 0:
                return "Just now"
            if seconds < 60:
                return f"{seconds}s ago"
            if seconds < 3600:
                return f"{seconds // 60}m ago"
            if seconds < 86400:
                return f"{seconds // 3600}h ago"
            return f"{seconds // 86400}d ago"
        except Exception:
            return "Recently"
