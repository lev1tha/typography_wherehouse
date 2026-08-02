from django.conf import settings
from django.urls import path

from .views import (
    MockPayView,
    PaymentWebhookView,
    TelegramCustomerWebhookView,
)

urlpatterns = [
    path("payments/webhook/", PaymentWebhookView.as_view(), name="payment-webhook"),
    path(
        "telegram/customer/webhook/",
        TelegramCustomerWebhookView.as_view(),
        name="telegram-customer-webhook",
    ),
]

# Заглушка «оплата прошла» — только для разработки. В проде её быть НЕ должно:
# она без авторизации и, зная UUID чека, позволила бы кому угодно закрыть заказ
# как оплаченный и списать склад.
# Раньше условие пускало её ещё и при PAYMENT_GATEWAY=mock — а на проде шлюз
# именно mock, пока нет ключей FreedomPay, то есть дыра была открыта.
if settings.DEBUG:
    urlpatterns.append(
        path("payments/mock/<uuid:receipt_id>/", MockPayView.as_view(), name="mock-pay")
    )
