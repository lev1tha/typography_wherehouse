"""Поступление: приём КВАДРАТАМИ и производство у ПАРТИИ.

Два запроса владельца (27.08).

**Квадратами.** Приём умел только «ширина × высота × кол-во листов + цена за
лист». Но поставщик выставляет счёт и в квадратах — так приходит обрез и
остатки: «45,3 кв.м по 700». Пересчитывать это в листы, чтобы ввести, значит
считать за систему то, что она посчитает сама, — и ошибиться в округлении.

**Производство партии.** У материала производство было, у партии — нет, и его
писали словом в маркировку («бишкек»). Свободный текст: ни отфильтровать, ни
свести. При этом партии одного акрила приходят из разных мест и стоят
по-разному — ровно то, ради чего партии и заведены.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from warehouse.models import Material, ProductionSite, Roll

URL = "/api/warehouse/materials/receive-roll/"


class IntakeByAreaTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="iba_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.mat = Material.objects.create(
            name="Акрил серебро", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET,
            sheet_width=Decimal("1.2"), sheet_height=Decimal("1.8"),
        )

    def _receive(self, **extra):
        body = {"material": self.mat.id, "form": "SHEET"}
        body.update(extra)
        return self.client.post(URL, body, format="json")

    def test_area_with_price_per_sqm_makes_a_lot(self):
        resp = self._receive(area="45.3", cost_per_sqm="700")
        self.assertEqual(resp.status_code, 201, resp.data)
        lot = Roll.objects.get()
        self.assertEqual(lot.initial_area, Decimal("45.3000"))
        self.assertEqual(lot.remaining_area, Decimal("45.3000"))
        # 45,3 × 700 = 31 710 — ровно то, что стоит в счёте.
        self.assertEqual(lot.purchase_cost, Decimal("31710.00"))
        self.assertEqual(lot.cost_per_sqm, Decimal("700.00"))

    def test_area_intake_asks_no_dimensions(self):
        """Размеров в таком счёте нет — и выдумывать их не заставляем."""
        self.assertEqual(self._receive(area="10", cost_per_sqm="500").status_code, 201)
        lot = Roll.objects.get()
        self.assertIsNone(lot.width)
        self.assertIsNone(lot.height)
        self.assertIsNone(lot.sheet_count)

    def test_lot_without_dimensions_is_labelled_by_its_area(self):
        """«Лист ?×?» врал бы вопросительными знаками там, где всё известно."""
        self._receive(area="10", cost_per_sqm="500")
        self.assertEqual(Roll.objects.get().dimensions_label, "10 кв.м")

    def test_area_lot_adds_to_material_stock(self):
        self._receive(area="45.3", cost_per_sqm="700")
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.quantity, Decimal("45.3000"))
        self.assertEqual(self.mat.purchase_price, Decimal("700.00"))

    def test_full_cost_may_be_given_instead_of_the_per_sqm_price(self):
        resp = self._receive(area="45.3", purchase_cost="31710")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Roll.objects.get().cost_per_sqm, Decimal("700.00"))

    def test_area_without_any_price_is_rejected(self):
        resp = self._receive(area="45.3")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(Roll.objects.count(), 0)

    def test_zero_area_is_rejected(self):
        self.assertEqual(self._receive(area="0", cost_per_sqm="700").status_code, 400)

    def test_the_usual_sheet_intake_still_works(self):
        """Приём квадратами не должен был сломать приём листами."""
        resp = self._receive(
            width="1.2", height="1.8", sheet_count="5", purchase_cost="10800",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        lot = Roll.objects.get()
        self.assertEqual(lot.initial_area, Decimal("10.8000"))
        self.assertEqual(lot.dimensions_label, "Лист 1.2×1.8 ×5")

    def test_sheet_intake_without_dimensions_and_without_area_is_rejected(self):
        self.assertEqual(self._receive(purchase_cost="1000").status_code, 400)


class LotProductionTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="lp_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.bishkek = ProductionSite.objects.create(code="bishkek-t", name="Бишкек")
        self.china = ProductionSite.objects.create(code="china-t", name="Китай")
        self.mat = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET, production=self.bishkek,
        )

    def _receive(self, **extra):
        body = {"material": self.mat.id, "form": "SHEET", "area": "10",
                "cost_per_sqm": "500"}
        body.update(extra)
        return self.client.post(URL, body, format="json")

    def test_production_is_taken_from_the_card_when_not_given(self):
        """Обычный случай: возят оттуда же, спрашивать незачем."""
        self.assertEqual(self._receive().status_code, 201)
        self.assertEqual(Roll.objects.get().production, self.bishkek)

    def test_production_can_differ_from_the_card(self):
        """Ради чего всё: эта партия приехала из Китая, хотя обычно из Бишкека."""
        self.assertEqual(self._receive(production=self.china.id).status_code, 201)
        self.assertEqual(Roll.objects.get().production, self.china)
        self.mat.refresh_from_db()
        self.assertEqual(self.mat.production, self.bishkek, "приход переписал карточку")

    def test_two_lots_keep_their_own_production(self):
        self._receive(production=self.bishkek.id, cost_per_sqm="500")
        self._receive(production=self.china.id, cost_per_sqm="700")
        got = {r.production.name: r.cost_per_sqm for r in Roll.objects.all()}
        self.assertEqual(got, {"Бишкек": Decimal("500.00"), "Китай": Decimal("700.00")})

    def test_production_may_be_empty(self):
        """У материала производства нет — и у партии не выдумываем."""
        self.mat.production = None
        self.mat.save(update_fields=["production"])
        self.assertEqual(self._receive().status_code, 201)
        self.assertIsNone(Roll.objects.get().production)

    def test_api_shows_the_production_name(self):
        """Подпись партии в кассе читает человек, ему нужно название."""
        self._receive(production=self.china.id)
        lot = Roll.objects.get()
        resp = self.client.get(f"/api/warehouse/rolls/{lot.id}/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["production"], self.china.id)
        self.assertEqual(resp.data["production_name"], "Китай")

    def test_production_name_is_null_when_there_is_none(self):
        self.mat.production = None
        self.mat.save(update_fields=["production"])
        self._receive()
        lot = Roll.objects.get()
        resp = self.client.get(f"/api/warehouse/rolls/{lot.id}/")
        self.assertIsNone(resp.data["production_name"])


class ProductionSiteDeleteTests(APITestCase):
    """Производство, на которое ссылаются ПАРТИИ, удалять нельзя — прячем.

    Регрессия, внесённая вместе с `Roll.production`: удаление проверяло только
    материалы (`site.materials.exists()`), а ссылка с партии стоит на `PROTECT`.
    Производство без материалов, но с историей приходов отдавало пятисотку —
    и как раз такое встречается: материал перевели на другое производство, а
    принятые партии остались за старым.
    """

    def setUp(self):
        self.admin = User.objects.create_user(
            username="psd_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.site = ProductionSite.objects.create(code="china-d", name="Китай")
        self.mat = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET,
        )

    def _url(self):
        return f"/api/warehouse/production-sites/{self.site.id}/"

    def test_site_with_lots_but_no_materials_is_archived_not_deleted(self):
        self.client.post(URL, {
            "material": self.mat.id, "form": "SHEET",
            "area": "10", "cost_per_sqm": "500", "production": self.site.id,
        }, format="json")
        # Материал за производством НЕ числится — ссылается только партия.
        self.assertFalse(self.site.materials.exists())
        self.assertTrue(self.site.rolls.exists())

        resp = self.client.delete(self._url())
        self.assertEqual(resp.status_code, 200, resp.data)
        self.site.refresh_from_db()
        self.assertTrue(self.site.is_archived, "производство удалили вместо архивации")

    def test_unused_site_is_still_deleted(self):
        """Проверка не должна была запретить удаление лишней строки справочника."""
        resp = self.client.delete(self._url())
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ProductionSite.objects.filter(pk=self.site.pk).exists())

    def test_site_used_only_by_a_material_is_archived_as_before(self):
        self.mat.production = self.site
        self.mat.save(update_fields=["production"])
        resp = self.client.delete(self._url())
        self.assertEqual(resp.status_code, 200, resp.data)
        self.site.refresh_from_db()
        self.assertTrue(self.site.is_archived)


class IntakeByAreaForRollTests(APITestCase):
    """Площадь у РУЛОНА обязана превратиться в метры, а не лечь «голой».

    Остаток рулона в метрах считается делением площади на ширину ПАРТИИ
    (`Roll.metres_remaining`), и партия без ширины возвращает `None`: площадь на
    складе числится, а продать её метрами нечем — тихое расхождение склада.

    Поэтому «площадью» у рулона — это способ ввода: система сама делит площадь
    на ширину из карточки. Интерфейс так и делает, но ручка держится и без него.
    """

    def setUp(self):
        self.admin = User.objects.create_user(
            username="iar_admin", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.admin)
        self.roll_mat = Material.objects.create(
            name="Оракал", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL, roll_width=Decimal("1.2"),
        )

    def _receive(self, **extra):
        body = {"material": self.roll_mat.id, "form": "ROLL"}
        body.update(extra)
        return self.client.post(URL, body, format="json")

    def test_area_becomes_width_and_length(self):
        resp = self._receive(area="60", cost_per_sqm="150")
        self.assertEqual(resp.status_code, 201, resp.data)
        lot = Roll.objects.get()
        self.assertEqual(lot.width, Decimal("1.20"))
        # 60 ÷ 1,2 = 50 пог.м
        self.assertEqual(lot.length, Decimal("50.00"))
        self.assertEqual(lot.initial_area, Decimal("60.0000"))

    def test_such_a_lot_knows_its_metres(self):
        """Ради чего проверка: рулон должен продаваться метрами."""
        self._receive(area="60", cost_per_sqm="150")
        lot = Roll.objects.get()
        self.assertEqual(lot.metres_initial, Decimal("50.00"))
        self.assertEqual(lot.metres_remaining, Decimal("50.00"))
        self.roll_mat.refresh_from_db()
        self.assertEqual(self.roll_mat.metres_remaining, Decimal("50.00"))

    def test_area_without_any_width_is_rejected(self):
        """Ширины нет ни в запросе, ни в карточке — принимать нельзя."""
        self.roll_mat.roll_width = None
        self.roll_mat.save(update_fields=["roll_width"])
        resp = self._receive(area="60", cost_per_sqm="150")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("width", resp.data)
        self.assertEqual(Roll.objects.count(), 0)

    def test_sheet_by_area_still_needs_no_width(self):
        """У ЛИСТА площадь ложится как есть — метров у него не бывает."""
        sheet = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.SHEET,
        )
        resp = self.client.post(URL, {
            "material": sheet.id, "form": "SHEET",
            "area": "45.3", "cost_per_sqm": "700",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        lot = Roll.objects.get(material=sheet)
        self.assertIsNone(lot.width)
        self.assertEqual(lot.initial_area, Decimal("45.3000"))
