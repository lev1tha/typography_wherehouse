import sys

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        from . import signals  # noqa: F401
        from config import checks  # noqa: F401  (регистрирует проверки `check --deploy`)

        # Громкий ERROR при старте, если вне DEBUG остались значения «для
        # разработки». Тесты и сама проверка (`check`) пишут о них по-своему.
        if not (len(sys.argv) > 1 and sys.argv[1] in ("test", "check")):
            checks.log_insecure_defaults()
