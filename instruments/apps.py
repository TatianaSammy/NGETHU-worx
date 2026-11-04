from django.apps import AppConfig


class InstrumentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'instruments'

    def ready(self):
        from . import signals
