"""RS-N3 (перепроверка 10.10, S3): выданных авансов поставщикам не было в «Сводке».

Аванс поставщику без накладных уменьшает кассу, а в «Сводке» его не видно:
«Долг поставщикам» показывает только то, что должны мы. Теперь рядом —
«Авансы поставщикам»: сальдо поставщика в нашу пользу на сегодня.
"""
from datetime import date
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from warehouse.models import Material, Supplier
from warehouse.supplier_ledger import record_payment

REPORT = "/api/finance/report/"
D = Decimal


class SupplierAdvancesTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="sa_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.sheet = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True, price_per_sqm=D("1470"),
        )
        self.globus = Supplier.objects.create(name="Глобус")
        self.akrilik = Supplier.objects.create(name="Акрилик")

    def report(self):
        r = self.client.get(REPORT, {"date_from": "2026-10-01", "date_to": "2026-10-31"})
        self.assertEqual(r.status_code, 200)
        return r.data

    def test_advance_without_invoices_is_shown(self):
        record_payment(supplier=self.globus, amount="15000", account="CASH",
                       paid_on=date(2026, 10, 2), user=self.admin)
        data = self.report()
        adv = data["supplier_advances"]
        self.assertEqual(D(str(adv["total"])), D("15000"))
        self.assertEqual([(r["supplier"], D(str(r["amount"])), D(str(r["advances"]))) for r in adv["rows"]],
                         [("Глобус", D("15000"), D("15000"))])
        self.assertEqual(D(str(data["suppliers"]["total"])), D("0"))

    def test_advance_that_covers_another_invoice_is_not_counted_twice(self):
        r = self.client.post("/api/warehouse/supplies/", {
            "number": "НК-7", "received_on": "2026-10-03", "supplier": self.akrilik.id,
            "lines": [{"material": self.sheet.id, "form": "SHEET",
                       "width": "1.2", "height": "2.4", "sheet_count": "2", "cost": "10000"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        record_payment(supplier=self.akrilik, amount="3000", account="CASH",
                       paid_on=date(2026, 10, 4), user=self.admin)
        data = self.report()
        self.assertEqual(D(str(data["suppliers"]["total"])), D("7000"))         # 10 000 − аванс 3 000
        self.assertEqual(D(str(data["supplier_advances"]["total"])), D("0"))
        self.assertEqual(data["supplier_advances"]["rows"], [])

    def test_nothing_paid_nothing_shown(self):
        self.assertEqual(self.report()["supplier_advances"], {"total": D("0"), "rows": []})
