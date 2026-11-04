from django.apps import AppConfig


class KstwConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'kstw'

    def ready(self):
        from . import signals
