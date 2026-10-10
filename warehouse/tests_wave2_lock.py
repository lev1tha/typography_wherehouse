"""Волна 2, п. 1 (F4/PNL-01): замок периода в отходе, списании и удалении материала.

Сентябрь сверен и закрыт. Брак 15.09, внесённый в октябре, менял прибыль
принятого месяца; удаление материала без продаж уносило его приходы и потери
из журнала. Теперь — 400 по дате операции (отход, списание), а материал с
движениями в закрытом периоде только прячется.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import PeriodLock
from finance.reports.pnl import pnl
from warehouse.models import InventoryLog, Material, Roll
from warehouse.rolls import receive_lot


def noon(day):
    return timezone.make_aware(datetime.combine(day, time(12, 0)))


class PeriodLockStockTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="lk_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="lk_store", password="x", role=User.Role.STOREKEEPER)
        today = timezone.localdate()
        self.closed = today.replace(day=1) - timedelta(days=1)       # конец прошлого месяца
        self.prev_first = self.closed.replace(day=1)
        self.mid_prev = self.prev_first + timedelta(days=14)          # «15.09»
        self.sheet = Material.objects.create(
            name="Акрил 3мм", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"), price_per_sqm=Decimal("1500"),
        )
        self.lot = receive_lot(self.sheet, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                               sheet_count=Decimal("10"), purchase_cost=Decimal("32000"),
                               received_at=noon(self.prev_first))
        self.sheet.refresh_from_db()

    def close(self):
        lock = PeriodLock.load()
        lock.closed_through = self.closed
        lock.save()

    def test_waste_into_closed_month_is_400_and_profit_unchanged(self):
        self.close()
        before = pnl(self.prev_first, self.closed)["net_profit"]
        self.client.force_authenticate(self.store)
        r = self.client.post("/api/warehouse/waste/", {
            "happened_on": self.mid_prev.isoformat(), "note": "брак",
            "lines": [{"material": self.sheet.id, "form": "SHEET", "width": "1.22", "height": "2.44", "sheet_count": "2"}],
        }, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("период закрыт", str(r.data))
        self.assertEqual(pnl(self.prev_first, self.closed)["net_profit"], before)
        self.assertFalse(InventoryLog.objects.filter(type=InventoryLog.Type.WRITE_OFF).exists())
        # Сегодняшним днём — можно.
        r = self.client.post("/api/warehouse/waste/", {
            "lines": [{"material": self.sheet.id, "form": "SHEET", "width": "1.22", "height": "2.44", "sheet_count": "2"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def test_waste_more_than_a_year_back_is_400(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/warehouse/waste/", {
            "happened_on": (timezone.localdate() - timedelta(days=400)).isoformat(),
            "lines": [{"material": self.sheet.id, "form": "AREA", "area": "1"}],
        }, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_write_off_with_date(self):
        self.close()
        self.client.force_authenticate(self.admin)
        body = {"material": self.sheet.id, "quantity": "1", "reason_code": "DEFECT"}
        r = self.client.post("/api/warehouse/materials/write-off/", dict(body, happened_on=self.mid_prev.isoformat()), format="json")
        self.assertEqual(r.status_code, 400, r.data)
        # Открываем период — списание ложится датой операции.
        PeriodLock.objects.update(closed_through=None)
        r = self.client.post("/api/warehouse/materials/write-off/", dict(body, happened_on=self.mid_prev.isoformat()), format="json")
        self.assertEqual(r.status_code, 200, r.data)
        log = InventoryLog.objects.get(type=InventoryLog.Type.WRITE_OFF)
        self.assertEqual(timezone.localtime(log.happened_at).date(), self.mid_prev)

    def test_delete_material_with_closed_history_only_hides(self):
        self.close()
        before = pnl(self.prev_first, self.closed)
        self.client.force_authenticate(self.admin)
        r = self.client.delete(f"/api/warehouse/materials/{self.sheet.id}/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data["archived"])
        self.assertTrue(r.data["period_closed"])
        self.assertTrue(Material.objects.filter(pk=self.sheet.id, is_archived=True).exists())
        self.assertTrue(InventoryLog.objects.filter(material_id=self.sheet.id).exists())
        self.assertEqual(pnl(self.prev_first, self.closed)["net_profit"], before["net_profit"])

    def test_delete_material_with_open_history_still_deletes(self):
        fresh = Material.objects.create(name="Дубль", unit=Material.Unit.SQM, is_roll_material=True,
                                        sheet_width=Decimal("1"), sheet_height=Decimal("1"))
        receive_lot(fresh, form=Roll.Form.SHEET, width=Decimal("1"), height=Decimal("1"),
                    sheet_count=Decimal("1"), purchase_cost=Decimal("100"))
        self.close()
        self.client.force_authenticate(self.admin)
        r = self.client.delete(f"/api/warehouse/materials/{fresh.id}/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data.get("deleted"))
        self.assertFalse(Material.objects.filter(pk=fresh.id).exists())
