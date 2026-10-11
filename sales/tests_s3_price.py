"""S3 перепроверки владельца: прайс (RP-N5, RP-N6, CALC-08).

- RP-N5: коэффициент толщины ложится только на ставку услуги/станка, а не на
  ставку материала, которая сама уже зависит от толщины;
- RP-N6: ручная цена — окончательная, скидка клиента к ней не применяется;
- CALC-08: складовщик вписал цену работы ниже доли каталога — предупреждение.
"""
from decimal import Decimal as D

from sales.models import TransactionItem
from sales.tests_calc_base import CalcBase
from services.models import PricingSettings, RateMatrixEntry, ThicknessCoefficient


class ThicknessOnMaterialRateTests(CalcBase):
    """RP-N5: LASER без своей ставки режет форекс 8 мм по ставке материала 45."""

    def setUp(self):
        super().setUp()
        ThicknessCoefficient.objects.create(kind="CUTTING", thickness_from=D("6"), coefficient=D("1.35"))

    def _work(self, svc, mat):
        r = self.preview([self.cut(mat, "0.1", "1.0", "1.0", svc=svc)])
        self.assertEqual(r.status_code, 200, r.data)
        return next(i for i in r.data["items"] if i["type"] == "SERVICE")

    def test_material_rate_is_not_scaled_again(self):
        work = self._work(self.laser, self.forex8)
        self.assertEqual(D(str(work["price_per_item"])), D("45"))       # а не 60,75
        self.assertIsNone(work["thickness_coef"])

    def test_machine_rate_still_gets_the_coefficient(self):
        self.cnc.rate_per_pm = D("35")
        self.cnc.save()
        work = self._work(self.cnc, self.forex8)
        self.assertEqual(D(str(work["price_per_item"])), D("47.25"))    # 35 × 1,35
        self.assertEqual(D(str(work["thickness_coef"])), D("1.350"))

    def test_thickness_matrix_is_not_scaled(self):
        RateMatrixEntry.objects.create(service=self.cnc, thickness_from=D("8"), rate=D("50"))
        work = self._work(self.cnc, self.forex8)
        self.assertEqual(D(str(work["price_per_item"])), D("50"))
        self.assertIsNone(work["thickness_coef"])

    def test_rate_endpoint_says_the_same(self):
        self.client.force_authenticate(self.admin)
        out = self.client.get("/api/services/rate/", {"service": self.laser.id, "material": self.forex8.id})
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(D(str(out.data["rate"])), D("45"))
        self.assertEqual(out.data["source"], "material")
        self.assertIsNone(out.data["coefficient"])

    def test_engraving_flat_rate_still_gets_its_coefficient(self):
        ThicknessCoefficient.objects.create(kind="ENGRAVING", thickness_from=D("6"), coefficient=D("1.2"))
        r = self.preview([{"type": "SERVICE", "service": self.engr.id, "material": self.forex8.id,
                           "width": "0.1", "length": "0.1"}])
        self.assertEqual(r.status_code, 200, r.data)
        work = next(i for i in r.data["items"] if i["type"] == "SERVICE")
        self.assertEqual(D(str(work["price_per_item"])), D("3600"))     # 3000 × 1,2


