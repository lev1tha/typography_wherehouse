"""Правка СОСТАВА чека: количество, цена, лишняя строка.

Правки «наименование / клиент / дата» оказалось мало: ошибаются в сумме, в
количестве листов и квадратных метров. Удалять весь чек и заводить заново
разумно ровно до момента, когда по заказу уже прошли оплаты, а неверна одна
строка из десяти.

Строку правим ЧЕРЕЗ СКЛАД, а не арифметикой по полям: возвращаем списанное,
применяем правку, списываем заново. Тогда себестоимость пересчитывается сама
(для рулонных — по партиям FIFO), а остаток не расходится с журналом.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from clients.models import Client
from sales.models import Receipt, TransactionItem
from sales.sale_service import create_sale
from services.models import PrintingService
from warehouse.models import InventoryLog, Material


class EditItemsTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="boss_ei", password="x", role=User.Role.ADMIN
        )
        self.keeper = User.objects.create_user(
            username="skl_ei", password="x", role=User.Role.STOREKEEPER
        )
        self.client_obj = Client.objects.create(full_name="Тахир", phone="+996555111222")
        self.bolts = Material.objects.create(
            name="Крепёж",
            unit=Material.Unit.PIECE,
            quantity=Decimal("1000"),
            price_per_unit=Decimal("150"),
            piece_price=Decimal("150"),
            purchase_price=Decimal("80"),
        )
        self.roll = Material.objects.create(
            name="Акрил рулон",
            unit=Material.Unit.SQM,
            quantity=Decimal("500"),
            is_roll_material=True,
            price_per_sqm=Decimal("1000"),
            purchase_price=Decimal("600"),
            cut_rate_per_pm=Decimal("50"),
        )
        self.cutting = PrintingService.objects.create(
            name="Резка", kind=PrintingService.Kind.CUTTING,
            machine=PrintingService.Machine.CNC,
        )

    def _sale(self, *, qty=10, paid=None):
        return create_sale(
            client=self.client_obj,
            cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": self.bolts, "quantity": qty}],
            amount_paid=paid,
        )

    def _edit(self, receipt, items):
        self.client.force_authenticate(self.admin)
        return self.client.post(
            f"/api/sales/receipts/{receipt.id}/edit-items/", {"items": items}, format="json"
        )

    def _line(self, receipt):
        return receipt.items.filter(type=TransactionItem.Type.MATERIAL).first()

    # ---- количество --------------------------------------------------------
    def test_lower_quantity_returns_the_difference_to_stock(self):
        """Написали 10 штук вместо 7 — три должны вернуться на склад."""
        before = self.bolts.quantity
        r = self._sale(qty=10, paid=Decimal("1500"))
        self.bolts.refresh_from_db()
        self.assertEqual(self.bolts.quantity, before - 10)

        resp = self._edit(r, [{"id": self._line(r).id, "quantity": 7}])
        self.assertEqual(resp.status_code, 200, resp.data)
        self.bolts.refresh_from_db()
        self.assertEqual(self.bolts.quantity, before - 7)
        r.refresh_from_db()
        self.assertEqual(r.total_price, Decimal("1050"))

    def test_higher_quantity_takes_more_from_stock(self):
        before = self.bolts.quantity
        r = self._sale(qty=5, paid=None)
        self._edit(r, [{"id": self._line(r).id, "quantity": 8}])
        self.bolts.refresh_from_db()
        self.assertEqual(self.bolts.quantity, before - 8)

    def test_quantity_beyond_stock_is_rejected_whole(self):
        """Правка не проходит частично: не хватило остатка — откатывается всё."""
        r = self._sale(qty=5, paid=None)
        before_total = r.total_price
        resp = self._edit(r, [{"id": self._line(r).id, "quantity": 100000}])
        self.assertEqual(resp.status_code, 400)
        r.refresh_from_db()
        self.assertEqual(r.total_price, before_total)

    def test_zero_quantity_is_rejected_as_a_typo(self):
        """Ноль — это удаление строки, и оно делается явным флагом."""
        r = self._sale(qty=5, paid=None)
        resp = self._edit(r, [{"id": self._line(r).id, "quantity": 0}])
        self.assertEqual(resp.status_code, 400)

    # ---- цена --------------------------------------------------------------
    def test_price_edit_changes_total_but_not_stock(self):
        before = self.bolts.quantity
        r = self._sale(qty=10, paid=None)
        self._edit(r, [{"id": self._line(r).id, "price_per_item": 200}])
        r.refresh_from_db()
        self.assertEqual(r.total_price, Decimal("2000"))
        self.bolts.refresh_from_db()
        self.assertEqual(self.bolts.quantity, before - 10)

    # ---- удаление строки ---------------------------------------------------
    def test_removing_a_line_returns_its_material(self):
        before = self.bolts.quantity
        r = create_sale(
            client=self.client_obj,
            cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {"type": "MATERIAL", "material": self.bolts, "quantity": 4},
                {"type": "MATERIAL", "material": self.roll, "quantity": 2, "mode": "SQM"},
            ],
            amount_paid=None,
        )
        line = r.items.filter(material=self.bolts).first()
        self._edit(r, [{"id": line.id, "remove": True}])
        self.bolts.refresh_from_db()
        self.assertEqual(self.bolts.quantity, before)
        r.refresh_from_db()
        self.assertEqual(r.items.count(), 1)

    # ---- себестоимость -----------------------------------------------------
    def test_cost_follows_the_new_quantity(self):
        """Себестоимость снимается заново при списании — значит она должна
        соответствовать исправленному количеству, а не прежнему."""
        r = self._sale(qty=10, paid=None)
        self.assertEqual(r.cost_total, Decimal("800"))  # 10 × 80
        self._edit(r, [{"id": self._line(r).id, "quantity": 4}])
        r.refresh_from_db()
        self.assertEqual(r.cost_total, Decimal("320"))  # 4 × 80

    # ---- журнал склада -----------------------------------------------------
    def test_journal_keeps_one_sale_line_not_a_return_pair(self):
        """Исправление опечатки — не возврат от клиента. В ленте движений должна
        остаться одна продажа с исправленным количеством."""
        r = self._sale(qty=10, paid=None)
        self._edit(r, [{"id": self._line(r).id, "quantity": 3}])
        logs = list(r.inventory_logs.all())
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].type, InventoryLog.Type.SALE)
        self.assertEqual(logs[0].quantity_changed, Decimal("-3"))

    def test_journal_totals_stay_right_with_the_same_material_twice(self):
        """Один материал двумя строками — журнал не должен потерять вторую."""
        r = create_sale(
            client=self.client_obj,
            cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {"type": "MATERIAL", "material": self.bolts, "quantity": 3},
                {"type": "MATERIAL", "material": self.bolts, "quantity": 5},
            ],
            amount_paid=None,
        )
        first = r.items.order_by("id").first()
        self._edit(r, [{"id": first.id, "quantity": 4}])
        moved = sum(
            (-l.quantity_changed for l in r.inventory_logs.filter(type=InventoryLog.Type.SALE)),
            Decimal("0"),
        )
        self.assertEqual(moved, Decimal("9"))  # 4 + 5

    # ---- деньги ------------------------------------------------------------
    def test_lowering_total_below_paid_turns_the_excess_into_change(self):
        """Тот самый случай: написали лишнее, клиент заплатил по завышенному
        счёту, ошибку нашли. Переплата не исчезает — она становится сдачей."""
        r = self._sale(qty=10, paid=Decimal("1500"))
        self._edit(r, [{"id": self._line(r).id, "quantity": 6}])
        r.refresh_from_db()
        self.assertEqual(r.total_price, Decimal("900"))
        self.assertEqual(r.amount_paid, Decimal("900"))
        self.assertEqual(r.change_due, Decimal("600"))
        self.assertEqual(r.debt, Decimal("0"))

    def test_raising_total_reopens_the_debt(self):
        r = self._sale(qty=5, paid=Decimal("750"))
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.PAID)
        self._edit(r, [{"id": self._line(r).id, "quantity": 8}])
        r.refresh_from_db()
        self.assertEqual(r.total_price, Decimal("1200"))
        self.assertEqual(r.debt, Decimal("450"))
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.PENDING)

    # ---- права и границы ---------------------------------------------------
    def test_storekeeper_cannot_edit_items(self):
        r = self._sale(qty=5, paid=None)
        self.client.force_authenticate(self.keeper)
        resp = self.client.post(
            f"/api/sales/receipts/{r.id}/edit-items/",
            {"items": [{"id": self._line(r).id, "quantity": 2}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_returned_line_cannot_be_edited(self):
        r = self._sale(qty=5, paid=Decimal("750"))
        self.client.force_authenticate(self.admin)
        self.client.post(f"/api/sales/receipts/{r.id}/refund/", {}, format="json")
        line = r.items.first()
        resp = self._edit(r, [{"id": line.id, "quantity": 2}])
        self.assertEqual(resp.status_code, 400)

    def test_edit_is_written_to_the_action_log(self):
        r = self._sale(qty=10, paid=None)
        self._edit(r, [{"id": self._line(r).id, "quantity": 3}])
        entry = AuditLog.objects.order_by("-id").first()
        self.assertIn("Правка состава чека", entry.action)
        self.assertIn("было", entry.action)
