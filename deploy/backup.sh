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
# Здесь: дамп пишется во временный файл, проверяется (gzip цел, дамп дошёл до
# конца, размер не подозрительный), и только после этого занимает место
# сегодняшней копии; ротация запускается лишь после УСПЕХА и базы, и фото.
# Любая ошибка — ненулевой код выхода, запись в лог и оповещение; старые копии
# не трогаются.
#
# Оповещение при сбое (по порядку, срабатывает первый доступный вариант):
#   1. BACKUP_ALERT_CMD — любая команда оболочки; текст ошибки лежит в
#      переменной BACKUP_ALERT_MESSAGE. Пример:
#        BACKUP_ALERT_CMD='mail -s "chpu-backup FAIL" you@example.com <<<"$BACKUP_ALERT_MESSAGE"'
#   2. Telegram: если в .env.prod заданы TELEGRAM_STAFF_BOT_TOKEN и
#      TELEGRAM_STAFF_CHAT_IDS (те же, что у уведомлений о низких остатках) —
#      сообщение уходит туда. Файл только читается, не исполняется.
#   3. Иначе — только лог и код выхода. Тогда cron-почта (MAILTO) или
#      внешний монитор — единственный способ узнать о сбое: см. DEPLOY.md.
#
# Выгрузка копии за пределы сервера (копии на одном диске с базой — это не
# защита от потери диска): BACKUP_UPLOAD_CMD, команда оболочки, получает пути
# в BACKUP_DB_FILE и BACKUP_MEDIA_FILE. Пример:
#   BACKUP_UPLOAD_CMD='rclone copy "$BACKUP_DB_FILE" remote:chpu-backups/'
# Сбой выгрузки — тоже ошибка (код 1, оповещение), но уже снятая локальная
# копия остаётся и ротация выполняется.
#
# Установка (на сервере):
#   sudo install -m 755 /opt/chpucenter/deploy/backup.sh /usr/local/bin/chpu-backup
#   sudo mkdir -p /root/backups
#   sudo crontab -e
#     0 3 * * * /usr/local/bin/chpu-backup >> /var/log/chpu-backup.log 2>&1
#
# Проверить руками:  sudo /usr/local/bin/chpu-backup
#
# Переменные (значения по умолчанию ниже): PROJECT_DIR, COMPOSE_FILE,
# BACKUP_DIR, MEDIA_DIR, KEEP_DAYS, MIN_DUMP_BYTES, ENV_FILE, DB_EXEC.
# DB_EXEC — префикс команды «выполнить внутри контейнера с базой»; нужен только
# для проверки скрипта без docker.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/chpucenter}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
BACKUP_DIR="${BACKUP_DIR:-/root/backups}"
MEDIA_DIR="${MEDIA_DIR:-/srv/chpucenter/media}"
KEEP_DAYS="${KEEP_DAYS:-14}"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env.prod}"
# Меньше килобайта — это не дамп, а сообщение об ошибке или пустота.
MIN_DUMP_BYTES="${MIN_DUMP_BYTES:-1024}"
DB_EXEC="${DB_EXEC:-docker compose -f $COMPOSE_FILE exec -T db}"

log() { echo "$(date '+%F %T') $*"; }

# --- оповещение ---------------------------------------------------------------
env_value() {  # env_value ИМЯ — значение из .env.prod без исполнения файла
    [ -r "$ENV_FILE" ] || return 0
    grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- | tr -d "\"'\r" || true
}

alert() {
    local msg
    msg="chpu-backup ($(hostname)): $1"
    export BACKUP_ALERT_MESSAGE="$msg"
    if [ -n "${BACKUP_ALERT_CMD:-}" ]; then
        sh -c "$BACKUP_ALERT_CMD" || log "оповещение (BACKUP_ALERT_CMD) не отправилось"
        return 0
    fi
    local token chats chat
    token="$(env_value TELEGRAM_STAFF_BOT_TOKEN)"
    chats="$(env_value TELEGRAM_STAFF_CHAT_IDS)"
    if [ -n "$token" ] && [ -n "$chats" ] && command -v curl >/dev/null; then
        for chat in $(echo "$chats" | tr ',[]' '   '); do
            # Адрес с токеном передаём через stdin (-K -), а не аргументом:
            # аргументы видны в списке процессов.
            printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token" \
                | curl -sS --max-time 15 -K - -o /dev/null \
                    --data-urlencode "chat_id=$chat" \
                    --data-urlencode "text=$msg" \
                || log "оповещение в Telegram (чат $chat) не отправилось"
        done
        return 0
    fi
    log "оповещение не настроено (ни BACKUP_ALERT_CMD, ни Telegram в $ENV_FILE)"
}

FAIL_REASON=""
LOCAL_OK=0
tmp_files=()
on_exit() {
    local status=$?
    if [ "${#tmp_files[@]}" -gt 0 ]; then rm -f "${tmp_files[@]}"; fi
    if [ "$status" -ne 0 ]; then
        if [ "$LOCAL_OK" = "1" ]; then
            log "ЛОКАЛЬНАЯ КОПИЯ СНЯТА, НО ВЫГРУЗКА НАРУЖУ НЕ УДАЛАСЬ (код $status)"
        else
            log "РЕЗЕРВНАЯ КОПИЯ НЕ СНЯТА (код $status); старые копии не тронуты"
        fi
        alert "${FAIL_REASON:-копия не снята (код $status), подробности в /var/log/chpu-backup.log}"
    fi
}
trap on_exit EXIT

