"""Статьи расходов не переписывают прошлые и закрытые месяцы.

Аудит 26.09, пп. 5–6:
- «Удалить» свою статью с тратами прятало её — и отчёт переставал её видеть:
  прибыль прошлых месяцев росла на сумму трат, а график по дням оставался
  прежним. Скрытая статья теперь остаётся в отчёте там, где у неё есть траты.
- Трату в закрытый месяц не внести, а снять у аренды галочку «входит в
  прибыль» было можно — и принятый сентябрь вырастал на 25 000. Теперь это
  держит замок периода.
- Ручная трата «Закуп материала» ложилась в плитку «Закуп» второй раз
  (оплату долга поставщику вносили именно так). Новые такие траты не
  заводятся, а плитка равна «пришло» в цепочке склада.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import ExpenseEntry, ExpenseKind

REPORT = "/api/finance/report/"
KINDS = "/api/finance/expense-kinds/"
ENTRIES = "/api/finance/expense-entries/"


class ExpenseKindGuardTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="kg_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.today = timezone.localdate()
        self.past = self.today - timedelta(days=20)
        self.rent = ExpenseKind.objects.get(code="RENT")
        ExpenseEntry.objects.create(kind=self.rent, amount=Decimal("25000"), spent_at=self.past)
        self.window = {"date_from": self.past.isoformat(), "date_to": self.past.isoformat()}

    def _profit(self):
        return Decimal(str(self.client.get(REPORT, self.window).data["profit"]))

    def test_unticking_profit_on_a_kind_with_closed_entries_is_refused(self):
        before = self._profit()
        self.client.patch("/api/finance/period/", {
            "closed_through": (self.today - timedelta(days=1)).isoformat(),
        }, format="json")
        r = self.client.patch(f"{KINDS}{self.rent.id}/", {"in_profit": False}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(self._profit(), before)

    def test_unticking_is_allowed_while_the_period_is_open(self):
        r = self.client.patch(f"{KINDS}{self.rent.id}/", {"in_profit": False}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_renaming_a_kind_is_not_blocked_by_the_lock(self):
        self.client.patch("/api/finance/period/", {
            "closed_through": (self.today - timedelta(days=1)).isoformat(),
        }, format="json")
        r = self.client.patch(f"{KINDS}{self.rent.id}/", {"name": "Аренда цеха"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_deleting_an_own_kind_keeps_its_money_in_the_report(self):
        r = self.client.post(KINDS, {"name": "Реклама", "block": "VARIABLE"}, format="json")
        ad = ExpenseKind.objects.get(pk=r.data["id"])
        ExpenseEntry.objects.create(kind=ad, amount=Decimal("10000"), spent_at=self.past)
        before = self._profit()
        month = {"year": self.past.year, "month": self.past.month}
        daily_before = self.client.get("/api/finance/daily/", month).data["totals"]["profit"]
        r = self.client.delete(f"{KINDS}{ad.id}/")
        self.assertEqual(r.data, {"archived": True})
        self.assertEqual(self._profit(), before)
        self.assertEqual(
            self.client.get("/api/finance/daily/", month).data["totals"]["profit"], daily_before
        )

    def test_material_purchase_cannot_be_entered_by_hand(self):
        purchase = ExpenseKind.objects.get(code="MATERIAL_PURCHASE")
        r = self.client.post(ENTRIES, {
            "kind": purchase.id, "name": "долг поставщику", "amount": "48000",
            "spent_at": self.today.isoformat(), "account": "CASH",
        }, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("Долге поставщикам", str(r.data))

    def test_purchase_tile_equals_the_stock_chain(self):
        # Старая ручная трата этого вида (до запрета) — в плитку не идёт.
        ExpenseEntry.objects.create(
            kind=ExpenseKind.objects.get(code="MATERIAL_PURCHASE"),
            amount=Decimal("48000"), spent_at=self.past,
        )
        stock = self.client.get(REPORT, self.window).data["stock"]
        self.assertEqual(
            Decimal(str(stock["purchases"])), Decimal(str(stock["reconcile"]["purchases"]))
        )
