from django.urls import path

from .customer import (
    CustomerLoginView,
    CustomerOrdersView,
    CustomerStatementView,
    CustomerSummaryView,
)

urlpatterns = [
    path("login/", CustomerLoginView.as_view(), name="customer-login"),
    path("orders/", CustomerOrdersView.as_view(), name="customer-orders"),
    path("statement/", CustomerStatementView.as_view(), name="customer-statement"),
    path("summary/", CustomerSummaryView.as_view(), name="customer-summary"),
]
