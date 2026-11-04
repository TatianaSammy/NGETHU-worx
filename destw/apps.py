from django.apps import AppConfig


class DestwConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'destw'

    def ready(self):
        from . import signals
