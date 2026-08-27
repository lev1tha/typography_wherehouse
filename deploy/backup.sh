#!/usr/bin/env bash
#
# Резервная копия ЧПУ Системы: база (pg_dump) + загруженные фото (media).
#
# Почему скриптом, а не строкой в cron. Однострочник вида
#   pg_dump ... | gzip > db-$(date +%F).sql.gz && find ... -mtime +14 -delete
# ломается ровно тогда, когда он нужнее всего: если pg_dump упал (контейнер не
# поднят, база занята), gzip всё равно создаёт ФАЙЛ — пустой, — и следующая
# команда честно удаляет старые копии. Через две недели таких ночей копий не
# остаётся вовсе, и узнают об этом в тот день, когда данные понадобились.
#
# Здесь: дамп сначала пишется во временный файл, проверяется на «непустой», и
# только после успеха он занимает место сегодняшней копии и запускается
# ротация. Любая ошибка — выход с ненулевым кодом и запись в лог; старое не
# трогается.
#
# Установка (на сервере):
#   sudo install -m 755 /opt/chpucenter/deploy/backup.sh /usr/local/bin/chpu-backup
#   sudo mkdir -p /root/backups
#   sudo crontab -e
#     0 3 * * * /usr/local/bin/chpu-backup >> /var/log/chpu-backup.log 2>&1
#
# Проверить руками:  sudo /usr/local/bin/chpu-backup
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/chpucenter}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
BACKUP_DIR="${BACKUP_DIR:-/root/backups}"
MEDIA_DIR="${MEDIA_DIR:-/srv/chpucenter/media}"
KEEP_DAYS="${KEEP_DAYS:-14}"
# Меньше килобайта — это не дамп, а сообщение об ошибке или пустота.
MIN_DUMP_BYTES="${MIN_DUMP_BYTES:-1024}"

log() { echo "$(date '+%F %T') $*"; }
fail() { log "ОШИБКА: $*"; exit 1; }

command -v docker >/dev/null || fail "docker не найден"
cd "$PROJECT_DIR" || fail "нет каталога проекта $PROJECT_DIR"
mkdir -p "$BACKUP_DIR"

stamp="$(date +%F)"
db_file="$BACKUP_DIR/db-$stamp.sql.gz"
tmp_file="$(mktemp "$BACKUP_DIR/.db-$stamp.XXXXXX.sql.gz")"
# Временный файл не должен пережить падение скрипта.
trap 'rm -f "$tmp_file"' EXIT

# Имя базы и пользователя берём из окружения самого контейнера: они заданы в
# .env проекта, и дублировать их здесь — значит однажды разойтись с ним.
log "дамп базы → $db_file"
set -o pipefail
if ! docker compose -f "$COMPOSE_FILE" exec -T db \
        sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$tmp_file"; then
    fail "pg_dump не отработал — старые копии не тронуты"
fi

size="$(wc -c < "$tmp_file" | tr -d " ")"
[ "$size" -ge "$MIN_DUMP_BYTES" ] || fail "дамп подозрительно мал ($size Б) — старые копии не тронуты"

mv "$tmp_file" "$db_file"
trap - EXIT
log "база готова: $(du -h "$db_file" | cut -f1)"

# Фото материалов лежат на диске хоста и в дамп не попадают. Без них база
# восстановится, но карточки останутся без картинок.
if [ -d "$MEDIA_DIR" ]; then
    media_file="$BACKUP_DIR/media-$stamp.tar.gz"
    log "фото → $media_file"
    tar -czf "$media_file" -C "$(dirname "$MEDIA_DIR")" "$(basename "$MEDIA_DIR")" \
        || fail "не удалось упаковать media"
    log "фото готовы: $(du -h "$media_file" | cut -f1)"
else
    log "каталог media ($MEDIA_DIR) не найден — пропускаю"
fi

# Ротация — ТОЛЬКО после успешной копии (см. комментарий в шапке).
deleted="$(find "$BACKUP_DIR" -maxdepth 1 -name 'db-*.sql.gz' -mtime "+$KEEP_DAYS" -print -delete | wc -l | tr -d ' ')"
find "$BACKUP_DIR" -maxdepth 1 -name 'media-*.tar.gz' -mtime "+$KEEP_DAYS" -delete
kept="$(find "$BACKUP_DIR" -maxdepth 1 -name 'db-*.sql.gz' | wc -l | tr -d ' ')"
log "готово. удалено старых копий: $deleted; хранится: $kept"
