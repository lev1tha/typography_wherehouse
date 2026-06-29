"""
Django settings for the Cloude warehouse & sales system.
"""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, True),
    SECRET_KEY=(str, "django-insecure-change-me-in-production"),
    ALLOWED_HOSTS=(list, ["*"]),
    CORS_ALLOWED_ORIGINS=(list, ["http://localhost:5173", "http://127.0.0.1:5173"]),
    # Integrations — real tokens are read from the environment (.env / shell).
    TELEGRAM_STAFF_BOT_TOKEN=(str, ""),
    TELEGRAM_STAFF_CHAT_IDS=(list, []),
    TELEGRAM_CUSTOMER_BOT_TOKEN=(str, ""),
    PAYMENT_GATEWAY=(str, "mock"),
    PAYMENT_API_KEY=(str, ""),
    PAYMENT_API_SECRET=(str, ""),
    PAYMENT_WEBHOOK_SECRET=(str, ""),
    SITE_BASE_URL=(str, "http://localhost:8000"),
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


# Database
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


# Django REST Framework + JWT
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=12),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "AUTH_HEADER_TYPES": ("Bearer",),
}


# CORS — frontend dev server (Vite) talks to this backend.
CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_CREDENTIALS = True


# --- External integrations (real, configured via environment) ---
TELEGRAM_STAFF_BOT_TOKEN = env("TELEGRAM_STAFF_BOT_TOKEN")
TELEGRAM_STAFF_CHAT_IDS = env("TELEGRAM_STAFF_CHAT_IDS")
TELEGRAM_CUSTOMER_BOT_TOKEN = env("TELEGRAM_CUSTOMER_BOT_TOKEN")

PAYMENT_GATEWAY = env("PAYMENT_GATEWAY")  # e.g. "mock", "freedompay", "elsom"
PAYMENT_API_KEY = env("PAYMENT_API_KEY")
PAYMENT_API_SECRET = env("PAYMENT_API_SECRET")
PAYMENT_WEBHOOK_SECRET = env("PAYMENT_WEBHOOK_SECRET")
SITE_BASE_URL = env("SITE_BASE_URL")
