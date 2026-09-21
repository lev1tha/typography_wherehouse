"""Отходы — услуга со свободной меркой (2026-09-21, просьба владельца).

Цех продаёт обрезки и брак. Отходы бывают от ЛЮБОГО товара на складе, поэтому
мерка у каждой строки своя: лист — квадратами, рулон — метрами, штучное —
штуками. Мерку и цену называют в кассе (цену — и складовщик: на отходы она
всегда договорная), склада услуга не касается — отход уже списан браком или
остался обрезком от резки.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt, TransactionItem
from services.models import PrintingService
from warehouse.models import InventoryLog, Material


class WasteServiceTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="w_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="w_store", password="x", role=User.Role.STOREKEEPER)
        self.waste = PrintingService.objects.get(kind=PrintingService.Kind.WASTE)
        self.waste.rate_flat = Decimal("300")       # за кв.м
        self.waste.rate_per_pm = Decimal("120")     # за пог.м
        self.waste.rate_per_piece = Decimal("50")   # за штуку
        self.waste.save()

    def _checkout(self, items, **extra):
        return self.client.post(
            "/api/sales/receipts/checkout/",
            {"payment_method": "CASH", "pay_full": True, "items": items, **extra},
            format="json",
        )

    def _line(self, **over):
        return {"type": "SERVICE", "service": self.waste.id, **over}

    # --- услуга заведена миграцией --------------------------------------

    def test_migration_created_the_service(self):
        svc = PrintingService.objects.filter(kind=PrintingService.Kind.WASTE)
        self.assertEqual(svc.count(), 1)
        self.assertEqual(svc.get().name_ru, "Отходы")
        self.assertTrue(svc.get().uses_free_measure)
        # Свободная мерка — НЕ площадная услуга: иначе касса требовала бы
        # «ширину × длину» у отходов, которые продают метрами.
        self.assertFalse(svc.get().uses_area)
        self.assertFalse(svc.get().uses_material)
        self.assertFalse(svc.get().uses_running_meter)
        self.assertFalse(svc.get().uses_pieces)
        self.assertTrue(svc.get().staff_sets_rate)

    # --- три мерки --------------------------------------------------------

    def test_square_metres_by_dimensions(self):
        self.client.force_authenticate(self.store)
        r = self._checkout([self._line(
            mode="SQM", width="0.8", length="1.2", note="обрезки акрила",
        )])
        self.assertEqual(r.status_code, 201, r.data)
        # 0.8 × 1.2 = 0.96 кв.м × 300 = 288
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("288"))
        item = TransactionItem.objects.get(receipt_id=r.data["id"])
        self.assertEqual(item.quantity, Decimal("0.960"))
        self.assertEqual(item.price_per_item, Decimal("300"))
        self.assertEqual(item.sale_mode, TransactionItem.SaleMode.SQM)
        self.assertEqual(item.note, "обрезки акрила")
        self.assertEqual(r.data["items"][0]["unit_code"], "SQM")
        self.assertEqual(r.data["items"][0]["unit_label"], "кв.м")

    def test_square_metres_by_ready_area(self):
        """Площадь можно прислать готовой — не всякий обрезок прямоугольный."""
        self.client.force_authenticate(self.store)
        r = self._checkout([self._line(mode="SQM", quantity="2.5")])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("750"))

    def test_running_metres(self):
        self.client.force_authenticate(self.store)
        r = self._checkout([self._line(mode="METER", quantity="3.5", note="остаток оракала")])
        self.assertEqual(r.status_code, 201, r.data)
        # 3.5 пог.м × 120 = 420
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("420"))
        item = TransactionItem.objects.get(receipt_id=r.data["id"])
        self.assertEqual(item.sale_mode, TransactionItem.SaleMode.METER)
        self.assertEqual(r.data["items"][0]["unit_code"], "METER")
        self.assertEqual(r.data["items"][0]["unit_label"], "пог.м")

    def test_pieces(self):
        self.client.force_authenticate(self.store)
        r = self._checkout([self._line(mode="PIECE", quantity="4", note="бракованные уголки")])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("200"))
        item = TransactionItem.objects.get(receipt_id=r.data["id"])
        self.assertEqual(item.sale_mode, TransactionItem.SaleMode.PIECE)
        self.assertEqual(r.data["items"][0]["unit_code"], "PIECE")
        self.assertEqual(r.data["items"][0]["unit_label"], "шт")

    def test_three_measures_in_one_receipt(self):
        """Отходы всех товаров сразу — ради этого мерка и сделана построчной."""
        self.client.force_authenticate(self.store)
        r = self._checkout([
            self._line(mode="SQM", width="1", length="1"),
            self._line(mode="METER", quantity="2"),
            self._line(mode="PIECE", quantity="3"),
        ])
        self.assertEqual(r.status_code, 201, r.data)
        # 300 + 240 + 150
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("690"))
        self.assertEqual(
            [i["unit_label"] for i in r.data["items"]], ["кв.м", "пог.м", "шт"]
        )

    def test_dimensions_are_kept_only_for_area(self):
        """У метров и штук размеров нет — чужие цифры в колонках врали бы."""
        self.client.force_authenticate(self.store)
        r = self._checkout([self._line(mode="METER", quantity="2", width="1", length="1")])
        self.assertEqual(r.status_code, 201, r.data)
        item = TransactionItem.objects.get(receipt_id=r.data["id"])
        self.assertIsNone(item.width)
        self.assertIsNone(item.length)
        self.assertEqual(item.quantity, Decimal("2.000"))

    # --- склада не касается ------------------------------------------------

    def test_never_touches_stock(self):
        self.client.force_authenticate(self.store)
        r = self._checkout([self._line(mode="SQM", width="2", length="2")])
        self.assertEqual(r.status_code, 201, r.data)
        item = TransactionItem.objects.get(receipt_id=r.data["id"])
        self.assertIsNone(item.material_id)
        self.assertEqual(item.cost_total, Decimal("0"))
        self.assertFalse(InventoryLog.objects.exists())

    def test_material_is_rejected_not_silently_dropped(self):
        """Отход уже списан — второе списание увело бы остаток в минус."""
        mat = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, quantity=Decimal("10"),
            price_per_unit=Decimal("1000"), is_roll_material=True,
        )
        self.client.force_authenticate(self.admin)
        r = self._checkout([self._line(mode="SQM", width="1", length="1", material=mat.id)])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("уже списан", str(r.data))
        self.assertFalse(Receipt.objects.exists())

    # --- мерка и количество обязательны ------------------------------------

    def test_mode_is_required(self):
        """2 у отходов рулона — метры, у отходов листа — квадраты. Не угадываем."""
        self.client.force_authenticate(self.admin)
        r = self._checkout([self._line(quantity="2")])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("укажите мерку", str(r.data))
        self.assertFalse(Receipt.objects.exists())

    def test_area_without_size_or_quantity_is_rejected(self):
        self.client.force_authenticate(self.admin)
        r = self._checkout([self._line(mode="SQM")])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("размеры", str(r.data))

    def test_metres_without_quantity_is_rejected(self):
        self.client.force_authenticate(self.admin)
        r = self._checkout([self._line(mode="METER")])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("длину в пог.м", str(r.data))

    def test_pieces_without_quantity_is_rejected(self):
        self.client.force_authenticate(self.admin)
        r = self._checkout([self._line(mode="PIECE")])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("количество штук", str(r.data))

    def test_area_rounding_to_zero_is_rejected(self):
        """0.01 × 0.01 округляется до 0.000 — строка ушла бы в чек нулём."""
        self.client.force_authenticate(self.admin)
        r = self._checkout([self._line(mode="SQM", width="0.01", length="0.01")])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertFalse(Receipt.objects.exists())

    # --- цена --------------------------------------------------------------

    def test_storekeeper_sets_the_price(self):
        """Цена на отходы всегда договорная — админа за ней не зовут."""
        self.client.force_authenticate(self.store)
        r = self._checkout([self._line(mode="SQM", width="1", length="1", cut_rate="150")])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("150"))

    def test_storekeeper_cannot_gift_waste(self):
        """Ноль — подарок, и это по-прежнему решение админа (аудит 18.08, п. 14)."""
        self.client.force_authenticate(self.store)
        r = self._checkout([self._line(mode="PIECE", quantity="2", cut_rate="0")])
        self.assertEqual(r.status_code, 403, r.data)
        self.assertIn("подарок", str(r.data))
        self.assertFalse(Receipt.objects.exists())
        self.client.force_authenticate(self.admin)
        r = self._checkout([self._line(mode="PIECE", quantity="2", cut_rate="0")])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("0"))

    def test_empty_catalogue_price_for_that_measure_is_rejected(self):
        """Пустая ставка — незаполненный справочник, а не скидка."""
        self.waste.rate_per_pm = Decimal("0")
        self.waste.save()
        self.client.force_authenticate(self.store)
        r = self._checkout([self._line(mode="METER", quantity="2")])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("цена за пог.м", str(r.data))
        # Другие мерки при этом работают: ставки у них свои.
        r = self._checkout([self._line(mode="SQM", width="1", length="1")])
        self.assertEqual(r.status_code, 201, r.data)

    def test_material_price_override_still_admin_only(self):
        self.client.force_authenticate(self.store)
        r = self._checkout([self._line(mode="SQM", width="1", length="1", material_price="1")])
        self.assertEqual(r.status_code, 403, r.data)

    def test_own_material_flag_does_not_apply(self):
        """«Материал клиента» у отходов бессмыслен: отходы всегда свои."""
        self.client.force_authenticate(self.store)
        r = self._checkout([self._line(mode="SQM", width="1", length="1", own_material=True)])
        self.assertEqual(r.status_code, 400, r.data)

    # --- дозаказ, возврат, каталог -----------------------------------------

    def test_add_items_waste_from_storekeeper(self):
        mat = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("10"),
            price_per_unit=Decimal("5"),
        )
        self.client.force_authenticate(self.store)
        r = self._checkout([{"type": "MATERIAL", "material": mat.id, "quantity": 2}])
        self.assertEqual(r.status_code, 201, r.data)
        rid = r.data["id"]
        r = self.client.post(
            f"/api/sales/receipts/{rid}/add-items/",
            {"items": [self._line(mode="METER", quantity="2", cut_rate="100")]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Receipt.objects.get(pk=rid).total_price, Decimal("210"))

    def test_refund_of_waste_line_restores_nothing_to_stock(self):
        self.client.force_authenticate(self.admin)
        r = self._checkout([self._line(mode="SQM", width="1", length="1")])
        rid = r.data["id"]
        item_id = r.data["items"][0]["id"]
        r = self.client.post(
            f"/api/sales/receipts/{rid}/refund/", {"items": [item_id]}, format="json"
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(TransactionItem.objects.get(pk=item_id).is_returned)
        self.assertFalse(InventoryLog.objects.exists())

    def test_pricing_page_edits_all_three_rates(self):
        self.client.force_authenticate(self.admin)
        r = self.client.patch(
            f"/api/services/services/{self.waste.id}/",
            {"rate_flat": "350", "rate_per_pm": "130", "rate_per_piece": "60"},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(str(r.data["rate_flat"])), Decimal("350"))
        self.assertEqual(Decimal(str(r.data["rate_per_pm"])), Decimal("130"))
        self.assertEqual(Decimal(str(r.data["rate_per_piece"])), Decimal("60"))
        # Касса складовщика читает свежие ставки и признак свободной мерки.
        self.client.force_authenticate(self.store)
        r = self.client.get(f"/api/services/services/{self.waste.id}/")
        self.assertEqual(r.data["kind"], "WASTE")
        self.assertTrue(r.data["uses_free_measure"])

    def test_waste_stays_out_of_the_cutting_machine_report(self):
        """Отходы — не работа станка: в отчёте по ЧПУ/лазеру их быть не должно."""
        self.client.force_authenticate(self.admin)
        r = self._checkout([self._line(mode="SQM", width="2", length="2")])
        self.assertEqual(r.status_code, 201, r.data)
        r = self.client.get("/api/finance/report/")
        self.assertEqual(r.status_code, 200, r.data)
        cutting = r.data["cutting"]
        self.assertEqual(Decimal(str(cutting["total"])), Decimal("0"))
        self.assertEqual(
            sum(Decimal(str(row["amount"])) for row in cutting["rows"]), Decimal("0")
        )
