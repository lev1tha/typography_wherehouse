"""Детали, проходы, толщина, матрица ставок, размеры и «по договорённости»
(2026-10-10, CALC-02, -04, -05, -06, -07, -08, XL-08, XL-09, F8).
"""
from decimal import Decimal as D

from sales.tests_calc_base import CalcBase
from services.models import RateMatrixEntry, ThicknessCoefficient


class PartsCountTests(CalcBase):
    def test_twelve_parts_one_line_rounded_once(self):
        # Excel: 12 деталей 0.33×0.37, рез 0.37 пог.м на деталь.
        item = self.cut(self.forex3, "0.33", "0.37", "0.37", parts_count=12)
        r = self.co([item])
        self.assertEqual(r.status_code, 201, r.data)
        work, material = r.data["items"]
        self.assertEqual(D(str(material["quantity"])), D("1.465"))   # 12 × 0.1221, округление один раз
        self.assertEqual(D(str(work["quantity"])), D("4.44"))        # 12 × 0.37 пог.м
        self.assertEqual(work["parts_count"], 12)
        self.assertEqual(D(str(work["width"])), D("0.33"))           # размеры ОДНОЙ детали
        self.assertEqual(D(str(work["price_per_item"])), D("35"))
        self.assertEqual(D(str(work["line_total"])), D("156"))       # 155.4 вверх
        self.assertEqual(D(str(material["line_total"])), D("733"))   # 732.5 вверх

    def test_quantity_with_dimensions_is_rejected_not_ignored(self):
        item = self.cut(self.forex3, "0.33", "0.37", "0.37", quantity=12)
        r = self.co([item])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("parts_count", str(r.data))

    def test_parts_count_without_dimensions_is_rejected(self):
        r = self.co([{"type": "SERVICE", "service": self.cnc.id, "material": self.forex3.id,
                      "running_meters": "1", "parts_count": 3}])
        self.assertEqual(r.status_code, 400, r.data)

    def test_parts_count_on_a_material_line_is_rejected(self):
        r = self.co([{"type": "MATERIAL", "material": self.forex3.id, "mode": "SQM",
                      "quantity": "1", "parts_count": 2}])
        self.assertEqual(r.status_code, 400, r.data)

    def test_engraving_parts_multiply_the_area(self):
        r = self.co([{"type": "SERVICE", "service": self.engr.id, "width": "0.2", "length": "0.3",
                      "parts_count": 4}])
        self.assertEqual(r.status_code, 201, r.data)
        line = r.data["items"][0]
        self.assertEqual(D(str(line["quantity"])), D("0.240"))       # 4 × 0.06 кв.м
        self.assertEqual(D(str(line["line_total"])), D("720"))

    def test_recipe_per_sqm_uses_all_parts(self):
        from services.models import ServiceRecipe
        from warehouse.models import Material

        glue = Material.objects.create(name="Клей", unit=Material.Unit.PIECE, quantity=D("100"),
                                       price_per_unit=D("10"), purchase_price=D("4"))
        ServiceRecipe.objects.create(service=self.cnc, material=glue, consumption_per_unit=D("2"),
                                     consumption_mode=ServiceRecipe.Mode.PER_SQM)
        r = self.co([self.cut(self.forex3, "0.33", "0.37", "0.37", parts_count=12)])
        self.assertEqual(r.status_code, 201, r.data)
        glue.refresh_from_db()
        self.assertEqual(glue.quantity, D("100") - D("2") * D("1.465"))


class ThreeDecimalSizesTests(CalcBase):
    def test_three_decimals_are_accepted_and_kept(self):
        r = self.co([self.cut(self.acr3, "0.455", "1.2", "1.2")])
        self.assertEqual(r.status_code, 201, r.data)
        work, material = r.data["items"]
        self.assertEqual(D(str(work["width"])), D("0.455"))
        self.assertEqual(D(str(material["quantity"])), D("0.546"))

    def test_four_decimals_are_rejected(self):
        r = self.co([self.cut(self.acr3, "0.4551", "1.2", "1.2")])
        self.assertEqual(r.status_code, 400, r.data)


