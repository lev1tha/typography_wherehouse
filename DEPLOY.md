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

Обычное обновление — одна команда (но перед ней — копия базы, см. ниже
«Обновление после правок кода»):

```bash
cd /opt/chpucenter && docker compose -f docker-compose.prod.yml up -d --build
```

Что происходит при `up -d --build`:

1. `db` поднимается и становится `healthy`;
2. сервис **`migrate`** (тот же образ) один раз выполняет: миграции → таблица
   кеша → collectstatic → выкладка фронтенда в `/srv/chpucenter/frontend`, и
   завершается. Фронтенд выкладывается **без пустого каталога**: новые файлы
   добавляются рядом со старыми, `index.html` подменяется последним
   (атомарно), старые хэшированные файлы в `assets/` живут ещё 14 дней — чтобы
   открытая вкладка со старой версией не получила ошибку загрузки модуля;
3. только если `migrate` завершился с кодом 0, пересоздаются `web` (и бот, если
   он включён). Ни `web`, ни бот сами миграции **не запускают**.

Отдельно прогнать выкладку (без пересоздания `web`) можно командой
`docker compose -f docker-compose.prod.yml run --rm migrate`. Если код и настройки
не менялись, `up -d` может не запускать `migrate` повторно — миграции при этом уже
применены, это нормально.

> Если `migrate` упал, `up -d` печатает ошибку и останавливается, а прежний
> контейнер `web` продолжает работать на старом коде — бесконечного цикла
> перезапусков нет. Что делать — раздел «Если выкладка не прошла» ниже.

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

> **Перед `down -v` — копия и проверка числа записей.** Команда выше только
> смотрит `sales_receipt` и `clients_client`; на боевой базе (с 18.08 там
> настоящие данные) сначала снимите копию (`sudo /usr/local/bin/chpu-backup`)
> и убедитесь, что она появилась в `/root/backups`. Том `pgdata` после `down -v`
> не возвращается.

После этого — `seed` (см. ниже), чтобы завести аккаунты и каталог-заготовку.

При старте стека сервис `migrate` сам: дождётся базы → применит миграции →
соберёт статику → выложит фронтенд в `/srv/chpucenter/frontend`.

Проверить:

```bash
docker compose -f docker-compose.prod.yml ps      # db и web Up (healthy); migrate в `ps` не виден — он уже завершился
docker compose -f docker-compose.prod.yml ps -a   # migrate: Exited (0)
docker compose -f docker-compose.prod.yml logs migrate | tail -20
docker compose -f docker-compose.prod.yml logs web | tail -20

# Заголовок Host обязателен: ALLOWED_HOSTS разрешает только chpucenter.com,
# поэтому запрос «просто на 127.0.0.1» вернёт 400 — это не поломка, а защита.
curl -sS -H 'Host: chpucenter.com' http://127.0.0.1:8001/api/health/   # ожидаем 200 {"status":"ok"}
```

`/api/health/` делает `SELECT 1`: **503** значит, что приложение живо, а база
недоступна. Тот же адрес проверяет healthcheck контейнера `web`.

### Первое наполнение (только на ПУСТОЙ базе)

```bash
docker compose -f docker-compose.prod.yml exec -e SEED_PASSWORD='<придумайте пароль>' web python manage.py seed
```

`seed` создаёт три учётки (`admin`, `storekeeper`, `accountant`) — **только если
в базе нет ни одного пользователя**, — и дополняет базовый каталог. Пароль
обязателен: переменная `SEED_PASSWORD` или `--password`; стандартных паролей в
коде нет. Повторный запуск безопасен: он **не меняет** существующих
пользователей, ставки и цены, не воскрешает удалённые учётки и не трогает
техкарты. Недостающие учётки в непустой базе — `--create-users`; только каталог
— `--no-users`.

> На боевой базе (там данные настоящие) `seed` **не запускают**. Пароль задаёте
> вы и храните в менеджере паролей; сменить позже:
> `docker compose -f docker-compose.prod.yml exec web python manage.py changepassword <логин>`.

