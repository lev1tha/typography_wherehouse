"""Волна 2, п. 16 (PNL-08): пересчёт себестоимости по FIFO после прихода задним числом."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import PeriodLock
from sales import sale_service
from sales.models import TransactionItem
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot

SHEET = Decimal("2.9768")


class FifoRecalcTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="ff_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        now = timezone.now()
        self.m = Material.objects.create(name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
                                         sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
                                         piece_price=Decimal("5000"))
        self.a = receive_lot(self.m, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                             sheet_count=Decimal("10"), purchase_cost=Decimal("40000"),
                             received_at=now - timedelta(days=5))
        self.receipt = sale_service.create_sale(
            client=None, cashier=self.admin, payment_method="CASH", pay_full=True,
            items_data=[{"type": "MATERIAL", "material": self.m, "quantity": Decimal("2"), "mode": "PIECE"}],
            created_at=now - timedelta(days=3),
        )
        self.item = TransactionItem.objects.get(receipt=self.receipt)
        self.assertEqual(self.item.cost_total, Decimal("8000.00"))
        # Поставку от «−8 дней» внесли только сейчас — по FIFO продажа должна была взять её.
        self.b = receive_lot(self.m, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                             sheet_count=Decimal("10"), purchase_cost=Decimal("30000"),
                             received_at=now - timedelta(days=8))
        self.since = (now - timedelta(days=10)).date().isoformat()

    def post(self, mode, **kw):
        return self.client.post(f"/api/warehouse/fifo-recalc/{mode}/",
                                {"material": self.m.id, "since": self.since, **kw}, format="json")

    def test_preview_then_apply(self):
        r = self.post("preview")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["cogs_delta"], Decimal("-2000.00"))
        self.assertEqual(r.data["items"][0]["cost_after"], Decimal("6000.00"))
        self.item.refresh_from_db()
        self.assertEqual(self.item.cost_total, Decimal("8000.00"))       # предпросмотр не пишет
        r = self.post("apply")
        self.assertEqual(r.status_code, 200, r.data)
        self.item.refresh_from_db()
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual(self.item.cost_total, Decimal("6000.00"))
        self.assertEqual(self.item.roll_id, self.b.id)
        self.assertEqual(self.a.remaining_area, SHEET * 10)
        self.assertEqual(self.b.remaining_area, SHEET * 8)
        self.assertEqual([(u.roll_id, u.area) for u in self.item.lot_uses.all()], [(self.b.id, SHEET * 2)])
        # Возврат кладёт в ту партию, откуда теперь «взято».
        sale_service.refund_receipt(self.receipt, user=self.admin)
        self.b.refresh_from_db()
        self.assertEqual(self.b.remaining_area, SHEET * 10)
        # Повторный пересчёт — нечего.
        self.assertEqual(self.post("apply").status_code, 400)

    def test_closed_month_is_400(self):
        lock = PeriodLock.load()
        lock.closed_through = timezone.localdate()
        lock.save()
        r = self.post("apply")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertTrue(r.data["closed_months"])

    def test_roll_by_metre_is_refused(self):
        film = Material.objects.create(name="Плёнка", unit=Material.Unit.SQM, is_roll_material=True,
                                       intake_form=Material.IntakeForm.ROLL, roll_width=Decimal("1"),
                                       price_per_pm=Decimal("100"))
        r = self.client.post("/api/warehouse/fifo-recalc/preview/", {"material": film.id}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
