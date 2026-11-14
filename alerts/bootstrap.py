import logging
from datetime import timedelta
from django.apps import apps
from django.utils import timezone
from django.db import transaction

logger = logging.getLogger(__name__)


def bootstrap_alerts_system():
    """
    Initializes default VoiceOver profiles, AlertTemplates, AlertCategories,
    and AlertConfigs so that alerts, bell notifications, and voice messages
    are always operational out-of-the-box.
    """
    try:
        User = apps.get_model("accounts", "CustomUser")
        AlertTemplate = apps.get_model("alerts", "AlertTemplate")
        AlertCategory = apps.get_model("alerts", "AlertCategory")
        AlertConfig = apps.get_model("alerts", "AlertConfig")
    except LookupError as e:
        logger.warning(f"[AlertsBootstrap] Required models not loaded: {e}")
        return

    admin_user = (
        User.objects.filter(is_superuser=True).first()
        or User.objects.first()
    )
    if not admin_user:
        logger.info("[AlertsBootstrap] No admin user found yet to attach alert templates.")
        return

    with transaction.atomic():
        # 1. Ensure default Categories
        cat_qc, _ = AlertCategory.objects.get_or_create(
            name="Water Quality Control",
            defaults={"description": "Notifications related to water analysis and sampling parameters."},
        )
        cat_maint, _ = AlertCategory.objects.get_or_create(
            name="Equipment & Maintenance",
            defaults={"description": "Equipment calibration, maintenance, and facility alerts."},
        )

        # 3. Ensure default AlertTemplates
        templates_def = [
            {
                "name": "Water Quality Nonconformity",
                "subject": "Water Quality Nonconformity: {parameter_name}",
                "body": "Quality parameter nonconformity detected: {parameter_name} measured {observed_value} outside limits ({min_limit}–{max_limit}).",
            },
            {
                "name": "Water Analysis Auto Verification",
                "subject": "Water Analysis Verified: {reference_id}",
                "body": "Water analysis {reference_id} has been verified and confirmed within standards.",
            },
            {
                "name": "Equipment Maintenance Due",
                "subject": "Equipment Maintenance Due: {name}",
                "body": "Equipment {name} has scheduled maintenance due. Please perform required servicing.",
            },
            {
                "name": "Equipment Calibration Pending",
                "subject": "Calibration Due: {name}",
                "body": "Equipment {name} calibration is due today. Verification pending.",
            },
            {
                "name": "Scheduled Analysis Generated",
                "subject": "Scheduled Analysis: {reference_id}",
                "body": "A scheduled water analysis batch {reference_id} has been created and awaits testing.",
            },
            {
                "name": "Chemical Workflow Alert",
                "subject": "Chemical Quality Alert",
                "body": "Chemical analysis SLA or quality specification alert requires attention.",
            },
        ]

        template_map = {}
        for tdef in templates_def:
            tmpl, _ = AlertTemplate.objects.get_or_create(
                name=tdef["name"],
                version=1,
                defaults={
                    "subject_template": tdef["subject"],
                    "body_template": tdef["body"],
                    "active": True,
                    "auto_voice_enabled": True,
                },
            )
            template_map[tdef["name"]] = tmpl

        # 4. Ensure default AlertConfigs
        configs_def = [
            {
                "alert_type": "nonconformity",
                "category": cat_qc,
                "priority": "critical",
                "template": template_map["Water Quality Nonconformity"],
            },
            {
                "alert_type": "auto_verification",
                "category": cat_qc,
                "priority": "low",
                "template": template_map["Water Analysis Auto Verification"],
            },
            {
                "alert_type": "maintenance_due",
                "category": cat_maint,
                "priority": "high",
                "template": template_map["Equipment Maintenance Due"],
            },
            {
                "alert_type": "verification_pending",
                "category": cat_maint,
                "priority": "high",
                "template": template_map["Equipment Calibration Pending"],
            },
            {
                "alert_type": "sampling_delay",
                "category": cat_qc,
                "priority": "medium",
                "template": template_map["Scheduled Analysis Generated"],
            },
            {
                "alert_type": "analysis_missed",
                "category": cat_qc,
                "priority": "high",
                "template": template_map["Scheduled Analysis Generated"],
            },
            {
                "alert_type": "ca_assignment_pending",
                "category": cat_qc,
                "priority": "medium",
                "template": template_map["Chemical Workflow Alert"],
                "voice_persistence_mode": AlertConfig.VoicePersistenceMode.UNTIL_ANALYST_ASSIGNED,
            },
            {
                "alert_type": "ca_assignment_escalation",
                "category": cat_qc,
                "priority": "high",
                "template": template_map["Chemical Workflow Alert"],
                "voice_persistence_mode": AlertConfig.VoicePersistenceMode.UNTIL_ANALYST_ASSIGNED,
            },
        ]

        for cdef in configs_def:
            cfg, created = AlertConfig.objects.get_or_create(
                alert_type=cdef["alert_type"],
                defaults={
                    "category": cdef["category"],
                    "priority": cdef["priority"],
                    "template": cdef["template"],
                    "verify_timeout": timedelta(minutes=30),
                    "verify_before_subsequent": timedelta(minutes=15),
                    "approve_timeout": timedelta(minutes=30),
                    "tat_for_escalation": timedelta(hours=1),
                    "escalation_timeout": timedelta(hours=2),
                    "voice_persistence_mode": cdef.get(
                        "voice_persistence_mode",
                        AlertConfig.VoicePersistenceMode.UNTIL_ACKNOWLEDGED,
                    ),
                    "voice_repeat_interval_seconds": 60,
                    "voice_repeat_max_count": 0,
                    "active": True,
                },
            )
            # Ensure admin / all staff users are in recipients
            if created or cfg.recipients.count() == 0:
                cfg.recipients.add(admin_user)

    logger.info("[AlertsBootstrap] Alerts bootstrap successfully finished.")
