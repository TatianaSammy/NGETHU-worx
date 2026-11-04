from django.apps import AppConfig


class WeeklytasksConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'weeklytasks'

    def ready(self):
        from . import signals
