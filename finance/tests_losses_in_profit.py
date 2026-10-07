"""Брак и недостача вычитаются из прибыли (пятая редакция формулы, 2026-09-27).

Аудит 26.09, п. 1: потери стояли «справочно». За сентябрь со склада мимо
продажи ушло 113 327 при прибыли 196 650 — и прибыль об этом не знала. Теперь
прибыль = выручка − себестоимость − расходы − списанное, одинаково в плитке и
в графике по дням. «Прибыль до расходов» (выручка − себестоимость) не
меняется: это другой вопрос, и «Обзор» с «Финансами» в нём сходятся.
"""
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from warehouse.models import InventoryLog, Material

REPORT = "/api/finance/report/"
DAILY = "/api/finance/daily/"


class LossesInProfitTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="lp_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.iron = Material.objects.create(
            name="Утюг", unit=Material.Unit.PIECE,
            quantity=Decimal("0"), purchase_price=Decimal("0"),
        )
        r = self.client.post("/api/warehouse/materials/supply/", {
            "material": self.iron.id, "quantity": "3", "actual_price": "800",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        today = timezone.localdate()
        self.month = {"date_from": today.replace(day=1).isoformat(), "date_to": today.isoformat()}

    def _write_off(self, qty="1"):
        r = self.client.post("/api/warehouse/materials/write-off/", {
            "material": self.iron.id, "quantity": qty, "reason_code": "DEFECT",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_defect_lowers_profit_by_its_cost(self):
        before = self.client.get(REPORT, self.month).data
        self._write_off()
        after = self.client.get(REPORT, self.month).data
        self.assertEqual(Decimal(str(after["losses"]["cost"])), Decimal("800.00"))
        self.assertEqual(
            Decimal(str(after["profit"])), Decimal(str(before["profit"])) - Decimal("800")
        )
        # С 2026-10-07 потери — в себестоимости, до валовой прибыли (D-15):
        # валовая прибыль падает на те же 800.
        self.assertEqual(
            Decimal(str(after["gross_margin"])), Decimal(str(before["gross_margin"])) - Decimal("800")
        )

    def test_stock_chain_explains_the_loss_instead_of_the_gap(self):
        before = self.client.get(REPORT, self.month).data["stock"]["reconcile"]
        self._write_off()
        after = self.client.get(REPORT, self.month).data["stock"]["reconcile"]
        self.assertEqual(Decimal(str(after["losses"])), Decimal("800.00"))
        self.assertEqual(after["gap"], before["gap"])

    def test_daily_chart_takes_the_loss_on_its_day(self):
        self._write_off()
        today = timezone.localdate()
        data = self.client.get(DAILY, {"year": today.year, "month": today.month}).data
        row = next(r for r in data["rows"] if r["day"] == today.day)
        self.assertEqual(Decimal(str(row["losses"])), Decimal("800.00"))
        self.assertEqual(Decimal(str(data["totals"]["losses"])), Decimal("800.00"))
        # Итог под графиком и плитка — одна цифра.
        month = self.client.get(REPORT, {
            "date_from": today.replace(day=1).isoformat(),
            "date_to": data["rows"][-1]["date"],
        }).data
        self.assertEqual(Decimal(str(data["totals"]["profit"])), Decimal(str(month["profit"])))

    def test_old_losses_without_cost_are_counted_not_guessed(self):
        InventoryLog.objects.create(
            type=InventoryLog.Type.ADJUSTMENT, material=self.iron,
            quantity_changed=Decimal("-1"), cost=None,
        )
        rep = self.client.get(REPORT, self.month).data
        self.assertEqual(rep["losses"]["unknown"], 1)
        self.assertEqual(Decimal(str(rep["losses"]["cost"])), Decimal("0"))
