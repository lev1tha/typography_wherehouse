from rest_framework.routers import DefaultRouter

from .views import (
    InventoryLogViewSet,
    MaterialImageViewSet,
    MaterialViewSet,
    RollViewSet,
)

router = DefaultRouter()
router.register("materials", MaterialViewSet, basename="material")
router.register("material-images", MaterialImageViewSet, basename="material-image")
router.register("inventory-logs", InventoryLogViewSet, basename="inventory-log")
router.register("rolls", RollViewSet, basename="roll")

urlpatterns = router.urls
