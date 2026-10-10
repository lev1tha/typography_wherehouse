"""Коммерческое предложение (2026-10-10, CALC-03): расчёт без продажи.

КП — это цены по правилам прайса, срок действия и своя печать; склад, долг,
касса и выручка остаются как были.
"""
from datetime import timedelta
from decimal import Decimal as D

from finance.models import CashEntry
from sales.models import Quote, Receipt
from sales.tests_calc_base import CalcBase
from warehouse.models import InventoryLog, Material

QUOTES = "/api/sales/quotes/"


class QuoteTests(CalcBase):
    def _quote(self, user=None, **extra):
        self.client.force_authenticate(user or self.store)
        body = {"client_id": self.ivan.id, "title": "Вывеска",
                "items": [self.cut(self.acr3, "1", "1", "4"),
                          {"type": "SERVICE", "service": self.mont.id, "quantity": 1}], **extra}
        return self.client.post(QUOTES, body, format="json")

    def test_quote_touches_neither_stock_nor_cash_nor_receipts(self):
        qty = Material.objects.get(pk=self.acr3.pk).quantity
        r = self._quote()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Receipt.objects.count(), 0)
        self.assertEqual(CashEntry.objects.count(), 0)
        self.assertEqual(InventoryLog.objects.filter(type="SALE").count(), 0)
        self.assertEqual(Material.objects.get(pk=self.acr3.pk).quantity, qty)
        self.assertEqual(Quote.objects.count(), 1)

    def test_quote_has_its_own_number_and_priced_lines(self):
        a = self._quote()
        b = self._quote()
        self.assertEqual((a.data["number"], b.data["number"]), (1, 2))
        # 4 пог.м × 65 = 260; материал 1×1 × 1550; монтаж 2854.
        self.assertEqual(D(str(a.data["total_price"])), D("260") + D("1550") + D("2854"))
        self.assertEqual(len(a.data["items"]), 3)
        self.assertEqual(D(str(a.data["items"][0]["price_per_item"])), D("65"))

    def test_quote_follows_the_price_rules(self):
        self.settings_(urgency_percent=25)
        r = self._quote(is_urgent=True)
        plain = self._quote()
        self.assertGreater(D(str(r.data["total_price"])), D(str(plain.data["total_price"])))
        self.assertTrue(r.data["is_urgent"])

    def test_quote_has_no_cost_figures_even_for_admin(self):
        r = self._quote(user=self.admin)
        self.assertTrue(all(i["cost_total"] is None for i in r.data["items"]))
        self.assertNotIn("margin", r.data)

    def test_valid_until_default_and_expiry_flag(self):
        r = self._quote()
        self.assertEqual(r.data["valid_until"], str(self.today + timedelta(days=14)))
        self.assertFalse(r.data["is_expired"])
        q = Quote.objects.get(pk=r.data["id"])
        q.valid_until = self.today - timedelta(days=1)
        q.save()
        self.assertTrue(self.client.get(f"{QUOTES}{q.id}/").data["is_expired"])

    def test_manual_prices_follow_the_cashier_rights(self):
        r = self._quote(user=self.store, items=[{"type": "SERVICE", "service": self.mont.id,
                                                  "quantity": 1, "cut_rate": "3800"}])
        self.assertEqual(r.status_code, 403, r.data)

    def test_accountant_cannot_make_quotes(self):
        self.assertEqual(self._quote(user=self.accountant).status_code, 403)

    def test_list_search_and_cancel(self):
        a = self._quote(title="Вывеска для кафе")
        self._quote(title="Табличка")
        self.assertEqual(len(self.client.get(QUOTES).data["results"]), 2)
        self.assertEqual(len(self.client.get(QUOTES, {"search": "кафе"}).data["results"]), 1)
        self.assertEqual(len(self.client.get(QUOTES, {"search": "КП 2"}).data["results"]), 1)
        out = self.client.post(f"{QUOTES}{a.data['id']}/cancel/", {})
        self.assertEqual(out.data["status"], "CANCELLED")
        self.assertEqual(len(self.client.get(QUOTES, {"status": "ACTIVE"}).data["results"]), 1)

    def test_checkout_from_a_quote_closes_it(self):
        q = self._quote()
        order = self.co(q.data["cart"], client_id=self.ivan.id, quote_id=q.data["id"])
        self.assertEqual(order.status_code, 201, order.data)
        quote = Quote.objects.get(pk=q.data["id"])
        self.assertEqual(quote.status, Quote.Status.ORDERED)
        self.assertEqual(str(quote.receipt_id), order.data["id"])
        self.assertEqual(order.data["total_price"], q.data["total_price"])
        self.assertEqual(self.client.post(f"{QUOTES}{q.data['id']}/cancel/", {}).status_code, 400)

    def test_only_admin_deletes(self):
        q = self._quote()
        self.assertEqual(self.client.delete(f"{QUOTES}{q.data['id']}/").status_code, 403)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.delete(f"{QUOTES}{q.data['id']}/").status_code, 204)
