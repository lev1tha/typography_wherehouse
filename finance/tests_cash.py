"""Касса и банк: сколько денег есть сейчас.

В системе были только ОБОРОТЫ — выручка, расходы, долги. На вопрос «сколько
сейчас должно быть в ящике» ответить было нечем, а это то, чем закрывают день.

Главное правило, которое здесь проверяется: в кассу попадает то, что клиент
ПРИНЁС, а не то, что зачлось за заказ. Заказ на 36, дал 100 — в ящике 100, и 64
из них уйдут сдачей. Запиши мы зачтённые 36, выдача сдачи увела бы кассу в минус.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from finance.models import CashEntry
from sales import sale_service
from sales.models import Receipt
from warehouse.models import Material

CASH = CashEntry.Account.CASH
BANK = CashEntry.Account.BANK


class CashBookTests(APITestCase):
    URL = "/api/finance/cash/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="ca_admin", password="x", role=User.Role.ADMIN
        )
        self.accountant = User.objects.create_user(
            username="ca_acc", password="x", role=User.Role.ACCOUNTANT
        )
        self.keeper = User.objects.create_user(
            username="ca_keeper", password="x", role=User.Role.STOREKEEPER
        )
        self.client.force_authenticate(self.admin)
        self.customer = Client.objects.create(full_name="Клиент", phone="+996700000777")
        self.material = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE,
            quantity=Decimal("500"), price_per_unit=Decimal("18"),
            purchase_price=Decimal("10"),
        )

    def _sale(self, *, paid, method=Receipt.PaymentMethod.CASH, qty="2"):
        return sale_service.create_sale(
            client=self.customer, cashier=self.admin, payment_method=method,
            items_data=[{
                "type": "MATERIAL", "material": self.material,
                "quantity": Decimal(qty), "mode": "SQM",
            }],
            amount_paid=Decimal(paid),
        )

    # ---- что пишется само ---------------------------------------------------
    def test_cash_sale_lands_in_the_drawer(self):
        self._sale(paid="36")
        self.assertEqual(CashEntry.balance(CASH), Decimal("36"))

    def test_drawer_holds_what_the_customer_brought_not_what_was_settled(self):
        """Заказ на 36, клиент дал 100 — в ящике 100. Сдача уйдёт отдельно."""
        receipt = self._sale(paid="100")
        self.assertEqual(receipt.total_price, Decimal("36"))
        self.assertEqual(receipt.change_due, Decimal("64"))
        self.assertEqual(CashEntry.balance(CASH), Decimal("100"))

    def test_giving_change_takes_it_back_out(self):
        receipt = self._sale(paid="100")
        sale_service.give_change(receipt, user=self.admin)
        self.assertEqual(CashEntry.balance(CASH), Decimal("36"))

    def test_transfer_goes_to_the_bank_not_the_drawer(self):
        """MBank и DemirBank — переводы: в ящике их нет, и складывать с
        наличными нельзя, иначе остаток не сойдётся с пересчётом."""
        self._sale(paid="36", method=Receipt.PaymentMethod.MBANK)
        self.assertEqual(CashEntry.balance(CASH), Decimal("0"))
        self.assertEqual(CashEntry.balance(BANK), Decimal("36"))

    def test_debt_payment_is_income_too(self):
        receipt = self._sale(paid="0")
        sale_service.apply_payment(receipt, amount=Decimal("20"), user=self.admin)
        self.assertEqual(CashEntry.balance(CASH), Decimal("20"))

    def test_refund_takes_money_out(self):
        receipt = self._sale(paid="36")
        sale_service.refund_receipt(receipt, user=self.admin)
        self.assertEqual(CashEntry.balance(CASH), Decimal("0"))

    def test_refund_of_an_unpaid_order_moves_nothing(self):
        """Денег не брали — возвращать из кассы нечего."""
        receipt = self._sale(paid="0")
        sale_service.refund_receipt(receipt, user=self.admin)
        self.assertEqual(CashEntry.balance(CASH), Decimal("0"))

    def test_unpay_writes_a_counter_entry_not_a_deletion(self):
        """Откат оплаты — встречный расход, а не подчистка: по книге должно
        быть видно, что деньги приходили и их откатили."""
        receipt = self._sale(paid="100")
        self.client.post(f"/api/sales/receipts/{receipt.id}/unpay/", {})
        self.assertEqual(CashEntry.balance(CASH), Decimal("0"))
        self.assertEqual(
            CashEntry.objects.filter(article=CashEntry.Article.UNPAY).count(), 1
        )
        # Приход остался в книге — история не переписывается.
        self.assertEqual(
            CashEntry.objects.filter(article=CashEntry.Article.SALE).count(), 1
        )

    def test_sale_date_dates_the_cash_entry(self):
        """Заказ задним числом принёс деньги тогда же, а не сегодня."""
        from django.utils import timezone
        from datetime import datetime, time

        when = timezone.make_aware(datetime.combine(timezone.localdate().replace(day=1), time(12)))
        sale_service.create_sale(
            client=self.customer, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": "MATERIAL", "material": self.material,
                "quantity": Decimal("1"), "mode": "SQM",
            }],
            amount_paid=Decimal("18"), created_at=when,
        )
        entry = CashEntry.objects.get(article=CashEntry.Article.SALE)
        self.assertEqual(entry.happened_on, when.date())

    # ---- что вносят руками --------------------------------------------------
    def test_admin_records_a_manual_expense(self):
        """Выдача больше остатка требует подтверждения, но проходит.

        Кассу вносят не по порядку: расходы за неделю сегодня, приходы завтра.
        Поэтому «в кассе меньше» — это вопрос, а не запрет: без подтверждения
        сервер отказывает (чаще всего это лишний ноль), с подтверждением
        записывает как есть.
        """
        body = {
            "account": "CASH", "kind": "OUT", "article": "SALARY",
            "amount": "8000", "happened_on": "2026-08-10", "note": "аванс мастеру",
        }
        first = self.client.post(self.URL, body, format="json")
        self.assertEqual(first.status_code, 400, first.data)
        self.assertIn("подтвердите", str(first.data).lower())

        resp = self.client.post(self.URL, {**body, "confirm_negative": True}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertFalse(resp.data["is_auto"])
        self.assertEqual(CashEntry.balance(CASH), Decimal("-8000"))

    def test_auto_only_articles_are_rejected_by_hand(self):
        """Оплату руками вносить нельзя: она придёт из чека, и вторая запись
        развела бы кассу с продажами."""
        resp = self.client.post(self.URL, {
            "account": "CASH", "kind": "IN", "article": "SALE", "amount": "100",
        }, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_zero_amount_is_not_an_operation(self):
        resp = self.client.post(self.URL, {
            "account": "CASH", "kind": "IN", "article": "DEPOSIT", "amount": "0",
        }, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_system_entry_cannot_be_edited_or_deleted(self):
        self._sale(paid="36")
        entry = CashEntry.objects.get(article=CashEntry.Article.SALE)
        self.assertEqual(
            self.client.delete(f"{self.URL}{entry.id}/").status_code, 400
        )
        patch = self.client.patch(f"{self.URL}{entry.id}/", {"amount": "1"}, format="json")
        self.assertEqual(patch.status_code, 400)
        self.assertEqual(CashEntry.balance(CASH), Decimal("36"))

    def test_manual_entry_can_be_deleted(self):
        created = self.client.post(self.URL, {
            "account": "CASH", "kind": "IN", "article": "DEPOSIT", "amount": "500",
        }, format="json").data
        self.assertEqual(self.client.delete(f"{self.URL}{created['id']}/").status_code, 204)
        self.assertEqual(CashEntry.balance(CASH), Decimal("0"))

    # ---- пересчёт -----------------------------------------------------------
    def test_counting_writes_the_shortfall_as_its_own_line(self):
        """Как инвентаризация склада: недостача записывается, а не затирается —
        иначе теряется единственный след того, что деньги пропали."""
        self._sale(paid="100")
        resp = self.client.post(f"{self.URL}count/", {"account": "CASH", "counted": "90"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Decimal(resp.data["diff"]), Decimal("-10"))
        self.assertEqual(CashEntry.balance(CASH), Decimal("90"))
        self.assertEqual(
            CashEntry.objects.filter(article=CashEntry.Article.COUNT).count(), 1
        )

    def test_counting_a_match_writes_nothing(self):
        self._sale(paid="100")
        resp = self.client.post(f"{self.URL}count/", {"account": "CASH", "counted": "100"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(CashEntry.objects.filter(article=CashEntry.Article.COUNT).exists())

    def test_counting_needs_a_number(self):
        resp = self.client.post(f"{self.URL}count/", {"account": "CASH"}, format="json")
        self.assertEqual(resp.status_code, 400)

    # ---- остаток и обороты --------------------------------------------------
    def test_balance_endpoint_splits_by_account(self):
        self._sale(paid="100")
        self._sale(paid="36", method=Receipt.PaymentMethod.MBANK)
        data = self.client.get(f"{self.URL}balance/").data
        by = {a["account"]: a for a in data["accounts"]}
        self.assertEqual(Decimal(str(by["CASH"]["balance"])), Decimal("100"))
        self.assertEqual(Decimal(str(by["BANK"]["balance"])), Decimal("36"))
        self.assertEqual(Decimal(str(data["total"])), Decimal("136"))

    def test_period_moves_turnover_but_not_the_balance(self):
        """«Остаток за июль» звучит бессмысленно: остаток всегда на сейчас, а
        обороты — за период. Иначе у двух людей это разные цифры."""
        self._sale(paid="100")
        data = self.client.get(f"{self.URL}balance/", {
            "date_from": "2020-01-01", "date_to": "2020-01-31",
        }).data
        cash = next(a for a in data["accounts"] if a["account"] == "CASH")
        self.assertEqual(Decimal(str(cash["income"])), Decimal("0"))
        self.assertEqual(Decimal(str(cash["balance"])), Decimal("0"))

    # ---- права --------------------------------------------------------------
    def test_accountant_reads_but_does_not_write(self):
        self.client.force_authenticate(self.accountant)
        self.assertEqual(self.client.get(self.URL).status_code, 200)
        resp = self.client.post(self.URL, {
            "account": "CASH", "kind": "IN", "article": "DEPOSIT", "amount": "100",
        }, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_storekeeper_has_no_access_to_the_cash_book(self):
        """Деньги закрыты от складовщика так же, как финотчёт: он их принимает,
        но остатки цеха — не его."""
        self.client.force_authenticate(self.keeper)
        self.assertEqual(self.client.get(self.URL).status_code, 403)
        self.assertEqual(self.client.get(f"{self.URL}balance/").status_code, 403)

    def test_storekeeper_payment_still_reaches_the_book(self):
        """Складовщик кассы не видит — но принятые им деньги в неё попадают."""
        sale_service.create_sale(
            client=self.customer, cashier=self.keeper,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": "MATERIAL", "material": self.material,
                "quantity": Decimal("1"), "mode": "SQM",
            }],
            amount_paid=Decimal("18"),
        )
        self.assertEqual(CashEntry.balance(CASH), Decimal("18"))
