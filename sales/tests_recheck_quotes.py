"""Перепроверка владельца 10.10, S2 «КП» (CALC-03): заказ из коммерческого
предложения держит обещанную цену и проверяет само КП.

Сценарий владельца (`scratchpad/recheck/price`, test_o7): КП на рез акрила
0,6×0,8, 2,8 пог.м, срочно 25 % — (182 + 744) × 1,25 = 1 157,5 → 1 158. Потом
акрил подорожал до 1 705, ставка реза — до 72, срочность стала 30 %.
"""
from datetime import timedelta
from decimal import Decimal as D

from django.utils import timezone

from sales.models import Quote, Receipt
from sales.tests_calc_base import CalcBase
from warehouse.models import Material

QUOTES = "/api/sales/quotes/"


class QuoteCheckoutTests(CalcBase):
    def setUp(self):
        super().setUp()
        self.settings_(urgency_percent=25)
        self.cart = [self.cut(self.acr3, "0.6", "0.8", "2.8")]
        self.client.force_authenticate(self.store)
        q = self.client.post(QUOTES, {"items": self.cart, "client_id": self.ivan.id, "is_urgent": True},
                             format="json")
        self.assertEqual(q.status_code, 201, q.data)
        self.assertEqual(D(str(q.data["total_price"])), D("1158"))
        self.quote_id = q.data["id"]
        # Прайс сменился после КП.
        Material.objects.filter(pk=self.acr3.pk).update(price_per_sqm=D("1705"), cut_rate_per_pm=D("72"))
        self.settings_(urgency_percent=30)

    def expire(self):
        Quote.objects.filter(pk=self.quote_id).update(valid_until=timezone.localdate() - timedelta(days=1))

    def test_quote_in_term_is_ordered_at_its_own_prices_with_its_urgency(self):
        r = self.co(self.cart, client_id=self.ivan.id, quote_id=self.quote_id)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(str(r.data["total_price"])), D("1158"))
        receipt = Receipt.objects.get(pk=r.data["id"])
        self.assertTrue(receipt.is_urgent)
        self.assertEqual(receipt.urgency_percent, D("25"))
        quote = Quote.objects.get(pk=self.quote_id)
        self.assertEqual(quote.status, Quote.Status.ORDERED)
        self.assertEqual(quote.receipt_id, receipt.pk)

    def test_preview_of_a_quote_in_term_shows_its_prices(self):
        r = self.preview(self.cart, client_id=self.ivan.id, quote_id=self.quote_id)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(str(r.data["total_price"])), D("1158"))
        self.assertEqual(r.data["confirm_warnings"], [])
        self.assertEqual(Quote.objects.get(pk=self.quote_id).status, Quote.Status.ACTIVE)

    def test_expired_quote_asks_was_and_now(self):
        self.expire()
        r = self.co(self.cart, client_id=self.ivan.id, quote_id=self.quote_id)
        self.assertEqual(r.status_code, 409, r.data)
        codes = [w["code"] for w in r.data["warnings"]]
        self.assertIn("quote_changed", codes)
        message = next(w["message"] for w in r.data["warnings"] if w["code"] == "quote_changed")
        # Сегодня без срочности: 2,8 × 72 = 201,6 → 202; 0,48 × 1 705 = 818,4 → 819.
        self.assertIn("КП №1: было 1158", message)
        self.assertIn("сейчас 1021", message)
        self.assertEqual(Receipt.objects.count(), 0)
        ok = self.co(self.cart, client_id=self.ivan.id, quote_id=self.quote_id,
                     confirmed_warnings=["quote_changed"])
        self.assertEqual(ok.status_code, 201, ok.data)
        self.assertEqual(D(str(ok.data["total_price"])), D("1021"))
        self.assertEqual(Quote.objects.get(pk=self.quote_id).status, Quote.Status.ORDERED)

    def test_changed_cart_asks_was_and_now(self):
        cart = self.cart + [{"type": "SERVICE", "service": self.mont.id, "quantity": 1}]
        r = self.co(cart, client_id=self.ivan.id, quote_id=self.quote_id)
        self.assertEqual(r.status_code, 409, r.data)
        self.assertIn("quote_changed", [w["code"] for w in r.data["warnings"]])
        pv = self.preview(cart, client_id=self.ivan.id, quote_id=self.quote_id)
        self.assertIn("quote_changed", [w["code"] for w in pv.data["confirm_warnings"]])

    def test_quote_is_ordered_only_once(self):
        first = self.co(self.cart, client_id=self.ivan.id, quote_id=self.quote_id)
        self.assertEqual(first.status_code, 201, first.data)
        again = self.co(self.cart, client_id=self.ivan.id, quote_id=self.quote_id, is_urgent=True)
        self.assertEqual(again.status_code, 409, again.data)
        self.assertIn("уже оформлен", again.data["detail"])
        self.assertEqual(Receipt.objects.count(), 1)

    def test_unknown_and_cancelled_quotes_are_refused(self):
        r = self.co(self.cart, client_id=self.ivan.id, quote_id=999999)
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("не найдено", r.data["detail"])
        Quote.objects.filter(pk=self.quote_id).update(status=Quote.Status.CANCELLED)
        r = self.co(self.cart, client_id=self.ivan.id, quote_id=self.quote_id)
        self.assertEqual(r.status_code, 409, r.data)
        self.assertIn("отменено", r.data["detail"])
        self.assertEqual(Receipt.objects.count(), 0)
