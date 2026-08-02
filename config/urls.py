"""URL configuration for the Cloude backend."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    # НЕ «admin/»: этот префикс занят интерфейсом самой системы — у React-роутера
    # на нём висят /admin/finance, /admin/catalog и остальные экраны админа.
    # Пока Django-админка стояла здесь же, любая ПОЛНАЯ загрузка такого адреса
    # (переход после входа, F5, ссылка из чата) уходила на сервер, Django узнавал
    # свой префикс раньше фронтенда и отправлял человека на
    # /admin/login/?next=/admin/finance — форму входа Django вместо системы.
    # На деве не воспроизводилось: Vite проксирует на Django только /api и
    # /media, поэтому /admin/finance там всегда отдавал фронтенд.
    path("django-admin/", admin.site.urls),
    # Auth
    path("api/", include("accounts.urls")),
    # Domain APIs (registered per-app as they are built out)
    path("api/warehouse/", include("warehouse.urls")),
    path("api/services/", include("services.urls")),
    path("api/clients/", include("clients.urls")),
    path("api/sales/", include("sales.urls")),
    path("api/audit/", include("audit.urls")),
    path("api/integrations/", include("integrations.urls")),
    path("api/finance/", include("finance.urls")),
    path("api/customer/", include("clients.customer_urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
