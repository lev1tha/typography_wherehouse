# Тестовый стенд — test.chpucenter.com

Отдельная копия системы на том же сервере, что и прод (167.233.170.216): на ней
проверяем новые версии и пробуем операции, не трогая боевые данные.

```
Cloudflare → nginx на хосте ─┬─ chpucenter.com      → 127.0.0.1:8001 → проект chpucenter          (прод)
                             └─ test.chpucenter.com → 127.0.0.1:8002 → проект chpucenter-staging  (стенд)
```

| | Прод | Стенд |
|---|---|---|
| Код | `/opt/chpucenter` | `/opt/chpucenter-staging` (свой git-чекаут, может стоять на любой ветке) |
| Compose | `docker-compose.prod.yml` | `docker-compose.staging.yml` |
| Настройки | `.env.prod` | `.env.staging` (пустые токены Telegram, свой `SECRET_KEY`) |
| База | том `chpucenter_pgdata` | том `chpucenter-staging_pgdata` — копия прода по команде |
| Файлы для nginx | `/srv/chpucenter/*` | `/srv/chpucenter-staging/*` |
| nginx | `sites-available/chpucenter.com` | `sites-available/test.chpucenter.com` |
| Вход | учётки системы | пароль nginx на страницы + те же учётки системы |

На стенде нет клиентского бота, уведомлений в Telegram и настоящей оплаты: в
базе стенда живые телефоны и Telegram-аккаунты клиентов, и стенд не должен им
писать. Скрипт обновления откажется работать, если в `.env.staging` появится
токен бота.

Внизу каждой страницы стенда — красная плашка «ТЕСТОВЫЙ СТЕНД — не прод».

---

## Первый запуск (один раз)

Все команды ниже — на сервере под root. Оболочка там **fish**: многострочные
куски запускать через `bash`, переменные вида `$(...)` fish тоже не понимает —
поэтому сначала набрать `bash`.

### 0. Cloudflare

DNS → **Add record**: тип `A`, имя `test`, адрес `167.233.170.216`,
**Proxied** (оранжевое облако).

### 1. Проверить, что серверу хватает места

```bash
ssh root@167.233.170.216
bash
free -h      # стенду нужно ~400–500 МБ памяти
df -h /      # и ~2 ГБ диска под образ и базу
# Сертификат прода должен покрывать *.chpucenter.com:
openssl x509 -in /etc/ssl/chpucenter/fullchain.pem -noout -ext subjectAltName
```

Если в выводе последней команды нет `*.chpucenter.com` — в Cloudflare
(SSL/TLS → Origin Server) выписать новый Origin-сертификат на
`chpucenter.com, *.chpucenter.com`, положить его на место старого и сделать
`systemctl reload nginx`.

### 2. Код и каталоги

Файлы стенда (`docker-compose.staging.yml`, `deploy/staging_refresh.sh`,
`deploy/nginx/test.chpucenter.com`) должны быть уже запушены в GitHub.

```bash
git clone "$(git -C /opt/chpucenter remote get-url origin)" /opt/chpucenter-staging
mkdir -p /srv/chpucenter-staging/{static,media,frontend}
chown -R 1000:1000 /srv/chpucenter-staging
```

### 3. Настройки стенда

```bash
cd /opt/chpucenter-staging
cp .env.staging.example .env.staging
nano .env.staging
```

Заполнить `SECRET_KEY` (новый, не прод-овский), `POSTGRES_PASSWORD` и тот же
пароль внутри `DATABASE_URL`, `FINANCE_PASSWORD`. **Не копировать `.env.prod`**
— в нём токены Telegram.

### 4. Пароль на вход в стенд

Логин `test`, пароль придумать (его будут вводить все, кто смотрит стенд):

```bash
printf 'test:%s\n' "$(openssl passwd -apr1 'ПРИДУМАННЫЙ-ПАРОЛЬ')" > /etc/nginx/chpu-staging.htpasswd
chmod 640 /etc/nginx/chpu-staging.htpasswd
chown root:www-data /etc/nginx/chpu-staging.htpasswd
```

### 5. nginx

```bash
cp /opt/chpucenter-staging/deploy/nginx/test.chpucenter.com /etc/nginx/sites-available/
ln -s /etc/nginx/sites-available/test.chpucenter.com /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

`nginx -t` обязателен: при ошибке в конфиге `reload` не выполнится, и прод
продолжит работать на старой конфигурации.

### 6. Сборка и копия базы прода

```bash
cd /opt/chpucenter-staging
docker compose -f docker-compose.staging.yml build
bash deploy/staging_refresh.sh
```

Скрипт спросит подтверждение (`yes`), снимет дамп прода (прод только
читается), зальёт его в базу стенда, скопирует фото материалов, применит
миграции из кода стенда и запустит его.

### 7. Проверка

```bash
docker compose -f docker-compose.staging.yml ps     # web — healthy
cd /opt/chpucenter && docker compose -f docker-compose.prod.yml ps   # прод тоже healthy
```

Открыть https://test.chpucenter.com — браузер спросит логин `test` и пароль из
шага 4, дальше обычный вход в систему теми же учётками, что на проде.

---

## Обычная работа

**Проверить новую версию (ветку) на стенде:**

```bash
cd /opt/chpucenter-staging
git fetch
git checkout ИМЯ-ВЕТКИ && git pull
docker compose -f docker-compose.staging.yml up -d --build
```

**Освежить данные (снова копия прода на эту минуту):**

```bash
bash /opt/chpucenter-staging/deploy/staging_refresh.sh
```

Всё, что вводили на стенде, при этом пропадёт. Тем же способом стенд
откатывается после ветки с миграциями: вернуть код на `main`
(`git checkout main && git pull`), пересобрать (`up -d --build`) и залить
базу заново — откатывать миграции руками не нужно.

**Логи:** `docker compose -f docker-compose.staging.yml logs -f web`

**Остановить стенд, не удаляя данных:** `docker compose -f docker-compose.staging.yml stop`

**Удалить стенд совсем** — только из `/opt/chpucenter-staging` и только этим
файлом (`-v` сносит том, а с прод-файлом снёс бы боевую базу):

```bash
cd /opt/chpucenter-staging
docker compose -f docker-compose.staging.yml down -v
rm /etc/nginx/sites-enabled/test.chpucenter.com && nginx -t && systemctl reload nginx
```

---

## Если правите `docker-compose.prod.yml`

`docker-compose.staging.yml` — отдельная копия, а не надстройка (почему — в
шапке файла). Новый сервис, переменную или том, добавленные в прод-файл,
повторить и здесь, сохранив отличия: `name`, тег образа, порт 8002, каталоги
`/srv/chpucenter-staging`, `.env.staging`, без клиентского бота.
