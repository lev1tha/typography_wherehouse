"""Волна 2, п. 13 (STK-04): снимок остатков партий на конец месяца."""
from datetime import datetime, time, timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import PeriodLock
from warehouse.models import InventoryLog, Material, Roll, StockSnapshot, stock_value_total
from warehouse.rolls import consume_area, receive_lot


def noon(day):
    return timezone.make_aware(datetime.combine(day, time(12, 0)))


class SnapshotTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="sn_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        today = timezone.localdate()
        self.prev_end = today.replace(day=1) - timedelta(days=1)
        self.prev_first = self.prev_end.replace(day=1)
        self.m = Material.objects.create(name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
                                         sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"))
        receive_lot(self.m, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=Decimal("10"), purchase_cost=Decimal("32000"), received_at=noon(self.prev_first))
        consume_area(self.m, Decimal("5.9536"), log_type=InventoryLog.Type.SALE,
                     happened_at=noon(self.prev_first + timedelta(days=5)))
        # Октябрь: ещё 1 лист продан — снимок на конец сентября его не видит.
        consume_area(self.m, Decimal("2.9768"), log_type=InventoryLog.Type.SALE)

    def close(self, day):
        lock = PeriodLock.load()
        lock.closed_through = day
        lock.save()

    def test_closing_takes_snapshot_and_value_is_frozen(self):
        self.close(self.prev_end)
        snap = StockSnapshot.objects.get(as_of=self.prev_end)
        self.assertEqual(snap.source, "close")
        self.assertEqual(snap.value, Decimal("25600.00"))                 # 8 листов × 3 200
        self.assertEqual(snap.lines.get().quantity, Decimal("23.8144"))
        # Поставка «задним числом» мимо замка (как старые данные) не двигает снимок.
        receive_lot(self.m, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=Decimal("5"), purchase_cost=Decimal("20000"), received_at=noon(self.prev_end))
        self.assertEqual(stock_value_total(self.prev_end), Decimal("25600.00"))
        r = self.client.get("/api/warehouse/materials/on-date/", {"date": self.prev_end.isoformat()})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["source"], "snapshot")
        self.assertEqual(r.data["rows"][0]["units"], Decimal("8.00"))
        # Открыли период — снимок закрытия больше не заперт.
        self.close(None)
        self.assertFalse(StockSnapshot.objects.exists())

    def test_command_and_calc_on_date(self):
        r = self.client.get("/api/warehouse/materials/on-date/", {"date": self.prev_end.isoformat()})
        self.assertEqual(r.data["source"], "calc")
        self.assertEqual(r.data["value"], Decimal("25600.00"))
        out = StringIO()
        call_command("stock_snapshot", "--month", self.prev_end.strftime("%Y-%m"), stdout=out)
        self.assertIn("25600.00", out.getvalue())
        self.assertEqual(StockSnapshot.objects.get(as_of=self.prev_end).source, "command")
        csv = self.client.get("/api/warehouse/materials/on-date/", {"date": self.prev_end.isoformat(), "export": "csv"})
        self.assertIn("Акрил;23,8144;8,00;лист.;25600,00", csv.content.decode("utf-8-sig"))
