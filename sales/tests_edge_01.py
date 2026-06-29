from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt, TransactionItem
from services.models import PrintingService
from warehouse.models import Material


class EdgeCuttingTests(APITestCase):
    """Пограничные случаи резки (работа+материал, пог.м, переопределения).

    Резка (PrintingService.Kind.CUTTING) расщепляется на SERVICE-линию (работа
    мастера по длине реза/площади) и опциональную MATERIAL-линию (по площади).
    Проверяем нулевые размеры, отсутствие материала, running_meters, точность
    площади, нулевые override и отсутствие услуги в каталоге.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="store_edge", password="x", role=User.Role.STOREKEEPER
        )
        self.admin = User.objects.create_user(
            username="admin_edge", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.user)
        self.acrylic = Material.objects.create(
            name="Акрил 3мм", category="Акрил", unit="SQM",
            quantity=Decimal("100"), price_per_unit=Decimal("0"),
            price_per_sqm=Decimal("1400"), piece_price=Decimal("3700"),
            piece_area=Decimal("2.98"), cut_rate_per_pm=Decimal("20"),
        )
        self.cutting = PrintingService.objects.create(
            name="Резка букв", kind=PrintingService.Kind.CUTTING,
            rate_flat=Decimal("200"),
        )

    def _checkout(self, items):
        return self.client.post(
            "/api/sales/receipts/checkout/",
            {"payment_method": "CASH", "items": items},
            format="json",
        )

    def _items(self, receipt):
        return list(receipt.items.all())

    # ---- Резка без выбранного материала ----------------------------------

    def test_cutting_without_material_makes_only_work_line(self):
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "width": "0.5", "length": "0.5",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        items = self._items(receipt)
        # Нет материала → нет отдельной MATERIAL-линии.
        self.assertEqual(len(items), 1)
        work = items[0]
        self.assertEqual(work.type, TransactionItem.Type.SERVICE)
        self.assertEqual(work.quantity, Decimal("0.250"))   # площадь
        # Нет материала и нет override → ставка резки 0.
        self.assertEqual(work.price_per_item, Decimal("0"))
        self.assertEqual(receipt.total_price, Decimal("0.00"))

    def test_cutting_without_material_uses_cut_rate_override(self):
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "width": "1", "length": "1", "cut_rate": "35",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        items = self._items(receipt)
        self.assertEqual(len(items), 1)
        work = items[0]
        self.assertEqual(work.price_per_item, Decimal("35"))
        self.assertEqual(work.quantity, Decimal("1.000"))
        self.assertEqual(receipt.total_price, Decimal("35.00"))

    # ---- running_meters: задан vs пусто ----------------------------------

    def test_running_meters_drives_work_not_material(self):
        # Площадь = 1×1 = 1; режем 4 пог.м.
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "material": self.acrylic.id, "width": "1", "length": "1",
            "running_meters": "4",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        work = receipt.items.get(type=TransactionItem.Type.SERVICE)
        mat = receipt.items.get(type=TransactionItem.Type.MATERIAL)
        # Работа считается по длине реза (running_meters).
        self.assertEqual(work.quantity, Decimal("4.000"))
        self.assertEqual(work.price_per_item, Decimal("20"))   # cut_rate_per_pm
        # Материал всегда по площади, НЕ по running_meters.
        self.assertEqual(mat.quantity, Decimal("1.000"))
        self.assertEqual(mat.price_per_item, Decimal("1400"))
        # Итог = 4×20 + 1×1400 = 80 + 1400 = 1480
        self.assertEqual(receipt.total_price, Decimal("1480.00"))

    def test_running_meters_empty_falls_back_to_area(self):
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "material": self.acrylic.id, "width": "0.5", "length": "0.5",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        work = receipt.items.get(type=TransactionItem.Type.SERVICE)
        mat = receipt.items.get(type=TransactionItem.Type.MATERIAL)
        self.assertEqual(work.quantity, Decimal("0.250"))   # площадь
        self.assertEqual(mat.quantity, Decimal("0.250"))

    # ---- Нулевые/пустые width-height -------------------------------------

    def test_zero_dimensions_no_quantity_yields_zero_lines(self):
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "material": self.acrylic.id,
        }])
        # Должно безопасно создаться (без 500), площадь 0.
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        work = receipt.items.get(type=TransactionItem.Type.SERVICE)
        mat = receipt.items.get(type=TransactionItem.Type.MATERIAL)
        self.assertEqual(work.quantity, Decimal("0.000"))
        self.assertEqual(mat.quantity, Decimal("0.000"))
        self.assertEqual(receipt.total_price, Decimal("0.00"))

    def test_explicit_zero_width_treated_as_blank(self):
        # width='0' falsy → 'width and length' ложно → area=quantity(0), а не 0*1.
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "material": self.acrylic.id, "width": "0", "length": "1",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        work = receipt.items.get(type=TransactionItem.Type.SERVICE)
        self.assertEqual(work.quantity, Decimal("0.000"))

    # ---- Точность площади (3 знака) --------------------------------------

    def test_area_quantized_to_three_decimals(self):
        # 0.33 × 0.33 = 0.1089 → поле quantity (decimal_places=3) → 0.109.
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "material": self.acrylic.id, "width": "0.33", "length": "0.33",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        receipt.refresh_from_db()
        work = receipt.items.get(type=TransactionItem.Type.SERVICE)
        mat = receipt.items.get(type=TransactionItem.Type.MATERIAL)
        work.refresh_from_db()
        mat.refresh_from_db()
        self.assertEqual(work.quantity, Decimal("0.109"))
        self.assertEqual(mat.quantity, Decimal("0.109"))

    # ---- Админ-override, равный нулю (подозрение на баг) ------------------

    def test_zero_cut_rate_override_is_respected(self):
        # Админ явно делает резку бесплатной: cut_rate=0. Ожидаем ставку 0,
        # а не подмену каталожной 20. Падение теста вскрывает falsy-баг
        # (_override('cut_rate') or material.cut_rate_per_pm).
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "material": self.acrylic.id, "width": "1", "length": "1",
            "cut_rate": "0",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        work = receipt.items.get(type=TransactionItem.Type.SERVICE)
        self.assertEqual(work.price_per_item, Decimal("0"))

    def test_zero_material_price_override_is_respected(self):
        # Админ явно делает материал бесплатным: material_price=0. Ожидаем 0,
        # а не каталожные 1400. Падение вскрывает тот же falsy-баг.
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "material": self.acrylic.id, "width": "1", "length": "1",
            "material_price": "0",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        mat = receipt.items.get(type=TransactionItem.Type.MATERIAL)
        self.assertEqual(mat.price_per_item, Decimal("0"))

    # ---- Отсутствует услуга резки в каталоге ------------------------------

    def test_missing_cutting_service_rejected(self):
        bad_id = self.cutting.id + 99999
        r = self._checkout([{
            "type": "SERVICE", "service": bad_id,
            "material": self.acrylic.id, "width": "1", "length": "1",
        }])
        # PrimaryKeyRelatedField не находит услугу → 400, чек не создаётся.
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(Receipt.objects.count(), 0)
