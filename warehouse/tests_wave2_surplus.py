"""Волна 2, п. 4 и 5 (F5/PNL-04, G3-N3, STK-10): излишек гасит потери; пересчёт на дату.

Правило: излишек инвентаризации и промера рулона записывается в журнал со
стоимостью по партии, куда он лёг (у материала без партий — по последней
закупочной), и «Потери материала» в ОПиУ считаются нетто: недостача и брак
минус излишки периода по дате операции. «Насчитал 8 из 10, нашёл ещё 2» не
оставляет убытка, склад — ровно свой закуп.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import PeriodLock
from finance.reports.bridge import bridge
from finance.reports.pnl import pnl
from warehouse.models import InventoryLog, Material, Roll
from warehouse.rolls import receive_lot

ADJUST = "/api/warehouse/materials/adjust/"


def noon(day):
    return timezone.make_aware(datetime.combine(day, time(12, 0)))


class SurplusTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="sp_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.today = timezone.localdate()
        self.first = self.today.replace(day=1)
        self.acr = Material.objects.create(
            name="Акрил 3мм", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"), price_per_sqm=Decimal("1500"),
        )
        self.lot = receive_lot(self.acr, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                               sheet_count=Decimal("10"), purchase_cost=Decimal("32000"),
                               received_at=noon(self.first))

    def count(self, material, **kw):
        r = self.client.post(ADJUST, {"material": material.id, **kw}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        material.refresh_from_db()
        return r

    def month(self):
        return pnl(self.first, self.today)

    def test_mistake_and_fix_leave_no_loss(self):
        self.count(self.acr, counted_sheets="8")
        p = self.month()
        self.assertEqual(p["losses"], Decimal("6400.00"))
        self.count(self.acr, counted_sheets="10")
        p = self.month()
        self.assertEqual(p["losses"], Decimal("0.00"))
        self.assertEqual(p["net_profit"], Decimal("0.00"))
        self.acr.refresh_from_db()
        self.assertEqual(self.acr.quantity, Decimal("29.7680"))
        self.assertEqual(self.acr.stock_value, Decimal("32000.00"))
        up = InventoryLog.objects.get(type=InventoryLog.Type.ADJUSTMENT, quantity_changed__gt=0)
        self.assertEqual(up.cost, Decimal("6400.00"))
        self.assertEqual(bridge(self.first, self.today)["unexplained"], Decimal("0"))

    def test_surplus_goes_back_into_the_lot_it_left(self):
        """Старая партия давно пуста, недостача ушла из свежей — излишек туда же."""
        cheap = Material.objects.create(
            name="Форекс", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1"), sheet_height=Decimal("2"),
        )
        old = receive_lot(cheap, form=Roll.Form.SHEET, width=Decimal("1"), height=Decimal("2"),
                          sheet_count=Decimal("5"), purchase_cost=Decimal("1000"),
                          received_at=noon(self.first))
        new = receive_lot(cheap, form=Roll.Form.SHEET, width=Decimal("1"), height=Decimal("2"),
                          sheet_count=Decimal("5"), purchase_cost=Decimal("3000"))
        old.remaining_area = Decimal("0")
        old.save()
        cheap.refresh_from_db()
        cheap.quantity = Decimal("10")
        cheap.save()
        self.count(cheap, counted_sheets="3")          # −2 листа из свежей: 1 200
        self.count(cheap, counted_sheets="5")          # +2 листа — обратно в свежую
        new.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(new.remaining_area, Decimal("10"))
        self.assertEqual(old.remaining_area, Decimal("0"))
        self.assertEqual(self.month()["losses"], Decimal("0.00"))

    def test_piece_material_without_lots(self):
        bolts = Material.objects.create(name="Болт", unit=Material.Unit.PIECE,
                                        quantity=Decimal("100"), purchase_price=Decimal("12.50"))
        self.count(bolts, counted_quantity="90")
        self.assertEqual(self.month()["losses"], Decimal("125.00"))
        self.count(bolts, counted_quantity="100")
        self.assertEqual(self.month()["losses"], Decimal("0.00"))

    def test_roll_stocktake_surplus_offsets_shortage(self):
        film = Material.objects.create(
            name="Баннер", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL, roll_width=Decimal("1.26"), price_per_pm=Decimal("300"),
        )
        roll = receive_lot(film, form=Roll.Form.ROLL, width=Decimal("1.26"), length=Decimal("50"),
                           purchase_cost=Decimal("25000"), received_at=noon(self.first))
        for metres in ("48.9", "50"):
            r = self.client.post(f"/api/warehouse/rolls/{roll.id}/stocktake/",
                                 {"counted_metres": metres, "reason_code": "MISCOUNT"}, format="json")
            self.assertEqual(r.status_code, 201, r.data)
        logs = InventoryLog.objects.filter(material=film, type=InventoryLog.Type.ADJUSTMENT).order_by("id")
        self.assertEqual([lg.cost for lg in logs], [Decimal("550.00"), Decimal("550.00")])
        self.assertEqual(self.month()["losses"], Decimal("0.00"))
        film.refresh_from_db()
        self.assertEqual(film.stock_value, Decimal("25000.00"))

    def test_fifo_cost_without_kopeck_tail(self):
        """STK-10: 20 листов партии за 92 280 — себестоимость ровно 92 280,00."""
        m = Material.objects.create(
            name="Акрил 10мм", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"), piece_price=Decimal("6000"),
        )
        receive_lot(m, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=Decimal("20"), purchase_cost=Decimal("92280"),
                    received_at=timezone.now() - timedelta(days=3))
        receive_lot(m, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=Decimal("20"), purchase_cost=Decimal("99600"))
        costs = []
        for n in (20, 5):
            r = self.client.post("/api/sales/receipts/checkout/", {
                "payment_method": "CASH", "pay_full": True,
                "items": [{"type": "MATERIAL", "material": m.id, "mode": "PIECE", "quantity": n}],
                "confirmed_warnings": ["line_total_high"],
            }, format="json")
            self.assertEqual(r.status_code, 201, r.data)
            costs.append(Decimal(str(r.data["items"][0]["cost_total"])))
        self.assertEqual(costs, [Decimal("92280.00"), Decimal("24900.00")])

    def test_write_off_whole_lot_costs_its_purchase(self):
        m = Material.objects.create(name="Лист", unit=Material.Unit.SQM, is_roll_material=True,
                                    sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"))
        receive_lot(m, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=Decimal("1"), purchase_cost=Decimal("5000"))
        r = self.client.post("/api/warehouse/materials/write-off/",
                             {"material": m.id, "sheets": "1", "reason_code": "DEFECT"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(InventoryLog.objects.get(material=m, type="WRITE_OFF").cost, Decimal("5000.00"))


class CountOnDateTests(APITestCase):
    """G3-N3: пересчёт 30.09, внесённый 2.10, — недостача сентября."""

    def setUp(self):
        self.admin = User.objects.create_user(username="cd_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        today = timezone.localdate()
        self.prev_last = today.replace(day=1) - timedelta(days=1)
        self.prev_first = self.prev_last.replace(day=1)
        self.this_first = today.replace(day=1)
        self.today = today
        self.acr = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
        )
        receive_lot(self.acr, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=Decimal("10"), purchase_cost=Decimal("32000"),
                    received_at=noon(self.prev_first))

    def test_shortage_lands_in_the_month_of_the_count(self):
        r = self.client.post(ADJUST, {"material": self.acr.id, "counted_sheets": "8",
                                      "happened_on": self.prev_last.isoformat()}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        log = InventoryLog.objects.get(type=InventoryLog.Type.ADJUSTMENT)
        self.assertEqual(timezone.localtime(log.happened_at).date(), self.prev_last)
        self.assertEqual(pnl(self.prev_first, self.prev_last)["losses"], Decimal("6400.00"))
        self.assertEqual(pnl(self.this_first, self.today)["losses"], Decimal("0.00"))

    def test_count_into_closed_month_is_400(self):
        lock = PeriodLock.load()
        lock.closed_through = self.prev_last
        lock.save()
        r = self.client.post(ADJUST, {"material": self.acr.id, "counted_sheets": "8",
                                      "happened_on": self.prev_last.isoformat()}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.acr.refresh_from_db()
        self.assertEqual(self.acr.quantity, Decimal("29.7680"))

    def test_future_date_is_400(self):
        r = self.client.post(ADJUST, {"material": self.acr.id, "counted_sheets": "8",
                                      "happened_on": (self.today + timedelta(days=1)).isoformat()}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
