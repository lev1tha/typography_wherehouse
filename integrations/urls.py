from django.urls import path

from .views import (
    MockPayView,
    PaymentWebhookView,
    TelegramCustomerWebhookView,
)

urlpatterns = [
    path("payments/webhook/", PaymentWebhookView.as_view(), name="payment-webhook"),
    path("payments/mock/<uuid:receipt_id>/", MockPayView.as_view(), name="mock-pay"),
    path(
        "telegram/customer/webhook/",
        TelegramCustomerWebhookView.as_view(),
        name="telegram-customer-webhook",
    ),
]
