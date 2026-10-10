"""Этап 1 переделки ОПиУ/ОДДС (2026-10-07): данные, справочник, замки.

Что держат эти тесты:
- справочник `finance.chart` полон: у каждой роли вида расхода и каждой статьи
  кассы есть строка ОПиУ (или явное «не участвует») и строка ОДДС;
- трата знает «за какой месяц» (D-2) и, если это капвложение выше порога, —
  срок службы и месяц выбытия (D-13, D-20, D-22);
- всё, что меняет прошлые месяцы ОПиУ, подчиняется замку периода (D-21);
- ставка налога хранится историей и действует только вперёд (D-10, D-19);
- оплата поставщику не стирается при отмене прихода, а возвращается
  встречной записью (аудит Б-13), и руками в кассе не вносится (Б-7);
- онлайн-заказ становится продажей только с подтверждением оплаты (D-14).
"""
from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from finance import chart
from finance.models import CashEntry, ExpenseEntry, ExpenseKind, FinanceSettings, TaxRate
from finance.periods import add_months, month_end, month_start, months_between, parse_month
from sales import sale_service
from sales.models import Receipt
from warehouse.models import Material, Roll

ENTRIES = "/api/finance/expense-entries/"
KINDS = "/api/finance/expense-kinds/"
RATES = "/api/finance/tax-rates/"
SETTINGS = "/api/finance/settings/"
CASH = "/api/finance/cash/"
PERIOD = "/api/finance/period/"


def ym(day):
    return day.strftime("%Y-%m")


