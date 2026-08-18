"""Рулонный материал продаётся ПОГОННЫМИ МЕТРАМИ.

Ширина рулона — свойство товара, а не поле ввода: ткань 0.9 м режут поперёк на
всю ширину, и 40 см ширины купить нельзя. Раньше цена считалась через площадь,
а ширину вбивал мастер — и в чек уезжало «1250 × 2.1 = 2625», где 2.1 это
1.5 × 1.4: ширина из прошлого заказа осталась в поле и залезла в расчёт.
"""
from decimal import Decimal

from django.db.models import Sum
from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt, TransactionItem
from warehouse.models import Material, Roll


class RollSoldByMetreTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="rm_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        # Туника: рулон шириной 0.9 м, прайс — 300 сом за погонный метр.
        self.mat = Material.objects.create(
            name="Туника", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL,
            roll_width=Decimal("0.9"), price_per_pm=Decimal("300"),
        )
        r = self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": self.mat.id, "form": "ROLL",
            "width": "0.9", "length": "50", "purchase_cost": "9000",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def _sell(self, metres, **extra):
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": self.mat.id,
                       "mode": "METER", "quantity": str(metres), **extra}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return Receipt.objects.get(pk=r.data["id"])

    def _left(self):
        self.mat.refresh_from_db()
        return self.mat.quantity

    # --- цена ---
    def test_price_is_rate_times_length(self):
        """Ровно пример владельца: 300 сом/пог.м × 1.4 м = 420."""
        receipt = self._sell("1.4")
        self.assertEqual(receipt.total_price, Decimal("420"))

    def test_width_cannot_leak_into_the_price(self):
        """Ширина в расчёт цены не входит вообще — её негде и подать."""
        by_metre = self._sell("1.4").total_price
        self.assertNotEqual(by_metre, Decimal("378"))   # 0.9 × 1.4 × 300
        self.assertNotEqual(by_metre, Decimal("630"))   # 1.5 × 1.4 × 300

    def test_the_line_is_measured_in_running_metres(self):
        receipt = self._sell("1.4")
        item = receipt.items.get()
        self.assertEqual(item.sale_mode, TransactionItem.SaleMode.METER)
        self.assertEqual(item.quantity, Decimal("1.400"))
        self.assertEqual(item.price_per_item, Decimal("300"))

    def test_mode_is_never_guessed_by_the_server(self):
        """Режим приходит явно и по справочнику НЕ подставляется.

        В режиме площади касса шлёт в `quantity` площадь. Молчаливая подмена
        «это же рулон, значит метры» превратила бы 1.26 кв.м в 1.26 пог.м —
        цифра выглядит правдоподобно, а заказ посчитан не по тому. Форму
        выбирает касса по `sells_by_metre`, сервер её не угадывает.

        Не угадывает — и не пропускает: у рулона нет цены за кв.м, и площадь
        уходила в чек за 0 сом (так «повторить заказ» продавал 1 пог.м как 1 кв.м
        бесплатно; аудит 2026-08-18, п. 2). Площадь у рулона — ошибка ввода,
        а не другой способ посчитать.
        """
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": self.mat.id,
                       "mode": "SQM", "quantity": "1.26"}],
        }, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("погонными метрами", str(r.data))
        self.assertEqual(Receipt.objects.count(), 0)

    def test_admin_can_still_override_the_price(self):
        receipt = self._sell("2", material_price="250")
        self.assertEqual(receipt.total_price, Decimal("500"))

    # --- склад ---
    def test_stock_goes_down_by_the_full_width(self):
        """Отрезают поперёк на всю ширину: 1.4 м рулона 0.9 = 1.26 кв.м."""
        before = self._left()
        self._sell("1.4")
        self.assertEqual(before - self._left(), Decimal("1.2600"))

    def test_narrow_piece_still_consumes_the_whole_width(self):
        """Клиенту нужна полоса 0.5 м шириной — всё равно уходит вся ширина,
        обрезок остаётся в цехе. Иначе цех дарил бы половину рулона."""
        before = self._left()
        self._sell("2")
        self.assertEqual(before - self._left(), Decimal("0.9") * Decimal("2"))

    def test_lots_and_stock_stay_in_step(self):
        self._sell("3")
        self.mat.refresh_from_db()
        lots = Roll.objects.filter(material=self.mat).aggregate(v=Sum("remaining_area"))["v"]
        self.assertEqual(self.mat.quantity, lots)

    def test_remaining_is_shown_in_running_metres(self):
        """Владелец меряет рулон метрами — «осталось 47.1 м», а не 42.39 кв.м."""
        self._sell("2.9")
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.metres_remaining, Decimal("47.10"))

    def test_refund_returns_the_full_width_too(self):
        before = self._left()
        receipt = self._sell("1.4")
        self.client.post(f"/api/sales/receipts/{receipt.id}/refund/", {}, format="json")
        self.assertEqual(self._left(), before)

    # --- лист не задет ---
    def test_sheet_material_is_untouched(self):
        sheet = Material.objects.create(
            name="Акрил 3мм", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
            price_per_sqm=Decimal("1500"),
        )
        self.assertFalse(sheet.sells_by_metre)
        self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": sheet.id, "form": "SHEET", "width": "1.22",
            "height": "2.44", "sheet_count": "3", "purchase_cost": "7800",
        }, format="json")
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "MATERIAL", "material": sheet.id,
                       "mode": "SQM", "quantity": "0.6"}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        # Лист по-прежнему считается площадью: 0.6 кв.м × 1500.
        self.assertEqual(Decimal(r.data["total_price"]), Decimal("900"))
