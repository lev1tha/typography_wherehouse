#!/bin/sh
# Запускается при старте контейнера web (см. Dockerfile ENTRYPOINT).
# Приводит окружение в рабочее состояние, затем передаёт управление gunicorn.
set -e

echo "→ Ждём базу данных…"
python - <<'PY'
import os, sys, time
import psycopg

url = os.environ.get("DATABASE_URL", "")
if not url:
    sys.exit(0)  # SQLite — ждать нечего

for attempt in range(1, 31):
    try:
        psycopg.connect(url, connect_timeout=3).close()
        print("  база отвечает")
        sys.exit(0)
    except Exception as exc:
        print(f"  попытка {attempt}/30: {exc}")
        time.sleep(2)

print("База так и не ответила за 60 секунд — прекращаю запуск.")
sys.exit(1)
PY

echo "→ Миграции…"
python manage.py migrate --noinput

echo "→ Статика Django (admin и т.п.)…"
python manage.py collectstatic --noinput

# Выкладываем собранный фронтенд в том, который отдаёт nginx с хоста.
# Делаем на каждом старте, чтобы `docker compose up -d --build` обновлял и UI.
if [ -d /app/frontend_dist ]; then
    echo "→ Обновляем фронтенд…"
    rm -rf /app/frontend_public/* 2>/dev/null || true
    cp -r /app/frontend_dist/. /app/frontend_public/
fi

echo "→ Стартуем: $*"
exec "$@"
