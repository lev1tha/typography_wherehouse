"""Аванс клиента без заказа и зачёт сдачи/аванса в долг (CLI-05, D-93).

Раньше принять предоплату «на будущие работы» было нечем: pay-debt без долга
отвечал 400, а обход через фиктивный заказ на 1 сом превращал аванс в «сдачу».
"""
from datetime import date, timedelta

from django.utils import timezone

from audit.models import AuditLog
from clients.advances import advance_available, take_advance
from clients.models import BalanceOffset, Client, ClientAdvance, ClientSettings
from clients.testkit import D, ShopCase
from finance.models import CashEntry, PeriodLock


class AcceptAdvanceTests(ShopCase):
    def url(self, client=None):
        return f"/api/clients/clients/{(client or self.agency).id}/advances/"

    def accept(self, amount="25000", **extra):
        return self.client.post(self.url(), {"amount": amount, "method": "CASH", **extra}, format="json")

    def test_advance_is_cash_and_balance_but_not_revenue(self):
        from finance.reports.bridge import bridge
        from finance.reports.pnl import pnl

        day = timezone.localdate()
        r = self.accept("25000", paid_on=str(day), note="на будущие работы")
        self.assertEqual(r.status_code, 201, r.data)
        adv = ClientAdvance.objects.get()
        self.assertEqual((adv.amount, adv.remaining), (D("25000"), D("25000")))
        entry = CashEntry.objects.get()
        self.assertEqual((entry.kind, entry.article, entry.amount, entry.account), ("IN", "SALE", D("25000"), "CASH"))
        self.assertIsNone(entry.receipt)
        card = self.card()
        self.assertEqual(D(str(card["advance_balance"])), D("25000"))
        self.assertEqual(D(str(card["balance"])), D("-25000"))
        self.assertEqual(D(str(card["debt"])), D("0"))
        self.assertEqual(D(str(card["change_due"])), D("0"))
        # не выручка, касса ≠ прибыль, «Не объяснено» остаётся нулём
        self.assertEqual(D(str(pnl(day.replace(day=1), day)["revenue"])), D("0"))
        self.assertEqual(D(str(bridge(day.replace(day=1), day)["unexplained"])), D("0"))
        self.assertTrue(AuditLog.objects.filter(action__startswith="Принят аванс").exists())

    def test_bank_method_goes_to_bank_account(self):
        r = self.client.post(self.url(), {"amount": "1000", "method": "MBANK"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(CashEntry.objects.get().account, "BANK")

    def test_validation(self):
        for body in ({"amount": "0"}, {"amount": "-5"}, {"amount": "abc"}, {"amount": ""},
                     {"amount": "100", "method": "WRITE_OFF"}, {"amount": "100", "method": "ONLINE"},
                     {"amount": "100", "paid_on": str(timezone.localdate() + timedelta(days=2))},
                     {"amount": "100", "paid_on": "вчера"}):
            r = self.client.post(self.url(), {"method": "CASH", **body}, format="json")
            self.assertEqual(r.status_code, 400, (body, r.data))
        self.assertFalse(ClientAdvance.objects.exists())
        self.assertFalse(CashEntry.objects.exists())

    def test_closed_period_refuses(self):
        PeriodLock.objects.create(closed_through=date(2026, 9, 30))
        r = self.accept("100", paid_on="2026-09-15")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertFalse(ClientAdvance.objects.exists())

    def test_roles(self):
        self.client.force_authenticate(self.acc)
        self.assertEqual(self.accept().status_code, 403)
        # владелец выключил приём денег складовщиком — только админ
        ClientSettings.objects.update_or_create(pk=1, defaults={"storekeeper_takes_debt": False})
        self.client.force_authenticate(self.store)
        self.assertEqual(self.accept().status_code, 403)
        ClientSettings.objects.update_or_create(pk=1, defaults={"storekeeper_takes_debt": True})
        r = self.accept("500")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(ClientAdvance.objects.get().created_by, self.store)
        # складовщик ставит только сегодняшнюю дату
        r = self.accept("500", paid_on=str(timezone.localdate() - timedelta(days=3)))
        self.assertEqual(r.status_code, 400, r.data)

    def test_list_and_card_show_advances(self):
        self.accept("700")
        r = self.client.get(self.url())
        self.assertEqual(r.status_code, 200)
        self.assertEqual([D(str(a["remaining"])) for a in r.data], [D("700")])
        card = self.card()
        self.assertEqual(len(card["advances"]), 1)

    def test_revert_unused_advance(self):
        self.accept("25000")
        adv = ClientAdvance.objects.get()
        r = self.client.post(f"{self.url()}{adv.id}/revert/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        adv.refresh_from_db()
        self.assertIsNotNone(adv.reverted_at)
        self.assertEqual(adv.remaining, D("0"))
        self.assertEqual(CashEntry.balance(), D("0"))
        self.assertEqual(D(str(self.card()["advance_balance"])), D("0"))
        self.assertEqual(D(str(self.statement()["closing"])), D("0"))
        # повторно нельзя
        self.assertEqual(self.client.post(f"{self.url()}{adv.id}/revert/", {}, format="json").status_code, 400)

    def test_revert_used_advance_is_refused(self):
        self.accept("25000")
        self.sale(10000, paid=0)
        self.client.post(f"/api/clients/clients/{self.agency.id}/pay-debt/", {"use_change": True}, format="json")
        adv = ClientAdvance.objects.get()
        r = self.client.post(f"{self.url()}{adv.id}/revert/", {}, format="json")
        self.assertEqual(r.status_code, 400, r.data)


class UseBalanceTests(ShopCase):
    PAY = "/api/clients/clients/{}/pay-debt/"

    def pay(self, **body):
        return self.client.post(self.PAY.format(self.agency.id), body, format="json")

    def advance(self, amount="25000"):
        self.client.post(f"/api/clients/clients/{self.agency.id}/advances/", {"amount": amount, "method": "CASH"}, format="json")

    def test_advance_closes_debt_without_cash(self):
        self.advance("25000")
        r1 = self.sale(10000, paid=0, days_ago=20)
        cash_before = CashEntry.balance()
        r = self.pay(use_change=True)
        self.assertEqual(r.status_code, 200, r.data)
        r1.refresh_from_db()
        self.assertEqual(r1.debt, D("0"))
        self.assertEqual(r1.payment_status, "PAID")
        self.assertEqual(r1.change_applied, D("10000"))
        self.assertEqual(CashEntry.balance(), cash_before)           # деньги уже в кассе с аванса
        adv = ClientAdvance.objects.get()
        self.assertEqual(adv.remaining, D("15000"))
        use = BalanceOffset.objects.get()
        self.assertEqual((use.source, use.amount, use.receipt_id), ("ADVANCE", D("10000"), r1.pk))
        card = self.card()
        self.assertEqual(D(str(card["debt"])), D("0"))
        self.assertEqual(D(str(card["balance"])), D("-15000"))
        self.assertEqual(D(str(self.statement()["closing"])), D("-15000"))
        self.assertEqual(D(str(r.data["debt"])), D("0"))
        self.assertEqual(D(str(r.data["offset"]["advance"])), D("10000"))

    def test_change_is_used_before_advance(self):
        self.sale(1000, paid=1600)                    # сдача 600
        self.advance("5000")
        self.sale(2000, paid=0)
        r = self.pay(use_change=True)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(str(r.data["offset"]["change"])), D("600"))
        self.assertEqual(D(str(r.data["offset"]["advance"])), D("1400"))
        self.assertEqual(ClientAdvance.objects.get().remaining, D("3600"))
        card = self.card()
        self.assertEqual(D(str(card["change_due"])), D("0"))
        self.assertEqual(D(str(card["balance"])), D("-3600"))
        self.assertEqual(D(str(self.statement()["closing"])), D("-3600"))

    def test_offset_then_cash_for_the_rest(self):
        self.advance("3000")
        self.sale(10000, paid=0)
        r = self.pay(use_change=True, amount="7000")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(str(r.data["debt"])), D("0"))
        self.assertEqual(D(str(r.data["paid"])), D("7000"))          # наличными
        self.assertEqual(CashEntry.balance(), D("3000") + D("7000"))
        self.assertEqual(ClientAdvance.objects.get().remaining, D("0"))

    def test_partial_cover_leaves_debt(self):
        self.advance("4000")
        self.sale(10000, paid=0)
        r = self.pay(use_change=True)
        self.assertEqual(r.status_code, 200, r.data)
        # аванс 4 000 закрыл часть; остальное «закрыть целиком» наличными нельзя без суммы:
        # amount не указан → закрывается остаток деньгами (как и обычная общая выплата)
        self.assertEqual(D(str(r.data["debt"])), D("0"))
        self.assertEqual(D(str(r.data["paid"])), D("6000"))

    def test_use_change_with_nothing_to_offset_is_a_plain_payment(self):
        """Ни сдачи, ни аванса: «зачесть» ничего не делает, остаток закрывается деньгами."""
        self.sale(10000, paid=0)
        r = self.pay(use_change=True)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(str(r.data["offset"]["total"])), D("0"))
        self.assertEqual(D(str(r.data["paid"])), D("10000"))

    def test_no_debt_means_nothing_to_offset(self):
        """Клиент без долга: 'зачесть' нечего."""
        self.advance("100")
        r = self.pay(use_change=True)
        self.assertEqual(r.status_code, 400, r.data)

    def test_use_change_with_write_off_is_refused(self):
        self.sale(100, paid=0)
        r = self.pay(use_change=True, method="WRITE_OFF")
        self.assertEqual(r.status_code, 400, r.data)

    def test_take_advance_contract(self):
        self.advance("5000")
        other = self.sale(1000, paid=0)
        self.assertEqual(advance_available(self.agency), D("5000"))
        applied = take_advance(self.agency, D("1200"), receipt=other, user=self.admin)
        self.assertEqual(applied, D("1200"))
        self.assertEqual(advance_available(self.agency), D("3800"))
        # больше, чем есть, — отдаёт сколько есть
        applied = take_advance(self.agency, D("9999"), receipt=other, user=self.admin)
        self.assertEqual(applied, D("3800"))
        self.assertEqual(advance_available(self.agency), D("0"))
        self.assertEqual(take_advance(self.agency, D("1"), receipt=other), D("0"))

    def test_oldest_advance_first(self):
        self.client.post(f"/api/clients/clients/{self.agency.id}/advances/", {"amount": "100", "method": "CASH", "paid_on": "2026-09-01"}, format="json")
        self.client.post(f"/api/clients/clients/{self.agency.id}/advances/", {"amount": "100", "method": "CASH", "paid_on": "2026-08-01"}, format="json")
        r = self.sale(1000, paid=0)
        take_advance(self.agency, D("150"), receipt=r)
        left = {a.paid_on.isoformat(): a.remaining for a in ClientAdvance.objects.all()}
        self.assertEqual(left, {"2026-08-01": D("0"), "2026-09-01": D("50")})


