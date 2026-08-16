from datetime import date, datetime
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt

from .models import ExpenseEntry, ExpenseKind


def kind(code):
    """Встроенный вид расхода по коду — их создаёт миграция."""
    return ExpenseKind.objects.get(code=code)


def entry(code, amount, day, name=""):
    return ExpenseEntry.objects.create(
        kind=kind(code), amount=Decimal(amount), spent_at=day, name=name
    )


class DailyReportTests(APITestCase):
    """GET /api/finance/daily/ — day-by-day P&L used for the "which day was I
    in the red" chart. Revenue/expenses are grouped by their own dated fields;
    fixed monthly costs (no date of their own) are split evenly across the
    days of the shown month.
    """

    URL = "/api/finance/daily/"

    def setUp(self):
        self.admin = User.objects.create_user(username="d_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="d_store", password="x", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(self.admin)

    def _receipt(self, *, day, payment_status=Receipt.PaymentStatus.PAID,
                 status=Receipt.Status.COMPLETED, total="0", amount_paid="0"):
        r = Receipt.objects.create(
            payment_status=payment_status, status=status,
            total_price=Decimal(total), amount_paid=Decimal(amount_paid),
        )
        # created_at is auto_now_add — must be back-dated via a plain UPDATE.
        # An aware midnight (not a bare date) avoids Django's naive-datetime
        # warning under USE_TZ=True and round-trips correctly through TruncDate.
        Receipt.objects.filter(pk=r.pk).update(
            created_at=timezone.make_aware(datetime.combine(day, datetime.min.time()))
        )
        return r

    def _expense(self, *, day, amount, code="VAR_OTHER"):
        return entry(code, amount, day)

    def _fixed(self, *, day, amount, code="RENT"):
        """Постоянный расход записью: месячная сумма берётся из записей,
        попавших в показываемый месяц."""
        return entry(code, amount, day)

    def _rows(self, year, month):
        r = self.client.get(self.URL, {"year": year, "month": month})
        self.assertEqual(r.status_code, 200, r.data)
        return r.data, {row["day"]: row for row in r.data["rows"]}

    # ---- revenue grouping ---------------------------------------------------
    def test_revenue_grouped_by_day_counts_whole_orders(self):
        """Выручка дня — ВСЕ заказы этого дня, а не только оплаченные.

        Раньше день, отработанный в долг, показывал только предоплату (или ноль)
        и выглядел убыточным: материал списан, работа сделана, а выручки нет.
        """
        self._receipt(day=date(2026, 6, 5), payment_status=Receipt.PaymentStatus.PAID,
                      total="1000", amount_paid="1000")
        self._receipt(day=date(2026, 6, 5), payment_status=Receipt.PaymentStatus.PENDING,
                      total="500", amount_paid="200")
        self._receipt(day=date(2026, 6, 6), payment_status=Receipt.PaymentStatus.PAID,
                      total="300", amount_paid="300")
        _, rows = self._rows(2026, 6)
        self.assertEqual(Decimal(str(rows[5]["revenue"])), Decimal("1500"))  # 1000 + 500
        self.assertEqual(Decimal(str(rows[6]["revenue"])), Decimal("300"))
        self.assertEqual(Decimal(str(rows[7]["revenue"])), Decimal("0"))

    def test_cancelled_receipt_excluded(self):
        self._receipt(day=date(2026, 6, 5), payment_status=Receipt.PaymentStatus.PAID,
                      status=Receipt.Status.CANCELLED, total="9000", amount_paid="9000")
        _, rows = self._rows(2026, 6)
        self.assertEqual(Decimal(str(rows[5]["revenue"])), Decimal("0"))

    # ---- expenses grouping ---------------------------------------------------
    def test_expenses_grouped_by_day(self):
        self._expense(day=date(2026, 6, 10), amount="150")
        self._expense(day=date(2026, 6, 10), amount="50")
        self._expense(day=date(2026, 6, 11), amount="20")
        _, rows = self._rows(2026, 6)
        self.assertEqual(Decimal(str(rows[10]["variable"])), Decimal("200"))
        self.assertEqual(Decimal(str(rows[11]["variable"])), Decimal("20"))

    # ---- fixed-cost proration --------------------------------------------
    def test_fixed_costs_split_evenly_across_month(self):
        # June has 30 days. rent 300, everything else 0 -> 10/day.
        self._fixed(day=date(2026, 6, 1), amount="300")
        data, rows = self._rows(2026, 6)
        self.assertEqual(data["days_in_month"], 30)
        for row in data["rows"]:
            self.assertEqual(Decimal(str(row["fixed_share"])), Decimal("10"))
        # No revenue anywhere -> every day is exactly its fixed share in the red.
        self.assertEqual(Decimal(str(rows[1]["profit"])), Decimal("-10"))
        self.assertEqual(Decimal(str(data["totals"]["fixed"])), Decimal("300"))

    def test_profit_positive_and_negative_days(self):
        self._fixed(day=date(2026, 6, 1), amount="300")  # 10/day in June
        self._receipt(day=date(2026, 6, 1), payment_status=Receipt.PaymentStatus.PAID,
                      total="100", amount_paid="100")
        self._expense(day=date(2026, 6, 2), amount="50")
        _, rows = self._rows(2026, 6)
        self.assertEqual(Decimal(str(rows[1]["profit"])), Decimal("90"))    # 100 - 0 - 10
        self.assertEqual(Decimal(str(rows[2]["profit"])), Decimal("-60"))   # 0 - 50 - 10
        self.assertEqual(Decimal(str(rows[3]["profit"])), Decimal("-10"))   # 0 - 0 - 10

    def test_totals_match_sum_of_rows(self):
        self._fixed(day=date(2026, 6, 1), amount="300")
        self._receipt(day=date(2026, 6, 1), payment_status=Receipt.PaymentStatus.PAID,
                      total="1000", amount_paid="1000")
        data, _ = self._rows(2026, 6)
        totals = data["totals"]
        self.assertEqual(Decimal(str(totals["revenue"])), Decimal("1000"))
        self.assertEqual(Decimal(str(totals["fixed"])), Decimal("300"))
        self.assertEqual(Decimal(str(totals["profit"])), Decimal("700"))  # 1000 - 0 - 300

    # ---- future days ---------------------------------------------------
    def test_future_days_have_null_profit_and_excluded_from_totals(self):
        # A day that hasn't happened yet must not show as "in the red" just
        # because it hasn't earned back its (unlived) share of rent yet.
        now = timezone.localdate()
        self._fixed(day=now.replace(day=1), amount="310")
        data, rows = self._rows(now.year, now.month)
        for day_num, row in rows.items():
            if day_num > now.day:
                self.assertIsNone(row["profit"], f"day {day_num} is in the future")
            else:
                self.assertIsNotNone(row["profit"], f"day {day_num} is past/today")
        share = Decimal(str(rows[1]["fixed_share"]))
        expected = -(share * now.day)  # only elapsed days contribute their share
        actual = Decimal(str(data["totals"]["profit"]))
        self.assertLess(abs(actual - expected), Decimal("0.05"))

    # ---- month/year boundaries -----------------------------------------
    def test_december_does_not_leak_into_next_january(self):
        self._receipt(day=date(2027, 1, 1), payment_status=Receipt.PaymentStatus.PAID,
                      total="500", amount_paid="500")
        self._receipt(day=date(2026, 12, 31), payment_status=Receipt.PaymentStatus.PAID,
                      total="700", amount_paid="700")
        data, rows = self._rows(2026, 12)
        self.assertEqual(data["days_in_month"], 31)
        self.assertEqual(Decimal(str(rows[31]["revenue"])), Decimal("700"))
        self.assertEqual(Decimal(str(data["totals"]["revenue"])), Decimal("700"))  # Jan 1 excluded

    def test_february_leap_vs_common_year_day_count(self):
        data, _ = self._rows(2028, 2)  # leap year
        self.assertEqual(data["days_in_month"], 29)
        data, _ = self._rows(2026, 2)  # common year
        self.assertEqual(data["days_in_month"], 28)

    # ---- defaults & validation -------------------------------------------
    def test_defaults_to_current_month_when_no_params(self):
        r = self.client.get(self.URL)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(len(r.data["rows"]), r.data["days_in_month"])

    def test_invalid_month_rejected(self):
        r = self.client.get(self.URL, {"year": 2026, "month": 13})
        self.assertEqual(r.status_code, 400)

    def test_invalid_year_type_rejected(self):
        r = self.client.get(self.URL, {"year": "abc", "month": 6})
        self.assertEqual(r.status_code, 400)

    # ---- today marker -------------------------------------------------------
    def test_today_marker_set_only_for_current_month(self):
        now = timezone.localdate()
        data, _ = self._rows(now.year, now.month)
        self.assertEqual(data["today"], now.isoformat())
        data, _ = self._rows(2019, 1)
        self.assertIsNone(data["today"])

    # ---- permissions -------------------------------------------------------
    def test_storekeeper_forbidden(self):
        self.client.force_authenticate(self.store)
        r = self.client.get(self.URL, {"year": 2026, "month": 6})
        self.assertEqual(r.status_code, 403)

    # ---- зарплаты тоже размазываются по дням --------------------------------
    def test_salary_counts_towards_daily_fixed_share(self):
        self._fixed(day=date(2026, 6, 1), amount="300")
        entry("SALARY", "600", date(2026, 6, 15), name="Мастер")
        data, _ = self._rows(2026, 6)
        # (300 + 600) / 30 дней = 30 в день, независимо от даты самой выплаты.
        self.assertEqual(Decimal(str(data["totals"]["fixed"])), Decimal("900"))
        for row in data["rows"]:
            self.assertEqual(Decimal(str(row["fixed_share"])), Decimal("30"))

    def test_fixed_expense_of_other_month_not_counted(self):
        self._fixed(day=date(2026, 5, 31), amount="3000")
        data, _ = self._rows(2026, 6)
        self.assertEqual(Decimal(str(data["totals"]["fixed"])), Decimal("0"))


def row_by_code(block, code):
    """Строка блока отчёта по коду вида расхода."""
    return next(r for r in block["rows"] if r["code"] == code)


class ExpenseEntryAPITests(APITestCase):
    """Траты по видам записями + фильтры периода."""

    URL = "/api/finance/expense-entries/"

    def setUp(self):
        self.admin = User.objects.create_user(username="f_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="f_store", password="x", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(self.admin)

    def test_create_records_author_and_kind(self):
        r = self.client.post(self.URL, {
            "kind": kind("RENT").id, "name": "Аренда за июль",
            "amount": "30000", "spent_at": "2026-07-01",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["kind_name"], "Аренда цеха")
        self.assertEqual(ExpenseEntry.objects.get().created_by, self.admin)

    def test_month_filter_selects_only_that_month(self):
        entry("RENT", "100", date(2026, 6, 1))
        entry("RENT", "200", date(2026, 7, 1))
        r = self.client.get(self.URL, {"year": 2026, "month": 7})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(len(r.data), 1)
        self.assertEqual(Decimal(r.data[0]["amount"]), Decimal("200"))

    def test_date_range_filter(self):
        entry("RENT", "100", date(2026, 6, 1))
        entry("RENT", "200", date(2026, 7, 1))
        r = self.client.get(self.URL, {"date_from": "2026-06-15", "date_to": "2026-07-31"})
        self.assertEqual(len(r.data), 1)
        self.assertEqual(Decimal(r.data[0]["amount"]), Decimal("200"))

    def test_broken_month_filter_is_ignored_not_500(self):
        entry("RENT", "100", date(2026, 6, 1))
        r = self.client.get(self.URL, {"year": "abc", "month": "99"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(len(r.data), 1)

    def test_kind_filter(self):
        entry("RENT", "100", date(2026, 6, 1))
        entry("INTERNET", "30", date(2026, 6, 1))
        r = self.client.get(self.URL, {"kind": kind("RENT").id})
        self.assertEqual(len(r.data), 1)

    def test_salary_keeps_employee_name(self):
        r = self.client.post(self.URL, {
            "kind": kind("SALARY").id, "name": "Азамат",
            "amount": "25000", "spent_at": "2026-07-05",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(ExpenseEntry.objects.get().name, "Азамат")

    def test_entries_are_not_paginated(self):
        """Диалог вида показывает месяц целиком: страница на 25 строк молча
        обрезала бы его, и итог разошёлся бы с отчётом."""
        for i in range(30):
            entry("RENT", "10", date(2026, 6, 1))
        r = self.client.get(self.URL)
        self.assertEqual(len(r.data), 30)

    def test_storekeeper_cannot_see_or_add(self):
        self.client.force_authenticate(self.store)
        self.assertEqual(self.client.get(self.URL).status_code, 403)
        self.assertEqual(self.client.get("/api/finance/expense-kinds/").status_code, 403)

    def test_report_sums_records_within_period(self):
        entry("RENT", "100", date(2026, 6, 10))
        entry("INTERNET", "30", date(2026, 6, 12))
        entry("RENT", "999", date(2026, 8, 1))
        entry("SALARY", "70", date(2026, 6, 20), name="Мастер")
        r = self.client.get("/api/finance/report/", {"date_from": "2026-06-01", "date_to": "2026-06-30"})
        self.assertEqual(r.status_code, 200, r.data)
        fixed = r.data["fixed"]
        self.assertEqual(Decimal(str(row_by_code(fixed, "RENT")["amount"])), Decimal("100"))
        self.assertEqual(Decimal(str(row_by_code(fixed, "INTERNET")["amount"])), Decimal("30"))
        self.assertEqual(Decimal(str(row_by_code(fixed, "SALARY")["amount"])), Decimal("70"))
        # Августовская аренда в июньский период не попадает.
        self.assertEqual(Decimal(str(fixed["total"])), Decimal("200"))


class ExpenseKindAPITests(APITestCase):
    """Справочник видов расхода: админ заводит свои строки отчёта."""

    URL = "/api/finance/expense-kinds/"

    def setUp(self):
        self.admin = User.objects.create_user(username="k_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)

    def test_custom_kind_appears_in_its_block(self):
        r = self.client.post(self.URL, {"name": "Реклама", "block": "FIXED"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertFalse(r.data["is_builtin"])
        entry_kind = ExpenseKind.objects.get(name="Реклама")
        ExpenseEntry.objects.create(kind=entry_kind, amount=Decimal("500"), spent_at=date(2026, 6, 5))
        rep = self.client.get("/api/finance/report/", {"date_from": "2026-06-01", "date_to": "2026-06-30"})
        row = next(x for x in rep.data["fixed"]["rows"] if x["name"] == "Реклама")
        self.assertEqual(Decimal(str(row["amount"])), Decimal("500"))
        self.assertEqual(Decimal(str(rep.data["fixed"]["total"])), Decimal("500"))

    def test_kind_out_of_profit_is_shown_but_not_in_total(self):
        """Как оборудование: строка видна, а прибыль не уменьшает."""
        r = self.client.post(
            self.URL, {"name": "Новый станок", "block": "VARIABLE", "in_profit": False}, format="json"
        )
        self.assertEqual(r.status_code, 201, r.data)
        ExpenseEntry.objects.create(
            kind=ExpenseKind.objects.get(name="Новый станок"),
            amount=Decimal("300000"), spent_at=date(2026, 6, 5),
        )
        rep = self.client.get("/api/finance/report/", {"date_from": "2026-06-01", "date_to": "2026-06-30"})
        row = next(x for x in rep.data["variable"]["rows"] if x["name"] == "Новый станок")
        self.assertEqual(Decimal(str(row["amount"])), Decimal("300000"))
        self.assertEqual(Decimal(str(rep.data["variable"]["total"])), Decimal("0"))
        self.assertEqual(Decimal(str(rep.data["investments"]["total"])), Decimal("300000"))

    def test_own_kind_allowed_in_every_block(self):
        """Свой вид можно завести в любом из трёх блоков, включая «Материалы»:
        итог там считается как Σ строк с флагом, поэтому лишняя строка формулу
        не ломает."""
        for block in ("MATERIALS", "FIXED", "VARIABLE"):
            r = self.client.post(self.URL, {"name": f"Своё {block}", "block": block}, format="json")
            self.assertEqual(r.status_code, 201, r.data)

    def test_unknown_block_rejected(self):
        r = self.client.post(self.URL, {"name": "Что-то", "block": "NOPE"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_builtin_kind_cannot_be_deleted_but_can_be_renamed(self):
        rent = kind("RENT")
        self.assertEqual(self.client.delete(f"{self.URL}{rent.id}/").status_code, 400)
        r = self.client.patch(f"{self.URL}{rent.id}/", {"name": "Аренда помещения"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        rent.refresh_from_db()
        self.assertEqual(rent.name, "Аренда помещения")

    def test_builtin_kind_cannot_change_block(self):
        r = self.client.patch(f"{self.URL}{kind('RENT').id}/", {"block": "VARIABLE"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_kind_with_entries_is_hidden_not_deleted(self):
        """Иначе суммы прошлых месяцев поехали бы задним числом."""
        self.client.post(self.URL, {"name": "Реклама", "block": "FIXED"}, format="json")
        ad = ExpenseKind.objects.get(name="Реклама")
        ExpenseEntry.objects.create(kind=ad, amount=Decimal("500"), spent_at=date(2026, 6, 5))
        r = self.client.delete(f"{self.URL}{ad.id}/")
        self.assertEqual(r.status_code, 200, r.data)
        ad.refresh_from_db()
        self.assertTrue(ad.is_archived)
        self.assertEqual(ExpenseEntry.objects.filter(kind=ad).count(), 1)
        # Скрытый вид пропадает из отчёта и из списка.
        rep = self.client.get("/api/finance/report/", {"date_from": "2026-06-01", "date_to": "2026-06-30"})
        self.assertFalse([x for x in rep.data["fixed"]["rows"] if x["name"] == "Реклама"])
        self.assertFalse([x for x in self.client.get(self.URL).data if x["name"] == "Реклама"])
        self.assertTrue([x for x in self.client.get(self.URL, {"archived": "1"}).data if x["name"] == "Реклама"])

    def test_kind_without_entries_is_deleted_outright(self):
        self.client.post(self.URL, {"name": "Опечатка", "block": "FIXED"}, format="json")
        typo = ExpenseKind.objects.get(name="Опечатка")
        self.assertEqual(self.client.delete(f"{self.URL}{typo.id}/").status_code, 204)
        self.assertFalse(ExpenseKind.objects.filter(pk=typo.pk).exists())

    def test_hidden_kind_can_be_restored_and_takes_no_new_entries(self):
        self.client.post(self.URL, {"name": "Реклама", "block": "FIXED"}, format="json")
        ad = ExpenseKind.objects.get(name="Реклама")
        ad.is_archived = True
        ad.save(update_fields=["is_archived"])
        blocked = self.client.post(
            "/api/finance/expense-entries/",
            {"kind": ad.id, "amount": "100", "spent_at": "2026-06-05"}, format="json",
        )
        self.assertEqual(blocked.status_code, 400, blocked.data)
        self.assertEqual(self.client.post(f"{self.URL}{ad.id}/restore/").status_code, 200)
        ad.refresh_from_db()
        self.assertFalse(ad.is_archived)

    def test_same_name_twice_gets_distinct_codes(self):
        self.client.post(self.URL, {"name": "Реклама", "block": "FIXED"}, format="json")
        self.client.post(self.URL, {"name": "Реклама", "block": "VARIABLE"}, format="json")
        codes = set(ExpenseKind.objects.filter(name="Реклама").values_list("code", flat=True))
        self.assertEqual(len(codes), 2)


class CogsTests(APITestCase):
    """Себестоимость проданного: фиксируется при списании (FIFO) и уменьшает прибыль."""

    REPORT = "/api/finance/report/"

    def setUp(self):
        from warehouse.models import Material
        from warehouse.rolls import receive_lot

        self.admin = User.objects.create_user(username="c_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)

        self.mat = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM,
            is_roll_material=True, price_per_sqm=Decimal("1000"),
        )
        # Партия: 10 кв.м за 2000 → 200 сом/кв.м себестоимость.
        receive_lot(self.mat, form="ROLL", width=Decimal("1"), length=Decimal("10"),
                    purchase_cost=Decimal("2000"))

    def _sell(self, area="2", price="1000", paid=None):
        """Продажа. paid=None → оплачена полностью (выручка сразу в отчёте);
        касса теперь требует указать сумму явно, пустое поле = долг."""
        from sales.sale_service import create_sale
        receipt = create_sale(
            client=None, cashier=self.admin, payment_method="CASH",
            items_data=[{
                "type": "MATERIAL", "material": self.mat,
                "quantity": Decimal(area), "mode": "SQM",
                "material_price": Decimal(price),
            }],
            amount_paid=paid,
        )
        if paid is None:
            receipt.amount_paid = receipt.total_price
            receipt.payment_status = "PAID"
            receipt.save(update_fields=["amount_paid", "payment_status"])
        return receipt

    def _report(self):
        r = self.client.get(self.REPORT)
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def test_cost_recorded_from_fifo_lot(self):
        receipt = self._sell(area="2")
        item = receipt.items.get()
        # 2 кв.м × 200 сом/кв.м = 400.
        self.assertEqual(Decimal(str(item.cost_total)), Decimal("400"))

    def test_profit_now_accounts_for_material_cost(self):
        self._sell(area="2", price="1000")   # выручка 2000, себестоимость 400
        data = self._report()
        self.assertEqual(Decimal(str(data["revenue"])), Decimal("2000"))
        self.assertEqual(Decimal(str(data["cogs"])), Decimal("400"))
        self.assertEqual(Decimal(str(data["gross_margin"])), Decimal("1600"))
        # Себестоимость — справочная цифра: в прибыль идёт итог блока
        # «Материалы» (решение заказчика), поэтому 2000 − cogs здесь не ждём.
        self.assertEqual(
            Decimal(str(data["profit"])),
            Decimal(str(data["revenue"])) - Decimal(str(data["total_expenses"])),
        )

    def test_returned_line_drops_out_of_cogs(self):
        from sales.sale_service import refund_receipt

        receipt = self._sell(area="2")
        self.assertEqual(Decimal(str(self._report()["cogs"])), Decimal("400"))
        refund_receipt(receipt, user=self.admin)
        # Материал вернулся на склад — его себестоимость больше не расход.
        self.assertEqual(Decimal(str(self._report()["cogs"])), Decimal("0"))

    def test_cost_is_a_snapshot_not_current_price(self):
        from warehouse.rolls import receive_lot

        self._sell(area="2")  # по 200 сом/кв.м
        # Новая партия ВДВОЕ дороже — прошлые продажи это не должно двигать.
        receive_lot(self.mat, form="ROLL", width=Decimal("1"), length=Decimal("10"),
                    purchase_cost=Decimal("4000"))
        self.assertEqual(Decimal(str(self._report()["cogs"])), Decimal("400"))

    def test_fifo_spans_two_lots(self):
        from warehouse.rolls import receive_lot

        # Вторая партия дороже; продаём больше, чем осталось в первой.
        receive_lot(self.mat, form="ROLL", width=Decimal("1"), length=Decimal("10"),
                    purchase_cost=Decimal("5000"))  # 500/кв.м
        receipt = self._sell(area="12")
        # 10 кв.м из первой (×200) + 2 из второй (×500) = 2000 + 1000 = 3000.
        self.assertEqual(Decimal(str(receipt.items.get().cost_total)), Decimal("3000"))

    def test_daily_report_uses_the_same_profit_formula_as_the_tiles(self):
        """График по дням и плитки сверху должны считать ОДНО И ТО ЖЕ.

        Раньше себестоимость проданного попадала в дневные расходы, а в плитки —
        нет, и на одном экране стояли две «Прибыли», которые спорили друг с
        другом (6 924 сверху и 939 в графике). Правило заказчика одно: прибыль =
        выручка − (Материалы + Постоянные + Переменные), себестоимость
        справочная. График живёт по нему же.
        """
        self._sell(area="2")  # себестоимость 400, записей трат нет
        today = timezone.localdate()
        r = self.client.get("/api/finance/daily/", {"year": today.year, "month": today.month})
        self.assertEqual(r.status_code, 200, r.data)
        row = next(x for x in r.data["rows"] if x["day"] == today.day)
        # Себестоимость (400) в расходы дня НЕ попадает. А вот ЗАКУП по приходу
        # партии (2000, сегодня) — попадает: плитки считают его сами по приходам
        # на склад, и график обязан видеть ту же цифру, иначе на одном экране
        # снова две «Прибыли» (−14 265 сверху и +3 735 в графике).
        self.assertEqual(Decimal(str(row["variable"])), Decimal("2000"))

        # Итог графика сходится с плитками месяца: те же расходы, та же прибыль.
        report = self._report()
        totals = r.data["totals"]
        self.assertEqual(Decimal(str(totals["variable"])), Decimal(str(report["total_expenses"])))
        self.assertEqual(Decimal(str(totals["variable"])), Decimal("2000"))
        self.assertEqual(Decimal(str(totals["profit"])), Decimal(str(report["profit"])))
        self.assertEqual(Decimal(str(totals["revenue"])), Decimal(str(report["revenue"])))

    def test_dated_expense_still_lands_on_its_day(self):
        """Убрав себестоимость, нельзя было потерять сами траты: запись с датой
        обязана попасть в свой день."""
        from finance.models import ExpenseEntry, ExpenseKind

        kind = ExpenseKind.objects.filter(block=ExpenseKind.Block.VARIABLE).first()
        today = timezone.localdate()
        ExpenseEntry.objects.create(kind=kind, amount=Decimal("700"), spent_at=today)
        r = self.client.get("/api/finance/daily/", {"year": today.year, "month": today.month})
        row = next(x for x in r.data["rows"] if x["day"] == today.day)
        # 700 трата + 2000 закуп партии из setUp — как в плитках.
        self.assertEqual(Decimal(str(row["variable"])), Decimal("2700"))
        self.assertEqual(
            Decimal(str(r.data["totals"]["variable"])),
            Decimal(str(self._report()["total_expenses"])),
        )


class MaterialsBlockTests(APITestCase):
    """Блок «Материалы»: расход материала = закуп + транспорт (+ свои виды).

    Остатков на начало и на конец в блоке НЕТ — заказчик попросил убрать обе
    строки (2026-08-14). До этого расход выводился через склад
    («начало + закуп + транспорт − конец»), и пока остатки не заполнены,
    формула уходила в минус на всю стоимость склада.
    """

    URL = "/api/finance/report/"

    def setUp(self):
        from warehouse.models import Material

        self.admin = User.objects.create_user(username="m_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        # Закуп и долг материала — виды расхода с записями, как остальные
        # строки отчёта, а не поля настроек.
        entry("MATERIAL_PURCHASE", "5000", timezone.localdate())
        entry("MATERIAL_DEBT", "700", timezone.localdate())
        # Склад на итог блока больше не влияет — материал заведён, чтобы это и
        # проверить.
        Material.objects.create(
            name="Акрил", quantity=Decimal("10"),
            purchase_price=Decimal("200"),
        )

    def _report(self):
        r = self.client.get(self.URL)
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def test_materials_total_formula(self):
        entry("TRANSPORT", "300", timezone.localdate())
        data = self._report()
        m = data["materials"]
        self.assertEqual(
            Decimal(str(row_by_code(m, "MATERIAL_PURCHASE")["amount"])), Decimal("5000")
        )
        self.assertEqual(Decimal(str(row_by_code(m, "TRANSPORT")["amount"])), Decimal("300"))
        # 5000 + 300 = 5300; склад (10 × 200) в итог не входит.
        self.assertEqual(Decimal(str(m["total"])), Decimal("5300"))

    def test_block_has_no_stock_balances(self):
        """Остатков в блоке нет ни строками, ни в ответе API."""
        m = self._report()["materials"]
        for key in ("stock_start", "stock_end", "needs_setup"):
            self.assertNotIn(key, m, key)

    def test_material_debt_is_informational_not_in_total(self):
        """Долг материала — строка со снятым флагом «входит в прибыль»:
        видна в блоке, но в итог не идёт (материал в долг уже в закупе)."""
        m = self._report()["materials"]
        debt = row_by_code(m, "MATERIAL_DEBT")
        self.assertEqual(Decimal(str(debt["amount"])), Decimal("700"))
        self.assertFalse(debt["in_profit"])
        # Только закуп: долг не добавлен.
        self.assertEqual(Decimal(str(m["total"])), Decimal("5000"))

    def test_transport_counted_once_not_in_variable(self):
        entry("TRANSPORT", "300", timezone.localdate())
        data = self._report()
        self.assertEqual(
            Decimal(str(row_by_code(data["materials"], "TRANSPORT")["amount"])), Decimal("300")
        )
        # В переменных расходах транспорта больше нет — иначе задвоился бы.
        self.assertEqual(Decimal(str(data["variable"]["total"])), Decimal("0"))

    def test_own_kind_can_be_added_to_materials_block(self):
        """Свой вид в «Материалах» складывается в расход материала."""
        r = self.client.post(
            "/api/finance/expense-kinds/",
            {"name": "Растаможка", "block": "MATERIALS"}, format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        ExpenseEntry.objects.create(
            kind=ExpenseKind.objects.get(name="Растаможка"),
            amount=Decimal("500"), spent_at=timezone.localdate(),
        )
        m = self._report()["materials"]
        # 5000 + 500 = 5500
        self.assertEqual(Decimal(str(m["total"])), Decimal("5500"))

    def test_investments_shown_but_not_in_profit(self):
        entry("EQUIPMENT", "9000", timezone.localdate())
        entry("CUTTER", "100", timezone.localdate())
        data = self._report()
        # Оборудование видно в блоке…
        self.assertEqual(
            Decimal(str(row_by_code(data["variable"], "EQUIPMENT")["amount"])), Decimal("9000")
        )
        # …но в подытог переменных (который идёт в прибыль) не входит.
        self.assertEqual(Decimal(str(data["variable"]["total"])), Decimal("100"))
        self.assertEqual(Decimal(str(data["investments"]["total"])), Decimal("9000"))

    def test_profit_uses_materials_block_not_cogs(self):
        data = self._report()
        expected = (
            Decimal(str(data["materials"]["total"]))
            + Decimal(str(data["fixed"]["total"]))
            + Decimal(str(data["variable"]["total"]))
        )
        self.assertEqual(Decimal(str(data["total_expenses"])), expected)
        self.assertEqual(
            Decimal(str(data["profit"])),
            Decimal(str(data["revenue"])) - expected,
        )
        # Себестоимость осталась справочной цифрой и в расходы не добавлена.
        self.assertIn("cogs", data)

    def test_full_stock_with_no_purchases_gives_zero_not_minus(self):
        """Полный склад и ни одной траты за месяц — расход материала ноль.

        Раньше в этом же случае формула («начало + закуп − конец») уходила в
        минус на всю стоимость склада, и отрицательный расход пришлось бы
        отдельно не пускать в прибыль.
        """
        ExpenseEntry.objects.all().delete()
        m = self._report()["materials"]
        self.assertEqual(Decimal(str(m["total"])), Decimal("0"))


class MaterialStockReportTests(APITestCase):
    """GET /api/finance/material-report/ — складская таблица как в Excel
    заказчика. Формула его листа: остаток на начало (вводится РУКАМИ) +
    поступление − проданные = остаток на конец."""

    URL = "/api/finance/material-report/"
    OPENING_URL = "/api/warehouse/month-openings/"

    def setUp(self):
        from warehouse.models import Material, ProductionSite

        self.admin = User.objects.create_user(username="ms_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        # Лист 1×2 м = 2 кв.м. Заказчик ведёт склад листами, поэтому отчёт
        # пересчитывает кв.м в листы.
        self.material = Material.objects.create(
            name="Форекс 8мм", unit="SQM", is_roll_material=True,
            piece_area=Decimal("2"), purchase_price=Decimal("100"),
            price_per_sqm=Decimal("200"), piece_price=Decimal("400"),
            production=ProductionSite.objects.get(code="BISHKEK"),
        )

    def _opening(self, *, sheets, year, month):
        r = self.client.post(self.OPENING_URL, {
            "material": self.material.id, "year": year, "month": month, "quantity": str(sheets),
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        return r

    def _receive(self, *, sheets, day):
        """Приход партии задним числом — дата передаётся прямо в приёмку."""
        from warehouse.rolls import receive_lot

        receive_lot(
            self.material, form="SHEET", purchase_cost=Decimal("100") * sheets * 2,
            width=Decimal("1"), height=Decimal("2"), sheet_count=Decimal(sheets),
            received_at=timezone.make_aware(datetime.combine(day, datetime.min.time())),
        )

    def _sell(self, *, sheets, day, returned=False):
        """Продажа листами, задним числом."""
        from sales.models import TransactionItem
        from warehouse.stock import apply_stock_change

        receipt = Receipt.objects.create(
            payment_status=Receipt.PaymentStatus.PAID, status=Receipt.Status.COMPLETED,
            total_price=Decimal("400") * sheets, amount_paid=Decimal("400") * sheets,
            stock_deducted=True,
        )
        Receipt.objects.filter(pk=receipt.pk).update(
            created_at=timezone.make_aware(datetime.combine(day, datetime.min.time()))
        )
        TransactionItem.objects.create(
            receipt=receipt, type=TransactionItem.Type.MATERIAL, material=self.material,
            quantity=Decimal(sheets), price_per_item=Decimal("400"),
            sale_mode=TransactionItem.SaleMode.PIECE, is_returned=returned,
        )
        apply_stock_change(self.material, Decimal(-2) * sheets)
        if returned:
            apply_stock_change(self.material, Decimal(2) * sheets)
        return receipt

    def _row(self, **params):
        r = self.client.get(self.URL, params)
        self.assertEqual(r.status_code, 200, r.data)
        return next(x for x in r.data["rows"] if x["id"] == self.material.id), r.data

    def test_excel_formula(self):
        """Начало + поступление − проданные = конец, как в таблице заказчика."""
        self._opening(sheets=42, year=2026, month=6)
        self._receive(sheets=105, day=date(2026, 6, 10))
        self._sell(sheets=97, day=date(2026, 6, 15))
        row, _ = self._row(year=2026, month=6)
        self.assertEqual(Decimal(str(row["stock_start"])), Decimal("42"))
        self.assertEqual(Decimal(str(row["received_qty"])), Decimal("105"))
        self.assertEqual(Decimal(str(row["sold_qty"])), Decimal("97"))
        self.assertEqual(Decimal(str(row["stock_end"])), Decimal("50"))

    def test_opening_carries_forward_without_retyping(self):
        """Главное: остаток вписывают ОДИН раз, дальше система переносит сама."""
        self._opening(sheets=42, year=2026, month=6)
        self._receive(sheets=105, day=date(2026, 6, 10))
        self._sell(sheets=97, day=date(2026, 6, 15))
        june, _ = self._row(year=2026, month=6)
        self.assertEqual(Decimal(str(june["stock_end"])), Decimal("50"))
        # Июль руками не вводили — начало должно приехать с конца июня.
        july, _ = self._row(year=2026, month=7)
        self.assertEqual(Decimal(str(july["stock_start"])), Decimal("50"))
        self.assertFalse(july["opening_is_manual"])

    def test_carry_forward_spans_several_quiet_months(self):
        """Между вводом и текущим месяцем может быть тишина — перенос не рвётся."""
        self._opening(sheets=20, year=2026, month=1)
        self._receive(sheets=5, day=date(2026, 3, 4))
        self._sell(sheets=8, day=date(2026, 5, 6))
        row, _ = self._row(year=2026, month=9)
        # 20 + 5 − 8 = 17, месяцы без движений ничего не меняют.
        self.assertEqual(Decimal(str(row["stock_start"])), Decimal("17"))

    def test_carry_forward_crosses_the_year(self):
        self._opening(sheets=10, year=2026, month=12)
        self._receive(sheets=3, day=date(2026, 12, 20))
        row, _ = self._row(year=2027, month=1)
        self.assertEqual(Decimal(str(row["stock_start"])), Decimal("13"))

    def test_manual_value_beats_carry_forward(self):
        """Инвентаризация или закупка мимо склада правится одной цифрой."""
        self._opening(sheets=42, year=2026, month=6)
        self._sell(sheets=2, day=date(2026, 6, 15))
        # Перенос дал бы 40, но в июле вписали своё.
        self._opening(sheets=100, year=2026, month=7)
        july, _ = self._row(year=2026, month=7)
        self.assertEqual(Decimal(str(july["stock_start"])), Decimal("100"))
        self.assertTrue(july["opening_is_manual"])
        # И дальше цепочка идёт уже от исправленного значения.
        august, _ = self._row(year=2026, month=8)
        self.assertEqual(Decimal(str(august["stock_start"])), Decimal("100"))

    def test_without_any_manual_value_counts_from_zero(self):
        """Ничего не вписывали — считаем с нуля от первого движения."""
        self._receive(sheets=9, day=date(2026, 6, 10))
        self._sell(sheets=4, day=date(2026, 6, 12))
        july, _ = self._row(year=2026, month=7)
        self.assertEqual(Decimal(str(july["stock_start"])), Decimal("5"))

    def test_month_before_the_manual_anchor_is_untouched(self):
        """Вписали остаток в июне — май задним числом не выдумываем."""
        self._opening(sheets=42, year=2026, month=6)
        may, _ = self._row(year=2026, month=5)
        self.assertEqual(Decimal(str(may["stock_start"])), Decimal("0"))

    def test_manual_opening_is_flagged(self):
        self._opening(sheets=42, year=2026, month=6)
        june, _ = self._row(year=2026, month=6)
        self.assertEqual(Decimal(str(june["stock_start"])), Decimal("42"))
        self.assertTrue(june["opening_is_manual"])

    def test_opening_upsert_overwrites(self):
        self._opening(sheets=42, year=2026, month=6)
        self._opening(sheets=50, year=2026, month=6)
        from warehouse.models import MaterialMonthOpening
        self.assertEqual(MaterialMonthOpening.objects.count(), 1)
        row, _ = self._row(year=2026, month=6)
        self.assertEqual(Decimal(str(row["stock_start"])), Decimal("50"))

    def test_counted_in_sheets_and_production_shown(self):
        row, _ = self._row(year=2026, month=6)
        self.assertEqual(row["counted_in"], "SHEET")
        self.assertEqual(row["production"], "Бишкек")

    def test_returned_sale_not_counted_as_sold(self):
        self._opening(sheets=10, year=2026, month=6)
        self._sell(sheets=4, day=date(2026, 6, 15), returned=True)
        row, _ = self._row(year=2026, month=6)
        self.assertEqual(Decimal(str(row["sold_qty"])), Decimal("0"))
        self.assertEqual(Decimal(str(row["stock_end"])), Decimal("10"))

    def test_receipts_listed_by_day(self):
        """Колонки «поступление товар» в Excel — приходы по датам."""
        self._receive(sheets=5, day=date(2026, 6, 3))
        row, _ = self._row(year=2026, month=6)
        self.assertEqual(row["receipts"], [{"date": "2026-06-03", "qty": Decimal("5")}])

    def test_opening_month_reported_only_for_whole_month(self):
        """Вводить остаток на начало можно только когда период — целый месяц."""
        _, whole = self._row(year=2026, month=6)
        self.assertEqual(whole["opening_month"], {"year": 2026, "month": 6})
        _, partial = self._row(date_from="2026-06-05", date_to="2026-06-20")
        self.assertIsNone(partial["opening_month"])

    def test_storekeeper_cannot_set_opening(self):
        store = User.objects.create_user(username="ms_store", password="x", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(store)
        r = self.client.post(self.OPENING_URL, {
            "material": self.material.id, "year": 2026, "month": 6, "quantity": "5",
        }, format="json")
        self.assertEqual(r.status_code, 403)


class AutoComputedInputsTests(APITestCase):
    """Заказчик всю жизнь вбивал эти цифры в Excel руками. Система знает их
    сама — проверяем, что считает верно и что ручная правка всё ещё побеждает."""

    URL = "/api/finance/report/"
    JUNE = {"date_from": "2026-06-01", "date_to": "2026-06-30"}

    def setUp(self):
        from finance.models import FinanceSettings
        from warehouse.models import Material

        self.admin = User.objects.create_user(username="a_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        FinanceSettings.objects.update_or_create(pk=1, defaults={"stock_start": None})
        self.material = Material.objects.create(
            name="Форекс 8мм", unit="SQM", is_roll_material=True,
            piece_area=Decimal("2"), purchase_price=Decimal("50"),
        )

    def _report(self, **params):
        r = self.client.get(self.URL, params or self.JUNE)
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def _supply(self, *, qty, price, day):
        """Приход на склад с указанной ценой — то, из чего считается закуп."""
        from warehouse.models import InventoryLog
        from warehouse.stock import apply_stock_change

        apply_stock_change(
            self.material, Decimal(qty),
            log_type=InventoryLog.Type.SUPPLY, actual_price=Decimal(price),
            happened_at=timezone.make_aware(datetime.combine(day, datetime.min.time())),
        )

    # ---- закуп материала считается по приходам на склад ---------------------
    def test_purchase_comes_from_stock_intakes(self):
        self._supply(qty=100, price=30, day=date(2026, 6, 5))   # 3000
        self._supply(qty=20, price=50, day=date(2026, 6, 20))   # 1000
        row = row_by_code(self._report()["materials"], "MATERIAL_PURCHASE")
        self.assertEqual(Decimal(str(row["amount"])), Decimal("4000"))
        self.assertEqual(Decimal(str(row["auto_amount"])), Decimal("4000"))
        self.assertEqual(Decimal(str(row["manual_amount"])), Decimal("0"))

    def test_purchase_outside_the_period_not_counted(self):
        self._supply(qty=100, price=30, day=date(2026, 5, 31))
        row = row_by_code(self._report()["materials"], "MATERIAL_PURCHASE")
        self.assertEqual(Decimal(str(row["amount"])), Decimal("0"))

    def test_manual_purchase_record_adds_to_the_auto_sum(self):
        """Купили мимо склада — вписали записью, она прибавляется."""
        self._supply(qty=100, price=30, day=date(2026, 6, 5))
        entry("MATERIAL_PURCHASE", "500", date(2026, 6, 7), name="Купили за наличные")
        row = row_by_code(self._report()["materials"], "MATERIAL_PURCHASE")
        self.assertEqual(Decimal(str(row["auto_amount"])), Decimal("3000"))
        self.assertEqual(Decimal(str(row["manual_amount"])), Decimal("500"))
        self.assertEqual(Decimal(str(row["amount"])), Decimal("3500"))

    def test_intake_without_price_is_not_invented(self):
        from warehouse.models import InventoryLog
        from warehouse.stock import apply_stock_change

        apply_stock_change(self.material, Decimal("10"), log_type=InventoryLog.Type.SUPPLY)
        row = row_by_code(self._report()["materials"], "MATERIAL_PURCHASE")
        self.assertEqual(Decimal(str(row["amount"])), Decimal("0"))

    # ---- остатки в блок «Материалы» больше не входят ------------------------
    def test_month_opening_does_not_touch_the_materials_block(self):
        """Остаток на начало месяца из складского листа на расход не влияет.

        Он и раньше был единственным, ради чего блоку нужен был склад; после
        отказа от остатков (просьба заказчика, 2026-08-14) расход материала —
        это только траты периода.
        """
        from warehouse.models import MaterialMonthOpening

        MaterialMonthOpening.objects.create(
            material=self.material, year=2026, month=6, quantity=Decimal("8")
        )
        self._supply(qty=100, price=30, day=date(2026, 6, 5))  # 3000
        m = self._report()["materials"]
        self.assertEqual(Decimal(str(m["total"])), Decimal("3000"))
        self.assertNotIn("stock_start", m)
