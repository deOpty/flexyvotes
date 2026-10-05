from django.apps import AppConfig


class ElectionsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'elections'
    verbose_name = 'Elections & ballots'

    def ready(self):
        from . import signals  # noqa: F401
