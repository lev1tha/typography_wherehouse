from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    ExpenseViewSet,
    FinanceReportView,
    FinanceSettingsView,
    MaterialReportView,
)

router = DefaultRouter()
router.register("expenses", ExpenseViewSet, basename="expense")

urlpatterns = [
    path("report/", FinanceReportView.as_view(), name="finance-report"),
    path("material-report/", MaterialReportView.as_view(), name="finance-material-report"),
    path("settings/", FinanceSettingsView.as_view(), name="finance-settings"),
] + router.urls
