"""Услуги с ценой 0 из каталога не уходят в чек молча.

У резки, гравировки и отходов пустая каталожная цена уже даёт понятную ошибку.
Фиксированные услуги («Прочее», установка) и наружная установка (ставка за
букву) её не имели: чек на 0.00 оформлялся со статусом 201.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from services.models import PrintingService


class ZeroPriceServiceTests(APITestCase):
    URL = "/api/sales/receipts/checkout/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="zp_boss", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)

    def _service_sale(self, service, qty=1):
        return self.client.post(self.URL, {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "SERVICE", "service": service.id, "quantity": qty}],
        }, format="json")

    def test_other_with_zero_base_price_is_refused(self):
        svc = PrintingService.objects.create(
            name="Прочее", kind=PrintingService.Kind.OTHER, base_price=Decimal("0")
        )
        resp = self._service_sale(svc)
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("не задана фиксированная цена", str(resp.data))

    def test_exterior_install_with_zero_rate_is_refused(self):
        svc = PrintingService.objects.create(
            name="Наружная установка", kind=PrintingService.Kind.INSTALL_EXTERIOR,
            rate_per_piece=Decimal("0"),
        )
        resp = self._service_sale(svc, qty=10)
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("не задана ставка за букву", str(resp.data))

    def test_priced_services_still_work(self):
        other = PrintingService.objects.create(
            name="Прочее", kind=PrintingService.Kind.OTHER, base_price=Decimal("500")
        )
        ext = PrintingService.objects.create(
            name="Наружная установка", kind=PrintingService.Kind.INSTALL_EXTERIOR,
            rate_per_piece=Decimal("40"),
        )
        self.assertEqual(self._service_sale(other).status_code, 201)
        resp = self._service_sale(ext, qty=10)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Decimal(resp.data["total_price"]), Decimal("400"))

    def test_add_items_is_guarded_too(self):
        other = PrintingService.objects.create(
            name="Прочее", kind=PrintingService.Kind.OTHER, base_price=Decimal("500")
        )
        free = PrintingService.objects.create(
            name="Пустая", kind=PrintingService.Kind.OTHER, base_price=Decimal("0")
        )
        first = self._service_sale(other)
        resp = self.client.post(
            f"/api/sales/receipts/{first.data['id']}/add-items/",
            {"items": [{"type": "SERVICE", "service": free.id, "quantity": 1}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
