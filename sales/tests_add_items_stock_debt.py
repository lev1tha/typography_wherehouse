"""Дозаказ: склад и доплата (аудит 2026-08-18, пункт 1).

Две дыры в `add_items_to_receipt`, обе видны только на числах:

- списание новых строк держалось на СТАТУСЕ ОПЛАТЫ, а не на том, уходил ли
  склад по чеку. Наличный заказ в долг (PENDING, `stock_deducted=True`) при
  дозаказе не списывал материал вовсе: лист ушёл с полки, остаток не изменился,
  себестоимость строки 0, а возврат такого чека клал на склад два листа вместо
  одного;
- доплата по оплаченному заказу не становилась долгом: статус оставался PAID,
  `debt` = 0, «Принять оплату» отвечала «долга нет», касса не росла — 3 700 сом
  исчезали из всех отчётов разом.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from finance.models import CashEntry
from sales.models import Receipt, TransactionItem
from sales.sale_service import create_sale, refund_receipt
from warehouse.models import InventoryLog, Material, Roll
from warehouse.rolls import receive_lot


class AddItemsStockAndDebtTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="ai_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="ai_store", password="x", role=User.Role.STOREKEEPER)
        self.client_obj = Client.objects.create(full_name="Азат", phone="+996772009031")
        # Листовой материал с партией: FIFO даёт настоящую себестоимость.
        self.acrylic = Material.objects.create(
            name="Белый акрил", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
            price_per_sqm=Decimal("1250"), piece_price=Decimal("3700"),
        )
        receive_lot(
            self.acrylic, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
            sheet_count=Decimal("5"), purchase_cost=Decimal("17500"), user=self.admin,
        )
        self.acrylic.refresh_from_db()
        self.sheet = self.acrylic.piece_area  # 2.9768

    def _sale(self, *, paid, user=None):
        """Один лист за 3 700 наличными; `paid=None` — весь заказ в долг."""
        return create_sale(
            client=self.client_obj, cashier=user or self.store,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": self.acrylic, "quantity": 1, "mode": "PIECE"}],
            amount_paid=paid,
        )

    def _add_sheet(self, receipt, user=None):
        self.client.force_authenticate(user or self.store)
        return self.client.post(
            f"/api/sales/receipts/{receipt.id}/add-items/",
            {"items": [{"type": "MATERIAL", "material": self.acrylic.id, "quantity": 1, "mode": "PIECE"}]},
            format="json",
        )

    def _stock(self):
        self.acrylic.refresh_from_db()
        return self.acrylic.quantity

    # --- склад -------------------------------------------------------------

    def test_add_items_to_cash_order_in_debt_deducts_stock(self):
        receipt = self._sale(paid=None)  # PENDING, склад списан при оформлении
        after_sale = self._stock()
        self.assertEqual(after_sale, Decimal("5") * self.sheet - self.sheet)

        r = self._add_sheet(receipt)
        self.assertEqual(r.status_code, 200, r.data)

        # Второй лист ушёл со склада сразу — как и первый.
        self.assertEqual(self._stock(), after_sale - self.sheet)
        added = receipt.items.order_by("id").last()
        self.assertGreater(added.cost_total, Decimal("0"))  # себестоимость по партии
        self.assertEqual(
            InventoryLog.objects.filter(receipt=receipt, type=InventoryLog.Type.SALE).count(), 2
        )
        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PENDING)
        self.assertEqual(receipt.debt, Decimal("7400"))

    def test_refund_after_add_items_returns_exactly_what_left(self):
        """Возврат кладёт на склад ровно два списанных листа, а не три."""
        before_all = self._stock()
        receipt = self._sale(paid=None)
        self._add_sheet(receipt)
        self.assertEqual(self._stock(), before_all - 2 * self.sheet)

        refund_receipt(receipt, user=self.admin)
        self.assertEqual(self._stock(), before_all)

    def test_add_items_to_unconfirmed_online_order_waits_for_gateway(self):
        """Онлайн-счёт без подтверждения склад не трогал — и дозаказ ждёт."""
        receipt = create_sale(
            client=self.client_obj, cashier=self.store,
            payment_method=Receipt.PaymentMethod.ONLINE,
            items_data=[{"type": "MATERIAL", "material": self.acrylic, "quantity": 1, "mode": "PIECE"}],
        )
        stock = self._stock()
        r = self._add_sheet(receipt)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self._stock(), stock)
        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PENDING)

    # --- деньги ------------------------------------------------------------

    def test_surcharge_on_paid_order_becomes_debt(self):
        receipt = self._sale(paid=Decimal("3700"))
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PAID)

        r = self._add_sheet(receipt)
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.total_price, Decimal("7400"))
        self.assertEqual(receipt.amount_paid, Decimal("3700"))
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PENDING)
        self.assertEqual(receipt.debt, Decimal("3700"))
        self.assertEqual(Decimal(str(r.data["debt"])), Decimal("3700"))

        # Доплату принимают как обычный долг — и она доходит до кассы.
        self.client.force_authenticate(self.admin)
        cash_before = CashEntry.balance()
        r = self.client.post(f"/api/sales/receipts/{receipt.id}/pay/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PAID)
        self.assertEqual(receipt.debt, Decimal("0"))
        self.assertEqual(CashEntry.balance() - cash_before, Decimal("3700"))

    def test_finance_report_adds_up_after_surcharge(self):
        """Выручка = на руках + долг: 7 400 = 3 700 + 3 700, а не 3 700 + 0."""
        receipt = self._sale(paid=Decimal("3700"))
        self._add_sheet(receipt)
        self.client.force_authenticate(self.admin)
        r = self.client.get("/api/finance/report/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Decimal(str(r.data["revenue"])), Decimal("7400"))
        self.assertEqual(Decimal(str(r.data["revenue_paid"])), Decimal("3700"))
        self.assertEqual(Decimal(str(r.data["client_debt"])), Decimal("3700"))

    def test_partially_refunded_receipt_keeps_its_status_and_counts_surcharge(self):
        receipt = create_sale(
            client=self.client_obj, cashier=self.store, payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {"type": "MATERIAL", "material": self.acrylic, "quantity": 1, "mode": "PIECE"},
                {"type": "MATERIAL", "material": self.acrylic, "quantity": 1, "mode": "PIECE"},
            ],
            pay_full=True,
        )
        first = receipt.items.order_by("id").first()
        refund_receipt(receipt, item_ids=[first.id], user=self.admin)
        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PARTIALLY_REFUNDED)
        self.assertEqual(receipt.debt, Decimal("0"))

        stock = self._stock()
        r = self._add_sheet(receipt)
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        # Статус «частичный возврат» остаётся — он в OWING_STATUSES, долг виден.
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PARTIALLY_REFUNDED)
        self.assertEqual(receipt.debt, Decimal("3700"))
        self.assertEqual(self._stock(), stock - self.sheet)

    def test_edit_after_partial_refund_does_not_refund_the_same_money_twice(self):
        """Правка состава чека с частичным возвратом считает от денег НА РУКАХ.

        Клиент принёс 7 400 за два листа, один вернул — 3 700 ему отдали из
        кассы. Потом второй лист уценили до 2 000: сдача — 1 700 (3 700 − 2 000),
        а не 5 400 (7 400 − 2 000): те 3 700 уже выданы.
        """
        receipt = create_sale(
            client=self.client_obj, cashier=self.store, payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {"type": "MATERIAL", "material": self.acrylic, "quantity": 1, "mode": "PIECE"},
                {"type": "MATERIAL", "material": self.acrylic, "quantity": 1, "mode": "PIECE"},
            ],
            pay_full=True,
        )
        first, second = receipt.items.order_by("id")
        refund_receipt(receipt, item_ids=[first.id], user=self.admin)
        refunded_cash = sum(
            (e.amount for e in CashEntry.objects.filter(receipt=receipt, article=CashEntry.Article.REFUND)),
            Decimal("0"),
        )
        self.assertEqual(refunded_cash, Decimal("3700"))

        self.client.force_authenticate(self.admin)
        r = self.client.post(
            f"/api/sales/receipts/{receipt.id}/edit-items/",
            {"items": [{"id": second.id, "price_per_item": 2000}]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.change_due, Decimal("1700"))
        self.assertEqual(receipt.amount_paid, Decimal("2000"))
        self.assertEqual(receipt.debt, Decimal("0"))
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PARTIALLY_REFUNDED)

        # А если строку, наоборот, подняли до 5 000 — долг 3 000 (5 000 − 2 000 на
        # руках после уценки), никакой сдачи сверх уже записанной.
        r = self.client.post(
            f"/api/sales/receipts/{receipt.id}/edit-items/",
            {"items": [{"id": second.id, "price_per_item": 5000}]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.change_due, Decimal("1700"))
        self.assertEqual(receipt.debt, Decimal("3000"))
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PARTIALLY_REFUNDED)

    def test_storekeeper_sees_the_new_debt_in_receipt_response(self):
        receipt = self._sale(paid=Decimal("3700"))
        r = self._add_sheet(receipt, user=self.store)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["payment_status"], Receipt.PaymentStatus.PENDING)
        self.assertEqual(Decimal(str(r.data["debt"])), Decimal("3700"))
        # Себестоимость складовщику по-прежнему не показывается.
        self.assertIsNone(r.data["cost_total"])
        self.assertTrue(all(i["cost_total"] is None for i in r.data["items"]))
        self.assertEqual(
            TransactionItem.objects.filter(receipt=receipt, cost_total__gt=0).count(), 2
        )
