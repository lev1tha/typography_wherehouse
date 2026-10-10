"""Трата аванса клиента в НОВОМ заказе (волна 2, D-93 — контракт для кассы).

Аванс — деньги «на будущие работы», они уже в кассе. Касса при оформлении
закрывает ими остаток заказа после сдачи: долг 0, аванс уменьшается, касса не
двигается.
"""
from django.utils import timezone

from clients.advances import accept_advance, advance_available
from clients.models import BalanceOffset
from clients.testkit import D, ShopCase
from finance.models import CashEntry
from sales.models import Receipt


def cash_total():
    total = D("0")
    for kind, amount in CashEntry.objects.values_list("kind", "amount"):
        total += amount if kind == "IN" else -amount
    return total


class AdvanceAtCheckoutTests(ShopCase):
    def checkout(self, total, **extra):
        body = {
            "client_id": self.agency.id, "payment_method": "CASH",
            "items": [{"type": "MATERIAL", "material": self.coin.id, "quantity": str(total), "mode": "PIECE"}],
            **extra,
        }
        r = self.client.post("/api/sales/receipts/checkout/", body, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def test_advance_5000_order_3000(self):
        accept_advance(self.agency, D("5000"), user=self.admin)
        cash_before = cash_total()
        data = self.checkout(3000, use_advance=True)
        self.assertEqual(D(str(data["debt"])), D("0"))
        self.assertEqual(data["payment_status"], "PAID")
        self.assertEqual(D(str(data["advance_applied"])), D("3000"))
        self.assertEqual(D(str(data["change_applied"])), D("3000"))
        self.assertEqual(advance_available(self.agency), D("2000"))
        self.assertEqual(cash_total(), cash_before)                 # касса не двигается
        card = self.card()
        self.assertEqual(D(str(card["debt"])), D("0"))
        self.assertEqual(D(str(card["advance_balance"])), D("2000"))
        self.assertEqual(D(str(card["balance"])), D("-2000"))
        st = self.statement()
        self.assertEqual(D(str(st["closing"])), D("-2000"))
        used = [r for r in st["rows"] if r["kind"] == "advance_used"]
        self.assertEqual(len(used), 1)
        self.assertEqual(D(str(used[0]["amount"])), D("3000"))
        # Список чеков отдаёт то же число (аннотация, а не запрос на чек).
        listed = self.client.get("/api/sales/receipts/").data["results"][0]
        self.assertEqual(D(str(listed["advance_applied"])), D("3000"))

    def test_without_flag_advance_is_kept(self):
        accept_advance(self.agency, D("5000"), user=self.admin)
        data = self.checkout(3000)
        self.assertEqual(D(str(data["debt"])), D("3000"))
        self.assertEqual(advance_available(self.agency), D("5000"))

    def test_pay_full_pays_only_the_rest(self):
        accept_advance(self.agency, D("2000"), user=self.admin)
        cash_before = cash_total()
        data = self.checkout(3000, use_advance=True, pay_full=True)
        self.assertEqual(D(str(data["debt"])), D("0"))
        self.assertEqual(D(str(data["change_due"])), D("0"))
        self.assertEqual(advance_available(self.agency), D("0"))
        self.assertEqual(cash_total() - cash_before, D("1000"))     # принесли только остаток

    def test_change_first_then_advance(self):
        old = self.sale(1000, paid=1500)                             # сдача 500 у цеха
        self.assertEqual(old.change_due, D("500"))
        accept_advance(self.agency, D("1000"), user=self.admin)
        data = self.checkout(3000, use_change=True, use_advance=True)
        self.assertEqual(D(str(data["change_applied"])), D("1500"))
        self.assertEqual(D(str(data["advance_applied"])), D("1000"))
        self.assertEqual(D(str(data["debt"])), D("1500"))
        self.assertEqual(advance_available(self.agency), D("0"))

    def test_delete_order_returns_advance(self):
        accept_advance(self.agency, D("5000"), user=self.admin)
        data = self.checkout(3000, use_advance=True)
        r = self.client.delete(f"/api/sales/receipts/{data['id']}/")
        self.assertIn(r.status_code, (200, 204), getattr(r, "data", r))
        self.assertEqual(advance_available(self.agency), D("5000"))
        self.assertFalse(BalanceOffset.objects.exists())
        self.assertEqual(D(str(self.card()["balance"])), D("-5000"))

    def test_unpay_returns_advance(self):
        accept_advance(self.agency, D("5000"), user=self.admin)
        data = self.checkout(3000, use_advance=True)
        cash_before = cash_total()
        r = self.client.post(f"/api/sales/receipts/{data['id']}/unpay/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(advance_available(self.agency), D("5000"))
        self.assertEqual(cash_total(), cash_before)
        self.assertEqual(Receipt.objects.get(pk=data["id"]).debt, D("3000"))

    def test_bridge_stays_zero(self):
        from finance.reports.bridge import bridge

        accept_advance(self.agency, D("5000"), user=self.admin)
        self.checkout(3000, use_advance=True)
        day = timezone.localdate()
        b = bridge(day.replace(day=1), day)
        self.assertEqual(b["unexplained"], D("0"))
        # Аванс привязан к клиенту (волна 2): долга по клиенту нет, у нас его 2 000.
        self.assertEqual(b["levels"]["receivables"], D("0"))
        self.assertEqual(b["levels"]["client_money"], D("2000"))
