"""Площадь реза — это работа станка, и возврат материала её не отменяет.

Плитка показывала «Лазер 0 кв.м · 3,7 пог.м реза · 444 сом»: деньги брались со
строки РАБОТЫ (она жива), а площадь — со строки материала, которую клиент
вернул. Станок при этом отрезал: работа была, и в отчёте по станкам она должна
остаться.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt
from services.models import PrintingService
from warehouse.models import Material


class CuttingAreaAfterRefundTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="ca_admin", password="x", role=User.Role.ADMIN
        )
        self.customer = Client.objects.create(full_name="Тахир", phone="+996555444333")
        self.client.force_authenticate(self.admin)
        self.mat = Material.objects.create(
            name="Акрил 3мм", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
            price_per_sqm=Decimal("1500"),
        )
        self.laser = PrintingService.objects.create(
            name="Резка лазером", kind=PrintingService.Kind.CUTTING,
            machine=PrintingService.Machine.LASER, rate_per_pm=Decimal("120"),
        )
        self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": self.mat.id, "form": "SHEET", "width": "1.22",
            "height": "2.44", "sheet_count": "5", "purchase_cost": "12000",
        }, format="json")

    def _cut_order(self):
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True, "client_id": self.customer.id,
            "items": [{"type": "SERVICE", "service": self.laser.id, "material": self.mat.id,
                       "width": "0.4", "length": "0.6", "running_meters": "3.7"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return Receipt.objects.get(pk=r.data["id"])

    def _laser(self):
        rows = self.client.get("/api/finance/report/").data["cutting"]["rows"]
        return next((m for m in rows if m["id"] == "LASER"), {"area": 0, "amount": 0})

    def test_area_and_money_are_both_there_before_any_refund(self):
        self._cut_order()
        laser = self._laser()
        self.assertEqual(Decimal(str(laser["area"])), Decimal("0.24"))
        self.assertEqual(Decimal(str(laser["amount"])), Decimal("444"))

    def test_refunding_the_material_does_not_zero_the_machine_area(self):
        """Ровно случай владельца: вернули материал, работу оставили."""
        receipt = self._cut_order()
        material_line = receipt.items.filter(material__isnull=False).first()
        r = self.client.post(f"/api/sales/receipts/{receipt.id}/refund/",
                             {"item_ids": [material_line.id]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

        laser = self._laser()
        # Деньги за работу остались — значит и площадь обязана остаться.
        self.assertEqual(Decimal(str(laser["amount"])), Decimal("444"))
        self.assertEqual(
            Decimal(str(laser["area"])), Decimal("0.24"),
            "возврат материала обнулил работу станка",
        )

    def test_fully_refunded_order_drops_out_entirely(self):
        """А полностью возвращённый заказ из отчёта уходит целиком — он отменён."""
        receipt = self._cut_order()
        self.client.post(f"/api/sales/receipts/{receipt.id}/refund/", {}, format="json")
        laser = self._laser()
        self.assertEqual(Decimal(str(laser["area"])), Decimal("0"))
        self.assertEqual(Decimal(str(laser["amount"])), Decimal("0"))