class ManualPriceIsFinalTests(CalcBase):
    """RP-N6: ручная цена админа 3 800 клиенту со скидкой 5 % — 3 800, как договорная."""

    def mount(self, **kw):
        return {"type": "SERVICE", "service": self.mont.id, "quantity": 1, **kw}

    def test_manual_price_gets_no_client_discount(self):
        r = self.co([self.mount(cut_rate="3800")], client_id=self.regular.id)
        self.assertEqual(r.status_code, 201, r.data)
        line = r.data["items"][0]
        self.assertEqual(D(str(r.data["total_price"])), D("3800"))
        self.assertTrue(line["price_is_manual"])
        self.assertEqual(D(str(line["discount_percent"])), D("0"))

    def test_catalogue_line_of_the_same_order_keeps_the_discount(self):
        r = self.co([self.mount(cut_rate="3800"), self.mount()], client_id=self.regular.id)
        self.assertEqual(r.status_code, 201, r.data)
        manual, catalogue = r.data["items"]
        self.assertEqual(D(str(manual["line_total"])), D("3800"))
        self.assertEqual(D(str(catalogue["line_total"])), D("2712"))      # ⌈2854 × 0,95⌉

    def test_urgency_and_minimum_still_apply(self):
        self.settings_(urgency_percent=25, min_line_amount=500)
        r = self.co([self.mount(cut_rate="3800")], client_id=self.regular.id, is_urgent=True)
        self.assertEqual(D(str(r.data["total_price"])), D("4750"))       # 3800 × 1,25, без скидки
        r = self.co([self.mount(cut_rate="100")], client_id=self.regular.id)
        self.assertEqual(D(str(r.data["total_price"])), D("500"))        # минимум строки

    def test_manual_material_price_is_final_too(self):
        r = self.co([{"type": "MATERIAL", "material": self.forex3.id, "mode": "SQM", "quantity": "1",
                      "material_price": "600"}], client_id=self.regular.id)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(str(r.data["total_price"])), D("600"))

    def test_edit_keeps_the_line_without_discount(self):
        r = self.co([self.mount(cut_rate="3800", quantity=2)], client_id=self.regular.id,
                    pay_full=False, amount_paid="0")
        line = r.data["items"][0]
        self.client.force_authenticate(self.admin)
        out = self.client.post(f"/api/sales/receipts/{r.data['id']}/edit-items/",
                               {"items": [{"id": line["id"], "quantity": 1}]}, format="json")
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(D(str(out.data["total_price"])), D("3800"))
        self.assertEqual(TransactionItem.objects.get(pk=line["id"]).discount_percent, D("0"))


class StaffLowPriceWarningTests(CalcBase):
    """CALC-08: складовщик вписал работу ниже 50 % каталога — предупреждение, не запрет."""

    def setUp(self):
        super().setUp()
        self.mont.negotiable_price = True
        self.mont.save()

    def mount(self, rate):
        return {"type": "SERVICE", "service": self.mont.id, "quantity": 1, "cut_rate": str(rate)}

    def codes(self, response):
        return [w["code"] for w in response.data.get("warnings") or []]

    def test_default_is_fifty_percent(self):
        self.assertEqual(PricingSettings.load().staff_price_warn_percent, D("50"))

    def test_storekeeper_below_half_gets_a_warning_in_checkout_and_preview(self):
        pv = self.preview([self.mount(1000)], user=self.store)
        self.assertEqual(pv.status_code, 200, pv.data)
        self.assertIn("low_manual_price", self.codes(pv))
        r = self.co([self.mount(1000)], user=self.store)
        self.assertEqual(r.status_code, 201, r.data)                        # не запрет
        warning = next(w for w in r.data["warnings"] if w["code"] == "low_manual_price")
        self.assertEqual(D(str(warning["catalog_price"])), D("2854"))
        self.assertEqual(D(str(warning["price"])), D("1000"))
        self.assertIn("Монтаж", warning["message"])

    def test_above_half_and_admin_are_quiet(self):
        self.assertNotIn("low_manual_price", self.codes(self.co([self.mount(1500)], user=self.store)))
        self.assertNotIn("low_manual_price", self.codes(self.co([self.mount(1000)])))

    def test_zero_turns_it_off(self):
        s = PricingSettings.load()
        s.staff_price_warn_percent = D("0")
        s.save()
        self.assertNotIn("low_manual_price", self.codes(self.co([self.mount(1000)], user=self.store)))

    def test_engraving_rate_per_sqm(self):
        r = self.co([{"type": "SERVICE", "service": self.engr.id, "width": "0.5", "length": "0.5",
                      "cut_rate": "1000"}], user=self.store)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIn("low_manual_price", self.codes(r))

    def test_setting_is_editable_and_bounded(self):
        self.client.force_authenticate(self.admin)
        ok = self.client.patch("/api/services/settings/", {"staff_price_warn_percent": "30"}, format="json")
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertEqual(PricingSettings.load().staff_price_warn_percent, D("30"))
        bad = self.client.patch("/api/services/settings/", {"staff_price_warn_percent": "120"}, format="json")
        self.assertEqual(bad.status_code, 400, bad.data)
