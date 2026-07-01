from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    DailyReportView,
    ExpenseViewSet,
    FinanceReportView,
    FinanceSettingsView,
    FinanceUnlockView,
    MaterialReportView,
)

router = DefaultRouter()
router.register("expenses", ExpenseViewSet, basename="expense")

urlpatterns = [
    path("report/", FinanceReportView.as_view(), name="finance-report"),
    path("material-report/", MaterialReportView.as_view(), name="finance-material-report"),
    path("daily/", DailyReportView.as_view(), name="finance-daily"),
    path("settings/", FinanceSettingsView.as_view(), name="finance-settings"),
    path("unlock/", FinanceUnlockView.as_view(), name="finance-unlock"),
] + router.urls
