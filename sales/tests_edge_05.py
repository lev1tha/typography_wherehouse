from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from warehouse.models import InventoryLog, Material, Roll
from warehouse.rolls import InsufficientStock, consume_area, receive_lot


class EdgeStockRollTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="edge_admin", password="x", role=User.Role.ADMIN
        )
        self.store = User.objects.create_user(
            username="edge_store", password="x", role=User.Role.STOREKEEPER
        )
        self.client.force_authenticate(self.store)
        self.customer = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Buyer", phone="+790001"
        )
        self.mat = Material.objects.create(
            name="Banner", category="Rolls", unit=Material.Unit.SQM,
            is_roll_material=True, quantity=Decimal("0"),
            price_per_sqm=Decimal("300"), piece_price=Decimal("1000"),
            piece_area=Decimal("2.00"),
        )
        self.plain = Material.objects.create(
            name="Tape", category="Misc", unit=Material.Unit.PIECE,
            quantity=Decimal("5"), price_per_unit=Decimal("50"),
        )

    def _make_two_rolls(self):
        old = receive_lot(
            self.mat, form=Roll.Form.ROLL, width=Decimal("1"), length=Decimal("10"),
            purchase_cost=Decimal("40"), markup_percent=Decimal("0"),
            code="OLD", user=self.admin,
        )
        new = receive_lot(
            self.mat, form=Roll.Form.ROLL, width=Decimal("1"), length=Decimal("10"),
            purchase_cost=Decimal("120"), markup_percent=Decimal("0"),
            code="NEW", user=self.admin,
        )
        self.mat.refresh_from_db()
        return old, new

    def _checkout(self, items):
        return self.client.post(
            "/api/sales/receipts/checkout/",
            {"payment_method": "CASH", "client_id": self.customer.id, "items": items},
            format="json",
        )

    def test_receive_roll_area(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post(
            "/api/warehouse/materials/receive-roll/",
            {"material": self.mat.id, "form": "ROLL", "width": "1.5",
             "length": "10", "purchase_cost": "100", "markup_percent": "0"},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        roll = Roll.objects.get(material=self.mat, form=Roll.Form.ROLL)
        self.assertEqual(roll.initial_area, Decimal("15.00"))
        self.assertEqual(roll.remaining_area, Decimal("15.00"))
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("15.00"))
        self.assertTrue(self.mat.is_roll_material)
        self.assertEqual(self.mat.unit, Material.Unit.SQM)

    def test_receive_sheet_area(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post(
            "/api/warehouse/materials/receive-roll/",
            {"material": self.mat.id, "form": "SHEET", "width": "1", "height": "2",
             "sheet_count": "10", "purchase_cost": "100", "markup_percent": "0"},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        roll = Roll.objects.get(material=self.mat, form=Roll.Form.SHEET)
        self.assertEqual(roll.initial_area, Decimal("20.00"))
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("20.00"))

    def test_fifo_consumes_oldest_first_with_cost(self):
        old, new = self._make_two_rolls()
        cogs = consume_area(self.mat, Decimal("12"), user=self.admin, reason="sale")
        old.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual(old.remaining_area, Decimal("0.00"))
        self.assertEqual(new.remaining_area, Decimal("8.00"))
        self.assertEqual(cogs, Decimal("64.00"))
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("8.00"))

    def test_consume_exactly_remaining(self):
        self._make_two_rolls()
        cogs = consume_area(self.mat, Decimal("20"), user=self.admin)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("0.00"))
        self.assertEqual(cogs, Decimal("160.00"))

    def test_consume_over_stock_raises(self):
        self._make_two_rolls()
        with self.assertRaises(InsufficientStock):
            consume_area(self.mat, Decimal("21"), user=self.admin)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("20.00"))

    def test_checkout_over_stock_returns_400(self):
        receive_lot(
            self.mat, form=Roll.Form.ROLL, width=Decimal("1"), length=Decimal("5"),
            purchase_cost=Decimal("50"), markup_percent=Decimal("0"), user=self.admin,
        )
        self.mat.refresh_from_db()
        r = self._checkout([
            {"type": "MATERIAL", "material": self.mat.id, "quantity": "10", "mode": "SQM"}
        ])
        self.assertEqual(r.status_code, 400, r.data)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("5.00"))
        roll = Roll.objects.get(material=self.mat)
        self.assertEqual(roll.remaining_area, Decimal("5.00"))

    def test_whole_piece_deducts_piece_area_times_qty(self):
        receive_lot(
            self.mat, form=Roll.Form.ROLL, width=Decimal("1"), length=Decimal("10"),
            purchase_cost=Decimal("100"), markup_percent=Decimal("0"), user=self.admin,
        )
        self.mat.refresh_from_db()
        r = self._checkout([
            {"type": "MATERIAL", "material": self.mat.id, "quantity": "2", "mode": "PIECE"}
        ])
        self.assertEqual(r.status_code, 201, r.data)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("6.00"))

    def test_whole_piece_zero_piece_area_deducts_raw_qty(self):
        self.mat.piece_area = Decimal("0")
        self.mat.save(update_fields=["piece_area"])
        receive_lot(
            self.mat, form=Roll.Form.ROLL, width=Decimal("1"), length=Decimal("10"),
            purchase_cost=Decimal("100"), markup_percent=Decimal("0"), user=self.admin,
        )
        self.mat.refresh_from_db()
        r = self._checkout([
            {"type": "MATERIAL", "material": self.mat.id, "quantity": "2", "mode": "PIECE"}
        ])
        self.assertEqual(r.status_code, 201, r.data)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("8.00"))

    def test_refund_restores_total_quantity(self):
        receive_lot(
            self.mat, form=Roll.Form.ROLL, width=Decimal("1"), length=Decimal("10"),
            purchase_cost=Decimal("100"), markup_percent=Decimal("0"), user=self.admin,
        )
        self.mat.refresh_from_db()
        r = self._checkout([
            {"type": "MATERIAL", "material": self.mat.id, "quantity": "3", "mode": "SQM"}
        ])
        self.assertEqual(r.status_code, 201, r.data)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("7.00"))
        rid = r.data["id"]
        ref = self.client.post(f"/api/sales/receipts/{rid}/refund/", {}, format="json")
        self.assertEqual(ref.status_code, 200, ref.data)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("10.00"))
        total = sum(
            (rr.remaining_area for rr in Roll.objects.filter(material=self.mat)),
            Decimal("0"),
        )
        self.assertEqual(total, Decimal("10.00"))

    def test_refund_restores_into_fifo_roll(self):
        old, new = self._make_two_rolls()
        r = self._checkout([
            {"type": "MATERIAL", "material": self.mat.id, "quantity": "12", "mode": "SQM"}
        ])
        self.assertEqual(r.status_code, 201, r.data)
        old.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual(old.remaining_area, Decimal("0.00"))
        self.assertEqual(new.remaining_area, Decimal("8.00"))
        rid = r.data["id"]
        ref = self.client.post(f"/api/sales/receipts/{rid}/refund/", {}, format="json")
        self.assertEqual(ref.status_code, 200, ref.data)
        old.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual(old.remaining_area, Decimal("10.00"))
        self.assertEqual(new.remaining_area, Decimal("10.00"))
        self.assertLessEqual(new.remaining_area, new.initial_area)

    def test_writeoff_roll_within_stock(self):
        self.client.force_authenticate(self.admin)
        receive_lot(
            self.mat, form=Roll.Form.ROLL, width=Decimal("1"), length=Decimal("10"),
            purchase_cost=Decimal("100"), markup_percent=Decimal("0"), user=self.admin,
        )
        self.mat.refresh_from_db()
        r = self.client.post(
            "/api/warehouse/materials/write-off/",
            {"material": self.mat.id, "quantity": "3", "reason_code": "DAMAGE"},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("7.00"))
        self.assertTrue(
            InventoryLog.objects.filter(
                material=self.mat, type=InventoryLog.Type.WRITE_OFF
            ).exists()
        )

    def test_writeoff_roll_over_stock_400(self):
        self.client.force_authenticate(self.admin)
        receive_lot(
            self.mat, form=Roll.Form.ROLL, width=Decimal("1"), length=Decimal("5"),
            purchase_cost=Decimal("50"), markup_percent=Decimal("0"), user=self.admin,
        )
        self.mat.refresh_from_db()
        r = self.client.post(
            "/api/warehouse/materials/write-off/",
            {"material": self.mat.id, "quantity": "10", "reason_code": "LOSS"},
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.data)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("5.00"))

    def test_writeoff_plain_over_stock_400(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post(
            "/api/warehouse/materials/write-off/",
            {"material": self.plain.id, "quantity": "99", "reason_code": "OTHER"},
            format="json",
        )
        self.assertEqual(r.status_code, 400, r.data)
        self.plain.refresh_from_db()
        self.assertEqual(self.plain.quantity, Decimal("5"))
