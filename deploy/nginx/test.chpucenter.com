# nginx тестового стенда: /etc/nginx/sites-available/test.chpucenter.com
# Ставится рядом с прод-конфигом chpucenter.com и его не трогает (STAGING.md).
#
# Отличия от прода:
#   * приложение на 127.0.0.1:8002, файлы из /srv/chpucenter-staging;
#   * весь сайт, КРОМЕ /api/, закрыт общим паролем (Basic Auth). /api/ закрыть
#     так нельзя: фронтенд кладёт в заголовок Authorization свой токен
#     (Bearer), и браузер уже не может отправить в нём пароль nginx — каждый
#     запрос получал бы 401. API и так пускает только по токену входа;
#   * поверх страниц красная плашка «ТЕСТОВЫЙ СТЕНД» — чтобы никто не провёл
#     настоящую продажу не в той вкладке;
#   * поисковикам запрещено индексировать (X-Robots-Tag).
#
# Сертификат — тот же Origin-сертификат Cloudflare, что у прода: по умолчанию
# Cloudflare выписывает его на chpucenter.com и *.chpucenter.com. Проверить:
#   openssl x509 -in /etc/ssl/chpucenter/fullchain.pem -noout -ext subjectAltName

upstream chpu_staging_app {
    server 127.0.0.1:8002;
}

# Свой лимит попыток входа, независимый от прода (имена зон глобальны на весь
# nginx, поэтому не chpu_login).
map $uri $chpu_staging_login_key {
    default                  "";
    /api/token/              $binary_remote_addr;
    /api/customer/login/     $binary_remote_addr;
    /django-admin/login/     $binary_remote_addr;
}
limit_req_zone $chpu_staging_login_key zone=chpu_staging_login:10m rate=30r/m;

server {
    listen 80;
    listen [::]:80;
    server_name test.chpucenter.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name test.chpucenter.com;

    ssl_certificate     /etc/ssl/chpucenter/fullchain.pem;
    ssl_certificate_key /etc/ssl/chpucenter/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;

    client_max_body_size 25m;

    # Настоящий адрес клиента из-за Cloudflare — как в прод-конфиге.
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

    access_log /var/log/nginx/chpucenter-staging.access.log;
    error_log  /var/log/nginx/chpucenter-staging.error.log;

    auth_basic           "Test stand";
    auth_basic_user_file /etc/nginx/chpu-staging.htpasswd;

    add_header X-Robots-Tag "noindex, nofollow" always;

    root  /srv/chpucenter-staging/frontend;
    index index.html;

    location ~ ^/(api|django-admin)/ {
        auth_basic off;
        proxy_pass http://chpu_staging_app;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host  $host;
        proxy_read_timeout 60s;
        proxy_redirect off;
        limit_req zone=chpu_staging_login burst=20 nodelay;
        limit_req_status 429;
        add_header X-Robots-Tag "noindex, nofollow" always;
    }

    location /static/ {
        alias /srv/chpucenter-staging/static/;
        access_log off;
    }

    location /media/ {
        alias /srv/chpucenter-staging/media/;
        access_log off;
    }

    location /assets/ {
        access_log off;
        expires 1y;
        add_header Cache-Control "public, immutable";
        add_header X-Robots-Tag "noindex, nofollow" always;
    }

    # SPA: любой путь → index.html, и в него вставляется плашка стенда.
    location / {
        try_files $uri $uri/ /index.html;
        add_header Cache-Control "no-cache";
        add_header X-Robots-Tag "noindex, nofollow" always;
        sub_filter_once on;
        sub_filter '</body>' '<div style="position:fixed;left:50%;bottom:0;transform:translateX(-50%);z-index:2147483647;background:#c62828;color:#fff;font:600 12px/1.6 system-ui,sans-serif;padding:2px 12px;border-radius:6px 6px 0 0;pointer-events:none;letter-spacing:.04em">ТЕСТОВЫЙ СТЕНД — не прод</div></body>';
    }
}
