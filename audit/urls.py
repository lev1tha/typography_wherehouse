from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import AuditLogViewSet, ClientPurchasesView, DashboardView

router = DefaultRouter()
router.register("logs", AuditLogViewSet, basename="audit-log")

urlpatterns = [
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
    path("client-purchases/", ClientPurchasesView.as_view(), name="client-purchases"),
] + router.urls
