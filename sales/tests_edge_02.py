from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt, TransactionItem
from warehouse.models import Material


class EdgeWholesaleTests(APITestCase):
    """Граничные/edge-случаи оптовой цены на листы (режим PIECE).

    Бьём через HTTP-эндпоинт /api/sales/receipts/checkout/ (как в sales/tests.py),
    плюс точечно дёргаем чистую доменную функцию Material.piece_price_for_qty.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="edge_store", password="x", role=User.Role.STOREKEEPER
        )
        self.client.force_authenticate(self.user)
        # Лист продаётся целиком (piece_price=3700), площадь листа 2.98 кв.м.
        self.acrylic = Material.objects.create(
            name="Акрил 3мм опт", category="Акрил", unit="SQM",
            quantity=Decimal("1000"), price_per_unit=Decimal("0"),
            price_per_sqm=Decimal("1400"), piece_price=Decimal("3700"),
            piece_area=Decimal("2.98"),
        )

    def _checkout(self, items):
        return self.client.post(
            "/api/sales/receipts/checkout/",
            {"payment_method": "CASH", "items": items},
            format="json",
        )

    def _item_price(self, response):
        self.assertEqual(response.status_code, 201, response.data)
        return Receipt.objects.get(pk=response.data["id"]).items.get().price_per_item

    # --- граница порога ---

    def test_qty_exactly_min_triggers_wholesale(self):
        self.acrylic.wholesale_price = Decimal("3000")
        self.acrylic.wholesale_min_qty = Decimal("3")
        self.acrylic.save()
        r = self._checkout([
            {"type": "MATERIAL", "material": self.acrylic.id,
             "quantity": 3, "mode": "PIECE"}
        ])
        self.assertEqual(self._item_price(r), Decimal("3000"))

    def test_qty_min_minus_one_stays_retail(self):
        self.acrylic.wholesale_price = Decimal("3000")
        self.acrylic.wholesale_min_qty = Decimal("3")
        self.acrylic.save()
        r = self._checkout([
            {"type": "MATERIAL", "material": self.acrylic.id,
             "quantity": 2, "mode": "PIECE"}
        ])
        self.assertEqual(self._item_price(r), Decimal("3700"))

    def test_min_qty_one_triggers_from_first_sheet(self):
        self.acrylic.wholesale_price = Decimal("3000")
        self.acrylic.wholesale_min_qty = Decimal("1")
        self.acrylic.save()
        r = self._checkout([
            {"type": "MATERIAL", "material": self.acrylic.id,
             "quantity": 1, "mode": "PIECE"}
        ])
        self.assertEqual(self._item_price(r), Decimal("3000"))

    def test_fractional_qty_at_boundary(self):
        self.acrylic.wholesale_price = Decimal("3000")
        self.acrylic.wholesale_min_qty = Decimal("3")
        self.acrylic.save()
        # 2.5 < 3 → розница.
        r = self._checkout([
            {"type": "MATERIAL", "material": self.acrylic.id,
             "quantity": "2.5", "mode": "PIECE"}
        ])
        self.assertEqual(self._item_price(r), Decimal("3700"))
        # 3.0 ровно → опт.
        r = self._checkout([
            {"type": "MATERIAL", "material": self.acrylic.id,
             "quantity": "3.0", "mode": "PIECE"}
        ])
        self.assertEqual(self._item_price(r), Decimal("3000"))

    # --- конфигурации опта ---

    def test_wholesale_price_zero_means_no_wholesale(self):
        self.acrylic.wholesale_price = Decimal("0")
        self.acrylic.wholesale_min_qty = Decimal("2")
        self.acrylic.save()
        r = self._checkout([
            {"type": "MATERIAL", "material": self.acrylic.id,
             "quantity": 5, "mode": "PIECE"}
        ])
        self.assertEqual(self._item_price(r), Decimal("3700"))

    def test_min_qty_zero_does_not_apply_wholesale(self):
        # Подозрение на конфиг-баг: 'опт от 0' интуитивно = 'всегда опт',
        # но Decimal('0') falsy → условие не срабатывает. Ассертим фактическое
        # поведение кода (розница). Падение здесь = поведение изменили.
        self.acrylic.wholesale_price = Decimal("3000")
        self.acrylic.wholesale_min_qty = Decimal("0")
        self.acrylic.save()
        r = self._checkout([
            {"type": "MATERIAL", "material": self.acrylic.id,
             "quantity": 1, "mode": "PIECE"}
        ])
        self.assertEqual(self._item_price(r), Decimal("3700"))
        # Доменная проверка той же ветки напрямую.
        self.assertEqual(
            self.acrylic.piece_price_for_qty(Decimal("10")), Decimal("3700")
        )

    # --- приоритет ручного override над оптом (режим PIECE) ---

    def test_manual_override_beats_wholesale_in_piece_mode(self):
        self.acrylic.wholesale_price = Decimal("3000")
        self.acrylic.wholesale_min_qty = Decimal("3")
        self.acrylic.save()
        # qty=4 (опт активен), но кассир задал ручную цену 2500 — она в приоритете.
        r = self._checkout([
            {"type": "MATERIAL", "material": self.acrylic.id,
             "quantity": 4, "mode": "PIECE", "material_price": "2500"}
        ])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        item = receipt.items.get()
        self.assertEqual(item.price_per_item, Decimal("2500"))
        self.assertEqual(item.sale_mode, TransactionItem.SaleMode.PIECE)
        # Итог = 4 × 2500.
        self.assertEqual(receipt.total_price, Decimal("10000.00"))

    # --- чистая доменная функция: точные границы ---

    def test_piece_price_for_qty_boundaries_unit(self):
        self.acrylic.wholesale_price = Decimal("3000")
        self.acrylic.wholesale_min_qty = Decimal("3")
        self.acrylic.save()
        self.assertEqual(self.acrylic.piece_price_for_qty(Decimal("2")), Decimal("3700"))
        self.assertEqual(self.acrylic.piece_price_for_qty(Decimal("3")), Decimal("3000"))
        self.assertEqual(self.acrylic.piece_price_for_qty(Decimal("4")), Decimal("3000"))
        # qty=0 и None → розница (qty < min).
        self.assertEqual(self.acrylic.piece_price_for_qty(0), Decimal("3700"))
        self.assertEqual(self.acrylic.piece_price_for_qty(None), Decimal("3700"))
