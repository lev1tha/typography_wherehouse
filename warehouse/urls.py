from rest_framework.routers import DefaultRouter

from .views import (
    InventoryLogViewSet,
    MaterialImageViewSet,
    MaterialMonthOpeningViewSet,
    MaterialTypeViewSet,
    MaterialViewSet,
    RollViewSet,
)

router = DefaultRouter()
router.register("materials", MaterialViewSet, basename="material")
router.register("material-types", MaterialTypeViewSet, basename="material-type")
router.register("material-images", MaterialImageViewSet, basename="material-image")
router.register("inventory-logs", InventoryLogViewSet, basename="inventory-log")
router.register("rolls", RollViewSet, basename="roll")
router.register("month-openings", MaterialMonthOpeningViewSet, basename="month-opening")

urlpatterns = router.urls
