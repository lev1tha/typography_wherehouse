"""«Исправить приход» — опечатка в цене или количестве партии ПОСЛЕ продаж.

Аудит 10.10 (XL-02, STK-02, PNL-02, F2): в Excel опечатка в закупке — правка
ячейки, в системе после первой продажи её не исправить ни из интерфейса, ни
командой (`fix_lot_cost` не трогает проданное). Обход — фиктивное списание,
которое портит прибыль.

Все числа «до/после» посчитаны руками в комментариях.
"""
from datetime import date, datetime, time
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from finance.models import CashEntry, PeriodLock
from finance.reports import bridge as bridge_mod
from finance.reports.pnl import pnl
from sales import sale_service
from sales.models import Receipt, TransactionItem
from warehouse.models import InventoryLog, Material, Roll, Supply, SupplyLine

D = Decimal
SEP_1, SEP_30 = date(2026, 9, 1), date(2026, 9, 30)
PREVIEW = "/api/warehouse/lot-correction/preview/"
APPLY = "/api/warehouse/lot-correction/apply/"


def noon(day):
    return timezone.make_aware(datetime.combine(day, time(12, 0)))


class Base(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="lc_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)

    def sell(self, material, qty, mode, day=date(2026, 9, 15), price=None, roll=None):
        entry = {"type": "MATERIAL", "material": material, "quantity": D(qty), "mode": mode}
        if roll is not None:
            entry["roll"] = roll
        if price is not None:
            entry["material_price"] = D(price)
        return sale_service.create_sale(
            client=None, cashier=self.admin, payment_method="CASH",
            items_data=[entry], pay_full=True, created_at=noon(day),
        )

    def unexplained(self):
        return bridge_mod.bridge(SEP_1, SEP_30)["unexplained"]


class SheetPriceTypoTests(Base):
    """Акрил листами 1×2 м (2 кв.м). Накладная от 10.09: 10 листов по 4 000
    вместо 400 — сумма строки 40 000 вместо 4 000. Оплачено 3 000 наличными.
    15.09 продали 2 листа по 3 000."""

    def setUp(self):
        super().setUp()
        self.acrylic = Material.objects.create(
            name="Акрил 3мм", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=D("1"), sheet_height=D("2"), piece_price=D("3000"),
        )
        r = self.client.post("/api/warehouse/supplies/", {
            "number": "17", "received_on": "2026-09-10",
            "paid_amount": "3000", "paid_account": "CASH",
            "lines": [{"material": self.acrylic.id, "form": "SHEET", "width": "1",
                       "height": "2", "sheet_count": "10", "cost": "40000"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.supply = Supply.objects.get(pk=r.data["id"])
        self.line = self.supply.lines.get()
        self.roll = self.line.roll
        # cps = 40 000 / 20 кв.м = 2 000; 2 листа = 4 кв.м → себестоимость 8 000.
        self.receipt = self.sell(self.acrylic, 2, "PIECE")
        self.item = self.receipt.items.get()
        self.assertEqual(self.item.cost_total, D("8000.00"))

    def payload(self, **kw):
        return {"roll": self.roll.id, "purchase_cost": "4000", **kw}

    def test_preview_shows_before_and_after_and_changes_nothing(self):
        r = self.client.post(PREVIEW, self.payload(), format="json")
        self.assertEqual(r.status_code, 200, r.data)
        p = r.data
        # Склад: 8 листов = 16 кв.м. Было 16 × 2 000 = 32 000, стало 16 × 200 = 3 200.
        self.assertEqual(D(p["stock"]["value_before"]), D("32000.00"))
        self.assertEqual(D(p["stock"]["value_after"]), D("3200.00"))
        # Накладная: 40 000 → 4 000; долг 37 000 → 1 000, оплачено 3 000 не трогаем.
        self.assertEqual(D(p["supply"]["total_before"]), D("40000.00"))
        self.assertEqual(D(p["supply"]["total_after"]), D("4000.00"))
        self.assertEqual(D(p["supply"]["paid"]), D("3000"))
        self.assertEqual(D(p["supply"]["debt_before"]), D("37000.00"))
        self.assertEqual(D(p["supply"]["debt_after"]), D("1000.00"))
        # Чек: выручка 6 000, себестоимость 8 000 → 800, маржа −2 000 → 5 200.
        [rec] = p["receipts"]
        self.assertEqual(rec["id"], str(self.receipt.id))
        self.assertEqual(D(rec["cost_before"]), D("8000.00"))
        self.assertEqual(D(rec["cost_after"]), D("800.00"))
        self.assertEqual(D(rec["margin_before"]), D("-2000.00"))
        self.assertEqual(D(rec["margin_after"]), D("5200.00"))
        self.assertEqual(D(p["cogs_delta"]), D("-7200.00"))
        self.assertEqual(p["months"], ["2026-09"])
        self.assertEqual(p["closed_months"], [])
        self.assertEqual(p["legacy_count"], 0)
        # Ничего не изменилось.
        self.item.refresh_from_db()
        self.roll.refresh_from_db()
        self.assertEqual(self.item.cost_total, D("8000.00"))
        self.assertEqual(self.roll.purchase_cost, D("40000"))

    def test_apply_moves_everything_at_once(self):
        before_unexplained = self.unexplained()
        losses_before = pnl(SEP_1, SEP_30)["losses"]
        r = self.client.post(APPLY, self.payload(), format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.item.refresh_from_db()
        self.roll.refresh_from_db()
        self.line.refresh_from_db()
        self.supply.refresh_from_db()
        self.acrylic.refresh_from_db()
        self.assertEqual(self.item.cost_total, D("800.00"))
        self.assertEqual(self.roll.purchase_cost, D("4000.00"))
        self.assertEqual(self.roll.cost_per_sqm, D("200.00"))
        self.assertEqual(self.roll.remaining_area, D("16"))
        self.assertEqual(self.line.cost, D("4000.00"))
        self.assertEqual(self.supply.total_cost, D("4000.00"))
        self.assertEqual(self.supply.paid_amount, D("3000"))
        self.assertEqual(self.supply.debt, D("1000.00"))
        self.assertEqual(self.acrylic.stock_value, D("3200.00"))
        self.assertEqual(self.acrylic.purchase_price, D("200.00"))
        # Касса не тронута: исправление — не деньги.
        self.assertEqual(CashEntry.objects.filter(article=CashEntry.Article.SUPPLY).count(), 1)
        # ОПиУ: себестоимость сентября 800, закуп 4 000; убытка/излишка нет.
        p = pnl(SEP_1, SEP_30)
        self.assertEqual(p["cogs_total"], D("800.00"))
        self.assertEqual(p["losses"], losses_before)
        self.assertEqual(Receipt.objects.get(pk=self.receipt.pk).margin, D("5200.00"))
        # Сверка ОПиУ → ОДДС: «Не объяснено» как было ноль, так и осталось.
        self.assertEqual(before_unexplained, D("0"))
        self.assertEqual(self.unexplained(), D("0"))

    def test_journal_gets_a_correction_record_linked_to_the_lot(self):
        self.client.post(APPLY, self.payload(), format="json")
        log = InventoryLog.objects.get(type=InventoryLog.Type.CORRECTION)
        self.assertEqual(log.roll_id, self.roll.id)
        self.assertEqual(log.supply_id, self.supply.id)
        self.assertEqual(log.quantity_changed, D("0"))
        self.assertIsNone(log.cost)
        self.assertIn("40 000", log.reason)
        # Приход в журнале — по новой цене (по нему считается закуп одиночных).
        supply_log = InventoryLog.objects.get(type=InventoryLog.Type.SUPPLY)
        self.assertEqual(supply_log.actual_price, D("200.00"))
        self.assertEqual(supply_log.roll_id, self.roll.id)

    def test_audit_lists_receipts(self):
        self.client.post(APPLY, self.payload(), format="json")
        rec = AuditLog.objects.filter(action__contains="Исправлен приход").get()
        self.assertIn("40 000", rec.action)
        self.assertIn("4 000", rec.action)
        self.assertIn(f"№{self.receipt.order_number}", rec.action)

    def test_closed_month_is_refused_with_the_month(self):
        lock = PeriodLock.load()
        lock.closed_through = SEP_30
        lock.save()
        r = self.client.post(PREVIEW, self.payload(), format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["closed_months"], ["2026-09"])
        r = self.client.post(APPLY, self.payload(), format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("09.2026", str(r.data))
        self.item.refresh_from_db()
        self.assertEqual(self.item.cost_total, D("8000.00"))

    def test_sale_in_closed_month_blocks_even_if_supply_is_open(self):
        """Приход в открытом месяце, а продажа из партии — в закрытом."""
        # Перенесём накладную на октябрь, а продажу оставим в сентябре.
        self.client.patch(f"/api/warehouse/supplies/{self.supply.id}/", {"received_on": "2026-10-01"}, format="json")
        lock = PeriodLock.load()
        lock.closed_through = SEP_30
        lock.save()
        r = self.client.post(APPLY, self.payload(), format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("09.2026", str(r.data))

    def test_permissions(self):
        for role in (User.Role.STOREKEEPER, User.Role.ACCOUNTANT):
            user = User.objects.create_user(username=f"lc_{role}", password="x", role=role)
            self.client.force_authenticate(user)
            for url in (PREVIEW, APPLY):
                with self.subTest(role=role, url=url):
                    self.assertEqual(self.client.post(url, self.payload(), format="json").status_code, 403)
        self.item.refresh_from_db()
        self.assertEqual(self.item.cost_total, D("8000.00"))

    def test_sheet_count_typo_after_sale(self):
        """Приняли 10 листов, на деле 12: остаток +2 листа, накладная по той
        же цене за лист — 48 000 (пока цену не поправили)."""
        r = self.client.post(APPLY, {"roll": self.roll.id, "sheet_count": "12"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.roll.refresh_from_db()
        self.acrylic.refresh_from_db()
        self.supply.refresh_from_db()
        self.assertEqual(self.roll.initial_area, D("24"))
        self.assertEqual(self.roll.remaining_area, D("20"))
        self.assertEqual(self.acrylic.quantity, D("20"))
        self.assertEqual(self.supply.total_cost, D("48000.00"))
        self.item.refresh_from_db()
        self.assertEqual(self.item.cost_total, D("8000.00"))   # цена листа та же

    def test_sheet_size_differs_from_card_warns(self):
        r = self.client.post(PREVIEW, {"roll": self.roll.id, "width": "1.22", "height": "2.44"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("sheet_size_mismatch", [w["code"] for w in r.data["warnings"]])


class LegacySalesTests(Base):
    """Продажи до 10.10 не знают, из какой партии взяли (`TransactionItemLot`)."""

    def setUp(self):
        super().setUp()
        self.mat = Material.objects.create(
            name="Форекс", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=D("1"), sheet_height=D("2"), piece_price=D("3000"),
        )
        from warehouse.rolls import receive_lot

        self.roll = receive_lot(
            self.mat, form=Roll.Form.SHEET, width=D("1"), height=D("2"),
            sheet_count=D("10"), purchase_cost=D("40000"), received_at=noon(date(2026, 9, 10)),
        )
        self.with_lots = self.sell(self.mat, 1, "PIECE").items.get()
        self.old_exact = self.sell(self.mat, 1, "PIECE").items.get()
        self.old_odd = self.sell(self.mat, 1, "PIECE").items.get()
        # Две строки «до 10.10»: записей о партиях нет.
        self.old_exact.lot_uses.all().delete()
        self.old_odd.lot_uses.all().delete()
        # У второй себестоимость не сходится с ценой партии — откуда взяли,
        # по ней не понять.
        TransactionItem.objects.filter(pk=self.old_odd.pk).update(cost_total=D("3500"))

    def test_old_sales_without_lots(self):
        r = self.client.post(APPLY, {"roll": self.roll.id, "purchase_cost": "4000"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        for item in (self.with_lots, self.old_exact, self.old_odd):
            item.refresh_from_db()
        # По записи партии — пересчитана: 2 кв.м × 200 = 400.
        self.assertEqual(self.with_lots.cost_total, D("400.00"))
        # Старая, но целиком из этой партии (её рулон и себестоимость ровно
        # 2 × 2 000) — восстановлена и пересчитана.
        self.assertEqual(self.old_exact.cost_total, D("400.00"))
        # Старая, не сходится — не тронута и названа в ответе.
        self.assertEqual(self.old_odd.cost_total, D("3500"))
        self.assertEqual(r.data["legacy_count"], 1)
        self.assertEqual(r.data["legacy"][0]["item"], self.old_odd.id)
        self.assertEqual(r.data["inferred_count"], 1)


class RollLengthTypoTests(Base):
    """Рулон баннера 1,5 м. Принят одиночным приходом 10.09 «в долг»: 50 м за
    15 000 (300 сом/м, 200 сом/кв.м). На деле 30 м. 15.09 продали 12 м."""

    def setUp(self):
        super().setUp()
        self.banner = Material.objects.create(
            name="Баннер", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL, roll_width=D("1.5"),
            price_per_pm=D("700"),
        )
        r = self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": self.banner.id, "form": "ROLL", "width": "1.5", "length": "50",
            "purchase_cost": "15000", "received_on": "2026-09-10", "payment": "DEBT",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.roll = Roll.objects.get(material=self.banner)
        self.assertEqual(self.roll.supplier_debt, D("15000"))
        # 12 м × 1,5 = 18 кв.м × 200 = 3 600.
        self.item = self.sell(self.banner, 12, "METER", roll=self.roll).items.get()
        self.assertEqual(self.item.cost_total, D("3600.00"))

    def test_length_50_to_30_keeps_the_metre_price(self):
        r = self.client.post(PREVIEW, {"roll": self.roll.id, "length": "30"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        p = r.data
        # Остаток 38 м (57 кв.м) → 18 м (27 кв.м); склад 11 400 → 5 400.
        self.assertEqual(D(p["stock"]["value_before"]), D("11400.00"))
        self.assertEqual(D(p["stock"]["value_after"]), D("5400.00"))
        # Долг за партию 15 000 → 9 000 (30 м × 300).
        self.assertEqual(D(p["lot_debt"]["debt_before"]), D("15000"))
        self.assertEqual(D(p["lot_debt"]["debt_after"]), D("9000.00"))
        self.assertEqual(p["receipts"], [])          # цена метра та же — чеки не меняются
        r = self.client.post(APPLY, {"roll": self.roll.id, "length": "30"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.roll.refresh_from_db()
        self.banner.refresh_from_db()
        self.assertEqual(self.roll.length, D("30"))
        self.assertEqual(self.roll.initial_area, D("45"))
        self.assertEqual(self.roll.remaining_area, D("27"))
        self.assertEqual(self.roll.metres_remaining, D("18.00"))
        self.assertEqual(self.roll.purchase_cost, D("9000.00"))
        self.assertEqual(self.roll.supplier_debt, D("9000.00"))
        self.assertEqual(self.banner.quantity, D("27"))
        self.assertEqual(self.banner.stock_value, D("5400.00"))
        self.item.refresh_from_db()
        self.assertEqual(self.item.cost_total, D("3600.00"))
        supply_log = InventoryLog.objects.get(type=InventoryLog.Type.SUPPLY)
        self.assertEqual(supply_log.quantity_changed, D("45"))
        self.assertEqual(supply_log.metres_changed, D("30"))
        # Журнал склада сходится с остатком: 45 − 18 = 27.
        total = sum(InventoryLog.objects.filter(material=self.banner).values_list("quantity_changed", flat=True))
        self.assertEqual(total, D("27"))
        self.assertEqual(self.unexplained(), D("0"))
        self.assertEqual(pnl(SEP_1, SEP_30)["losses"], D("0"))

    def test_cannot_go_below_what_was_already_sold(self):
        r = self.client.post(PREVIEW, {"roll": self.roll.id, "length": "10"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("12", str(r.data))
        r = self.client.post(APPLY, {"roll": self.roll.id, "length": "10"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.roll.refresh_from_db()
        self.assertEqual(self.roll.length, D("50"))

    def test_total_price_typo_reprices_sold_metres(self):
        """Длина верна, а сумма — 1 500 вместо 15 000: 12 м стоят 360, не 3 600."""
        r = self.client.post(APPLY, {"roll": self.roll.id, "purchase_cost": "1500"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.item.refresh_from_db()
        self.roll.refresh_from_db()
        self.assertEqual(self.item.cost_total, D("360.00"))
        self.assertEqual(self.roll.supplier_debt, D("1500.00"))

    def test_nothing_to_change(self):
        r = self.client.post(PREVIEW, {"roll": self.roll.id, "length": "50"}, format="json")
        self.assertEqual(r.status_code, 400)


class PieceSupplyLineTests(Base):
    """Штучный материал по накладной — без партии (строка QTY)."""

    def setUp(self):
        super().setUp()
        self.glue = Material.objects.create(name="Клей", unit=Material.Unit.PIECE, price_per_unit=D("500"))
        r = self.client.post("/api/warehouse/supplies/", {
            "received_on": "2026-09-10",
            "lines": [{"material": self.glue.id, "form": "QTY", "quantity": "10", "cost": "1000"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.line = SupplyLine.objects.get(material=self.glue)

    def test_quantity_and_price_on_a_line_without_lot(self):
        r = self.client.post(APPLY, {"supply_line": self.line.id, "quantity": "8", "purchase_cost": "1600"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.line.refresh_from_db()
        self.glue.refresh_from_db()
        self.assertEqual(self.line.quantity, D("8"))
        self.assertEqual(self.line.cost, D("1600.00"))
        self.assertEqual(self.glue.quantity, D("8"))
        self.assertEqual(self.glue.purchase_price, D("200.00"))
        log = InventoryLog.objects.get(type=InventoryLog.Type.SUPPLY)
        self.assertEqual((log.quantity_changed, log.actual_price), (D("8"), D("200.00")))

    def test_cannot_take_more_than_is_left(self):
        self.sell(self.glue, 5, "SQM")
        r = self.client.post(PREVIEW, {"supply_line": self.line.id, "quantity": "4"}, format="json")
        self.assertEqual(r.status_code, 400)


class SheetSizeAtIntakeTests(Base):
    """F7: размер листа в приходе сверяется с карточкой — предупреждением."""

    def setUp(self):
        super().setUp()
        self.mat = Material.objects.create(
            name="ПВХ", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=D("1"), sheet_height=D("2"),
        )

    def test_receive_roll_warns(self):
        r = self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": self.mat.id, "form": "SHEET", "width": "1.22", "height": "2.44",
            "sheet_count": "5", "purchase_cost": "5000",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual([w["code"] for w in r.data["warnings"]], ["sheet_size_mismatch"])

    def test_rotated_same_size_is_fine(self):
        r = self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": self.mat.id, "form": "SHEET", "width": "2", "height": "1",
            "sheet_count": "5", "purchase_cost": "5000",
        }, format="json")
        self.assertEqual(r.data["warnings"], [])

    def test_supply_warns(self):
        r = self.client.post("/api/warehouse/supplies/", {
            "received_on": "2026-09-10",
            "lines": [{"material": self.mat.id, "form": "SHEET", "width": "1.22",
                       "height": "2.44", "sheet_count": "5", "cost": "5000"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual([w["code"] for w in r.data["warnings"]], ["sheet_size_mismatch"])
        self.assertIsNotNone(r.data["lines"][0]["roll"])
