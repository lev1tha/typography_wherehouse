# Деплой ЧПУ Системы на сервер

Домен: **chpucenter.com** · сервер: **167.233.170.216** · DNS: Cloudflare (proxy включён).

## Как всё устроено

```
Интернет → Cloudflare → nginx на хосте (:443) ─┬─ /            → /srv/chpucenter/frontend  (React, статика)
                                               ├─ /static/     → /srv/chpucenter/static    (админка Django)
                                               ├─ /media/      → /srv/chpucenter/media     (фото материалов)
                                               └─ /api/, /django-admin/ → 127.0.0.1:8001 → docker: gunicorn → Django
                                                                                    docker: PostgreSQL
```

- **nginx живёт на хосте** (`/etc/nginx/sites-available/chpucenter.com`), не в докере.
- **В докере** только Django (gunicorn) и PostgreSQL — `docker-compose.prod.yml`.
- Порт приложения (`8001`) слушает **только localhost**, снаружи в него не попасть.
- База наружу не публикуется вообще — доступна только контейнеру приложения.

---

## Шаг 1. Подготовка сервера (один раз)

```bash
ssh root@167.233.170.216

apt update && apt upgrade -y
apt install -y docker.io docker-compose-plugin nginx git
systemctl enable --now docker nginx
```

Каталоги, из которых nginx отдаёт файлы (их же монтирует докер):

```bash
mkdir -p /srv/chpucenter/{static,media,frontend}

# Контейнер работает от непривилегированного пользователя с uid 1000. Каталоги
# создаются под root, и без chown приложение не сможет в них писать —
# collectstatic упадёт с «Permission denied».
chown -R 1000:1000 /srv/chpucenter
```

Файрвол — наружу только SSH и веб:

```bash
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw enable
```

---

## Шаг 2. Код и настройки

```bash
git clone <адрес-репозитория> /opt/chpucenter
cd /opt/chpucenter

cp .env.prod.example .env.prod
nano .env.prod
```

Обязательно заполнить в `.env.prod`:

| Переменная | Чем заполнить |
|---|---|
| `SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `POSTGRES_PASSWORD` | свой пароль |
| `DATABASE_URL` | тот же пароль внутри строки подключения |
| `FINANCE_PASSWORD` | свой пароль на финансовые экраны |

Остальное (`ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `SITE_BASE_URL`) уже заполнено под chpucenter.com.

> **Важно:** файл должен называться ровно `.env.prod` и лежать рядом с
> `docker-compose.prod.yml` — оба сервиса читают его через `env_file`.
> Переменные оттуда попадают **внутрь контейнеров**; подставлять их в сам
> compose-файл через `${...}` нельзя (compose для подстановки читает только
> шелл и файл с именем ровно `.env`).

---

## Шаг 3. SSL-сертификат

DNS проксируется через Cloudflare (оранжевое облако), поэтому самый простой и надёжный
путь — **Origin-сертификат Cloudflare**: он живёт 15 лет и не требует продления.

1. Cloudflare → **SSL/TLS → Origin Server → Create Certificate** → создать.
2. Положить на сервер:

```bash
mkdir -p /etc/ssl/chpucenter
nano /etc/ssl/chpucenter/fullchain.pem   # вставить Origin Certificate
nano /etc/ssl/chpucenter/privkey.pem     # вставить Private Key
chmod 600 /etc/ssl/chpucenter/privkey.pem
```

3. Cloudflare → **SSL/TLS → Overview** → режим **Full (strict)**.

> Режим **Flexible** не использовать: между Cloudflare и сервером трафик пойдёт
> открытым, а Django за прокси будет считать соединение защищённым.

<details>
<summary>Альтернатива — Let's Encrypt вместо сертификата Cloudflare</summary>

```bash
apt install -y certbot python3-certbot-nginx
mkdir -p /var/www/certbot
certbot --nginx -d chpucenter.com -d www.chpucenter.com
```

Пути к сертификату в конфиге nginx поменять на `/etc/letsencrypt/live/chpucenter.com/…`.
Обновляется автоматически таймером certbot.
</details>

---

## Шаг 4. Запуск приложения

Обычное обновление — одна команда:

```bash
cd /opt/chpucenter && docker compose -f docker-compose.prod.yml up -d --build
```

### Если `migrate` упал с «relation … does not exist»