### Если выкладка не прошла (`migrate` завершился с ошибкой)

Признак: `up -d --build` вывел `service "migrate" didn't complete successfully`.
**Прежний `web` при этом не тронут** и продолжает обслуживать сайт (compose
пересоздаёт `web` только после успешного `migrate`). Убедитесь в этом командой
`ps` ниже; если `web` всё же не работает — вернитесь на прежнюю версию кода
(раздел «Откат»).

```bash
cd /opt/chpucenter
docker compose -f docker-compose.prod.yml logs migrate | tail -50   # причина
docker compose -f docker-compose.prod.yml ps                        # web всё ещё Up (healthy)?
```

Дальше один из двух путей:

1. **Исправить и повторить.** Поправили код или данные (например, дубли, мешающие
   новой уникальности) → `git pull` → `up -d --build`. Миграция, упавшая
   посередине, откатывается целиком (каждая миграция идёт в транзакции
   PostgreSQL), так что повтор безопасен.
2. **Вернуться на прежнюю версию кода** (если чинить нечем и срочно) — раздел
   «Откат» ниже. Прежний образ `web` всё это время работает, откат нужен
   только чтобы код в репозитории соответствовал тому, что запущено.

Не делайте `down -v`, не удаляйте контейнер `db` и не переименовывайте файлы
миграций: имена уже применённых миграций записаны в `django_migrations`
(переименование однажды уронило прод).

Миграции, которые удаляют колонки или сужают типы, несовместимы со старым кодом:
перед такой выкладкой остановите `web` вручную (`docker compose … stop web`) —
иначе между миграцией и пересозданием контейнера старый код будет работать
против новой схемы.

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

## Включение защиты входа

Вход сотрудника, вход клиента и форма входа Django-админки считают неудачные
попытки **по адресу клиента**. За Cloudflare и nginx настоящий адрес приходит
только в заголовках, и пока nginx дописывает (`$proxy_add_x_forwarded_for`), а
не перезаписывает `X-Forwarded-For`, клиент может приписать слева любую
подпись и каждый раз получать чистый счётчик. Защита включается **в паре**:
конфиг nginx в репозитории (перезапись заголовка + адрес из `CF-Connecting-IP`
только для запросов от Cloudflare + лимит запросов на три входа) и переменная
`TRUSTED_PROXY_COUNT=1` в `.env.prod` (её читает бэкенд).

> **Порядок важен. Нельзя включать `TRUSTED_PROXY_COUNT` БЕЗ нового конфига
> nginx.** Со старым nginx бэкенд возьмёт адрес из заголовка, который nginx не
> перезаписывает, — а при любой ошибке в цепочке адресом клиента окажется адрес
> Cloudflare: **все пользователи попадут в один общий лимит**, и десять
> неудачных входов любого человека заблокируют вход всем.

1. **Обновить конфиг nginx на хосте** и перечитать:

   ```bash
   cd /opt/chpucenter && git pull
   sudo cp deploy/nginx/chpucenter.com /etc/nginx/sites-available/chpucenter.com
   sudo nginx -t && sudo systemctl reload nginx
   ```

   `nginx -t` обязан сказать `test is successful`. Если пишет про неизвестную
   директиву `set_real_ip_from` — в сборке nginx нет модуля `realip`
   (в пакетах Debian/Ubuntu он есть); до выяснения дальше не идите.
2. **Добавить в `.env.prod`** строку:

   ```
   TRUSTED_PROXY_COUNT=1
   ```
3. **Пересоздать контейнер**, чтобы он прочитал переменную (`restart` не
   подходит — `env_file` читается при создании):

   ```bash
   cd /opt/chpucenter && docker compose -f docker-compose.prod.yml up -d --force-recreate web
   ```
