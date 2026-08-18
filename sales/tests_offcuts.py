"""Обрезок: сколько списали, но клиенту не отдали.

Полосу 0.5 м от рулона шириной 0.9 отрезают на всю ширину — иначе никак.
Клиент забирает 0.5, а 0.4 остаётся в цехе и обычно идёт в мусор. Деньги берут
за полную ширину, и это правильно: материал потрачен весь. Но цифра «сколько я
подарил» до сих пор не считалась нигде — обрезок растворялся в списании.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt
from warehouse.models import Material, Roll


class OffcutTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="oc_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.mat = Material.objects.create(
            name="Оракал", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL,
            roll_width=Decimal("0.9"), price_per_pm=Decimal("300"),
        )
        # 20 м за 1800 → 100 сом/кв.м (0.9 × 20 = 18 кв.м).
        r = self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": self.mat.id, "form": "ROLL", "width": "0.9",
            "length": "20", "purchase_cost": "1800",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.roll = Roll.objects.get(material=self.mat)

    def _sell(self, metres, used_width=None):
        item = {"type": "MATERIAL", "material": self.mat.id,
                "mode": "METER", "quantity": str(metres)}
        if used_width is not None:
            item["used_width"] = str(used_width)
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True, "items": [item],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return Receipt.objects.get(pk=r.data["id"])

    # --- сам обрезок ---
    def test_narrow_strip_leaves_a_measured_offcut(self):
        """Ровно случай владельца: нужна полоса 0.5 × 2 от рулона 0.9."""
        receipt = self._sell("2", used_width="0.5")
        item = receipt.items.get()
        # (0.9 − 0.5) × 2 = 0.8 кв.м ушло в мусор.
        self.assertEqual(item.offcut_area, Decimal("0.8000"))
        # По цене той партии, из которой резали: 0.8 × 100.
        self.assertEqual(item.offcut_cost, Decimal("80.00"))

    def test_full_width_leaves_nothing(self):
        item = self._sell("2", used_width="0.9").items.get()
        self.assertEqual(item.offcut_area, Decimal("0"))
        self.assertEqual(item.offcut_cost, Decimal("0"))

    def test_without_the_width_we_do_not_invent_an_offcut(self):
        """Не назвали ширину изделия — считаем, что ушло всё. Выдумывать нельзя."""
        item = self._sell("2").items.get()
        self.assertIsNone(item.used_width)
        self.assertEqual(item.offcut_area, Decimal("0"))

    # --- деньги и склад не поехали ---
    def test_the_client_still_pays_for_the_full_width(self):
        """Обрезок не скидка: материал потрачен весь, и цена та же."""
        with_width = self._sell("2", used_width="0.5")
        without = self._sell("2")
        self.assertEqual(with_width.total_price, without.total_price)

    def test_stock_goes_down_by_the_full_width_either_way(self):
        before = Material.objects.get(pk=self.mat.pk).quantity
        self._sell("2", used_width="0.5")
        self.mat.refresh_from_db()
        self.assertEqual(before - self.mat.quantity, Decimal("1.8000"))

    def test_offcut_cost_is_part_of_the_line_cost_not_extra(self):
        """Обрезок сидит ВНУТРИ себестоимости строки, а не сверх неё."""
        item = self._sell("2", used_width="0.5").items.get()
        self.assertEqual(item.cost_total, Decimal("180.00"))   # 1.8 кв.м × 100
        self.assertLess(item.offcut_cost, item.cost_total)

    # --- возврат ---
    def test_returned_line_has_no_offcut(self):
        """Материал вернулся на склад — отхода не было."""
        receipt = self._sell("2", used_width="0.5")
        self.client.post(f"/api/sales/receipts/{receipt.id}/refund/", {}, format="json")
        item = receipt.items.get()
        item.refresh_from_db()
        self.assertEqual(item.offcut_area, Decimal("0"))

    # --- отчёт ---
    def test_report_sums_the_offcuts(self):
        self._sell("2", used_width="0.5")     # 0.8 кв.м / 80 сом
        self._sell("3", used_width="0.6")     # 0.9 кв.м / 90 сом
        self._sell("1")                       # без ширины — не считается
        data = self.client.get("/api/finance/report/").data["offcuts"]
        self.assertEqual(Decimal(str(data["area"])), Decimal("1.70"))
        self.assertEqual(Decimal(str(data["cost"])), Decimal("170.00"))

    def test_report_is_empty_when_nobody_named_a_width(self):
        self._sell("2")
        data = self.client.get("/api/finance/report/").data["offcuts"]
        self.assertEqual(Decimal(str(data["area"])), Decimal("0"))

    def test_refunded_order_drops_out_of_the_report(self):
        receipt = self._sell("2", used_width="0.5")
        self.client.post(f"/api/sales/receipts/{receipt.id}/refund/", {}, format="json")
        data = self.client.get("/api/finance/report/").data["offcuts"]
        self.assertEqual(Decimal(str(data["area"])), Decimal("0"))

    def test_offcut_is_priced_by_the_lot_it_came_from(self):
        """Второй рулон дороже — обрезок из него стоит дороже."""
        self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": self.mat.id, "form": "ROLL", "width": "0.9",
            "length": "10", "purchase_cost": "3600",   # 400 сом/кв.м
        }, format="json")
        pricey = Roll.objects.filter(material=self.mat).order_by("-id").first()
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": self.mat.id, "mode": "METER",
                       "quantity": "2", "used_width": "0.5", "roll": pricey.id}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        item = Receipt.objects.get(pk=r.data["id"]).items.get()
        self.assertEqual(item.offcut_cost, Decimal("320.00"))   # 0.8 × 400