Так выглядит база от старой схемы. Миграции пересобраны с нуля (по одной
`0001_initial` на приложение), и на сервере, который уже поднимался, имена в
`django_migrations` не совпадают: Django считает `0001_initial` применённой,
пропускает создание таблиц и падает на первой же миграции с данными.

Лечится пересозданием базы, но **сначала посмотрите, что в ней лежит** —
команда только читает:

```bash
cd /opt/chpucenter && docker compose -f docker-compose.prod.yml exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select (select count(*) from sales_receipt) as чеки, (select count(*) from clients_client) as клиенты;"'
```

**Не нули — стоп.** Разберитесь, что это за записи, прежде чем сносить: дальше
идёт необратимая команда. Однажды там нашлись 4 чека и 2 клиента, и хорошо, что
их успели опознать как тестовые.

Нули (или вы точно знаете, что записи выбрасываемые) — пересоздавайте.
`down -v` удаляет только том `pgdata`; статика, медиа и фронтенд лежат в
`/srv/chpucenter/*` на хосте и не пострадают:

```bash
cd /opt/chpucenter && docker compose -f docker-compose.prod.yml down -v && docker compose -f docker-compose.prod.yml up -d --build
```

После этого — `seed` (см. ниже), чтобы завести аккаунты и каталог-заготовку.

При старте контейнер сам: дождётся базы → применит миграции → соберёт статику →
выложит фронтенд в `/srv/chpucenter/frontend`.

Проверить:

```bash
docker compose -f docker-compose.prod.yml ps      # оба сервиса Up (healthy)
docker compose -f docker-compose.prod.yml logs web | tail -20

# Заголовок Host обязателен: ALLOWED_HOSTS разрешает только chpucenter.com,
# поэтому запрос «просто на 127.0.0.1» вернёт 400 — это не поломка, а защита.
curl -I -H 'Host: chpucenter.com' http://127.0.0.1:8001/django-admin/login/   # ожидаем 200
```

Создать рабочие аккаунты (каталог + услуги + 2 пользователя):

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py seed
```

**Сразу сменить пароли** (`seed` создаёт стандартные `admin/admin12345`):

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py changepassword admin
docker compose -f docker-compose.prod.yml exec web python manage.py changepassword storekeeper
```

---

## Шаг 5. nginx

```bash
cp /opt/chpucenter/deploy/nginx/chpucenter.com /etc/nginx/sites-available/chpucenter.com
ln -s /etc/nginx/sites-available/chpucenter.com /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default     # убрать заглушку «Welcome to nginx»

nginx -t && systemctl reload nginx
```

Открыть **https://chpucenter.com** — должна появиться страница входа.

---

## Обновление после правок кода

