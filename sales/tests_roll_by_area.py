"""Рулон по площади ИЗДЕЛИЯ — вторая цена рулона (CALC-10, D-140).

Баннер и самоклейку в Excel владелец считает по площади изделия: баннер 1×2 м
по 220 сом/кв.м = 440. Режут при этом поперёк рулона на всю ширину — из рулона
1.6 м уходит 1.6 × 2 = 3.2 кв.м, лишние 0.6 × 2 — обрезок цеха. Раньше рулон
продавался только погонными метрами на всю ширину (2 м × 300 = 600).

Цена за кв.м у рулона пуста — всё как раньше: только метры.
"""
from decimal import Decimal as D

from django.db.models import Sum
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from finance.reports.summary import finance_summary
from sales.models import Receipt, TransactionItem
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot

CHECKOUT = "/api/sales/receipts/checkout/"


class RollByAreaTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="ra_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="ra_store", password="x", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(self.admin)
        # Баннер литой: рулон 1.6 м, 300 сом/пог.м и 220 сом/кв.м изделия.
        self.banner = Material.objects.create(
            name="Баннер литой", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL, roll_width=D("1.6"),
            price_per_pm=D("300"), price_per_sqm=D("220"),
        )
        # 1.6 × 50 = 80 кв.м за 12 000 — 150 сом/кв.м.
        self.lot = receive_lot(
            self.banner, form=Roll.Form.ROLL, width=D("1.6"), length=D("50"), purchase_cost=D("12000"),
        )

    def sell(self, item, user=None, expect=201):
        if user is not None:
            self.client.force_authenticate(user)
        body = {"payment_method": "CASH", "pay_full": True,
                "items": [{"type": "MATERIAL", "material": self.banner.id, **item}]}
        r = self.client.post(CHECKOUT, body, format="json")
        self.assertEqual(r.status_code, expect, getattr(r, "data", r))
        return r

    def area(self, w="1.0", l="2.0", **extra):
        return {"mode": "SQM", "width": w, "length": l, **extra}

    def left(self):
        self.banner.refresh_from_db()
        return self.banner.quantity

    # --- сценарий владельца ---------------------------------------------------
    def test_banner_1x2_at_220_is_440_and_takes_full_width(self):
        """Баннер 1×2 м по 220 = 440, со склада — 1.6 × 2 = 3.2 кв.м."""
        before = self.left()
        r = self.sell(self.area())
        self.assertEqual(D(str(r.data["total_price"])), D("440"))
        self.assertEqual(before - self.left(), D("3.2000"))
        item = Receipt.objects.get(pk=r.data["id"]).items.get()
        self.assertTrue(item.roll_area)
        self.assertEqual(item.sale_mode, TransactionItem.SaleMode.SQM)
        self.assertEqual((item.quantity, item.price_per_item), (D("2.000"), D("220")))
        self.assertEqual((item.width, item.length), (D("1.000"), D("2.000")))
        self.assertEqual(item.roll_id, self.lot.id)
        # Себестоимость — вся отрезанная ширина: 3.2 × 150 = 480.
        self.assertEqual(item.cost_total, D("480.00"))
        # Обрезок — как у метров: (1.6 − 1.0) × 2 = 1.2 кв.м на 180 сом.
        self.assertEqual(item.offcut_area, D("1.2000"))
        self.assertEqual(item.offcut_cost, D("180.00"))
        line = r.data["items"][0]
        self.assertEqual((line["unit_code"], line["roll_area"]), ("SQM", True))
        # Партия и карточка в ногу; рулон — в метрах.
        lots = Roll.objects.filter(material=self.banner).aggregate(v=Sum("remaining_area"))["v"]
        self.assertEqual(self.left(), lots)
        self.assertEqual(self.banner.metres_remaining, D("48.00"))

    def test_metres_still_work_on_the_same_roll(self):
        r = self.sell({"mode": "METER", "quantity": "2"})
        self.assertEqual(D(str(r.data["total_price"])), D("600"))

    def test_storekeeper_sells_by_area_at_catalogue_price(self):
        r = self.sell(self.area("0.8", "1.5"), user=self.store)
        self.assertEqual(D(str(r.data["total_price"])), D("264"))      # 1.2 × 220

    def test_full_width_product_has_no_offcut(self):
        r = self.sell(self.area("1.6", "2"))
        self.assertEqual(D(str(r.data["total_price"])), D("704"))      # 3.2 × 220
        item = TransactionItem.objects.get(receipt_id=r.data["id"])
        self.assertEqual(item.offcut_area, D("0"))

    def test_admin_price_override(self):
        r = self.sell(self.area(material_price="200"))
        self.assertEqual(D(str(r.data["total_price"])), D("400"))

    # --- отказы -----------------------------------------------------------------
    def test_no_sqm_price_keeps_old_behaviour(self):
        self.banner.price_per_sqm = D("0")
        self.banner.save()
        self.assertFalse(self.banner.sells_roll_by_area)
        r = self.sell(self.area(), expect=400)
        self.assertNotIn("roll_area", str(r.data))
        r = self.sell({"mode": "SQM", "quantity": "2"}, expect=400)
        self.assertIn("погонными метрами", str(r.data))
        self.assertEqual(Receipt.objects.count(), 0)

    def test_size_is_required(self):
        r = self.sell({"mode": "SQM", "quantity": "2"}, expect=400)
        self.assertIn("ширину и длину", str(r.data))
        self.sell({"mode": "SQM", "width": "1"}, expect=400)
        self.assertEqual(Receipt.objects.count(), 0)

    def test_quantity_and_used_width_are_rejected(self):
        self.sell(self.area(quantity="2"), expect=400)
        self.sell(self.area(used_width="1.0"), expect=400)

    def test_wider_than_roll_is_rejected(self):
        r = self.sell(self.area("1.7", "2"), expect=400)
        self.assertIn("шире рулона", str(r.data))
        self.assertEqual(self.left(), D("80.0000"))

    def test_narrower_chosen_roll_is_rejected(self):
        narrow = receive_lot(
            self.banner, form=Roll.Form.ROLL, width=D("1.0"), length=D("10"), purchase_cost=D("1500"),
        )
        r = self.sell(self.area("1.2", "1", roll=narrow.id), expect=400)
        self.assertIn("шире рулона", str(r.data))

    def test_preview_and_add_items_price_the_same(self):
        body = {"payment_method": "CASH",
                "items": [{"type": "MATERIAL", "material": self.banner.id, **self.area()}]}
        p = self.client.post("/api/sales/receipts/preview/", body, format="json")
        self.assertEqual(p.status_code, 200, p.data)
        self.assertEqual(D(str(p.data["total_price"])), D("440"))
        self.assertEqual(self.left(), D("80.0000"))                       # предпросмотр склад не трогает
        r = self.sell({"mode": "METER", "quantity": "1"})
        add = self.client.post(f"/api/sales/receipts/{r.data['id']}/add-items/", {
            "items": [{"type": "MATERIAL", "material": self.banner.id, **self.area("0.5", "1")}],
        }, format="json")
        self.assertEqual(add.status_code, 200, add.data)
        self.assertEqual(D(str(add.data["total_price"])), D("410"))     # 300 + 0.5 × 220
        self.assertEqual(self.left(), D("76.8000"))                       # 1.6 × (1 + 1)

    def test_material_card_exposes_the_mode(self):
        r = self.client.get(f"/api/warehouse/materials/{self.banner.id}/")
        self.assertTrue(r.data["sells_roll_by_area"])
        self.assertTrue(r.data["sells_by_metre"])

    # --- возврат, правка, отчёты ----------------------------------------------------
    def test_refund_returns_full_width(self):
        before = self.left()
        r = self.sell(self.area())
        back = self.client.post(f"/api/sales/receipts/{r.data['id']}/refund/", {}, format="json")
        self.assertEqual(back.status_code, 200, back.data)
        self.assertEqual(self.left(), before)
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.remaining_area, self.lot.initial_area)

    def test_partial_refund_is_refused(self):
        r = self.sell(self.area())
        item = TransactionItem.objects.get(receipt_id=r.data["id"])
        back = self.client.post(
            f"/api/sales/receipts/{r.data['id']}/refund/",
            {"quantities": [{"id": item.id, "quantity": "1"}]}, format="json",
        )
        self.assertEqual(back.status_code, 400)
        self.assertEqual(self.left(), D("76.8000"))

    def test_edit_length_recounts_price_and_stock(self):
        r = self.sell(self.area())
        item = TransactionItem.objects.get(receipt_id=r.data["id"])
        e = self.client.post(f"/api/sales/receipts/{r.data['id']}/edit-items/",
                             {"items": [{"id": item.id, "length": "3"}]}, format="json")
        self.assertEqual(e.status_code, 200, e.data)
        self.assertEqual(D(str(e.data["total_price"])), D("660"))       # 1 × 3 × 220
        self.assertEqual(self.left(), D("75.2000"))                       # 80 − 1.6 × 3
        item.refresh_from_db()
        self.assertEqual((item.quantity, item.cost_total), (D("3.000"), D("720.00")))

    def test_edit_area_alone_is_refused(self):
        r = self.sell(self.area())
        item = TransactionItem.objects.get(receipt_id=r.data["id"])
        e = self.client.post(f"/api/sales/receipts/{r.data['id']}/edit-items/",
                             {"items": [{"id": item.id, "quantity": "3"}]}, format="json")
        self.assertEqual(e.status_code, 400)
        self.assertEqual(self.left(), D("76.8000"))

    def test_summary_counts_offcut_of_area_line(self):
        self.sell(self.area())
        today = timezone.localdate()
        s = finance_summary(today.replace(day=1), today)
        self.assertEqual(s["offcuts"]["area"], D("1.20"))
        self.assertEqual(s["offcuts"]["cost"], D("180.00"))

    def test_material_report_counts_metres_cut(self):
        self.sell(self.area())
        today = timezone.localdate()
        r = self.client.get("/api/finance/material-report/", {
            "date_from": today.replace(day=1).isoformat(), "date_to": today.isoformat(),
        })
        self.assertEqual(r.status_code, 200, r.data)
        rows = r.data["rows"] if isinstance(r.data, dict) else r.data
        row = next(x for x in rows if x["id"] == self.banner.id)
        # Складской лист рулона — в метрах: отрезано 2 м (не 2 кв.м ÷ 1.6).
        self.assertEqual(D(str(row["sold_qty"])), D("2.00"))
        self.assertEqual(D(str(row["sold_area"])), D("3.2"))
