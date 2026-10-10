"""«Резка, всего» в кв.м не складывает штуки.

Строка штучного материала (саморезы, 100 шт), проданная без режима, получала
режим «по площади» — и её количество уходило в кв.м: «Резка, всего: 100,55 кв.м»
вместо 0,55. Площадь берём только у материалов, которые измеряются в кв.м
(листовые и рулонные), а не у штучных.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from finance.reports.summary import finance_summary
from sales.models import Receipt, TransactionItem
from sales.sale_service import create_sale
from services.models import PrintingService
from warehouse.models import Material


class CuttingAreaIgnoresPiecesTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="cu_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.roll = Material.objects.create(
            name="Акрил рулон", unit=Material.Unit.SQM, quantity=Decimal("500"),
            is_roll_material=True, price_per_sqm=Decimal("1000"),
            purchase_price=Decimal("600"), cut_rate_per_pm=Decimal("50"),
        )
        self.screws = Material.objects.create(
            name="Саморезы", unit=Material.Unit.PIECE, quantity=Decimal("1000"),
            price_per_unit=Decimal("5"), purchase_price=Decimal("2"),
        )
        self.cutting = PrintingService.objects.create(
            name="Резка", kind=PrintingService.Kind.CUTTING,
            machine=PrintingService.Machine.CNC,
        )

    def _cutting(self):
        r = self.client.get("/api/finance/report/")
        self.assertEqual(r.status_code, 200, r.data)
        return r.data["cutting"]

    def test_screws_sold_as_a_separate_line_do_not_become_square_metres(self):
        create_sale(
            client=None, cashier=self.admin, payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {"type": "SERVICE", "service": self.cutting, "material": self.roll,
                 "width": "0.554", "length": "1", "running_meters": "3"},
                # Без mode: режим по умолчанию — «по площади».
                {"type": "MATERIAL", "material": self.screws, "quantity": 100},
            ],
            amount_paid=None,
        )
        modes = set(TransactionItem.objects.filter(material=self.screws).values_list("sale_mode", flat=True))
        self.assertTrue(modes)          # строка саморезов есть
        cutting = self._cutting()
        self.assertEqual(Decimal(str(cutting["area"])), Decimal("0.55"))
        self.assertEqual(sum(Decimal(str(r["area"])) for r in cutting["rows"]), Decimal("0.55"))

    def test_square_metre_material_is_still_counted(self):
        create_sale(
            client=None, cashier=self.admin, payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {"type": "SERVICE", "service": self.cutting, "material": self.roll,
                 "width": "2", "length": "3", "running_meters": "10"},
            ],
            amount_paid=None,
        )
        self.assertEqual(Decimal(str(self._cutting()["area"])), Decimal("6"))
