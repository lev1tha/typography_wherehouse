"""E2E API tests for client referrals ("кто кого привёл").

Covers the reported bug: a client could appear to refer themselves and the
referral never stuck (the select reopened after refresh). These pin the
contract: self-referral is rejected as a `referred_by` field error, a valid
referral persists, and a referral is locked once set.
"""
from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client, ReferralChangeRequest


class ReferralAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="store_t", password="x", role=User.Role.STOREKEEPER
        )
        self.client.force_authenticate(self.user)
        self.alice = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Алиса", phone="+700001"
        )
        self.bob = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Боб", phone="+700002"
        )

    def test_self_referral_rejected_as_field_error(self):
        r = self.client.patch(
            f"/api/clients/clients/{self.alice.id}/",
            {"referred_by": self.alice.id}, format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("referred_by", r.data)  # field error, not a bare detail
        self.alice.refresh_from_db()
        self.assertIsNone(self.alice.referred_by_id)

    def test_valid_referral_persists(self):
        r = self.client.patch(
            f"/api/clients/clients/{self.bob.id}/",
            {"referred_by": self.alice.id}, format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.referred_by_id, self.alice.id)
        # And it is exposed back so the UI can show the lock.
        detail = self.client.get(f"/api/clients/clients/{self.bob.id}/").data
        self.assertEqual(detail["referred_by"], self.alice.id)
        self.assertEqual(detail["referred_by_name"], "Алиса")

    def test_referral_locked_once_set(self):
        self.bob.referred_by = self.alice
        self.bob.save()
        carol = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Кэрол", phone="+700003"
        )
        r = self.client.patch(
            f"/api/clients/clients/{self.bob.id}/",
            {"referred_by": carol.id}, format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.referred_by_id, self.alice.id)

    def test_referrals_count_and_list(self):
        self.bob.referred_by = self.alice
        self.bob.save()
        detail = self.client.get(f"/api/clients/clients/{self.alice.id}/").data
        self.assertEqual(detail["referrals_count"], 1)
        self.assertEqual(detail["referrals"]["count"], 1)
        self.assertEqual(detail["referrals"]["list"][0]["display_name"], "Боб")


class ReferralChangeRequestTests(APITestCase):
    """Storekeeper files a change request; admin approves or rejects it."""

    def setUp(self):
        self.store = User.objects.create_user(
            username="store_r", password="x", role=User.Role.STOREKEEPER
        )
        self.admin = User.objects.create_user(
            username="admin_r", password="x", role=User.Role.ADMIN
        )
        self.alice = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Алиса", phone="+710001"
        )
        self.carol = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Кэрол", phone="+710003"
        )
        # Bob already has Alice as referrer (locked).
        self.bob = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Боб", phone="+710002",
            referred_by=self.alice,
        )

    def _request_change(self, to_client):
        return self.client.post(
            f"/api/clients/clients/{self.bob.id}/request-referral-change/",
            {"referred_by": to_client.id, "reason": "ошиблись"}, format="json",
        )

    def test_storekeeper_files_pending_request(self):
        self.client.force_authenticate(self.store)
        r = self._request_change(self.carol)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["status"], "PENDING")
        # Referrer not changed yet.
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.referred_by_id, self.alice.id)
        self.assertEqual(
            ReferralChangeRequest.objects.filter(client=self.bob).count(), 1
        )

    def test_duplicate_pending_rejected(self):
        self.client.force_authenticate(self.store)
        self.assertEqual(self._request_change(self.carol).status_code, 201)
        r = self._request_change(self.carol)
        self.assertEqual(r.status_code, 400)

    def test_admin_approve_applies_change(self):
        self.client.force_authenticate(self.store)
        req_id = self._request_change(self.carol).data["id"]
        self.client.force_authenticate(self.admin)
        r = self.client.post(f"/api/clients/referral-requests/{req_id}/approve/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], "APPROVED")
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.referred_by_id, self.carol.id)

    def test_admin_reject_keeps_referrer(self):
        self.client.force_authenticate(self.store)
        req_id = self._request_change(self.carol).data["id"]
        self.client.force_authenticate(self.admin)
        r = self.client.post(
            f"/api/clients/referral-requests/{req_id}/reject/",
            {"reason": "нет оснований"}, format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], "REJECTED")
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.referred_by_id, self.alice.id)

    def test_storekeeper_cannot_approve(self):
        self.client.force_authenticate(self.store)
        req_id = self._request_change(self.carol).data["id"]
        r = self.client.post(f"/api/clients/referral-requests/{req_id}/approve/")
        self.assertEqual(r.status_code, 403)

    def test_admin_can_change_referrer_directly(self):
        self.client.force_authenticate(self.admin)
        r = self.client.patch(
            f"/api/clients/clients/{self.bob.id}/",
            {"referred_by": self.carol.id}, format="json",
        )
        self.assertEqual(r.status_code, 200)
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.referred_by_id, self.carol.id)

    def test_pending_request_exposed_in_client_detail(self):
        self.client.force_authenticate(self.store)
        self._request_change(self.carol)
        detail = self.client.get(f"/api/clients/clients/{self.bob.id}/").data
        self.assertIsNotNone(detail["pending_referral_request"])
        self.assertEqual(
            detail["pending_referral_request"]["new_referred_by"], self.carol.id
        )


