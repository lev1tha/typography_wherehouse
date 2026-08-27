"""Цена за кв.м и цена за лист — РАЗНЫЕ числа, и касса берёт каждое своё.

Решение владельца (2026-08-27): кв.м куском и кв.м внутри целого листа — разные
товары. За кусок платят дороже: обрезок остаётся в цехе, а рез — это работа.

Раздельные поля в базе были с самого начала, но форма материала пересчитывала
одно из другого, и развести их было нельзя — у всех 26 листовых материалов
каталога цена листа с точностью до сома равнялась «за кв.м × площадь». Связку
сняли в карточке (`Catalog.jsx`), а эти тесты держат ту же границу со стороны
СЕРВЕРА: там, где считаются деньги.

Смысл именно в расхождении: цены здесь взяты так, что вывести одну из другой
нельзя. Если кто-нибудь потом «по-хорошему» заставит сервер считать кв.м из
цены листа (или наоборот), сломается ровно это.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import TransactionItem
from sales.sale_service import create_sale
from warehouse.models import Material
from warehouse.rolls import receive_lot


class SqmPriceIndependentTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="spi_admin", password="x", role=User.Role.ADMIN
        )
        # Лист 1 × 2 = 2 кв.м. Куском — 1700 за кв.м, целым листом — 3000.
        # Через площадь лист стоил бы 3400, через цену листа кв.м был бы 1500:
        # ни одна цифра не выводится из другой, и подмена сразу видна.
        self.mat = Material.objects.create(
            name="Белый акрил", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET,
            sheet_width=Decimal("1"), sheet_height=Decimal("2"),
            price_per_sqm=Decimal("1700"), piece_price=Decimal("3000"),
        )
        receive_lot(
            self.mat, form="SHEET", width=Decimal("1"), height=Decimal("2"),
            sheet_count=10, purchase_cost=Decimal("5000"), code="пачка",
        )

    def _sell(self, *, mode, quantity):
        return create_sale(
            client=None, cashier=self.admin, payment_method="CASH",
            items_data=[{
                "type": "MATERIAL", "material": self.mat,
                "quantity": Decimal(quantity), "mode": mode,
            }],
            amount_paid=Decimal("0"),
        )

    def _price(self, receipt):
        return receipt.items.get(type=TransactionItem.Type.MATERIAL).price_per_item

    def test_square_metre_uses_its_own_price(self):
        """1 кв.м куском — 1700, а не 1500 (доля цены листа)."""
        self.assertEqual(self._price(self._sell(mode="SQM", quantity="1")), Decimal("1700"))

    def test_whole_sheet_uses_its_own_price(self):
        """Лист целиком — 3000, а не 3400 (площадь × цена за кв.м)."""
        self.assertEqual(self._price(self._sell(mode="PIECE", quantity="1")), Decimal("3000"))

    def test_the_same_area_costs_more_in_pieces_than_as_a_whole_sheet(self):
        """Ради чего всё: 2 кв.м кусками дороже, чем тот же лист целиком."""
        by_area = self._sell(mode="SQM", quantity="2").total_price
        whole = self._sell(mode="PIECE", quantity="1").total_price
        self.assertEqual(by_area, Decimal("3400"))
        self.assertEqual(whole, Decimal("3000"))
        self.assertGreater(
            by_area, whole,
            "кв.м куском вышел не дороже целого листа — цены снова связаны",
        )

    def test_changing_one_price_leaves_the_other_alone(self):
        """Сервер не пересчитывает пару при сохранении карточки."""
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(
            f"/api/warehouse/materials/{self.mat.id}/",
            {"price_per_sqm": "1900"}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.price_per_sqm, Decimal("1900"))
        self.assertEqual(
            self.mat.piece_price, Decimal("3000"),
            "правка цены за кв.м переписала цену листа на сервере",
        )

    def test_zero_piece_price_is_not_a_missing_price(self):
        """Ноль в цене листа — «продажа целиком недоступна», и он не подменяется.

        У ШТУЧНОГО материала ноль означает другое (цены за лист там не бывает
        вовсе, берётся обычная розничная) — здесь материал листовой.
        """
        self.mat.piece_price = Decimal("0")
        self.mat.save(update_fields=["piece_price"])
        self.assertEqual(self._price(self._sell(mode="PIECE", quantity="1")), Decimal("0"))
        # А цена за кв.м при этом живёт своей жизнью и работает.
        self.assertEqual(self._price(self._sell(mode="SQM", quantity="1")), Decimal("1700"))
