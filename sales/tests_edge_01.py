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
            name="Акрил 3мм", unit="SQM",
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

    # ---- Целый лист + резка (пог.м) ---------------------------------------

    def test_whole_sheet_plus_cutting_bills_sheet_and_work_only(self):
        # «Весь лист» + резка: материал по цене ЛИСТА (PIECE) + работа реза
        # (пог.м × ставка). Отдельной MATERIAL-линии по площади быть НЕ должно.
        r = self._checkout([
            {"type": "MATERIAL", "material": self.acrylic.id, "quantity": 1, "mode": "PIECE"},
            {"type": "SERVICE", "service": self.cutting.id, "material": self.acrylic.id, "running_meters": "5"},
        ])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        items = self._items(receipt)
        self.assertEqual(len(items), 2)  # лист + работа, без площади-материала
        mat = receipt.items.get(type=TransactionItem.Type.MATERIAL)
        work = receipt.items.get(type=TransactionItem.Type.SERVICE)
        self.assertEqual(mat.sale_mode, TransactionItem.SaleMode.PIECE)
        self.assertEqual(mat.quantity, Decimal("1"))
        self.assertEqual(mat.price_per_item, Decimal("3700"))
        self.assertEqual(work.quantity, Decimal("5"))
        self.assertEqual(work.price_per_item, Decimal("20"))  # ставка реза материала
        self.assertEqual(receipt.total_price, Decimal("3800.00"))
        # Списание: только площадь листа (2.98), работа склад не трогает.
        self.acrylic.refresh_from_db()
        self.assertEqual(self.acrylic.quantity, Decimal("97.02"))  # 100 − 2.98

    def test_whole_sheet_plus_cutting_admin_rate_override(self):
        self.client.force_authenticate(self.admin)
        r = self._checkout([
            {"type": "MATERIAL", "material": self.acrylic.id, "quantity": 2, "mode": "PIECE"},
            {"type": "SERVICE", "service": self.cutting.id, "material": self.acrylic.id,
             "running_meters": "10", "cut_rate": "35"},
        ])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        work = receipt.items.get(type=TransactionItem.Type.SERVICE)
        self.assertEqual(work.quantity, Decimal("10"))
        self.assertEqual(work.price_per_item, Decimal("35"))  # override
        # 2 листа × 3700 + 10 пог.м × 35 = 7400 + 350 = 7750
        self.assertEqual(receipt.total_price, Decimal("7750.00"))

    # ---- Округление цены строки вверх до целого сома ----------------------

    def test_line_totals_round_up_to_whole_som(self):
        # Площадь 0.33×0.33 = 0.109; материал 0.109×1400 = 152.6 → 153;
        # работа (длина реза 0.11) × 20 = 2.2 → 3; итог 156. Округляем ВВЕРХ.
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "material": self.acrylic.id, "width": "0.33", "length": "0.33",
            "running_meters": "0.11",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        work = receipt.items.get(type=TransactionItem.Type.SERVICE)
        mat = receipt.items.get(type=TransactionItem.Type.MATERIAL)
        self.assertEqual(work.line_total, Decimal("3"))
        self.assertEqual(mat.line_total, Decimal("153"))
        self.assertEqual(receipt.total_price, Decimal("156.00"))

    # ---- Резка без выбранного материала ----------------------------------

    def test_cutting_without_material_needs_a_rate(self):
        """Нет материала (ставку взять неоткуда) и нет override → отказ, а не
        работа за ноль. Раньше строка уходила в чек с ценой 0 (аудит
        2026-08-18, п. 8: неявный ноль — ошибка каталога, явный — подарок)."""
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "width": "0.5", "length": "0.5", "running_meters": "0.25",
        }])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("Ставка резки не задана", str(r.data))
        self.assertEqual(Receipt.objects.count(), 0)
        # Со ставкой станка работа без материала оформляется — одной строкой.
        self.cutting.rate_per_pm = Decimal("100")
        self.cutting.save(update_fields=["rate_per_pm"])
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "width": "0.5", "length": "0.5", "running_meters": "0.25",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        items = self._items(receipt)
        self.assertEqual(len(items), 1)   # нет материала → нет MATERIAL-линии
        work = items[0]
        self.assertEqual(work.type, TransactionItem.Type.SERVICE)
        self.assertEqual(work.quantity, Decimal("0.250"))   # длина реза
        self.assertEqual(work.price_per_item, Decimal("100"))
        self.assertEqual(receipt.total_price, Decimal("25.00"))

    def test_cutting_without_material_uses_cut_rate_override(self):
        # Ручные цены — право админа (аудит п. 14).
        self.client.force_authenticate(self.admin)
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "width": "1", "length": "1", "cut_rate": "35", "running_meters": "1",
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

    def test_running_meters_empty_is_refused_not_charged_as_zero(self):
        """Пустая длина реза → 400, а не работа за ноль и не «площадь как пог.м».

        Раньше сюда подставлялась площадь и умножалась на ставку за погонный
        метр — кв.м считались как пог.м. Потом пустота стала нулём — и фигурный
        рез (длину кривой вводит мастер) молча уезжал в чек бесплатно. Теперь
        пустая длина у резки — ошибка ввода (см. sales/tests_cut_requires_length)."""
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "material": self.acrylic.id, "width": "0.5", "length": "0.5",
        }])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("длину реза", str(r.data))
        self.assertFalse(Receipt.objects.exists())
        # Материал не списан: заказ не состоялся целиком.
        self.acrylic.refresh_from_db()
        self.assertEqual(self.acrylic.quantity, Decimal("100"))

    def test_zero_dimensions_are_refused_instead_of_an_empty_receipt(self):
        """Ни размеров, ни количества — это промах по кнопке, а не заказ.

        Раньше такой запрос создавал чек «на 0 сом»: пустышка оседала в списке
        чеков и в статистике. Проверка теперь на входе — важно, что ответ
        внятный 400, а не 500 и не молчаливый пустой чек. У резки первой
        срабатывает проверка длины реза (она строже), у прочих услуг —
        общая «нет ни одной позиции с количеством или размером».
        """
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "material": self.acrylic.id,
        }])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("длину реза", str(r.data))
        self.assertFalse(Receipt.objects.exists())

        interior = PrintingService.objects.create(
            name="Монтаж", kind=PrintingService.Kind.INSTALL_INTERIOR, rate_flat=Decimal("100"),
        )
        r = self._checkout([{
            "type": "SERVICE", "service": interior.id, "material": self.acrylic.id,
        }])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("размер", str(r.data))
        self.assertFalse(Receipt.objects.exists())

    def test_explicit_zero_width_treated_as_blank(self):
        # width='0' falsy → 'width and length' ложно → площади нет. Куска без
        # ширины не бывает, поэтому заказ отклоняется на входе, а не заводит
        # чек на ноль. Длина реза при этом не указана — работы тоже нет.
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "material": self.acrylic.id, "width": "0", "length": "1",
        }])
        self.assertEqual(r.status_code, 400, r.data)

    # ---- Точность площади (3 знака) --------------------------------------

    def test_area_quantized_to_three_decimals(self):
        # 0.33 × 0.33 = 0.1089 → поле quantity (decimal_places=3) → 0.109.
        # Длина реза приходит отдельным полем (в нём 2 знака после запятой).
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "material": self.acrylic.id, "width": "0.33", "length": "0.33",
            "running_meters": "0.11",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        receipt.refresh_from_db()
        work = receipt.items.get(type=TransactionItem.Type.SERVICE)
        mat = receipt.items.get(type=TransactionItem.Type.MATERIAL)
        work.refresh_from_db()
        mat.refresh_from_db()
        self.assertEqual(work.quantity, Decimal("0.110"))
        # Площадь материала — именно она квантуется из 0.1089.
        self.assertEqual(mat.quantity, Decimal("0.109"))

    # ---- Админ-override, равный нулю (подозрение на баг) ------------------

    def test_zero_cut_rate_override_is_respected(self):
        # Ручные цены — право админа (аудит п. 14).
        self.client.force_authenticate(self.admin)
        # Админ явно делает резку бесплатной: cut_rate=0. Ожидаем ставку 0,
        # а не подмену каталожной 20. Падение теста вскрывает falsy-баг
        # (_override('cut_rate') or material.cut_rate_per_pm).
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "material": self.acrylic.id, "width": "1", "length": "1",
            "running_meters": "1", "cut_rate": "0",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        work = receipt.items.get(type=TransactionItem.Type.SERVICE)
        self.assertEqual(work.price_per_item, Decimal("0"))

    def test_zero_material_price_override_is_respected(self):
        # Ручные цены — право админа (аудит п. 14).
        self.client.force_authenticate(self.admin)
        # Админ явно делает материал бесплатным: material_price=0. Ожидаем 0,
        # а не каталожные 1400. Падение вскрывает тот же falsy-баг.
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "material": self.acrylic.id, "width": "1", "length": "1",
            "running_meters": "1", "material_price": "0",
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


