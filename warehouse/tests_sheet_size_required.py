"""У формы «Лист» размер листа обязателен — из него считается всё «в листах».

Находка с прода 27.08: в каталоге у «Акрил белый 2,5 мм» остаток стоял просто
«36 кв.м», без «≈ N лист.», и под закупочной ценой не было строки «сом/лист» —
при том что у соседних материалов (Форекс 1,2×2,4, Ромарк 1,2×0,6) обе строки
на месте.

Причина одна на оба пропуска: у карточки не заполнен размер листа, значит
`piece_area = 0`, а обе цифры считаются из неё:

    остаток в листах   = quantity ÷ piece_area
    закупка за лист    = purchase_price × piece_area

Розничная цена за лист при этом ВИДНА — она лежит отдельным полем
(`piece_price`) и площади не требует. Оттого и выглядело как каприз системы:
одна строка «за лист» есть, две других нет.

Особенно легко попасть, переключив штучный материал на листовой: размера у него
никогда не было, а форма его не требовала. Ровно так и появился «Акрил».

У формы «Рулон» ширина обязательна с самого начала — здесь та же мысль,
доведённая до листа.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from warehouse.models import Material

URL = "/api/warehouse/materials/"


class SheetSizeRequiredTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="ssr_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)

    def _sheet_payload(self, **extra):
        body = {
            "name": "Акрил белый 2,5 мм",
            "unit": Material.Unit.SQM,
            "is_roll_material": True,
            "intake_form": Material.IntakeForm.SHEET,
            "price_per_sqm": "1450",
        }
        body.update(extra)
        return body

    # ---- заведение нового материала ---------------------------------------
    def test_sheet_without_size_is_rejected(self):
        resp = self.client.post(URL, self._sheet_payload(), format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("sheet_width", resp.data)

    def test_sheet_with_size_is_accepted_and_gets_its_area(self):
        resp = self.client.post(URL, self._sheet_payload(
            sheet_width="1.22", sheet_height="2.44",
        ), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        mat = Material.objects.get(pk=resp.data["id"])
        self.assertEqual(mat.piece_area, Decimal("2.9768"))

    def test_area_may_be_given_directly_for_a_non_standard_sheet(self):
        """Обрезной лист без ровных размеров — площадь вписывают руками."""
        resp = self.client.post(URL, self._sheet_payload(piece_area="1.75"),
                                format="json")
        self.assertEqual(resp.status_code, 201, resp.data)

    # ---- переключение формы у существующего материала ---------------------
    def _piece_material(self):
        return Material.objects.create(
            name="Бумага", unit=Material.Unit.PIECE, quantity=Decimal("36"),
            purchase_price=Decimal("3000"), price_per_unit=Decimal("1450"),
        )

    def test_switching_a_piece_material_to_sheet_without_size_is_rejected(self):
        """Тот самый путь, которым «Акрил» остался без размера."""
        mat = self._piece_material()
        resp = self.client.patch(f"{URL}{mat.id}/", {
            "is_roll_material": True,
            "intake_form": Material.IntakeForm.SHEET,
            "unit": Material.Unit.SQM,
        }, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        mat.refresh_from_db()
        self.assertFalse(mat.is_roll_material, "форму переключили несмотря на отказ")

    def test_switching_to_sheet_with_size_works(self):
        mat = self._piece_material()
        resp = self.client.patch(f"{URL}{mat.id}/", {
            "is_roll_material": True,
            "intake_form": Material.IntakeForm.SHEET,
            "unit": Material.Unit.SQM,
            "sheet_width": "1.22", "sheet_height": "2.44",
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)

    # ---- ради чего всё: обе строки «в листах» появляются -------------------
    def test_with_a_size_the_catalogue_can_show_sheets_and_cost_per_sheet(self):
        mat = Material.objects.create(
            name="Акрил белый 2,5 мм", unit=Material.Unit.SQM,
            is_roll_material=True, intake_form=Material.IntakeForm.SHEET,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
            quantity=Decimal("36"), purchase_price=Decimal("3000"),
        )
        resp = self.client.get(f"{URL}{mat.id}/")
        self.assertEqual(resp.status_code, 200)
        # 36 ÷ 2,9768 = 12,09 листа — строка «≈12 лист.» в каталоге.
        self.assertEqual(Decimal(resp.data["sheets_remaining"]), Decimal("12.09"))
        # Закупка за лист = 3000 × 2,9768 = 8930,4 — её каталог считает из площади.
        self.assertEqual(Decimal(resp.data["piece_area"]), Decimal("2.9768"))

    def test_without_a_size_there_is_nothing_to_show(self):
        """Как было у «Акрила» до правки: остаток есть, листов нет."""
        mat = Material.objects.create(
            name="Без размера", unit=Material.Unit.SQM,
            is_roll_material=True, intake_form=Material.IntakeForm.SHEET,
            quantity=Decimal("36"),
        )
        resp = self.client.get(f"{URL}{mat.id}/")
        self.assertIsNone(resp.data["sheets_remaining"])

    # ---- соседние формы не задеты -----------------------------------------
    def test_roll_still_requires_its_width_and_not_a_sheet_size(self):
        resp = self.client.post(URL, {
            "name": "Оракал", "unit": Material.Unit.SQM,
            "is_roll_material": True, "intake_form": Material.IntakeForm.ROLL,
        }, format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("roll_width", resp.data)

        ok = self.client.post(URL, {
            "name": "Оракал", "unit": Material.Unit.SQM,
            "is_roll_material": True, "intake_form": Material.IntakeForm.ROLL,
            "roll_width": "1.0",
        }, format="json")
        self.assertEqual(ok.status_code, 201, ok.data)

    def test_piece_material_needs_no_size(self):
        resp = self.client.post(URL, {
            "name": "Люверс", "unit": Material.Unit.PIECE,
            "purchase_price": "330", "price_per_unit": "500",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
