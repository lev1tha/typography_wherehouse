"""Роли в зоне клиентов (CLI-08, D-96): бухгалтер читает, складовщик принимает долг по настройке."""
from datetime import timedelta

from django.utils import timezone

from audit.models import AuditLog
from clients.models import ClientSettings
from clients.testkit import D, ShopCase
from sales.models import Payment


class AccountantReadOnlyTests(ShopCase):
    def setUp(self):
        super().setUp()
        self.sale(1000, paid=0, days_ago=40)
        self.client.force_authenticate(self.acc)

    def test_reads_everything_a_debtors_page_needs(self):
        for url in (
            "/api/clients/clients/", f"/api/clients/clients/{self.agency.id}/",
            f"/api/clients/clients/{self.agency.id}/statement/", "/api/clients/clients/aging/",
            "/api/clients/clients/export/?has_debt=1", "/api/clients/settings/",
            f"/api/clients/clients/{self.agency.id}/advances/",
            f"/api/clients/clients/{self.agency.id}/credit-check/?amount=10",
        ):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_cannot_write_anything(self):
        base = f"/api/clients/clients/{self.agency.id}/"
        calls = [
            ("patch", base, {"company_name": "Взлом"}),
            ("post", base + "pay-debt/", {}),
            ("post", base + "advances/", {"amount": "100", "method": "CASH"}),
            ("post", base + "set-password/", {}),
            ("post", base + "referral-bonus/pay/", {"referred": self.agency.id}),
            ("patch", "/api/clients/settings/", {"storekeeper_takes_debt": False}),
            ("post", "/api/clients/clients/", {"full_name": "Новый", "phone": "+996700999111"}),
        ]
        for method, url, body in calls:
            r = getattr(self.client, method)(url, body, format="json")
            self.assertEqual(r.status_code, 403, (method, url, r.data))
        self.assertTrue(ClientSettings.load().storekeeper_takes_debt)      # настройку не тронули

    def test_accountant_sees_margin_but_does_not_edit(self):
        row = self.client.get("/api/clients/clients/").data["results"][0]
        self.assertIn("margin", row)


class StorekeeperTakesDebtTests(ShopCase):
    def setUp(self):
        super().setUp()
        self.order = self.sale(4000, paid=0, days_ago=20)
        self.url = f"/api/clients/clients/{self.agency.id}/pay-debt/"

    def test_by_default_storekeeper_takes_money(self):
        """Как и оплата по чеку (`/pay/`): складовщик принимает, запись помнит кто."""
        self.client.force_authenticate(self.store)
        r = self.client.post(self.url, {"amount": "1000"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.amount_paid, D("1000"))

    def test_owner_can_switch_it_off(self):
        ClientSettings.objects.update_or_create(pk=1, defaults={"storekeeper_takes_debt": False})
        self.client.force_authenticate(self.store)
        r = self.client.post(self.url, {"amount": "1000"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.order.refresh_from_db()
        self.assertEqual(self.order.amount_paid, D("0"))
        # админу выключатель не мешает
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post(self.url, {"amount": "1000"}, format="json").status_code, 200)

    def test_storekeeper_payment_is_journaled(self):
        self.client.force_authenticate(self.store)
        r = self.client.post(self.url, {"amount": "4000"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.debt, D("0"))
        payment = Payment.objects.get()
        self.assertEqual(payment.created_by, self.store)
        log = AuditLog.objects.filter(action__startswith="Общая выплата").get()
        self.assertEqual(log.user, self.store)
        self.assertIn("принял складовщик", log.action)

    def test_storekeeper_cannot_backdate_or_write_off(self):
        self.client.force_authenticate(self.store)
        old = (timezone.localdate() - timedelta(days=5)).isoformat()
        r = self.client.post(self.url, {"amount": "100", "paid_on": old}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        r = self.client.post(self.url, {"method": "WRITE_OFF"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("администратор", r.data["detail"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.amount_paid, D("0"))

    def test_admin_can_roll_the_payment_back(self):
        self.client.force_authenticate(self.store)
        self.client.post(self.url, {"amount": "4000"}, format="json")
        self.client.force_authenticate(self.admin)
        r = self.client.post(f"/api/sales/receipts/{self.order.id}/unpay/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.debt, D("4000"))

    def test_accountant_never(self):
        self.client.force_authenticate(self.acc)
        self.assertEqual(self.client.post(self.url, {"amount": "1"}, format="json").status_code, 403)


class JournalTests(ShopCase):
    """«Было → стало» при правке скидки, лимита, телефона, реферера (XL-07/F3)."""

    def test_each_money_edit_leaves_a_before_after_line(self):
        other = type(self.agency).objects.create(full_name="Привёл", phone="+996700888777")
        url = f"/api/clients/clients/{self.agency.id}/"
        self.client.patch(url, {"discount_percent": "5"}, format="json")
        self.client.patch(url, {"discount_percent": "7.5"}, format="json")
        self.client.patch(url, {"credit_limit": "30000"}, format="json")
        self.client.patch(url, {"phone": "+996555998877"}, format="json")
        self.client.patch(url, {"referred_by": other.id}, format="json")
        lines = list(AuditLog.objects.order_by("id").values_list("action", flat=True))
        joined = "\n".join(lines)
        self.assertIn("Изменена скидка клиента «ОсОО «Ак Жол»»: 0 → 5%", joined)
        # Новые записи — с запятой и разрядами (RU-N22, D-189).
        self.assertIn("5 → 7,5%", joined)
        self.assertIn("лимит долга клиента «ОсОО «Ак Жол»»: не задан → 30 000 сом", joined)
        self.assertIn("+996555112233 → +996555998877", joined)
        self.assertIn("реферер клиента «ОсОО «Ак Жол»»: нет → Привёл", joined)

    def test_unchanged_value_writes_nothing(self):
        url = f"/api/clients/clients/{self.agency.id}/"
        self.client.patch(url, {"discount_percent": "0", "company_name": "ОсОО «Ак Жол»"}, format="json")
        self.assertFalse(AuditLog.objects.exists())


class WriteOffButtonTests(ShopCase):
    """«Списать долг» в карточке клиента — pay-debt с method=WRITE_OFF (admin)."""

    def test_admin_writes_off_the_whole_debt_with_a_reason(self):
        self.sale(8400, paid=0, days_ago=60)
        self.sale(1600, paid=0, days_ago=30)
        r = self.client.post(f"/api/clients/clients/{self.agency.id}/pay-debt/",
                             {"method": "WRITE_OFF", "note": "клиент закрыл ИП"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(str(r.data["debt"])), D("0"))
        self.assertEqual(D(str(r.data["paid"])), D("10000"))
        # денег в кассе нет, а расход «Безнадёжные долги» есть
        from finance.models import CashEntry, ExpenseEntry
        self.assertEqual(CashEntry.objects.count(), 0)
        self.assertEqual(ExpenseEntry.objects.filter(kind__code="BAD_DEBT").count(), 2)
        log = AuditLog.objects.get(action__startswith="Списание долга клиента")
        self.assertIn("клиент закрыл ИП", log.action)
        self.assertEqual(D(str(self.statement()["closing"])), D("0"))
        kinds = [x["kind"] for x in self.statement()["rows"]]
        self.assertEqual(kinds.count("write_off"), 2)

    def test_partial_write_off(self):
        self.sale(8400, paid=0)
        r = self.client.post(f"/api/clients/clients/{self.agency.id}/pay-debt/",
                             {"method": "WRITE_OFF", "amount": "1500"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(str(r.data["debt"])), D("6900"))
