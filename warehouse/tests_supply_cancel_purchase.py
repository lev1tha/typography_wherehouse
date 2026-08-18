"""Отмена накладной убирает её из закупа и из складского листа (аудит 2026-08-18, п. 6).

Раньше при отмене логи поступления отвязывались от документа (`supply=None`)
и оставались в журнале рядом со встречной корректировкой. Но закуп месяца
считает «одиночные приходы» как раз по логам без накладной с ценой — и сумма
отменённой накладной (35 000 сом) оставалась в «Закуп материала» навсегда,
занижая прибыль; складской лист показывал её в «поступлении».

Теперь отмена — как удаление ошибочного чека: движений по накладной не
остаётся, след с составом — в журнале действий.
"""
from datetime import date
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from finance.material_sheet import purchases_from_stock
from warehouse.models import InventoryLog, Material, Roll, Supplier


class SupplyCancelPurchaseTests(APITestCase):
    URL = "/api/warehouse/supplies/"

    def setUp(self):
        self.admin = User.objects.create_user(username="sc_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.sheet = Material.objects.create(
            name="Белый акрил", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
        )
        self.bolts = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("500"),
            purchase_price=Decimal("10"),
        )
        self.supplier = Supplier.objects.create(name="Глобал")
        self.day = date(2026, 8, 18)

    def _create(self):
        r = self.client.post(self.URL, {
            "number": "T-1", "supplier": self.supplier.id, "received_on": self.day.isoformat(),
            "stated_total": None, "paid_amount": 0, "note": "",
            "lines": [
                {"material": self.sheet.id, "form": "SHEET", "width": "1.22", "height": "2.44",
                 "sheet_count": "10", "quantity": 0, "cost": "35000", "code": ""},
                {"material": self.bolts.id, "form": "QTY", "quantity": "100", "cost": "1200", "code": ""},
            ],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data["id"]

    def _purchase(self):
        return purchases_from_stock(self.day, self.day)

    def _received_in_sheet(self):
        d = self.client.get("/api/finance/material-report/", {"year": 2026, "month": 8}).data
        return {row["name"]: Decimal(str(row["received"])) for row in d["rows"]}

    def test_cancelling_removes_the_purchase_from_the_report(self):
        purchase0 = self._purchase()
        sid = self._create()
        self.assertEqual(self._purchase() - purchase0, Decimal("36200"))

        r = self.client.delete(f"{self.URL}{sid}/")
        self.assertEqual(r.status_code, 204)
        # Закуп дня — как до накладной, а не 36 200 навсегда.
        self.assertEqual(self._purchase(), purchase0)
        # Складской лист «поступление» тоже пуст.
        received = self._received_in_sheet()
        self.assertEqual(received.get("Белый акрил", Decimal("0")), Decimal("0"))
        self.assertEqual(received.get("Крепёж", Decimal("0")), Decimal("0"))

    def test_cancelling_leaves_no_stock_movements_but_a_trace_in_the_action_log(self):
        sid = self._create()
        self.assertEqual(InventoryLog.objects.filter(material=self.sheet).count(), 1)
        self.client.delete(f"{self.URL}{sid}/")
        # Ни поступления, ни встречной корректировки: поставки не было.
        self.assertFalse(InventoryLog.objects.filter(material__in=[self.sheet, self.bolts]).exists())
        self.assertFalse(Roll.objects.filter(material=self.sheet).exists())
        self.sheet.refresh_from_db()
        self.bolts.refresh_from_db()
        self.assertEqual(self.sheet.quantity, Decimal("0"))
        self.assertEqual(self.bolts.quantity, Decimal("500"))
        entry = AuditLog.objects.order_by("-id").first()
        self.assertIn("Отменена приходная накладная №T-1 от Глобал", entry.action)
        self.assertIn("Белый акрил", entry.action)
        self.assertIn("35000", entry.action)

    def test_quick_intake_stays_in_the_purchase_when_an_invoice_is_cancelled(self):
        """Одиночный приход (кнопка «Поступление») закуп не теряет — трогаем
        только логи самой отменённой накладной."""
        purchase0 = self._purchase()
        r = self.client.post("/api/warehouse/materials/supply/", {
            "material": self.bolts.id, "quantity": "10", "actual_price": "12",
            "happened_on": self.day.isoformat(),
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        sid = self._create()
        self.client.delete(f"{self.URL}{sid}/")
        self.assertEqual(self._purchase() - purchase0, Decimal("120"))
