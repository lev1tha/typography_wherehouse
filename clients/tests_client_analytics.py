"""Поиск клиента по цифрам телефона, CSV списка, «Покупки по клиентам» (CLI-10)."""
import csv
import io
from clients.models import Client
from clients.testkit import D, ShopCase


class SearchTests(ShopCase):
    def setUp(self):
        super().setUp()
        self.other = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Бакыт Осмонов", phone="+996700123456",
        )

    def found(self, q):
        r = self.client.get("/api/clients/clients/", {"search": q})
        self.assertEqual(r.status_code, 200, r.data)
        return {x["display_name"] for x in r.data["results"]}

    def test_phone_in_any_spelling(self):
        for q in ("+996555112233", "555112233", "0555112233", "0555 11 22 33",
                  "+996 555 11 22 33", "(555) 11-22-33", "996555112233"):
            self.assertEqual(self.found(q), {"ОсОО «Ак Жол»"}, q)

    def test_part_of_the_number(self):
        self.assertEqual(self.found("555 11 22"), {"ОсОО «Ак Жол»"})
        self.assertEqual(self.found("0700 123"), {"Бакыт Осмонов"})

    def test_name_with_punctuation(self):
        for q in ("Ак-Жол", "ак жол", "АК  ЖОЛ", "«Ак Жол»", "осоо ак-жол"):
            self.assertEqual(self.found(q), {"ОсОО «Ак Жол»"}, q)
        self.assertEqual(self.found("бакыт"), {"Бакыт Осмонов"})

    def test_nothing_found(self):
        self.assertEqual(self.found("0999 88 77 66"), set())
        self.assertEqual(self.found("Нет-такого"), set())

    def test_order_number_still_works(self):
        self.sale(100, paid=100)
        self.assertEqual(self.found("№1"), {"ОсОО «Ак Жол»"})


class CsvExportTests(ShopCase):
    URL = "/api/clients/clients/export/"

    def setUp(self):
        super().setUp()
        self.sale(1500, paid=500, days_ago=40)                       # долг 1 000, маржа 900
        self.bakyt = Client.objects.create(full_name="Бакыт", phone="+996700123456")
        self.sale(200, client=self.bakyt, paid=200, days_ago=3)

    def rows(self, **params):
        r = self.client.get(self.URL, params)
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        self.assertIn("attachment", r["Content-Disposition"])
        text = r.content.decode("utf-8")
        self.assertTrue(text.startswith("﻿"), "нужен BOM, иначе русский Excel покажет кракозябры")
        return list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))

    def test_header_and_decimal_comma(self):
        rows = self.rows()
        header = rows[0]
        self.assertIn("Клиент", header)
        self.assertIn("Долг, сом", header)
        by_name = {r[0]: dict(zip(header, r)) for r in rows[1:]}
        ak = by_name["ОсОО «Ак Жол»"]
        self.assertEqual(ak["Долг, сом"], "1000,00")
        self.assertEqual(ak["Дней долга"], "40")
        self.assertEqual(ak["Маржа, сом"], "900,00")
        self.assertEqual(by_name["Бакыт"]["Долг, сом"], "0,00")

    def test_filters_apply_to_the_file(self):
        names = [r[0] for r in self.rows(has_debt=1)[1:]]
        self.assertEqual(names, ["ОсОО «Ак Жол»"])
        names = [r[0] for r in self.rows(overdue_days=30)[1:]]
        self.assertEqual(names, ["ОсОО «Ак Жол»"])

    def test_not_paginated(self):
        for i in range(30):
            Client.objects.create(full_name=f"Клиент {i}", phone=f"+9967001000{i:02d}")
        self.assertEqual(len(self.rows()[1:]), 32)

    def test_storekeeper_has_no_margin_column(self):
        self.client.force_authenticate(self.store)
        header = self.rows()[0]
        self.assertNotIn("Маржа, сом", header)
        self.client.force_authenticate(self.acc)
        self.assertIn("Маржа, сом", self.rows()[0])

    def test_formula_injection_is_defused(self):
        Client.objects.create(full_name="=HYPERLINK(\"http://x\")", phone="+996700555000")
        names = [r[0] for r in self.rows()[1:]]
        self.assertIn("'=HYPERLINK(\"http://x\")", names)

    def test_anonymous_is_refused(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.URL).status_code, 401)


class PurchasesByUnitTests(ShopCase):
    """«Покупки по клиентам» не складывает листы с квадратными метрами."""

    URL = "/api/audit/client-purchases/"

    def test_units_are_reported_separately(self):
        from sales import sale_service
        from warehouse.models import Material, Roll
        from warehouse.rolls import receive_lot

        sheet = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=D("1.22"), sheet_height=D("2.44"), price_per_sqm=D("1000"), piece_price=D("3000"),
        )
        receive_lot(sheet, form=Roll.Form.SHEET, area=D("2.9768") * 30, purchase_cost=D("1000") * 30, user=self.admin)
        for items in (
            [{"type": "MATERIAL", "material": sheet, "quantity": D("20"), "mode": "PIECE"}],
            [{"type": "MATERIAL", "material": sheet, "quantity": D("0.96"), "mode": "SQM"}],
        ):
            sale_service.create_sale(
                client=self.agency, cashier=self.admin, payment_method="CASH",
                items_data=items, amount_paid=D("0"),
            )
        r = self.client.get(self.URL)
        self.assertEqual(r.status_code, 200, r.data)
        row = next(x for x in r.data if x["client_id"] == self.agency.id)
        by_unit = {u["unit"]: D(str(u["qty"])) for u in row["qty_by_unit"]}
        self.assertEqual(by_unit, {"PIECE": D("20"), "SQM": D("0.96")})
        # одно число «20,96» больше не отдаём как будто это единицы одного рода
        self.assertEqual(row["material_qty_label"], "20 шт + 0.96 кв.м")
