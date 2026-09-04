"""Гравировка — новая услуга по кв.м (2026-09-04, просьба владельца).

Цена за квадратный метр гравируемой площади, материал отдельной строкой не
идёт. Цену за кв.м в момент продажи правят и админ, и складовщик: у крупных
заказов она своя («5 000 за квадрат»). Услуга заводится миграцией — на проде
она нужна сразу после обновления.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt, TransactionItem
from services.models import PrintingService
from warehouse.models import InventoryLog, Material


class EngravingTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="en_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="en_store", password="x", role=User.Role.STOREKEEPER)
        self.engraving = PrintingService.objects.get(kind=PrintingService.Kind.ENGRAVING)
        self.engraving.rate_flat = Decimal("3000")
        self.engraving.save()

    def _checkout(self, items, **extra):
        return self.client.post(
            "/api/sales/receipts/checkout/",
            {"payment_method": "CASH", "pay_full": True, "items": items, **extra},
            format="json",
        )

    def test_migration_created_the_service(self):
        svc = PrintingService.objects.filter(kind=PrintingService.Kind.ENGRAVING)
        self.assertEqual(svc.count(), 1)
        self.assertEqual(svc.get().name_ru, "Гравировка")
        self.assertTrue(svc.get().uses_area)
        self.assertFalse(svc.get().uses_material)
        self.assertFalse(svc.get().uses_running_meter)

    def test_priced_by_area_at_catalogue_rate(self):
        self.client.force_authenticate(self.store)
        r = self._checkout([{
            "type": "SERVICE", "service": self.engraving.id, "width": "0.5", "length": "0.4",
            "note": "логотип на табличке",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        # 0.5 × 0.4 = 0.2 кв.м × 3000 = 600
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("600"))
        item = TransactionItem.objects.get(receipt_id=r.data["id"])
        self.assertEqual(item.quantity, Decimal("0.200"))
        self.assertEqual(item.price_per_item, Decimal("3000"))
        self.assertEqual(item.note, "логотип на табличке")
        self.assertEqual(r.data["items"][0]["unit_code"], "SQM")
        # Материала в строке нет, склад не тронут.
        self.assertIsNone(item.material_id)
        self.assertFalse(InventoryLog.objects.exists())

    def test_storekeeper_sets_price_per_sqm_for_big_order(self):
        """«Для больших заказов цена за кв.м будет 5 000» — вписывает и складовщик."""
        self.client.force_authenticate(self.store)
        r = self._checkout([{
            "type": "SERVICE", "service": self.engraving.id, "width": "2", "length": "1.5",
            "cut_rate": "5000",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("15000"))

    def test_admin_sets_price_too(self):
        self.client.force_authenticate(self.admin)
        r = self._checkout([{
            "type": "SERVICE", "service": self.engraving.id, "width": "1", "length": "1", "cut_rate": "4500",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("4500"))

    def test_no_rate_anywhere_is_rejected(self):
        self.engraving.rate_flat = Decimal("0")
        self.engraving.save()
        self.client.force_authenticate(self.store)
        r = self._checkout([{"type": "SERVICE", "service": self.engraving.id, "width": "1", "length": "1"}])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("впишите цену", str(r.data))
        self.assertFalse(Receipt.objects.exists())

    def test_needs_dimensions(self):
        self.client.force_authenticate(self.admin)
        r = self._checkout([{"type": "SERVICE", "service": self.engraving.id, "cut_rate": "5000"}])
        self.assertEqual(r.status_code, 400, r.data)

    def test_own_material_flag_is_allowed_on_engraving(self):
        self.client.force_authenticate(self.store)
        r = self._checkout([{
            "type": "SERVICE", "service": self.engraving.id, "own_material": True,
            "width": "0.3", "length": "0.3", "cut_rate": "5000", "note": "кружка клиента",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("450"))
        self.assertTrue(r.data["items"][0]["own_material"])

    def test_add_items_engraving_from_storekeeper(self):
        mat = Material.objects.create(name="Крепёж", unit=Material.Unit.PIECE, quantity=Decimal("10"),
                                      price_per_unit=Decimal("5"))
        self.client.force_authenticate(self.store)
        r = self._checkout([{"type": "MATERIAL", "material": mat.id, "quantity": 2}])
        self.assertEqual(r.status_code, 201, r.data)
        rid = r.data["id"]
        r = self.client.post(
            f"/api/sales/receipts/{rid}/add-items/",
            {"items": [{"type": "SERVICE", "service": self.engraving.id, "width": "1", "length": "1",
                        "cut_rate": "5000"}]},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Receipt.objects.get(pk=rid).total_price, Decimal("5010"))

    def test_storekeeper_cannot_gift_engraving(self):
        """Ноль за квадрат — подарок, а его оформляет только админ.

        Цену гравировки владелец попросил отдать складовщику («для больших
        заказов 5 000»), но «за ноль» — это другое решение, и оно остаётся
        админским. Кнопка в кассе при нулевой цене и так закрыта.
        """
        self.client.force_authenticate(self.store)
        r = self._checkout([{
            "type": "SERVICE", "service": self.engraving.id,
            "width": "2", "length": "2", "cut_rate": "0",
        }])
        self.assertEqual(r.status_code, 403, r.data)
        self.assertIn("подарок", str(r.data))
        self.assertFalse(Receipt.objects.exists())
        # Админ дарит по-прежнему.
        self.client.force_authenticate(self.admin)
        r = self._checkout([{
            "type": "SERVICE", "service": self.engraving.id,
            "width": "2", "length": "2", "cut_rate": "0",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(str(r.data["total_price"])), Decimal("0"))

    def test_material_price_override_still_admin_only(self):
        self.client.force_authenticate(self.store)
        r = self._checkout([{
            "type": "SERVICE", "service": self.engraving.id, "width": "1", "length": "1",
            "cut_rate": "5000", "material_price": "1",
        }])
        self.assertEqual(r.status_code, 403, r.data)

    def test_pricing_page_can_edit_rate(self):
        self.client.force_authenticate(self.admin)
        r = self.client.patch(f"/api/services/services/{self.engraving.id}/", {"rate_flat": "3500"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(str(r.data["rate_flat"])), Decimal("3500"))
        # Складовщик читает свежую ставку — по ней касса считает предпросмотр.
        self.client.force_authenticate(self.store)
        r = self.client.get(f"/api/services/services/{self.engraving.id}/")
        self.assertEqual(Decimal(str(r.data["rate_flat"])), Decimal("3500"))
        self.assertEqual(r.data["kind"], "ENGRAVING")
