"""Возврат строки, проданной из ДВУХ партий, возвращает каждой партии её долю.

Строка помнила только первую партию (`TransactionItem.roll`), и возврат целиком
ложился в неё. Пример аудита: партия A — 2 шт по 1000, партия B — 1 шт за 3000.
Продали 1 шт (из A), потом 2 шт (A + B, себестоимость 4000), вернули вторую:
ждали A=1, B=1 и склад на 4000, получали A=2, B=0, склад на 2000 — и следующие
продажи шли по 1000 вместо 3000. Теперь на строке записано, сколько из какой
партии взято (`TransactionItemLot`), и возврат идёт по этой записи.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt, TransactionItemLot
from sales.sale_service import create_sale, refund_receipt, update_receipt_items
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot


class ReturnToTheSameLotsTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="rl_boss", password="x", role=User.Role.ADMIN
        )
        self.mat = Material.objects.create(
            name="Диод", unit=Material.Unit.PIECE, price_per_unit=Decimal("5000"),
            piece_price=Decimal("5000"),
        )
        now = timezone.now()
        self.a = receive_lot(
            self.mat, form=Roll.Form.PIECE, purchase_cost=Decimal("2000"),
            sheet_count=2, received_at=now - timedelta(days=2), user=self.admin,
        )
        self.b = receive_lot(
            self.mat, form=Roll.Form.PIECE, purchase_cost=Decimal("3000"),
            sheet_count=1, received_at=now - timedelta(days=1), user=self.admin,
        )

    def _sell(self, qty):
        return create_sale(
            client=None, cashier=self.admin, payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": self.mat, "quantity": qty}],
            pay_full=True,
        )

    def _lots(self):
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        return self.a.remaining_area, self.b.remaining_area

    def test_return_of_a_two_lot_line_restores_both_lots(self):
        self._sell(1)
        second = self._sell(2)
        self.assertEqual(second.items.get().cost_total, Decimal("4000"))
        self.assertEqual(self._lots(), (Decimal("0"), Decimal("0")))

        refund_receipt(second, user=self.admin)

        self.assertEqual(self._lots(), (Decimal("1"), Decimal("1")))
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("2"))
        # Следующая продажа из двух снова стоит 1000 + 3000, а не 1000 + 1000.
        self.assertEqual(self.mat.stock_value, Decimal("4000"))
        third = self._sell(2)
        self.assertEqual(third.items.get().cost_total, Decimal("4000"))

    def test_lot_uses_are_recorded_per_line(self):
        self._sell(1)
        second = self._sell(2)
        item = second.items.get()
        uses = {u.roll_id: u.area for u in item.lot_uses.all()}
        self.assertEqual(uses, {self.a.pk: Decimal("1"), self.b.pk: Decimal("1")})

    def test_line_without_records_keeps_the_old_behaviour(self):
        """Строка, проданная до учёта партий: записей нет — возврат как раньше."""
        self._sell(1)
        second = self._sell(2)
        TransactionItemLot.objects.all().delete()
        refund_receipt(second, user=self.admin)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("2"))

    def test_editing_quantity_rewrites_the_lot_records(self):
        self._sell(1)
        second = self._sell(2)
        item = second.items.get()
        update_receipt_items(second, [{"id": item.id, "quantity": "1"}], user=self.admin)
        item.refresh_from_db()
        # Одна штука — из A (старейшая партия с остатком).
        self.assertEqual(self._lots(), (Decimal("0"), Decimal("1")))
        self.assertEqual(
            {u.roll_id: u.area for u in item.lot_uses.all()}, {self.a.pk: Decimal("1")}
        )

    def test_fifo_is_deterministic_for_lots_received_at_the_same_moment(self):
        """Равный `received_at` — порядок по номеру партии, не по прихоти базы."""
        mat = Material.objects.create(
            name="Шуруп", unit=Material.Unit.PIECE, price_per_unit=Decimal("10"),
            piece_price=Decimal("10"),
        )
        moment = timezone.now()
        first = receive_lot(mat, form=Roll.Form.PIECE, purchase_cost=Decimal("100"),
                            sheet_count=5, received_at=moment, user=self.admin)
        receive_lot(mat, form=Roll.Form.PIECE, purchase_cost=Decimal("500"),
                    sheet_count=5, received_at=moment, user=self.admin)
        sale = create_sale(
            client=None, cashier=self.admin, payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": mat, "quantity": 3}],
            pay_full=True,
        )
        first.refresh_from_db()
        self.assertEqual(first.remaining_area, Decimal("2"))
        self.assertEqual(sale.items.get().cost_total, Decimal("60"))


class ReturnMetresToTheSameRollsTests(APITestCase):
    """Рулон, проданный метрами с двух рулонов, возвращается каждому его метры."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="rlm_boss", password="x", role=User.Role.ADMIN
        )
        self.mat = Material.objects.create(
            name="Туника", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL, roll_width=Decimal("1"),
            price_per_pm=Decimal("300"),
        )
        now = timezone.now()
        self.r1 = receive_lot(
            self.mat, form=Roll.Form.ROLL, purchase_cost=Decimal("400"),
            width=Decimal("1"), length=Decimal("2"), received_at=now - timedelta(days=2),
            user=self.admin,
        )
        self.r2 = receive_lot(
            self.mat, form=Roll.Form.ROLL, purchase_cost=Decimal("1200"),
            width=Decimal("1"), length=Decimal("2"), received_at=now - timedelta(days=1),
            user=self.admin,
        )

    def _sell(self, metres):
        return create_sale(
            client=None, cashier=self.admin, payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": self.mat, "mode": "METER",
                         "quantity": Decimal(metres)}],
            pay_full=True,
        )

    def test_metres_return_to_both_rolls(self):
        self._sell("1")                 # 1 м с первого рулона
        second = self._sell("2")        # 1 м с первого (остаток) + 1 м со второго
        self.r1.refresh_from_db(); self.r2.refresh_from_db()
        self.assertEqual((self.r1.remaining_area, self.r2.remaining_area),
                         (Decimal("0"), Decimal("1")))
        refund_receipt(second, user=self.admin)
        self.r1.refresh_from_db(); self.r2.refresh_from_db()
        # Каждому рулону — его метр; раньше оба шли в первый (он «выбранный»).
        self.assertEqual((self.r1.remaining_area, self.r2.remaining_area),
                         (Decimal("1"), Decimal("2")))