class AdvanceWithOtherPartsTests(ShopCase):
    def test_merge_moves_advances_and_offsets(self):
        from clients.merge import merge_clients

        twin = Client.objects.create(full_name="Двойник", phone="+996700999888")
        self.client.post(f"/api/clients/clients/{twin.id}/advances/", {"amount": "700", "method": "CASH"}, format="json")
        merge_clients(self.agency, twin, user=self.admin)
        self.assertEqual(ClientAdvance.objects.get().client_id, self.agency.id)
        self.assertEqual(D(str(self.card()["advance_balance"])), D("700"))

    def test_has_change_filter_includes_advances(self):
        self.client.post(f"/api/clients/clients/{self.agency.id}/advances/", {"amount": "700", "method": "CASH"}, format="json")
        r = self.client.get("/api/clients/clients/", {"has_change": 1})
        self.assertEqual([x["id"] for x in r.data["results"]], [self.agency.id])

    def test_delete_client_with_advance_is_refused_cleanly(self):
        self.client.post(f"/api/clients/clients/{self.agency.id}/advances/", {"amount": "700", "method": "CASH"}, format="json")
        r = self.client.delete(f"/api/clients/clients/{self.agency.id}/")
        self.assertIn(r.status_code, (400,))


class OffsetOnlyTests(ShopCase):
    PAY = "/api/clients/clients/{}/pay-debt/"

    def test_offset_only_never_takes_cash(self):
        self.client.post(f"/api/clients/clients/{self.agency.id}/advances/", {"amount": "4000", "method": "CASH"}, format="json")
        self.sale(10000, paid=0)
        before = CashEntry.balance()
        r = self.client.post(self.PAY.format(self.agency.id), {"offset_only": True}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(str(r.data["debt"])), D("6000"))        # остаток остаётся долгом
        self.assertEqual(D(str(r.data["paid"])), D("0"))
        self.assertEqual(CashEntry.balance(), before)

    def test_offset_only_with_nothing_to_offset_is_an_error(self):
        self.sale(10000, paid=0)
        r = self.client.post(self.PAY.format(self.agency.id), {"offset_only": True}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_offset_only_refuses_an_amount(self):
        self.client.post(f"/api/clients/clients/{self.agency.id}/advances/", {"amount": "400", "method": "CASH"}, format="json")
        self.sale(1000, paid=0)
        r = self.client.post(self.PAY.format(self.agency.id), {"offset_only": True, "amount": "100"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(ClientAdvance.objects.get().remaining, D("400"))     # откат целиком


class AdvanceInPaymentHistoryTests(ShopCase):
    def test_advance_use_is_listed_among_payments(self):
        self.client.post(f"/api/clients/clients/{self.agency.id}/advances/", {"amount": "4000", "method": "CASH"}, format="json")
        r1 = self.sale(1500, paid=0)
        self.client.post(f"/api/clients/clients/{self.agency.id}/pay-debt/", {"offset_only": True}, format="json")
        pays = self.card()["payments"]
        use = [p for p in pays if p["method"] == "ADVANCE"]
        self.assertEqual(len(use), 1)
        self.assertEqual((D(str(use[0]["amount"])), use[0]["order_number"]), (D("1500"), r1.order_number))
