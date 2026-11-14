# alerts/registry.py

import logging
import re
from typing import Any, Dict

from django.apps import apps
from django.db.models import QuerySet

from core.registry import register_model

logger = logging.getLogger(__name__)

APP_LABEL = "alerts"


# ==========================================================
# SAFE MODEL RESOLUTION
# ==========================================================
def get_model(name: str):
    """
    Lazy model resolver.
    Prevents circular imports during app loading.
    """
    return apps.get_model(APP_LABEL, name)


# ==========================================================
# MODEL REFERENCES
# ==========================================================
AlertTemplate = get_model("AlertTemplate")
AlertCategory = get_model("AlertCategory")
AlertConfig = get_model("AlertConfig")
AlertState = get_model("AlertState")
AlertNotification = get_model("AlertNotification")
AlarmAcknowledgment = get_model("AlarmAcknowledgment")
AlertLog = get_model("AlertLog")
UserAlertPreference = get_model("UserAlertPreference")
VoiceAlert = get_model("VoiceAlert")


# ==========================================================
# ICON MAP
# ==========================================================
ICONS = {
    "AlertTemplate": "file-alt",
    "AlertCategory": "tags",
    "AlertConfig": "bell",
    "AlertState": "stream",
    "AlertNotification": "envelope-open-text",
    "AlarmAcknowledgment": "check-circle",
    "AlertLog": "scroll",
    "UserAlertPreference": "sliders-h",
    "VoiceAlert": "volume-high",
}


# ==========================================================
# GROUP MAP
# ==========================================================
GROUPS = {
    "AlertTemplate": "templates",
    "AlertCategory": "taxonomy",
    "AlertConfig": "alerts",
    "AlertState": "alerts",
    "AlertNotification": "alerts",
    "AlarmAcknowledgment": "audit",
    "AlertLog": "audit",
    "UserAlertPreference": "users",
    "VoiceAlert": "voice",
}


# ==========================================================
# DISPLAY HELPERS
# ==========================================================
def humanize_model_name(model) -> str:
    return re.sub(
        r"(?<!^)(?=[A-Z])",
        " ",
        model.__name__,
    ).strip()


def build_meta(model) -> Dict[str, Any]:
    verbose = humanize_model_name(model)

    return {
        "key": f"{model._meta.app_label}.{model._meta.model_name}",
        "app_label": model._meta.app_label,
        "model_name": model._meta.model_name,
        "verbose_name": verbose,
        "verbose_name_plural": f"{verbose}s",
    }


# ==========================================================
# QUERYSET FACTORIES
# ==========================================================
def template_qs(model) -> QuerySet:
    return model.objects.all()


def category_qs(model) -> QuerySet:
    return model.objects.select_related("parent")


def config_qs(model) -> QuerySet:
    return model.objects.select_related(
        "category",
        "template",
        "escalate_to",
    ).prefetch_related(
        "recipients",
        "target_sections",
        "target_groups",
        "target_treatment_plants",
    )


def state_qs(model) -> QuerySet:
    return model.objects.select_related("alert_config")


def ack_qs(model) -> QuerySet:
    return model.objects.select_related("user", "content_type")


def log_qs(model) -> QuerySet:
    return model.objects.select_related("alert", "content_type")


def pref_qs(model) -> QuerySet:
    return model.objects.select_related("user")


def notification_qs(model) -> QuerySet:
    return model.objects.select_related(
        "recipient",
        "alert_state",
        "alert_state__alert_config",
    )


def voice_alert_qs(model) -> QuerySet:
    return model.objects.select_related(
        "alert_config",
        "alert_state",
        "speaker_user",
    )


# ==========================================================
# SHARED CONFIG
# ==========================================================
CONFIG_BASE = {
    "search_fields": [
        "alert_type",
        "priority",
    ],
    "filter_fields": [
        "alert_type",
        "priority",
        "active",
    ],
    "ordering": [
        "alert_type",
    ],
}


# ==========================================================
# REGISTRATION WRAPPER
# ==========================================================
def safe_register(model, **kwargs):
    """
    Registry normalization layer.
    """

    if model is None:
        raise RuntimeError("Model resolution failed.")

    meta = kwargs.get("meta", {})
    meta.update(build_meta(model))
    kwargs["meta"] = meta

    ui = kwargs.get("ui", {})
    ui.setdefault(
        "icon",
        ICONS.get(model.__name__, "bell"),
    )
    ui.setdefault(
        "group",
        GROUPS.get(model.__name__, "alerts"),
    )

    kwargs["ui"] = ui

    kwargs.pop("templates", None)

    try:
        return register_model(**kwargs)(model)
    except Exception:
        logger.exception(
            "[alerts.registry] Failed registering %s",
            model,
        )
        raise


# ==========================================================
# CORE MODELS
# ==========================================================
safe_register(
    AlertTemplate,
    search_fields=["name", "subject_template"],
    ordering=["name"],
    inline_edit_fields=["name", "version", "active"],
    queryset_factory=template_qs,
)

safe_register(
    AlertCategory,
    search_fields=["name"],
    filter_fields=["parent"],
    ordering=["name"],
    select_related=["parent"],
    queryset_factory=category_qs,
)

safe_register(
    AlertConfig,
    search_fields=["alert_type", "priority"],
    filter_fields=["alert_type", "priority", "voice_persistence_mode", "active"],
    ordering=["alert_type"],
    select_related=[
        "category",
        "template",
        "escalate_to",
    ],
    prefetch_related=[
        "recipients",
        "target_sections",
        "target_groups",
        "target_treatment_plants",
    ],
    queryset_factory=config_qs,
    inline_edit_fields=[
        "alert_type",
        "priority",
        "voice_persistence_mode",
        "voice_repeat_interval_seconds",
        "voice_repeat_max_count",
        "active",
        "last_triggered",
    ],
)

safe_register(
    AlertState,
    search_fields=["state", "instance_ref"],
    filter_fields=["state", "alert_config", "repeat_due_at"],
    ordering=["-updated_at"],
    select_related=["alert_config"],
    queryset_factory=state_qs,
)

safe_register(
    AlarmAcknowledgment,
    search_fields=["user"],
    filter_fields=["acknowledged", "verified"],
    ordering=["-timestamp"],
    select_related=["user", "content_type"],
    queryset_factory=ack_qs,
)

safe_register(
    AlertLog,
    search_fields=["alert"],
    ordering=["-timestamp"],
    select_related=["alert", "content_type"],
    queryset_factory=log_qs,
)

safe_register(
    UserAlertPreference,
    search_fields=["user"],
    filter_fields=["receive_email", "receive_sms", "receive_push"],
    ordering=["user"],
    select_related=["user"],
    queryset_factory=pref_qs,
)

safe_register(
    AlertNotification,
    search_fields=["title", "message", "recipient__username", "recipient__email"],
    filter_fields=["delivery_status", "is_escalation", "priority"],
    ordering=["-created_at"],
    select_related=["recipient", "alert_state", "alert_state__alert_config"],
    queryset_factory=notification_qs,
)

safe_register(
    VoiceAlert,
    search_fields=["title", "message", "speaker_user__username"],
    filter_fields=["is_played", "repeat_due_at"],
    ordering=["-created_at"],
    select_related=["alert_config", "alert_state", "speaker_user"],
    queryset_factory=voice_alert_qs,
)


# ==========================================================
# DEBUG
# ==========================================================
try:
    from core.registry import registry

    logger.info(
        "[alerts.registry] loaded app=%s models=%s",
        APP_LABEL,
        len(registry._registry),
    )

except Exception:
    logger.exception(
        "[alerts.registry] registry unavailable during import"
    )
