#!/bin/sh
# Точка входа контейнеров (см. Dockerfile ENTRYPOINT).
#
# Два режима — чтобы «подготовка к выкладке» шла ровно один раз, а не в каждом
# контейнере, который стартует из этого образа:
#
#   entrypoint.sh release   — разовая выкладка: миграции → таблица кеша →
#                             статика Django → фронтенд в каталог nginx.
#                             Запускается сервисом `migrate` из
#                             docker-compose.prod.yml и завершается. web и бот
#                             стартуют только после его успешного завершения
#                             (depends_on: service_completed_successfully).
#   entrypoint.sh <команда> — обычный запуск (gunicorn у web, run_customer_bot
#                             у бота): дождаться базы и передать управление.
#                             Ни migrate, ни collectstatic здесь НЕ выполняются.
#
# Почему так (аудит 2026-10): раньше миграции гнали web и бот одновременно, а
# упавшая миграция превращалась в бесконечный перезапуск web при уже снесённом
# рабочем контейнере. Теперь при ошибке выкладки `up -d` останавливается на
# сервисе migrate, а действующий web не пересоздаётся.
#
# Запасной выключатель для запуска без compose (docker run, локальная проверка
# образа): RUN_RELEASE=1 — перед командой выполнить те же шаги выкладки.
set -e

wait_for_db() {
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
}

# Каталоги примонтированы с хоста и могли остаться во владении root — тогда
# collectstatic падает длинным трейсбеком. Проверяем заранее и говорим прямо,
# что делать.
check_dirs() {
    for dir in /app/staticfiles /app/media /app/frontend_public; do
        if [ ! -w "$dir" ]; then
            echo ""
            echo "ОШИБКА: нет прав на запись в $dir"
            echo "Каталоги на хосте принадлежат root, а контейнер работает от uid 1000."
            echo "Выполните на сервере:  chown -R 1000:1000 /srv/chpucenter"
            echo ""
            exit 1
        fi
    done
}

# Выкладка фронтенда в каталог, который nginx читает ПРЯМО СЕЙЧАС.
#
# Раньше было `rm -rf frontend_public/*` и затем `cp`: между двумя командами
# каталог пуст, и любой запрос в эту секунду получал 404/403, а открытая вкладка
# со старой версией, которой нужен старый хэшированный файл из assets/,
# получала ошибку загрузки модуля.
#
# Теперь порядок такой, что каталог НИКОГДА не пуст и не «наполовину новый»:
#   1. assets/ (файлы с хэшем в имени) ДОБАВЛЯЮТСЯ рядом со старыми — старые
#      вкладки продолжают находить свои файлы;
#   2. остальные файлы верхнего уровня заменяются атомарно (копия под
#      временным именем → mv, а mv в пределах одного каталога — это rename);
#   3. index.html подменяется ПОСЛЕДНИМ, тоже через rename: пока он старый —
#      он ссылается на старые, ещё лежащие на месте assets; как только новый —
#      на новые, уже лежащие на месте;
#   4. из assets/ удаляется то, что не обновлялось дольше FRONTEND_KEEP_DAYS
#      дней: всё из текущей сборки только что перезаписано и под удаление не
#      попадает.
deploy_frontend() {
    src=/app/frontend_dist
    dst=/app/frontend_public
    keep_days="${FRONTEND_KEEP_DAYS:-14}"
    [ -d "$src" ] || return 0
    echo "→ Обновляем фронтенд…"

    for entry in "$src"/* "$src"/.[!.]*; do
        [ -e "$entry" ] || continue
        name="$(basename "$entry")"
        [ "$name" = "index.html" ] && continue
        if [ -d "$entry" ]; then
            mkdir -p "$dst/$name"
            cp -r "$entry"/. "$dst/$name"/
        else
            cp "$entry" "$dst/.$name.new"
            mv -f "$dst/.$name.new" "$dst/$name"
        fi
    done

    if [ -f "$src/index.html" ]; then
        cp "$src/index.html" "$dst/.index.html.new"
        mv -f "$dst/.index.html.new" "$dst/index.html"
    fi

    if [ -d "$dst/assets" ]; then
        pruned="$(find "$dst/assets" -type f -mtime "+$keep_days" -print -delete | wc -l | tr -d ' ')"
        echo "  старых файлов assets удалено: $pruned (старше $keep_days дн.)"
    fi
}

release() {
    check_dirs

    echo "→ Миграции…"
    python manage.py migrate --noinput

    # Таблица кеша: в ней живут счётчики попыток входа. Кеш общий на все
    # воркеры gunicorn — в памяти процесса счётчик был бы у каждого свой, и
    # предел попыток по факту умножился бы. Команда идемпотентна.
    echo "→ Таблица кеша…"
    python manage.py createcachetable

    echo "→ Статика Django (admin и т.п.)…"
    python manage.py collectstatic --noinput

    deploy_frontend
    echo "→ Выкладка завершена."
}

if [ "$1" = "release" ]; then
    wait_for_db
    release
    exit 0
fi

wait_for_db

if [ "${RUN_RELEASE:-0}" = "1" ]; then
    release
fi

echo "→ Стартуем: $*"
exec "$@"
