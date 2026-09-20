"""Команда правки цены партии двигает все три места разом.

Цену при приёмке путают регулярно (на проде 20.09: форекс 8мм по 1 100 вместо
900, лист золота за 1 сом вместо 2 000). Цена живёт в партии, в записи журнала
и в карточке материала; поправить одно и забыть другое — значит развести склад
с финотчётом, и потом искать разницу в «Не объяснено».
"""
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from audit.models import AuditLog
from warehouse.models import InventoryLog, Material, Roll
from warehouse.rolls import receive_lot


class FixLotCostTests(TestCase):
    def setUp(self):
        self.material = Material.objects.create(
            name="Форекс", unit=Material.Unit.SQM, is_roll_material=True,
            price_per_sqm=Decimal("500"),
        )
        # 10 листов 1,2×2,4 = 28,8 кв.м за 11 000 — цена завышена, должно быть 9 000.
        self.roll = receive_lot(
            self.material, form=Roll.Form.SHEET, width=Decimal("1.2"),
            height=Decimal("2.4"), sheet_count=Decimal("10"),
            purchase_cost=Decimal("11000"),
        )

    def _run(self, *args):
        out = StringIO()
        call_command("fix_lot_cost", *args, stdout=out)
        return out.getvalue()

    def _log(self):
        return InventoryLog.objects.get(material=self.material, type=InventoryLog.Type.SUPPLY)

    def test_dry_run_changes_nothing(self):
        out = self._run(str(self.roll.id), "9000")
        self.assertIn("11 000.00 → 9 000.00", out)
        self.roll.refresh_from_db()
        self.assertEqual(self.roll.purchase_cost, Decimal("11000"))
        self.assertEqual(self._log().actual_price, Decimal("381.94"))

    def test_applies_to_lot_log_and_card_at_once(self):
        self._run(str(self.roll.id), "9000", "--yes")
        self.roll.refresh_from_db()
        self.material.refresh_from_db()
        self.assertEqual(self.roll.purchase_cost, Decimal("9000"))
        self.assertEqual(self.roll.cost_per_sqm, Decimal("312.50"))
        # Закуп в финотчёте считается по журналу — он должен поехать вместе.
        self.assertEqual(self._log().actual_price, Decimal("312.50"))
        # Цена последнего прихода — это цена карточки.
        self.assertEqual(self.material.purchase_price, Decimal("312.50"))

    def test_stock_value_follows(self):
        # `receive_lot` поднимает остаток на своей копии материала — читаем из базы.
        self.material.refresh_from_db()
        before = self.material.stock_value
        self._run(str(self.roll.id), "9000", "--yes")
        self.material.refresh_from_db()
        # 11 000 / 28,8 не делится нацело: цена за кв.м округляется до копеек,
        # и склад показывает 10 999.87. После правки 9 000 / 28,8 = 312,50 ровно.
        self.assertEqual(before, Decimal("10999.87"))
        self.assertEqual(self.material.stock_value, Decimal("9000.00"))

    def test_older_lot_does_not_touch_the_card(self):
        """Правим старую партию — цена карточки принадлежит свежей."""
        newer = receive_lot(
            self.material, form=Roll.Form.SHEET, width=Decimal("1.2"),
            height=Decimal("2.4"), sheet_count=Decimal("5"),
            purchase_cost=Decimal("4500"),
        )
        self.material.refresh_from_db()
        card = self.material.purchase_price
        out = self._run(str(self.roll.id), "9000", "--yes")
        self.material.refresh_from_db()
        self.assertIn("не последняя", out)
        self.assertEqual(self.material.purchase_price, card)
        self.assertEqual(newer.cost_per_sqm, Decimal("312.50"))

    def test_warns_when_the_lot_was_already_used(self):
        self.roll.remaining_area = Decimal("10")
        self.roll.save(update_fields=["remaining_area"])
        self.assertIn("уже ушло", self._run(str(self.roll.id), "9000"))

    def test_writes_an_audit_record(self):
        self._run(str(self.roll.id), "9000", "--yes")
        self.assertTrue(AuditLog.objects.filter(action__contains="Исправлена цена партии").exists())

    def test_unknown_lot_and_bad_amount_are_refused(self):
        with self.assertRaises(CommandError):
            self._run("999999", "9000")
        with self.assertRaises(CommandError):
            self._run(str(self.roll.id), "не число")
        with self.assertRaises(CommandError):
            self._run(str(self.roll.id), "-5")

    def test_refuses_when_the_journal_entry_is_ambiguous(self):
        """Две одинаковые записи журнала — сама не угадывает, какую править."""
        log = self._log()
        log.pk = None
        log.save()
        with self.assertRaises(CommandError):
            self._run(str(self.roll.id), "9000", "--yes")
        self.roll.refresh_from_db()
        self.assertEqual(self.roll.purchase_cost, Decimal("11000"))