class ClientListFilteringTests(APITestCase):
    """Список клиентов: счётчик заказов, долг, период и поиск по номеру чека.

    Отдельно закрываем классическую ловушку агрегации: если считать заказы и
    долг одним join'ом без distinct, чек с несколькими позициями задваивает
    и то и другое.
    """

    URL = "/api/clients/clients/"

    def setUp(self):
        from decimal import Decimal

        from django.utils import timezone
        from sales.models import Receipt, TransactionItem
        from warehouse.models import Material

        self.staff = User.objects.create_user(
            username="cl_store", password="x", role=User.Role.STOREKEEPER
        )
        self.client.force_authenticate(self.staff)

        self.mat = Material.objects.create(name="М", category="К", quantity=Decimal("100"))
        self.a = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Анара", phone="+996700111000"
        )
        self.b = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Бакыт", phone="+996700222000"
        )

        def receipt(client, *, day, total, paid, items=1, status=Receipt.Status.COMPLETED,
                    pay=Receipt.PaymentStatus.PENDING):
            r = Receipt.objects.create(
                client=client, total_price=Decimal(total), amount_paid=Decimal(paid),
                payment_status=pay, status=status,
            )
            for _ in range(items):
                TransactionItem.objects.create(
                    receipt=r, type=TransactionItem.Type.MATERIAL, material=self.mat,
                    quantity=Decimal("1"), price_per_item=Decimal("1"),
                )
            Receipt.objects.filter(pk=r.pk).update(
                created_at=timezone.make_aware(
                    timezone.datetime.combine(day, timezone.datetime.min.time())
                )
            )
            return r

        from datetime import date

        # Анара: июньский чек на 3 позиции с долгом 100 + июльский без долга.
        self.june = receipt(self.a, day=date(2026, 6, 10), total="100", paid="0", items=3)
        receipt(self.a, day=date(2026, 7, 10), total="50", paid="50",
                pay=Receipt.PaymentStatus.PAID)
        # Бакыт: только отменённый — не должен считаться заказом.
        receipt(self.b, day=date(2026, 6, 12), total="900", paid="0",
                status=Receipt.Status.CANCELLED)

    def _by_name(self, rows, name):
        return next(r for r in rows if r["display_name"] == name)

    def test_orders_count_ignores_item_rows_and_cancelled(self):
        rows = self.client.get(self.URL).data["results"]
        # У Анары 2 заказа, а не 4 (первый чек — из трёх позиций).
        self.assertEqual(self._by_name(rows, "Анара")["orders_count"], 2)
        # Отменённый чек Бакыта не считается.
        self.assertEqual(self._by_name(rows, "Бакыт")["orders_count"], 0)

    def test_debt_not_multiplied_by_item_count(self):
        from decimal import Decimal

        rows = self.client.get(self.URL).data["results"]
        self.assertEqual(Decimal(str(self._by_name(rows, "Анара")["debt"])), Decimal("100"))
        # Долг по отменённому чеку не висит.
        self.assertEqual(Decimal(str(self._by_name(rows, "Бакыт")["debt"])), Decimal("0"))

    def test_period_filters_clients_and_counts(self):
        r = self.client.get(self.URL, {"date_from": "2026-07-01", "date_to": "2026-07-31"})
        rows = r.data["results"]
        names = [x["display_name"] for x in rows]
        self.assertIn("Анара", names)
        self.assertNotIn("Бакыт", names)  # в июле не заказывал
        # За июль у Анары один заказ…
        self.assertEqual(self._by_name(rows, "Анара")["orders_count"], 1)

    def test_debt_stays_current_even_with_period(self):
        from decimal import Decimal

        # …но долг остаётся «на сейчас», хотя должный чек — июньский.
        r = self.client.get(self.URL, {"date_from": "2026-07-01", "date_to": "2026-07-31"})
        anara = self._by_name(r.data["results"], "Анара")
        self.assertEqual(Decimal(str(anara["debt"])), Decimal("100"))

    def test_search_by_receipt_number(self):
        rows = self.client.get(self.URL, {"search": str(self.june.order_number)}).data["results"]
        self.assertEqual([x["display_name"] for x in rows], ["Анара"])

    def test_ordering_by_orders_count(self):
        rows = self.client.get(self.URL, {"ordering": "-orders_count"}).data["results"]
        self.assertEqual(rows[0]["display_name"], "Анара")

    def test_card_orders_follow_the_period(self):
        detail = self.client.get(
            f"{self.URL}{self.a.id}/", {"date_from": "2026-07-01", "date_to": "2026-07-31"}
        ).data
        self.assertEqual(len(detail["orders"]), 1)
        # Без периода — вся история.
        full = self.client.get(f"{self.URL}{self.a.id}/").data
        self.assertEqual(len(full["orders"]), 2)
