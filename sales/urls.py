from rest_framework.routers import DefaultRouter

from .quotes import QuoteViewSet
from .views import ReceiptViewSet

router = DefaultRouter()
router.register("receipts", ReceiptViewSet, basename="receipt")
router.register("quotes", QuoteViewSet, basename="quote")

urlpatterns = router.urls
