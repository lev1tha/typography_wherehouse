"""Партии у ШТУЧНОГО материала: приход заводит их, касса даёт выбрать.

Просьба владельца (2026-08-27). До этого партий у штучных не было вовсе: приход
поднимал только число остатка, и новая закупочная цена молча переоценивала весь
старый запас. Для саморезов неважно, для дорогой смолы — уже нет: две поставки
одного оргстекла по 320 и по 450 лежат на полке одновременно, а система знала
про них одну цену.

Хранится штучная партия тем же `Roll`: `initial_area` — это КОЛИЧЕСТВО, а
`cost_per_sqm` — цена одной штуки. Заводить второй, почти такой же механизм ради
другой единицы значило бы удвоить FIFO, возврат в свою партию, себестоимость
снимком и журнал.

**Материал БЕЗ партий работает как раньше.** Остаток, заведённый до этой правки,
списывается по закупочной цене из карточки: FIFO по нему не из чего считать, а
запретить его продажу значило бы заморозить весь старый склад.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import TransactionItem
from sales.sale_service import create_sale, refund_receipt
from warehouse.models import Material, Roll

SUPPLY = "/api/warehouse/materials/supply/"


class PieceLotsTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="pl_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.mat = Material.objects.create(
            name="Оргстекло 2 мм", unit=Material.Unit.PIECE,
            quantity=Decimal("0"), purchase_price=Decimal("0"),
            price_per_unit=Decimal("500"),
        )

    def _receive(self, qty, unit_cost, code=""):
        r = self.client.post(SUPPLY, {
            "material": self.mat.id, "quantity": str(qty),
            "actual_price": str(unit_cost), "reason": code,
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.mat.refresh_from_db()
        return r

    def _sell(self, qty, roll=None):
        item = {"type": "MATERIAL", "material": self.mat,
                "quantity": Decimal(str(qty)), "mode": "PIECE"}
        if roll is not None:
            item["roll"] = roll
        return create_sale(
            client=None, cashier=self.admin, payment_method="CASH",
            items_data=[item], amount_paid=Decimal("0"),
        )

    # ---- приход ------------------------------------------------------------
    def test_intake_creates_a_piece_lot(self):
        self._receive(50, 320, "поставка А")
        lot = Roll.objects.get()
        self.assertEqual(lot.form, Roll.Form.PIECE)
        self.assertEqual(lot.initial_area, Decimal("50.0000"))
        self.assertEqual(lot.cost_per_sqm, Decimal("320.00"))
        self.assertEqual(lot.code, "поставка А")
        self.assertEqual(self.mat.quantity, Decimal("50.0000"))

    def test_intake_does_not_turn_the_material_into_an_area_one(self):
        """Пометить саморезы площадными — значит перевести их в кв.м."""
        self._receive(50, 320)
        self.assertFalse(self.mat.is_roll_material)
        self.assertEqual(self.mat.unit, Material.Unit.PIECE)

    def test_lot_is_labelled_in_the_material_unit(self):
        self._receive(50, 320)
        self.assertEqual(Roll.objects.get().dimensions_label, "50 шт")

    def test_two_lots_keep_their_own_price(self):
        self._receive(50, 320, "А")
        self._receive(30, 450, "Б")
        got = {r.code: r.cost_per_sqm for r in Roll.objects.all()}
        self.assertEqual(got, {"А": Decimal("320.00"), "Б": Decimal("450.00")})

    # ---- продажа -----------------------------------------------------------
    def test_sale_without_a_choice_takes_the_oldest(self):
        self._receive(50, 320, "А")
        self._receive(30, 450, "Б")
        receipt = self._sell(10)
        item = receipt.items.get()
        # 10 × 320 — старейшая партия.
        self.assertEqual(item.cost_total, Decimal("3200.00"))

    def test_sale_from_the_chosen_lot_uses_its_price(self):
        """Ради чего всё: взяли из той поставки, что стоит ближе."""
        self._receive(50, 320, "А")
        self._receive(30, 450, "Б")
        lot_b = Roll.objects.get(code="Б")
        receipt = self._sell(10, roll=lot_b)
        item = receipt.items.get()
        self.assertEqual(item.cost_total, Decimal("4500.00"))   # 10 × 450
        self.assertEqual(item.roll_id, lot_b.id, "партия не запомнилась на строке")
        lot_b.refresh_from_db()
        self.assertEqual(lot_b.remaining_area, Decimal("20.0000"))
        # Старейшая не тронута.
        self.assertEqual(Roll.objects.get(code="А").remaining_area, Decimal("50.0000"))

    def test_refund_returns_into_the_same_lot(self):
        self._receive(50, 320, "А")
        self._receive(30, 450, "Б")
        lot_b = Roll.objects.get(code="Б")
        receipt = self._sell(10, roll=lot_b)
        refund_receipt(receipt, user=self.admin)
        lot_b.refresh_from_db()
        self.assertEqual(lot_b.remaining_area, Decimal("30.0000"), "вернулось не в свою партию")
        self.assertEqual(Roll.objects.get(code="А").remaining_area, Decimal("50.0000"))

    def test_sale_spanning_two_lots_costs_each_at_its_own_price(self):
        """Партия кончилась посреди заказа — добираем со следующей."""
        self._receive(10, 320, "А")
        self._receive(30, 450, "Б")
        receipt = self._sell(15)
        # 10 × 320 + 5 × 450 = 3200 + 2250 = 5450
        self.assertEqual(receipt.items.get().cost_total, Decimal("5450.00"))

    # ---- старый запас без партий -------------------------------------------
    def test_material_without_lots_still_sells(self):
        """Остаток, заведённый до этой правки, продаётся по цене из карточки."""
        self.mat.quantity = Decimal("100")
        self.mat.purchase_price = Decimal("300")
        self.mat.save(update_fields=["quantity", "purchase_price"])
        self.assertFalse(Roll.objects.filter(material=self.mat).exists())

        receipt = self._sell(4)
        self.assertEqual(receipt.items.get().cost_total, Decimal("1200.00"))
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("96.0000"))

    def test_intake_without_a_price_falls_back_to_the_card(self):
        """Цену не назвали — партия берёт последнюю закупочную, а не ноль.

        Партия с нулевой себестоимостью завысила бы прибыль на всю свою
        стоимость, причём молча.
        """
        self.mat.purchase_price = Decimal("280")
        self.mat.save(update_fields=["purchase_price"])
        r = self.client.post(SUPPLY, {
            "material": self.mat.id, "quantity": "10",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Roll.objects.get().cost_per_sqm, Decimal("280.00"))
