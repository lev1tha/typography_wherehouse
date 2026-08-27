"""Инвентаризация материала, которому ПОМЕНЯЛИ форму со штучной на листовую.

Воспроизведение отказа с прода 27.08: `POST /api/warehouse/materials/adjust/`
отдавал 500 из «Склад → Каталог» после того, как у материала переключили форму
«Штучный» → «Лист» и стали править остаток.

Штучный материал приходит без партий: остаток ему двигает `apply_stock_change`,
а `Roll` у него нет ни одного. Переключение формы ставит `is_roll_material=True`
— и инвентаризация уходит в рулонную ветку, которая рассчитана на материал с
партиями.
"""
from decimal import Decimal

from rest_framework.test import APITransactionTestCase

from accounts.models import User
from warehouse.models import Material


class AdjustAfterFormChangeTests(APITransactionTestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="afc_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        # Материал завели штучным и приняли 100 шт — партий у него нет.
        self.mat = Material.objects.create(
            name="Бумага", unit=Material.Unit.PIECE, quantity=Decimal("100"),
            purchase_price=Decimal("30"), price_per_unit=Decimal("50"),
        )

    def _to_sheet(self, *, with_size=True):
        """То же, что делает карточка при переключении формы на «Лист»."""
        self.mat.is_roll_material = True
        self.mat.intake_form = Material.IntakeForm.SHEET
        self.mat.unit = Material.Unit.SQM
        if with_size:
            self.mat.sheet_width = Decimal("1.22")
            self.mat.sheet_height = Decimal("2.44")
        self.mat.save()
        self.mat.refresh_from_db()

    def _adjust(self, counted):
        return self.client.post("/api/warehouse/materials/adjust/", {
            "material": self.mat.id, "counted_quantity": str(counted),
        }, format="json")

    def test_adjust_down_after_switching_to_sheet(self):
        """Пересчитали в меньшую сторону — списывать не из чего, партий нет."""
        self._to_sheet()
        resp = self._adjust("40")
        self.assertLess(resp.status_code, 500, f"500 на уменьшении: {resp.content[:400]}")

    def test_adjust_up_after_switching_to_sheet(self):
        self._to_sheet()
        resp = self._adjust("150")
        self.assertLess(resp.status_code, 500, f"500 на увеличении: {resp.content[:400]}")

    def test_adjust_to_zero_after_switching_to_sheet(self):
        self._to_sheet()
        resp = self._adjust("0")
        self.assertLess(resp.status_code, 500, f"500 на обнулении: {resp.content[:400]}")

    def test_adjust_without_sheet_size(self):
        """Форму переключили, а размер листа задать забыли."""
        self._to_sheet(with_size=False)
        resp = self._adjust("40")
        self.assertLess(resp.status_code, 500, f"500 без размера листа: {resp.content[:400]}")

    def test_adjust_on_a_roll_form_material_without_lots(self):
        """Та же карточка, но переключили на «Рулон»."""
        self.mat.is_roll_material = True
        self.mat.intake_form = Material.IntakeForm.ROLL
        self.mat.unit = Material.Unit.SQM
        self.mat.roll_width = Decimal("1.2")
        self.mat.save()
        resp = self._adjust("40")
        self.assertLess(resp.status_code, 500, f"500 на рулоне: {resp.content[:400]}")
