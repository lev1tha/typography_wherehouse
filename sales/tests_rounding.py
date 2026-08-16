"""Одна площадь — одно число, у кассы и у сервера.

Кусок 0.45 × 1.23 м: точная площадь 0.5535 кв.м. Касса на экране резала её до
0.553 (`toFixed(3)` в double), сервер считал по точной (830.25 → 831), а на
PostgreSQL колонка хранила бы 0.554. Три числа для одного куска — и «Вся сумма»,
посчитанная кассой (978), оставляла полностью оплаченный заказ с долгом в 1 сом.

Теперь правило одно: площадь квантуется до 0.001 «половиной вверх» ДО расчёта
цены и списания (как хранит колонка `quantity`), а «Вся сумма» едет флагом
`pay_full` — сумму чека знает только сервер.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt
from services.models import PrintingService
from warehouse.models import Material, MaterialType


class AreaRoundingTests(APITestCase):
    CHECKOUT = "/api/sales/receipts/checkout/"

    def setUp(self):
        self.admin = User.objects.create_user(username="rnd_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.client_obj = Client.objects.create(full_name="Айбек", phone="+996555111222")
        self.acryl = Material.objects.create(
            name="Акрил 3мм", type=MaterialType.objects.get(code="ACRYL"), unit="SQM",
            is_roll_material=True, quantity=Decimal("14.884"),
            purchase_price=Decimal("806.23"), price_per_sqm=Decimal("1500"),
            cut_rate_per_pm=Decimal("120"), piece_area=Decimal("2.9768"),
        )
        self.cutting = PrintingService.objects.create(
            name="Резка", kind=PrintingService.Kind.CUTTING, machine=PrintingService.Machine.CNC,
        )

    def _cut(self, **extra):
        payload = {
            "payment_method": "CASH",
            "client_id": self.client_obj.id,
            "items": [{
                "type": "SERVICE", "service": self.cutting.id, "material": self.acryl.id,
                "width": "0.45", "length": "1.23", "running_meters": "1.23",
            }],
            **extra,
        }
        res = self.client.post(self.CHECKOUT, payload, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return Receipt.objects.get(pk=res.data["id"])

    def test_area_is_quantized_half_up_before_pricing(self):
        """0.45 × 1.23 = 0.5535 → 0.554 кв.м; материал 0.554 × 1500 = 831, работа 148."""
        r = self._cut(amount_paid="979")
        mat = r.items.get(type="MATERIAL")
        work = r.items.get(type="SERVICE")
        self.assertEqual(mat.quantity, Decimal("0.554"))
        self.assertEqual(mat.line_total, Decimal("831"))
        self.assertEqual(work.quantity, Decimal("1.230"))
        self.assertEqual(work.line_total, Decimal("148"))
        self.assertEqual(r.total_price, Decimal("979"))
        # Со склада ушло ровно то же число, что стоит в строке чека.
        self.acryl.refresh_from_db()
        self.assertEqual(self.acryl.quantity, Decimal("14.330"))

    def test_pay_full_flag_pays_exactly_the_server_total(self):
        """«Вся сумма» — флаг: ни долга, ни сдачи, что бы ни насчитала касса."""
        r = self._cut(pay_full=True, amount_paid="978")
        self.assertEqual(r.total_price, Decimal("979"))
        self.assertEqual(r.amount_paid, Decimal("979"))
        self.assertEqual(r.change_due, Decimal("0"))
        self.assertEqual(r.debt, Decimal("0"))
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.PAID)

    def test_amount_below_total_without_flag_is_still_a_debt(self):
        """Число без флага — как раньше: недоплата остаётся долгом."""
        r = self._cut(amount_paid="978")
        self.assertEqual(r.debt, Decimal("1"))
        self.assertEqual(r.payment_status, Receipt.PaymentStatus.PENDING)

    def test_area_sale_accepts_three_decimals(self):
        """Продажа по площади без реза шлёт площадь куска (0.554 кв.м) — три
        знака, как у колонки. Раньше поле принимало два, и такой заказ падал с
        «не более 2 цифры после запятой»."""
        res = self.client.post(self.CHECKOUT, {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": self.acryl.id, "quantity": "0.554", "mode": "SQM"}],
        }, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        r = Receipt.objects.get(pk=res.data["id"])
        self.assertEqual(r.items.get().quantity, Decimal("0.554"))
        self.assertEqual(r.total_price, Decimal("831"))
        self.assertEqual(r.amount_paid, Decimal("831"))
        # Четвёртый знак — уже не точность колонки: отклоняем явно, а не режем молча.
        res = self.client.post(self.CHECKOUT, {
            "payment_method": "CASH",
            "items": [{"type": "MATERIAL", "material": self.acryl.id, "quantity": "0.5535", "mode": "SQM"}],
        }, format="json")
        self.assertEqual(res.status_code, 400)
