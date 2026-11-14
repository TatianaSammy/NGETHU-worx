# t/apps.py
from django.apps import AppConfig
import importlib
import logging

logger = logging.getLogger(__name__)


class TConfig(AppConfig):
    name = "t"

    def ready(self):
        # Connect TreatedWaterLimit signals to invalidate the WQI limit cache
        try:
            from t.signals import connect_signals
            connect_signals()
        except Exception as exc:
            logger.warning("[t.apps] Could not connect WQI signals: %s", exc)

