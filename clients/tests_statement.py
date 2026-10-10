"""Акт сверки считает сервер (CLI-04, S1).

Раньше входящее сальдо было зашито 0,00 в PrintAct.jsx: за III квартал акт
печатал долг 5 963 вместо 19 963, а отрицательное сальдо — без знака. Теперь
выписка — `GET /api/clients/clients/<id>/statement/`, и закрывающее сальдо без
дат равно тому, что показывает карточка клиента (долг − сдача − аванс).
"""
from datetime import date
from clients.models import Client
from clients.testkit import D, ShopCase
from sales import sale_service


def kinds(rows):
    return [(r["kind"], r.get("order_number")) for r in rows]


class QuarterStatementTests(ShopCase):
    """Сценарий владельца: ОсОО, III квартал 2026."""

    def build(self):
        a = self.sale(24000, paid=10000, on=date(2026, 5, 15))
        b = self.sale(4763, paid=0, on=date(2026, 7, 20))
        c = self.sale(8500, paid=8500, on=date(2026, 8, 25))
        d = self.sale(6200, paid=3000, on=date(2026, 9, 20))
        self.pay(b, 2000, on=date(2026, 9, 28))
        return a, b, c, d

    def test_third_quarter_opening_turnover_closing(self):
        self.build()
        st = self.statement(date_from="2026-07-01", date_to="2026-09-30")
        self.assertEqual(D(str(st["opening"])), D("14000"))
        self.assertEqual(D(str(st["turnover"]["debit"])), D("19463"))
        self.assertEqual(D(str(st["turnover"]["credit"])), D("13500"))
        self.assertEqual(D(str(st["closing"])), D("19963"))
        self.assertEqual(st["closing_side"], "debt")

    def test_whole_history_equals_card_debt(self):
        self.build()
        st = self.statement()
        card = self.card()
        self.assertEqual(D(str(st["opening"])), D("0"))
        self.assertEqual(D(str(st["closing"])), D(str(card["debt"])))
        self.assertEqual(D(str(card["debt"])), D("19963"))

    def test_september_only(self):
        self.build()
        st = self.statement(date_from="2026-09-01", date_to="2026-09-30")
        # до сентября: 14000 (май) + 4763 (июль) + 0 (август оплачен) = 18763
        self.assertEqual(D(str(st["opening"])), D("18763"))
        self.assertEqual(D(str(st["closing"])), D("19963"))

    def test_periods_chain(self):
        """Входящее сальдо следующего периода = исходящее предыдущего."""
        self.build()
        q2 = self.statement(date_from="2026-04-01", date_to="2026-06-30")
        q3 = self.statement(date_from="2026-07-01", date_to="2026-09-30")
        self.assertEqual(D(str(q2["closing"])), D(str(q3["opening"])))
        self.assertEqual(D(str(q2["closing"])), D("14000"))

    def test_rows_are_dated_and_named(self):
        self.build()
        st = self.statement(date_from="2026-09-01", date_to="2026-09-30")
        got = [(r["date"], r["kind"], r["order_number"]) for r in st["rows"]]
        self.assertIn(("2026-09-20", "order", 4), got)
        self.assertIn(("2026-09-28", "payment", 2), got)
        for r in st["rows"]:
            self.assertGreaterEqual(D(str(r["debit"])), 0)
            self.assertGreaterEqual(D(str(r["credit"])), 0)


