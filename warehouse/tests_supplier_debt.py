"""Долг поставщикам виден и гасится через кассу.

Аудит 26.09, п. 3: приход «в долг» одиночной кнопкой не записывал долг
нигде — система знала, что материал пришёл, но не знала, что за него должны.
А накладную «оплачивали» правкой поля: долг становился нулём, 48 000 уходили
из ящика без строки в книге.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import CashEntry
from warehouse.models import Material, Roll, Supply

CASH, BANK = CashEntry.Account.CASH, CashEntry.Account.BANK
REPORT = "/api/finance/report/"


class LotOnCreditTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="sd_admin", password="x", role=User.Role.ADMIN
        )
        self.keeper = User.objects.create_user(
            username="sd_keeper", password="x", role=User.Role.STOREKEEPER
        )
        self.client.force_authenticate(self.admin)
        self.acrylic = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET, price_per_sqm=Decimal("1500"),
        )

    def _receive(self, payment):
        r = self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": self.acrylic.id, "form": "SHEET", "width": "1.22",
            "height": "2.44", "sheet_count": "10", "purchase_cost": "48000",
            "payment": payment,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return Roll.objects.latest("id")

    def test_lot_on_credit_becomes_a_supplier_debt(self):
        lot = self._receive("DEBT")
        self.assertEqual(lot.supplier_debt, Decimal("48000.00"))
        suppliers = self.client.get(REPORT).data["suppliers"]
        self.assertEqual(Decimal(str(suppliers["total"])), Decimal("48000.00"))
        self.assertEqual(suppliers["rows"][0]["kind"], "LOT")
        self.assertEqual(CashEntry.balance(), Decimal("0"))

    def test_lot_paid_on_intake_owes_nothing(self):
        lot = self._receive("CASH")
        self.assertEqual(lot.supplier_debt, Decimal("0"))
        self.assertEqual(CashEntry.balance(CASH), Decimal("-48000"))

    def test_paying_the_lot_moves_money_and_debt_together(self):
        lot = self._receive("DEBT")
        url = f"/api/warehouse/rolls/{lot.id}/pay-supplier/"
        r = self.client.post(url, {"amount": "20000", "account": "BANK"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(str(r.data["supplier_debt"])), Decimal("28000.00"))
        self.assertEqual(CashEntry.balance(BANK), Decimal("-20000"))
        # Пустая сумма — остаток целиком.
        r = self.client.post(url, {"account": "CASH"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(CashEntry.balance(CASH), Decimal("-28000"))
        self.assertEqual(Decimal(str(self.client.get(REPORT).data["suppliers"]["total"])), Decimal("0"))

    def test_cannot_overpay_or_pay_without_an_account(self):
        lot = self._receive("DEBT")
        url = f"/api/warehouse/rolls/{lot.id}/pay-supplier/"
        self.assertEqual(self.client.post(url, {"amount": "50000", "account": "CASH"},
                                          format="json").status_code, 400)
        self.assertEqual(self.client.post(url, {"amount": "100"}, format="json").status_code, 400)
        self.assertEqual(CashEntry.balance(), Decimal("0"))

    def test_storekeeper_cannot_pay_suppliers(self):
        lot = self._receive("DEBT")
        self.client.force_authenticate(self.keeper)
        r = self.client.post(f"/api/warehouse/rolls/{lot.id}/pay-supplier/",
                             {"account": "CASH"}, format="json")
        self.assertEqual(r.status_code, 403)


class SupplyPaymentTests(APITestCase):
    URL = "/api/warehouse/supplies/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="sp2_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.sheet = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            price_per_sqm=Decimal("1470"),
        )
        r = self.client.post(self.URL, {
            "received_on": "2026-09-10", "paid_amount": "0",
            "lines": [{"material": self.sheet.id, "form": "SHEET",
                       "width": "1.2", "height": "2.4", "sheet_count": "10", "cost": "48000"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.supply = Supply.objects.get(pk=r.data["id"])

    def test_paying_by_editing_the_document_reaches_the_cash(self):
        """Критерий аудита: 0 → 48 000 наличными уменьшает кассу на 48 000."""
        r = self.client.patch(f"{self.URL}{self.supply.id}/",
                              {"paid_amount": "48000", "paid_account": "CASH"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(CashEntry.balance(CASH), Decimal("-48000"))
        # Обратная правка возвращает деньги.
        r = self.client.patch(f"{self.URL}{self.supply.id}/", {"paid_amount": "0"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(CashEntry.balance(CASH), Decimal("0"))

    def test_editing_payment_without_an_account_is_refused(self):
        r = self.client.patch(f"{self.URL}{self.supply.id}/", {"paid_amount": "1000"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.supply.refresh_from_db()
        self.assertEqual(self.supply.paid_amount, Decimal("0"))

    def test_switching_the_account_moves_the_money(self):
        self.client.patch(f"{self.URL}{self.supply.id}/",
                          {"paid_amount": "10000", "paid_account": "CASH"}, format="json")
        r = self.client.patch(f"{self.URL}{self.supply.id}/", {"paid_account": "BANK"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(CashEntry.balance(CASH), Decimal("0"))
        self.assertEqual(CashEntry.balance(BANK), Decimal("-10000"))

    def test_pay_action_pays_part_of_the_debt(self):
        r = self.client.post(f"{self.URL}{self.supply.id}/pay/",
                             {"amount": "30000", "account": "BANK"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(str(r.data["debt"])), Decimal("18000.00"))
        self.assertEqual(CashEntry.balance(BANK), Decimal("-30000"))
        suppliers = self.client.get(REPORT).data["suppliers"]
        self.assertEqual(Decimal(str(suppliers["total"])), Decimal("18000.00"))