class SizeWarningsTests(CalcBase):
    def test_part_bigger_than_the_sheet_needs_confirmation(self):
        item = self.cut(self.acr3, "3.0", "1.0", "4")   # лист 1.22×2.44
        r = self.co([item])
        self.assertEqual(r.status_code, 409, r.data)
        self.assertTrue(r.data["needs_confirmation"])
        self.assertEqual(r.data["warnings"][0]["code"], "size_exceeds_sheet")
        from sales.models import Receipt
        self.assertEqual(Receipt.objects.count(), 0)   # ничего не создано
        again = self.co([item], confirmed_warnings=["size_exceeds_sheet"])
        self.assertEqual(again.status_code, 201, again.data)

    def test_rotated_part_that_fits_is_fine(self):
        r = self.co([self.cut(self.acr3, "2.4", "1.2", "4")])
        self.assertEqual(r.status_code, 201, r.data)

    def test_centimetres_instead_of_metres_cost_a_confirmation(self):
        # 450 × 1230 «метров» — чек на сотни миллионов.
        r = self.co([{"type": "SERVICE", "service": self.engr.id, "width": "450", "length": "1230"}])
        self.assertEqual(r.status_code, 409, r.data)
        self.assertEqual({w["code"] for w in r.data["warnings"]}, {"line_total_high"})

    def test_threshold_is_a_setting(self):
        item = {"type": "MATERIAL", "material": self.acr3.id, "mode": "PIECE", "quantity": 2}
        self.assertEqual(self.co([item]).status_code, 201)             # ~9 200 < 100 000
        self.settings_(confirm_line_total=5000)
        self.assertEqual(self.co([item]).status_code, 409)
        self.settings_(confirm_line_total=0)                            # 0 — не спрашивать
        self.assertEqual(self.co([item]).status_code, 201)

    def test_add_items_asks_for_confirmation_too(self):
        r = self.co([{"type": "MATERIAL", "material": self.forex3.id, "mode": "SQM", "quantity": "1"}],
                    client_id=self.ivan.id, pay_full=False, amount_paid="0")
        url = f"/api/sales/receipts/{r.data['id']}/add-items/"
        body = {"items": [self.cut(self.acr3, "3.0", "1.0", "4")]}
        first = self.client.post(url, body, format="json")
        self.assertEqual(first.status_code, 409, first.data)
        self.assertEqual(first.data["warnings"][0]["code"], "size_exceeds_sheet")
        before = self.receipt(r).items.count()
        self.assertEqual(before, 1)                                    # ничего не добавилось
        again = self.client.post(url, {**body, "confirmed_warnings": ["size_exceeds_sheet"]}, format="json")
        self.assertEqual(again.status_code, 200, again.data)
        self.assertEqual(self.receipt(r).items.count(), 3)

    def test_storekeeper_hard_cap(self):
        self.settings_(staff_line_cap=5000, confirm_line_total=0)
        item = {"type": "MATERIAL", "material": self.acr3.id, "mode": "PIECE", "quantity": 2}
        r = self.co([item], user=self.store)
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("потолк", r.data["detail"])
        self.assertEqual(self.co([item], user=self.admin).status_code, 201)


