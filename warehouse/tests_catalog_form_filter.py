"""Фильтр каталога по форме материала: штучный / лист / рулон.

В базе форма — это ПАРА полей (`is_roll_material` + `intake_form`), и фильтровать
по ним по отдельности бесполезно: `is_roll_material=True` — это и лист, и рулон
разом. Поэтому у списка материалов свой параметр `?form=`.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from warehouse.models import Material, MaterialType


class CatalogFormFilterTests(APITestCase):
    URL = "/api/warehouse/materials/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="cff_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        acryl = MaterialType.objects.get(code="ACRYL")
        self.sheet = Material.objects.create(
            name="Белый акрил 3 мм", type=acryl, unit=Material.Unit.SQM,
            is_roll_material=True, intake_form=Material.IntakeForm.SHEET,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
        )
        self.roll = Material.objects.create(
            name="Плёнка Oracal", type=acryl, unit=Material.Unit.SQM,
            is_roll_material=True, intake_form=Material.IntakeForm.ROLL,
            roll_width=Decimal("1.26"), price_per_pm=Decimal("300"),
        )
        self.piece = Material.objects.create(
            name="Саморез 4×30", type=acryl, unit=Material.Unit.PIECE,
        )
        self.hidden = Material.objects.create(
            name="Старая плёнка", type=acryl, unit=Material.Unit.SQM,
            is_roll_material=True, intake_form=Material.IntakeForm.ROLL,
            roll_width=Decimal("1.0"), is_archived=True,
        )

    def _names(self, **params):
        response = self.client.get(self.URL, params)
        self.assertEqual(response.status_code, 200)
        return {row["name"] for row in response.data["results"]}

    def test_filters_by_form(self):
        self.assertEqual(self._names(form="PIECE"), {"Саморез 4×30"})
        self.assertEqual(self._names(form="SHEET"), {"Белый акрил 3 мм"})
        self.assertEqual(self._names(form="ROLL"), {"Плёнка Oracal"})

    def test_without_the_filter_everything_visible_is_listed(self):
        self.assertEqual(
            self._names(),
            {"Белый акрил 3 мм", "Плёнка Oracal", "Саморез 4×30"},
        )

    def test_hidden_materials_stay_hidden_under_the_filter(self):
        """Фильтр формы не должен возвращать в каталог скрытые материалы —
        иначе «покажи рулоны» вытаскивало бы удалённые вместе с живыми."""
        self.assertNotIn("Старая плёнка", self._names(form="ROLL"))
        self.assertEqual(self._names(form="ROLL", archived="1"), {"Старая плёнка"})

    def test_unknown_form_value_is_ignored(self):
        self.assertEqual(len(self._names(form="ЧТО-ТО")), 3)
