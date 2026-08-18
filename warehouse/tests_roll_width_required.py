"""У формы «Рулон» ширина обязательна, а продажа метрами от неё не зависит.

Раньше `sells_by_metre` требовал три условия сразу, включая `roll_width` из
карточки. Забыл заполнить ширину — и материал МОЛЧА возвращался к четырём
вкладкам и продаже по площади: та же ошибка «1.5 × 1.4 = 2.1 кв.м вместо 1.4
пог.м», от которой уходили, только спрятанная за пустым полем и без единого
предупреждения.

Теперь развилку решает ФОРМА (рулон — метры, точка), а незаполненная ширина —
ошибка ввода при сохранении карточки, а не тихий откат через месяц в чеке.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from warehouse.models import Material, MaterialType


class RollWidthRequiredTests(APITestCase):
    URL = "/api/warehouse/materials/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="rw_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.type = MaterialType.objects.get(code="ACRYL")

    def _roll_payload(self, **over):
        payload = {
            "name": "Туника",
            "type": self.type.id,
            "unit": "SQM",
            "is_roll_material": True,
            "intake_form": "ROLL",
            "roll_width": "0.9",
            "price_per_pm": "300",
        }
        payload.update(over)
        return payload

    # --- Сохранение карточки -------------------------------------------------

    def test_roll_without_width_is_rejected_on_create(self):
        """Касса шлёт стёртое поле нулём — ноль у рулона такая же пустота."""
        for empty in ("0", None):
            res = self.client.post(self.URL, self._roll_payload(roll_width=empty), format="json")
            self.assertEqual(res.status_code, 400, (empty, res.data))
            self.assertIn("roll_width", res.data)
        self.assertFalse(Material.objects.filter(name="Туника").exists())

    def test_roll_with_width_is_saved_and_sells_by_metre(self):
        res = self.client.post(self.URL, self._roll_payload(), format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data["sells_by_metre"])
        self.assertEqual(Decimal(res.data["roll_width"]), Decimal("0.900"))

    def test_width_cannot_be_erased_from_an_existing_roll(self):
        """Правка карточки: стёрли ширину — 400, а не откат к продаже листом."""
        res = self.client.post(self.URL, self._roll_payload(), format="json")
        material_id = res.data["id"]
        res = self.client.put(
            f"{self.URL}{material_id}/", self._roll_payload(roll_width="0"), format="json"
        )
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("roll_width", res.data)
        m = Material.objects.get(pk=material_id)
        self.assertEqual(m.roll_width, Decimal("0.900"))
        self.assertTrue(m.sells_by_metre)

    def test_partial_update_keeps_the_stored_width(self):
        """PATCH только цены не должен требовать ширину заново: она у записи есть."""
        res = self.client.post(self.URL, self._roll_payload(), format="json")
        material_id = res.data["id"]
        res = self.client.patch(
            f"{self.URL}{material_id}/", {"price_per_pm": "350"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(Decimal(res.data["price_per_pm"]), Decimal("350"))

    def test_sheet_form_does_not_need_a_roll_width(self):
        """Лист шириной рулона не описывается — правило только для рулона."""
        res = self.client.post(
            self.URL,
            self._roll_payload(name="Акрил 3мм", intake_form="SHEET", roll_width="0",
                               sheet_width="1.22", sheet_height="2.44"),
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertFalse(res.data["sells_by_metre"])

    # --- Развилка в кассе ------------------------------------------------------

    def test_sells_by_metre_follows_the_form_not_the_width(self):
        """Рулон без ширины в карточке (старая запись) всё равно продаётся
        метрами: молчаливого отката к площади больше нет — метры считаются по
        ширине каждой партии, а карточку с пустой шириной сохранить уже нельзя."""
        legacy = Material.objects.create(
            name="Оракал", type=self.type, unit=Material.Unit.SQM,
            is_roll_material=True, intake_form=Material.IntakeForm.ROLL,
            roll_width=None, price_per_pm=Decimal("200"),
        )
        self.assertTrue(legacy.sells_by_metre)
        res = self.client.get(f"{self.URL}{legacy.id}/")
        self.assertTrue(res.data["sells_by_metre"])

    def test_piece_material_never_sells_by_metre(self):
        piece = Material.objects.create(
            name="Клей", unit=Material.Unit.PIECE, is_roll_material=False,
            intake_form=Material.IntakeForm.ROLL,
        )
        self.assertFalse(piece.sells_by_metre)
