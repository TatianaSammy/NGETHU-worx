from django.apps import AppConfig


class CentralLabConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "central_lab"

    def ready(self):
        import central_lab.signals
