"""Правка состава чека: ширина, длина, деталей, проходы и станок
(2026-10-10, STAFF-08) — вместо молчаливого «ничего не изменилось».
Износ расходника «на пог.м реза» (PNL-05, часть).
"""
from decimal import Decimal as D

from sales.tests_calc_base import CalcBase, RECEIPTS
from services.models import ServiceRecipe
from warehouse.models import Material

EDIT = "edit-items"


class EditDimsTests(CalcBase):
    def _cut_order(self):
        r = self.co([self.cut(self.acr3, "0.5", "0.5", "2")])
        self.assertEqual(r.status_code, 201, r.data)
        rc = self.receipt(r)
        return rc, rc.items.get(type="SERVICE"), rc.items.get(type="MATERIAL")

    def _edit(self, receipt, *changes):
        self.client.force_authenticate(self.admin)
        return self.client.post(f"{RECEIPTS}{receipt.id}/{EDIT}/", {"items": list(changes)}, format="json")

    def test_unknown_edit_field_is_a_400_not_a_silent_noop(self):
        rc, work, material = self._cut_order()
        out = self._edit(rc, {"id": material.id, "colour": "red"})
        self.assertEqual(out.status_code, 400, out.data)
        self.assertIn("colour", out.data["detail"])

    def test_material_dimensions_recompute_area_stock_and_total(self):
        rc, work, material = self._cut_order()
        before_stock = Material.objects.get(pk=self.acr3.pk).quantity
        out = self._edit(rc, {"id": material.id, "width": "0.6", "length": "0.5"})
        self.assertEqual(out.status_code, 200, out.data)
        material.refresh_from_db()
        self.assertEqual(material.quantity, D("0.300"))                    # 0.6 × 0.5
        self.assertEqual((material.width, material.length), (D("0.600"), D("0.500")))
        self.assertEqual(Material.objects.get(pk=self.acr3.pk).quantity, before_stock - D("0.05"))
        rc.refresh_from_db()
        self.assertEqual(rc.total_price, sum(i.sold_total for i in rc.items.all()))
        self.assertEqual(material.sold_total, D("465"))                    # 0.3 × 1550

    def test_parts_count_scales_the_cut_length(self):
        rc, work, material = self._cut_order()
        out = self._edit(rc, {"id": work.id, "parts_count": 3})
        self.assertEqual(out.status_code, 200, out.data)
        work.refresh_from_db()
        self.assertEqual((work.parts_count, work.quantity), (3, D("6.000")))
        self.assertEqual(work.sold_total, D("390"))                         # 6 пог.м × 65

    def test_passes_multiply_the_per_pass_rate(self):
        r = self.co([{"type": "SERVICE", "service": self.engr.id, "width": "0.2", "length": "0.3"}])
        rc = self.receipt(r)
        line = rc.items.get()
        out = self._edit(rc, {"id": line.id, "passes": 2})
        self.assertEqual(out.status_code, 200, out.data)
        line.refresh_from_db()
        self.assertEqual((line.passes, line.catalog_price), (2, D("6000.00")))
        self.assertEqual(line.sold_total, D("360"))                        # 0.06 × 6000

    def test_machine_switch_takes_the_new_machine_rate(self):
        self.laser.rate_per_pm = D("28")
        self.laser.save()
        rc, work, material = self._cut_order()
        out = self._edit(rc, {"id": work.id, "machine": "LASER"})
        self.assertEqual(out.status_code, 200, out.data)
        work.refresh_from_db()
        self.assertEqual(work.service_id, self.laser.id)
        self.assertEqual(work.catalog_price, D("28.00"))
        self.assertEqual(work.sold_total, D("56"))

    def test_machine_must_be_one_active_cutting_service(self):
        rc, work, material = self._cut_order()
        self.assertEqual(self._edit(rc, {"id": work.id, "machine": "PLASMA"}).status_code, 400)
        self.assertEqual(self._edit(rc, {"id": material.id, "machine": "LASER"}).status_code, 400)

    def test_dimensions_where_there_are_none_are_refused(self):
        r = self.co([{"type": "SERVICE", "service": self.mont.id, "quantity": 1}])
        rc = self.receipt(r)
        self.assertEqual(self._edit(rc, {"id": rc.items.get().id, "width": "1"}).status_code, 400)
        piece = self.co([{"type": "MATERIAL", "material": self.acr3.id, "mode": "PIECE", "quantity": 1}])
        prc = self.receipt(piece)
        self.assertEqual(self._edit(prc, {"id": prc.items.get().id, "width": "1"}).status_code, 400)

    def test_bad_numbers_are_refused(self):
        rc, work, material = self._cut_order()
        for body in ({"width": "0"}, {"width": "abc"}, {"length": "1.2345"}, {"parts_count": 0},
                     {"parts_count": "x"}, {"passes": 99}):
            out = self._edit(rc, {"id": material.id, **body})
            self.assertEqual(out.status_code, 400, (body, out.data))


class RecipePerMetreTests(CalcBase):
    def test_tool_wear_follows_the_cut_length(self):
        bit = Material.objects.create(name="Фреза 3 мм", unit=Material.Unit.PIECE, quantity=D("10"),
                                      price_per_unit=D("900"), purchase_price=D("900"))
        ServiceRecipe.objects.create(service=self.cnc, material=bit, consumption_per_unit=D("0.01"),
                                     consumption_mode=ServiceRecipe.Mode.PER_PM)
        r = self.co([self.cut(self.acr3, "0.5", "0.5", "3")])
        self.assertEqual(r.status_code, 201, r.data)
        bit.refresh_from_db()
        self.assertEqual(bit.quantity, D("10") - D("0.03"))                 # 3 пог.м × 0.01
        work = self.receipt(r).items.get(type="SERVICE")
        self.assertEqual(work.cost_total, D("27.00"))                       # 0.03 × 900 — износ в себестоимость

    def test_per_metre_norm_does_nothing_for_non_cutting_services(self):
        bit = Material.objects.create(name="Трубка", unit=Material.Unit.PIECE, quantity=D("10"),
                                      price_per_unit=D("900"), purchase_price=D("900"))
        ServiceRecipe.objects.create(service=self.engr, material=bit, consumption_per_unit=D("0.5"),
                                     consumption_mode=ServiceRecipe.Mode.PER_PM)
        self.co([{"type": "SERVICE", "service": self.engr.id, "width": "0.2", "length": "0.3"}])
        bit.refresh_from_db()
        self.assertEqual(bit.quantity, D("10"))

    def test_returning_the_order_gives_the_wear_back(self):
        bit = Material.objects.create(name="Фреза", unit=Material.Unit.PIECE, quantity=D("10"),
                                      price_per_unit=D("900"), purchase_price=D("900"))
        ServiceRecipe.objects.create(service=self.cnc, material=bit, consumption_per_unit=D("0.01"),
                                     consumption_mode=ServiceRecipe.Mode.PER_PM)
        r = self.co([self.cut(self.acr3, "0.5", "0.5", "3")])
        self.client.post(f"{RECEIPTS}{r.data['id']}/refund/", {}, format="json")
        bit.refresh_from_db()
        self.assertEqual(bit.quantity, D("10"))
