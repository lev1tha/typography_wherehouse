from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.customer import mint_customer_token
from clients.models import Client
from sales.models import Receipt, TransactionItem
from services.models import PrintingService
from warehouse.models import Material


class EdgeAddItemsCustomerTests(APITestCase):
    """Edge-cases дозаказа (add-items) и клиентского портала (login/orders).

    Эндпоинты:
      POST /api/sales/receipts/<uuid>/add-items/
      POST /api/customer/login/
      GET  /api/customer/orders/
    """

    def setUp(self):
        self.storekeeper = User.objects.create_user(
            username="store_edge", password="x", role=User.Role.STOREKEEPER
        )
        self.admin = User.objects.create_user(
            username="admin_edge", password="x", role=User.Role.ADMIN
        )
        # Material продаётся листами: piece_area задаёт списание по площади.
        self.acrylic = Material.objects.create(
            name="Акрил 3мм edge", category="Акрил", unit="SQM",
            quantity=Decimal("100"), price_per_unit=Decimal("0"),
            price_per_sqm=Decimal("1400"), piece_price=Decimal("3700"),
            piece_area=Decimal("2.00"),
        )
        self.client_a = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Клиент А",
            phone="+996555112233",
        )
        self.client_b = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Клиент Б",
            phone="+996700998877",
        )

    # ---- helpers --------------------------------------------------------

    def _make_receipt(self, *, payment_status, status=Receipt.Status.COMPLETED,
                      stock_deducted=True, client=None, payment_method=Receipt.PaymentMethod.CASH):
        """Создаёт чек напрямую (без прохода через checkout) с одной MATERIAL-позицией."""
        receipt = Receipt.objects.create(
            client=client,
            cashier=self.storekeeper,
            payment_method=payment_method,
            payment_status=payment_status,
            status=status,
            stock_deducted=stock_deducted,
        )
        TransactionItem.objects.create(
            receipt=receipt, type=TransactionItem.Type.MATERIAL, material=self.acrylic,
            quantity=Decimal("1"), price_per_item=Decimal("3700"),
            sale_mode=TransactionItem.SaleMode.PIECE,
        )
        receipt.recalculate_total()
        receipt.save(update_fields=["total_price"])
        return receipt

    def _add_items_url(self, receipt):
        return f"/api/sales/receipts/{receipt.pk}/add-items/"

    def _piece_item_payload(self, qty=1):
        return {"items": [{
            "type": "MATERIAL", "material": self.acrylic.id,
            "quantity": qty, "mode": "PIECE",
        }]}

    # ---- дозаказ: закрытые чеки → 400 -----------------------------------

    def test_add_items_to_cancelled_returns_400(self):
        self.client.force_authenticate(self.storekeeper)
        receipt = self._make_receipt(
            payment_status=Receipt.PaymentStatus.REFUNDED,
            status=Receipt.Status.CANCELLED,
        )
        before = receipt.items.count()
        r = self.client.post(self._add_items_url(receipt), self._piece_item_payload(), format="json")
        self.assertEqual(r.status_code, 400, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.items.count(), before)  # ничего не добавлено

    def test_add_items_to_refunded_returns_400(self):
        self.client.force_authenticate(self.storekeeper)
        # payment_status=REFUNDED при status=COMPLETED тоже должен блокироваться.
        receipt = self._make_receipt(
            payment_status=Receipt.PaymentStatus.REFUNDED,
            status=Receipt.Status.COMPLETED,
        )
        before = receipt.items.count()
        r = self.client.post(self._add_items_url(receipt), self._piece_item_payload(), format="json")
        self.assertEqual(r.status_code, 400, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.items.count(), before)

    # ---- дозаказ в PAID: списание склада немедленно ---------------------

    def test_add_items_to_paid_deducts_stock_immediately(self):
        self.client.force_authenticate(self.storekeeper)
        receipt = self._make_receipt(
            payment_status=Receipt.PaymentStatus.PAID, stock_deducted=True,
        )
        stock_before = self.acrylic.quantity  # 100
        total_before = receipt.total_price     # 3700
        r = self.client.post(self._add_items_url(receipt), self._piece_item_payload(qty=2), format="json")
        self.assertEqual(r.status_code, 200, r.data)
        # Списание: piece_area(2.00) × qty(2) = 4.00
        self.acrylic.refresh_from_db()
        self.assertEqual(self.acrylic.quantity, stock_before - Decimal("4.00"))
        # Total вырос на 2 × 3700 = 7400 (recalculate_total по свежим позициям).
        receipt.refresh_from_db()
        self.assertEqual(receipt.total_price, total_before + Decimal("7400"))
        self.assertEqual(receipt.items.count(), 2)

    # ---- дозаказ в PENDING: склад НЕ списывается -------------------------

    def test_add_items_to_pending_does_not_deduct_stock(self):
        self.client.force_authenticate(self.storekeeper)
        receipt = self._make_receipt(
            payment_status=Receipt.PaymentStatus.PENDING,
            stock_deducted=False,
            payment_method=Receipt.PaymentMethod.ONLINE,
        )
        stock_before = self.acrylic.quantity
        r = self.client.post(self._add_items_url(receipt), self._piece_item_payload(qty=3), format="json")
        self.assertEqual(r.status_code, 200, r.data)
        # Склад не тронут — списание произойдёт только при confirm_payment.
        self.acrylic.refresh_from_db()
        self.assertEqual(self.acrylic.quantity, stock_before)
        # Но позиция добавлена и total пересчитан.
        receipt.refresh_from_db()
        self.assertEqual(receipt.items.count(), 2)
        self.assertEqual(receipt.total_price, Decimal("3700") + Decimal("3") * Decimal("3700"))

    # ---- дозаказ в PARTIALLY_REFUNDED считается settled ------------------

    def test_add_items_to_partially_refunded_deducts_stock(self):
        self.client.force_authenticate(self.storekeeper)
        receipt = self._make_receipt(
            payment_status=Receipt.PaymentStatus.PARTIALLY_REFUNDED,
            status=Receipt.Status.COMPLETED, stock_deducted=True,
        )
        stock_before = self.acrylic.quantity
        r = self.client.post(self._add_items_url(receipt), self._piece_item_payload(qty=1), format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.acrylic.refresh_from_db()
        self.assertEqual(self.acrylic.quantity, stock_before - Decimal("2.00"))  # 2.00 × 1
        receipt.refresh_from_db()
        self.assertEqual(receipt.items.count(), 2)

    # ---- recalculate_total берёт свежие позиции -------------------------

    def test_recalculate_total_reflects_added_items(self):
        self.client.force_authenticate(self.storekeeper)
        receipt = self._make_receipt(payment_status=Receipt.PaymentStatus.PAID)
        r = self.client.post(self._add_items_url(receipt), self._piece_item_payload(qty=1), format="json")
        self.assertEqual(r.status_code, 200, r.data)
        # Ответ вьюхи (_fresh_response) должен отражать новый total в JSON.
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("7400.00"))
        self.assertEqual(len(r.data["items"]), 2)

    # ---- логин клиента: нормализация телефона ---------------------------

    def test_customer_login_normalizes_phone(self):
        # Клиент сохранён как '+996555112233'; логин с пробелами/скобками/дефисами.
        r = self.client.post(
            "/api/customer/login/",
            {"phone": "+996 (555) 11-22-33"}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("access", r.data)
        self.assertEqual(r.data["client"]["id"], self.client_a.id)

    def test_customer_login_plain_digits_match(self):
        # Цифры без '+' тоже должны совпасть с сохранённым +996...
        r = self.client.post(
            "/api/customer/login/", {"phone": "996555112233"}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["client"]["id"], self.client_a.id)

    def test_customer_login_empty_phone_400(self):
        r = self.client.post("/api/customer/login/", {"phone": ""}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_customer_login_unknown_phone_400(self):
        r = self.client.post(
            "/api/customer/login/", {"phone": "+996000000000"}, format="json",
        )
        self.assertEqual(r.status_code, 400, r.data)

    # ---- изоляция заказов клиента ---------------------------------------

    def test_customer_orders_isolated_per_client(self):
        # У A — два чека, у B — один.
        a1 = Receipt.objects.create(client=self.client_a, cashier=self.storekeeper)
        a2 = Receipt.objects.create(client=self.client_a, cashier=self.storekeeper)
        b1 = Receipt.objects.create(client=self.client_b, cashier=self.storekeeper)

        token = mint_customer_token(self.client_a)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        r = self.client.get("/api/customer/orders/")
        self.assertEqual(r.status_code, 200, getattr(r, "data", r))
        returned_ids = {str(row["id"]) for row in r.data}
        self.assertEqual(returned_ids, {str(a1.id), str(a2.id)})
        self.assertNotIn(str(b1.id), returned_ids)
        self.client.credentials()  # сброс

    def test_customer_orders_requires_customer_token(self):
        # Без токена — не аутентифицирован как клиент.
        self.client.credentials()
        r = self.client.get("/api/customer/orders/")
        self.assertIn(r.status_code, (401, 403), getattr(r, "data", r))
