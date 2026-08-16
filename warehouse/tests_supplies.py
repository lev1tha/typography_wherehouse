"""Приходная накладная: поставка одним документом.

Раньше приход вводился по одной позиции с кнопки на строке материала, и сверить
итог с бумажной накладной было нечем — общей суммы поставки система не знала.
Документ считает её сам и показывает расхождение; склад при этом поднимается
теми же примитивами, что и раньше, поэтому закуп в финотчёте и складской журнал
продолжают работать без правок — это здесь тоже проверяется.
"""
from decimal import Decimal

from django.utils.timezone import localtime
from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales import sale_service
from sales.models import Receipt
from warehouse.models import InventoryLog, Material, Roll, Supplier, Supply


class SupplyDocumentTests(APITestCase):
    URL = "/api/warehouse/supplies/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="sp_admin", password="x", role=User.Role.ADMIN
        )
        self.keeper = User.objects.create_user(
            username="sp_keeper", password="x", role=User.Role.STOREKEEPER
        )
        self.client.force_authenticate(self.admin)
        self.supplier = Supplier.objects.create(name="Глобал")
        self.sheet = Material.objects.create(
            name="Акрил 2мм", unit=Material.Unit.SQM, is_roll_material=True,
            piece_area=Decimal("2.9768"), price_per_sqm=Decimal("1470"),
        )
        self.bolts = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE,
            quantity=Decimal("500"), purchase_price=Decimal("10"),
        )

    def _payload(self, **over):
        data = {
            "number": "ТН-00123",
            "supplier": self.supplier.id,
            "received_on": "2026-08-10",
            "stated_total": "50000",
            "paid_amount": "0",
            "lines": [
                {
                    "material": self.sheet.id, "form": "SHEET",
                    "width": "1.22", "height": "2.44", "sheet_count": "10",
                    "cost": "48000", "code": "партия A",
                },
                {
                    "material": self.bolts.id, "form": "QTY",
                    "quantity": "100", "cost": "1200",
                },
            ],
        }
        data.update(over)
        return data

    def _create(self, **over):
        return self.client.post(self.URL, self._payload(**over), format="json")

    # ---- проведение ---------------------------------------------------------
    def test_document_puts_every_line_on_the_shelf(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 201, resp.data)
        self.sheet.refresh_from_db()
        self.bolts.refresh_from_db()
        # 1.22 × 2.44 × 10 = 29.768 кв.м
        self.assertEqual(self.sheet.quantity, Decimal("29.7680"))
        self.assertEqual(self.bolts.quantity, Decimal("600.0000"))

    def test_area_line_creates_a_lot_with_its_own_cost(self):
        """Партия — источник FIFO и стоимости склада; без неё приход по
        документу считался бы иначе, чем приход кнопкой."""
        self._create()
        roll = Roll.objects.get(material=self.sheet)
        self.assertEqual(roll.initial_area, Decimal("29.7680"))
        self.assertEqual(roll.purchase_cost, Decimal("48000"))
        self.assertEqual(roll.code, "партия A")

    def test_journal_entries_point_back_to_the_document(self):
        resp = self._create()
        supply = Supply.objects.get(pk=resp.data["id"])
        logs = supply.inventory_logs.all()
        self.assertEqual(logs.count(), 2)
        self.assertTrue(all(x.type == InventoryLog.Type.SUPPLY for x in logs))

    def test_document_date_lands_on_the_movements(self):
        """Поставку вносят задним числом — и в журнале, и в FIFO должна стоять
        дата накладной, а не момент ввода."""
        self._create(received_on="2026-07-05")
        log = InventoryLog.objects.filter(material=self.sheet).first()
        self.assertEqual(localtime(log.happened_at).date().isoformat(), "2026-07-05")
        self.assertEqual(
            localtime(Roll.objects.get(material=self.sheet).received_at).date().isoformat(),
            "2026-07-05",
        )

    # ---- сверка с бумагой ---------------------------------------------------
    def test_discrepancy_with_the_paper_note(self):
        """Ради этой цифры документ и заведён: 48000 + 1200 = 49200 против
        50000 в бумаге — 800 сом где-то потерялись."""
        resp = self._create()
        self.assertEqual(Decimal(str(resp.data["total_cost"])), Decimal("49200"))
        self.assertEqual(Decimal(str(resp.data["discrepancy"])), Decimal("800"))

    def test_no_stated_total_means_nothing_to_reconcile(self):
        resp = self._create(stated_total=None)
        self.assertEqual(Decimal(str(resp.data["discrepancy"])), Decimal("0"))

    def test_supplier_debt_is_what_is_left_to_pay(self):
        resp = self._create(paid_amount="20000")
        self.assertEqual(Decimal(str(resp.data["debt"])), Decimal("29200"))

    # ---- закуп в финотчёте --------------------------------------------------
    def test_purchase_reaches_the_finance_report(self):
        """Закуп считается по приходам на склад — документ не должен требовать
        отдельной проводки в финансах."""
        self._create(received_on="2026-08-10")
        rows = self.client.get(
            "/api/finance/report/", {"date_from": "2026-08-01", "date_to": "2026-08-31"}
        ).data["materials"]["rows"]
        purchase = next(r for r in rows if r["code"] == "MATERIAL_PURCHASE")
        self.assertEqual(Decimal(str(purchase["amount"])), Decimal("49200"))

    # ---- отмена -------------------------------------------------------------
    def test_untouched_document_can_be_cancelled(self):
        resp = self._create()
        before_sheet = Decimal("0")
        del_resp = self.client.delete(f"{self.URL}{resp.data['id']}/")
        self.assertEqual(del_resp.status_code, 204, getattr(del_resp, "data", None))
        self.sheet.refresh_from_db()
        self.bolts.refresh_from_db()
        self.assertEqual(self.sheet.quantity, before_sheet)
        self.assertEqual(self.bolts.quantity, Decimal("500.0000"))
        self.assertFalse(Roll.objects.filter(material=self.sheet).exists())

    def test_cannot_cancel_once_the_material_was_cut(self):
        """Из партии уже резали — откат сдвинул бы себестоимость закрытых
        заказов. Честнее отказать, чем тихо переписать прошлое."""
        resp = self._create()
        customer = Client.objects.create(full_name="Клиент", phone="+996700000123")
        sale_service.create_sale(
            client=customer, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": "MATERIAL", "material": self.sheet,
                "quantity": Decimal("2"), "mode": "SQM",
            }],
            amount_paid=Decimal("0"),
        )
        del_resp = self.client.delete(f"{self.URL}{resp.data['id']}/")
        self.assertEqual(del_resp.status_code, 400)
        self.assertIn("резали", del_resp.data["detail"])
        self.assertTrue(Supply.objects.filter(pk=resp.data["id"]).exists())

    def test_storekeeper_cannot_cancel(self):
        resp = self._create()
        self.client.force_authenticate(self.keeper)
        del_resp = self.client.delete(f"{self.URL}{resp.data['id']}/")
        self.assertEqual(del_resp.status_code, 403)

    # ---- права и проверки ---------------------------------------------------
    def test_storekeeper_can_receive_a_delivery(self):
        """Товар принимает он — документ заводит тоже он."""
        self.client.force_authenticate(self.keeper)
        self.assertEqual(self._create().status_code, 201)

    def test_accountant_cannot_receive(self):
        accountant = User.objects.create_user(
            username="sp_acc", password="x", role=User.Role.ACCOUNTANT
        )
        self.client.force_authenticate(accountant)
        self.assertEqual(self._create().status_code, 403)

    def test_empty_document_is_rejected(self):
        resp = self._create(lines=[])
        self.assertEqual(resp.status_code, 400)

    def test_line_without_dimensions_is_rejected(self):
        resp = self._create(lines=[
            {"material": self.sheet.id, "form": "SHEET", "cost": "1000"},
        ])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("количество", resp.data["detail"])
        # Ничего не оприходовалось: документ проводится целиком или никак.
        self.sheet.refresh_from_db()
        self.assertEqual(self.sheet.quantity, Decimal("0"))
        self.assertFalse(Supply.objects.exists())

    def test_lines_are_not_editable_after_posting(self):
        """Состав проведённого документа не правим: часть материала могла уже
        уйти в заказы. Бумажная часть — номер, дата, оплата — правится."""
        resp = self._create()
        supply_id = resp.data["id"]
        patch = self.client.patch(
            f"{self.URL}{supply_id}/",
            {"paid_amount": "10000", "lines": []}, format="json",
        )
        self.assertEqual(patch.status_code, 200, patch.data)
        self.assertEqual(Decimal(str(patch.data["paid_amount"])), Decimal("10000"))
        self.assertEqual(len(patch.data["lines"]), 2)


