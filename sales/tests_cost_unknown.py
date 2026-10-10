"""Нулевая себестоимость не проходит молча.

Материал без партий и без закупочной цены уходил в чек с `cost_total = 0`:
маржа 100%, ни предупреждения, ни следа. Продажу НЕ блокируем (так работает
цех: товар на полке есть, а цену закупки ещё не занесли), но ответ оформления
теперь несёт `warnings` с кодом `cost_unknown`, а в журнале действий остаётся
запись.

Чтобы вместо предупреждения ЗАПРЕЩАТЬ такую продажу (решение владельца), в
`_deduct_stock_for_item` (sales/sale_service.py) вместо записи в журнал
действий достаточно бросить `InsufficientStock(warning["message"])`: оформление
отвечает 400 и откатывается целиком.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from sales.models import Receipt
from warehouse.models import Material


class CostUnknownWarningTests(APITestCase):
    URL = "/api/sales/receipts/checkout/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="cu_boss", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.known = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("10"), purchase_price=Decimal("4"),
        )
        self.unknown = Material.objects.create(
            name="Диод без цены", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            price_per_unit=Decimal("10"), purchase_price=Decimal("0"),
        )

    def _checkout(self, *materials):
        return self.client.post(self.URL, {
            "payment_method": "CASH", "pay_full": True,
            "items": [
                {"type": "MATERIAL", "material": m.id, "quantity": 2} for m in materials
            ],
        }, format="json")

    def test_sale_goes_through_but_warns(self):
        resp = self._checkout(self.unknown)
        self.assertEqual(resp.status_code, 201, resp.data)
        warnings = resp.data["warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["code"], "cost_unknown")
        self.assertEqual(warnings[0]["material"], self.unknown.id)
        self.assertEqual(warnings[0]["material_name"], "Диод без цены")
        self.assertEqual(warnings[0]["item"], resp.data["items"][0]["id"])
        receipt = Receipt.objects.get(pk=resp.data["id"])
        self.assertEqual(receipt.cost_total, Decimal("0"))   # продан, как и раньше

    def test_the_warning_is_written_to_the_audit_log(self):
        resp = self._checkout(self.unknown)
        number = resp.data["order_number"]
        entry = AuditLog.objects.filter(action__contains="себестоимость неизвестна").get()
        self.assertIn(f"Чек {number}", entry.action)
        self.assertIn("Диод без цены", entry.action)
        self.assertEqual(entry.user, self.admin)

    def test_known_cost_gives_no_warning(self):
        resp = self._checkout(self.known)
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["warnings"], [])
        self.assertFalse(AuditLog.objects.filter(action__contains="себестоимость неизвестна").exists())

    def test_only_the_unknown_line_is_reported(self):
        resp = self._checkout(self.known, self.unknown)
        self.assertEqual([w["material"] for w in resp.data["warnings"]], [self.unknown.id])

    def test_add_items_reports_it_too(self):
        first = self._checkout(self.known)
        resp = self.client.post(
            f"/api/sales/receipts/{first.data['id']}/add-items/",
            {"items": [{"type": "MATERIAL", "material": self.unknown.id, "quantity": 1}]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual([w["code"] for w in resp.data["warnings"]], ["cost_unknown"])
