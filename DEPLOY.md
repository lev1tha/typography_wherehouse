# Деплой ЧПУ Системы на сервер

Домен: **chpucenter.com** · сервер: **167.233.170.216** · DNS: Cloudflare (proxy включён).

## Как всё устроено

```
Интернет → Cloudflare → nginx на хосте (:443) ─┬─ /            → /srv/chpucenter/frontend  (React, статика)
                                               ├─ /static/     → /srv/chpucenter/static    (админка Django)
                                               ├─ /media/      → /srv/chpucenter/media     (фото материалов)
                                               └─ /api/, /admin/ → 127.0.0.1:8001 → docker: gunicorn → Django
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

```bash
cd /opt/chpucenter
docker compose -f docker-compose.prod.yml up -d --build
```

При старте контейнер сам: дождётся базы → применит миграции → соберёт статику →
выложит фронтенд в `/srv/chpucenter/frontend`.

Проверить:

```bash
docker compose -f docker-compose.prod.yml ps      # оба сервиса healthy/running
docker compose -f docker-compose.prod.yml logs -f web
curl -I http://127.0.0.1:8001/admin/              # ожидаем 301/302, не connection refused
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

## Резервные копии

База живёт в docker-томе `pgdata`. Дамп:

```bash
docker compose -f docker-compose.prod.yml exec -T db \
    pg_dump -U chpu chpu | gzip > /root/backup-$(date +%F).sql.gz
```

Автоматизировать (ежедневно в 3 ночи, хранить 14 дней) — `crontab -e`:

```
0 3 * * * cd /opt/chpucenter && docker compose -f docker-compose.prod.yml exec -T db pg_dump -U chpu chpu | gzip > /root/backups/db-$(date +\%F).sql.gz && find /root/backups -name 'db-*.sql.gz' -mtime +14 -delete
```

Не забыть про загруженные фото — `/srv/chpucenter/media`.

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
- [ ] `/admin/` открывается со стилями (значит `/static/` отдаётся)
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
- **HSTS выключен** (`SECURE_HSTS_SECONDS=0`). Включать (`31536000`) после
  того, как убедились, что https стабилен — браузеры запомнят это на год.