fail() { FAIL_REASON="$*"; log "ОШИБКА: $*"; exit 1; }

# --- проверки ----------------------------------------------------------------
command -v docker >/dev/null || [ "${DB_EXEC%% *}" != "docker" ] || fail "docker не найден"
cd "$PROJECT_DIR" || fail "нет каталога проекта $PROJECT_DIR"
mkdir -p "$BACKUP_DIR"

stamp="$(date +%F)"
db_file="$BACKUP_DIR/db-$stamp.sql.gz"
# mktemp переносим: XXXXXX строго в конце (суффикс после них — расширение GNU).
tmp_file="$(mktemp "$BACKUP_DIR/.db-$stamp.XXXXXX")"
tmp_files+=("$tmp_file")

# --- база --------------------------------------------------------------------
# Имя базы и пользователя берём из окружения самого контейнера: они заданы в
# .env проекта, и дублировать их здесь — значит однажды разойтись с ним.
log "дамп базы → $db_file"
# shellcheck disable=SC2086,SC2016  # DB_EXEC разбивается на слова; $VAR раскроет контейнер
if ! $DB_EXEC sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$tmp_file"; then
    fail "pg_dump не отработал — контейнер db не поднят или база недоступна"
fi

size="$(wc -c < "$tmp_file" | tr -d " ")"
[ "$size" -ge "$MIN_DUMP_BYTES" ] || fail "дамп подозрительно мал ($size Б)"
gzip -t "$tmp_file" || fail "архив дампа повреждён (gzip -t)"
# Обрыв посреди pg_dump (контейнер перезапустился, диск кончился) оставляет
# валидный gzip с обрезанным SQL. Целый дамп заканчивается этой строкой (за ней
# в новых версиях бывает ещё служебный \unrestrict).
dump_tail="$(gzip -dc "$tmp_file" | tail -n 20)"
case "$dump_tail" in
    *'PostgreSQL database dump complete'*) ;;
    *) fail "дамп обрезан: нет завершающей строки pg_dump" ;;
esac
# (grep -c, а не grep -q: ранний выход grep -q даёт SIGPIPE и под pipefail
# превратился бы в ложную ошибку.)
copies="$(gzip -dc "$tmp_file" | grep -c '^COPY ' || true)"
[ "${copies:-0}" -gt 0 ] || fail "в дампе нет данных таблиц (COPY)"

mv "$tmp_file" "$db_file"
chmod 600 "$db_file"
log "база готова: $(du -h "$db_file" | cut -f1)"

# --- фото --------------------------------------------------------------------
# Фото материалов лежат на диске хоста и в дамп не попадают. Без них база
# восстановится, но карточки останутся без картинок.
media_file=""
if [ -d "$MEDIA_DIR" ]; then
    media_file="$BACKUP_DIR/media-$stamp.tar.gz"
    media_tmp="$(mktemp "$BACKUP_DIR/.media-$stamp.XXXXXX")"
    tmp_files+=("$media_tmp")
    log "фото → $media_file"
    tar -czf "$media_tmp" -C "$(dirname "$MEDIA_DIR")" "$(basename "$MEDIA_DIR")" \
        || fail "не удалось упаковать media"
    tar -tzf "$media_tmp" >/dev/null || fail "архив media повреждён"
    mv "$media_tmp" "$media_file"
    chmod 600 "$media_file"
    log "фото готовы: $(du -h "$media_file" | cut -f1)"
else
    log "каталог media ($MEDIA_DIR) не найден — пропускаю"
fi

# --- выгрузка наружу (необязательная) ----------------------------------------
LOCAL_OK=1
upload_failed=0
if [ -n "${BACKUP_UPLOAD_CMD:-}" ]; then
    log "выгрузка наружу (BACKUP_UPLOAD_CMD)"
    export BACKUP_DB_FILE="$db_file" BACKUP_MEDIA_FILE="$media_file"
    sh -c "$BACKUP_UPLOAD_CMD" || { upload_failed=1; log "ОШИБКА: выгрузка наружу не удалась"; }
fi

# --- ротация -----------------------------------------------------------------
# ТОЛЬКО после успешной копии (см. шапку).
deleted="$(find "$BACKUP_DIR" -maxdepth 1 -name 'db-*.sql.gz' -mtime "+$KEEP_DAYS" -print -delete | wc -l | tr -d ' ')"
find "$BACKUP_DIR" -maxdepth 1 -name 'media-*.tar.gz' -mtime "+$KEEP_DAYS" -delete
kept="$(find "$BACKUP_DIR" -maxdepth 1 -name 'db-*.sql.gz' | wc -l | tr -d ' ')"
log "готово. удалено старых копий: $deleted; хранится: $kept"

if [ "$upload_failed" -ne 0 ]; then
    FAIL_REASON="локальная копия снята, но выгрузка наружу (BACKUP_UPLOAD_CMD) не удалась"
    exit 1
fi
