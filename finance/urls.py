from django.urls import path
from rest_framework.routers import DefaultRouter

from .payroll_views import (
    PayrollAccrueView,
    PayrollAdjustmentViewSet,
    PayrollDefaultPeriodView,
    PayrollExportView,
    PayrollPaymentViewSet,
    PayrollView,
    PaySchemeViewSet,
    RecurringExpenseViewSet,
    RecurringRunView,
)
from .views import (
    BridgeView,
    CashEntryViewSet,
    CashFlowView,
    PeriodExportView,
    PeriodLockView,
    CompanyProfileView,
    DailyReportView,
    ExpenseEntryViewSet,
    ExpenseKindViewSet,
    FinanceReportView,
    FinanceSettingsView,
    FinanceUnlockView,
    MaterialReportView,
    PnlView,
    TaxRateViewSet,
    WhatIfView,
)

router = DefaultRouter()
router.register("cash", CashEntryViewSet, basename="cash")
router.register("expense-kinds", ExpenseKindViewSet, basename="expense-kind")
router.register("expense-entries", ExpenseEntryViewSet, basename="expense-entry")
router.register("tax-rates", TaxRateViewSet, basename="tax-rate")
router.register("pay-schemes", PaySchemeViewSet, basename="pay-scheme")
router.register("recurring", RecurringExpenseViewSet, basename="recurring-expense")
router.register("payroll/payments", PayrollPaymentViewSet, basename="payroll-payment")
router.register("payroll/adjustments", PayrollAdjustmentViewSet, basename="payroll-adjustment")

urlpatterns = [
    path("report/", FinanceReportView.as_view(), name="finance-report"),
    path("material-report/", MaterialReportView.as_view(), name="finance-material-report"),
    path("daily/", DailyReportView.as_view(), name="finance-daily"),
    path("pnl/what-if/", WhatIfView.as_view(), name="finance-pnl-what-if"),
    path("pnl/", PnlView.as_view(), name="finance-pnl"),
    path("cash-flow/", CashFlowView.as_view(), name="finance-cash-flow"),
    path("bridge/", BridgeView.as_view(), name="finance-bridge"),
    path("export/period/", PeriodExportView.as_view(), name="finance-export-period"),
    path("recurring/run/", RecurringRunView.as_view(), name="finance-recurring-run"),
    path("payroll/", PayrollView.as_view(), name="finance-payroll"),
    path("payroll/accrue/", PayrollAccrueView.as_view(), name="finance-payroll-accrue"),
    path("payroll/export/", PayrollExportView.as_view(), name="finance-payroll-export"),
    path("payroll/default-period/", PayrollDefaultPeriodView.as_view(), name="finance-payroll-period"),
    path("settings/", FinanceSettingsView.as_view(), name="finance-settings"),
    path("company/", CompanyProfileView.as_view(), name="finance-company"),
    path("period/", PeriodLockView.as_view(), name="finance-period"),
    path("unlock/", FinanceUnlockView.as_view(), name="finance-unlock"),
] + router.urls
