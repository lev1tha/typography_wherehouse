"""Лента «Все траты за период» показывает и приходы материала, не только записи.

Закуп материала система считает сама, по приходам на склад, и записью траты
(`ExpenseEntry`) он не становится никогда. Пока список читал только записи, у
заказчика он был пуст ВСЕГДА: все его траты — приходы материала, а в отчёте
сверху при этом стояло «Расходы 25 000».
"""
from datetime import date
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import ExpenseEntry, ExpenseKind
from warehouse.models import Material, Supplier


class ExpenseFeedTests(APITestCase):
    FEED = "/api/finance/expense-entries/feed/"
    SUPPLIES = "/api/warehouse/supplies/"
    REPORT = "/api/finance/report/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="feed_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.supplier = Supplier.objects.create(name="Глобал")
        self.sheet = Material.objects.create(
            name="Акрил 3мм", unit=Material.Unit.SQM, is_roll_material=True,
            piece_area=Decimal("2.9768"), price_per_sqm=Decimal("1500"),
        )
        self.piece = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, price_per_unit=Decimal("10"),
        )
        today = timezone.localdate()
        self.today = today
        self.day = date(today.year, today.month, 1) if today.day > 1 else today

    def _feed(self):
        r = self.client.get(self.FEED, {
            "year": self.today.year, "month": self.today.month,
            "date_from": date(self.today.year, self.today.month, 1).isoformat(),
        })
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def _supply(self, cost="18000"):
        return self.client.post(self.SUPPLIES, {
            "number": "НК-001", "supplier": self.supplier.id,
            "received_on": self.day.isoformat(),
            "stated_total": cost, "paid_amount": "0",
            "lines": [{
                "material": self.sheet.id, "form": "SHEET",
                "width": "1.22", "height": "2.44", "sheet_count": "5", "cost": cost,
            }],
        }, format="json")

    def test_supply_document_shows_up_as_a_row(self):
        self.assertEqual(self._supply().status_code, 201)
        data = self._feed()
        rows = data["results"]
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["source"], "SUPPLY")
        self.assertEqual(row["name"], "Накладная НК-001")
        self.assertEqual(Decimal(str(row["amount"])), Decimal("18000"))
        self.assertEqual(str(row["spent_at"]), self.day.isoformat())
        # Итог ленты — ВСЕ деньги, ушедшие за период, включая закуп. С
        # «Расходами» отчёта он теперь расходится сознательно: закуп — оборот
        # (в «Расходах» его нет), а лента отвечает на «куда ушли деньги».
        self.assertEqual(Decimal(str(data["total"])), Decimal("18000"))
        report = self.client.get(
            self.REPORT, {"year": self.today.year, "month": self.today.month}
        ).data
        self.assertEqual(Decimal(str(report["total_expenses"])), Decimal("0"))

    def test_single_intake_shows_up_too(self):
        """Приход кнопкой на строке материала — мимо накладной, но это трата."""
        r = self.client.post("/api/warehouse/materials/supply/", {
            "material": self.piece.id, "quantity": "100", "actual_price": "12",
            "happened_on": self.day.isoformat(),
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        rows = self._feed()["results"]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["source"], "SUPPLY")
        self.assertEqual(rows[0]["name"], "Крепёж")
        self.assertEqual(Decimal(str(rows[0]["amount"])), Decimal("1200"))

    def test_manual_entry_and_supply_live_in_one_list(self):
        self.assertEqual(self._supply().status_code, 201)
        rent = ExpenseKind.objects.get(code="RENT")
        ExpenseEntry.objects.create(
            kind=rent, name="Аренда цеха", amount=Decimal("30000"), spent_at=self.day
        )
        data = self._feed()
        sources = sorted(r["source"] for r in data["results"])
        self.assertEqual(sources, ["MANUAL", "SUPPLY"])
        self.assertEqual(Decimal(str(data["total"])), Decimal("48000"))
        # Ручная запись остаётся правимой: у неё есть свой id записи траты.
        manual = next(r for r in data["results"] if r["source"] == "MANUAL")
        self.assertTrue(
            ExpenseEntry.objects.filter(id=manual["id"]).exists(), manual
        )

    def test_rows_outside_the_period_are_not_listed(self):
        self.assertEqual(self._supply().status_code, 201)
        r = self.client.get(self.FEED, {
            "date_from": "2000-01-01", "date_to": "2000-01-31",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["results"], [])
        self.assertEqual(Decimal(str(r.data["total"])), Decimal("0"))
