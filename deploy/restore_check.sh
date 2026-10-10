#!/usr/bin/env bash
#
# Проверка, что последняя резервная копия РЕАЛЬНО восстанавливается.
#
# Непроверенная копия — не копия: дамп может быть целым gzip-архивом и всё же не
# заливаться (обрезан, версия не та, роль не существует). Узнавать об этом в
# день аварии поздно, поэтому раз в неделю скрипт сам:
#   1. берёт самый свежий db-*.sql.gz и проверяет, что он не старше
#      MAX_AGE_HOURS (иначе cron backup молча перестал работать);
#   2. проверяет gzip -t и архив фото;
#   3. создаёт ВРЕМЕННУЮ базу рядом с боевой (боевую не трогает — только читает
#      для сравнения) и заливает дамп с `ON_ERROR_STOP` в одной транзакции:
#      любая ошибка = копия негодна;
#   4. сверяет число строк ключевых таблиц с боевой базой: копия не должна быть
#      пустой и не должна сильно отставать;
#   5. удаляет временную базу (всегда, и при ошибке тоже).
# Ненулевой код выхода + оповещение — как у backup.sh (см. его шапку:
# BACKUP_ALERT_CMD либо Telegram из .env.prod).
#
# Установка (на сервере):
#   sudo install -m 755 /opt/chpucenter/deploy/restore_check.sh /usr/local/bin/chpu-restore-check
#   sudo crontab -e
#     30 4 * * 0 /usr/local/bin/chpu-restore-check >> /var/log/chpu-restore-check.log 2>&1
#
# Проверить руками:  sudo /usr/local/bin/chpu-restore-check
#
# Переменные: PROJECT_DIR, COMPOSE_FILE, BACKUP_DIR, ENV_FILE, MAX_AGE_HOURS (36),
# MIN_RATIO_PCT (80), KEY_TABLES, DB_EXEC (префикс «выполнить внутри контейнера
# db»; нужен только для проверки скрипта без docker).
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/chpucenter}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
BACKUP_DIR="${BACKUP_DIR:-/root/backups}"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.env.prod}"
MAX_AGE_HOURS="${MAX_AGE_HOURS:-36}"
# Копия снята до последней минуты, а в боевой базе за это время могли
# появиться новые строки — но не исчезнуть. Допускаем отставание до 20 %.
MIN_RATIO_PCT="${MIN_RATIO_PCT:-80}"
KEY_TABLES="${KEY_TABLES:-accounts_user clients_client warehouse_material warehouse_inventorylog sales_receipt sales_transactionitem services_printingservice django_migrations}"
DB_EXEC="${DB_EXEC:-docker compose -f $COMPOSE_FILE exec -T db}"

log() { echo "$(date '+%F %T') $*"; }

# --- оповещение (то же, что в backup.sh) ---------------------------------------
env_value() {
    [ -r "$ENV_FILE" ] || return 0
    grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- | tr -d "\"'\r" || true
}

