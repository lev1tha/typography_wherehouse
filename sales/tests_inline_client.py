"""Касса: клиент, заведённый «на лету», по телефону.

1. Через кассу нельзя замкнуть реферальное кольцо «А привёл Б, Б привёл А»:
   вызывается та же проверка, что и в карточке клиента.
2. Телефон совпал с существующим клиентом, а имя набрали другое: заказ уходит
   на найденного клиента (процесс не ломаем), а ответ несёт
   `client_name_mismatch: true` и `client_name` найденного — кассир увидит,
   что это, возможно, другой человек.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from warehouse.models import Material


class InlineClientTests(APITestCase):
    URL = "/api/sales/receipts/checkout/"

    def setUp(self):
        self.user = User.objects.create_user(
            username="ic_keeper", password="x", role=User.Role.STOREKEEPER
        )
        self.client.force_authenticate(self.user)
        self.material = Material.objects.create(
            name="Акрил", unit="SQM", quantity=Decimal("100"),
            price_per_unit=Decimal("360"),
        )
        self.alice = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Иван Петров", phone="+996555111222"
        )
        self.bob = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Борис", phone="+996555333444",
            referred_by=self.alice,
        )

    def _checkout(self, client_dict):
        return self.client.post(self.URL, {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": self.material.id, "quantity": 1}],
            "client": client_dict,
        }, format="json")

    # ---- кольцо ----------------------------------------------------------
    def test_a_ring_through_the_till_is_refused(self):
        """Борис приведён Иваном; кассир записывает, что Ивана привёл Борис."""
        resp = self._checkout({
            "type": "PHYSICAL", "full_name": "Иван Петров", "phone": "+996555111222",
            "referred_by": self.bob.id,
        })
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("referred_by", resp.data["client"])
        self.alice.refresh_from_db()
        self.assertIsNone(self.alice.referred_by_id)

    def test_a_normal_referrer_is_still_saved(self):
        carol = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Карина", phone="+996555777888"
        )
        resp = self._checkout({
            "type": "PHYSICAL", "full_name": "Карина", "phone": "+996555777888",
            "referred_by": self.alice.id,
        })
        self.assertEqual(resp.status_code, 201, resp.data)
        carol.refresh_from_db()
        self.assertEqual(carol.referred_by_id, self.alice.id)

    # ---- расхождение имени -------------------------------------------------
    def test_different_name_for_a_known_phone_is_flagged(self):
        resp = self._checkout({
            "type": "PHYSICAL", "full_name": "Пётр Сидоров", "phone": "0555 111 222",
        })
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIs(resp.data["client_name_mismatch"], True)
        self.assertEqual(resp.data["client_name"], "Иван Петров")
        self.assertEqual(resp.data["client"], self.alice.id)   # заказ — на найденного

    def test_same_name_in_another_case_and_spacing_is_not_a_mismatch(self):
        resp = self._checkout({
            "type": "PHYSICAL", "full_name": "  иван   ПЕТРОВ ", "phone": "+996555111222",
        })
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIs(resp.data["client_name_mismatch"], False)

    def test_new_client_and_picked_client_are_not_a_mismatch(self):
        new = self._checkout({
            "type": "PHYSICAL", "full_name": "Новый Человек", "phone": "+996555999000",
        })
        self.assertEqual(new.status_code, 201, new.data)
        self.assertIs(new.data["client_name_mismatch"], False)
        picked = self.client.post(self.URL, {
            "payment_method": "CASH", "pay_full": True, "client_id": self.alice.id,
            "items": [{"type": "MATERIAL", "material": self.material.id, "quantity": 1}],
        }, format="json")
        self.assertIs(picked.data["client_name_mismatch"], False)

    def test_no_typed_name_is_not_a_mismatch(self):
        resp = self._checkout({"type": "PHYSICAL", "phone": "+996555111222"})
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertIs(resp.data["client_name_mismatch"], False)
