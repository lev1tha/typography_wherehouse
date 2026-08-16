from rest_framework.routers import DefaultRouter

from .views import (
    InventoryLogViewSet,
    MaterialImageViewSet,
    MaterialMonthOpeningViewSet,
    MaterialTypeViewSet,
    ProductionSiteViewSet,
    MaterialViewSet,
    RollViewSet,
    SupplierViewSet,
    SupplyViewSet,
)

router = DefaultRouter()
router.register("materials", MaterialViewSet, basename="material")
router.register("material-types", MaterialTypeViewSet, basename="material-type")
router.register("production-sites", ProductionSiteViewSet, basename="production-site")
router.register("material-images", MaterialImageViewSet, basename="material-image")
router.register("inventory-logs", InventoryLogViewSet, basename="inventory-log")
router.register("rolls", RollViewSet, basename="roll")
router.register("month-openings", MaterialMonthOpeningViewSet, basename="month-opening")
router.register("suppliers", SupplierViewSet, basename="supplier")
router.register("supplies", SupplyViewSet, basename="supply")

urlpatterns = router.urls
