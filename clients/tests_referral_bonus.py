"""Реферальный бонус — таблица начислений (CLI-07, D-95).

Раньше бонус = ставка × число привязок: платил за клиентов без заказов и за
возвращённые, менял прошлое при смене ставки и не помнил «выплачено».
Теперь: начисление — за первый ОПЛАЧЕННЫЙ и не возвращённый заказ приведённого,
по ставке на момент начисления.
"""
from datetime import timedelta

from django.utils import timezone

from audit.models import AuditLog
from clients.models import Client, ReferralBonus
from clients.testkit import D, ShopCase
from finance.models import FinanceSettings
from sales import sale_service


def set_rate(value):
    s = FinanceSettings.load()
    s.referral_bonus = D(str(value))
    s.save()


class BonusRulesTests(ShopCase):
    """Бакыт привёл Х (3 заказа на 40 000), Y (заказов нет) и Z (заказ возвращён целиком)."""

    def setUp(self):
        super().setUp()
        set_rate(500)
        self.boss = Client.objects.create(full_name="Бакыт", phone="+996700000001")
        self.x = Client.objects.create(full_name="Х", phone="+996700000002", referred_by=self.boss)
        self.y = Client.objects.create(full_name="Y", phone="+996700000003", referred_by=self.boss)
        self.z = Client.objects.create(full_name="Z", phone="+996700000004", referred_by=self.boss)

    def card(self, client=None):
        return super().card(client or self.boss)

    def test_pays_only_for_clients_with_a_paid_order(self):
        self.sale(15000, client=self.x, paid=15000)
        self.sale(15000, client=self.x, paid=15000)
        self.sale(10000, client=self.x, paid=10000)
        zr = self.sale(8000, client=self.z, paid=8000)
        sale_service.refund_receipt(zr, user=self.admin)
        ref = self.card()["referrals"]
        self.assertEqual(ref["count"], 3)
        self.assertEqual(D(str(ref["total_value"])), D("40000"))
        self.assertEqual(D(str(ref["bonus_accrued"])), D("500"))       # не 1 500
        self.assertEqual(D(str(ref["bonus"])), D("500"))
        by_name = {r["display_name"]: r for r in ref["list"]}
        self.assertEqual(by_name["Х"]["bonus"]["status"], "accrued")
        self.assertIsNone(by_name["Y"]["bonus"])
        self.assertIsNone(by_name["Z"]["bonus"])
        self.assertEqual(ReferralBonus.objects.filter(voided_at__isnull=True).count(), 1)

    def test_unpaid_order_does_not_count_until_it_is_paid(self):
        order = self.sale(5000, client=self.y, paid=1000)
        self.assertFalse(ReferralBonus.objects.exists())
        self.assertEqual(D(str(self.card()["referrals"]["bonus_accrued"])), D("0"))
        sale_service.apply_payment(order, None, user=self.admin)
        row = ReferralBonus.objects.get()
        self.assertEqual((row.referrer, row.referred, row.amount), (self.boss, self.y, D("500")))
        self.assertEqual(row.order_number, order.order_number)

    def test_rate_change_does_not_rewrite_the_past(self):
        self.sale(1000, client=self.x, paid=1000)
        set_rate(700)
        self.assertEqual(D(str(self.card()["referrals"]["bonus_accrued"])), D("500"))
        self.sale(1000, client=self.y, paid=1000)                # по новой ставке
        ref = self.card()["referrals"]
        self.assertEqual(D(str(ref["bonus_accrued"])), D("1200"))
        self.assertEqual(sorted(r.amount for r in ReferralBonus.objects.all()), [D("500"), D("700")])
        self.assertEqual(D(str(ref["rate"])), D("700"))

    def test_second_order_does_not_pay_again(self):
        self.sale(1000, client=self.x, paid=1000)
        self.sale(1000, client=self.x, paid=1000)
        self.assertEqual(ReferralBonus.objects.count(), 1)

    def test_zero_rate_accrues_nothing(self):
        set_rate(0)
        self.sale(1000, client=self.x, paid=1000)
        self.assertFalse(ReferralBonus.objects.exists())
        self.assertEqual(D(str(self.card()["referrals"]["bonus_accrued"])), D("0"))

    def test_written_off_order_is_not_a_paid_order(self):
        order = self.sale(1000, client=self.x, paid=0)
        sale_service.apply_payment(order, None, user=self.admin, method="WRITE_OFF")
        self.assertFalse(ReferralBonus.objects.filter(voided_at__isnull=True).exists())

    def test_refund_before_payout_voids_the_accrual(self):
        order = self.sale(1000, client=self.x, paid=1000)
        self.assertEqual(ReferralBonus.objects.filter(voided_at__isnull=True).count(), 1)
        sale_service.refund_receipt(order, user=self.admin)
        self.assertEqual(ReferralBonus.objects.filter(voided_at__isnull=True).count(), 0)
        self.assertEqual(D(str(self.card()["referrals"]["bonus_accrued"])), D("0"))
        # следующий оплаченный заказ — снова повод
        self.sale(2000, client=self.x, paid=2000)
        self.assertEqual(ReferralBonus.objects.filter(voided_at__isnull=True).count(), 1)

    def test_unpaid_by_rollback_voids_too(self):
        order = self.sale(1000, client=self.x, paid=1000)
        self.client.post(f"/api/sales/receipts/{order.id}/unpay/", {}, format="json")
        self.assertEqual(ReferralBonus.objects.filter(voided_at__isnull=True).count(), 0)

    def test_changing_referrer_moves_unpaid_bonus(self):
        self.sale(1000, client=self.x, paid=1000)
        other = Client.objects.create(full_name="Другой", phone="+996700000009")
        r = self.client.patch(f"/api/clients/clients/{self.x.id}/", {"referred_by": other.id}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        active = ReferralBonus.objects.get(voided_at__isnull=True)
        self.assertEqual(active.referrer, other)
        self.assertEqual(D(str(self.card()["referrals"]["bonus_accrued"])), D("0"))
        self.assertTrue(AuditLog.objects.filter(action__startswith="Изменён реферер клиента «Х»").exists())


class OldBindingsTests(ShopCase):
    """Привязки, сделанные до таблицы: считаем расчётом, без миграции данных."""

    def setUp(self):
        super().setUp()
        self.boss = Client.objects.create(full_name="Бакыт", phone="+996700000001")
        self.x = Client.objects.create(full_name="Х", phone="+996700000002", referred_by=self.boss)
        self.y = Client.objects.create(full_name="Y", phone="+996700000003", referred_by=self.boss)
        set_rate(500)
        self.sale(1000, client=self.x, paid=1000, days_ago=40)
        ReferralBonus.objects.all().delete()           # как будто таблицы раньше не было

    def test_shown_by_calculation_as_estimated(self):
        ref = self.card(self.boss)["referrals"]
        item = next(r for r in ref["list"] if r["display_name"] == "Х")
        self.assertTrue(item["bonus"]["estimated"])
        self.assertIsNone(item["bonus"]["id"])
        self.assertEqual(D(str(item["bonus"]["amount"])), D("500"))
        self.assertEqual(item["bonus"]["status"], "accrued")
        self.assertEqual(D(str(ref["bonus_accrued"])), D("500"))
        self.assertFalse(ReferralBonus.objects.exists())     # читали — ничего не писали

    def test_rate_change_freezes_old_bindings_at_the_old_rate(self):
        set_rate(900)
        row = ReferralBonus.objects.get()
        self.assertEqual((row.referred, row.amount), (self.x, D("500")))
        ref = self.card(self.boss)["referrals"]
        self.assertEqual(D(str(ref["bonus_accrued"])), D("500"))
        item = next(r for r in ref["list"] if r["display_name"] == "Х")
        self.assertFalse(item["bonus"]["estimated"])

    def test_payout_materializes_the_row(self):
        r = self.client.post(f"/api/clients/clients/{self.boss.id}/referral-bonus/pay/",
                             {"referred": self.x.id}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        row = ReferralBonus.objects.get()
        self.assertEqual((row.amount, row.paid_amount), (D("500"), D("500")))


class PayoutTests(ShopCase):
    def setUp(self):
        super().setUp()
        set_rate(500)
        self.boss = Client.objects.create(full_name="Бакыт", phone="+996700000001")
        self.x = Client.objects.create(full_name="Х", phone="+996700000002", referred_by=self.boss)
        self.sale(1000, client=self.x, paid=1000)
        self.url = f"/api/clients/clients/{self.boss.id}/referral-bonus/"

    def pay(self, **body):
        return self.client.post(self.url + "pay/", {"referred": self.x.id, **body}, format="json")

    def test_full_payout_is_recorded_with_date(self):
        day = (timezone.localdate() - timedelta(days=2)).isoformat()
        r = self.pay(paid_on=day)
        self.assertEqual(r.status_code, 200, r.data)
        row = ReferralBonus.objects.get()
        self.assertEqual((row.paid_amount, row.paid_on.isoformat(), row.paid_by), (D("500"), day, self.admin))
        item = self.card(self.boss)["referrals"]["list"][0]["bonus"]
        self.assertEqual(item["status"], "paid")
        self.assertEqual(D(str(item["due"])), D("0"))
        ref = self.card(self.boss)["referrals"]
        self.assertEqual(D(str(ref["bonus_paid"])), D("500"))
        self.assertEqual(D(str(ref["bonus_due"])), D("0"))
        self.assertTrue(AuditLog.objects.filter(action__startswith="Выплачен реферальный бонус").exists())

    def test_partial_payout_and_overpay(self):
        self.assertEqual(self.pay(amount="200").status_code, 200)
        item = self.card(self.boss)["referrals"]["list"][0]["bonus"]
        self.assertEqual((item["status"], D(str(item["due"]))), ("partial", D("300")))
        self.assertEqual(self.pay(amount="301").status_code, 400)
        self.assertEqual(self.pay(amount="300").status_code, 200)
        self.assertEqual(self.pay().status_code, 400)               # платить больше нечего

    def test_validation(self):
        for body in ({"amount": "0"}, {"amount": "-1"}, {"amount": "x"},
                     {"paid_on": str(timezone.localdate() + timedelta(days=1))}, {"paid_on": "вчера"}):
            self.assertEqual(self.pay(**body).status_code, 400, body)
        nobody = self.client.post(self.url + "pay/", {"referred": 999999}, format="json")
        self.assertEqual(nobody.status_code, 404)
        stranger = Client.objects.create(full_name="Чужой", phone="+996700000005")
        r = self.client.post(f"/api/clients/clients/{stranger.id}/referral-bonus/pay/", {"referred": self.x.id}, format="json")
        self.assertEqual(r.status_code, 400)            # Х привёл не он

    def test_only_admin_pays(self):
        for user in (self.store, self.acc):
            self.client.force_authenticate(user)
            self.assertEqual(self.pay().status_code, 403)

    def test_unpay(self):
        self.pay()
        r = self.client.post(self.url + "unpay/", {"referred": self.x.id}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        row = ReferralBonus.objects.get()
        self.assertEqual((row.paid_amount, row.paid_on), (D("0"), None))
        self.client.force_authenticate(self.store)
        self.assertEqual(self.client.post(self.url + "unpay/", {"referred": self.x.id}, format="json").status_code, 403)

    def test_paid_bonus_survives_a_later_refund(self):
        self.pay()
        order = self.x.receipts.get()
        sale_service.refund_receipt(order, user=self.admin)
        self.assertEqual(ReferralBonus.objects.filter(voided_at__isnull=True).count(), 1)


class SetReferrerRulesTests(ShopCase):
    """Проставить реферера клиенту с историей заказов — только админ."""

    def setUp(self):
        super().setUp()
        self.boss = Client.objects.create(full_name="Бакыт", phone="+996700000001")
        self.old = Client.objects.create(full_name="Давний", phone="+996700000002")
        self.sale(1000, client=self.old, paid=1000, days_ago=200)
        self.fresh = Client.objects.create(full_name="Новый", phone="+996700000003")

    def patch(self, client):
        return self.client.patch(f"/api/clients/clients/{client.id}/", {"referred_by": self.boss.id}, format="json")

    def test_storekeeper_cannot_set_for_client_with_orders(self):
        self.client.force_authenticate(self.store)
        r = self.patch(self.old)
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("только администратор", str(r.data["referred_by"]))
        self.old.refresh_from_db()
        self.assertIsNone(self.old.referred_by_id)

    def test_storekeeper_can_set_for_client_without_orders(self):
        self.client.force_authenticate(self.store)
        self.assertEqual(self.patch(self.fresh).status_code, 200)

    def test_admin_can_set_for_anyone(self):
        self.assertEqual(self.patch(self.old).status_code, 200)

    def test_checkout_inline_path_is_covered_too(self):
        """Касса вызывает `validate_referred_by` напрямую (см. sales.views)."""
        from rest_framework import serializers

        from clients.serializers import ClientSerializer

        class Req:
            user = self.store

        with self.assertRaises(serializers.ValidationError):
            ClientSerializer(self.old, context={"request": Req()}).validate_referred_by(self.boss)
        ClientSerializer(self.fresh, context={"request": Req()}).validate_referred_by(self.boss)


class MergeBonusTests(ShopCase):
    def setUp(self):
        super().setUp()
        set_rate(500)
        self.keep = Client.objects.create(full_name="Остаётся", phone="+996700000011")
        self.drop = Client.objects.create(full_name="Удаляется", phone="+996700000012")

    def test_bonuses_of_dropped_referrer_move(self):
        kid = Client.objects.create(full_name="Привёл", phone="+996700000013", referred_by=self.drop)
        self.sale(1000, client=kid, paid=1000)
        from clients.merge import merge_clients
        merge_clients(self.keep, self.drop, user=self.admin)
        row = ReferralBonus.objects.get(voided_at__isnull=True)
        self.assertEqual((row.referrer, row.referred), (self.keep, kid))

    def test_only_one_active_row_when_both_were_referred(self):
        boss = Client.objects.create(full_name="Босс", phone="+996700000010")
        self.keep.referred_by = boss
        self.keep.save()
        self.drop.referred_by = boss
        self.drop.save()
        self.sale(1000, client=self.keep, paid=1000)
        self.sale(1000, client=self.drop, paid=1000)
        self.assertEqual(ReferralBonus.objects.filter(voided_at__isnull=True).count(), 2)
        from clients.merge import merge_clients
        merge_clients(self.keep, self.drop, user=self.admin)
        self.assertEqual(ReferralBonus.objects.filter(voided_at__isnull=True).count(), 1)
        self.assertEqual(ReferralBonus.objects.get(voided_at__isnull=True).referred, self.keep)
