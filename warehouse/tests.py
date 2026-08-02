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
from warehouse.models import (
    InventoryLog,
    Material,
    MaterialType,
    ProductionSite,
    Roll,
)
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

    def test_bulk_creates_catalogue_and_derives_what_it_can(self):
        """Сетка массового ввода: имя, единица и площадь листа выводятся сами."""
        self.client.force_authenticate(self.admin)
        kind = MaterialType.objects.create(code="forex", name="Форекс")
        site = ProductionSite.objects.create(code="bishkek", name="Бишкек")
        r = self.client.post("/api/warehouse/materials/bulk/", {"rows": [
            # Справочники названием — так приходит вставка из Excel.
            {"type": "форекс", "color": "молочный", "thickness_mm": "8",
             "sheet_width": "1.22", "sheet_height": "2.44",
             "production": "бишкек", "piece_price": "4500", "price_per_sqm": "1600"},
            # Штучный расходник: размера нет, значит и в кв.м его не считают.
            {"name": "Скотч двусторонний", "price_per_unit": "180"},
        ]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["created"], 2)

        sheet = Material.objects.get(name="Форекс молочный 8 мм 1,22×2,44")
        self.assertTrue(sheet.is_roll_material)
        self.assertEqual(sheet.unit, Material.Unit.SQM)
        self.assertEqual(sheet.piece_area, Decimal("2.9768"))
        self.assertEqual(sheet.type, kind)
        self.assertEqual(sheet.production, site)

        piece = Material.objects.get(name="Скотч двусторонний")
        self.assertFalse(piece.is_roll_material)
        self.assertEqual(piece.unit, Material.Unit.PIECE)

    def test_bulk_is_all_or_nothing_and_reports_row_numbers(self):
        """Опечатка в одной строке не должна оставить полпачки в каталоге."""
        self.client.force_authenticate(self.admin)
        MaterialType.objects.create(code="forex2", name="Форекс")
        before = Material.objects.count()
        r = self.client.post("/api/warehouse/materials/bulk/", {"rows": [
            {"type": "Форекс", "color": "белый"},
            {"type": "Такого нет", "color": "синий"},
            {"color": ""},
            {"name": self.piece.name},
        ]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Material.objects.count(), before)
        errors = {item["row"]: item["fields"] for item in r.data["errors"]}
        self.assertEqual(set(errors), {1, 2, 3})
        self.assertIn("type", errors[1])
        self.assertIn("name", errors[2])
        self.assertIn("уже есть", str(errors[3]["name"]))

    def test_bulk_catches_duplicate_inside_the_batch(self):
        """Дубль внутри самой пачки в базе ещё не лежит — ловим отдельно."""
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/warehouse/materials/bulk/", {"rows": [
            {"name": "Плёнка матовая"},
            {"name": "плёнка матовая"},
        ]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["errors"][0]["row"], 1)
        self.assertFalse(Material.objects.filter(name__iexact="плёнка матовая").exists())

    def test_bulk_is_admin_only(self):
        keeper = User.objects.create_user(
            username="grid_keeper", password="x", role=User.Role.STOREKEEPER
        )
        self.client.force_authenticate(keeper)
        r = self.client.post("/api/warehouse/materials/bulk/", {"rows": [
            {"name": "Что-нибудь"},
        ]}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Material.objects.filter(name="Что-нибудь").exists())

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
