"""Стоимость склада — по остаткам ПАРТИЙ, а не по цене последнего прихода.

Раньше `Material.stock_value` считал `quantity × purchase_price`, а
`purchase_price` у материала по кв.м обновляется каждым поступлением: это цена
ПОСЛЕДНЕЙ партии. Из-за этого весь остаток переоценивался по последнему приходу
— подорожал акрил вдвое, и «Стоимость склада» в обзоре вырастала вдвое, хотя на
складе лежал тот же самый материал, купленный по старой цене.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from warehouse.models import InventoryLog, Material, Roll
from warehouse.rolls import consume_area, receive_lot
from warehouse.stock import apply_stock_change


class StockValueTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="sv_admin", password="x", role=User.Role.ADMIN
        )
        self.material = Material.objects.create(
            name="Акрил 2мм", unit=Material.Unit.SQM, is_roll_material=True,
            piece_area=Decimal("2"),
        )

    def _lot(self, area, cost):
        return receive_lot(
            self.material, form=Roll.Form.SHEET, area=Decimal(area),
            purchase_cost=Decimal(cost), user=self.admin,
        )

    def test_two_lots_are_valued_each_at_its_own_cost(self):
        self._lot(10, 1000)   # 100 сом/кв.м
        self._lot(10, 3000)   # 300 сом/кв.м
        self.material.refresh_from_db()
        # Последний приход втрое дороже, но старая партия от этого не дорожает.
        self.assertEqual(self.material.purchase_price, Decimal("300"))
        self.assertEqual(self.material.stock_value, Decimal("4000"))

    def test_selling_the_old_lot_leaves_the_new_price(self):
        """Продали всё, что было по старой цене — склад стоит по новой."""
        self._lot(10, 1000)
        self._lot(10, 3000)
        consume_area(
            self.material, Decimal("10"), user=self.admin,
            log_type=InventoryLog.Type.SALE,
        )
        self.material.refresh_from_db()
        self.assertEqual(self.material.quantity, Decimal("10"))
        self.assertEqual(self.material.stock_value, Decimal("3000"))

    def test_piece_material_still_uses_its_purchase_price(self):
        """У штучного материала партий нет — считаем по его закупочной цене."""
        bolts = Material.objects.create(
            name="Саморезы", unit=Material.Unit.PIECE,
            quantity=Decimal("50"), purchase_price=Decimal("3"),
        )
        self.assertEqual(bolts.stock_value, Decimal("150"))

    def test_stock_added_past_the_lots_is_valued_at_last_price(self):
        """Инвентаризация правит количество, партий не создавая.

        Остаток сверх партий оценивать нечем, кроме последней закупочной цены —
        зато он и не пропадает из стоимости склада.
        """
        self._lot(10, 1000)  # 100 сом/кв.м
        apply_stock_change(
            self.material, Decimal("5"), log_type=InventoryLog.Type.ADJUSTMENT,
            reason="пересчёт", user=self.admin,
        )
        self.material.refresh_from_db()
        self.assertEqual(self.material.quantity, Decimal("15"))
        # 10 из партии по 100 + 5 «лишних» по той же последней цене.
        self.assertEqual(self.material.stock_value, Decimal("1500"))

    def test_inventory_shortage_does_not_value_phantom_area(self):
        """Пересчёт нашёл меньше, чем числится в партиях — считаем по факту."""
        self._lot(10, 1000)
        apply_stock_change(
            self.material, Decimal("-4"), log_type=InventoryLog.Type.ADJUSTMENT,
            reason="недостача", user=self.admin,
        )
        self.material.refresh_from_db()
        self.assertEqual(self.material.stock_value, Decimal("600"))

    def test_empty_stock_is_worth_nothing(self):
        self._lot(10, 1000)
        consume_area(
            self.material, Decimal("10"), user=self.admin,
            log_type=InventoryLog.Type.SALE,
        )
        self.material.refresh_from_db()
        self.assertEqual(self.material.stock_value, Decimal("0"))

    def test_dashboard_asset_matches_the_sum_of_materials(self):
        self._lot(10, 1000)
        self._lot(10, 3000)
        Material.objects.create(
            name="Саморезы", unit=Material.Unit.PIECE,
            quantity=Decimal("50"), purchase_price=Decimal("3"),
        )
        self.client.force_authenticate(self.admin)
        r = self.client.get("/api/audit/dashboard/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(str(r.data["unrealised_asset"])), Decimal("4150"))
