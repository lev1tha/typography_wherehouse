"""Отход (брак) — теми же мерками, что и приход (2026-09-04, просьба владельца).

Лист — ширина × высота × количество или площадью, рулон — метрами с рулона,
штучное — количеством. Каждая строка уходит обычным списанием (FIFO по
партиям, рулон — по рулону), в журнал пишется причина «Отход/брак» и
СЕБЕСТОИМОСТЬ выброшенного; себестоимость видна только тем, кто видит деньги.
Пишет и складовщик — брак видит тот, кто стоит у станка.
"""
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from warehouse.models import InventoryLog, Material, Roll
from warehouse.rolls import receive_lot


class WasteTests(APITestCase):
    URL = "/api/warehouse/waste/"

    def setUp(self):
        self.admin = User.objects.create_user(username="ws_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="ws_store", password="x", role=User.Role.STOREKEEPER)
        self.acct = User.objects.create_user(username="ws_acct", password="x", role=User.Role.ACCOUNTANT)
        self.sheet = Material.objects.create(
            name="Акрил 2мм", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"), price_per_sqm=Decimal("1470"),
        )
        # Две пачки по разной цене: старая дешевле — FIFO берёт её первой.
        self.lot_old = receive_lot(self.sheet, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                                   sheet_count=Decimal("2"), purchase_cost=Decimal("4000"))
        self.lot_new = receive_lot(self.sheet, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                                   sheet_count=Decimal("5"), purchase_cost=Decimal("15000"))
        self.film = Material.objects.create(
            name="Оракал", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL, roll_width=Decimal("1.2"), price_per_pm=Decimal("300"),
        )
        self.roll = receive_lot(self.film, form=Roll.Form.ROLL, width=Decimal("1.2"), length=Decimal("10"),
                                purchase_cost=Decimal("2000"))
        self.bolts = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("100"), purchase_price=Decimal("10"),
        )
        for m in (self.sheet, self.film, self.bolts):
            m.refresh_from_db()
        self.client.force_authenticate(self.store)

    def _post(self, lines, **extra):
        return self.client.post(self.URL, {"lines": lines, **extra}, format="json")

    def test_sheet_by_size_goes_fifo_with_cost(self):
        r = self._post([{
            "material": self.sheet.id, "form": "SHEET",
            "width": "1.22", "height": "2.44", "sheet_count": "2", "note": "царапины",
        }], note="разгрузка")
        self.assertEqual(r.status_code, 201, r.data)
        self.sheet.refresh_from_db()
        self.lot_old.refresh_from_db()
        # 1.22 × 2.44 × 2 = 5.9536 кв.м — ушло из СТАРОЙ пачки целиком.
        self.assertEqual(self.sheet.quantity, Decimal("14.8840"))
        self.assertEqual(self.lot_old.remaining_area, Decimal("0"))
        log = InventoryLog.objects.get(type=InventoryLog.Type.WRITE_OFF)
        self.assertEqual(log.quantity_changed, Decimal("-5.9536"))
        self.assertIn("Отход/брак: царапины, разгрузка", log.reason)
        self.assertIn("лист 1.22×2.44 ×2", log.reason)
        # Себестоимость — по цене старой пачки: 4000 за 5.9536 кв.м.
        self.assertEqual(log.cost, (Decimal("5.9536") * self.lot_old.cost_per_sqm).quantize(Decimal("0.01")))
        self.assertEqual(log.created_by, self.store)
        # Складовщику себестоимость не отдаётся, владельцу — да.
        self.assertIsNone(r.data[0]["cost"])
        self.client.force_authenticate(self.admin)
        rows = self.client.get("/api/warehouse/inventory-logs/", {"type": "WRITE_OFF"}).data["results"]
        self.assertEqual(Decimal(str(rows[0]["cost"])), log.cost)
        self.assertTrue(AuditLog.objects.filter(action__contains="Отход/брак").exists())

    def test_sheet_by_area_from_chosen_lot(self):
        r = self._post([{"material": self.sheet.id, "form": "AREA", "area": "1.5", "roll": self.lot_new.id}])
        self.assertEqual(r.status_code, 201, r.data)
        self.lot_new.refresh_from_db()
        self.lot_old.refresh_from_db()
        self.assertEqual(self.lot_new.remaining_area, self.lot_new.initial_area - Decimal("1.5"))
        self.assertEqual(self.lot_old.remaining_area, self.lot_old.initial_area)
        log = InventoryLog.objects.get(type=InventoryLog.Type.WRITE_OFF)
        # 15000 / 14.884 = 1007.79 за кв.м × 1.5
        self.assertEqual(log.cost, (Decimal("1.5") * self.lot_new.cost_per_sqm).quantize(Decimal("0.01")))
        self.assertIn("1.5 кв.м", log.reason)

    def test_roll_waste_is_metres_from_that_roll(self):
        r = self._post([{"material": self.film.id, "form": "ROLL", "roll": self.roll.id,
                         "length": "2", "note": "зажевало"}])
        self.assertEqual(r.status_code, 201, r.data)
        self.roll.refresh_from_db()
        self.assertEqual(self.roll.metres_remaining, Decimal("8.00"))
        log = InventoryLog.objects.get(type=InventoryLog.Type.WRITE_OFF)
        self.assertEqual(log.metres_changed, Decimal("-2.000"))
        self.assertEqual(log.cost, Decimal("400.00"))  # 200 сом/м × 2
        # Метры пишет `write_off_roll` — в причине они СТОЯТ ОДИН РАЗ. Пока
        # отход добавлял свой «— 2 м» поверх, читалось «… — 2 м Рулон №1: 2 м».
        self.assertEqual(
            log.reason, f"Отход/брак: зажевало — Рулон №{self.roll.pk}: 2 м"
        )

    def test_roll_waste_by_area_converts_with_roll_width(self):
        r = self._post([{"material": self.film.id, "form": "AREA", "area": "2.4"}])
        self.assertEqual(r.status_code, 201, r.data)
        self.roll.refresh_from_db()
        self.assertEqual(self.roll.metres_remaining, Decimal("8.00"))

    def test_waste_crossing_two_lots_costs_by_each_of_them(self):
        """Отход больше одной пачки идёт FIFO и считается ПО КАЖДОЙ партии.

        Списать 8 кв.м при старой пачке в 5.95 — это 5.95 по её цене плюс
        остаток по цене следующей. Одна средняя цена на весь отход соврала бы
        на разнице закупок, а она тут полуторакратная.
        """
        old_cost = self.lot_old.cost_per_sqm
        new_cost = self.lot_new.cost_per_sqm
        self.assertNotEqual(old_cost, new_cost)
        r = self._post([{"material": self.sheet.id, "form": "AREA", "area": "8"}])
        self.assertEqual(r.status_code, 201, r.data)
        self.lot_old.refresh_from_db()
        self.lot_new.refresh_from_db()
        # Старая пачка вычерпана до нуля, остаток ушёл из новой.
        rest = Decimal("8") - Decimal("5.9536")
        self.assertEqual(self.lot_old.remaining_area, Decimal("0"))
        self.assertEqual(self.lot_new.remaining_area, self.lot_new.initial_area - rest)
        log = InventoryLog.objects.get(type=InventoryLog.Type.WRITE_OFF)
        self.assertEqual(log.quantity_changed, Decimal("-8.0000"))
        self.assertEqual(
            log.cost,
            (Decimal("5.9536") * old_cost + rest * new_cost).quantize(Decimal("0.01")),
        )

    def test_roll_waste_to_the_last_metre_leaves_no_tail(self):
        """Рулон, испорченный целиком, уходит в ноль без хвоста округления.

        Метры × ширина не всегда дают ровно площадь партии, и без отдельной
        ветки в рулоне оставалось бы 0.0004 кв.м: пустой рулон продолжал бы
        числиться на складе и лезть в FIFO.
        """
        r = self._post([{"material": self.film.id, "form": "ROLL",
                         "roll": self.roll.id, "length": "10", "note": "утопили"}])
        self.assertEqual(r.status_code, 201, r.data)
        self.roll.refresh_from_db()
        self.film.refresh_from_db()
        self.assertEqual(self.roll.remaining_area, Decimal("0"))
        self.assertEqual(self.roll.metres_remaining, Decimal("0.00"))
        self.assertEqual(self.film.quantity, Decimal("0"))
        log = InventoryLog.objects.get(type=InventoryLog.Type.WRITE_OFF)
        self.assertEqual(log.cost, Decimal("2000.00"))  # вся закупка рулона
        # Списывать из пустого рулона больше нечего.
        r = self._post([{"material": self.film.id, "form": "ROLL",
                         "roll": self.roll.id, "length": "1"}])
        self.assertEqual(r.status_code, 400, r.data)

    def test_piece_material_by_quantity(self):
        r = self._post([{"material": self.bolts.id, "quantity": "10", "note": "погнутые"}])
        self.assertEqual(r.status_code, 201, r.data)
        self.bolts.refresh_from_db()
        self.assertEqual(self.bolts.quantity, Decimal("90"))
        log = InventoryLog.objects.get(type=InventoryLog.Type.WRITE_OFF)
        self.assertEqual(log.cost, Decimal("100.00"))
        self.assertIn("10 шт", log.reason)

    def test_several_lines_at_once_all_or_nothing(self):
        r = self._post([
            {"material": self.bolts.id, "quantity": "5"},
            {"material": self.sheet.id, "form": "SHEET", "width": "1.22", "height": "2.44", "sheet_count": "100"},
        ])
        self.assertEqual(r.status_code, 400, r.data)
        self.bolts.refresh_from_db()
        self.assertEqual(self.bolts.quantity, Decimal("100"))
        self.assertFalse(InventoryLog.objects.filter(type=InventoryLog.Type.WRITE_OFF).exists())

    def test_backdated(self):
        r = self._post([{"material": self.bolts.id, "quantity": "1"}], happened_on="2026-08-20")
        self.assertEqual(r.status_code, 201, r.data)
        log = InventoryLog.objects.get(type=InventoryLog.Type.WRITE_OFF)
        self.assertEqual(timezone.localtime(log.happened_at).date().isoformat(), "2026-08-20")
        r = self._post([{"material": self.bolts.id, "quantity": "1"}], happened_on="2099-01-01")
        self.assertEqual(r.status_code, 400, r.data)

    def test_sheet_needs_dimensions_and_roll_needs_metres(self):
        r = self._post([{"material": self.sheet.id, "form": "SHEET", "width": "1.22"}])
        self.assertEqual(r.status_code, 400, r.data)
        r = self._post([{"material": self.film.id, "form": "ROLL"}])
        self.assertEqual(r.status_code, 400, r.data)
        r = self._post([])
        self.assertEqual(r.status_code, 400, r.data)

    def test_accountant_is_read_only(self):
        self.client.force_authenticate(self.acct)
        r = self._post([{"material": self.bolts.id, "quantity": "1"}])
        self.assertEqual(r.status_code, 403, r.data)
        self.client.force_authenticate(None)
        r = self._post([{"material": self.bolts.id, "quantity": "1"}])
        self.assertIn(r.status_code, (401, 403))
