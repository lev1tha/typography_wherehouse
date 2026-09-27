"""Возврат — событие своего дня (решение владельца, 2026-09-27).

Аудит 26.09, п. 7: возврат 26.09 по заказу 10.09 уменьшал выручку 1–15.09
(459 313 → 457 025), а деньги уходили из кассы 26.09. Если 1–15.09 был
закрыт, возврат не оформлялся вовсе, пока период не откроешь.

Теперь продажа считается днём ЗАКАЗА, возврат — днём ВОЗВРАТА, на всех
денежных экранах одинаково: «Финансы», график по дням, «Обзор», складской
лист, станки. Месяц заказа после возврата не меняется ни на сом.
"""
import calendar
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt
from services.models import PrintingService
from warehouse.models import Material

REPORT = "/api/finance/report/"
DASHBOARD = "/api/audit/dashboard/"
DAILY = "/api/finance/daily/"
SHEET = "/api/finance/material-report/"


def month_of(day):
    last = calendar.monthrange(day.year, day.month)[1]
    return {
        "date_from": day.replace(day=1).isoformat(),
        "date_to": day.replace(day=last).isoformat(),
    }


class RefundDatedByReturnTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="rd_admin", password="x", role=User.Role.ADMIN
        )
        self.customer = Client.objects.create(full_name="Тахир", phone="+996555000111")
        self.client.force_authenticate(self.admin)
        self.mat = Material.objects.create(
            name="Акрил 3мм", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
            price_per_sqm=Decimal("1500"),
        )
        self.laser = PrintingService.objects.create(
            name="Резка лазером", kind=PrintingService.Kind.CUTTING,
            machine=PrintingService.Machine.LASER, rate_per_pm=Decimal("120"),
        )
        self.today = timezone.localdate()
        self.order_day = self.today - timedelta(days=40)
        r = self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": self.mat.id, "form": "SHEET", "width": "1.22",
            "height": "2.44", "sheet_count": "5", "purchase_cost": "12000",
            "received_on": (self.order_day - timedelta(days=1)).isoformat(),
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True, "client_id": self.customer.id,
            "order_date": self.order_day.isoformat(),
            "items": [{"type": "SERVICE", "service": self.laser.id, "material": self.mat.id,
                       "width": "0.4", "length": "0.6", "running_meters": "3.7"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.receipt = Receipt.objects.get(pk=r.data["id"])
        self.material_line = self.receipt.items.get(material__isnull=False)
        self.cut_line = self.receipt.items.get(service__isnull=False)
        self.then = month_of(self.order_day)
        self.now = month_of(self.today)

    def _refund(self, *lines):
        r = self.client.post(f"/api/sales/receipts/{self.receipt.id}/refund/",
                             {"item_ids": [line.id for line in lines]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def _report(self, period):
        return self.client.get(REPORT, period).data

    def test_order_month_does_not_move_after_a_later_refund(self):
        before = self._report(self.then)
        self._refund(self.material_line)
        after = self._report(self.then)
        for key in ("revenue", "cogs", "gross_margin", "profit"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(Decimal(str(after["revenue"])), self.receipt.total_price)

    def test_refund_lowers_the_month_it_was_made_in(self):
        self._refund(self.material_line)
        now = self._report(self.now)
        value = self.material_line.sold_total
        self.assertEqual(Decimal(str(now["refunds"])), value)
        self.assertEqual(Decimal(str(now["revenue"])), -value)
        self.assertEqual(Decimal(str(now["cogs"])), -self.material_line.cost_total)

    def test_all_time_totals_are_unchanged_by_the_rule(self):
        self._refund(self.material_line)
        whole = self._report({})
        self.receipt.refresh_from_db()
        self.assertEqual(
            Decimal(str(whole["revenue"])),
            self.receipt.total_price - self.receipt.refunded_amount,
        )

    def test_overview_and_finance_agree_in_both_months(self):
        self._refund(self.material_line)
        for period in (self.then, self.now):
            fin = self._report(period)
            dash = self.client.get(DASHBOARD, period).data
            self.assertEqual(Decimal(str(dash["revenue"]["total"])), Decimal(str(fin["revenue"])))
            self.assertEqual(
                Decimal(str(dash["breakdown"]["profit_before_expenses"])),
                Decimal(str(fin["gross_margin"])),
            )
            # Работа + материал = выручка и в месяце возврата.
            self.assertEqual(
                Decimal(str(dash["breakdown"]["work_revenue"]))
                + Decimal(str(dash["breakdown"]["material_revenue"])),
                Decimal(str(fin["revenue"])),
            )

    def test_daily_chart_puts_the_refund_on_its_day(self):
        self._refund(self.material_line)
        data = self.client.get(DAILY, {"year": self.today.year, "month": self.today.month}).data
        row = next(r for r in data["rows"] if r["day"] == self.today.day)
        self.assertEqual(Decimal(str(row["revenue"])), -self.material_line.sold_total)

    def test_material_sheet_takes_the_return_in_its_month(self):
        self._refund(self.material_line)
        then = {r["id"]: r for r in self.client.get(SHEET, self.then).data["rows"]}
        now = {r["id"]: r for r in self.client.get(SHEET, self.now).data["rows"]}
        self.assertEqual(Decimal(str(then[self.mat.id]["sold_area"])), Decimal("0.240"))
        self.assertEqual(Decimal(str(now[self.mat.id]["sold_area"])), Decimal("-0.240"))
        self.assertEqual(
            Decimal(str(now[self.mat.id]["material_revenue"])), -self.material_line.sold_total
        )

    def test_returned_work_leaves_the_machine_in_the_refund_month(self):
        self._refund(self.cut_line)
        laser = lambda rep: next(m for m in rep["cutting"]["rows"] if m["id"] == "LASER")  # noqa: E731
        self.assertEqual(Decimal(str(laser(self._report(self.then))["amount"])), Decimal("444"))
        now = self._report(self.now)
        self.assertEqual(Decimal(str(now["cutting"]["total"])), Decimal("-444"))
        self.assertEqual(Decimal(str(laser(now)["amount"])), Decimal("-444"))
        # И в складском листе столбец резки сходится с «Резкой, всего».
        rows = self.client.get(SHEET, self.now).data
        self.assertEqual(Decimal(str(rows["totals"]["cut_revenue"])), Decimal("-444"))