class NoPrepaymentMeansDebtTests(APITestCase):
    """Пустая сумма оплаты = ничего не приняли, весь заказ в долг.

    Раньше пустое поле молча означало «оплачено полностью», и продажа в долг
    выглядела закрытой.
    """

    def setUp(self):
        self.store = User.objects.create_user(
            username="store_nopay", password="x", role=User.Role.STOREKEEPER
        )
        self.client.force_authenticate(self.store)
        self.material = Material.objects.create(
            name="Лист", unit="SQM",
            quantity=Decimal("100"), price_per_unit=Decimal("1000"),
        )

    def _checkout(self, **extra):
        payload = {
            "payment_method": "CASH",
            "items": [{"type": "MATERIAL", "material": self.material.id,
                       "quantity": 2, "mode": "SQM"}],
            **extra,
        }
        return self.client.post("/api/sales/receipts/checkout/", payload, format="json")

    def test_no_amount_means_full_debt(self):
        r = self._checkout()
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PENDING)
        self.assertEqual(receipt.amount_paid, Decimal("0"))
        self.assertEqual(receipt.debt, Decimal("2000"))
        # Товар всё равно отгружён — склад списан.
        self.material.refresh_from_db()
        self.assertEqual(self.material.quantity, Decimal("98.00"))

    def test_partial_amount_leaves_the_rest_as_debt(self):
        receipt = Receipt.objects.get(pk=self._checkout(amount_paid=500).data["id"])
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PENDING)
        self.assertEqual(receipt.debt, Decimal("1500"))

    def test_full_amount_closes_the_order(self):
        receipt = Receipt.objects.get(pk=self._checkout(amount_paid=2000).data["id"])
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PAID)
        self.assertEqual(receipt.debt, Decimal("0"))

    def test_overpayment_is_capped_no_negative_debt(self):
        # Дали больше — лишнее это сдача, в долг минусом не уходит.
        receipt = Receipt.objects.get(pk=self._checkout(amount_paid=5000).data["id"])
        self.assertEqual(receipt.payment_status, Receipt.PaymentStatus.PAID)
        self.assertEqual(receipt.amount_paid, Decimal("2000"))
        self.assertEqual(receipt.debt, Decimal("0"))
