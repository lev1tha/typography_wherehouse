"""Партию можно выбрать не только у рулона, но и у ЛИСТА.

У листового материала партии тоже есть — пачки с разной закупочной ценой, — но
списание всегда шло строго FIFO, и мастер, взявший лист из новой пачки, ничего
не мог об этом сказать: чек считал себестоимость по старейшей.

Выбранная партия встаёт первой, остальные идут за ней обычным порядком: если в
ней не хватило, добираем со следующей — так пачка и кончается посреди заказа.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import TransactionItem
from sales.sale_service import create_sale
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot


class SheetLotChoiceTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="slc_admin", password="x", role=User.Role.ADMIN
        )
        self.mat = Material.objects.create(
            name="Белый акрил", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET,
            sheet_width=Decimal("1"), sheet_height=Decimal("2"),
            price_per_sqm=Decimal("1000"), piece_price=Decimal("2000"),
        )
        # Две пачки по 5 листов (по 2 кв.м каждый): старая по 100, новая по 300.
        self.old = receive_lot(
            self.mat, form="SHEET", width=Decimal("1"), height=Decimal("2"),
            sheet_count=5, purchase_cost=Decimal("1000"), code="старая",
        )
        self.new = receive_lot(
            self.mat, form="SHEET", width=Decimal("1"), height=Decimal("2"),
            sheet_count=5, purchase_cost=Decimal("3000"), code="новая",
        )

    def _sell(self, *, area="2", roll=None):
        item = {
            "type": "MATERIAL", "material": self.mat,
            "quantity": Decimal(area), "mode": "SQM",
        }
        if roll is not None:
            item["roll"] = roll
        return create_sale(
            client=None, cashier=self.admin, payment_method="CASH",
            items_data=[item], amount_paid=Decimal("0"),
        )

    def test_without_a_choice_the_oldest_pack_goes_first(self):
        receipt = self._sell(area="2")
        self.assertEqual(receipt.items.first().cost_total, Decimal("200.00"))
        self.old.refresh_from_db()
        self.assertEqual(self.old.remaining_area, Decimal("8.0000"))

    def test_chosen_pack_is_the_one_that_gets_cut(self):
        receipt = self._sell(area="2", roll=self.new)
        self.assertEqual(receipt.items.first().cost_total, Decimal("600.00"))
        self.new.refresh_from_db()
        self.old.refresh_from_db()
        self.assertEqual(self.new.remaining_area, Decimal("8.0000"))
        self.assertEqual(self.old.remaining_area, Decimal("10.0000"))

    def test_the_line_remembers_the_pack_even_when_nobody_picked_one(self):
        """Иначе возврат вернул бы листы не в ту пачку, а себестоимость строки
        разошлась бы с той, по которой продали."""
        receipt = self._sell(area="2")
        self.assertEqual(receipt.items.first().roll_id, self.old.id)

    def test_short_pack_is_topped_up_from_the_next_one(self):
        """В выбранной пачке 10 кв.м, продаём 12 — четыре кв.м доберутся из
        соседней: 10 × 300 + 2 × 100 = 3200."""
        receipt = self._sell(area="12", roll=self.new)
        self.assertEqual(receipt.items.first().cost_total, Decimal("3200.00"))
        self.new.refresh_from_db()
        self.old.refresh_from_db()
        self.assertEqual(self.new.remaining_area, Decimal("0.0000"))
        self.assertEqual(self.old.remaining_area, Decimal("8.0000"))

    def test_refund_goes_back_into_the_pack_it_was_cut_from(self):
        receipt = self._sell(area="2", roll=self.new)
        from sales.sale_service import refund_receipt

        refund_receipt(receipt, user=self.admin)
        self.new.refresh_from_db()
        self.old.refresh_from_db()
        self.assertEqual(self.new.remaining_area, Decimal("10.0000"))
        self.assertEqual(self.old.remaining_area, Decimal("10.0000"))

    def test_whole_sheet_sale_also_honours_the_choice(self):
        """Продажа целым листом списывает площадь листа — из выбранной пачки."""
        receipt = create_sale(
            client=None, cashier=self.admin, payment_method="CASH",
            items_data=[{
                "type": "MATERIAL", "material": self.mat,
                "quantity": Decimal("1"), "mode": "PIECE", "roll": self.new,
            }],
            amount_paid=Decimal("0"),
        )
        self.assertEqual(receipt.items.first().cost_total, Decimal("600.00"))
        self.new.refresh_from_db()
        self.assertEqual(self.new.remaining_area, Decimal("8.0000"))
