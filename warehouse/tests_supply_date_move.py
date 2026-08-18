"""Дата проведённой накладной переносится вместе со следом на складе и держится
замком периода (аудит 2026-08-18, п. 13).

Раньше `PATCH /supplies/<id>/ {received_on}` двигал только дату документа:
закуп месяца (`SupplyLine` по `received_on`) уезжал в новый месяц, а партии
(`Roll.received_at`, по ним FIFO) и движения склада (`InventoryLog.happened_at`,
по ним складской лист) оставались в старом — два отчёта расходились на сумму
накладной, и всё это без проверки закрытого периода.
"""
from datetime import date
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from finance.material_sheet import purchases_from_stock
from finance.models import PeriodLock
from warehouse.models import InventoryLog, Material, Roll


class SupplyDateMoveTests(APITestCase):
    URL = "/api/warehouse/supplies/"

    def setUp(self):
        self.admin = User.objects.create_user(username="sdm_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.sheet = Material.objects.create(
            name="Форекс", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
        )
        r = self.client.post(self.URL, {
            "number": "D-1", "received_on": "2026-08-18", "stated_total": None, "paid_amount": 0, "note": "",
            "lines": [{"material": self.sheet.id, "form": "SHEET", "width": "1.22", "height": "2.44",
                       "sheet_count": "5", "quantity": 0, "cost": "9000", "code": ""}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.sid = r.data["id"]

    def _dates(self):
        roll = Roll.objects.get(material=self.sheet)
        log = InventoryLog.objects.get(material=self.sheet, type=InventoryLog.Type.SUPPLY)
        return (
            timezone.localtime(roll.received_at).date(),
            timezone.localtime(log.happened_at).date(),
        )

    def test_moving_the_date_moves_lots_and_journal_and_purchase(self):
        aug = (date(2026, 8, 1), date(2026, 8, 31))
        jul = (date(2026, 7, 1), date(2026, 7, 31))
        self.assertEqual(purchases_from_stock(*aug), Decimal("9000"))
        r = self.client.patch(f"{self.URL}{self.sid}/", {"received_on": "2026-07-25"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["received_on"], "2026-07-25")
        # Закуп, партия и журнал — все в июле.
        self.assertEqual(purchases_from_stock(*aug), Decimal("0"))
        self.assertEqual(purchases_from_stock(*jul), Decimal("9000"))
        self.assertEqual(self._dates(), (date(2026, 7, 25), date(2026, 7, 25)))

    def test_lot_moved_back_goes_first_in_fifo(self):
        # Вторая партия «раньше» по вводу, но накладная датирована позже.
        r = self.client.post(self.URL, {
            "number": "D-2", "received_on": "2026-08-10", "stated_total": None, "paid_amount": 0, "note": "",
            "lines": [{"material": self.sheet.id, "form": "SHEET", "width": "1.22", "height": "2.44",
                       "sheet_count": "5", "quantity": 0, "cost": "15000", "code": ""}],
        }, format="json")
        second = r.data["id"]
        # Переносим ПЕРВУЮ (9 000, 18.08) на 25.07 — теперь она старше второй.
        self.client.patch(f"{self.URL}{self.sid}/", {"received_on": "2026-07-25"}, format="json")
        first_roll = Roll.objects.filter(material=self.sheet, purchase_cost=Decimal("9000")).get()
        second_roll = Roll.objects.filter(material=self.sheet, purchase_cost=Decimal("15000")).get()
        self.assertLess(first_roll.received_at, second_roll.received_at)
        self.assertEqual(list(Roll.objects.filter(material=self.sheet).order_by("received_at"))[0].pk, first_roll.pk)
        self.assertTrue(second)

    def test_period_lock_holds_both_dates(self):
        lock = PeriodLock.load()
        lock.closed_through = date(2026, 7, 31)
        lock.save()
        # В закрытый месяц — нельзя.
        r = self.client.patch(f"{self.URL}{self.sid}/", {"received_on": "2026-07-25"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("закрыт", str(r.data))
        self.assertEqual(self._dates(), (date(2026, 8, 18), date(2026, 8, 18)))
        # Правка номера без даты проходит.
        r = self.client.patch(f"{self.URL}{self.sid}/", {"number": "D-1a"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        # Из закрытого месяца — тоже нельзя.
        lock.closed_through = date(2026, 8, 31)
        lock.save()
        r = self.client.patch(f"{self.URL}{self.sid}/", {"received_on": "2026-09-01"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_same_date_is_a_no_op(self):
        r = self.client.patch(f"{self.URL}{self.sid}/", {"received_on": "2026-08-18", "note": "ok"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self._dates(), (date(2026, 8, 18), date(2026, 8, 18)))
