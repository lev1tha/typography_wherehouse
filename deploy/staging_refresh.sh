#!/usr/bin/env bash
# Залить на тестовый стенд свежую копию боевой базы и фото материалов.
#
#   bash /opt/chpucenter-staging/deploy/staging_refresh.sh
#
# (на сервере оболочка fish — запускать именно через bash, как выше.)
#
# Прод только ЧИТАЕТСЯ: pg_dump из его контейнера и копирование каталога медиа.
# Всё, что стирается и пишется, — база и медиа стенда. Всё, что было введено на
# стенде, пропадает: стенд после запуска — копия прода на эту минуту плюс
# миграции из кода стенда.
#
# Переменные (по умолчанию — раскладка из STAGING.md): PROD_DIR, STAGING_DIR,
# PROD_MEDIA, STAGING_MEDIA. YES=1 — не спрашивать подтверждение.
set -euo pipefail

PROD_DIR="${PROD_DIR:-/opt/chpucenter}"
STAGING_DIR="${STAGING_DIR:-/opt/chpucenter-staging}"
PROD_MEDIA="${PROD_MEDIA:-/srv/chpucenter/media}"
STAGING_MEDIA="${STAGING_MEDIA:-/srv/chpucenter-staging/media}"

PROD="docker compose --project-directory $PROD_DIR -f $PROD_DIR/docker-compose.prod.yml"
STAGE="docker compose --project-directory $STAGING_DIR -f $STAGING_DIR/docker-compose.staging.yml"

log() { echo "$(date '+%F %T') $*"; }
die() { echo "ОШИБКА: $*" >&2; exit 1; }

# --- защита от перепутанных путей ---------------------------------------------
[ "$(realpath "$PROD_DIR")" != "$(realpath "$STAGING_DIR")" ] \
    || die "PROD_DIR и STAGING_DIR указывают в одно место"
[ "${PROD_MEDIA%/}" != "${STAGING_MEDIA%/}" ] \
    || die "PROD_MEDIA и STAGING_MEDIA указывают в одно место"
[ -f "$STAGING_DIR/docker-compose.staging.yml" ] || die "нет $STAGING_DIR/docker-compose.staging.yml"
[ -f "$STAGING_DIR/.env.staging" ] || die "нет $STAGING_DIR/.env.staging (см. STAGING.md)"
grep -q '^name: chpucenter-staging$' "$STAGING_DIR/docker-compose.staging.yml" \
    || die "в compose-файле стенда нет 'name: chpucenter-staging' — не уверен, что пишу не в прод"

env_value() {  # env_value ФАЙЛ ИМЯ
    grep -E "^$2=" "$1" | tail -n 1 | cut -d= -f2- | tr -d "\"'\r"
}
STAGING_DB="$(env_value "$STAGING_DIR/.env.staging" POSTGRES_DB)"
STAGING_USER="$(env_value "$STAGING_DIR/.env.staging" POSTGRES_USER)"
[ -n "$STAGING_DB" ] && [ -n "$STAGING_USER" ] || die "в .env.staging не заданы POSTGRES_DB / POSTGRES_USER"

# Токены Telegram на стенде с боевой базой = сообщения настоящим людям.
for var in TELEGRAM_STAFF_BOT_TOKEN TELEGRAM_CUSTOMER_BOT_TOKEN; do
    [ -z "$(env_value "$STAGING_DIR/.env.staging" "$var" || true)" ] \
        || die "в .env.staging заполнен $var — на стенде он должен быть пустым"
done

if [ "${YES:-0}" != "1" ]; then
    echo "База стенда ($STAGING_DB) и $STAGING_MEDIA будут ЗАМЕНЕНЫ копией прода."
    read -r -p "Продолжить? Напишите yes: " answer
    [ "$answer" = "yes" ] || die "отменено"
fi

# --- 1. дамп прода (только чтение) --------------------------------------------
DUMP="$(mktemp /tmp/chpu-staging-XXXXXX.sql)"
trap 'rm -f "$DUMP"' EXIT
log "Снимаю дамп прода…"
$PROD exec -T db sh -c 'pg_dump --no-owner --no-acl -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$DUMP"
[ "$(wc -c < "$DUMP")" -gt 1024 ] || die "дамп подозрительно маленький — прерываю, стенд не тронут"
log "  дамп: $(du -h "$DUMP" | cut -f1)"

# --- 2. база стенда ------------------------------------------------------------
log "Останавливаю приложение стенда…"
$STAGE stop web >/dev/null 2>&1 || true
$STAGE up -d --wait db

log "Заливаю копию в базу стенда…"
{
    echo "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
    # \restrict / \unrestrict пишет pg_dump начиная с 17.6; psql стенда может
    # оказаться старше прод-образа и споткнуться о них. Дамп свой, доверенный.
    grep -vE '^\\(restrict|unrestrict) ' "$DUMP"
    # Кеш с чужими счётчиками попыток входа стенду ни к чему. Имя со схемой:
    # дамп обнуляет search_path.
    echo "DO \$\$ BEGIN IF to_regclass('public.django_cache') IS NOT NULL THEN TRUNCATE public.django_cache; END IF; END \$\$;"
} |$STAGE exec -T db psql -q -v ON_ERROR_STOP=1 -U "$STAGING_USER" -d "$STAGING_DB" >/dev/null

# --- 3. медиа ------------------------------------------------------------------
log "Копирую фото материалов…"
mkdir -p "$STAGING_MEDIA"
if command -v rsync >/dev/null; then
    rsync -a --delete "$PROD_MEDIA"/ "$STAGING_MEDIA"/
else
    find "$STAGING_MEDIA" -mindepth 1 -delete
    cp -a "$PROD_MEDIA"/. "$STAGING_MEDIA"/
fi
chown -R 1000:1000 "$STAGING_MEDIA"

# --- 4. миграции кода стенда и запуск -------------------------------------------
log "Миграции и запуск стенда…"
$STAGE up -d --force-recreate migrate web
log "Готово: https://test.chpucenter.com"