4. **Проверка**: два запроса с одного адреса и разными поддельными
   `X-Forwarded-For` должны считаться одним клиентом. Сделайте 11 неудачных
   входов подряд, каждый раз меняя подпись:

   ```bash
   for i in $(seq 1 11); do
     curl -s -o /dev/null -w "%{http_code} " -X POST https://chpucenter.com/api/token/ \
       -H 'Content-Type: application/json' -H "X-Forwarded-For: 10.0.0.$i" \
       -d '{"username":"проверка-лимита","password":"неверный"}'
   done; echo
   ```

   Ожидаемо: сначала `401`, затем `429` (одиннадцатая попытка). Если все
   одиннадцать — `401`, защита не включилась: проверьте шаги 1–3. После
   проверки подождите окно блокировки (около минуты) — выдуманный логин
   «проверка-лимита» настоящим пользователям не мешает.

   В логе nginx (`/var/log/nginx/chpucenter.access.log`) в первой колонке
   должен стоять реальный адрес клиента, а не адрес Cloudflare
   (`173.245.…`, `104.16.…`, `172.64.…`).

**Откат**: убрать строку `TRUSTED_PROXY_COUNT` из `.env.prod` и пересоздать
`web` (шаг 3) — поведение вернётся прежнее. Конфиг nginx при этом можно
оставить: сам по себе он безопасен.

**Лимит запросов в nginx.** Три входа (`/api/token/`, `/api/customer/login/`,
`/django-admin/login/`) ограничены на уровне nginx: 30 запросов в минуту с
одного адреса с запасом 20 без задержки, дальше `429`. Остальной `/api/` лимитом
не затронут (включая `/api/token/refresh/`). Это первый рубеж; второй — счётчик
неудачных попыток в самом Django. Правится в начале `deploy/nginx/chpucenter.com`
(`limit_req_zone … rate=…`, `limit_req … burst=…`).

**Диапазоны Cloudflare** в конфиге получены 2026-10-10 с
<https://www.cloudflare.com/ips-v4> и <https://www.cloudflare.com/ips-v6>. Раз в
полгода сверяйте с этими страницами и при расхождении обновляйте список
`set_real_ip_from` (затем `nginx -t && systemctl reload nginx`): адрес Cloudflare,
которого нет в списке, перестанет считаться доверенным, и запросы с него
получат адрес самого Cloudflare.

---

## Обновление после правок кода

Порядок: **копия → код → проверка до → выкладка → проверка после**. Копия снимается
всегда, даже для «мелкой» правки: миграции необратимы чаще, чем кажется.

```bash
cd /opt/chpucenter

# 1. Копия базы и фото (если скрипт установлен как chpu-backup; иначе deploy/backup.sh)
sudo /usr/local/bin/chpu-backup

# 2. Что было ДО выкладки — запишите числа
git rev-parse --short HEAD
docker compose -f docker-compose.prod.yml exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "select (select count(*) from sales_receipt), (select count(*) from clients_client), (select count(*) from warehouse_material)"'
docker compose -f docker-compose.prod.yml exec web python manage.py showmigrations | grep -c '\[X\]'

# 3. Выкладка
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

Миграции, статика и свежий фронтенд применяются сервисом `migrate` до запуска
нового `web` (см. «Шаг 4»).

> **Первая выкладка после перехода на сервис `migrate` и ротацию логов**
> пересоздаст и контейнер `db` (у него поменялись настройки логирования):
> PostgreSQL перезапустится на несколько секунд, данные в томе `pgdata`
> сохраняются. Делайте её в спокойное время и обязательно после копии (шаг 1).

### Чек-лист после выкладки

- [ ] `docker compose -f docker-compose.prod.yml ps` — `db` и `web` в состоянии `Up (healthy)`
      (`healthy` у web появляется в пределах минуты);
- [ ] `docker compose -f docker-compose.prod.yml ps -a` — `migrate` в `Exited (0)`;
- [ ] `curl -sS -H 'Host: chpucenter.com' http://127.0.0.1:8001/api/health/` — `{"status":"ok"}`;
- [ ] те же три числа (чеки / клиенты / материалы), что и «до», — равны или больше
      (меньше быть не должно, если выкладка не чистила данные намеренно);
