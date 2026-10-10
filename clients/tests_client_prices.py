"""Договорные цены клиента (CLI-02, часть; волна 2).

Цена клиента на материал или работу — вместо каталога; скидка клиента к такой
строке не применяется; вписанная руками цена сильнее договорной.
"""
from decimal import Decimal

from audit.models import AuditLog
from clients.models import Client, ClientPrice
from clients.testkit import D, ShopCase
from sales.models import TransactionItem
from sales.sale_service import create_sale, reprice_receipt
from services.models import PrintingService


class ClientPriceCase(ShopCase):
    def setUp(self):
        super().setUp()
        self.install = PrintingService.objects.create(
            name="Монтаж", kind=PrintingService.Kind.INSTALLATION, base_price=D("2500"),
        )

    def add_price(self, **body):
        r = self.client.post("/api/clients/client-prices/", {"client": self.agency.id, **body}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def order(self, items, client=None, **kw):
        return create_sale(client=client or self.agency, cashier=self.admin, payment_method="CASH",
                           items_data=items, **kw)


class PricingTests(ClientPriceCase):
    def test_material_and_work_take_the_contract_price(self):
        self.add_price(material=self.coin.id, sale_mode="PIECE", price="0.80")
        self.add_price(service=self.install.id, price="2000")
        r = self.order([
            {"type": "MATERIAL", "material": self.coin, "quantity": D("1000"), "mode": "PIECE"},
            {"type": "SERVICE", "service": self.install, "quantity": 1},
        ])
        mat, work = r.items.order_by("id")
        self.assertEqual((mat.sold_total, mat.client_price), (D("800"), True))
        self.assertEqual((work.sold_total, work.client_price), (D("2000"), True))
        self.assertEqual(r.total_price, D("2800"))

    def test_discount_does_not_apply_to_contract_lines(self):
        self.agency.discount_percent = D("10")
        self.agency.save(update_fields=["discount_percent"])
        self.add_price(service=self.install.id, price="2000")
        r = self.order(
            [{"type": "SERVICE", "service": self.install, "quantity": 1},
             {"type": "MATERIAL", "material": self.coin, "quantity": D("1000"), "mode": "PIECE"}],
            discount_percent=D("10"),
        )
        work, mat = r.items.order_by("id")
        self.assertEqual(work.sold_total, D("2000"))                 # без скидки
        self.assertEqual(mat.sold_total, D("900"))                   # каталог − 10 %

    def test_manual_price_beats_contract_and_other_clients_pay_catalog(self):
        self.add_price(service=self.install.id, price="2000")
        r = self.order([{"type": "SERVICE", "service": self.install, "quantity": 1, "cut_rate": D("1500")}])
        self.assertEqual(r.items.get().sold_total, D("1500"))
        other = Client.objects.create(full_name="Другой", phone="+996700999888")
        r = self.order([{"type": "SERVICE", "service": self.install, "quantity": 1}], client=other)
        line = r.items.get()
        self.assertEqual((line.sold_total, line.client_price), (D("2500"), False))

    def test_reprice_keeps_contract_lines(self):
        self.add_price(service=self.install.id, price="2000")
        r = self.order([{"type": "SERVICE", "service": self.install, "quantity": 1}])
        self.install.base_price = D("3000")
        self.install.save(update_fields=["base_price"])
        out = reprice_receipt(r, user=self.admin)
        self.assertEqual(out["after"], D("2000"))
        self.assertEqual(len(out["skipped"]), 1)

    def test_checkout_api_and_preview_use_it(self):
        self.add_price(service=self.install.id, price="2000")
        body = {"client_id": self.agency.id, "payment_method": "CASH",
                "items": [{"type": "SERVICE", "service": self.install.id, "quantity": 1}]}
        r = self.client.post("/api/sales/receipts/preview/", body, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), D("2000"))
        self.assertTrue(r.data["items"][0]["client_price"])


class ApiTests(ClientPriceCase):
    def test_validation_and_roles(self):
        bad = [
            {},                                                        # ни услуги, ни материала
            {"material": self.coin.id, "price": "1"},                  # материал без единицы
            {"service": self.install.id, "sale_mode": "SQM", "price": "1"},
            {"service": self.install.id, "price": "-1"},
        ]
        for body in bad:
            r = self.client.post("/api/clients/client-prices/", {"client": self.agency.id, **body}, format="json")
            self.assertEqual(r.status_code, 400, (body, r.data))
        self.add_price(service=self.install.id, price="2000")
        dup = self.client.post("/api/clients/client-prices/",
                               {"client": self.agency.id, "service": self.install.id, "price": "1900"},
                               format="json")
        self.assertEqual(dup.status_code, 400)
        cp = ClientPrice.objects.get()
        r = self.client.patch(f"/api/clients/client-prices/{cp.id}/", {"price": "1800"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(AuditLog.objects.filter(action__contains="2000.00 → 1800").exists())
        self.client.force_authenticate(self.store)
        self.assertEqual(self.client.get("/api/clients/client-prices/", {"client": self.agency.id}).status_code, 200)
        r = self.client.post("/api/clients/client-prices/",
                             {"client": self.agency.id, "material": self.coin.id, "sale_mode": "PIECE",
                              "price": "1"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(TransactionItem.objects.count(), 0)
