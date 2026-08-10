"""Сдача: переплата, которую клиенту ещё не отдали.

Заказ на 1500, принесли 3000, а мелочи в кассе не было — 1500 остались у цеха.
Это долг ЦЕХА перед клиентом, зеркальный обычному долгу. Раньше переплата просто
отбрасывалась (`min(amount_paid, total)`), и назавтра вспомнить, сколько за кем
осталось, было нечем.

Хранится отдельным полем `change_due`, а НЕ через `amount_paid > total`:
`amount_paid` участвует в долге, статусе оплаты и во всех отчётах, и «оплачено
3000» по заказу на 1500 сделало бы выручку неверной.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt
from sales.sale_service import create_sale, pay_client_debt
from warehouse.models import Material


class ChangeDueTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="boss_ch", password="x", role=User.Role.ADMIN
        )
        self.keeper = User.objects.create_user(
            username="skl_ch", password="x", role=User.Role.STOREKEEPER
        )
        self.client_obj = Client.objects.create(full_name="Тахир", phone="+996555111222")
        self.material = Material.objects.create(
            name="Крепёж",
            unit=Material.Unit.PIECE,
            quantity=Decimal("1000"),
            price_per_unit=Decimal("150"),
            piece_price=Decimal("150"),
            purchase_price=Decimal("80"),
        )

    def _sale(self, *, qty=10, paid=None, client=None):
        """Заказ на qty × 150 сом."""
        return create_sale(
            client=client if client is not None else self.client_obj,
            cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": self.material, "quantity": qty}],
            amount_paid=paid,
        )

    # ---- переплата в кассе -------------------------------------------------
    def test_overpayment_is_remembered_as_change(self):
        """Заказ 1500, принесли 3000 → 1500 сдачи остались за цехом."""
        r = self._sale(qty=10, paid=Decimal("3000"))
        self.assertEqual(r.total_price, Decimal("1500"))
        self.assertEqual(r.amount_paid, Decimal("1500"))
        self.assertEqual(r.change_due, Decimal("1500"))
        self.assertEqual(r.debt, Decimal("0"))
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.PAID)

    def test_exact_payment_leaves_no_change(self):
        r = self._sale(qty=10, paid=Decimal("1500"))
        self.assertEqual(r.change_due, Decimal("0"))

    def test_pay_full_flag_leaves_no_change(self):
        """«Заплатил ровно сколько вышло» — флагом, а не большим числом.

        Сумма чека известна только внутри create_sale, поэтому вызывающий её
        назвать не может. Раньше для этого передавали 9 999 999 и полагались на
        обрезку до суммы чека; теперь переплата запоминается сдачей, и такой
        сентинел означал бы девять миллионов долга цеха перед клиентом.
        """
        r = create_sale(
            client=self.client_obj,
            cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": self.material, "quantity": 10}],
            pay_full=True,
        )
        self.assertEqual(r.total_price, Decimal("1500"))
        self.assertEqual(r.amount_paid, Decimal("1500"))
        self.assertEqual(r.change_due, Decimal("0"))
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.PAID)

    def test_underpayment_is_debt_not_change(self):
        """Недоплата — это долг клиента, сдача тут ни при чём."""
        r = self._sale(qty=10, paid=Decimal("500"))
        self.assertEqual(r.debt, Decimal("1000"))
        self.assertEqual(r.change_due, Decimal("0"))

    def test_revenue_is_not_inflated_by_overpayment(self):
        """Главное, ради чего сдача — отдельное поле: выручка считается по
        `total_price`, и переплата не должна её раздувать."""
        r = self._sale(qty=10, paid=Decimal("3000"))
        self.assertEqual(r.total_price, Decimal("1500"))
        self.assertLessEqual(r.amount_paid, r.total_price)

    # ---- переплата при погашении долга ------------------------------------
    def test_paying_more_than_the_debt_records_change(self):
        r = self._sale(qty=10, paid=None)  # весь заказ в долг
        self.client.force_authenticate(self.admin)
        resp = self.client.post(f"/api/sales/receipts/{r.id}/pay/", {"amount": "2000"})
        self.assertEqual(resp.status_code, 200, resp.data)
        r.refresh_from_db()
        self.assertEqual(r.amount_paid, Decimal("1500"))
        self.assertEqual(r.change_due, Decimal("500"))
        self.assertEqual(r.debt, Decimal("0"))

    def test_bulk_payment_leftover_lands_on_the_last_order(self):
        """Общая выплата: остаток вешается на последний погашенный заказ — тот,
        на котором деньги кончились. Раньше он просто возвращался числом."""
        first = self._sale(qty=2, paid=None)   # 300
        second = self._sale(qty=3, paid=None)  # 450
        allocations, change = pay_client_debt(
            self.client_obj, Decimal("1000"), user=self.admin
        )
        self.assertEqual(change, Decimal("250"))  # 1000 − 300 − 450
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.change_due, Decimal("0"))
        self.assertEqual(second.change_due, Decimal("250"))

    # ---- выдача сдачи ------------------------------------------------------
    def test_giving_change_back_clears_it(self):
        r = self._sale(qty=10, paid=Decimal("3000"))
        self.client.force_authenticate(self.admin)
        resp = self.client.post(f"/api/sales/receipts/{r.id}/give-change/", {})
        self.assertEqual(resp.status_code, 200, resp.data)
        r.refresh_from_db()
        self.assertEqual(r.change_due, Decimal("0"))

    def test_partial_change_is_allowed(self):
        """Мелочи в кассе может не хватить и во второй раз — «отдал тысячу из
        полутора» рабочая ситуация, а не ошибка ввода."""
        r = self._sale(qty=10, paid=Decimal("3000"))
        self.client.force_authenticate(self.admin)
        self.client.post(f"/api/sales/receipts/{r.id}/give-change/", {"amount": "1000"})
        r.refresh_from_db()
        self.assertEqual(r.change_due, Decimal("500"))

    def test_cannot_give_more_change_than_owed(self):
        r = self._sale(qty=10, paid=Decimal("3000"))
        self.client.force_authenticate(self.admin)
        self.client.post(f"/api/sales/receipts/{r.id}/give-change/", {"amount": "99999"})
        r.refresh_from_db()
        self.assertEqual(r.change_due, Decimal("0"))  # отдали ровно 1500, не больше

    def test_cannot_give_change_when_there_is_none(self):
        r = self._sale(qty=10, paid=Decimal("1500"))
        self.client.force_authenticate(self.admin)
        resp = self.client.post(f"/api/sales/receipts/{r.id}/give-change/", {})
        self.assertEqual(resp.status_code, 400)

    def test_storekeeper_cannot_give_change(self):
        """Деньги из кассы — за админом, как оплата долга и откат."""
        r = self._sale(qty=10, paid=Decimal("3000"))
        self.client.force_authenticate(self.keeper)
        resp = self.client.post(f"/api/sales/receipts/{r.id}/give-change/", {})
        self.assertEqual(resp.status_code, 403)

    def test_unpay_clears_change_too(self):
        """Откат оплаты означает «денег не брали». Оставить сдачу значило бы,
        что цех должен сдачу с платежа, которого не было."""
        r = self._sale(qty=10, paid=Decimal("3000"))
        self.client.force_authenticate(self.admin)
        self.client.post(f"/api/sales/receipts/{r.id}/unpay/", {})
        r.refresh_from_db()
        self.assertEqual(r.amount_paid, Decimal("0"))
        self.assertEqual(r.change_due, Decimal("0"))

    # ---- где сдачу видно ---------------------------------------------------
    def test_receipts_filter_by_change(self):
        with_change = self._sale(qty=10, paid=Decimal("3000"))
        self._sale(qty=10, paid=Decimal("1500"))
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/sales/receipts/", {"has_change": "1"})
        numbers = {row["order_number"] for row in resp.data["results"]}
        self.assertEqual(numbers, {with_change.order_number})

    def test_receipts_stats_show_total_change(self):
        self._sale(qty=10, paid=Decimal("3000"))
        self._sale(qty=10, paid=Decimal("2000"))
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/sales/receipts/stats/")
        self.assertEqual(Decimal(str(resp.data["change_due"])), Decimal("2000"))

    def test_client_card_shows_change_owed_to_him(self):
        self._sale(qty=10, paid=Decimal("3000"))
        self.client.force_authenticate(self.admin)
        resp = self.client.get(f"/api/clients/clients/{self.client_obj.id}/")
        self.assertEqual(Decimal(str(resp.data["change_due"])), Decimal("1500"))

    def test_clients_filter_by_change(self):
        other = Client.objects.create(full_name="Нурлан", phone="+996555333444")
        self._sale(qty=10, paid=Decimal("3000"))
        self._sale(qty=10, paid=Decimal("1500"), client=other)
        self.client.force_authenticate(self.admin)
        resp = self.client.get("/api/clients/clients/", {"has_change": "1"})
        names = {c["display_name"] for c in resp.data["results"]}
        self.assertEqual(names, {"Тахир"})

    def test_deleting_receipt_takes_change_with_it(self):
        r = self._sale(qty=10, paid=Decimal("3000"))
        self.client.force_authenticate(self.admin)
        self.client.delete(f"/api/sales/receipts/{r.id}/")
        resp = self.client.get(f"/api/clients/clients/{self.client_obj.id}/")
        self.assertEqual(Decimal(str(resp.data["change_due"])), Decimal("0"))
