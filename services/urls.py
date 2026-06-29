from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import PrintingServiceViewSet, PricingSettingsView, ServiceRecipeViewSet

router = DefaultRouter()
router.register("services", PrintingServiceViewSet, basename="service")
router.register("recipes", ServiceRecipeViewSet, basename="recipe")

urlpatterns = router.urls + [
    path("settings/", PricingSettingsView.as_view(), name="pricing-settings"),
]
