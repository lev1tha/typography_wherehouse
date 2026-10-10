"""Небезопасные значения по умолчанию.

Настройки читают окружение с запасными значениями «для разработки»
(DEBUG=True, ALLOWED_HOSTS=*, SECRET_KEY «django-insecure…», FINANCE_PASSWORD
«finance123», шлюз mock). Забыть переменную на сервере легко — и система молча
стартует в открытом виде.

Ронять старт из-за этого НЕЛЬЗЯ: прод может опираться на такие значения, и
контейнер ушёл бы в бесконечный перезапуск. Поэтому две вещи:
  * при старте вне DEBUG — громкий ERROR в лог (виден в `docker logs`);
  * `manage.py check --deploy` — те же проблемы как ошибки системной проверки.
"""
import logging

from django.conf import settings
from django.core import checks

logger = logging.getLogger("config.security")

DEFAULT_FINANCE_PASSWORD = "finance123"


def insecure_defaults():
    """[(id, level, текст)] — что в текущих настройках выглядит как забытое."""
    problems = []
    if str(settings.SECRET_KEY).startswith("django-insecure"):
        problems.append((
            "cloude.E001", checks.ERROR,
            "SECRET_KEY — значение по умолчанию («django-insecure…»): подписи "
            "сессий и токенов может подделать любой, кто видел исходники. "
            "Задайте случайный SECRET_KEY в окружении.",
        ))
    if "*" in (settings.ALLOWED_HOSTS or []):
        problems.append((
            "cloude.E002", checks.ERROR,
            "ALLOWED_HOSTS содержит «*» — сервер отвечает на любой Host. "
            "Перечислите домены (ALLOWED_HOSTS=chpucenter.com,www.chpucenter.com).",
        ))
    if settings.FINANCE_PASSWORD == DEFAULT_FINANCE_PASSWORD:
        problems.append((
            "cloude.E003", checks.ERROR,
            "FINANCE_PASSWORD — значение по умолчанию («finance123»): финансовые "
            "экраны защищены общеизвестным паролем. Задайте свой в окружении.",
        ))
    if (settings.PAYMENT_GATEWAY or "mock").lower() == "mock":
        problems.append((
            "cloude.W004", checks.WARNING,
            "PAYMENT_GATEWAY=mock — вне DEBUG онлайн-оплата не принимается "
            "(вебхук заглушки отклоняется). Для боевых платежей задайте шлюз и ключи.",
        ))
    return problems


@checks.register(checks.Tags.security, deploy=True)
def check_insecure_defaults(app_configs, **kwargs):
    return [
        (checks.Error if level == checks.ERROR else checks.Warning)(msg, id=check_id)
        for check_id, level, msg in insecure_defaults()
    ]


def log_insecure_defaults():
    """Вне DEBUG — громкий ERROR при старте. Старт не прерываем."""
    if settings.DEBUG:
        return
    for check_id, level, msg in insecure_defaults():
        log = logger.error if level == checks.ERROR else logger.warning
        log("НЕБЕЗОПАСНЫЕ НАСТРОЙКИ [%s]: %s", check_id, msg)
