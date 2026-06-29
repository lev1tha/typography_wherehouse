from rest_framework.routers import DefaultRouter

from .views import ClientViewSet, ReferralChangeRequestViewSet

router = DefaultRouter()
router.register("clients", ClientViewSet, basename="client")
router.register(
    "referral-requests", ReferralChangeRequestViewSet, basename="referral-request"
)

urlpatterns = router.urls
