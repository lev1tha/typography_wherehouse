"""S3 перепроверки владельца 10.10 — ввод: сумма строки накладной как в Excel
(«6 500,00») и «Ввести пачкой» с неизвестным производством (RU-N11)."""
from decimal import Decimal

from audit.models import AuditLog
from warehouse.models import Material, ProductionSite, Supply
from warehouse.tests_recheck_stock import SUPPLIES, Base

D = Decimal
BULK = "/api/warehouse/materials/bulk/"


class SupplyLineNumbersTests(Base):
    """Сумма строки «6 500,00», «6 500», «6500,5» — как во вставке из Excel (было 400)."""

    def setUp(self):
        super().setUp()
        self.bp = Material.objects.create(name="БП 12В 100Вт", unit=Material.Unit.PIECE,
                                          price_per_unit=D("900"), purchase_price=D("400"))

    def post(self, number, cost, **extra):
        return self.client.post(SUPPLIES, {
            "number": number, "supplier": self.sup.id, "received_on": "2026-10-04",
            "lines": [{"material": self.bp.id, "form": "QTY", "quantity": "10", "cost": cost}], **extra,
        }, format="json")

    def test_excel_style_line_sums(self):
        for number, text, value in (("Э-102", "6 500,00", "6500.00"), ("Э-103", "6 500", "6500.00"),
                                    ("Э-104", "6500,5", "6500.50"), ("Э-105", "6 500,00 сом", "6500.00")):
            r = self.post(number, text, force=True)
            self.assertEqual(r.status_code, 201, (text, r.data))
            self.assertEqual(Supply.objects.get(pk=r.data["id"]).lines.get().cost, D(value), text)

    def test_paid_amount_and_sheet_count_with_comma(self):
        r = self.post("Э-110", "6 500,00", paid_amount="6 500", paid_account="CASH", stated_total="6 500,00")
        self.assertEqual(r.status_code, 201, r.data)
        supply = Supply.objects.get(pk=r.data["id"])
        self.assertEqual((supply.paid_amount, supply.stated_total), (D("6500"), D("6500")))
        r = self.client.post(SUPPLIES, {
            "number": "X-1", "supplier": self.sup.id, "received_on": "2026-10-06",
            "lines": [{"material": self.m.id, "form": "SHEET", "width": "1,22", "height": "2,44",
                       "sheet_count": "2,0", "cost": "7 000,50 сом"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Supply.objects.get(pk=r.data["id"]).lines.get().cost, D("7000.50"))

    def test_garbage_is_still_an_error(self):
        r = self.post("Э-120", "1,2,3")
        self.assertEqual(r.status_code, 400)
        self.assertIn("cost", r.data["lines"][0])


class BulkUnknownProductionTests(Base):
    """RU-N11: неизвестное производство не отклоняет пачку молча — сервер
    называет, чего нет, и по подтверждению заводит справочник."""

    ROW = {"type": "Акрил", "thickness_mm": "3", "sheet_width": "1.22", "sheet_height": "2.44",
           "price_per_sqm": "1550", "cut_rate_per_pm": "65", "piece_price": "4614"}

    def rows(self):
        return [dict(self.ROW, name="К1", production="Лазер"), dict(self.ROW, name="К2", production="лазер "),
                dict(self.ROW, name="К3", production="")]

    def test_refused_with_the_names_to_create(self):
        r = self.client.post(BULK, {"rows": self.rows()}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["missing_sites"], ["Лазер"])
        self.assertEqual([e["row"] for e in r.data["errors"]], [0, 1])
        self.assertFalse(Material.objects.filter(name__in=["К1", "К2", "К3"]).exists())
        self.assertFalse(ProductionSite.objects.filter(name="Лазер").exists())

    def test_create_on_confirmation_for_all_rows(self):
        r = self.client.post(BULK, {"rows": self.rows(), "create_sites": ["Лазер"]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        site = ProductionSite.objects.get(name="Лазер")
        self.assertEqual(set(Material.objects.filter(production=site).values_list("name", flat=True)), {"К1", "К2"})
        self.assertEqual(r.data["created_sites"], ["Лазер"])
        self.assertTrue(AuditLog.objects.filter(action__contains="«Лазер»").exists())

    def test_preview_does_not_create_the_site(self):
        r = self.client.post(BULK, {"rows": self.rows(), "create_sites": ["Лазер"], "preview": True,
                                    "mode": "upsert"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["create_sites"], ["Лазер"])
        self.assertFalse(ProductionSite.objects.filter(name="Лазер").exists())

    def test_other_errors_roll_the_new_site_back(self):
        rows = self.rows() + [dict(self.ROW, name="К4", price_per_sqm="abc")]
        r = self.client.post(BULK, {"rows": rows, "create_sites": ["Лазер"]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("missing_sites", r.data)
        self.assertFalse(ProductionSite.objects.filter(name="Лазер").exists())
        self.assertFalse(Material.objects.filter(name="К1").exists())

    def test_storekeeper_cannot(self):
        self.client.force_authenticate(self.keeper)
        r = self.client.post(BULK, {"rows": self.rows(), "create_sites": ["Лазер"]}, format="json")
        self.assertEqual(r.status_code, 403)