- [ ] `showmigrations`: нет строк `[ ]` (непримённых) —
      `docker compose -f docker-compose.prod.yml exec web python manage.py showmigrations | grep '\[ \]'` должен быть пуст;
- [ ] https://chpucenter.com открывается, вход выполняется, `/django-admin/` со стилями;
- [ ] в `docker compose … logs --since 10m web` нет трейсбеков (500).

### Откат

Откат кода — это пересборка предыдущей версии:

```bash
cd /opt/chpucenter
git log --oneline -10                 # найти версию, которая работала
git checkout <sha-или-тег>            # детачнутый HEAD — нормально для отката
docker compose -f docker-compose.prod.yml up -d --build
```

После починки вернитесь на ветку: `git checkout main && git pull`.

**Откат миграций — только осознанно.** Если выкладка добавила миграции, старый
код может не работать с новой схемой, а обратная миграция иногда теряет данные
(удаляет колонки). Порядок:

1. Откройте копию, снятую в шаге 1, и решите, что дороже: потерять данные,
   внесённые после выкладки, или откатить схему.
2. Откат схемы приложения до конкретной миграции (имя смотрите в
   `showmigrations`):
   `docker compose -f docker-compose.prod.yml exec web python manage.py migrate <app> <предыдущая_миграция>`
   — выполняется **до** `git checkout` старого кода (старый код не знает
   новых миграций и откатить их не сможет).
3. Затем `git checkout <старая версия>` и `up -d --build`.
4. Если откат миграции невозможен или потеряет данные — восстановление из копии
   в **новую** базу («Резервные копии → Восстановление»), а не в действующую.