class PassesAndThicknessTests(CalcBase):
    def test_engraving_passes_multiply_the_rate(self):
        item = {"type": "SERVICE", "service": self.engr.id, "width": "0.2", "length": "0.3", "passes": 3}
        r = self.co([item])
        self.assertEqual(r.status_code, 201, r.data)
        line = r.data["items"][0]
        self.assertEqual(line["passes"], 3)
        self.assertEqual(D(str(line["price_per_item"])), D("9000"))
        self.assertEqual(D(str(line["line_total"])), D("540"))         # Excel: 0.06 × 3 × 3000

    def test_passes_on_a_material_are_rejected(self):
        r = self.co([{"type": "MATERIAL", "material": self.forex3.id, "mode": "SQM", "quantity": "1",
                      "passes": 2}])
        self.assertEqual(r.status_code, 400, r.data)

    def test_thickness_coefficient_scales_the_catalogue_rate(self):
        # Коэффициент — к ставке СТАНКА; ставка материала своя для каждой
        # толщины, и на неё он не ложится (RP-N5, D-180: `tests_s3_price`).
        self.cnc.rate_per_pm = D("45")
        self.cnc.save()
        ThicknessCoefficient.objects.create(kind="CUTTING", thickness_from=D("5"), coefficient=D("1.35"))
        thick = self.co([self.cut(self.forex8, "0.5", "0.5", "1")])
        self.cnc.rate_per_pm = D("35")
        self.cnc.save()
        thin = self.co([self.cut(self.forex3, "0.5", "0.5", "1")])
        work_thick = thick.data["items"][0]
        self.assertEqual(D(str(work_thick["price_per_item"])), D("60.75"))   # 45 × 1.35
        self.assertEqual(D(str(work_thick["thickness_coef"])), D("1.350"))
        self.assertEqual(D(str(thin.data["items"][0]["price_per_item"])), D("35"))
        self.assertIsNone(thin.data["items"][0]["thickness_coef"])

    def test_thickness_table_is_a_step_function(self):
        self.cnc.rate_per_pm = D("35")
        self.cnc.save()
        ThicknessCoefficient.objects.create(kind="CUTTING", thickness_from=D("3"), coefficient=D("1.1"))
        ThicknessCoefficient.objects.create(kind="CUTTING", thickness_from=D("6"), coefficient=D("1.5"))
        r3 = self.co([self.cut(self.forex3, "0.5", "0.5", "1")])
        r8 = self.co([self.cut(self.forex8, "0.5", "0.5", "1")])
        self.assertEqual(D(str(r3.data["items"][0]["thickness_coef"])), D("1.100"))
        self.assertEqual(D(str(r8.data["items"][0]["thickness_coef"])), D("1.500"))

    def test_manual_rate_is_not_scaled_by_thickness_but_passes_apply(self):
        ThicknessCoefficient.objects.create(kind="CUTTING", thickness_from=D("5"), coefficient=D("2"))
        r = self.co([self.cut(self.forex8, "0.5", "0.5", "1", cut_rate="50", passes=2)])
        self.assertEqual(D(str(r.data["items"][0]["price_per_item"])), D("100"))


class RateMatrixTests(CalcBase):
    def _work_price(self, svc, mat):
        r = self.co([self.cut(mat, "0.1", "1.0", "1.0", svc=svc)])
        self.assertEqual(r.status_code, 201, r.data)
        return D(str(r.data["items"][0]["price_per_item"]))

    def test_without_matrix_the_old_chain_is_unchanged(self):
        self.assertEqual(self._work_price(self.cnc, self.forex3), D("35"))
        self.assertEqual(self._work_price(self.cnc, self.forex8), D("45"))
        self.laser.rate_per_pm = D("28")
        self.laser.save()
        self.assertEqual(self._work_price(self.laser, self.forex3), D("28"))

    def test_material_matrix_beats_machine_and_material_rate(self):
        self.laser.rate_per_pm = D("30")
        self.laser.save()
        RateMatrixEntry.objects.create(service=self.laser, material=self.forex3, rate=D("28"))
        self.assertEqual(self._work_price(self.laser, self.forex3), D("28"))
        self.assertEqual(self._work_price(self.laser, self.forex8), D("30"))   # без строки матрицы

    def test_thickness_matrix_row(self):
        RateMatrixEntry.objects.create(service=self.cnc, thickness_from=D("6"), rate=D("50"))
        self.assertEqual(self._work_price(self.cnc, self.forex8), D("50"))
        self.assertEqual(self._work_price(self.cnc, self.forex3), D("35"))

    def test_material_row_beats_thickness_row(self):
        RateMatrixEntry.objects.create(service=self.cnc, thickness_from=D("6"), rate=D("50"))
        RateMatrixEntry.objects.create(service=self.cnc, material=self.forex8, rate=D("55"))
        self.assertEqual(self._work_price(self.cnc, self.forex8), D("55"))

    def test_matrix_rate_is_not_scaled_by_thickness_coefficient(self):
        ThicknessCoefficient.objects.create(kind="CUTTING", thickness_from=D("5"), coefficient=D("2"))
        RateMatrixEntry.objects.create(service=self.cnc, material=self.forex8, rate=D("50"))
        self.assertEqual(self._work_price(self.cnc, self.forex8), D("50"))

    def test_matrix_makes_a_rateless_machine_sellable(self):
        # Ни у станка, ни у материала ставки нет — раньше 400, теперь ставку даёт матрица.
        from warehouse.models import Material
        m = Material.objects.create(name="ПВХ", unit=Material.Unit.SQM, is_roll_material=True,
                                    thickness_mm=D("5"), sheet_width=D("1"), sheet_height=D("2"),
                                    price_per_sqm=D("100"), piece_price=D("200"))
        no = self.co([self.cut(m, "0.1", "0.1", "1")])
        self.assertEqual(no.status_code, 400, no.data)
        RateMatrixEntry.objects.create(service=self.cnc, material=m, rate=D("40"))
        # ...а материалу нужны партии, чтобы продаться — проверим расчёт предпросмотром без склада.
        resolved = self.client.get("/api/services/rate/", {"service": self.cnc.id, "material": m.id})
        self.assertEqual(D(str(resolved.data["rate"])), D("40"))
        self.assertEqual(resolved.data["source"], "matrix_material")


