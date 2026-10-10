"""Правка состава чека: складской журнал обязан сходиться с остатком.

`update_receipt_items` возвращала на склад старое количество, списывала заново,
а потом «подчищала» журнал, подбирая старые записи по МАТЕРИАЛУ, а не по строке:
- удалили строку на 10 шт → остаток 1000, а в журнале осталось «−10» (Σ 990);
- чек 10 + 3 шт одного материала, вторую строку правим до 1 → остаток 989, а
  убрана запись «−10» вместо «−3» (Σ 996).
Теперь движения привязаны к строке (`InventoryLog.receipt_item`).
"""
from decimal import Decimal

from django.db.models import Sum
from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt
from sales.sale_service import create_sale, update_receipt_items
from warehouse.models import InventoryLog, Material
from warehouse.rolls import receive_lot
from warehouse.models import Roll


class EditJournalTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="ej_boss", password="x", role=User.Role.ADMIN
        )
        self.bolts = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("1000"),
            price_per_unit=Decimal("150"), piece_price=Decimal("150"),
            purchase_price=Decimal("80"),
        )

    def _sale(self, *qtys, material=None):
        material = material or self.bolts
        return create_sale(
            client=None, cashier=self.admin, payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {"type": "MATERIAL", "material": material, "quantity": q} for q in qtys
            ],
            pay_full=True,
        )

    def _journal(self, material=None):
        material = material or self.bolts
        total = InventoryLog.objects.filter(material=material).aggregate(
            s=Sum("quantity_changed")
        )["s"]
        return total or Decimal("0")

    def _stock(self, material=None):
        material = material or self.bolts
        material.refresh_from_db()
        return material.quantity

    def test_removing_a_line_leaves_no_trace_in_the_journal(self):
        r = self._sale(10)
        self.assertEqual(self._stock(), Decimal("990"))
        item = r.items.get()
        update_receipt_items(r, [{"id": item.id, "remove": True}], user=self.admin)
        self.assertEqual(self._stock(), Decimal("1000"))
        # Журнал сходится с остатком: движений по этому чеку не осталось.
        self.assertEqual(self._journal(), Decimal("0"))
        self.assertFalse(InventoryLog.objects.filter(receipt=r).exists())

    def test_editing_the_second_of_two_lines_of_one_material(self):
        r = self._sale(10, 3)
        first, second = r.items.order_by("id")
        update_receipt_items(r, [{"id": second.id, "quantity": "1"}], user=self.admin)
        self.assertEqual(self._stock(), Decimal("989"))
        self.assertEqual(self._journal(), Decimal("-11"))
        sales = sorted(
            InventoryLog.objects.filter(receipt=r, type=InventoryLog.Type.SALE)
            .values_list("quantity_changed", flat=True)
        )
        self.assertEqual(sales, [Decimal("-10"), Decimal("-1")])
        # И записи привязаны к своим строкам.
        self.assertEqual(
            InventoryLog.objects.get(receipt_item=first).quantity_changed, Decimal("-10")
        )
        self.assertEqual(
            InventoryLog.objects.get(receipt_item=second).quantity_changed, Decimal("-1")
        )

    def test_raising_a_quantity_keeps_one_record_per_line(self):
        r = self._sale(10, 3)
        first, _second = r.items.order_by("id")
        update_receipt_items(r, [{"id": first.id, "quantity": "12"}], user=self.admin)
        self.assertEqual(self._stock(), Decimal("985"))
        self.assertEqual(self._journal(), Decimal("-15"))
        self.assertEqual(
            InventoryLog.objects.filter(receipt=r, type=InventoryLog.Type.RETURN).count(), 0
        )

    def test_old_unlinked_records_are_matched_by_quantity(self):
        """Чек, проведённый до привязки журнала к строкам: записи без ссылки на
        строку. Убираем ту, что совпадает по количеству, а не первую по порядку."""
        r = self._sale(10, 3)
        InventoryLog.objects.filter(receipt=r).update(receipt_item=None)
        _first, second = r.items.order_by("id")
        update_receipt_items(r, [{"id": second.id, "quantity": "1"}], user=self.admin)
        self.assertEqual(self._stock(), Decimal("989"))
        self.assertEqual(self._journal(), Decimal("-11"))

    def test_lots_material_journal_matches_after_edit(self):
        """Партионный материал: журнал и остаток сходятся и после правки."""
        mat = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            price_per_sqm=Decimal("1000"), purchase_price=Decimal("600"),
        )
        receive_lot(
            mat, form=Roll.Form.SHEET, purchase_cost=Decimal("12000"),
            width=Decimal("1"), height=Decimal("1"), sheet_count=20, user=self.admin,
        )
        mat.refresh_from_db()
        base = self._journal(mat)  # приход
        r = create_sale(
            client=None, cashier=self.admin, payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {"type": "MATERIAL", "material": mat, "quantity": Decimal("4"), "mode": "SQM"},
                {"type": "MATERIAL", "material": mat, "quantity": Decimal("2"), "mode": "SQM"},
            ],
            pay_full=True,
        )
        first, second = r.items.order_by("id")
        update_receipt_items(r, [{"id": first.id, "remove": True}], user=self.admin)
        self.assertEqual(self._stock(mat), Decimal("18"))
        self.assertEqual(self._journal(mat), base - Decimal("2"))
