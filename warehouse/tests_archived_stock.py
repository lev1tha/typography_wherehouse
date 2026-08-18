"""«Удалённый» материал: возврат поднимает его из архива, а его остаток всё
это время остаётся в стоимости склада.

Материал с продажами не удаляется, а прячется. Пока его нет в наличии — это
верно. Но остаток при этом никуда не девался: нажатие «Удалить» на позиции с
товаром мгновенно убирало её стоимость из активов, а возврат возвращал материал
в позицию, которой не видно ни в каталоге, ни в кассе, — со стороны владельца
«сделал возврат, а на склад ничего не вернулось».
"""
from decimal import Decimal

from django.db.models import Sum
from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from clients.models import Client
from warehouse.models import Material, Roll

SHEET = Decimal("1.22") * Decimal("2.44")


class ArchivedMaterialStockTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="am_admin", password="x", role=User.Role.ADMIN
        )
        self.customer = Client.objects.create(full_name="Тахир", phone="+996555000222")
        self.client.force_authenticate(self.admin)
        self.mat = Material.objects.create(
            name="Акрил 3мм", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
            price_per_sqm=Decimal("1500"),
        )
        r = self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": self.mat.id, "form": "SHEET", "width": "1.22",
            "height": "2.44", "sheet_count": "5", "purchase_cost": "12000",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    # --- helpers ---
    def _sell(self, sheets=2):
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True, "client_id": self.customer.id,
            "items": [{"type": "MATERIAL", "material": self.mat.id, "mode": "SQM",
                       "quantity": str((SHEET * sheets).quantize(Decimal("0.001")))}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data["id"]

    def _archive(self):
        r = self.client.delete(f"/api/warehouse/materials/{self.mat.id}/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data.get("archived"), r.data)

    def _stock_value_in_dashboard(self):
        return Decimal(str(self.client.get("/api/audit/dashboard/").data["unrealised_asset"]))

    def _lots(self):
        return Roll.objects.filter(material=self.mat).aggregate(v=Sum("remaining_area"))["v"]

    # --- стоимость склада ---
    def test_hiding_a_material_does_not_wipe_its_value(self):
        self._sell()
        before = self._stock_value_in_dashboard()
        self.assertGreater(before, 0)

        self._archive()
        after = self._stock_value_in_dashboard()
        self.assertEqual(after, before, "стоимость склада упала от одного «Удалить»")

    def test_hidden_material_without_stock_adds_nothing(self):
        """Исходная жалоба «удалил, а он в отчётах» — пустой скрытый даёт ноль."""
        self._sell(sheets=5)          # продали всё
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("0.0000"))
        self._archive()
        self.assertEqual(self._stock_value_in_dashboard(), Decimal("0"))

    # --- возврат ---
    def test_refund_brings_the_material_back_to_the_catalogue(self):
        rid = self._sell()
        self._archive()
        hidden = self.client.get("/api/warehouse/materials/?page_size=200").data["results"]
        self.assertNotIn(self.mat.id, [m["id"] for m in hidden])

        qty_before, lots_before = self.mat.quantity, self._lots()
        r = self.client.post(f"/api/sales/receipts/{rid}/refund/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)

        self.mat.refresh_from_db()
        self.assertFalse(self.mat.is_archived, "материал остался скрытым после возврата")
        visible = self.client.get("/api/warehouse/materials/?page_size=200").data["results"]
        self.assertIn(self.mat.id, [m["id"] for m in visible])
        # И сам товар действительно вернулся — числом и партиями.
        self.assertGreater(self.mat.quantity, qty_before)
        self.assertGreater(self._lots(), lots_before)

    def test_the_return_is_explained_in_the_action_log(self):
        rid = self._sell()
        self._archive()
        self.client.post(f"/api/sales/receipts/{rid}/refund/", {}, format="json")
        self.assertTrue(
            AuditLog.objects.filter(action__icontains="возвращён в каталог").exists(),
            "в журнале действий не видно, почему материал снова появился",
        )

    def test_a_visible_material_is_not_touched(self):
        rid = self._sell()
        self.client.post(f"/api/sales/receipts/{rid}/refund/", {}, format="json")
        self.mat.refresh_from_db()
        self.assertFalse(self.mat.is_archived)
        self.assertFalse(AuditLog.objects.filter(action__icontains="возвращён в каталог").exists())

    def test_deleting_the_receipt_also_brings_it_back(self):
        """Удаление ошибочного чека возвращает товар так же, как возврат."""
        rid = self._sell()
        self._archive()
        r = self.client.delete(f"/api/sales/receipts/{rid}/")
        self.assertEqual(r.status_code, 204)
        self.mat.refresh_from_db()
        self.assertFalse(self.mat.is_archived)
