"""Волна 2, п. 8 (STK-07/G4-N1): КИМ раскроя у материала.

Кусок 0.77 кв.м при КИМ 62 % снимает со склада 0.77 / 0.62 = 1.2419 кв.м, и вся
эта площадь — в себестоимости строки. Целые листы — как есть. Пусто — как было.
Возврат строки возвращает на склад всё, что она забрала.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales import sale_service
from sales.models import Receipt
from warehouse.models import InventoryLog, Material, Roll
from warehouse.rolls import receive_lot

CHECKOUT = "/api/sales/receipts/checkout/"


class KimTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="kim_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.m = Material.objects.create(
            name="акрил 3 мм 3050x2050", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("3.05"), sheet_height=Decimal("2.05"),
            price_per_sqm=Decimal("1550"), piece_price=Decimal("9690"), kim_percent=Decimal("62"),
        )
        # 6.2525 кв.м × 5 = 31.2625 кв.м за 29 075.
        receive_lot(self.m, form=Roll.Form.SHEET, width=Decimal("3.05"), height=Decimal("2.05"),
                    sheet_count=Decimal("5"), purchase_cost=Decimal("29075"))
        self.m.refresh_from_db()

    def sell(self, **item):
        r = self.client.post(CHECKOUT, {"payment_method": "CASH", "pay_full": True,
                                        "items": [{"type": "MATERIAL", "material": self.m.id, **item}]},
                             format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def test_piece_consumes_area_over_kim_and_costs_it(self):
        data = self.sell(mode="SQM", quantity="0.77")
        log = InventoryLog.objects.get(type=InventoryLog.Type.SALE)
        self.assertEqual(log.quantity_changed, Decimal("-1.2419"))
        self.assertIn("КИМ 62 %", log.reason)
        # 29 075 × 1.2419 / 31.2625 = 1 155.005 — обрезки в себестоимости строки.
        self.assertEqual(Decimal(str(data["items"][0]["cost_total"])), Decimal("1155.00"))
        self.m.refresh_from_db()
        self.assertEqual(self.m.quantity, Decimal("31.2625") - Decimal("1.2419"))

    def test_return_gives_back_everything_the_line_took(self):
        data = self.sell(mode="SQM", quantity="0.77")
        sale_service.refund_receipt(Receipt.objects.get(pk=data["id"]), user=self.admin)
        self.m.refresh_from_db()
        self.assertEqual(self.m.quantity, Decimal("31.2625"))
        self.assertEqual(Roll.objects.get(material=self.m).remaining_area, Decimal("31.2625"))

    def test_whole_sheet_is_not_nested(self):
        self.sell(mode="PIECE", quantity="1")
        log = InventoryLog.objects.get(type=InventoryLog.Type.SALE)
        self.assertEqual(log.quantity_changed, Decimal("-6.2525"))

    def test_empty_kim_is_as_before(self):
        self.m.kim_percent = None
        self.m.save()
        self.sell(mode="SQM", quantity="0.77")
        self.assertEqual(InventoryLog.objects.get(type=InventoryLog.Type.SALE).quantity_changed, Decimal("-0.7700"))

    def test_kim_never_takes_more_than_stock(self):
        self.client.post("/api/warehouse/materials/write-off/",
                         {"material": self.m.id, "sheets": "4", "reason_code": "OTHER"}, format="json")
        self.sell(mode="SQM", quantity="5")             # 5 / 0.62 = 8.06 > 6.2525 на складе
        self.m.refresh_from_db()
        self.assertEqual(self.m.quantity, Decimal("0"))

    def test_kim_validation(self):
        r = self.client.patch(f"/api/warehouse/materials/{self.m.id}/", {"kim_percent": "0"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        r = self.client.patch(f"/api/warehouse/materials/{self.m.id}/", {"kim_percent": "75"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