**Не использовать** для отката `docker compose down -v`: он удаляет том с базой
вместе со всеми данными.

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
sudo install -m 755 /opt/chpucenter/deploy/restore_check.sh /usr/local/bin/chpu-restore-check
sudo mkdir -p /root/backups
sudo /usr/local/bin/chpu-backup        # проверить руками, что копия снимается
sudo /usr/local/bin/chpu-restore-check # и что она восстанавливается
```

Затем в `sudo crontab -e` — ежедневно в 3 ночи копия (хранить 14 дней) и раз в
неделю, в воскресенье в 4:30, автоматическая проверка восстановления:

```
MAILTO=you@example.com
0 3 * * *  /usr/local/bin/chpu-backup >> /var/log/chpu-backup.log 2>&1
30 4 * * 0 /usr/local/bin/chpu-restore-check >> /var/log/chpu-restore-check.log 2>&1
```

> **Почему скриптом, а не строкой в cron.** Однострочник
> `pg_dump … | gzip > db-$(date +%F).sql.gz && find … -delete` ломается ровно
> тогда, когда он нужнее всего: если `pg_dump` упал (контейнер не поднят, база
> занята), `gzip` всё равно создаёт файл — пустой, — и следующая команда честно
> удаляет старые копии. Через две недели таких ночей копий не остаётся вовсе, и
> узнают об этом в тот день, когда данные понадобились. Скрипт пишет дамп во
> временный файл, проверяет его (`gzip -t`, дамп дошёл до конца, не слишком мал,
> есть данные таблиц) и только после этого запускает ротацию; при любой ошибке
> выходит с **ненулевым кодом**, пишет в лог, шлёт оповещение и **не трогает
> старое**.

Настройки — переменными окружения (`KEEP_DAYS`, `BACKUP_DIR`, `MEDIA_DIR`,
`MIN_DUMP_BYTES`), значения по умолчанию в шапке скрипта.

### Как узнать о сбое копии

Скрипт при ошибке завершается с кодом ≠ 0 и оповещает так (первое доступное):

1. `BACKUP_ALERT_CMD` — любая команда оболочки, текст ошибки в переменной
   `BACKUP_ALERT_MESSAGE`. Задаётся строкой в crontab перед командой, например:
   `BACKUP_ALERT_CMD='mail -s "chpu-backup FAIL" you@example.com <<<"$BACKUP_ALERT_MESSAGE"'`.
2. Telegram — если в `.env.prod` заполнены `TELEGRAM_STAFF_BOT_TOKEN` и
   `TELEGRAM_STAFF_CHAT_IDS` (те же, что у уведомлений о низких остатках), сообщение
   уйдёт в эти чаты. Файл только читается, токен в списке процессов не светится.
3. Иначе — только лог `/var/log/chpu-backup.log` и код выхода. Тогда сбой заметите
   лишь через `MAILTO` в crontab (cron присылает вывод, если у сервера настроена
   почта) или проверяя лог глазами. **Пока токены Telegram пусты, а почты нет,
   копия может молча не сниматься** — заполните токены или настройте
   `BACKUP_ALERT_CMD`.

Недельная проверка (`chpu-restore-check`) заодно ловит «молчаливо остановившийся
cron»: если последняя копия старше 36 часов, это ошибка с тем же оповещением.

### Где хранить `.env.prod`

В копию его **не кладём намеренно**: там `SECRET_KEY`, пароль базы и токены, а
копии лежат на диске в открытом виде. Но без него базу не поднять. Храните
отдельную копию `.env.prod` в **менеджере паролей** (вложением или текстом) и
обновляйте после каждого изменения файла. `SECRET_KEY` особенно важен: потеряв
его, вы разлогините всех и сломаете подписанные токены.

### Копии лежат на том же сервере

Диск умирает вместе с ними, поэтому раз в сутки их нужно забирать **наружу**.
Существующую схему это не ломает — скрипт по-прежнему кладёт всё в `/root/backups`.

Вариант 1 (с вашего компьютера, вручную раз в неделю):

```bash
scp 'root@167.233.170.216:/root/backups/db-*.sql.gz' 'root@167.233.170.216:/root/backups/media-*.tar.gz' ~/chpu-backups/
```

Вариант 2 (автоматически, сразу после копии): `BACKUP_UPLOAD_CMD` — команда,
получающая пути в `BACKUP_DB_FILE` и `BACKUP_MEDIA_FILE`. С `rclone` (настройте
`rclone config` на любое облако, например `remote:`):

```
0 3 * * * BACKUP_UPLOAD_CMD='rclone copy "$BACKUP_DB_FILE" remote:chpu-backups/ && rclone copy "$BACKUP_MEDIA_FILE" remote:chpu-backups/' /usr/local/bin/chpu-backup >> /var/log/chpu-backup.log 2>&1
```

Сбой выгрузки — тоже ошибка (код 1, оповещение), но уже снятая локальная копия
остаётся, ротация выполняется. Копии содержат клиентов и деньги: храните
внешнюю копию в закрытом хранилище, лучше зашифрованной (`rclone crypt`).

### Проверка копии руками

Раз в неделю это делает `chpu-restore-check`; вручную — так же, во **временную**
базу, а не в боевую:

```bash
cd /opt/chpucenter
F=/root/backups/db-2026-08-27.sql.gz       # подставьте нужную копию
gzip -t "$F" && echo "архив цел"
docker compose -f docker-compose.prod.yml exec -T db sh -c 'createdb -U "$POSTGRES_USER" chpu_check'
gunzip -c "$F" | docker compose -f docker-compose.prod.yml exec -T db sh -c \
  'psql -q -U "$POSTGRES_USER" -d chpu_check -v ON_ERROR_STOP=1 --single-transaction -o /dev/null'
docker compose -f docker-compose.prod.yml exec -T db sh -c \
  'psql -U "$POSTGRES_USER" -d chpu_check -c "select (select count(*) from sales_receipt) as чеки, (select count(*) from clients_client) as клиенты, (select count(*) from warehouse_material) as материалы"'