```bash
cd /opt/chpucenter
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

Миграции, статика и свежий фронтенд подтянутся сами при старте контейнера.

---

## HSTS (только https)

`Strict-Transport-Security` говорит браузеру: этот домен открывать только по
https — и он перестаёт ходить по http вообще, даже по прямой ссылке. Заголовок
ставит **nginx**, а не Django: фронтенд и статику nginx отдаёт с диска, мимо
приложения, и на самый частый ответ (`index.html`) заголовок Django просто не
попал бы.

Конфиг уже в репозитории; на сервере его надо разложить и перечитать:

```bash
sudo cp /opt/chpucenter/deploy/nginx/chpucenter.com /etc/nginx/sites-available/chpucenter.com
sudo nginx -t && sudo systemctl reload nginx
```

Проверить, что заголовок отдаётся — и на странице, и на статике:

```bash
curl -sI https://chpucenter.com | grep -i strict
curl -sI https://chpucenter.com/assets/ | grep -i strict
```

Ожидаемо в обоих случаях: `strict-transport-security: max-age=31536000; includeSubDomains`.

Две тонкости, из-за которых HSTS обычно оказывается наполовину нерабочим:

- `always` — без него nginx не ставит заголовок на ответы 3xx/4xx/5xx;
- свой `add_header` внутри `location` **отменяет** наследование заголовков
  сервера, поэтому HSTS повторён в `/static/` и `/assets/`. Забыть там — значит
  раздавать половину сайта без политики и не заметить.

`preload` не включаем: попадание в preload-список браузеров необратимо на
месяцы, а поддомены (например, отдельный для бота) могут понадобиться.

Если сайт стоит за Cloudflare в режиме proxy, тот же заголовок можно включить и
в его панели (SSL/TLS → Edge Certificates → HSTS) — тогда он появится даже на
ответах, которые Cloudflare отдаёт из кэша сам.

---

## Резервные копии

База живёт в docker-томе `pgdata`, фото — на диске хоста (`/srv/chpucenter/media`).
Копии снимает `deploy/backup.sh`: он забирает и базу, и фото.

Установка (один раз):

```bash
sudo install -m 755 /opt/chpucenter/deploy/backup.sh /usr/local/bin/chpu-backup
sudo mkdir -p /root/backups
sudo /usr/local/bin/chpu-backup        # проверить руками, что копия снимается
```

Затем в `sudo crontab -e` — ежедневно в 3 ночи, хранить 14 дней:

```
0 3 * * * /usr/local/bin/chpu-backup >> /var/log/chpu-backup.log 2>&1
```

> **Почему скриптом, а не строкой в cron.** Однострочник
> `pg_dump … | gzip > db-$(date +%F).sql.gz && find … -delete` ломается ровно
> тогда, когда он нужнее всего: если `pg_dump` упал (контейнер не поднят, база
> занята), `gzip` всё равно создаёт файл — пустой, — и следующая команда честно
> удаляет старые копии. Через две недели таких ночей копий не остаётся вовсе, и
> узнают об этом в тот день, когда данные понадобились. Скрипт пишет дамп во
> временный файл, проверяет, что он не пуст, и только после этого запускает
> ротацию; при любой ошибке выходит с ненулевым кодом и **не трогает старое**.

Настройки — переменными окружения (`KEEP_DAYS`, `BACKUP_DIR`, `MEDIA_DIR`,
`MIN_DUMP_BYTES`), значения по умолчанию в шапке скрипта.

**Копию нужно хотя бы раз восстановить.** Непроверенная копия — это не копия:
проверять её в день аварии поздно. Раз в пару месяцев:

```bash
gunzip -c /root/backups/db-2026-08-27.sql.gz | \
  docker compose -f docker-compose.prod.yml exec -T db psql -U chpu -d postgres \
    -c 'DROP DATABASE IF EXISTS chpu_check' -c 'CREATE DATABASE chpu_check'
```

и залить дамп в `chpu_check`, а не в боевую базу.

**Копии лежат на том же сервере.** Диск умирает вместе с ними — раз в неделю
забирайте архив к себе: `scp root@167.233.170.216:/root/backups/db-*.sql.gz .`

Восстановление:

```bash
gunzip -c backup-2026-07-31.sql.gz | \
  docker compose -f docker-compose.prod.yml exec -T db psql -U chpu chpu
```

---

## Что проверить после первого запуска

- [ ] https://chpucenter.com открывается, замок в браузере зелёный
- [ ] www.chpucenter.com редиректит на основной домен
- [ ] Вход админом работает, пароли по умолчанию **изменены**
- [ ] `/django-admin/` открывается со стилями (значит `/static/` отдаётся)
- [ ] `/admin/finance` после ПОЛНОЙ перезагрузки открывает систему, а не форму
      входа Django (`/admin/*` — экраны React, Django туда лезть не должен)
- [ ] Загрузка фото материала работает и картинка видна (значит `/media/` отдаётся)
- [ ] Клиентский портал: вход по телефону + код, выданный из карточки
- [ ] Финансовые экраны просят отдельный пароль

## Известные ограничения на старте

- **Платёжный шлюз в режиме `mock`** — реальные онлайн-оплаты не проводятся.
  Пока `PAYMENT_GATEWAY=mock`, доступен служебный эндпоинт «оплата прошла»
  без авторизации (он же нужен для тестов). Перед приёмом настоящих денег:
  выставить `PAYMENT_GATEWAY=freedompay` и заполнить ключи — эндпоинт при этом
  автоматически исчезнет.
- **Telegram-уведомления молчат**, пока не заполнены токены. После заполнения
  бот клиентов поднимается отдельно:
  `docker compose -f docker-compose.prod.yml --profile bot up -d`
- **HSTS включён в nginx** (`deploy/nginx/chpucenter.com`), см. раздел ниже.
  В Django он остаётся выключенным (`SECURE_HSTS_SECONDS=0`) НАМЕРЕННО: иначе
  на ответы `/api/` заголовок уйдёт дважды — от приложения и от nginx.
