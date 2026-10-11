"""Лимит долга клиента и общие настройки клиентов (CLI-03, D-92).

По умолчанию лимита нет и предупреждений нет — поведение прода не меняется,
пока владелец не задаст значения. Складовщик принимает оплату долга (как и по
чеку, `/pay/`), владелец может это выключить.
"""
from audit.models import AuditLog
from clients.credit import credit_warnings
from clients.models import Client, ClientSettings
from clients.testkit import D, ShopCase


class DefaultsTests(ShopCase):
    def test_everything_is_off_by_default(self):
        s = ClientSettings.load()
        self.assertIsNone(s.default_credit_limit)
        self.assertTrue(s.storekeeper_takes_debt)
        self.assertIsNone(self.agency.credit_limit)
        self.assertIsNone(self.agency.effective_credit_limit)
        self.sale(80000, paid=0, days_ago=130)
        self.assertEqual(credit_warnings(self.agency, D("30000")), [])

    def test_card_and_list_expose_limits(self):
        card = self.card()
        self.assertIsNone(card["credit_limit"])
        self.assertIsNone(card["effective_credit_limit"])
        row = self.client.get("/api/clients/clients/").data["results"][0]
        self.assertIn("credit_limit", row)


class SettingsApiTests(ShopCase):
    URL = "/api/clients/settings/"

    def test_everyone_reads_only_admin_writes(self):
        for user in (self.admin, self.store, self.acc):
            self.client.force_authenticate(user)
            r = self.client.get(self.URL)
            self.assertEqual(r.status_code, 200, (user.role, r.data))
            self.assertIsNone(r.data["default_credit_limit"])
        self.client.force_authenticate(self.store)
        self.assertEqual(self.client.patch(self.URL, {"default_credit_limit": "5000"}, format="json").status_code, 403)
        self.client.force_authenticate(self.acc)
        self.assertEqual(self.client.patch(self.URL, {"storekeeper_takes_debt": True}, format="json").status_code, 403)

    def test_admin_changes_and_it_is_journaled(self):
        r = self.client.patch(self.URL, {"default_credit_limit": "50000", "storekeeper_takes_debt": False}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        s = ClientSettings.load()
        self.assertEqual(s.default_credit_limit, D("50000"))
        self.assertFalse(s.storekeeper_takes_debt)
        log = AuditLog.objects.filter(action__startswith="Настройки клиентов").get()
        self.assertIn("не задан → 50000", log.action)
        self.assertIn("вкл → выкл", log.action)

    def test_negative_limit_is_refused(self):
        r = self.client.patch(self.URL, {"default_credit_limit": "-1"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_empty_limit_clears_it(self):
        ClientSettings.objects.update_or_create(pk=1, defaults={"default_credit_limit": D("100")})
        r = self.client.patch(self.URL, {"default_credit_limit": None}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIsNone(ClientSettings.load().default_credit_limit)


class ClientLimitTests(ShopCase):
    def url(self):
        return f"/api/clients/clients/{self.agency.id}/"

    def test_admin_sets_limit_and_it_is_journaled(self):
        r = self.client.patch(self.url(), {"credit_limit": "50000"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.agency.refresh_from_db()
        self.assertEqual(self.agency.credit_limit, D("50000"))
        self.assertEqual(D(str(r.data["effective_credit_limit"])), D("50000"))
        log = AuditLog.objects.filter(action__startswith="Изменён лимит долга клиента").get()
        self.assertIn("не задан → 50 000 сом", log.action)
        # и обратно: убрать лимит
        self.client.patch(self.url(), {"credit_limit": None}, format="json")
        log = AuditLog.objects.filter(action__startswith="Изменён лимит долга клиента").first()
        self.assertIn("50 000 → не задан", log.action)

    def test_storekeeper_cannot_set_limit(self):
        self.client.force_authenticate(self.store)
        r = self.client.patch(self.url(), {"credit_limit": "50000"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("только администратор", str(r.data["credit_limit"]))
        self.agency.refresh_from_db()
        self.assertIsNone(self.agency.credit_limit)

    def test_storekeeper_keeps_other_edits(self):
        self.client.force_authenticate(self.store)
        r = self.client.patch(self.url(), {"company_name": "ОсОО «Ак Жол Плюс»"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

    def test_negative_limit_refused(self):
        r = self.client.patch(self.url(), {"credit_limit": "-5"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_own_limit_beats_default(self):
        ClientSettings.objects.update_or_create(pk=1, defaults={"default_credit_limit": D("10000")})
        self.assertEqual(Client.objects.get(pk=self.agency.pk).effective_credit_limit, D("10000"))
        self.agency.credit_limit = D("70000")
        self.agency.save()
        self.assertEqual(self.agency.effective_credit_limit, D("70000"))
        self.agency.credit_limit = D("0")      # 0 — «в долг нельзя», не «лимита нет»
        self.agency.save()
        self.assertEqual(self.agency.effective_credit_limit, D("0"))


class WarningsTests(ShopCase):
    def test_over_limit_warns_with_numbers(self):
        self.sale(80000, paid=0, days_ago=130)
        self.agency.credit_limit = D("100000")
        self.agency.save()
        w = credit_warnings(self.agency, D("30000"))
        self.assertEqual([x["code"] for x in w], ["debt_over_limit"])
        self.assertEqual(D(str(w[0]["debt"])), D("80000"))
        self.assertEqual(D(str(w[0]["debt_after"])), D("110000"))
        self.assertEqual(D(str(w[0]["limit"])), D("100000"))
        self.assertEqual(w[0]["source"], "client")
        # влезает в лимит — предупреждения нет
        self.assertEqual(credit_warnings(self.agency, D("15000")), [])

    def test_fully_paid_order_never_warns(self):
        self.sale(80000, paid=0, days_ago=130)
        self.agency.credit_limit = D("1")
        self.agency.save()
        self.assertEqual(credit_warnings(self.agency, D("0")), [])

    def test_default_limit_applies_to_clients_without_own(self):
        ClientSettings.objects.update_or_create(pk=1, defaults={"default_credit_limit": D("50000")})
        self.sale(40000, paid=0, days_ago=5)
        w = credit_warnings(Client.objects.get(pk=self.agency.pk), D("20000"))
        self.assertEqual(w[0]["source"], "default")
        self.assertEqual(D(str(w[0]["limit"])), D("50000"))

    def test_no_client_no_warnings(self):
        self.assertEqual(credit_warnings(None, D("100")), [])

    def test_credit_check_endpoint(self):
        self.sale(80000, paid=0, days_ago=130)
        self.agency.credit_limit = D("100000")
        self.agency.save()
        url = f"/api/clients/clients/{self.agency.id}/credit-check/"
        r = self.client.get(url, {"amount": "30000"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual([w["code"] for w in r.data["warnings"]], ["debt_over_limit"])
        self.assertEqual(D(str(r.data["debt"])), D("80000"))
        for user in (self.store, self.acc):
            self.client.force_authenticate(user)
            self.assertEqual(self.client.get(url, {"amount": "1"}).status_code, 200)
        self.assertEqual(self.client.get(url, {"amount": "abc"}).status_code, 400)


class CheckoutContractTests(ShopCase):
    """Касса читает `client.credit_limit` (контракт с продажами)."""

    def test_checkout_reports_debt_over_limit(self):
        self.sale(80000, paid=0, days_ago=130)
        self.agency.credit_limit = D("100000")
        self.agency.save()
        self.client.force_authenticate(self.store)
        r = self.client.post("/api/sales/receipts/checkout/", {
            "client_id": self.agency.id, "payment_method": "CASH", "amount_paid": "0",
            "items": [{"type": "MATERIAL", "material": self.coin.id, "quantity": 30000, "mode": "PIECE"}],
        }, format="json")
        text = str(r.data)
        self.assertIn("debt_over_limit", text, (r.status_code, r.data))
