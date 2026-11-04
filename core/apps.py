# core/apps.py

from __future__ import annotations

import logging
import os
import sys
import threading
import time

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class CoreConfig(AppConfig):
    """
    Core application bootstrap.

    Startup order

        1. Discover model registries

    Scheduling is handled entirely by django-celery-beat.
    No custom Beat registry, loader or synchronization is performed.
    """

    name = "core"
    verbose_name = "Core"

    _initialized = False
    _lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def ready(self) -> None:
        cls = self.__class__

        # These small, idempotent hooks establish request-scoped superuser
        # authorization in Django, the admin, DRF, deletion Collector, and
        # Viewflow before optional startup work is considered.
        from core.engine.superuser_authorization import install_superuser_authorization

        install_superuser_authorization()

        from core.admin_enhancements import register_local_admin_models

        register_local_admin_models(os.path.dirname(os.path.dirname(__file__)))

        # Register post-commit live refresh events even for lightweight
        # management startup modes; registration itself performs no I/O.
        from core.realtime_signals import register_realtime_signals

        register_realtime_signals()

        if cls._initialized:
            return

        if not cls._lock.acquire(blocking=False):
            return

        started = time.perf_counter()

        try:
            if cls._initialized:
                return

            if self._skip_startup():
                return

            logger.info("=" * 80)
            logger.info("[Core] Starting application initialization")
            logger.info("=" * 80)

            self._load_model_registries()

            cls._initialized = True

            logger.info(
                "[Core] Initialization completed in %.3fs",
                time.perf_counter() - started,
            )

            logger.info("=" * 80)

        except Exception:
            logger.exception("[Core] Initialization failed.")
            raise

        finally:
            cls._lock.release()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _skip_startup(self) -> bool:
        """
        Skip expensive initialization during management commands
        that don't require runtime bootstrap.
        """

        if os.environ.get("DJANGO_RUN_MODE") == "MIGRATE":
            logger.info(
                "[Core] Migration mode detected. Startup skipped."
            )
            return True

        management_commands = {
            "makemigrations",
            "migrate",
            "collectstatic",
            "compilemessages",
            "test",
        }

        if any(cmd in sys.argv for cmd in management_commands):
            logger.info(
                "[Core] Management command detected. "
                "Skipping runtime initialization."
            )
            return True

        return False

    def _load_model_registries(self) -> None:
        """
        Auto-discover all model registries.
        """

        from core.registry_loader import autodiscover_registry

        autodiscover_registry()

        logger.info("[Core] Model registries loaded.")
