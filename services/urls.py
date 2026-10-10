from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    PricingRulesView,
    PricingSettingsView,
    PrintingServiceViewSet,
    RateMatrixViewSet,
    ResolvedRateView,
    ServiceRecipeViewSet,
    ThicknessCoefficientViewSet,
)

router = DefaultRouter()
router.register("services", PrintingServiceViewSet, basename="service")
router.register("recipes", ServiceRecipeViewSet, basename="recipe")
router.register("rate-matrix", RateMatrixViewSet, basename="rate-matrix")
router.register("thickness-coefficients", ThicknessCoefficientViewSet, basename="thickness-coef")

urlpatterns = router.urls + [
    path("settings/", PricingSettingsView.as_view(), name="pricing-settings"),
    path("rules/", PricingRulesView.as_view(), name="pricing-rules"),
    path("rate/", ResolvedRateView.as_view(), name="pricing-rate"),
]
