from datetime import date, datetime
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt

from .models import Expense, FixedExpense, SalaryPayment


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

    def _expense(self, *, day, amount, category=Expense.Category.OTHER):
        e = Expense.objects.create(category=category, amount=Decimal(amount))
        Expense.objects.filter(pk=e.pk).update(spent_at=day)
        return e

    def _fixed(self, *, day, amount, category=FixedExpense.Category.RENT):
        """Постоянный расход записью — с переходом на FixedExpense месячная
        сумма берётся из записей, попавших в показываемый месяц."""
        return FixedExpense.objects.create(
            category=category, amount=Decimal(amount), spent_at=day,
        )

    def _rows(self, year, month):
        r = self.client.get(self.URL, {"year": year, "month": month})
        self.assertEqual(r.status_code, 200, r.data)
        return r.data, {row["day"]: row for row in r.data["rows"]}

    # ---- revenue grouping ---------------------------------------------------
    def test_revenue_grouped_by_day_paid_plus_prepay(self):
        self._receipt(day=date(2026, 6, 5), payment_status=Receipt.PaymentStatus.PAID,
                      total="1000", amount_paid="1000")
        self._receipt(day=date(2026, 6, 5), payment_status=Receipt.PaymentStatus.PENDING,
                      total="500", amount_paid="200")
        self._receipt(day=date(2026, 6, 6), payment_status=Receipt.PaymentStatus.PAID,
                      total="300", amount_paid="300")
        _, rows = self._rows(2026, 6)
        self.assertEqual(Decimal(str(rows[5]["revenue"])), Decimal("1200"))  # 1000 + 200 prepay
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
        SalaryPayment.objects.create(
            employee="Мастер", amount=Decimal("600"), paid_at=date(2026, 6, 15),
        )
        data, _ = self._rows(2026, 6)
        # (300 + 600) / 30 дней = 30 в день, независимо от даты самой выплаты.
        self.assertEqual(Decimal(str(data["totals"]["fixed"])), Decimal("900"))
        for row in data["rows"]:
            self.assertEqual(Decimal(str(row["fixed_share"])), Decimal("30"))

    def test_fixed_expense_of_other_month_not_counted(self):
        self._fixed(day=date(2026, 5, 31), amount="3000")
        data, _ = self._rows(2026, 6)
        self.assertEqual(Decimal(str(data["totals"]["fixed"])), Decimal("0"))


class FixedExpenseAndSalaryAPITests(APITestCase):
    """Постоянные расходы и зарплаты записями + месячный фильтр."""

    FIXED_URL = "/api/finance/fixed-expenses/"
    SALARY_URL = "/api/finance/salaries/"

    def setUp(self):
        self.admin = User.objects.create_user(username="f_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="f_store", password="x", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(self.admin)

    def test_create_fixed_expense_records_author(self):
        r = self.client.post(self.FIXED_URL, {
            "category": "RENT", "name": "Аренда за июль",
            "amount": "30000", "spent_at": "2026-07-01",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["category_display"], "Аренда цеха")
        self.assertEqual(FixedExpense.objects.get().created_by, self.admin)

    def test_month_filter_selects_only_that_month(self):
        FixedExpense.objects.create(category="RENT", amount=Decimal("100"), spent_at=date(2026, 6, 1))
        FixedExpense.objects.create(category="RENT", amount=Decimal("200"), spent_at=date(2026, 7, 1))
        r = self.client.get(self.FIXED_URL, {"year": 2026, "month": 7})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(len(r.data["results"]), 1)
        self.assertEqual(Decimal(r.data["results"][0]["amount"]), Decimal("200"))

    def test_broken_month_filter_is_ignored_not_500(self):
        FixedExpense.objects.create(category="RENT", amount=Decimal("100"), spent_at=date(2026, 6, 1))
        r = self.client.get(self.FIXED_URL, {"year": "abc", "month": "99"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(len(r.data["results"]), 1)

    def test_salary_is_per_employee(self):
        r = self.client.post(self.SALARY_URL, {
            "employee": "Азамат", "amount": "25000", "paid_at": "2026-07-05",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(SalaryPayment.objects.get().employee, "Азамат")

    def test_storekeeper_cannot_see_or_add(self):
        self.client.force_authenticate(self.store)
        self.assertEqual(self.client.get(self.FIXED_URL).status_code, 403)
        self.assertEqual(self.client.get(self.SALARY_URL).status_code, 403)

    def test_report_sums_records_within_period(self):
        FixedExpense.objects.create(category="RENT", amount=Decimal("100"), spent_at=date(2026, 6, 10))
        FixedExpense.objects.create(category="INTERNET", amount=Decimal("30"), spent_at=date(2026, 6, 12))
        FixedExpense.objects.create(category="RENT", amount=Decimal("999"), spent_at=date(2026, 8, 1))
        SalaryPayment.objects.create(employee="Мастер", amount=Decimal("70"), paid_at=date(2026, 6, 20))
        r = self.client.get("/api/finance/report/", {"date_from": "2026-06-01", "date_to": "2026-06-30"})
        self.assertEqual(r.status_code, 200, r.data)
        fixed = r.data["fixed"]
        self.assertEqual(Decimal(str(fixed["rent"])), Decimal("100"))
        self.assertEqual(Decimal(str(fixed["internet"])), Decimal("30"))
        self.assertEqual(Decimal(str(fixed["salary"])), Decimal("70"))
        # Августовская аренда в июньский период не попадает.
        self.assertEqual(Decimal(str(fixed["total"])), Decimal("200"))
