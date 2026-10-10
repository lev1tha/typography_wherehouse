"""Оплата поставщику при создании накладной — право администратора.

`update()` и `pay()` накладной требуют роль админа, а `create()` принимал
`paid_amount`/`paid_account` от кого угодно — и складовщик одним запросом писал
расход в кассу. Накладная без оплаты складовщику по-прежнему доступна.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import CashEntry
from warehouse.models import Material, Supply


class SupplyCreatePaymentPermissionTests(APITestCase):
    URL = "/api/warehouse/supplies/"

    def setUp(self):
        self.keeper = User.objects.create_user(
            username="sp_keeper", password="x", role=User.Role.STOREKEEPER
        )
        self.admin = User.objects.create_user(
            username="sp_boss", password="x", role=User.Role.ADMIN
        )
        self.piece = Material.objects.create(
            name="Саморез", unit=Material.Unit.PIECE, quantity=Decimal("0"),
            purchase_price=Decimal("10"),
        )

    def _payload(self, **extra):
        return {
            "received_on": "2026-10-01",
            "lines": [{
                "material": self.piece.id, "form": "QTY", "quantity": "100", "cost": "1000",
            }],
            **extra,
        }

    def test_storekeeper_cannot_pay_the_supplier_through_create(self):
        self.client.force_authenticate(self.keeper)
        resp = self.client.post(
            self.URL, self._payload(paid_amount="1000", paid_account="CASH"), format="json"
        )
        self.assertEqual(resp.status_code, 403, resp.data)
        self.assertFalse(Supply.objects.exists())
        self.assertFalse(CashEntry.objects.exists())

    def test_storekeeper_cannot_name_a_payment_account_either(self):
        self.client.force_authenticate(self.keeper)
        resp = self.client.post(self.URL, self._payload(paid_account="BANK"), format="json")
        self.assertEqual(resp.status_code, 403, resp.data)

    def test_storekeeper_still_creates_a_supply_without_payment(self):
        self.client.force_authenticate(self.keeper)
        # Номера разные: одинаковый ввод (поставщик + сумма + дата) система
        # принимает за двойной и просит подтверждения (F11) — это другой тест.
        for n, extra in enumerate(({}, {"paid_amount": 0}, {"paid_amount": "0", "paid_account": ""})):
            resp = self.client.post(
                self.URL, self._payload(number=f"К-{n}", **extra), format="json"
            )
            self.assertEqual(resp.status_code, 201, (extra, resp.data))
        self.assertFalse(CashEntry.objects.exists())

    def test_admin_can_pay_on_create(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post(
            self.URL, self._payload(paid_amount="1000", paid_account="CASH"), format="json"
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(CashEntry.balance(CashEntry.Account.CASH), Decimal("-1000"))
