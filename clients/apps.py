from django.apps import AppConfig


class ClientsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'clients'

    def ready(self):
        # Реферальные начисления ловят сохранение чека и смену ставки (D-95).
        from . import signals  # noqa: F401
