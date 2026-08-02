"""Журнал движений склада: пишется ли каждое движение и сходится ли лента.

Раньше продажи в InventoryLog не попадали вовсе, а сам эндпоинт журнала падал
с 500 (сортировка по полю, которого у модели нет). Ни одного экрана поверх него
не было, поэтому не замечали ни того, ни другого.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales import sale_service
from sales.models import Receipt, TransactionItem
from warehouse.models import InventoryLog, Material, Roll
from warehouse.rolls import receive_lot


class StockJournalTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="journal_admin", password="x", role=User.Role.ADMIN
        )
        self.customer = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Покупатель", phone="+996700111"
        )
        # Рулонный (площадь, FIFO по партиям) и штучный — они ходят по разным
        # веткам списания, и раньше молчали обе.
        self.sheet = Material.objects.create(
            name="Форекс 3мм", unit=Material.Unit.SQM,
            is_roll_material=True, quantity=Decimal("0"),
            price_per_sqm=Decimal("500"), piece_price=Decimal("1500"),
            piece_area=Decimal("2.9768"),
        )
        receive_lot(
            self.sheet, form=Roll.Form.SHEET, width=Decimal("1.22"),
            height=Decimal("2.44"), sheet_count=10,
            purchase_cost=Decimal("10000"), user=self.admin,
        )
        self.sheet.refresh_from_db()
        self.piece = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE,
            quantity=Decimal("100"), price_per_unit=Decimal("30"),
            purchase_price=Decimal("12"),
        )

    def _sell(self, entries, **kwargs):
        return sale_service.create_sale(
            client=self.customer, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=entries, **kwargs,
        )

    def _logs(self, log_type):
        return InventoryLog.objects.filter(type=log_type)

    def test_roll_sale_writes_log_with_receipt(self):
        receipt = self._sell([{
            "type": TransactionItem.Type.MATERIAL, "material": self.sheet,
            "mode": TransactionItem.SaleMode.SQM, "quantity": Decimal("5"),
        }], title="Вывеска")
        log = self._logs(InventoryLog.Type.SALE).get()
        self.assertEqual(log.material, self.sheet)
        self.assertEqual(log.quantity_changed, Decimal("-5.0000"))
        self.assertEqual(log.receipt, receipt)
        self.assertIn(f"№{receipt.order_number}", log.reason)
        self.assertIn("Вывеска", log.reason)

    def test_piece_sale_writes_log(self):
        self._sell([{
            "type": TransactionItem.Type.MATERIAL, "material": self.piece,
            "mode": TransactionItem.SaleMode.PIECE, "quantity": Decimal("4"),
        }])
        log = self._logs(InventoryLog.Type.SALE).get()
        self.assertEqual(log.quantity_changed, Decimal("-4.0000"))

    def test_sale_by_piece_logs_area_for_roll_material(self):
        """Лист продан штукой — со склада уходит его площадь, она же в журнале."""
        self._sell([{
            "type": TransactionItem.Type.MATERIAL, "material": self.sheet,
            "mode": TransactionItem.SaleMode.PIECE, "quantity": Decimal("2"),
        }])
        log = self._logs(InventoryLog.Type.SALE).get()
        self.assertEqual(log.quantity_changed, Decimal("-5.9536"))

    def test_refund_pairs_every_sale_both_kinds(self):
        """У каждого расхода есть парный приход — и у рулонного, и у штучного.

        Именно здесь журнал не сходился: возврат штучного писался, рулонного —
        нет, и в ленте висел приход без расхода.
        """
        receipt = self._sell([
            {"type": TransactionItem.Type.MATERIAL, "material": self.sheet,
             "mode": TransactionItem.SaleMode.SQM, "quantity": Decimal("3")},
            {"type": TransactionItem.Type.MATERIAL, "material": self.piece,
             "mode": TransactionItem.SaleMode.PIECE, "quantity": Decimal("10")},
        ])
        sale_service.refund_receipt(receipt, user=self.admin)

        self.assertEqual(self._logs(InventoryLog.Type.SALE).count(), 2)
        returns = self._logs(InventoryLog.Type.RETURN)
        self.assertEqual(returns.count(), 2)
        self.assertEqual({log.material_id for log in returns}, {self.sheet.id, self.piece.id})
        self.assertEqual(sum(log.quantity_changed for log in returns), Decimal("13.0000"))
        # Возврат — не «корректировка»: инвентаризацию в ленте видно отдельно.
        self.assertFalse(self._logs(InventoryLog.Type.ADJUSTMENT).exists())

    def test_unpaid_online_order_writes_nothing(self):
        """Онлайн-заказ до оплаты склад не трогает — и в журнал не пишет."""
        receipt = sale_service.create_sale(
            client=self.customer, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.ONLINE,
            items_data=[{
                "type": TransactionItem.Type.MATERIAL, "material": self.piece,
                "mode": TransactionItem.SaleMode.PIECE, "quantity": Decimal("3"),
            }],
        )
        self.assertFalse(self._logs(InventoryLog.Type.SALE).exists())
        sale_service.confirm_payment(receipt)
        self.assertEqual(self._logs(InventoryLog.Type.SALE).count(), 1)

    def test_journal_endpoint_lists_and_filters(self):
        self.client.force_authenticate(self.admin)
        self._sell([{
            "type": TransactionItem.Type.MATERIAL, "material": self.piece,
            "mode": TransactionItem.SaleMode.PIECE, "quantity": Decimal("2"),
        }])
        r = self.client.get("/api/warehouse/inventory-logs/")
        self.assertEqual(r.status_code, 200, r.data)
        # Приход партии + продажа.
        self.assertEqual(r.data["count"], 2)
        # Свежее сверху.
        self.assertEqual(r.data["results"][0]["type"], "SALE")
        self.assertEqual(r.data["results"][0]["order_number"], 1)

        by_type = self.client.get("/api/warehouse/inventory-logs/?type=SALE")
        self.assertEqual(by_type.data["count"], 1)
        by_material = self.client.get(
            f"/api/warehouse/inventory-logs/?material={self.sheet.id}"
        )
        self.assertEqual(by_material.data["count"], 1)
        self.assertEqual(by_material.data["results"][0]["type"], "SUPPLY")

    def test_journal_month_filter_and_garbage_params(self):
        self.client.force_authenticate(self.admin)
        log = self._logs(InventoryLog.Type.SUPPLY).get()
        year, month = log.happened_at.year, log.happened_at.month
        hit = self.client.get(f"/api/warehouse/inventory-logs/?year={year}&month={month}")
        self.assertEqual(hit.data["count"], 1)
        other = 1 if month != 1 else 2
        miss = self.client.get(f"/api/warehouse/inventory-logs/?year={year}&month={other}")
        self.assertEqual(miss.data["count"], 0)
        # Мусор в параметрах не должен ронять ленту.
        junk = self.client.get("/api/warehouse/inventory-logs/?year=абв&month=")
        self.assertEqual(junk.status_code, 200)
        self.assertEqual(junk.data["count"], 1)

    def test_page_size_param_returns_whole_catalogue(self):
        """Выпадающий список материалов не должен обрезаться страницей в 25."""
        Material.objects.bulk_create([
            Material(name=f"Материал {i}", unit=Material.Unit.PIECE,
                     quantity=Decimal("1"), price_per_unit=Decimal("10"))
            for i in range(30)
        ])
        self.client.force_authenticate(self.admin)
        default = self.client.get("/api/warehouse/materials/")
        self.assertEqual(len(default.data["results"]), 25)
        whole = self.client.get("/api/warehouse/materials/?page_size=200")
        self.assertEqual(len(whole.data["results"]), 32)
