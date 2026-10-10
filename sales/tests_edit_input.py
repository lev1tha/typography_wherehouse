"""edit-items: NaN, бесконечность и лишние знаки не доходят до базы.

`Decimal("NaN")` и `Decimal("Infinity")` разбираются без ошибки и роняли запись
пятисоткой; `4.56789` принималось, на Postgres колонка (3 знака) округляла его
сама, а склад списывал сырое значение — три разных числа об одной строке.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt
from sales.sale_service import create_sale
from warehouse.models import Material


class EditItemsInputTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="eii_boss", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.mat = Material.objects.create(
            name="Клей", unit=Material.Unit.LITER, quantity=Decimal("100"),
            price_per_unit=Decimal("100"), purchase_price=Decimal("40"),
        )
        self.receipt = create_sale(
            client=None, cashier=self.admin, payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{"type": "MATERIAL", "material": self.mat, "quantity": 5}],
            pay_full=True,
        )
        self.item = self.receipt.items.get()

    def _edit(self, **fields):
        return self.client.post(
            f"/api/sales/receipts/{self.receipt.id}/edit-items/",
            {"items": [{"id": self.item.id, **fields}]}, format="json",
        )

    def _untouched(self):
        self.mat.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("95"))
        self.assertEqual(self.item.quantity, Decimal("5"))
        self.assertEqual(self.item.price_per_item, Decimal("100"))

    def test_nan_infinity_and_garbage_are_400_not_500(self):
        for bad in ("NaN", "Infinity", "-Infinity", "abc", "1e999"):
            resp = self._edit(quantity=bad)
            self.assertEqual(resp.status_code, 400, (bad, resp.data))
        self._untouched()

    def test_too_many_decimal_places_are_refused(self):
        resp = self._edit(quantity="4.56789")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("знаков", resp.data["detail"])
        self._untouched()

    def test_three_places_are_fine(self):
        resp = self._edit(quantity="4.568")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("95.432"))

    def test_zero_and_negative_quantity_still_refused(self):
        self.assertEqual(self._edit(quantity="0").status_code, 400)
        self.assertEqual(self._edit(quantity="-1").status_code, 400)
        self._untouched()

    def test_price_must_be_a_finite_money_amount(self):
        for bad in ("NaN", "Infinity", "10.999", "-1"):
            resp = self._edit(price_per_item=bad)
            self.assertEqual(resp.status_code, 400, (bad, resp.data))
        self._untouched()

    def test_a_valid_edit_still_works(self):
        resp = self._edit(quantity="3", price_per_item="120")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("97"))

    def test_a_bad_value_in_the_second_change_rolls_back_the_first(self):
        resp = self.client.post(
            f"/api/sales/receipts/{self.receipt.id}/edit-items/",
            {"items": [
                {"id": self.item.id, "quantity": "3"},
                {"id": self.item.id, "quantity": "NaN"},
            ]}, format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self._untouched()
