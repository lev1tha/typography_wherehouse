"""Кнопка «Удалить» на материале: что она удаляет и что остаётся.

Заказчик сообщил (2026-08-14): «нажимаешь удалить — из вида пропадает, а в
django-админке и в расчётах финансов остаётся». Так и было: материал прятался
(``is_archived``) уже из-за одного прихода, а ни стоимость склада, ни закуп, ни
отчёт по материалам скрытые материалы не фильтровали.

Граница теперь проходит по ПРОДАЖАМ, а не по любой истории:
продаж не было — удаляем насовсем вместе с приходами; были — прячем, потому что
строки старых чеков ссылаются на материал.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales import sale_service
from sales.models import Receipt
from warehouse.models import InventoryLog, Material, Roll
from warehouse.rolls import receive_lot


class MaterialDeleteTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="del_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.material = Material.objects.create(
            name="Акрил на удаление", unit=Material.Unit.SQM, is_roll_material=True,
            piece_area=Decimal("2"), price_per_sqm=Decimal("100"),
        )

    def _lot(self, area="10", cost="5000"):
        return receive_lot(
            self.material, form=Roll.Form.SHEET, area=Decimal(area),
            purchase_cost=Decimal(cost), user=self.admin,
        )

    def _sell(self, qty="2"):
        customer = Client.objects.create(full_name="Клиент", phone="+996700000009")
        return sale_service.create_sale(
            client=customer, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": "MATERIAL", "material": self.material,
                "quantity": Decimal(qty), "mode": "SQM",
            }],
            amount_paid=Decimal("200"),
        )

    def _delete(self):
        return self.client.delete(f"/api/warehouse/materials/{self.material.id}/")

    # ---- продаж не было: удаляем насовсем -----------------------------------
    def test_material_without_history_is_deleted(self):
        resp = self._delete()
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data["deleted"])
        self.assertFalse(Material.objects.filter(pk=self.material.pk).exists())

    def test_intake_only_material_is_deleted_with_its_lots(self):
        """Самый частый случай: завели материал, приняли поставку, увидели дубль.

        Раньше он всего лишь прятался, а его приход продолжал сидеть в закупе.
        """
        self._lot()
        resp = self._delete()
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data["deleted"])
        self.assertFalse(Material.objects.filter(pk=self.material.pk).exists())
        self.assertFalse(Roll.objects.filter(material_id=self.material.id).exists())
        self.assertFalse(InventoryLog.objects.filter(material_id=self.material.id).exists())

    def test_purchase_drops_out_of_the_finance_report(self):
        """Закуп считается по приходам — вместе с материалом уходит и он."""
        self._lot(cost="5000")
        before = self._purchase_row()
        self.assertEqual(before, Decimal("5000"))
        self._delete()
        self.assertEqual(self._purchase_row(), Decimal("0"))

    def test_stock_value_drops_out_of_the_dashboard(self):
        self._lot()
        self.assertEqual(self._stock_value(), Decimal("5000"))
        self._delete()
        self.assertEqual(self._stock_value(), Decimal("0"))

    # ---- продажи были: прячем, но из расчётов убираем ------------------------
    def test_sold_material_is_hidden_not_deleted(self):
        self._lot()
        self._sell()
        resp = self._delete()
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data["archived"])
        self.material.refresh_from_db()
        self.assertTrue(self.material.is_archived)

    def test_hidden_material_leaves_the_catalogue_and_the_till(self):
        self._lot()
        self._sell()
        self._delete()
        listing = self.client.get("/api/warehouse/materials/?page_size=200").data
        self.assertNotIn(self.material.id, [m["id"] for m in listing["results"]])

    def test_hidden_material_stops_counting_in_stock_value(self):
        """Остаток скрытого материала в «Стоимости склада» больше не участвует:
        админ нажал «Удалить», и товара для него больше нет."""
        self._lot()
        self._sell()
        self.assertGreater(self._stock_value(), Decimal("0"))
        self._delete()
        self.assertEqual(self._stock_value(), Decimal("0"))

    def test_hidden_material_keeps_its_sales_in_the_period_report(self):
        """Строку с ПРОДАЖАМИ периода не прячем: это настоящие деньги месяца,
        без неё выручка в отчёте не сойдётся."""
        self._lot()
        self._sell()
        self._delete()
        rows = self.client.get("/api/finance/material-report/").data["rows"]
        row = next((r for r in rows if r["id"] == self.material.id), None)
        self.assertIsNotNone(row, "продажи месяца пропали из отчёта")
        self.assertGreater(Decimal(str(row["material_revenue"])), Decimal("0"))

    def test_hidden_material_without_movement_leaves_the_report(self):
        """А вот в периоде, где по нему ничего не было, пустая строка не нужна."""
        self._lot()
        self._sell()
        self._delete()
        rows = self.client.get(
            "/api/finance/material-report/", {"date_from": "2020-01-01", "date_to": "2020-01-31"}
        ).data["rows"]
        self.assertNotIn(self.material.id, [r["id"] for r in rows])

    def test_hidden_material_is_not_offered_as_low_stock(self):
        """Докупать то, что удалили из каталога, не нужно."""
        self.material.critical_balance = Decimal("100")
        self.material.save(update_fields=["critical_balance"])
        self._lot()
        self._sell()
        data = self.client.get("/api/audit/dashboard/").data
        self.assertIn(self.material.id, [m["id"] for m in data["low_stock_items"]])
        self._delete()
        data = self.client.get("/api/audit/dashboard/").data
        self.assertNotIn(self.material.id, [m["id"] for m in data["low_stock_items"]])

    def test_storekeeper_cannot_delete(self):
        keeper = User.objects.create_user(
            username="del_keeper", password="x", role=User.Role.STOREKEEPER
        )
        self.client.force_authenticate(keeper)
        self.assertEqual(self._delete().status_code, 403)
        self.assertTrue(Material.objects.filter(pk=self.material.pk).exists())

    # ---- helpers ------------------------------------------------------------
    def _stock_value(self):
        return Decimal(str(self.client.get("/api/audit/dashboard/").data["unrealised_asset"]))

    def _purchase_row(self):
        rows = self.client.get("/api/finance/report/").data["materials"]["rows"]
        return Decimal(str(next(r["amount"] for r in rows if r["code"] == "MATERIAL_PURCHASE")))
