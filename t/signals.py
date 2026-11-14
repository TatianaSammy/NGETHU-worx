"""t/signals.py

Post-save / post-delete signals for TreatedWaterLimit.
Invalidates the in-process WQI plant-limit cache whenever a limit record
is created, changed, or removed so the next WQI calculation picks up the
latest values from the database.
"""
import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


def _invalidate_wqi_cache(instance, **kwargs):
    """Invalidate the WQI limit cache for the affected treatment plant."""
    try:
        from qc.services.wqi_service import invalidate_plant_limit_cache
        plant_id = getattr(instance, "treatment_plant_id", None)
        invalidate_plant_limit_cache(plant_id)
        logger.debug(
            "[t.signals] WQI limit cache invalidated for plant_id=%s after %s change.",
            plant_id,
            instance.__class__.__name__,
        )
    except Exception as exc:
        logger.warning("[t.signals] Could not invalidate WQI cache: %s", exc)


def connect_signals():
    """Connect TreatedWaterLimit signals. Called from TConfig.ready()."""
    from t.models import TreatedWaterLimit  # local import avoids app registry issues

    post_save.connect(_invalidate_wqi_cache, sender=TreatedWaterLimit, weak=False)
    post_delete.connect(_invalidate_wqi_cache, sender=TreatedWaterLimit, weak=False)

