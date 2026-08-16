from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    CashEntryViewSet,
    PeriodLockView,
    CompanyProfileView,
    DailyReportView,
    ExpenseEntryViewSet,
    ExpenseKindViewSet,
    FinanceReportView,
    FinanceSettingsView,
    FinanceUnlockView,
    MaterialReportView,
)

router = DefaultRouter()
router.register("cash", CashEntryViewSet, basename="cash")
router.register("expense-kinds", ExpenseKindViewSet, basename="expense-kind")
router.register("expense-entries", ExpenseEntryViewSet, basename="expense-entry")

urlpatterns = [
    path("report/", FinanceReportView.as_view(), name="finance-report"),
    path("material-report/", MaterialReportView.as_view(), name="finance-material-report"),
    path("daily/", DailyReportView.as_view(), name="finance-daily"),
    path("settings/", FinanceSettingsView.as_view(), name="finance-settings"),
    path("company/", CompanyProfileView.as_view(), name="finance-company"),
    path("period/", PeriodLockView.as_view(), name="finance-period"),
    path("unlock/", FinanceUnlockView.as_view(), name="finance-unlock"),
] + router.urls