class AdvanceAndSignTests(ShopCase):
    def test_client_brought_100000_for_75000_is_advance_25000(self):
        e1 = self.sale(30000, paid=100000, on=date(2026, 8, 1))
        self.assertEqual(e1.change_due, D("70000"))
        e2 = self.sale(45000, use_change=True, on=date(2026, 9, 10))
        self.assertEqual(e2.change_applied, D("45000"))
        card = self.card()
        self.assertEqual(D(str(card["debt"])), D("0"))
        self.assertEqual(D(str(card["change_due"])), D("25000"))
        st = self.statement()
        self.assertEqual(D(str(st["closing"])), D("-25000"))
        self.assertEqual(st["closing_side"], "advance")
        self.assertIn("аванс", st["closing_label"].lower())
        self.assertEqual(D(str(st["turnover"]["debit"])), D("75000"))
        self.assertEqual(D(str(st["turnover"]["credit"])), D("100000"))

    def test_overpayment_is_negative_not_clipped(self):
        """Принесли 1 500 сверх заказа — это наш долг клиенту, не «ноль»."""
        self.sale(1000, paid=2500, on=date(2026, 9, 1))
        st = self.statement()
        self.assertEqual(D(str(st["closing"])), D("-1500"))
        self.assertEqual(st["closing_side"], "advance")

    def test_given_change_is_a_debit_row_on_its_own_date(self):
        r = self.sale(1000, paid=2500, on=date(2026, 9, 1))
        sale_service.give_change(r, D("500"), user=self.admin)
        st = self.statement()
        self.assertEqual(D(str(st["closing"])), D("-1000"))
        kinds_ = [x["kind"] for x in st["rows"]]
        self.assertIn("change_given", kinds_)
        # сколько принесли — видно целиком, а не «минус выданное»
        self.assertEqual(D(str(st["turnover"]["credit"])), D("2500"))


class DriftTests(ShopCase):
    def test_edit_items_down_keeps_statement_equal_to_card(self):
        """seed 58: заказ уменьшили после оплаты — Payment остался, amount_paid
        упал. Акт обязан сойтись с карточкой (раньше 4 453 против 5 753)."""
        a = self.sale(5000, paid=1000, on=date(2026, 9, 1))
        self.pay(a, 2300, on=date(2026, 9, 5))
        item = a.items.get()
        sale_service.update_receipt_items(
            a, [{"id": item.id, "quantity": D("1000")}], user=self.admin,
        )
        a.refresh_from_db()
        self.sale(4453, paid=0, on=date(2026, 9, 10))
        card = self.card()
        st = self.statement()
        saldo = D(str(card["debt"])) - D(str(card["change_due"]))
        self.assertEqual(D(str(st["closing"])), saldo)

    def test_refund_row_and_paid_back(self):
        r = self.sale(1000, paid=1000, on=date(2026, 9, 1))
        sale_service.refund_receipt(r, user=self.admin)
        st = self.statement()
        self.assertEqual(D(str(st["closing"])), D("0"))
        k = [x["kind"] for x in st["rows"]]
        self.assertIn("refund", k)
        self.assertIn("refund_paid", k)

    def test_unrecognized_online_invoice_is_not_in_statement(self):
        self.sale(7000, method="ONLINE", paid=None, on=date(2026, 9, 1))
        self.sale(500, paid=0, on=date(2026, 9, 2))
        st = self.statement()
        self.assertEqual(D(str(st["closing"])), D("500"))
        self.assertEqual(D(str(self.card()["debt"])), D("500"))

    def test_partial_refund_with_debt(self):
        r = self.sale(979, paid=0, on=date(2026, 9, 1))
        sale_service.refund_receipt(r, user=self.admin)
        st = self.statement()
        self.assertEqual(D(str(st["closing"])), D(str(self.card()["debt"])))


class AccessTests(ShopCase):
    def test_roles(self):
        self.sale(100, paid=0)
        for user, code in ((self.store, 200), (self.acc, 200)):
            self.client.force_authenticate(user)
            r = self.client.get(f"/api/clients/clients/{self.agency.id}/statement/")
            self.assertEqual(r.status_code, code, (user.role, r.data))
        self.client.force_authenticate(None)
        r = self.client.get(f"/api/clients/clients/{self.agency.id}/statement/")
        self.assertEqual(r.status_code, 401)

    def test_bad_dates_are_400_not_500(self):
        r = self.client.get(
            f"/api/clients/clients/{self.agency.id}/statement/", {"date_from": "вчера"}
        )
        self.assertEqual(r.status_code, 400)
        r = self.client.get(
            f"/api/clients/clients/{self.agency.id}/statement/",
            {"date_from": "2026-10-01", "date_to": "2026-09-01"},
        )
        self.assertEqual(r.status_code, 400)


