"""Остаток материала обязан сходиться с суммой остатков партий FIFO.

`Material.quantity` и Σ`Roll.remaining_area` — две записи об одном и том же:
сколько материала лежит на складе. Разъехались — значит одна из операций подвинула
только одну из них, и дальше врут либо остаток, либо себестоимость по партиям.

Проверяется ПОСЛЕ КАЖДОГО пути, который двигает склад: продажа куском и целым
листом, возврат, правка количества, правка цены, удаление строки, удаление чека.
Комбинации важны не меньше отдельных операций: восстановление партии ограничено
её начальной площадью, и ошибка вылезает именно там, где восстанавливают дважды.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt, TransactionItem
from sales.sale_service import create_sale, delete_receipt, refund_receipt, update_receipt_items
from services.models import PrintingService
from warehouse.models import Material
from warehouse.rolls import receive_lot


class StockIntegrityTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="boss_si", password="x", role=User.Role.ADMIN
        )
        self.client_obj = Client.objects.create(full_name="Тахир", phone="+996555111222")
        self.mat = Material.objects.create(
            name="Акрил лист",
            unit=Material.Unit.SQM,
            quantity=Decimal("0"),
            is_roll_material=True,
            price_per_sqm=Decimal("1000"),
            piece_price=Decimal("3000"),
            piece_area=Decimal("2.9768"),
            purchase_price=Decimal("600"),
            cut_rate_per_pm=Decimal("50"),
        )
        # Две партии: восстановление ограничено начальной площадью КАЖДОЙ, и
        # ошибки видны только когда партий больше одной.
        receive_lot(self.mat, form="SHEET", width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=5, purchase_cost=Decimal("9000"))
        receive_lot(self.mat, form="SHEET", width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=5, purchase_cost=Decimal("10000"))
        self.cutting = PrintingService.objects.create(
            name="Резка", kind=PrintingService.Kind.CUTTING,
            machine=PrintingService.Machine.CNC,
        )

    # ---- helpers -----------------------------------------------------------
    def assertStockConsistent(self, msg=""):
        self.mat.refresh_from_db()
        lots = sum((r.remaining_area for r in self.mat.rolls.all()), Decimal("0"))
        self.assertEqual(
            self.mat.quantity.quantize(Decimal("0.0001")),
            lots.quantize(Decimal("0.0001")),
            f"остаток {self.mat.quantity} разошёлся с партиями {lots}. {msg}",
        )

    def _sale_area(self, area="2"):
        return create_sale(
            client=self.client_obj, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": self.mat,
                         "quantity": Decimal(area), "mode": "SQM"}],
            amount_paid=None,
        )

    def _sale_pieces(self, count=1):
        return create_sale(
            client=self.client_obj, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": self.mat,
                         "quantity": Decimal(count), "mode": "PIECE"}],
            amount_paid=None,
        )

    def _line(self, receipt):
        return receipt.items.filter(type=TransactionItem.Type.MATERIAL).first()

    # ---- продажа -----------------------------------------------------------
    def test_area_sale_keeps_stock_and_lots_together(self):
        self._sale_area("4")
        self.assertStockConsistent("после продажи куском")

    def test_piece_sale_keeps_stock_and_lots_together(self):
        """Продажа ЛИСТОМ у рулонного материала списывает площадь листа —
        именно на этом пути расхождение и всплывало."""
        self._sale_pieces(2)
        self.assertStockConsistent("после продажи листами")

    # ---- удаление чека -----------------------------------------------------
    def test_delete_after_area_sale(self):
        r = self._sale_area("4")
        delete_receipt(r, user=self.admin)
        self.assertStockConsistent("после удаления чека с продажей куском")

    def test_delete_after_piece_sale(self):
        r = self._sale_pieces(3)
        delete_receipt(r, user=self.admin)
        self.assertStockConsistent("после удаления чека с продажей листами")

    def test_delete_returns_exactly_what_was_taken(self):
        self.mat.refresh_from_db()  # приход поднял остаток уже после create()
        before = self.mat.quantity
        r = self._sale_pieces(3)
        delete_receipt(r, user=self.admin)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, before)

    # ---- правка состава ----------------------------------------------------
    def test_edit_quantity_down(self):
        r = self._sale_area("6")
        update_receipt_items(r, [{"id": self._line(r).id, "quantity": 2}], user=self.admin)
        self.assertStockConsistent("после уменьшения количества")

    def test_edit_quantity_up(self):
        r = self._sale_area("2")
        update_receipt_items(r, [{"id": self._line(r).id, "quantity": 7}], user=self.admin)
        self.assertStockConsistent("после увеличения количества")

    def test_edit_piece_quantity(self):
        r = self._sale_pieces(1)
        update_receipt_items(r, [{"id": self._line(r).id, "quantity": 3}], user=self.admin)
        self.assertStockConsistent("после правки количества листов")

    def test_edit_price_only_does_not_move_stock(self):
        r = self._sale_area("3")
        self.mat.refresh_from_db()
        before = self.mat.quantity
        update_receipt_items(r, [{"id": self._line(r).id, "price_per_item": 1234}], user=self.admin)
        self.assertStockConsistent("после правки только цены")
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, before)

    def test_remove_line(self):
        r = self._sale_area("5")
        update_receipt_items(r, [{"id": self._line(r).id, "remove": True}], user=self.admin)
        self.assertStockConsistent("после удаления строки")

    # ---- возврат -----------------------------------------------------------
    def test_refund_after_piece_sale(self):
        r = self._sale_pieces(2)
        refund_receipt(r, user=self.admin)
        self.assertStockConsistent("после возврата листов")

    # ---- комбинации --------------------------------------------------------
    def test_edit_then_delete(self):
        r = self._sale_pieces(3)
        update_receipt_items(r, [{"id": self._line(r).id, "quantity": 1}], user=self.admin)
        delete_receipt(r, user=self.admin)
        self.assertStockConsistent("правка, затем удаление чека")

    def test_edit_twice_then_refund(self):
        r = self._sale_area("6")
        update_receipt_items(r, [{"id": self._line(r).id, "quantity": 3}], user=self.admin)
        update_receipt_items(r, [{"id": self._line(r).id, "quantity": 5}], user=self.admin)
        refund_receipt(r, user=self.admin)
        self.assertStockConsistent("две правки, затем возврат")

    def test_edit_spanning_two_lots(self):
        """Правка, которая перешагивает границу партии: восстановление обязано
        вернуться в те же партии, из которых материал ушёл."""
        r = self._sale_area("18")  # больше одной партии (14.884 кв.м)
        self.assertStockConsistent("продажа через две партии")
        update_receipt_items(r, [{"id": self._line(r).id, "quantity": 4}], user=self.admin)
        self.assertStockConsistent("правка вниз через границу партии")

    # ---- инвентаризация и списание -----------------------------------------
    def test_inventory_count_down_moves_lots_too(self):
        """Инвентаризация нашла МЕНЬШЕ, чем числилось.

        Раньше правился только `Material.quantity`, а партии оставались как
        были: остаток и партии расходились, и дальше себестоимость по FIFO
        считалась по материалу, которого на складе уже нет. Списание брака это
        делало правильно, инвентаризация — нет.
        """
        self.client.force_authenticate(self.admin)
        self.mat.refresh_from_db()
        # Форма инвентаризации принимает два знака — считают в целых листах,
        # а не в долях квадрата.
        counted = (self.mat.quantity - Decimal("5")).quantize(Decimal("0.01"))
        resp = self.client.post(
            "/api/warehouse/materials/adjust/",
            {"material": self.mat.id, "counted_quantity": str(counted)},
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, counted)
        self.assertStockConsistent("после инвентаризации в минус")

    def test_inventory_count_up_moves_lots_too(self):
        """Нашли БОЛЬШЕ, чем числилось: излишек возвращается в партии."""
        self.client.force_authenticate(self.admin)
        self._sale_area("10")  # сначала израсходуем, чтобы партиям было куда расти
        self.mat.refresh_from_db()
        counted = (self.mat.quantity + Decimal("3")).quantize(Decimal("0.01"))
        resp = self.client.post(
            "/api/warehouse/materials/adjust/",
            {"material": self.mat.id, "counted_quantity": str(counted)},
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, counted)
        self.assertStockConsistent("после инвентаризации в плюс")

    def test_write_off_moves_lots_too(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post(
            "/api/warehouse/materials/write-off/",
            {"material": self.mat.id, "quantity": "4", "reason_code": "DEFECT"},
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertStockConsistent("после списания брака")

    def test_cut_receipt_with_service_line(self):
        """Резка: строка работы плюс строка материала. Правка материала не должна
        сбивать склад из-за соседней строки услуги."""
        r = create_sale(
            client=self.client_obj, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "SERVICE", "service": self.cutting, "material": self.mat,
                         "width": "2", "length": "3", "running_meters": "10"}],
            amount_paid=None,
        )
        self.assertStockConsistent("после резки")
        line = r.items.filter(type=TransactionItem.Type.MATERIAL).first()
        update_receipt_items(r, [{"id": line.id, "quantity": 2}], user=self.admin)
        self.assertStockConsistent("после правки резаного куска")
        delete_receipt(r, user=self.admin)
        self.assertStockConsistent("после удаления чека с резкой")
