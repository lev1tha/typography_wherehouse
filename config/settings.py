"""
Django settings for the Cloude warehouse & sales system.
"""

import sys
from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, True),
    SECRET_KEY=(str, "django-insecure-change-me-in-production"),
    ALLOWED_HOSTS=(list, ["*"]),
    CORS_ALLOWED_ORIGINS=(list, ["http://localhost:5710", "http://127.0.0.1:5710"]),
    # Прод: домены, которым доверяем CSRF-проверку (со схемой), напр.
    # https://chpucenter.com,https://www.chpucenter.com
    CSRF_TRUSTED_ORIGINS=(list, []),
    # Пусто = SQLite (разработка). В проде — postgres://user:pass@db:5432/имя
    DATABASE_URL=(str, ""),
    # Редирект на HTTPS средствами Django. По умолчанию выключен: в нашей схеме
    # 301 делает nginx, а Django стоит за прокси (двойной редирект = петля).
    SECURE_SSL_REDIRECT=(bool, False),
    # Integrations — real tokens are read from the environment (.env / shell).
    TELEGRAM_STAFF_BOT_TOKEN=(str, ""),
    TELEGRAM_STAFF_CHAT_IDS=(list, []),
    TELEGRAM_CUSTOMER_BOT_TOKEN=(str, ""),
    # Секрет вебхука Telegram: тот же, что передаётся в setWebhook
    # (secret_token). Telegram шлёт его заголовком с каждым запросом —
    # без него в вебхук пишет кто угодно. Пусто = вебхук закрыт.
    TELEGRAM_WEBHOOK_SECRET=(str, ""),
    PAYMENT_GATEWAY=(str, "mock"),
    PAYMENT_API_KEY=(str, ""),
    PAYMENT_API_SECRET=(str, ""),
    PAYMENT_WEBHOOK_SECRET=(str, ""),
    SITE_BASE_URL=(str, "http://localhost:8710"),
    # Separate password gating the admin Finance & analytics screens.
    FINANCE_PASSWORD=(str, "finance123"),
    # Сколько прокси стоит между клиентом и Django (nginx = 1). Пусто — не
    # задано: поведение прежнее (X-Forwarded-For берётся целиком, подделывается).
    # Задавать ТОЛЬКО в паре с nginx, который перезаписывает X-Forwarded-For
    # (`proxy_set_header X-Forwarded-For $remote_addr`) и подставляет настоящий
    # адрес из Cloudflare (real_ip). См. accounts/throttling.py.
    TRUSTED_PROXY_COUNT=(str, ""),
)

# Load .env if present (keeps real secrets out of source control).
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")


# Application definition

INSTALLED_APPS = [
    # modeltranslation must come before django.contrib.admin.
    "modeltranslation",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "rest_framework_simplejwt",
    # Чёрный список refresh-токенов: ротация гасит использованный токен.
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    # Local apps
    "accounts",
    "warehouse",
    "services",
    "clients",
    "sales",
    "audit",
    "integrations",
    "finance",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    # Предел неудачных попыток на форме входа Django-админки (API лимитирует DRF).
    "accounts.middleware.AdminLoginThrottleMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database — SQLite для разработки; в проде задаём DATABASE_URL (PostgreSQL).
if env("DATABASE_URL"):
    DATABASES = {"default": env.db("DATABASE_URL")}
    # Держим соединение открытым между запросами (иначе на каждый запрос новый
    # коннект к Postgres) и проверяем его живость перед использованием.
    DATABASES["default"]["CONN_MAX_AGE"] = 60
    DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


# Custom user model — roles Admin / Storekeeper live here.
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Internationalization — Russian / Kyrgyz / English (django-modeltranslation).
LANGUAGE_CODE = "ru"
TIME_ZONE = "Asia/Bishkek"
USE_I18N = True
USE_TZ = True

LANGUAGES = [
    ("ru", "Русский"),
    ("ky", "Кыргызча"),
    ("en", "English"),
]
MODELTRANSLATION_DEFAULT_LANGUAGE = "ru"
MODELTRANSLATION_LANGUAGES = ("ru", "ky", "en")
LOCALE_PATHS = [BASE_DIR / "locale"]


# Static & media files
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Количество доверенных прокси для определения адреса клиента (лимит входа).
# None = как раньше; число = брать адрес, добавленный N-м с конца прокси.
TRUSTED_PROXY_COUNT = (
    int(env("TRUSTED_PROXY_COUNT")) if env("TRUSTED_PROXY_COUNT").strip() else None
)


# Django REST Framework + JWT
REST_FRAMEWORK = {
    "NUM_PROXIES": TRUSTED_PROXY_COUNT,
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "accounts.authentication.CloudeJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    # ?page_size= разрешён: списки-выпадашки (материалы в журнале движений, в
    # инвентаризации, в списании) грузятся одним запросом. Без него страница в
    # 25 строк молча обрезала каталог, и материала №26 в выборе просто не было.
    "DEFAULT_PAGINATION_CLASS": "config.pagination.SizedPageNumberPagination",
    "PAGE_SIZE": 25,
    # Пределы попыток входа. Ограничения нет только у логинов — остальные
    # запросы идут с токеном, и перебирать там нечего.
    #
    # `login` / `customer-login` считают по АДРЕСУ, `login-account` — по самому
    # логину (телефону): иначе пароль клиентского кабинета, который выдаёт
    # админ, перебирается с разных адресов без единой помехи.
    "DEFAULT_THROTTLE_RATES": {
        "login": "10/min",
        "login-account": "20/hour",
        "customer-login": "10/min",
    },
}

# Кеш держит счётчики попыток входа. В БД, а не в памяти процесса: воркеров
# gunicorn три, и у каждого был бы свой счётчик — предел утроился бы.
# Таблицу создаёт `manage.py createcachetable` (вызывается в entrypoint).
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "django_cache",
    }
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=12),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "AUTH_HEADER_TYPES": ("Bearer",),
    # Каждое обновление выдаёт новый refresh и гасит старый: украденный refresh
    # живёт до первого использования настоящим владельцем.
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}


