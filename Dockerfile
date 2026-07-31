# --- Стадия 1: сборка фронтенда (React + Vite) ---
FROM node:22-alpine AS frontend
WORKDIR /build

# Сначала только манифесты — слой с npm ci переиспользуется, пока не менялись
# зависимости (правки в исходниках не заставляют ставить пакеты заново).
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci

COPY frontend/ ./
RUN npm run build          # → /build/dist


# --- Стадия 2: бэкенд (Django + gunicorn) ---
FROM python:3.13-slim AS backend

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl нужен для healthcheck'а контейнера; psycopg[binary] системных
# библиотек не требует, поэтому build-essential не ставим.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-prod.txt ./
RUN pip install -r requirements-prod.txt

COPY . .

# Собранный фронтенд кладём в образ: entrypoint выложит его в том, который
# читает nginx на хосте (см. docker-compose.prod.yml).
COPY --from=frontend /build/dist /app/frontend_dist

COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /app/staticfiles /app/media /app/frontend_public \
    && chown -R app:app /app

USER app

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "60", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
