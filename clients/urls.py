from rest_framework.routers import DefaultRouter

from django.urls import path

from .views import ClientPriceViewSet, ClientSettingsView, ClientViewSet, OpeningBalanceViewSet

router = DefaultRouter()
router.register("clients", ClientViewSet, basename="client")
# Входящие остатки при переезде из Excel (волна 2).
router.register("opening-balances", OpeningBalanceViewSet, basename="opening-balance")
# Договорные цены клиента (волна 2, CLI-02).
router.register("client-prices", ClientPriceViewSet, basename="client-price")
# Очередь заявок на смену реферера («Заявки на рефералов») убрана по просьбе
# владельца 2026-09-27: ей почти не пользовались. Реферера меняет админ прямо в
# карточке клиента; старые заявки остались в базе и видны в django-admin.

urlpatterns = [path("settings/", ClientSettingsView.as_view(), name="client-settings")] + router.urls
