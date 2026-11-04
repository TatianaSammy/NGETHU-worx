from django.apps import AppConfig


class MicrobialConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'microbial'

    def ready(self):
        import microbial.workflow_signals
