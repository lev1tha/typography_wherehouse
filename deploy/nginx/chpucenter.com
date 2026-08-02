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
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        # Без этого заголовка Django за прокси считает соединение http —
        # см. SECURE_PROXY_SSL_HEADER в config/settings.py.
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host  $host;

        proxy_read_timeout 60s;
        proxy_redirect off;
    }

    # --- Файлы, которые Django отдавать не должен ---
    location /static/ {
        alias /srv/chpucenter/static/;
        access_log off;
        expires 30d;
        add_header Cache-Control "public, immutable";
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
    }
}
