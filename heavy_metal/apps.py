from django.apps import AppConfig

class HeavyMetalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "heavy_metal"

    def ready(self):
        import heavy_metal.signals