# CORS — frontend dev server (Vite) talks to this backend.
CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_CREDENTIALS = True


# --- External integrations (real, configured via environment) ---
TELEGRAM_STAFF_BOT_TOKEN = env("TELEGRAM_STAFF_BOT_TOKEN")
TELEGRAM_STAFF_CHAT_IDS = env("TELEGRAM_STAFF_CHAT_IDS")
TELEGRAM_CUSTOMER_BOT_TOKEN = env("TELEGRAM_CUSTOMER_BOT_TOKEN")
TELEGRAM_WEBHOOK_SECRET = env("TELEGRAM_WEBHOOK_SECRET")

PAYMENT_GATEWAY = env("PAYMENT_GATEWAY")  # e.g. "mock", "freedompay", "elsom"
PAYMENT_API_KEY = env("PAYMENT_API_KEY")
PAYMENT_API_SECRET = env("PAYMENT_API_SECRET")
PAYMENT_WEBHOOK_SECRET = env("PAYMENT_WEBHOOK_SECRET")
SITE_BASE_URL = env("SITE_BASE_URL")

# Extra password protecting the Finance & detailed-analytics screens (on top of
# the admin login). Change it in .env via FINANCE_PASSWORD=...
FINANCE_PASSWORD = env("FINANCE_PASSWORD")


# --- Production hardening (действует только при DEBUG=False) ---
# Схема запроса приходит от nginx (а до него — от Cloudflare) заголовком
# X-Forwarded-Proto. Без этого Django считает соединение незащищённым и,
# например, ставит куки без флага secure и строит http-ссылки.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")

if not DEBUG:
    SECURE_SSL_REDIRECT = env("SECURE_SSL_REDIRECT")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"
    # HSTS включаем осознанно: браузер запомнит «только https» на год. Ставить
    # после того, как убедились, что сайт открывается по https без сюрпризов.
    SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# --- Логи ---
# Без этого блока при DEBUG=False трассировка 500-й ошибки не попадала никуда:
# стандартная настройка Django отправляет django.request на почту админам
# (ADMINS пуст) и в консоль только при DEBUG. В `docker logs` было пусто.
# Всё от WARNING и выше (500-ки с трассировкой, 4xx, наши предупреждения)
# уходит в stderr контейнера. Уровень — LOG_LEVEL в окружении.
LOG_LEVEL = env.str("LOG_LEVEL", default="WARNING").upper()
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    # Все логгеры приложений (accounts, finance, integrations…) наследуют корень.
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        # Перекрывает стандартный `django` (консоль только при DEBUG + почта).
        # django.server (runserver) свой обработчик сохраняет.
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}

# Под `manage.py test` консоль молчит: сотни ожидаемых 400/401 в прогоне — шум.
# Тесты, которым нужен лог, ловят его сами (`assertLogs`).
if len(sys.argv) > 1 and sys.argv[1] == "test":
    LOGGING["root"]["level"] = "CRITICAL"
    LOGGING["loggers"]["django"]["level"] = "CRITICAL"

# --- Тесты: быстрый хешер паролей ---
# Боевой PBKDF2 (1,2 млн итераций, ~0,3 с на пароль) в setUp каждого теста
# превращал полный прогон в ~10 минут. Под `manage.py test` берём MD5 —
# тесты проверяют логику, а не стойкость хеша. Проверяем именно имя команды
# (sys.argv[1]), а не `"test" in sys.argv`, чтобы `createsuperuser --username test`
# на проде случайно не записал слабый хеш.
if len(sys.argv) > 1 and sys.argv[1] == "test":
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