class FuzzStatementTests(ShopCase):
    """Случайные цепочки операций: акт без дат всегда равен «долг − сдача − аванс».

    Старый акт расходился с карточкой в 1 из 180 состояний (seed 58, правка
    состава вниз после оплаты). Здесь ходят по всем опасным переходам: оплата,
    возврат, правка состава, откат оплаты, выдача сдачи, зачёт сдачи в новый
    заказ, удаление.
    """

    CHAINS = 20
    STEPS = 12

    def run_chain(self, seed):
        import random

        from sales.sale_service import DeleteRejected, ItemEditRejected, PaymentRejected

        rnd = random.Random(seed)
        person = Client.objects.create(full_name=f"Фаззер {seed}", phone=f"+99655500{seed:04d}")
        for _step in range(self.STEPS):
            receipts = list(person.receipts.order_by("created_at"))
            op = rnd.choice(["sale", "sale", "pay", "refund", "edit", "unpay", "give", "sale_change", "delete",
                             "pay_change", "write_off", "advance", "advance", "offset_all"])
            r = rnd.choice(receipts) if receipts else None
            try:
                if op in ("sale", "sale_change") or r is None:
                    total = rnd.choice([500, 1000, 1500, 3000, 7200])
                    paid = rnd.choice([None, 0, 200, total, total + 700])
                    self.sale(total, client=person, paid=paid, days_ago=rnd.randint(0, 40),
                              use_change=(op == "sale_change"))
                elif op == "pay" and r.debt > 0:
                    self.pay(r, rnd.choice([100, r.debt, r.debt + 300]), on=None)
                elif op == "pay_change" and r.debt > 0:
                    sale_service.apply_payment(r, rnd.choice([None, D("100")]), user=self.admin, use_change=True)
                elif op == "write_off" and r.debt > 0:
                    sale_service.apply_payment(r, rnd.choice([None, D("100")]), user=self.admin, method="WRITE_OFF")
                elif op == "advance":
                    self.client.post(f"/api/clients/clients/{person.id}/advances/",
                                     {"amount": rnd.choice([500, 2000, 9000]), "method": "CASH"}, format="json")
                elif op == "offset_all":
                    self.client.post(f"/api/clients/clients/{person.id}/pay-debt/",
                                     {"use_change": True, "amount": rnd.choice([None, "100"])}, format="json")
                elif op == "refund":
                    sale_service.refund_receipt(r, user=self.admin)
                elif op == "edit":
                    item = r.items.filter(is_returned=False).first()
                    if item:
                        sale_service.update_receipt_items(
                            r, [{"id": item.id, "quantity": D(str(rnd.choice([100, 400, 900, 2500])))}],
                            user=self.admin,
                        )
                elif op == "unpay":
                    self.client.post(f"/api/sales/receipts/{r.id}/unpay/", {}, format="json")
                elif op == "give" and r.change_due > 0:
                    sale_service.give_change(r, rnd.choice([None, D("100")]), user=self.admin)
                elif op == "delete":
                    sale_service.delete_receipt(r, user=self.admin)
            except (PaymentRejected, ItemEditRejected, DeleteRejected):
                continue
            card = self.card(person)
            saldo = D(str(card["debt"])) - D(str(card["change_due"])) - D(str(card["advance_balance"]))
            self.assertEqual(D(str(card["balance"])), saldo)
            st = self.statement(person)
            self.assertEqual(
                D(str(st["closing"])), saldo,
                f"seed {seed} шаг {_step} {op}: акт {st['closing']} ≠ карточка {saldo}",
            )

    def test_chains(self):
        for seed in range(self.CHAINS):
            self.run_chain(seed)


