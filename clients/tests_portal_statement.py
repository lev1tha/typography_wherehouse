"""Выписка и сальдо в кабинете клиента (CLI-13).

Клиент сам видит акт сверки за период и своё сальдо — без внутренних цифр
(себестоимость, маржа) и внутренних примечаний сотрудников.
"""
from datetime import date

from clients.customer import mint_customer_token
from clients.models import Client
from clients.testkit import D, ShopCase


class PortalStatementTests(ShopCase):
    def setUp(self):
        super().setUp()
        self.sale(24000, paid=10000, on=date(2026, 5, 15))
        b = self.sale(4763, paid=0, on=date(2026, 7, 20))
        self.pay(b, 2000, on=date(2026, 9, 28))
        self.stranger = Client.objects.create(full_name="Чужой", phone="+996700777000")
        self.sale(9999, client=self.stranger, paid=0, on=date(2026, 8, 1))
        self.client.force_authenticate(None)

    def get(self, path, token=None, **params):
        if token is not None:
            self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return self.client.get(path, params)

    def test_client_reads_own_statement_for_a_period(self):
        token = mint_customer_token(self.agency)
        r = self.get("/api/customer/statement/", token, date_from="2026-07-01", date_to="2026-09-30")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(str(r.data["opening"])), D("14000"))
        self.assertEqual(D(str(r.data["closing"])), D("16763"))     # 14 000 + 4 763 − 2 000
        self.assertEqual(r.data["closing_side"], "debt")
        text = str(r.data)
        self.assertNotIn("Чужой", text)

    def test_no_internal_notes_or_costs(self):
        token = mint_customer_token(self.agency)
        r = self.get("/api/customer/statement/", token)
        for row in r.data["rows"]:
            self.assertNotIn("note", row)
        self.assertNotIn("margin", str(r.data))
        self.assertNotIn("cost", str(r.data))

    def test_summary_has_balance_with_advance(self):
        self.client.force_authenticate(self.admin)
        # Без зачёта в долг (D-165: по умолчанию аванс при долге гасит долг) —
        # здесь проверяется, что кабинет показывает аванс рядом с долгом.
        self.client.post(f"/api/clients/clients/{self.agency.id}/advances/",
                         {"amount": "5000", "method": "CASH", "offset_debt": False}, format="json")
        self.client.force_authenticate(None)
        token = mint_customer_token(self.agency)
        r = self.get("/api/customer/summary/", token)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(str(r.data["debt"])), D("16763"))
        self.assertEqual(D(str(r.data["advance_balance"])), D("5000"))
        self.assertEqual(D(str(r.data["balance"])), D("11763"))

    def test_staff_token_and_anonymous_are_refused(self):
        # так же, как и у списка заказов кабинета: без клиентского токена не пускаем
        orders = self.get("/api/customer/orders/").status_code
        self.assertIn(orders, (401, 403))
        self.assertEqual(self.get("/api/customer/statement/").status_code, orders)
        self.assertEqual(self.get("/api/customer/summary/").status_code, orders)
        from rest_framework_simplejwt.tokens import AccessToken

        staff = str(AccessToken.for_user(self.admin))        # настоящий токен сотрудника
        self.assertEqual(self.get("/api/customer/statement/", staff).status_code, 401)
        self.assertEqual(self.get("/api/customer/summary/", staff).status_code, 401)

    def test_bad_dates(self):
        token = mint_customer_token(self.agency)
        self.assertEqual(self.get("/api/customer/statement/", token, date_from="x").status_code, 400)
