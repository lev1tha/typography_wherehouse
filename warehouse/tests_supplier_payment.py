"""Оплата поставщику попадает в кассовую книгу.

Проверка прод-данных 19.09.2026: за месяц материала закуплено на 1 678 477
сомов, а «Оплат поставщику» в кассе — ноль. Остаток «сколько сейчас в ящике»
про эти деньги не знал вовсе.

Главное правило: система НЕ УГАДЫВАЕТ. Она не может знать, отдали за поставку
деньги или взяли в долг, поэтому пишет расход только когда при приёмке выбрали
счёт. Не выбрали — записи нет, как было раньше; так же ведут себя старые вызовы
API и приходы, внесённые до этого дня.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import CashEntry
from warehouse.models import Material, Roll

CASH = CashEntry.Account.CASH
BANK = CashEntry.Account.BANK


class SupplierPaymentTests(APITestCase):
    ROLL = "/api/warehouse/materials/receive-roll/"
    QUICK = "/api/warehouse/materials/supply/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="sp_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.sheet = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            price_per_sqm=Decimal("1000"), sheet_width=Decimal("1"), sheet_height=Decimal("2"),
        )
        self.piece = Material.objects.create(
            name="Саморез", unit=Material.Unit.PIECE,
            quantity=Decimal("0"), purchase_price=Decimal("10"),
        )

    def _lot(self, **extra):
        payload = {
            "material": self.sheet.id, "form": "SHEET",
            "width": "1", "height": "2", "sheet_count": "5",
            "purchase_cost": "12000", **extra,
        }
        r = self.client.post(self.ROLL, payload, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r

    def test_paid_in_cash_leaves_the_drawer(self):
        self._lot(payment="CASH")
        entry = CashEntry.objects.get()
        self.assertEqual(entry.kind, CashEntry.Kind.OUT)
        self.assertEqual(entry.article, CashEntry.Article.SUPPLY)
        self.assertEqual(entry.account, CASH)
        self.assertEqual(entry.amount, Decimal("12000"))
        self.assertEqual(CashEntry.balance(CASH), Decimal("-12000"))

    def test_paid_by_transfer_touches_the_bank(self):
        self._lot(payment="BANK")
        self.assertEqual(CashEntry.balance(BANK), Decimal("-12000"))
        self.assertEqual(CashEntry.balance(CASH), Decimal("0"))

    def test_on_credit_writes_nothing(self):
        """Материал приехал, деньги не ушли."""
        self._lot(payment="DEBT")
        self.assertFalse(CashEntry.objects.exists())

    def test_silence_is_not_a_payment(self):
        """Старый вызов без поля не должен выдумывать расход."""
        self._lot()
        self.assertFalse(CashEntry.objects.exists())

    def test_quick_intake_of_pieces_pays_too(self):
        r = self.client.post(self.QUICK, {
            "material": self.piece.id, "quantity": "100",
            "actual_price": "10", "payment": "CASH",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(CashEntry.balance(CASH), Decimal("-1000"))

    def test_payment_is_dated_by_the_intake_not_today(self):
        """Поставку вносят задним числом — расход датируется её днём."""
        self._lot(payment="CASH", received_on="2026-09-01")
        self.assertEqual(CashEntry.objects.get().happened_on.isoformat(), "2026-09-01")

    def test_deleting_the_lot_takes_the_payment_with_it(self):
        """Иначе в кассе остался бы расход за материал, которого нет."""
        self._lot(payment="CASH")
        Roll.objects.get().delete()
        self.assertFalse(CashEntry.objects.exists())
        self.assertEqual(CashEntry.balance(CASH), Decimal("0"))

    def test_storekeeper_cannot_receive_at_all(self):
        """Приёмка админская — значит и деньги из кассы трогает только админ."""
        keeper = User.objects.create_user(
            username="sp_keeper", password="x", role=User.Role.STOREKEEPER
        )
        self.client.force_authenticate(keeper)
        r = self.client.post(self.ROLL, {
            "material": self.sheet.id, "form": "SHEET", "width": "1", "height": "2",
            "sheet_count": "5", "purchase_cost": "12000", "payment": "CASH",
        }, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertFalse(CashEntry.objects.exists())


class SupplyDocumentPaymentTests(APITestCase):
    """Накладная платится ЦЕЛИКОМ, одной записью, а не построчно.

    Платят за документ, и в кассовой книге он должен читаться так же. Оплата
    бывает частичной — в кассу уходит ровно внесённая сумма, остаток честно
    висит долгом поставщику (`Supply.debt`).
    """

    URL = "/api/warehouse/supplies/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="sdp_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.sheet = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            price_per_sqm=Decimal("1470"),
        )

    def _create(self, **over):
        data = {
            "received_on": "2026-09-10",
            "paid_amount": "0",
            "lines": [{
                "material": self.sheet.id, "form": "SHEET",
                "width": "1.2", "height": "2.4", "sheet_count": "10", "cost": "48000",
            }],
            **over,
        }
        r = self.client.post(self.URL, data, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def test_paid_document_is_one_cash_row(self):
        self._create(paid_amount="48000", paid_account="CASH")
        entry = CashEntry.objects.get()
        self.assertEqual(entry.amount, Decimal("48000"))
        self.assertEqual(entry.article, CashEntry.Article.SUPPLY)
        self.assertEqual(entry.happened_on.isoformat(), "2026-09-10")

    def test_partial_payment_writes_only_what_was_paid(self):
        data = self._create(paid_amount="20000", paid_account="CASH")
        self.assertEqual(CashEntry.balance(CASH), Decimal("-20000"))
        self.assertEqual(Decimal(str(data["debt"])), Decimal("28000"))

    def test_document_taken_on_credit_writes_nothing(self):
        self._create(paid_amount="0", paid_account="")
        self.assertFalse(CashEntry.objects.exists())

    def test_cancelling_the_document_returns_the_money(self):
        data = self._create(paid_amount="48000", paid_account="CASH")
        r = self.client.delete(f"{self.URL}{data['id']}/")
        self.assertIn(r.status_code, (200, 204), getattr(r, "data", None))
        self.assertEqual(CashEntry.balance(CASH), Decimal("0"))