class SupplierTests(APITestCase):
    URL = "/api/warehouse/suppliers/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="su_admin", password="x", role=User.Role.ADMIN
        )
        self.keeper = User.objects.create_user(
            username="su_keeper", password="x", role=User.Role.STOREKEEPER
        )

    def test_storekeeper_can_add_a_supplier(self):
        """Новая фирма всплывает в момент приёмки — гонять складовщика к админу
        за строчкой справочника значит получить накладную без поставщика."""
        self.client.force_authenticate(self.keeper)
        resp = self.client.post(self.URL, {"name": "Новый поставщик"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_supplier_with_deliveries_is_hidden_not_deleted(self):
        self.client.force_authenticate(self.admin)
        supplier = Supplier.objects.create(name="Глобал")
        Supply.objects.create(supplier=supplier, received_on="2026-08-01")
        resp = self.client.delete(f"{self.URL}{supplier.id}/")
        self.assertEqual(resp.status_code, 200, resp.data)
        supplier.refresh_from_db()
        self.assertTrue(supplier.is_archived)

    def test_storekeeper_cannot_delete_a_supplier(self):
        self.client.force_authenticate(self.keeper)
        supplier = Supplier.objects.create(name="Глобал")
        self.assertEqual(self.client.delete(f"{self.URL}{supplier.id}/").status_code, 403)

    def test_debt_sums_up_across_the_deliveries(self):
        """«Сколько я должен Глобалу» — сумма долгов по его накладным."""
        self.client.force_authenticate(self.admin)
        supplier = Supplier.objects.create(name="Глобал")
        material = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("0")
        )
        for paid in ("0", "500"):
            self.client.post(
                "/api/warehouse/supplies/",
                {
                    "supplier": supplier.id, "received_on": "2026-08-01",
                    "paid_amount": paid,
                    "lines": [{
                        "material": material.id, "form": "QTY",
                        "quantity": "10", "cost": "1000",
                    }],
                },
                format="json",
            )
        row = next(s for s in self.client.get(self.URL).data if s["id"] == supplier.id)
        # 1000 + (1000 − 500) = 1500
        self.assertEqual(Decimal(str(row["debt"])), Decimal("1500"))