class NegotiablePriceTests(CalcBase):
    def test_admin_names_the_price_of_a_fixed_service(self):
        r = self.co([{"type": "SERVICE", "service": self.mont.id, "quantity": 1, "cut_rate": "3800"}])
        self.assertEqual(r.status_code, 201, r.data)
        line = r.data["items"][0]
        self.assertEqual(D(str(line["price_per_item"])), D("3800"))
        self.assertTrue(line["price_is_manual"])

    def test_admin_names_the_price_per_letter(self):
        r = self.co([{"type": "SERVICE", "service": self.letters.id, "quantity": 14, "cut_rate": "220"}])
        self.assertEqual(D(str(r.data["total_price"])), D("3080"))     # Excel: 14 × 220

    def test_without_the_flag_a_storekeeper_cannot_name_the_price(self):
        r = self.co([{"type": "SERVICE", "service": self.mont.id, "quantity": 1, "cut_rate": "3800"}],
                    user=self.store)
        self.assertEqual(r.status_code, 403, r.data)

    def test_with_the_flag_a_storekeeper_can_but_not_for_free(self):
        self.mont.negotiable_price = True
        self.mont.save()
        ok = self.co([{"type": "SERVICE", "service": self.mont.id, "quantity": 1, "cut_rate": "3800"}],
                     user=self.store)
        self.assertEqual(ok.status_code, 201, ok.data)
        gift = self.co([{"type": "SERVICE", "service": self.mont.id, "quantity": 1, "cut_rate": "0"}],
                       user=self.store)
        self.assertEqual(gift.status_code, 403, gift.data)

    def test_material_price_on_a_fixed_service_is_rejected(self):
        r = self.co([{"type": "SERVICE", "service": self.mont.id, "quantity": 1, "material_price": "3800"}])
        self.assertEqual(r.status_code, 400, r.data)

    def test_catalogue_price_is_still_used_when_nothing_is_named(self):
        r = self.co([{"type": "SERVICE", "service": self.mont.id, "quantity": 1}], user=self.store)
        self.assertEqual(D(str(r.data["total_price"])), D("2854"))

    def test_dimensions_on_a_fixed_service_are_rejected(self):
        r = self.co([{"type": "SERVICE", "service": self.mont.id, "quantity": 1,
                      "width": "1", "length": "2"}])
        self.assertEqual(r.status_code, 400, r.data)


class StaffPriceFloorTests(CalcBase):
    def _engr(self, rate):
        return [{"type": "SERVICE", "service": self.engr.id, "width": "0.2", "length": "0.3",
                 "cut_rate": str(rate)}]

    def test_floor_is_off_by_default(self):
        self.assertEqual(self.co(self._engr("0.01"), user=self.store).status_code, 201)

    def test_storekeeper_cannot_go_below_the_floor(self):
        self.settings_(staff_min_price_percent=50)
        low = self.co(self._engr("1000"), user=self.store)
        self.assertEqual(low.status_code, 403, low.data)
        self.assertIn("1500", low.data["detail"])
        self.assertEqual(self.co(self._engr("1500"), user=self.store).status_code, 201)

    def test_admin_is_not_bound_by_the_floor(self):
        self.settings_(staff_min_price_percent=50)
        self.assertEqual(self.co(self._engr("1000"), user=self.admin).status_code, 201)

    def test_own_material_has_no_catalogue_to_compare_with(self):
        self.settings_(staff_min_price_percent=50)
        item = {"type": "SERVICE", "service": self.cnc.id, "own_material": True,
                "running_meters": "2", "cut_rate": "10"}
        self.assertEqual(self.co([item], user=self.store).status_code, 201)
