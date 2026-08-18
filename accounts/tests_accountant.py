"""Роль бухгалтера: смотрит деньги, но ничего не меняет.

Бухгалтер — ПРОВЕРЯЮЩИЙ. Ему открыты журнал действий, финансовый отчёт, обзор и
чеки с себестоимостью и маржой. Оформлять продажи, принимать деньги, править
склад и заводить клиентов он не может — иначе он проверял бы собственную работу.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client, ReferralChangeRequest
from sales.models import Receipt


class AccountantAccessTests(APITestCase):
    def setUp(self):
        self.accountant = User.objects.create_user(
            username="buh", password="x", role=User.Role.ACCOUNTANT
        )
        self.admin = User.objects.create_user(
            username="boss", password="x", role=User.Role.ADMIN
        )
        self.keeper = User.objects.create_user(
            username="skl", password="x", role=User.Role.STOREKEEPER
        )
        self.client_obj = Client.objects.create(full_name="Тахир", phone="+996555111222")
        self.receipt = Receipt.objects.create(
            client=self.client_obj,
            cashier=self.keeper,
            payment_method=Receipt.PaymentMethod.CASH,
            payment_status=Receipt.PaymentStatus.PAID,
            total_price=Decimal("1000"),
            amount_paid=Decimal("1000"),
        )

    # ---- что бухгалтеру ОТКРЫТО -------------------------------------------
    def test_reads_audit_log(self):
        """Ради этого роль и заводилась: проверять журнал действий."""
        self.client.force_authenticate(self.accountant)
        self.assertEqual(self.client.get("/api/audit/logs/").status_code, 200)

    def test_reads_finance_report_and_dashboard(self):
        self.client.force_authenticate(self.accountant)
        self.assertEqual(self.client.get("/api/finance/report/").status_code, 200)
        self.assertEqual(self.client.get("/api/audit/dashboard/").status_code, 200)

    def test_reads_receipts_with_cost_and_margin(self):
        """Себестоимость и маржа приходят ему так же, как админу: это его работа.
        Складовщику на тех же строках приходит null."""
        self.client.force_authenticate(self.accountant)
        resp = self.client.get("/api/sales/receipts/")
        self.assertEqual(resp.status_code, 200)
        row = resp.data["results"][0]
        self.assertIsNotNone(row["margin"])
        self.assertIsNotNone(row["cost_total"])

        self.client.force_authenticate(self.keeper)
        row = self.client.get("/api/sales/receipts/").data["results"][0]
        self.assertIsNone(row["margin"])
        self.assertIsNone(row["cost_total"])

    def test_unlocks_finance_password(self):
        """Финансовый пароль — вход в раздел, который ему открыли. Отдать 403 на
        разблокировке значило бы закрыть весь раздел."""
        self.client.force_authenticate(self.accountant)
        resp = self.client.post("/api/finance/unlock/", {"password": "wrong"})
        # Пароль неверный — но это 403 про ПАРОЛЬ, а не про роль: сама вьюха ему
        # доступна. Проверяем, что не «нет прав» ещё до сверки пароля.
        self.assertEqual(resp.status_code, 403)
        self.assertIn("пароль", str(resp.data.get("detail", "")).lower())

    # ---- что бухгалтеру ЗАКРЫТО -------------------------------------------
    def test_cannot_create_sale(self):
        self.client.force_authenticate(self.accountant)
        resp = self.client.post("/api/sales/receipts/checkout/", {"items": []}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_cannot_delete_or_edit_receipt(self):
        self.client.force_authenticate(self.accountant)
        self.assertEqual(
            self.client.delete(f"/api/sales/receipts/{self.receipt.id}/").status_code, 403
        )
        self.assertEqual(
            self.client.patch(
                f"/api/sales/receipts/{self.receipt.id}/", {"title": "правка"}, format="json"
            ).status_code,
            403,
        )

    def test_cannot_take_payment(self):
        self.client.force_authenticate(self.accountant)
        resp = self.client.post(f"/api/sales/receipts/{self.receipt.id}/pay/", {"amount": "100"})
        self.assertEqual(resp.status_code, 403)

    def test_cannot_create_client(self):
        self.client.force_authenticate(self.accountant)
        resp = self.client.post(
            "/api/clients/clients/", {"full_name": "Новый", "phone": "+996700000001"}
        )
        self.assertEqual(resp.status_code, 403)

    def test_cannot_touch_warehouse_or_prices(self):
        self.client.force_authenticate(self.accountant)
        self.assertEqual(
            self.client.post("/api/warehouse/materials/", {"name": "Х"}).status_code, 403
        )
        self.assertEqual(
            self.client.post("/api/services/services/", {"name": "Х"}).status_code, 403
        )

    def test_cannot_file_a_referral_change_request(self):
        """Единственная дыра в матрице прав, которую находил аудит.

        `permission_classes` на экшене ЗАМЕЩАЮТ список вьюсета, а не дополняют
        его: там оставался голый `IsAuthenticated`, и бухгалтер заводил заявку
        на смену реферера, оставляя в журнале действий запись от своего имени.
        Реферальный бонус это деньги, а он их проверяет.
        """
        other = Client.objects.create(full_name="Другой", phone="+996555999888")
        self.client.force_authenticate(self.accountant)
        resp = self.client.post(
            f"/api/clients/clients/{self.client_obj.id}/request-referral-change/",
            {"referred_by": other.id, "reason": "проверка"}, format="json",
        )
        self.assertEqual(resp.status_code, 403, resp.data)
        self.assertEqual(
            ReferralChangeRequest.objects.count(), 0,
            "бухгалтер оставил в системе запись",
        )

    def test_storekeeper_can_still_file_a_referral_change_request(self):
        """Складовщику эта дверь открыта — её и закрывать было не нужно."""
        other = Client.objects.create(full_name="Ещё один", phone="+996555999777")
        self.client.force_authenticate(self.keeper)
        resp = self.client.post(
            f"/api/clients/clients/{self.client_obj.id}/request-referral-change/",
            {"referred_by": other.id, "reason": "ошиблись при оформлении"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_cannot_add_expense_entry(self):
        """Финансы он читает, но траты вносит админ: иначе бухгалтер вписывал бы
        расход и сам же его проверял."""
        self.client.force_authenticate(self.accountant)
        resp = self.client.post("/api/finance/expense-entries/", {"kind": 1, "amount": "100"})
        self.assertEqual(resp.status_code, 403)

    def test_storekeeper_still_has_no_finance(self):
        """Роль бухгалтера не должна была приоткрыть деньги складовщику."""
        self.client.force_authenticate(self.keeper)
        self.assertEqual(self.client.get("/api/finance/report/").status_code, 403)
        self.assertEqual(self.client.get("/api/audit/logs/").status_code, 403)

    def test_admin_keeps_full_access(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get("/api/finance/report/").status_code, 200)
        self.assertEqual(
            self.client.patch(
                f"/api/sales/receipts/{self.receipt.id}/", {"title": "правка"}, format="json"
            ).status_code,
            200,
        )
