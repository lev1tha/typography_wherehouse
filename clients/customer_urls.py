from django.urls import path

from .customer import CustomerLoginView, CustomerOrdersView

urlpatterns = [
    path("login/", CustomerLoginView.as_view(), name="customer-login"),
    path("orders/", CustomerOrdersView.as_view(), name="customer-orders"),
]
