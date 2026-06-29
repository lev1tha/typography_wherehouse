from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt, TransactionItem
from sales.sale_service import create_sale
from warehouse.models import Material


class EdgeReceiptStatsTests(APITestCase):
    """Edge-cases for the receipts summary (/api/sales/receipts/stats/) and the
    fulfillment status transitions (mark-ready / mark-issued), including role
    scoping and ?search filtering.
    """

    STATS_URL = "/api/sales/receipts/stats/"

    def setUp(self):
        self.store_a = User.objects.create_user(
            username="store_a", password="x", role=User.Role.STOREKEEPER
        )
        self.store_b = User.objects.create_user(
            username="store_b", password="x", role=User.Role.STOREKEEPER
        )
        self.admin = User.objects.create_user(
            username="admin_s", password="x", role=User.Role.ADMIN
        )
        # A simple piece material (3700/sheet) for material-only receipts.
        self.material = Material.objects.create(
            name="Акрил лист",
            category="Акрил",
            unit=Material.Unit.SQM,
            quantity=Decimal("100"),
            price_per_unit=Decimal("0"),
            piece_price=Decimal("3700"),
            piece_area=Decimal("2"),
        )
        self.client_one = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Иван Первый", phone="+790001"
        )
        self.client_two = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Пётр Второй", phone="+790002"
        )

    # ---- helpers -----------------------------------------------------------

    def _material_receipt(self, cashier, *, client=None, amount_paid=None):
        """Cash material-only receipt. amount_paid=None → fully PAID; a value
        below total → stays PENDING with debt."""
        return create_sale(
            client=client,
            cashier=cashier,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": "MATERIAL",
                "material": self.material,
                "quantity": 1,
                "mode": "PIECE",
            }],
            amount_paid=amount_paid,
        )

    def _service_receipt(self, cashier, *, fulfillment, client=None):
        """Build a receipt that carries a SERVICE line directly (so it counts
        toward working/ready) without touching pricing/stock plumbing."""
        receipt = Receipt.objects.create(
            client=client,
            cashier=cashier,
            payment_method=Receipt.PaymentMethod.CASH,
            payment_status=Receipt.PaymentStatus.PAID,
            amount_paid=Decimal("200"),
            total_price=Decimal("200"),
            fulfillment_status=fulfillment,
        )
        TransactionItem.objects.create(
            receipt=receipt,
            type=TransactionItem.Type.SERVICE,
            quantity=Decimal("1"),
            price_per_item=Decimal("200"),
        )
        receipt.total_price = receipt.recalculate_total()
        receipt.save(update_fields=["total_price"])
        return receipt

    def _stats(self, user, query=""):
        self.client.force_authenticate(user)
        url = self.STATS_URL + (f"?{query}" if query else "")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp.data

    # ---- role scoping ------------------------------------------------------

    def test_storekeeper_stats_only_own_receipts(self):
        self._material_receipt(self.store_a)
        self._material_receipt(self.store_b)  # foreign receipt
        data = self._stats(self.store_a)
        self.assertEqual(data["total"], 1)

    def test_admin_stats_sees_all_receipts(self):
        self._material_receipt(self.store_a)
        self._material_receipt(self.store_b)
        data = self._stats(self.admin)
        self.assertEqual(data["total"], 2)

    # ---- working / ready depend on has_service -----------------------------

    def test_working_counts_only_service_receipts(self):
        # Material-only receipt: default fulfillment_status == PROCESSING.
        self._material_receipt(self.store_a)
        # Service receipt in PROCESSING → this is the only one that should count.
        self._service_receipt(
            self.store_a, fulfillment=Receipt.FulfillmentStatus.PROCESSING
        )
        data = self._stats(self.store_a)
        self.assertEqual(data["working"], 1)
        self.assertEqual(data["ready"], 0)

    def test_ready_counts_service_receipts_in_ready(self):
        self._service_receipt(
            self.store_a, fulfillment=Receipt.FulfillmentStatus.READY
        )
        data = self._stats(self.store_a)
        self.assertEqual(data["ready"], 1)
        self.assertEqual(data["working"], 0)

    def test_material_only_receipt_not_in_working_or_ready(self):
        self._material_receipt(self.store_a)  # PROCESSING but no service line
        data = self._stats(self.store_a)
        self.assertEqual(data["working"], 0)
        self.assertEqual(data["ready"], 0)

    # ---- debt --------------------------------------------------------------

    def test_debt_sums_only_pending_outstanding(self):
        # PENDING receipt: total 3700, prepaid 1000 → owed 2700.
        self._material_receipt(self.store_a, amount_paid=Decimal("1000"))
        # PAID receipt: full settle, contributes nothing.
        self._material_receipt(self.store_a)
        data = self._stats(self.store_a)
        self.assertEqual(Decimal(str(data["debt"])), Decimal("2700"))

    def test_debt_excludes_cancelled_and_paid(self):
        # PAID receipt (no debt).
        self._material_receipt(self.store_a)
        # Cancelled/refunded receipt must not add debt even if numbers look open.
        cancelled = self._material_receipt(self.store_a, amount_paid=Decimal("0"))
        cancelled.status = Receipt.Status.CANCELLED
        cancelled.payment_status = Receipt.PaymentStatus.REFUNDED
        cancelled.save(update_fields=["status", "payment_status"])
        data = self._stats(self.store_a)
        self.assertEqual(Decimal(str(data["debt"])), Decimal("0"))

    def test_cancelled_excluded_from_active_but_counted_in_total(self):
        self._material_receipt(self.store_a)  # active PAID
        cancelled = self._material_receipt(self.store_a, amount_paid=Decimal("0"))
        cancelled.status = Receipt.Status.CANCELLED
        cancelled.payment_status = Receipt.PaymentStatus.REFUNDED
        cancelled.save(update_fields=["status", "payment_status"])
        data = self._stats(self.store_a)
        # total counts every receipt of this cashier, including the cancelled one.
        self.assertEqual(data["total"], 2)
        # but the cancelled one contributes nothing to debt/working/ready.
        self.assertEqual(Decimal(str(data["debt"])), Decimal("0"))
        self.assertEqual(data["working"], 0)
        self.assertEqual(data["ready"], 0)

    # ---- ?search honoured by stats ----------------------------------------

    def test_stats_respects_search_filter(self):
        self._material_receipt(self.store_a, client=self.client_one)
        self._material_receipt(self.store_a, client=self.client_two)
        # Search by the phone of client_one only.
        data = self._stats(self.store_a, query="search=%2B790001")
        self.assertEqual(data["total"], 1)

    # ---- fulfillment transitions ------------------------------------------

    def test_mark_ready_sets_fulfillment_ready(self):
        receipt = self._service_receipt(
            self.store_a, fulfillment=Receipt.FulfillmentStatus.PROCESSING
        )
        self.client.force_authenticate(self.store_a)
        resp = self.client.post(f"/api/sales/receipts/{receipt.id}/mark-ready/")
        self.assertEqual(resp.status_code, 200, resp.data)
        receipt.refresh_from_db()
        self.assertEqual(
            receipt.fulfillment_status, Receipt.FulfillmentStatus.READY
        )

    def test_mark_issued_sets_fulfillment_issued(self):
        receipt = self._service_receipt(
            self.store_a, fulfillment=Receipt.FulfillmentStatus.READY
        )
        self.client.force_authenticate(self.store_a)
        resp = self.client.post(f"/api/sales/receipts/{receipt.id}/mark-issued/")
        self.assertEqual(resp.status_code, 200, resp.data)
        receipt.refresh_from_db()
        self.assertEqual(
            receipt.fulfillment_status, Receipt.FulfillmentStatus.ISSUED
        )

    def test_repeated_mark_ready_is_idempotent(self):
        receipt = self._service_receipt(
            self.store_a, fulfillment=Receipt.FulfillmentStatus.PROCESSING
        )
        self.client.force_authenticate(self.store_a)
        first = self.client.post(f"/api/sales/receipts/{receipt.id}/mark-ready/")
        second = self.client.post(f"/api/sales/receipts/{receipt.id}/mark-ready/")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        receipt.refresh_from_db()
        self.assertEqual(
            receipt.fulfillment_status, Receipt.FulfillmentStatus.READY
        )

    def test_storekeeper_cannot_mark_ready_foreign_receipt(self):
        receipt = self._service_receipt(
            self.store_a, fulfillment=Receipt.FulfillmentStatus.PROCESSING
        )
        # store_b is scoped to its own receipts → foreign one is invisible (404).
        self.client.force_authenticate(self.store_b)
        resp = self.client.post(f"/api/sales/receipts/{receipt.id}/mark-ready/")
        self.assertEqual(resp.status_code, 404, getattr(resp, "data", resp))
        receipt.refresh_from_db()
        self.assertEqual(
            receipt.fulfillment_status, Receipt.FulfillmentStatus.PROCESSING
        )
