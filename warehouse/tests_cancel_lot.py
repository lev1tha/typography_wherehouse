"""Отмена ошибочного прихода убирает и материал, и деньги.

Правка остатка («инвентаризация») убирает только материал: деньги остаются в
закупе, и отчёт показывает поставку, которой не было. На проде 20.09 так висели
102 960 сомов по акрилу салатовому.
"""
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from audit.models import AuditLog
from finance.material_sheet import purchases_from_stock
from sales.models import Receipt, TransactionItem
from warehouse.models import InventoryLog, Material, Roll
from warehouse.rolls import receive_lot


class CancelLotTests(TestCase):
    def setUp(self):
        self.material = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            price_per_sqm=Decimal("1000"),
        )
        self.good = receive_lot(
            self.material, form=Roll.Form.SHEET, width=Decimal("1.2"),
            height=Decimal("2.4"), sheet_count=Decimal("5"),
            purchase_cost=Decimal("8320"),
        )
        # Второй приход того же — ошибка ввода, поставки не было.
        self.bogus = receive_lot(
            self.material, form=Roll.Form.SHEET, width=Decimal("1.2"),
            height=Decimal("2.4"), sheet_count=Decimal("10"),
            purchase_cost=Decimal("30000"),
        )

    def _run(self, *args):
        out = StringIO()
        call_command("cancel_lot", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        out = self._run(str(self.bogus.id))
        # 30 000 / 28,8 не делится нацело: цена за кв.м округляется до копеек,
        # и закуп в отчёте считается уже по ней — отсюда копеечный хвост.
        self.assertIn("−30 000.10", out)
        self.assertTrue(Roll.objects.filter(pk=self.bogus.pk).exists())

    def test_removes_the_lot_its_journal_entry_and_the_stock(self):
        self.material.refresh_from_db()
        before = self.material.quantity
        self._run(str(self.bogus.id), "--yes")
        self.material.refresh_from_db()
        self.assertFalse(Roll.objects.filter(pk=self.bogus.pk).exists())
        self.assertEqual(self.material.quantity, before - Decimal("28.8000"))
        self.assertEqual(
            InventoryLog.objects.filter(type=InventoryLog.Type.SUPPLY).count(), 1
        )

    def test_purchases_drop_by_the_lot(self):
        before = purchases_from_stock(None, None)
        self._run(str(self.bogus.id), "--yes")
        self.assertEqual(before - purchases_from_stock(None, None), Decimal("30000.096"))

    def test_card_price_falls_back_to_the_remaining_lot(self):
        self.material.refresh_from_db()
        self.assertEqual(self.material.purchase_price, self.bogus.cost_per_sqm)
        self._run(str(self.bogus.id), "--yes")
        self.material.refresh_from_db()
        self.assertEqual(self.material.purchase_price, self.good.cost_per_sqm)

    def test_receipt_lines_survive_with_their_cost(self):
        """Строка чека остаётся, себестоимость в ней — прежняя."""
        receipt = Receipt.objects.create(total_price=Decimal("1000"))
        item = TransactionItem.objects.create(
            receipt=receipt, type=TransactionItem.Type.MATERIAL,
            material=self.material, roll=self.bogus,
            quantity=Decimal("1"), price_per_item=Decimal("1000"),
            cost_total=Decimal("500"),
        )
        out = self._run(str(self.bogus.id), "--yes")
        item.refresh_from_db()
        self.assertIn("строк чеков: 1", out)
        self.assertIsNone(item.roll_id)
        self.assertEqual(item.cost_total, Decimal("500"))

    def test_partly_used_lot_warns_but_works(self):
        self.bogus.remaining_area = Decimal("10")
        self.bogus.save(update_fields=["remaining_area"])
        self.material.refresh_from_db()
        before = self.material.quantity
        out = self._run(str(self.bogus.id), "--yes")
        self.material.refresh_from_db()
        self.assertIn("уже ушло", out)
        # С остатка снимаем только то, что от партии ещё не ушло.
        self.assertEqual(self.material.quantity, before - Decimal("10"))

    def test_writes_an_audit_record(self):
        self._run(str(self.bogus.id), "--yes")
        self.assertTrue(AuditLog.objects.filter(action__contains="Отменён приход").exists())

    def test_unknown_lot_is_refused(self):
        with self.assertRaises(CommandError):
            self._run("999999")
