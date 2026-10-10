"""Волна 2, п. 12 (STK-05/G4-N4): площадка хранения у партии и «Перемещение».

10 листов приняли в Бишкек, 6 перевезли в Глобал: остаток материала тот же,
в ОПиУ ни потерь, ни закупа; по площадкам — 4 и 6 листов по цене партии;
фильтр партий по площадке работает.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from finance.reports.pnl import pnl
from warehouse.models import InventoryLog, Material, ProductionSite, Roll
from warehouse.rolls import receive_lot
from warehouse.sites import placements

SHEET = Decimal("2.9768")


class TransferTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="tr_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="tr_store", password="x", role=User.Role.STOREKEEPER)
        self.acct = User.objects.create_user(username="tr_acct", password="x", role=User.Role.ACCOUNTANT)
        self.bish = ProductionSite.objects.create(code="bish-t", name="Бишкек-т")
        self.glob = ProductionSite.objects.create(code="glob-t", name="Глобал-т")
        self.m = Material.objects.create(
            name="акрил 6 мм", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"), price_per_sqm=Decimal("1650"),
            piece_price=Decimal("4900"),
        )
        self.lot = receive_lot(self.m, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                               sheet_count=Decimal("10"), purchase_cost=Decimal("46140"), site=self.bish,
                               received_at=timezone.now() - timedelta(days=2))
        self.client.force_authenticate(self.store)

    def move(self, **kw):
        return self.client.post("/api/warehouse/transfers/", {"material": self.m.id, **kw}, format="json")

    def test_move_six_sheets(self):
        today = timezone.localdate()
        before = pnl(today.replace(day=1), today)
        r = self.move(from_site=self.bish.id, to_site=self.glob.id, sheets="6", note="в цех")
        self.assertEqual(r.status_code, 201, r.data)
        self.m.refresh_from_db()
        self.assertEqual(self.m.quantity, SHEET * 10)
        self.lot.refresh_from_db()
        self.assertEqual(placements(self.lot), {self.bish.id: SHEET * 4, self.glob.id: SHEET * 6})
        after = pnl(today.replace(day=1), today)
        self.assertEqual(after["losses"], before["losses"])
        self.assertEqual(after["net_profit"], before["net_profit"])
        log = InventoryLog.objects.get(type=InventoryLog.Type.TRANSFER)
        self.assertEqual(log.quantity_changed, Decimal("0"))
        self.client.force_authenticate(self.admin)
        card = self.client.get(f"/api/warehouse/materials/{self.m.id}/").data
        by = {row["name"]: row for row in card["by_site"]}
        self.assertEqual(by["Бишкек-т"]["area"], SHEET * 4)
        self.assertEqual(by["Глобал-т"]["value"], Decimal("27684.00"))
        lots = self.client.get("/api/warehouse/rolls/", {"site": self.glob.id}).data
        lots = lots.get("results", lots)
        self.assertEqual([x["id"] for x in lots], [self.lot.id])
        none = self.client.get("/api/warehouse/rolls/", {"site": ProductionSite.objects.create(code="x-t", name="X").id}).data
        self.assertEqual(len(none.get("results", none)), 0)

    def test_sale_takes_home_site_first_then_moved(self):
        self.move(from_site=self.bish.id, to_site=self.glob.id, sheets="6")
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": self.m.id, "mode": "PIECE", "quantity": "5"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.lot.refresh_from_db()
        self.assertEqual(placements(self.lot), {self.glob.id: SHEET * 5})

    def test_cannot_move_more_than_lies_there(self):
        r = self.move(from_site=self.glob.id, to_site=self.bish.id, sheets="1")
        self.assertEqual(r.status_code, 400, r.data)
        r = self.move(from_site=self.bish.id, to_site=self.bish.id, sheets="1")
        self.assertEqual(r.status_code, 400, r.data)
        self.client.force_authenticate(self.acct)
        self.assertEqual(self.move(from_site=self.bish.id, to_site=self.glob.id, sheets="1").status_code, 403)

    def test_move_back_and_from_unknown_site(self):
        self.move(from_site=self.bish.id, to_site=self.glob.id, sheets="6")
        r = self.move(from_site=self.glob.id, to_site=self.bish.id, sheets="6")
        self.assertEqual(r.status_code, 201, r.data)
        self.lot.refresh_from_db()
        self.assertEqual(placements(self.lot), {self.bish.id: SHEET * 10})
        self.assertFalse(self.lot.placements.exists())
        old = receive_lot(self.m, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                          sheet_count=Decimal("2"), purchase_cost=Decimal("9228"))
        r = self.move(to_site=self.glob.id, sheets="2")          # из «без площадки»
        self.assertEqual(r.status_code, 201, r.data)
        old.refresh_from_db()
        self.assertEqual(placements(old), {self.glob.id: SHEET * 2})