class AdminCase(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="rd_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.today = timezone.localdate()
        self.rent = ExpenseKind.objects.get(code="RENT")
        self.equipment = ExpenseKind.objects.get(code=ExpenseKind.EQUIPMENT)
        self.improvement = ExpenseKind.objects.get(code=ExpenseKind.IMPROVEMENT)

    def close_through(self, day):
        r = self.client.patch(PERIOD, {"closed_through": day.isoformat()}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def post_entry(self, kind, amount, spent_at, expect=201, **extra):
        r = self.client.post(ENTRIES, {
            "kind": kind.id, "amount": str(amount), "spent_at": spent_at.isoformat(), **extra,
        }, format="json")
        self.assertEqual(r.status_code, expect, r.data)
        return r.data


class PeriodHelpersTests(APITestCase):
    def test_month_arithmetic(self):
        self.assertEqual(month_start(date(2026, 10, 31)), date(2026, 10, 1))
        self.assertEqual(month_end(date(2028, 2, 3)), date(2028, 2, 29))
        self.assertEqual(add_months(date(2026, 11, 15), 3), date(2027, 2, 1))
        self.assertEqual(add_months(date(2026, 1, 15), -1), date(2025, 12, 1))
        self.assertEqual(months_between(date(2026, 11, 1), date(2028, 10, 31)), 24)
        self.assertEqual(months_between(date(2026, 11, 1), date(2026, 10, 1)), 0)
        self.assertEqual(parse_month("2026-08"), date(2026, 8, 1))
        self.assertEqual(parse_month("2026-08-19"), date(2026, 8, 1))
        self.assertIsNone(parse_month("август"))

    def test_local_midnight_stays_in_its_month(self):
        """Полночь 1 ноября по Бишкеку — 31 октября по UTC; месяц — ноябрь."""
        from datetime import datetime, time

        moment = timezone.make_aware(datetime.combine(date(2026, 11, 1), time(0, 30)))
        self.assertEqual(month_start(moment), date(2026, 11, 1))


class ChartTests(APITestCase):
    def test_every_role_and_article_is_mapped(self):
        self.assertEqual(set(chart.ROLES), set(ExpenseKind.Role.values))
        self.assertEqual(set(chart.ARTICLES), set(CashEntry.Article.values))

    def test_mappings_point_to_known_lines(self):
        for mapping in [*chart.ROLES.values(), *chart.ARTICLES.values(),
                        *chart.SYSTEM.values(), chart.CAPEX_EXPENSED]:
            self.assertTrue(mapping.pnl is None or mapping.pnl in chart.PNL_LINES, mapping)
            self.assertTrue(
                mapping.cash_section is None or mapping.cash_section in chart.CASH_SECTIONS,
                mapping,
            )

    def test_methodology_corner_cases(self):
        A = CashEntry.Article
        # Переводы и ввод остатка — вне потока (D-5, D-6).
        self.assertEqual(chart.for_article(A.TRANSFER).cash_section, chart.OUTSIDE)
        self.assertEqual(chart.for_article(A.OPENING).cash_section, chart.OUTSIDE)
        # Недостача кассы — и поток, и прибыль (D-5).
        count = chart.for_article(A.COUNT)
        self.assertEqual((count.pnl, count.cash_section), (chart.CASH_COUNT, chart.OPERATING))
        # Откаты — своей строкой мимо ОПиУ (D-18).
        unpay = chart.for_article(A.UNPAY)
        self.assertEqual((unpay.pnl, unpay.cash_line), (None, "unpay"))
        # Проценты — ОПиУ ниже операционной и операционная в ОДДС (D-4).
        interest = chart.for_role(ExpenseKind.Role.INTEREST)
        self.assertEqual((interest.pnl, interest.cash_section), (chart.INTEREST, chart.OPERATING))
        # Уплата налога в ОПиУ не идёт: налог начисляется от выручки (D-10).
        self.assertIsNone(chart.for_role(ExpenseKind.Role.TAX).pnl)

    def test_capex_below_threshold_is_an_ordinary_expense(self):
        kind = ExpenseKind.objects.get(code=ExpenseKind.EQUIPMENT)
        asset = ExpenseEntry(kind=kind, amount=Decimal("300000"), useful_life_months=60)
        cheap = ExpenseEntry(kind=kind, amount=Decimal("5000"))
        self.assertEqual(chart.for_expense(asset).cash_section, chart.INVESTING)
        self.assertEqual(chart.for_expense(asset).pnl, chart.DEPRECIATION)
        self.assertEqual(chart.for_expense(cheap).cash_section, chart.OPERATING)
        self.assertEqual(chart.for_expense(cheap).pnl, chart.OPEX)


class BuiltinDataTests(AdminCase):
    def test_roles_after_migration(self):
        roles = dict(ExpenseKind.objects.values_list("code", "role"))
        self.assertEqual(roles["EQUIPMENT"], "CAPEX")
        self.assertEqual(roles["IMPROVEMENT"], "CAPEX")
        self.assertEqual(roles["MATERIAL_PURCHASE"], "INVENTORY")
        self.assertEqual(roles["MATERIAL_DEBT"], "NOT_CASH")
        self.assertEqual(roles["RENT"], "OPEX")
        self.assertEqual(roles["TRANSPORT"], "OPEX")

    def test_interest_and_tax_kinds_exist_below_the_line(self):
        interest = ExpenseKind.objects.get(code=ExpenseKind.INTEREST)
        tax = ExpenseKind.objects.get(code=ExpenseKind.TAX)
        self.assertEqual((interest.block, interest.role), ("BELOW", "INTEREST"))
        self.assertEqual((tax.block, tax.role), ("BELOW", "TAX"))
        self.assertTrue(interest.is_builtin and tax.is_builtin)

    def test_tax_payment_and_interest_leave_the_cash_box(self):
        for code in (ExpenseKind.TAX, ExpenseKind.INTEREST):
            self.post_entry(ExpenseKind.objects.get(code=code), "1000", self.today)
        self.assertEqual(CashEntry.balance(CashEntry.Account.CASH), Decimal("-2000"))

    def test_not_cash_role_writes_no_cash(self):
        self.post_entry(ExpenseKind.objects.get(code="MATERIAL_DEBT"), "7000", self.today)
        self.assertFalse(CashEntry.objects.exists())

    def test_user_kind_cannot_go_below_the_line(self):
        r = self.client.post(KINDS, {"name": "Мой налог", "block": "BELOW"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_user_investment_kind_is_capex(self):
        r = self.client.post(KINDS, {"name": "Компрессор", "block": "INVESTMENT"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["role"], "CAPEX")
        self.assertFalse(r.data["in_profit"])


class AccrualMonthTests(AdminCase):
    def test_period_defaults_to_the_payment_month(self):
        data = self.post_entry(self.rent, 25000, date(2026, 9, 5))
        self.assertEqual(data["period"], "2026-09")

    def test_explicit_period_is_kept(self):
        """Аренда сентября, оплаченная 5 октября: ОПиУ — сентябрь, касса — октябрь."""
        data = self.post_entry(self.rent, 25000, date(2026, 10, 5), period="2026-09")
        self.assertEqual(data["period"], "2026-09")
        entry = ExpenseEntry.objects.get(pk=data["id"])
        self.assertEqual(entry.accrual_month, date(2026, 9, 1))
        self.assertEqual(entry.cash_entries.get().happened_on, date(2026, 10, 5))

    def test_default_period_follows_the_payment_date(self):
        data = self.post_entry(self.rent, 25000, date(2026, 9, 5))
        r = self.client.patch(f"{ENTRIES}{data['id']}/", {"spent_at": "2026-10-02"}, format="json")
        self.assertEqual(r.data["period"], "2026-10")

    def test_chosen_period_survives_a_payment_date_fix(self):
        data = self.post_entry(self.rent, 25000, date(2026, 10, 5), period="2026-09")
        r = self.client.patch(f"{ENTRIES}{data['id']}/", {"spent_at": "2026-10-07"}, format="json")
        self.assertEqual(r.data["period"], "2026-09")

    def test_orm_entry_gets_its_month_too(self):
        entry = ExpenseEntry.objects.create(kind=self.rent, amount=Decimal("1"), spent_at="2026-07-19")
        self.assertEqual(entry.period, date(2026, 7, 1))

    def test_bad_month_is_rejected(self):
        self.post_entry(self.rent, 25000, date(2026, 9, 5), expect=400, period="сентябрь")


class CapitalizationTests(AdminCase):
    def test_purchase_above_threshold_is_an_asset_for_60_months(self):
        data = self.post_entry(self.equipment, 300000, self.today)
        self.assertTrue(data["is_capitalized"])
        self.assertEqual(data["useful_life_months"], 60)

    def test_threshold_itself_is_an_asset(self):
        data = self.post_entry(self.equipment, 20000, self.today)
        self.assertTrue(data["is_capitalized"])

    def test_purchase_below_threshold_is_an_expense(self):
        data = self.post_entry(self.equipment, "19999.99", self.today, useful_life_months=12)
        self.assertFalse(data["is_capitalized"])
        self.assertIsNone(data["useful_life_months"])

    def test_life_can_be_set_by_hand(self):
        data = self.post_entry(self.equipment, 300000, self.today, useful_life_months=36)
        self.assertEqual(data["useful_life_months"], 36)

    def test_threshold_change_does_not_rewrite_past_purchases(self):
        data = self.post_entry(self.equipment, 30000, self.today)
        self.client.patch(SETTINGS, {"capitalization_threshold": "50000"}, format="json")
        r = self.client.patch(f"{ENTRIES}{data['id']}/", {"note": "ЧПУ-станок"}, format="json")
        self.assertTrue(r.data["is_capitalized"])
        # А новая сумма решается по новому порогу.
        r = self.client.patch(f"{ENTRIES}{data['id']}/", {"amount": "40000"}, format="json")
        self.assertFalse(r.data["is_capitalized"])

    def test_non_capex_ignores_asset_fields(self):
        data = self.post_entry(self.rent, 25000, self.today, useful_life_months=12,
                               depreciate_until=ym(self.today))
        self.assertIsNone(data["useful_life_months"])
        self.assertIsNone(data["depreciate_until"])

    def test_improvement_life_is_capped_by_the_lease(self):
        lease_month = add_months(self.today, 24)
        self.client.patch(SETTINGS, {"lease_until": month_end(lease_month).isoformat()}, format="json")
        data = self.post_entry(self.improvement, 100000, self.today)
        self.assertEqual(data["useful_life_months"], 24)
        self.post_entry(self.improvement, 100000, self.today, expect=400, useful_life_months=30)
        # Оборудование арендой не ограничено.
        self.assertEqual(self.post_entry(self.equipment, 100000, self.today)["useful_life_months"], 60)

    def test_improvement_after_the_lease_ends_writes_off_next_month(self):
        self.client.patch(SETTINGS, {"lease_until": (self.today - timedelta(days=40)).isoformat()},
                          format="json")
        self.assertEqual(self.post_entry(self.improvement, 100000, self.today)["useful_life_months"], 1)

    def test_improvement_without_lease_is_60_months(self):
        self.assertEqual(self.post_entry(self.improvement, 100000, self.today)["useful_life_months"], 60)

    def test_disposal_month(self):
        data = self.post_entry(self.equipment, 300000, date(2026, 10, 5))
        r = self.client.patch(f"{ENTRIES}{data['id']}/", {"depreciate_until": "2027-03"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["depreciate_until"], "2027-03")
        r = self.client.patch(f"{ENTRIES}{data['id']}/", {"depreciate_until": "2026-09"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_cheap_purchase_has_nothing_to_dispose(self):
        data = self.post_entry(self.equipment, 5000, self.today)
        r = self.client.patch(f"{ENTRIES}{data['id']}/", {"depreciate_until": ym(self.today)},
                              format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_settings_validation(self):
        r = self.client.patch(SETTINGS, {"capitalization_threshold": "-1"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        r = self.client.patch(SETTINGS, {"capitalization_threshold": "25000",
                                         "lease_until": "2028-06-30"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        s = FinanceSettings.load()
        self.assertEqual((s.capitalization_threshold, s.lease_until),
                         (Decimal("25000"), date(2028, 6, 30)))


class LockTests(AdminCase):
    """Всё, что меняет закрытые месяцы ОПиУ, держит замок периода."""

    def setUp(self):
        super().setUp()
        self.this_month = month_start(self.today)
        self.prev_end = self.this_month - timedelta(days=1)
        self.two_ago = add_months(self.today, -2).replace(day=5)

    def test_expense_cannot_be_attributed_to_a_closed_month(self):
        self.close_through(self.prev_end)
        self.post_entry(self.rent, 25000, self.today, expect=400, period=ym(self.prev_end))
        self.post_entry(self.rent, 25000, self.today, period=ym(self.today))

    def test_month_closed_halfway_is_closed_for_accrual(self):
        """Замок «по середину месяца» уже закрыл часть его прибыли."""
        # Середина ПРОШЛОГО месяца, а не «сегодня минус день»: в первое число
        # «вчера» — это уже конец прошлого месяца целиком (замок не «по
        # середину»), и тест приходилось пропускать. Пятнадцатое есть в любом
        # месяце, поэтому результат не зависит от того, какое сегодня число.
        half = self.prev_end.replace(day=15)
        self.close_through(half)
        self.post_entry(self.rent, 25000, half + timedelta(days=5), expect=400)

    def test_period_cannot_be_moved_out_of_a_closed_month(self):
        data = self.post_entry(self.rent, 25000, self.today, period=ym(self.prev_end))
        self.close_through(self.prev_end)
        r = self.client.patch(f"{ENTRIES}{data['id']}/", {"period": ym(self.today)}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_asset_bought_in_a_closed_month(self):
        data = self.post_entry(self.equipment, 300000, self.two_ago)
        self.close_through(self.prev_end)
        url = f"{ENTRIES}{data['id']}/"
        # Срок — нельзя: график уже начался в закрытом месяце (D-21).
        r = self.client.patch(url, {"useful_life_months": 36}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("амортизировать до", str(r.data))
        # Сумму — нельзя: покупка в закрытом периоде.
        r = self.client.patch(url, {"amount": "310000"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        # Выбытие в открытом месяце — можно: прошлое не меняется.
        r = self.client.patch(url, {"depreciate_until": ym(self.today)}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        # В закрытый — нельзя.
        r = self.client.patch(url, {"depreciate_until": ym(self.prev_end)}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        # Убрать выбытие, назначенное на открытый месяц, — можно.
        r = self.client.patch(url, {"depreciate_until": None}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_life_change_is_fine_while_the_schedule_is_open(self):
        data = self.post_entry(self.equipment, 300000, self.today)
        self.close_through(self.prev_end)
        r = self.client.patch(f"{ENTRIES}{data['id']}/", {"useful_life_months": 36}, format="json")
        self.assertEqual(r.status_code, 200, r.data)


class TaxRateHistoryTests(AdminCase):
    def test_owner_rate_is_4_percent_from_october_2026(self):
        r = self.client.get(RATES)
        self.assertEqual(r.status_code, 200)
        self.assertIn({"valid_from": "2026-10", "rate": "4.00"},
                      [{k: x[k] for k in ("valid_from", "rate")} for x in r.data])
        self.assertEqual(TaxRate.rate_for(date(2026, 9, 30)), Decimal("0"))
        self.assertEqual(TaxRate.rate_for(date(2026, 10, 1)), Decimal("4.00"))

    def test_new_rate_works_only_forward(self):
        r = self.client.post(RATES, {"valid_from": "2030-01", "rate": "3"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(TaxRate.rate_for(date(2029, 12, 31)), Decimal("4.00"))
        self.assertEqual(TaxRate.rate_for(date(2030, 1, 1)), Decimal("3.00"))
        self.assertEqual(TaxRate.rate_for(date(2031, 6, 1)), Decimal("3.00"))

    def test_one_rate_per_month(self):
        r = self.client.post(RATES, {"valid_from": "2026-10", "rate": "5"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_rate_bounds(self):
        r = self.client.post(RATES, {"valid_from": "2030-02", "rate": "120"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)


class TaxRateLockTests(AdminCase):
    def setUp(self):
        super().setUp()
        TaxRate.objects.all().delete()
        self.prev = add_months(self.today, -1)
        self.prev_end = month_start(self.today) - timedelta(days=1)

    def test_closed_month_rate_cannot_be_added_changed_or_removed(self):
        r = self.client.post(RATES, {"valid_from": ym(self.prev), "rate": "4"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        rate_id = r.data["id"]
        self.close_through(self.prev_end)
        r = self.client.patch(f"{RATES}{rate_id}/", {"rate": "5"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        r = self.client.delete(f"{RATES}{rate_id}/")
        self.assertEqual(r.status_code, 400)
        r = self.client.post(RATES, {"valid_from": ym(add_months(self.today, -3)), "rate": "2"},
                             format="json")
        self.assertEqual(r.status_code, 400, r.data)
        # Вперёд — можно.
        r = self.client.post(RATES, {"valid_from": ym(add_months(self.today, 1)), "rate": "3"},
                             format="json")
        self.assertEqual(r.status_code, 201, r.data)


class CashArticleTests(AdminCase):
    def test_manual_supplier_payment_is_refused(self):
        r = self.client.post(CASH, {
            "account": "CASH", "kind": "OUT", "article": "SUPPLY", "amount": "100",
            "confirm_negative": True,
        }, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_old_manual_supplier_payment_stays_editable(self):
        entry = CashEntry.objects.create(
            account="CASH", kind="OUT", article="SUPPLY", amount=Decimal("100"),
        )
        r = self.client.patch(f"{CASH}{entry.id}/", {"note": "старая запись", "confirm_negative": True},
                              format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_opening_balance_can_be_entered(self):
        r = self.client.post(CASH, {
            "account": "BANK", "kind": "IN", "article": "OPENING", "amount": "150000",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(CashEntry.balance(CashEntry.Account.BANK), Decimal("150000"))


class SupplierPaymentReversalTests(AdminCase):
    def setUp(self):
        super().setUp()
        self.sheet = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            price_per_sqm=Decimal("1470"),
        )

    def test_cancelled_document_keeps_its_payment_and_gets_a_counter_entry(self):
        r = self.client.post("/api/warehouse/supplies/", {
            "received_on": "2026-09-10", "paid_amount": "48000", "paid_account": "CASH",
            "lines": [{"material": self.sheet.id, "form": "SHEET", "width": "1.2",
                       "height": "2.4", "sheet_count": "10", "cost": "48000"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        r = self.client.delete(f"/api/warehouse/supplies/{r.data['id']}/")
        self.assertIn(r.status_code, (200, 204))
        paid = CashEntry.objects.get(kind="OUT")
        back = CashEntry.objects.get(kind="IN")
        self.assertEqual(paid.happened_on, date(2026, 9, 10))
        self.assertEqual(back.happened_on, self.today)
        self.assertEqual((back.amount, back.article), (Decimal("48000"), "SUPPLY"))
        self.assertEqual(CashEntry.balance(), Decimal("0"))

    def test_deleted_material_reverses_its_lot_payments(self):
        r = self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": self.sheet.id, "form": "SHEET", "width": "1", "height": "2",
            "sheet_count": "5", "purchase_cost": "12000", "payment": "BANK",
            "received_on": "2026-09-01",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        r = self.client.delete(f"/api/warehouse/materials/{self.sheet.id}/")
        self.assertIn(r.status_code, (200, 204))
        self.assertFalse(Roll.objects.exists())
        self.assertEqual(CashEntry.objects.count(), 2)
        self.assertEqual(CashEntry.balance(CashEntry.Account.BANK), Decimal("0"))


class RevenueRecognitionTests(AdminCase):
    def setUp(self):
        super().setUp()
        self.material = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("10"), purchase_price=Decimal("4"),
        )

    def checkout(self, method):
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": method,
            "items": [{"type": "MATERIAL", "material": self.material.id, "quantity": 5}],
            **({"pay_full": True} if method != "ONLINE" else {}),
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return Receipt.objects.get(pk=r.data["id"])

    def test_ordinary_order_is_recognized_on_its_order_date(self):
        receipt = self.checkout("CASH")
        self.assertEqual(receipt.revenue_recognized_at, receipt.created_at)

    def test_moving_the_order_date_moves_recognition(self):
        receipt = self.checkout("CASH")
        day = self.today - timedelta(days=3)
        r = self.client.patch(f"/api/sales/receipts/{receipt.id}/", {"order_date": day.isoformat()},
                              format="json")
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.revenue_recognized_at, receipt.created_at)
        self.assertEqual(timezone.localtime(receipt.revenue_recognized_at).date(), day)

    def test_unpaid_online_order_is_not_a_sale(self):
        receipt = self.checkout("ONLINE")
        self.assertIsNone(receipt.revenue_recognized_at)
        self.assertFalse(receipt.stock_deducted)

    def test_confirmation_recognizes_and_deducts_stock_together(self):
        receipt = self.checkout("ONLINE")
        ordered_at = receipt.created_at
        sale_service.confirm_payment(receipt)
        receipt.refresh_from_db()
        self.assertIsNotNone(receipt.revenue_recognized_at)
        self.assertTrue(receipt.stock_deducted)
        self.assertEqual(receipt.created_at, ordered_at)
        self.material.refresh_from_db()
        self.assertEqual(self.material.quantity, Decimal("95"))
        self.assertEqual(sum(i.cost_total for i in receipt.items.all()), Decimal("20"))

    def test_online_order_paid_at_the_counter_is_recognized_too(self):
        receipt = self.checkout("ONLINE")
        sale_service.apply_payment(receipt, None, user=self.admin, method="CASH")
        receipt.refresh_from_db()
        self.assertIsNotNone(receipt.revenue_recognized_at)
        self.assertTrue(receipt.stock_deducted)

    def test_partial_payment_of_online_order_is_an_advance(self):
        receipt = self.checkout("ONLINE")
        sale_service.apply_payment(receipt, Decimal("20"), user=self.admin, method="CASH")
        receipt.refresh_from_db()
        self.assertIsNone(receipt.revenue_recognized_at)
        self.assertFalse(receipt.stock_deducted)