class OffsetAndWriteOffTests(ShopCase):
    """Зачёт сдачи в долг и списание долга видны в акте отдельными строками."""

    def test_change_offset_is_neutral_when_the_cash_trail_is_known(self):
        """Деньги внесены в августе — в акте они в августе; зачёт в сентябре
        отдельной строкой не светится (это не новые деньги)."""
        self.sale(1000, paid=2500, on=date(2026, 8, 1))                  # сдача 1 500
        b = self.sale(2000, paid=0, on=date(2026, 8, 5))                 # долг 2 000
        sale_service.apply_payment(b, None, user=self.admin, paid_on=date(2026, 9, 20), use_change=True)
        card = self.card()
        self.assertEqual(D(str(card["change_due"])), D("0"))
        self.assertEqual(D(str(card["debt"])), D("0"))
        st = self.statement()
        self.assertEqual(D(str(st["closing"])), D("0"))
        self.assertEqual([r for r in st["rows"] if r["kind"] == "offset"], [])
        cash_part = [r for r in st["rows"] if r["kind"] == "payment"]
        self.assertEqual([(r["date"], D(r["credit"])) for r in cash_part], [("2026-09-20", D("500"))])
        # на 31 августа зачёта ещё нет: долг 2 000 минус наша сдача 1 500
        aug = self.statement(date_to="2026-08-31")
        self.assertEqual(D(str(aug["closing"])), D("500"))

    def test_offset_row_stays_when_there_is_no_cash_trail(self):
        """Старые заказы без кассовой книги: источник сдачи неизвестен, зачёт
        остаётся строкой в день зачёта (сальдо на сегодня при этом верное)."""
        from finance.models import CashEntry

        self.sale(1000, paid=2500, on=date(2026, 8, 1))
        b = self.sale(2000, paid=0, on=date(2026, 8, 5))
        CashEntry.objects.all().delete()
        sale_service.apply_payment(b, None, user=self.admin, paid_on=date(2026, 9, 20), use_change=True)
        st = self.statement()
        self.assertEqual(D(str(st["closing"])), D("0"))
        offsets = [r for r in st["rows"] if r["kind"] == "offset"]
        self.assertEqual([(r["date"], D(r["credit"])) for r in offsets], [("2026-09-20", D("1500"))])

    def test_write_off_row(self):
        b = self.sale(8400, paid=0, on=date(2026, 9, 1))
        sale_service.apply_payment(b, None, user=self.admin, paid_on=date(2026, 10, 1), method="WRITE_OFF")
        st = self.statement()
        self.assertEqual(D(str(st["closing"])), D("0"))
        self.assertIn(("2026-10-01", "write_off"), [(r["date"], r["kind"]) for r in st["rows"]])
        sep = self.statement(date_to="2026-09-30")
        self.assertEqual(D(str(sep["closing"])), D("8400"))      # до списания долг ещё был


class PrepaymentHistoryTests(ShopCase):
    """Клиент внёс 100 000 в августе, заказ на 45 000 — в сентябре: на конец
    августа цех должен ему 70 000, а не 25 000 (иначе акт за период врёт)."""

    def test_balance_on_each_date(self):
        self.sale(30000, paid=100000, on=date(2026, 8, 1))
        self.sale(45000, use_change=True, on=date(2026, 9, 10))
        for day, expected in (
            ("2026-07-31", "0"), ("2026-08-31", "-70000"), ("2026-09-09", "-70000"),
            ("2026-09-30", "-25000"),
        ):
            st = self.statement(date_to=day)
            self.assertEqual(D(str(st["closing"])), D(expected), day)

    def test_offset_via_pay_change_keeps_history(self):
        self.sale(1000, paid=2500, on=date(2026, 8, 1))                      # сдача 1 500
        b = self.sale(2000, paid=0, on=date(2026, 8, 5))
        sale_service.apply_payment(b, None, user=self.admin, paid_on=date(2026, 9, 20), use_change=True)
        self.assertEqual(D(str(self.statement(date_to="2026-08-31")["closing"])), D("500"))
        self.assertEqual(D(str(self.statement()["closing"])), D("0"))
