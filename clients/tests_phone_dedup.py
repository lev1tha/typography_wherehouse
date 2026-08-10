"""Один человек — один клиент, как бы ни записали его номер.

Телефон хранится ровно так, как его набрали, а искали по строке — поэтому
`+996555111222` и `0555 111 222` заводили ДВУХ клиентов. Заказы и долг одного
человека расходились по двум карточкам: «Тахир ака» и «ака Тахир» оказывались
разными людьми.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client, ReferralChangeRequest
from clients.phones import find_client_by_phone, phone_key
from sales.models import Receipt
from warehouse.models import Material


class PhoneKeyTests(APITestCase):
    def test_same_number_written_differently_has_one_key(self):
        variants = [
            "+996555111222",
            "996555111222",
            "0555111222",
            "0555 111 222",
            "+996 (555) 11-12-22",
            "555111222",
        ]
        self.assertEqual(len({phone_key(v) for v in variants}), 1)

    def test_different_numbers_keep_different_keys(self):
        self.assertNotEqual(phone_key("+996555111222"), phone_key("+996700333444"))


class ClientLookupTests(APITestCase):
    def setUp(self):
        self.tahir = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="ака Тахир", phone="+996555111222"
        )

    def test_finds_client_by_any_spelling(self):
        for variant in ["0555111222", "0555 111 222", "+996 (555) 11-12-22", "555111222"]:
            self.assertEqual(find_client_by_phone(variant), self.tahir, variant)

    def test_does_not_find_a_stranger(self):
        self.assertIsNone(find_client_by_phone("+996700333444"))

    def test_empty_phone_finds_nothing(self):
        self.assertIsNone(find_client_by_phone(""))
        self.assertIsNone(find_client_by_phone(None))

    def test_short_fragment_matches_nothing(self):
        """Огрызок номера совпал бы со слишком многим — такой поиск не считается."""
        self.assertIsNone(find_client_by_phone("222"))

    def test_exact_digits_win_over_tail(self):
        """Если хвосты совпали у двух номеров, точное совпадение важнее."""
        landline = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Городской", phone="555111222"
        )
        self.assertEqual(find_client_by_phone("555111222"), landline)
        self.assertEqual(find_client_by_phone("+996555111222"), self.tahir)


class CheckoutReusesClientTests(APITestCase):
    """Касса не должна заводить второго клиента под тем же номером."""

    URL = "/api/sales/receipts/checkout/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin_dedup", password="x", role=User.Role.ADMIN
        )
        self.material = Material.objects.create(
            name="Форекс дедуп",
            unit=Material.Unit.SQM,
            quantity=Decimal("100"),
            price_per_unit=Decimal("0"),
            piece_price=Decimal("1000"),
            piece_area=Decimal("1"),
        )
        self.tahir = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="ака Тахир", phone="+996555111222"
        )

    def _sale(self, client_payload):
        self.client.force_authenticate(self.admin)
        return self.client.post(
            self.URL,
            {
                "payment_method": "CASH",
                "client": client_payload,
                "amount_paid": "1000",
                "items": [
                    {"type": "MATERIAL", "material": self.material.id, "quantity": 1, "mode": "PIECE"}
                ],
            },
            format="json",
        )

    def test_other_spelling_of_the_phone_reuses_the_client(self):
        """Номер в другом формате и имя задом наперёд — тот же человек."""
        resp = self._sale({"type": "PHYSICAL", "full_name": "Тахир ака", "phone": "0555 111 222"})
        self.assertEqual(resp.status_code, 201, resp.data)

        self.assertEqual(Client.objects.count(), 1)
        receipt = Receipt.objects.get(id=resp.data["id"])
        self.assertEqual(receipt.client, self.tahir)

    def test_receipts_stay_on_one_client_card(self):
        """Оба заказа висят на одной карточке — долг не разъезжается надвое."""
        self._sale({"type": "PHYSICAL", "full_name": "ака Тахир", "phone": "+996555111222"})
        self._sale({"type": "PHYSICAL", "full_name": "Тахир ака", "phone": "0555111222"})
        self.assertEqual(Client.objects.count(), 1)
        self.assertEqual(self.tahir.receipts.count(), 2)

    def test_new_number_still_creates_a_client(self):
        """Другой номер — действительно другой человек, его заводим."""
        resp = self._sale({"type": "PHYSICAL", "full_name": "Новый", "phone": "+996700333444"})
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Client.objects.count(), 2)

    def test_existing_name_is_not_overwritten(self):
        """Имя в карточке остаётся прежним: заказчик его сам правит, и подменять
        его написанием из последнего чека нельзя."""
        self._sale({"type": "PHYSICAL", "full_name": "Тахир ака", "phone": "0555111222"})
        self.tahir.refresh_from_db()
        self.assertEqual(self.tahir.full_name, "ака Тахир")


class ManualClientCreationTests(APITestCase):
    """Заведение клиента руками (Клиенты → «+ Новый клиент»)."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin_manual", password="x", role=User.Role.ADMIN
        )
        self.tahir = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="ака Тахир", phone="+996555111222"
        )
        self.client.force_authenticate(self.admin)

    def test_duplicate_number_is_rejected_with_a_name(self):
        resp = self.client.post(
            "/api/clients/clients/",
            {"type": "PHYSICAL", "full_name": "Тахир ака", "phone": "0555 111 222"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        # Сообщение должно называть, под кем номер уже записан, — иначе неясно,
        # что делать дальше.
        self.assertIn("ака Тахир", str(resp.data))
        self.assertEqual(Client.objects.count(), 1)

    def test_editing_the_same_client_is_allowed(self):
        """Свой же номер не должен считаться занятым при правке карточки."""
        resp = self.client.patch(
            f"/api/clients/clients/{self.tahir.id}/",
            {"phone": "+996 555 111 222"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_a_genuinely_new_number_passes(self):
        resp = self.client.post(
            "/api/clients/clients/",
            {"type": "PHYSICAL", "full_name": "Другой", "phone": "+996700333444"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)


class MergeClientsTests(APITestCase):
    """Склейка двойников: всё переезжает на одну карточку, вторая исчезает."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin_merge", password="x", role=User.Role.ADMIN
        )
        self.keeper = User.objects.create_user(
            username="keeper_merge", password="x", role=User.Role.STOREKEEPER
        )
        self.material = Material.objects.create(
            name="Форекс слияние",
            unit=Material.Unit.SQM,
            quantity=Decimal("100"),
            price_per_unit=Decimal("0"),
            piece_price=Decimal("1000"),
            piece_area=Decimal("1"),
        )
        self.keep = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="ака Тахир", phone="+996555111222"
        )
        self.drop = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Тахир ака", phone="0777 999 888"
        )

    def _order(self, client, *, paid):
        from sales.sale_service import create_sale

        return create_sale(
            client=client,
            cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {"type": "MATERIAL", "material": self.material, "quantity": 1, "mode": "PIECE"}
            ],
            amount_paid=Decimal("1000") if paid else None,
        )

    def _merge(self, user=None):
        self.client.force_authenticate(user or self.admin)
        return self.client.post(
            f"/api/clients/clients/{self.keep.id}/merge/",
            {"from": str(self.drop.id)},
            format="json",
        )

    def test_orders_and_debt_move_to_one_card(self):
        self._order(self.keep, paid=True)
        self._order(self.drop, paid=False)   # 1000 в долг на второй карточке

        resp = self._merge()
        self.assertEqual(resp.status_code, 200, resp.data)

        self.assertFalse(Client.objects.filter(pk=self.drop.pk).exists())
        self.assertEqual(self.keep.receipts.count(), 2)
        self.assertEqual(
            sum((r.debt for r in self.keep.receipts.all()), Decimal("0")), Decimal("1000")
        )

    def test_preview_shows_what_moves_before_confirming(self):
        self._order(self.drop, paid=False)
        self.client.force_authenticate(self.admin)
        resp = self.client.get(
            f"/api/clients/clients/{self.keep.id}/merge-preview/", {"from": self.drop.id}
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["orders"], 1)
        self.assertEqual(Decimal(str(resp.data["debt"])), Decimal("1000"))
        # Предпросмотр ничего не трогает.
        self.assertTrue(Client.objects.filter(pk=self.drop.pk).exists())

    def test_referrals_follow_the_surviving_card(self):
        brought = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Приведённый", phone="+996700555666",
            referred_by=self.drop,
        )
        self._merge()
        brought.refresh_from_db()
        self.assertEqual(brought.referred_by, self.keep)

    def test_referral_requests_survive_the_merge(self):
        """Заявка ссылается на удаляемую карточку через CASCADE — без переноса
        она исчезла бы вместе с ней."""
        req = ReferralChangeRequest.objects.create(
            client=self.keep, new_referred_by=self.drop, requested_by=self.keeper
        )
        self._merge()
        req.refresh_from_db()
        # Ссылка на саму себя не остаётся: клиент не может привести сам себя.
        self.assertIsNone(req.new_referred_by)

    def test_telegram_and_password_are_not_lost(self):
        self.drop.telegram_chat_id = "12345"
        self.drop.set_password("999888")
        self.drop.save()
        self._merge()
        self.keep.refresh_from_db()
        self.assertEqual(self.keep.telegram_chat_id, "12345")
        self.assertTrue(self.keep.check_password("999888"))

    def test_kept_card_keeps_its_own_name_and_phone(self):
        self._merge()
        self.keep.refresh_from_db()
        self.assertEqual(self.keep.full_name, "ака Тахир")
        self.assertEqual(self.keep.phone, "+996555111222")

    def test_self_referral_is_not_created(self):
        """Остающаяся карточка была «приведена» удаляемой — после склейки это
        стало бы «привёл сам себя»."""
        self.keep.referred_by = self.drop
        self.keep.save(update_fields=["referred_by"])
        self._merge()
        self.keep.refresh_from_db()
        self.assertIsNone(self.keep.referred_by)

    def test_cannot_merge_a_card_into_itself(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post(
            f"/api/clients/clients/{self.keep.id}/merge/",
            {"from": str(self.keep.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertTrue(Client.objects.filter(pk=self.keep.pk).exists())

    def test_unknown_card_is_rejected(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post(
            f"/api/clients/clients/{self.keep.id}/merge/", {"from": 999999}, format="json"
        )
        self.assertEqual(resp.status_code, 404, resp.data)

    def test_storekeeper_cannot_merge(self):
        """Склейка необратима и двигает чужие заказы — только админ."""
        resp = self._merge(user=self.keeper)
        self.assertEqual(resp.status_code, 403, resp.data)
        self.assertTrue(Client.objects.filter(pk=self.drop.pk).exists())
