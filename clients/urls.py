from rest_framework.routers import DefaultRouter

from .views import ClientViewSet

router = DefaultRouter()
router.register("clients", ClientViewSet, basename="client")
# Очередь заявок на смену реферера («Заявки на рефералов») убрана по просьбе
# владельца 2026-09-27: ей почти не пользовались. Реферера меняет админ прямо в
# карточке клиента; старые заявки остались в базе и видны в django-admin.

urlpatterns = router.urls
