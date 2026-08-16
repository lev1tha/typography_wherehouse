"""Данные, без которых печатные формы врут.

Колонка «Ед.» в накладной и счёте и колонка «Оплачено» в акте сверки берутся
не из воздуха — обе добавлены в API специально под документы, и обе легко
незаметно сломать.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales import sale_service
from sales.models import Receipt
from services.models import PrintingService
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot


class PrintDataTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="pr_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.customer = Client.objects.create(
            type=Client.Type.OSOO, company_name="ОсОО «Ак Жол»",
            phone="+996312556677", inn="02505201910136",
        )
        self.sheet = Material.objects.create(
            name="Форекс 8мм", unit=Material.Unit.SQM, is_roll_material=True,
            piece_area=Decimal("2"), price_per_sqm=Decimal("900"),
            piece_price=Decimal("1800"), cut_rate_per_pm=Decimal("45"),
        )
        receive_lot(
            self.sheet, form=Roll.Form.SHEET, area=Decimal("40"),
            purchase_cost=Decimal("20000"), user=self.admin,
        )
        self.bolts = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE,
            quantity=Decimal("500"), price_per_unit=Decimal("18"),
        )
        self.cutting = PrintingService.objects.create(
            name="Резка букв", kind=PrintingService.Kind.CUTTING,
            machine=PrintingService.Machine.CNC, rate_per_pm=Decimal("45"),
        )

    def _items(self, receipt):
        data = self.client.get(f"/api/sales/receipts/{receipt.id}/").data
        # DRF не кладёт в ответ поле, чей источник упёрся в None: у строки
        # материала нет `service_name`, у строки услуги — `material_name`.
        return {
            (i.get("material_name") or i.get("service_name")): i["unit_label"]
            for i in data["items"]
        }

    # ---- колонка «Ед.» -----------------------------------------------------
    def test_units_of_a_cut_piece(self):
        """Резка меряется погонными метрами, материал куска — квадратными.
        Смешать их в накладной — это счёт клиенту не на ту сумму."""
        receipt = sale_service.create_sale(
            client=self.customer, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": "SERVICE", "service": self.cutting, "material": self.sheet,
                "width": Decimal("0.5"), "length": Decimal("1.2"),
                "running_meters": Decimal("1.2"),
            }],
            amount_paid=Decimal("0"),
        )
        units = self._items(receipt)
        self.assertEqual(units["Резка букв"], "пог.м")
        self.assertEqual(units["Форекс 8мм"], "кв.м")

    def test_unit_of_a_whole_sheet(self):
        receipt = sale_service.create_sale(
            client=self.customer, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": "MATERIAL", "material": self.sheet,
                "quantity": Decimal("2"), "mode": "PIECE",
            }],
            amount_paid=Decimal("0"),
        )
        self.assertEqual(self._items(receipt)["Форекс 8мм"], "шт")

    def test_unit_of_a_piece_material(self):
        receipt = sale_service.create_sale(
            client=self.customer, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": "MATERIAL", "material": self.bolts,
                "quantity": Decimal("10"), "mode": "SQM",
            }],
            amount_paid=Decimal("0"),
        )
        self.assertEqual(self._items(receipt)["Крепёж"], "шт")

    # ---- «Оплачено» в акте сверки ------------------------------------------
    def test_client_orders_carry_amount_paid(self):
        """Оплата, принятая ПРЯМО В КАССЕ, записи `Payment` не создаёт — она
        заводится только при погашении долга. Без `amount_paid` на заказе акт
        сверки показывал бы все заказы неоплаченными.
        """
        sale_service.create_sale(
            client=self.customer, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": "MATERIAL", "material": self.bolts,
                "quantity": Decimal("10"), "mode": "SQM",
            }],
            amount_paid=Decimal("100"),
        )
        card = self.client.get(f"/api/clients/clients/{self.customer.id}/").data
        order = card["orders"][0]
        self.assertEqual(Decimal(str(order["amount_paid"])), Decimal("100"))
        self.assertEqual(card["payments"], [])

    def test_act_balance_matches_the_debt(self):
        """Сальдо акта = Σ(начислено) − Σ(оплачено) и обязано совпасть с долгом
        клиента: разойдись они — спорить с клиентом будет нечем."""
        receipt = sale_service.create_sale(
            client=self.customer, cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": "MATERIAL", "material": self.bolts,
                "quantity": Decimal("10"), "mode": "SQM",
            }],
            amount_paid=Decimal("100"),
        )
        # Часть долга гасим позже — это уже отдельная запись `Payment`.
        sale_service.apply_payment(receipt, amount=Decimal("50"), user=self.admin)

        card = self.client.get(f"/api/clients/clients/{self.customer.id}/").data
        order = card["orders"][0]
        later = sum(Decimal(str(p["amount"])) for p in card["payments"])
        charged = Decimal(str(order["total_price"]))
        # Так же, как считает акт: предоплата = принято всего − поздние платежи.
        upfront = Decimal(str(order["amount_paid"])) - later
        balance = charged - upfront - later
        self.assertEqual(later, Decimal("50"))
        self.assertEqual(balance, Decimal(str(card["debt"])))
