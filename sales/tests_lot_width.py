"""Обрезок и площадь резки — по ширине ПАРТИИ, а не карточки (аудит 2026-08-18, п. 10).

Ширина заморожена в партии при приёмке; под одной карточкой законно лежат
рулоны разной ширины, и `Material.roll_width` — лишь значение по умолчанию.
Раньше обрезок (`TransactionItem.offcut_area`) и площадь резки в финотчёте
считались по карточке: рулон 1.5 при карточке 1.2 → обрезок 0.4 кв.м вместо
1.0, площадь 2.4 вместо 3.0. Заодно: строка METER без выбранного рулона теперь
помнит рулон, с которого FIFO и начал, а накладная подставляет ширину рулона
из карточки, если её не ввели.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt, TransactionItem
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot


class LotWidthTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="lw_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.mat = Material.objects.create(
            name="Оракал", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL, roll_width=Decimal("1.2"),
            price_per_pm=Decimal("1000"),
        )
        # Партия шире карточки — так и приняли (или так набрали руками).
        self.wide = receive_lot(
            self.mat, form=Roll.Form.ROLL, width=Decimal("1.5"), length=Decimal("10"),
            purchase_cost=Decimal("6000"),
        )
        self.mat.refresh_from_db()

    def _sell(self, metres, used_width, roll=None):
        item = {"type": "MATERIAL", "material": self.mat.id, "quantity": str(metres), "mode": "METER",
                "used_width": str(used_width)}
        if roll is not None:
            item["roll"] = roll.pk
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True, "items": [item],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return TransactionItem.objects.get(receipt_id=r.data["id"])

    def test_offcut_uses_the_lot_width(self):
        item = self._sell(2, 1, roll=self.wide)
        self.assertEqual(item.roll_width, Decimal("1.5"))
        self.assertEqual(item.offcut_area, Decimal("1.0000"))       # (1.5 − 1.0) × 2, не 0.4
        self.assertEqual(item.offcut_cost, Decimal("400.00"))       # 1.0 × 400/кв.м
        report = self.client.get("/api/finance/report/").data
        self.assertEqual(report["offcuts"]["area"], Decimal("1.00"))
        self.assertEqual(report["offcuts"]["cost"], Decimal("400.00"))

    def test_metre_line_without_a_chosen_roll_remembers_the_fifo_roll(self):
        item = self._sell(2, 1)   # рулон не выбран — дозаказ, повтор
        self.assertEqual(item.roll_id, self.wide.pk)
        self.assertEqual(item.offcut_area, Decimal("1.0000"))

    def test_cutting_area_in_the_report_uses_the_lot_width(self):
        from services.models import PrintingService

        cutting = PrintingService.objects.create(
            name="Резка", kind=PrintingService.Kind.CUTTING, rate_per_pm=Decimal("100"),
        )
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [
                {"type": "MATERIAL", "material": self.mat.id, "quantity": "2", "mode": "METER", "roll": self.wide.pk},
                {"type": "SERVICE", "service": cutting.id, "running_meters": "3"},
            ],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        report = self.client.get("/api/finance/report/").data
        # 2 м × ширина партии 1.5 = 3.0 кв.м, а не 2 × 1.2 = 2.4.
        self.assertEqual(Decimal(str(report["cutting"]["area"])), Decimal("3.00"))

    def test_invoice_roll_line_takes_the_width_from_the_card(self):
        r = self.client.post("/api/warehouse/supplies/", {
            "number": "W-1", "received_on": "2026-08-18", "stated_total": None, "paid_amount": 0, "note": "",
            "lines": [{"material": self.mat.id, "form": "ROLL", "length": "5", "quantity": 0, "cost": "3000", "code": ""}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        roll = Roll.objects.filter(material=self.mat).order_by("-id").first()
        self.assertEqual(roll.width, Decimal("1.20"))
        self.assertEqual(roll.metres_initial, Decimal("5.00"))