docker compose -f docker-compose.prod.yml exec -T db sh -c 'dropdb -U "$POSTGRES_USER" chpu_check'
```

Код возврата первой команды `psql` обязан быть 0, а числа — похожи на боевые.

### Восстановление (авария)

**Не заливайте дамп в действующую базу.** Дамп содержит `CREATE TABLE`/`COPY` для
таблиц, которые там уже есть: `psql` без `ON_ERROR_STOP` отвечает сотней ошибок
(«already exists», «duplicate key»), продолжает и **всё равно завершается с кодом
0** — выглядит как успех, а на деле база осталась смесью старого и нового
(проверено: 262 ошибки при коде 0). Правильно — поднять **новую** базу, залить в
неё, проверить и только потом переключить приложение.

```bash
cd /opt/chpucenter
F=/root/backups/db-2026-08-27.sql.gz         # нужная копия
DC="docker compose -f docker-compose.prod.yml"

# 1. Новая пустая база рядом с боевой
$DC exec -T db sh -c 'createdb -U "$POSTGRES_USER" chpu_restored'

# 2. Заливка: одна транзакция, остановка на первой ошибке. Любая ошибка = откат
#    всей заливки и ненулевой код — «наполовину залитой» базы не бывает.
gunzip -c "$F" | $DC exec -T db sh -c \
  'psql -U "$POSTGRES_USER" -d chpu_restored -v ON_ERROR_STOP=1 --single-transaction -o /dev/null'
echo "код заливки: $?"                        # обязан быть 0

# 3. Проверка: число строк ключевых таблиц
$DC exec -T db sh -c 'psql -U "$POSTGRES_USER" -d chpu_restored -c "
  select (select count(*) from sales_receipt) as чеки,
         (select count(*) from clients_client) as клиенты,
         (select count(*) from warehouse_material) as материалы,
         (select count(*) from accounts_user) as пользователи,
         (select count(*) from django_migrations) as миграции"'
```

Если числа правдоподобны — **переключение** (простой несколько минут; перед ним
снимите копию того, что есть сейчас, даже сломанного):

```bash
$DC stop web                                  # приложение не пишет в базу
sudo /usr/local/bin/chpu-backup               # копия текущего состояния — на всякий случай
$DC exec -T db sh -c 'psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "ALTER DATABASE \"$POSTGRES_DB\" RENAME TO \"${POSTGRES_DB}_broken\"" \
  -c "ALTER DATABASE chpu_restored RENAME TO \"$POSTGRES_DB\""'
$DC up -d                                     # migrate докатит схему, web поднимется
```

`RENAME` не сработает, пока к базе подключены клиенты; поэтому `web` останавливаем
первым, а бот (`--profile bot`), если он включён, — `$DC stop customer-bot`.
Старая база остаётся как `<имя>_broken` до тех пор, пока вы не убедитесь, что
всё работает; удалять её — осознанным решением (`dropdb`).

Фото восстанавливаются отдельно:
`tar -xzf /root/backups/media-<дата>.tar.gz -C /srv/chpucenter && chown -R 1000:1000 /srv/chpucenter/media`.

Если нужно восстановить базу **на новом сервере**: пройти «Шаг 1» и «Шаг 2»
(положив туда `.env.prod` из менеджера паролей), поднять только базу
(`docker compose -f docker-compose.prod.yml up -d db`), выполнить пункты 1–3
этого раздела, переименовать `chpu_restored` в боевое имя (как в блоке
«переключение», без шага `_broken`) и только потом `up -d`.

---

## Что проверить после первого запуска

- [ ] https://chpucenter.com открывается, замок в браузере зелёный
- [ ] www.chpucenter.com редиректит на основной домен
- [ ] Вход админом работает; пароль — свой (из менеджера паролей), стандартных паролей в системе нет
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
