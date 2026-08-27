"""Рулон заводится сеткой массового ввода — как лист.

До этого сетка умела только листы и штучное: у рулона нет ни ширины полотна, ни
цены за метр, и каждый рулонный материал приходилось заводить карточкой по
одному. Форму выводим из ЗАПОЛНЕННЫХ полей: стоит ширина рулона — значит рулон,
стоит размер листа — значит лист. Отдельной колонки «форма» нет намеренно.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from warehouse.models import Material


class BulkRollsTests(APITestCase):
    URL = "/api/warehouse/materials/bulk/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="br_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)

    def _post(self, *rows):
        return self.client.post(self.URL, {"rows": list(rows)}, format="json")

    def test_roll_row_creates_a_roll_material(self):
        r = self._post({
            "name": "Плёнка Oracal", "type": "Плёнка",
            "roll_width": "1.26", "price_per_pm": "260", "cut_rate_per_pm": "40",
        })
        self.assertEqual(r.status_code, 201, r.data)
        m = Material.objects.get(name="Плёнка Oracal")
        self.assertTrue(m.is_roll_material)
        self.assertEqual(m.intake_form, Material.IntakeForm.ROLL)
        self.assertEqual(m.unit, Material.Unit.SQM)
        self.assertEqual(m.roll_width, Decimal("1.260"))
        self.assertEqual(m.price_per_pm, Decimal("260.00"))
        # Продажа метрами включается формой — то самое, ради чего всё затевалось.
        self.assertTrue(m.sells_by_metre)

    def test_sheet_row_still_creates_a_sheet(self):
        r = self._post({
            "name": "Акрил белый", "type": "Акрил",
            "sheet_width": "1.22", "sheet_height": "2.44", "price_per_sqm": "1500",
        })
        self.assertEqual(r.status_code, 201, r.data)
        m = Material.objects.get(name="Акрил белый")
        self.assertTrue(m.is_roll_material)
        self.assertEqual(m.intake_form, Material.IntakeForm.SHEET)
        self.assertFalse(m.sells_by_metre)

    def test_roll_without_price_per_pm_is_rejected(self):
        """Без цены за метр рулон не продать — касса откажет на первой продаже,
        и узнать об этом в момент ввода каталога лучше, чем у клиента."""
        r = self._post({"name": "Баннер", "type": "Плёнка", "roll_width": "1.6"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("price_per_pm", r.data["errors"][0]["fields"])
        self.assertFalse(Material.objects.filter(name="Баннер").exists())

    def test_roll_and_sheet_sizes_together_are_rejected(self):
        r = self._post({
            "name": "Непонятно что", "type": "Плёнка",
            "roll_width": "1.6", "price_per_pm": "300",
            "sheet_width": "1.22", "sheet_height": "2.44",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("roll_width", r.data["errors"][0]["fields"])

    def test_whole_batch_is_rejected_when_one_roll_row_is_broken(self):
        """Всё или ничего — как и было: пачка не должна оставить полкаталога."""
        r = self._post(
            {"name": "Хорошая плёнка", "type": "Плёнка", "roll_width": "1.26", "price_per_pm": "260"},
            {"name": "Плохая плёнка", "type": "Плёнка", "roll_width": "1.26"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Material.objects.filter(name="Хорошая плёнка").exists())
