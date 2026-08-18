"""Резка без длины реза не проходит: иначе работа уходит в чек бесплатно.

Длину кривой при фигурном резе вводит мастер руками. Пустое поле раньше молча
превращалось в нулевую работу: материал посчитан, а самая дорогая работа цеха
— бесплатно, и в чеке это никак не видно (строка «Резка 0 пог.м × 120 = 0»
выглядит как «работы не было»). Теперь пустая длина — ошибка ввода, а не ноль:
её отклоняет `SaleItemInputSerializer`, то есть обе ручки, касса и дозаказ.

Площадь вместо длины по-прежнему НЕ подставляется — так уже было, и кв.м
считались как пог.м.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt
from services.models import PrintingService
from warehouse.models import Material, MaterialType


class CutRequiresLengthTests(APITestCase):
    CHECKOUT = "/api/sales/receipts/checkout/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="cutlen_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.client_obj = Client.objects.create(full_name="Нурлан", phone="+996555000111")
        self.acryl = Material.objects.create(
            name="Акрил 3мм", type=MaterialType.objects.get(code="ACRYL"), unit="SQM",
            is_roll_material=True, quantity=Decimal("14.884"),
            purchase_price=Decimal("806.23"), price_per_sqm=Decimal("1500"),
            cut_rate_per_pm=Decimal("120"), piece_area=Decimal("2.9768"),
            piece_price=Decimal("3700"),
        )
        self.cutting = PrintingService.objects.create(
            name="Резка", kind=PrintingService.Kind.CUTTING,
            machine=PrintingService.Machine.CNC,
        )
        self.install = PrintingService.objects.create(
            name="Внутренний монтаж", kind=PrintingService.Kind.INSTALL_INTERIOR,
            rate_flat=Decimal("200"),
        )

    def _checkout(self, items, **extra):
        payload = {
            "payment_method": "CASH",
            "client_id": self.client_obj.id,
            "items": items,
            **extra,
        }
        return self.client.post(self.CHECKOUT, payload, format="json")

    def _cut_item(self, **fields):
        item = {
            "type": "SERVICE", "service": self.cutting.id, "material": self.acryl.id,
            "width": "0.5", "length": "1.2",
        }
        item.update(fields)
        return item

    # --- Отклоняем ---------------------------------------------------------

    def test_figured_cut_without_running_meters_is_rejected(self):
        """Поле пустое (не прислано) — 400, а не чек с работой за ноль."""
        res = self._checkout([self._cut_item()])
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("длину реза", str(res.data))
        self.assertEqual(Receipt.objects.count(), 0)
        # Склад не тронут: заказ не состоялся целиком, а не «материал списан,
        # работа потерялась».
        self.acryl.refresh_from_db()
        self.assertEqual(self.acryl.quantity, Decimal("14.884"))

    def test_zero_running_meters_is_rejected_too(self):
        """Ноль — та же пустота, только записанная числом."""
        for zero in ("0", "0.00", None, ""):
            res = self._checkout([self._cut_item(running_meters=zero)])
            self.assertEqual(res.status_code, 400, (zero, res.data))
        self.assertEqual(Receipt.objects.count(), 0)

    def test_whole_sheet_cut_work_line_needs_length_as_well(self):
        """Работа по целому листу (без размеров куска) — тоже резка, и без
        длины её не бывает."""
        res = self._checkout([
            {"type": "MATERIAL", "material": self.acryl.id, "quantity": 1, "mode": "PIECE"},
            {"type": "SERVICE", "service": self.cutting.id, "material": self.acryl.id},
        ])
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(Receipt.objects.count(), 0)

    def test_add_items_endpoint_applies_the_same_rule(self):
        """Дозаказ идёт через тот же сериализатор — дыры сбоку нет."""
        res = self._checkout([self._cut_item(running_meters="1.2")], pay_full=True)
        self.assertEqual(res.status_code, 201, res.data)
        receipt_id = res.data["id"]
        res = self.client.post(
            f"/api/sales/receipts/{receipt_id}/add-items/",
            {"items": [self._cut_item()]},
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("длину реза", str(res.data))
        # Состав чека не изменился.
        self.assertEqual(Receipt.objects.get(pk=receipt_id).items.count(), 2)

    # --- Пропускаем --------------------------------------------------------

    def test_cut_with_length_is_priced_by_length(self):
        """С длиной — работа считается: 120 × 3.4 = 408, материал 0.6 × 1500 = 900."""
        res = self._checkout([self._cut_item(running_meters="3.4")], pay_full=True)
        self.assertEqual(res.status_code, 201, res.data)
        receipt = Receipt.objects.get(pk=res.data["id"])
        work = receipt.items.get(type="SERVICE")
        material = receipt.items.get(type="MATERIAL")
        self.assertEqual(work.quantity, Decimal("3.400"))
        self.assertEqual(work.line_total, Decimal("408"))
        self.assertEqual(material.line_total, Decimal("900"))
        self.assertEqual(receipt.total_price, Decimal("1308"))

    def test_zero_rate_is_still_a_valid_gift(self):
        """Бесплатная работа — это нулевая СТАВКА, заданная админом явно, а не
        забытая длина. Такой заказ проходит."""
        res = self._checkout(
            [self._cut_item(running_meters="1.2", cut_rate="0")], pay_full=True
        )
        self.assertEqual(res.status_code, 201, res.data)
        receipt = Receipt.objects.get(pk=res.data["id"])
        work = receipt.items.get(type="SERVICE")
        self.assertEqual(work.quantity, Decimal("1.200"))
        self.assertEqual(work.line_total, Decimal("0"))

    def test_interior_install_does_not_need_running_meters(self):
        """Монтаж считается площадью — погонных метров у него нет, и правило
        резки его не касается."""
        res = self._checkout(
            [{
                "type": "SERVICE", "service": self.install.id, "material": self.acryl.id,
                "width": "1", "length": "2",
            }],
            pay_full=True,
        )
        self.assertEqual(res.status_code, 201, res.data)
        receipt = Receipt.objects.get(pk=res.data["id"])
        work = receipt.items.get(type="SERVICE")
        self.assertEqual(work.quantity, Decimal("2.000"))
        self.assertEqual(work.line_total, Decimal("400"))