alert() {
    local msg
    msg="chpu-restore-check ($(hostname)): $1"
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

# --- работа с базой внутри контейнера -------------------------------------------
# Имя пользователя берём из окружения контейнера (POSTGRES_USER), как в backup.sh.
# shellcheck disable=SC2086,SC2016
dbsh() { $DB_EXEC sh -c "$@"; }

check_db=""
cleanup() {
    local status=$?
    if [ -n "$check_db" ]; then
        # shellcheck disable=SC2086,SC2016
        if $DB_EXEC sh -c 'dropdb -U "$POSTGRES_USER" --if-exists "$0"' "$check_db" >/dev/null 2>&1; then
            log "временная база $check_db удалена"
        else
            log "ВНИМАНИЕ: не удалось удалить временную базу $check_db — удалите вручную (dropdb)"
        fi
    fi
    if [ "$status" -ne 0 ]; then
        log "ПРОВЕРКА ВОССТАНОВЛЕНИЯ НЕ ПРОЙДЕНА (код $status)"
        alert "${FAIL_REASON:-проверка не пройдена (код $status), см. /var/log/chpu-restore-check.log}"
    fi
}
trap cleanup EXIT

FAIL_REASON=""
fail() { FAIL_REASON="$*"; log "ОШИБКА: $*"; exit 1; }

cd "$PROJECT_DIR" || fail "нет каталога проекта $PROJECT_DIR"

# --- 1. свежая копия есть --------------------------------------------------------
latest="$(find "$BACKUP_DIR" -maxdepth 1 -name 'db-*.sql.gz' -print 2>/dev/null | sort | tail -n 1)"
[ -n "$latest" ] || fail "в $BACKUP_DIR нет ни одной копии db-*.sql.gz"
fresh="$(find "$BACKUP_DIR" -maxdepth 1 -name "$(basename "$latest")" -mmin "-$((MAX_AGE_HOURS * 60))" -print)"
[ -n "$fresh" ] || fail "последняя копия $(basename "$latest") старше $MAX_AGE_HOURS ч — ночной backup не работает"
log "проверяю $latest ($(du -h "$latest" | cut -f1))"

# --- 2. целостность ------------------------------------------------------------
gzip -t "$latest" || fail "архив $latest повреждён (gzip -t)"
latest_media="$(find "$BACKUP_DIR" -maxdepth 1 -name 'media-*.tar.gz' -print 2>/dev/null | sort | tail -n 1)"
if [ -n "$latest_media" ]; then
    tar -tzf "$latest_media" >/dev/null || fail "архив фото $latest_media повреждён"
fi

# --- 3. заливка во временную базу ----------------------------------------------
check_db="chpu_restore_check_$(date +%s)"
log "создаю временную базу $check_db"
# shellcheck disable=SC2086,SC2016
$DB_EXEC sh -c 'createdb -U "$POSTGRES_USER" "$0"' "$check_db" || { check_db=""; fail "не удалось создать временную базу"; }

log "заливаю дамп (ON_ERROR_STOP, одна транзакция)"
# shellcheck disable=SC2086,SC2016
if ! gzip -dc "$latest" | $DB_EXEC sh -c 'psql -q -U "$POSTGRES_USER" -d "$0" -v ON_ERROR_STOP=1 --single-transaction -o /dev/null' "$check_db"; then
    fail "дамп НЕ заливается без ошибок — копия негодна"
fi

# --- 4. сверка строк с боевой базой ---------------------------------------------
count_in() {  # count_in БАЗА ТАБЛИЦА → число строк (или пусто при ошибке)
    # shellcheck disable=SC2086,SC2016
    $DB_EXEC sh -c 'psql -At -U "$POSTGRES_USER" -d "$0" -c "select count(*) from $1"' "$1" "$2" 2>/dev/null
}
# shellcheck disable=SC2016
live_db="$(dbsh 'printf %s "$POSTGRES_DB"')"
problems=""
for t in $KEY_TABLES; do
    restored="$(count_in "$check_db" "$t" || true)"
    live="$(count_in "$live_db" "$t" || true)"
    case "$restored$live" in
        ''|*[!0-9]*) problems="$problems $t(нет таблицы)"; log "  $t: не удалось сосчитать (копия=$restored боевая=$live)"; continue ;;
    esac
    log "  $t: копия=$restored боевая=$live"
    if [ "$live" -gt 0 ] && [ "$restored" -eq 0 ]; then
        problems="$problems $t(пусто в копии)"
    elif [ $((restored * 100)) -lt $((live * MIN_RATIO_PCT)) ]; then
        problems="$problems $t(копия $restored из $live)"
    fi
done
[ -z "$problems" ] || fail "сверка строк не сошлась:$problems"

log "OK: копия $(basename "$latest") восстанавливается, ключевые таблицы на месте"
