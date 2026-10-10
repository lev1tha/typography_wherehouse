# ЧПУ Система — конфиг сайта для nginx на хосте.
#
#   sudo cp deploy/nginx/chpucenter.com /etc/nginx/sites-available/chpucenter.com
#   sudo ln -s /etc/nginx/sites-available/chpucenter.com /etc/nginx/sites-enabled/
#   sudo nginx -t && sudo systemctl reload nginx
#
# Схема: Cloudflare (proxy) → этот nginx → gunicorn в docker на 127.0.0.1:8001.
# Статику, медиа и сам фронтенд nginx отдаёт с диска, минуя Django.

upstream chpu_app {
    server 127.0.0.1:8001;
}

# --- Лимит запросов на входы (защита от подбора пароля) ---
# Файл подключается внутрь http{}, поэтому limit_req_zone/map здесь уместны.
# Ключ — адрес клиента ПОСЛЕ модуля realip (см. ниже): за Cloudflare это
# CF-Connecting-IP, а не адрес самого Cloudflare, иначе все пользователи
# делили бы один лимит. Пустой ключ = запрос не считается, так лимит
# применяется только к трём дверям, а остальной /api/ не затронут.
#
# 30 запросов в минуту с одного адреса + запас burst=20 без задержки: живой
# человек (и целый офис за одним адресом) до этого не доберётся, перебор
# паролей упрётся. Это ПЕРВЫЙ рубеж; второй — счётчик неудачных попыток в
# самом Django (accounts/throttling.py).
map $uri $chpu_login_key {
    default                  "";
    /api/token/              $binary_remote_addr;
    /api/customer/login/     $binary_remote_addr;
    /django-admin/login/     $binary_remote_addr;
}
limit_req_zone $chpu_login_key zone=chpu_login:10m rate=30r/m;
limit_req_status 429;
limit_req_log_level warn;

# --- HTTP: только редирект на HTTPS ---
server {
    listen 80;
    listen [::]:80;
    server_name chpucenter.com www.chpucenter.com;

    # Оставляем открытым для проверки Let's Encrypt (если будете брать certbot
    # вместо сертификата Cloudflare).
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

# --- www → без www (канонический домен один) ---
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name www.chpucenter.com;

    ssl_certificate     /etc/ssl/chpucenter/fullchain.pem;
    ssl_certificate_key /etc/ssl/chpucenter/privkey.pem;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    return 301 https://chpucenter.com$request_uri;
}

# --- Основной сайт ---
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name chpucenter.com;

    ssl_certificate     /etc/ssl/chpucenter/fullchain.pem;
    ssl_certificate_key /etc/ssl/chpucenter/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;

    # Фото материалов заливают с телефона — дефолтного лимита в 1 МБ мало.
    client_max_body_size 25m;

    # --- Настоящий адрес клиента за Cloudflare ---
    # Запросы приходят с адресов Cloudflare, а клиент — в заголовке
    # CF-Connecting-IP. Верим этому заголовку ТОЛЬКО если соединение пришло
    # из диапазонов Cloudflare (set_real_ip_from): с любого другого адреса
    # заголовок игнорируется, подделать его нельзя.
    # Диапазоны получены 2026-10-10 с https://www.cloudflare.com/ips-v4 и
    # https://www.cloudflare.com/ips-v6. Cloudflare меняет их редко, но
    # меняет — раз в полгода сверяйте и обновляйте список (DEPLOY.md,
    # «Включение защиты входа»).
    set_real_ip_from 173.245.48.0/20;
    set_real_ip_from 103.21.244.0/22;
    set_real_ip_from 103.22.200.0/22;
    set_real_ip_from 103.31.4.0/22;
    set_real_ip_from 141.101.64.0/18;
    set_real_ip_from 108.162.192.0/18;
    set_real_ip_from 190.93.240.0/20;
    set_real_ip_from 188.114.96.0/20;
    set_real_ip_from 197.234.240.0/22;
    set_real_ip_from 198.41.128.0/17;
    set_real_ip_from 162.158.0.0/15;
    set_real_ip_from 104.16.0.0/13;
    set_real_ip_from 104.24.0.0/14;
    set_real_ip_from 172.64.0.0/13;
    set_real_ip_from 131.0.72.0/22;
    set_real_ip_from 2400:cb00::/32;
    set_real_ip_from 2606:4700::/32;
    set_real_ip_from 2803:f800::/32;
    set_real_ip_from 2405:b500::/32;
    set_real_ip_from 2405:8100::/32;
    set_real_ip_from 2a06:98c0::/29;
    set_real_ip_from 2c0f:f248::/32;
    real_ip_header CF-Connecting-IP;

    # HSTS: браузер запоминает «этот домен только по https» и больше не ходит
    # по http вообще — даже по прямой ссылке. Ставится ЗДЕСЬ, а не только в
    # Django: фронтенд и статику nginx отдаёт с диска, мимо приложения, и на
    # самый частый ответ (index.html) заголовок Django просто не попадает.
    #
    # `always` обязателен — иначе заголовка нет на ответах 3xx/4xx/5xx.
    # Год и includeSubDomains — то, что требует preload-список; сам preload не
    # включаем: он необратим на месяцы, а поддомены могут понадобиться.
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    access_log /var/log/nginx/chpucenter.access.log;
    error_log  /var/log/nginx/chpucenter.error.log;

    # Собранный фронтенд (кладётся контейнером web при каждом старте).
    root /srv/chpucenter/frontend;
    index index.html;

    # --- Django ---
    # django-admin, а не admin: на /admin/* живут экраны самой системы (React),
    # и отдавать их Django нельзя — он уводил на свою форму входа.
    location ~ ^/(api|django-admin)/ {
        proxy_pass http://chpu_app;
        proxy_http_version 1.1;

        proxy_set_header Host              $host;
        # $remote_addr после realip — настоящий клиент. X-Forwarded-For
        # ПЕРЕЗАПИСЫВАЕМ, а не дописываем ($proxy_add_x_forwarded_for оставлял
        # слева то, что прислал сам клиент, и подпись в заголовке обходила
        # лимиты входа). Работает в паре с TRUSTED_PROXY_COUNT=1 в .env.prod.
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $remote_addr;
        # Без этого заголовка Django за прокси считает соединение http —
        # см. SECURE_PROXY_SSL_HEADER в config/settings.py.
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host  $host;

        proxy_read_timeout 60s;
        proxy_redirect off;

        # Лимит срабатывает только на трёх входах (см. map вверху файла).
        limit_req zone=chpu_login burst=20 nodelay;
    }

    # --- Файлы, которые Django отдавать не должен ---
    # Свой add_header в location ОТМЕНЯЕТ наследование заголовков сервера —
    # поэтому HSTS повторяется в каждой такой локации. Забыть здесь значит
    # раздавать половину сайта без политики и не заметить этого.
    location /static/ {
        alias /srv/chpucenter/static/;
        access_log off;
        expires 30d;
        add_header Cache-Control "public, immutable";
        add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    }

    location /media/ {
        alias /srv/chpucenter/media/;
        access_log off;
        expires 7d;
    }

    # --- SPA: любой неизвестный путь отдаём index.html, роутинг делает React ---
    location / {
        try_files $uri $uri/ /index.html;
    }

    # Хеш в имени файла меняется при каждой сборке → можно кэшировать надолго.
    location /assets/ {
        access_log off;
        expires 1y;
        add_header Cache-Control "public, immutable";
        add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    }
}
