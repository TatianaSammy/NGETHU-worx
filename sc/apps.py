# sc/apps.py
from django.apps import AppConfig
import sys

class ScConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'sc'
    verbose_name = 'Shift Work Management'

    def ready(self):
        # Avoid loading signals during migrations
        if 'migrate' in sys.argv:
            return
        from . import signals  # noqa
