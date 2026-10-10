from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    FifoRecalcView,
    LotCorrectionView,
    WasteView,
    InventoryLogViewSet,
    MaterialImageViewSet,
    MaterialMonthOpeningViewSet,
    MaterialTypeViewSet,
    ProductionSiteViewSet,
    MaterialViewSet,
    RollStocktakeViewSet,
    RollViewSet,
    StockTransferViewSet,
    SupplierOpeningDebtViewSet,
    SupplierPaymentViewSet,
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
router.register("roll-stocktakes", RollStocktakeViewSet, basename="roll-stocktake")
router.register("month-openings", MaterialMonthOpeningViewSet, basename="month-opening")
router.register("suppliers", SupplierViewSet, basename="supplier")
router.register("supplies", SupplyViewSet, basename="supply")
router.register("supplier-payments", SupplierPaymentViewSet, basename="supplier-payment")
router.register("supplier-opening-debts", SupplierOpeningDebtViewSet, basename="supplier-opening-debt")
router.register("transfers", StockTransferViewSet, basename="stock-transfer")

urlpatterns = [
    path("waste/", WasteView.as_view(), name="waste"),
    path("lot-correction/preview/", LotCorrectionView.as_view(mode="preview"), name="lot-correction-preview"),
    path("lot-correction/apply/", LotCorrectionView.as_view(mode="apply"), name="lot-correction-apply"),
    path("fifo-recalc/preview/", FifoRecalcView.as_view(mode="preview"), name="fifo-recalc-preview"),
    path("fifo-recalc/apply/", FifoRecalcView.as_view(mode="apply"), name="fifo-recalc-apply"),
] + router.urls
