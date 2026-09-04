"""«Только резка» — материал клиента (2026-09-04, просьба владельца).

Клиент приносит своё, цех только режет. В чеке одна строка работы: цена резки
× сколько отрезано, что резали — комментарием. Со склада ничего не уходит.
Цену резки здесь вписывает и складовщик: каталожной ставки у чужого материала
нет, а звать админа на каждый такой заказ незачем. На обычные строки правило
аудита (п. 14) не распространяется — там ставку по-прежнему правит только админ.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt, TransactionItem
from services.models import PrintingService
from warehouse.models import InventoryLog, Material, Roll
from warehouse.rolls import receive_lot


class OwnMaterialLifecycleTests(APITestCase):
    """Жизнь строки «материал клиента» после продажи: возврат, удаление чека,
    правка состава. Ни одно из этих действий не должно трогать склад: брать
    оттуда было нечего. Флаг и комментарий обязаны пережить правку — без них
    строка в чеке снова становится безымянной «Резкой ЧПУ».
    """

    def setUp(self):
        self.admin = User.objects.create_user(username="oml_admin", password="x", role=User.Role.ADMIN)
        self.cnc = PrintingService.objects.create(
            name="Резка ЧПУ", kind=PrintingService.Kind.CUTTING, machine=PrintingService.Machine.CNC,
        )
        self.engraving = PrintingService.objects.get(kind=PrintingService.Kind.ENGRAVING)
        self.engraving.rate_flat = Decimal("3000")
        self.engraving.save()
        # Материал цеха в том же чеке: он-то списаться обязан — иначе тест
        # «склад не двинулся» проходил бы и на сломанном списании.
        self.sheet = Material.objects.create(
            name="Акрил 3мм", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"), price_per_sqm=Decimal("1000"),
        )
        receive_lot(self.sheet, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=Decimal("2"), purchase_cost=Decimal("6000"))
        self.sheet.refresh_from_db()
        self.stock = self.sheet.quantity
        self.client.force_authenticate(self.admin)

    def _order(self):
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True, "items": [
                {"type": "SERVICE", "service": self.cnc.id, "own_material": True,
                 "running_meters": "10", "cut_rate": "50", "note": "чужой лист"},
                {"type": "SERVICE", "service": self.engraving.id,
                 "width": "1", "length": "1", "note": "лого"},
                {"type": "MATERIAL", "material": self.sheet.id, "quantity": "0.5", "mode": "SQM"},
            ]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        # 10 × 50 + 1 кв.м × 3000 + 0.5 кв.м × 1000 = 4 000
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("4000"))
        return r.data["id"]

    def _sold(self, rid):
        """Сколько движений склада наделал чек: у чужого материала и гравировки
        их быть не должно, у материала цеха — ровно одно."""
        return list(
            InventoryLog.objects.filter(receipt_id=rid).values_list("type", "quantity_changed")
        )

    def test_only_the_shop_material_moves_the_stock(self):
        rid = self._order()
        self.assertEqual(self._sold(rid), [("SALE", Decimal("-0.5000"))])
        self.sheet.refresh_from_db()
        self.assertEqual(self.sheet.quantity, self.stock - Decimal("0.5"))

    def test_refund_returns_the_material_and_nothing_else(self):
        rid = self._order()
        r = self.client.post(f"/api/sales/receipts/{rid}/refund/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.sheet.refresh_from_db()
        # Материал вернулся на полку, работа по чужому листу возврата не имеет.
        self.assertEqual(self.sheet.quantity, self.stock)
        # Лента отсортирована по дате операции, свежие сверху: возврат впереди.
        kinds = sorted(kind for kind, _ in self._sold(rid))
        self.assertEqual(kinds, ["RETURN", "SALE"])
        receipt = Receipt.objects.get(pk=rid)
        self.assertEqual(receipt.refunded_amount, Decimal("4000"))

    def test_delete_wipes_the_order_without_a_stock_trace(self):
        rid = self._order()
        r = self.client.delete(f"/api/sales/receipts/{rid}/")
        self.assertEqual(r.status_code, 204, r.data)
        self.sheet.refresh_from_db()
        self.assertEqual(self.sheet.quantity, self.stock)
        # Удаление — исправление опечатки, а не событие склада: следа нет.
        self.assertEqual(self._sold(rid), [])
        self.assertFalse(Receipt.objects.filter(pk=rid).exists())

    def test_edit_items_keeps_the_flag_and_the_note(self):
        rid = self._order()
        receipt = Receipt.objects.get(pk=rid)
        own = receipt.items.get(own_material=True)
        engraving = receipt.items.get(service=self.engraving)

        # Длина реза 10 → 20 пог.м: 500 → 1 000, итог 4 000 → 4 500.
        r = self.client.post(f"/api/sales/receipts/{rid}/edit-items/",
                             {"items": [{"id": own.id, "quantity": "20"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.total_price, Decimal("4500"))

        # Цена резки 50 → 60: 1 000 → 1 200.
        r = self.client.post(f"/api/sales/receipts/{rid}/edit-items/",
                             {"items": [{"id": own.id, "price_per_item": "60"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.total_price, Decimal("4700"))

        # Площадь гравировки 1 → 2 кв.м: 3 000 → 6 000.
        r = self.client.post(f"/api/sales/receipts/{rid}/edit-items/",
                             {"items": [{"id": engraving.id, "quantity": "2"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.total_price, Decimal("7700"))

        own.refresh_from_db()
        engraving.refresh_from_db()
        self.assertTrue(own.own_material)
        self.assertEqual(own.note, "чужой лист")
        self.assertEqual(engraving.note, "лого")
        # Правка чужого материала складом не двигала — движение осталось одно.
        self.assertEqual(len(self._sold(rid)), 1)

    def test_edit_items_can_remove_the_own_material_line(self):
        rid = self._order()
        receipt = Receipt.objects.get(pk=rid)
        own = receipt.items.get(own_material=True)
        r = self.client.post(f"/api/sales/receipts/{rid}/edit-items/",
                             {"items": [{"id": own.id, "remove": True}]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.total_price, Decimal("3500"))
        self.assertFalse(receipt.items.filter(own_material=True).exists())
        self.sheet.refresh_from_db()
        self.assertEqual(self.sheet.quantity, self.stock - Decimal("0.5"))


class OwnMaterialCutTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="om_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="om_store", password="x", role=User.Role.STOREKEEPER)
        self.acct = User.objects.create_user(username="om_acct", password="x", role=User.Role.ACCOUNTANT)
        # У станка своей ставки нет — как на проде: «берётся у материала».
        self.cnc = PrintingService.objects.create(
            name="Резка ЧПУ", kind=PrintingService.Kind.CUTTING, machine=PrintingService.Machine.CNC,
        )
        self.laser = PrintingService.objects.create(
            name="Резка лазером", kind=PrintingService.Kind.CUTTING,
            machine=PrintingService.Machine.LASER, rate_per_pm=Decimal("30"),
        )
        self.sheet = Material.objects.create(
            name="Акрил 3мм", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
            price_per_sqm=Decimal("1250"), cut_rate_per_pm=Decimal("20"),
        )
        receive_lot(self.sheet, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=Decimal("2"), purchase_cost=Decimal("7000"))
        self.sheet.refresh_from_db()

    def _checkout(self, items, **extra):
        return self.client.post(
            "/api/sales/receipts/checkout/",
            {"payment_method": "CASH", "pay_full": True, "items": items, **extra},
            format="json",
        )

    def test_storekeeper_sells_cut_only_with_own_price(self):
        self.client.force_authenticate(self.store)
        stock_before = self.sheet.quantity
        r = self._checkout([{
            "type": "SERVICE", "service": self.cnc.id, "own_material": True,
            "running_meters": "12.5", "cut_rate": "40", "note": "акрил 3 мм клиента",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("500"))
        item = TransactionItem.objects.get(receipt_id=r.data["id"])
        self.assertTrue(item.own_material)
        self.assertIsNone(item.material_id)
        self.assertEqual(item.note, "акрил 3 мм клиента")
        self.assertEqual(item.quantity, Decimal("12.500"))
        self.assertEqual(item.price_per_item, Decimal("40"))
        self.assertEqual(item.cost_total, Decimal("0"))
        # Со склада ничего не ушло и в журнале склада пусто.
        self.sheet.refresh_from_db()
        self.assertEqual(self.sheet.quantity, stock_before)
        self.assertFalse(InventoryLog.objects.filter(type=InventoryLog.Type.SALE).exists())
        # В ответе видно, что это чужой материал и что резали.
        line = r.data["items"][0]
        self.assertTrue(line["own_material"])
        self.assertEqual(line["note"], "акрил 3 мм клиента")
        self.assertEqual(line["unit_code"], "METER")

    def test_machine_rate_is_used_when_price_not_given(self):
        """У лазера ставка задана — цену можно и не вписывать."""
        self.client.force_authenticate(self.store)
        r = self._checkout([{
            "type": "SERVICE", "service": self.laser.id, "own_material": True, "running_meters": "2",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("60"))

    def test_no_rate_anywhere_is_rejected_with_a_hint(self):
        """ЧПУ без ставки и без вписанной цены — отказ, а не работа за 0."""
        self.client.force_authenticate(self.store)
        r = self._checkout([{
            "type": "SERVICE", "service": self.cnc.id, "own_material": True, "running_meters": "2",
        }])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("материала клиента", str(r.data))
        self.assertFalse(Receipt.objects.exists())

    def test_own_material_with_stock_material_is_rejected(self):
        """Чужой материал И материал со склада разом — противоречие, не угадываем."""
        self.client.force_authenticate(self.admin)
        r = self._checkout([{
            "type": "SERVICE", "service": self.cnc.id, "own_material": True,
            "material": self.sheet.id, "running_meters": "2", "cut_rate": "40",
        }])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("материал клиента", str(r.data))

    def test_own_material_needs_cut_length(self):
        self.client.force_authenticate(self.store)
        r = self._checkout([{
            "type": "SERVICE", "service": self.cnc.id, "own_material": True, "cut_rate": "40",
        }])
        self.assertEqual(r.status_code, 400, r.data)

    def test_own_material_only_for_area_services(self):
        other = PrintingService.objects.create(
            name="Доставка", kind=PrintingService.Kind.OTHER, base_price=Decimal("300"),
        )
        self.client.force_authenticate(self.admin)
        r = self._checkout([{"type": "SERVICE", "service": other.id, "own_material": True, "quantity": 1}])
        self.assertEqual(r.status_code, 400, r.data)

    def test_storekeeper_cannot_gift_the_work_for_free(self):
        """Цену складовщик называет, а ПОДАРОК остаётся правом админа.

        Интерфейс нулевую цену не даёт добавить (кнопка закрыта, пока цена не
        больше нуля), но API принимал её от кого угодно: консоли браузера
        хватало, чтобы отдать резку чужого материала даром. Для обычных строк
        эту дверь закрыл аудит 18.08 (п. 14); открыв цену складовщику 04.09,
        мы её приоткрыли снова.
        """
        self.client.force_authenticate(self.store)
        r = self._checkout([{
            "type": "SERVICE", "service": self.cnc.id, "own_material": True,
            "running_meters": "10", "cut_rate": "0", "note": "чужой лист",
        }])
        self.assertEqual(r.status_code, 403, r.data)
        self.assertIn("подарок", str(r.data))
        self.assertFalse(Receipt.objects.exists())
        # Дозаказ — та же дверь, и она тоже закрыта.
        r = self._checkout([{
            "type": "SERVICE", "service": self.cnc.id, "own_material": True,
            "running_meters": "5", "cut_rate": "40",
        }])
        rid = r.data["id"]
        r = self.client.post(
            f"/api/sales/receipts/{rid}/add-items/",
            {"items": [{"type": "SERVICE", "service": self.cnc.id, "own_material": True,
                        "running_meters": "2", "cut_rate": "0"}]},
            format="json",
        )
        self.assertEqual(r.status_code, 403, r.data)
        # А цену больше нуля складовщик по-прежнему называет сам.
        r = self.client.post(
            f"/api/sales/receipts/{rid}/add-items/",
            {"items": [{"type": "SERVICE", "service": self.cnc.id, "own_material": True,
                        "running_meters": "2", "cut_rate": "35"}]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)

    def test_admin_may_still_gift_the_work(self):
        """Явный ноль от админа — осознанный подарок, он проходит как раньше."""
        self.client.force_authenticate(self.admin)
        r = self._checkout([{
            "type": "SERVICE", "service": self.cnc.id, "own_material": True,
            "running_meters": "10", "cut_rate": "0", "note": "в подарок постоянному",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("0"))

    def test_storekeeper_still_cannot_override_catalogue_cut_rate(self):
        """Правило аудита п. 14 для обычной резки не ослабло."""
        self.client.force_authenticate(self.store)
        r = self._checkout([{
            "type": "SERVICE", "service": self.cnc.id, "material": self.sheet.id,
            "width": "0.5", "length": "1", "running_meters": "2", "cut_rate": "0",
        }])
        self.assertEqual(r.status_code, 403, r.data)
        # И цену материала при чужом материале тоже не пропускаем.
        r = self._checkout([{
            "type": "SERVICE", "service": self.cnc.id, "own_material": True,
            "running_meters": "2", "cut_rate": "40", "material_price": "0",
        }])
        self.assertEqual(r.status_code, 403, r.data)

    def test_accountant_cannot_sell(self):
        self.client.force_authenticate(self.acct)
        r = self._checkout([{
            "type": "SERVICE", "service": self.cnc.id, "own_material": True,
            "running_meters": "2", "cut_rate": "40",
        }])
        self.assertEqual(r.status_code, 403, r.data)

    def test_add_items_accepts_own_material_from_storekeeper(self):
        self.client.force_authenticate(self.store)
        r = self._checkout([{"type": "MATERIAL", "material": self.sheet.id, "quantity": 1, "mode": "PIECE",
                             }], pay_full=False, amount_paid=0)
        self.assertEqual(r.status_code, 400, r.data)  # у листа нет цены за лист — обычная проверка
        r = self._checkout([{"type": "MATERIAL", "material": self.sheet.id, "quantity": "0.5", "mode": "SQM"}])
        self.assertEqual(r.status_code, 201, r.data)
        rid = r.data["id"]
        r = self.client.post(
            f"/api/sales/receipts/{rid}/add-items/",
            {"items": [{"type": "SERVICE", "service": self.cnc.id, "own_material": True,
                        "running_meters": "3", "cut_rate": "50", "note": "фанера клиента"}]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        receipt = Receipt.objects.get(pk=rid)
        self.assertEqual(receipt.total_price, Decimal("625") + Decimal("150"))
        self.assertEqual(receipt.items.filter(own_material=True).count(), 1)

    def test_own_material_cut_counts_in_cutting_report(self):
        """Работа по чужому материалу — тоже работа станка: в «Резке по
        станкам» её сумма и метры есть, площадь материала — ноль."""
        self.client.force_authenticate(self.admin)
        self._checkout([{
            "type": "SERVICE", "service": self.laser.id, "own_material": True,
            "running_meters": "4", "cut_rate": "25",
        }])
        r = self.client.get("/api/finance/report/")
        self.assertEqual(r.status_code, 200, r.data)
        cutting = r.data["cutting"]
        self.assertEqual(Decimal(str(cutting["total"])), Decimal("100"))
        laser = next(row for row in cutting["rows"] if row["id"] == "LASER")
        self.assertEqual(Decimal(str(laser["running_meters"])), Decimal("4"))
        self.assertEqual(Decimal(str(laser["area"])), Decimal("0"))
