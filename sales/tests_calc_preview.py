"""Предпросмотр корзины с маржой, «цена изменилась» в дозаказе и пересчёт
заказа по текущему прайсу (2026-10-10, CALC-08, G4-N3).
"""
from decimal import Decimal as D

from audit.models import AuditLog
from finance.models import CashEntry
from sales.models import Receipt, TransactionItem
from sales.tests_calc_base import CalcBase, RECEIPTS
from warehouse.models import InventoryLog, Material


class PreviewTests(CalcBase):
    def _cart(self):
        return [self.cut(self.acr3, "1", "1", "4")]

    def test_preview_saves_nothing(self):
        before_qty = Material.objects.get(pk=self.acr3.pk).quantity
        logs = InventoryLog.objects.count()
        audits = AuditLog.objects.count()
        r = self.preview(self._cart())
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data["dry_run"])
        self.assertEqual(Receipt.objects.count(), 0)
        self.assertEqual(TransactionItem.objects.count(), 0)
        self.assertEqual(CashEntry.objects.count(), 0)
        self.assertEqual(InventoryLog.objects.count(), logs)
        self.assertEqual(AuditLog.objects.count(), audits)
        self.assertEqual(Material.objects.get(pk=self.acr3.pk).quantity, before_qty)

    def test_admin_sees_cost_and_margin_before_checkout(self):
        r = self.preview(self._cart())
        self.assertEqual(r.status_code, 200, r.data)
        total = D(str(r.data["total_price"]))
        cost = D(str(r.data["cost_total"]))
        self.assertGreater(cost, 0)
        self.assertEqual(D(str(r.data["margin"])), total - cost)
        material = r.data["items"][1]
        self.assertGreater(D(str(material["cost_total"])), 0)
        # то же число, что получится после оформления
        real = self.co(self._cart())
        self.assertEqual(D(str(real.data["cost_total"])), cost)
        self.assertEqual(real.data["total_price"], r.data["total_price"])

    def test_storekeeper_gets_prices_but_no_cost(self):
        r = self.preview(self._cart(), user=self.store)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIsNone(r.data["cost_total"])
        self.assertIsNone(r.data["margin"])
        self.assertTrue(all(i["cost_total"] is None for i in r.data["items"]))
        self.assertGreater(D(str(r.data["total_price"])), 0)

    def test_preview_lists_questions_instead_of_refusing(self):
        r = self.preview([self.cut(self.acr3, "3.0", "1.0", "4")])
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual([w["code"] for w in r.data["confirm_warnings"]], ["size_exceeds_sheet"])

    def test_preview_rejects_unknown_fields_like_checkout(self):
        item = self._cart()[0]
        item["materials"] = [1]
        self.assertEqual(self.preview([item]).status_code, 400)

    def test_preview_of_a_debt_order_does_not_need_a_buyer_name(self):
        r = self.preview(self._cart(), pay_full=False)
        self.assertEqual(r.status_code, 200, r.data)

    def test_preview_below_cost_warning_for_admin_only_cost(self):
        sale = {"type": "MATERIAL", "material": self.acr3.id, "mode": "SQM", "quantity": "1.0",
                "material_price": "300"}
        admin = self.preview([sale])
        self.assertIn("below_cost", [w["code"] for w in admin.data["warnings"]])
        self.assertLess(D(str(admin.data["margin"])), 0)


class PriceChangedTests(CalcBase):
    def _open_order(self):
        r = self.co([self.cut(self.acr3, "0.6", "0.8", "0.8")], client_id=self.ivan.id,
                    pay_full=False, amount_paid="0")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data["id"]

    def _raise_prices(self):
        self.client.force_authenticate(self.admin)
        self.client.patch(f"/api/warehouse/materials/{self.acr3.id}/",
                          {"price_per_sqm": "1705", "cut_rate_per_pm": "72"}, format="json")

    def test_add_items_after_a_price_change_warns_with_was_and_now(self):
        rid = self._open_order()
        self._raise_prices()
        r = self.client.post(f"{RECEIPTS}{rid}/add-items/",
                             {"items": [self.cut(self.acr3, "0.6", "0.8", "0.8")]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        changed = [w for w in r.data["warnings"] if w["code"] == "price_changed"]
        self.assertEqual(len(changed), 2)                      # работа и материал
        by_name = {w["name"]: w for w in changed}
        self.assertEqual(D(str(by_name["белый акрил 3 мм"]["was"])), D("1550"))
        self.assertEqual(D(str(by_name["белый акрил 3 мм"]["now"])), D("1705"))

    def test_add_items_at_the_same_price_does_not_warn(self):
        rid = self._open_order()
        r = self.client.post(f"{RECEIPTS}{rid}/add-items/",
                             {"items": [self.cut(self.acr3, "0.6", "0.8", "0.8")]}, format="json")
        self.assertEqual([w for w in r.data["warnings"] if w["code"] == "price_changed"], [])


class RepriceTests(CalcBase):
    def _open_order(self, **extra):
        r = self.co([self.cut(self.acr3, "0.6", "0.8", "0.8")], client_id=self.ivan.id,
                    pay_full=False, amount_paid="0", **extra)
        self.assertEqual(r.status_code, 201, r.data)
        return r.data["id"]

    def _raise_prices(self):
        self.client.force_authenticate(self.admin)
        self.client.patch(f"/api/warehouse/materials/{self.acr3.id}/",
                          {"price_per_sqm": "1705", "cut_rate_per_pm": "72"}, format="json")

    def test_reprice_brings_an_open_order_to_the_current_catalogue(self):
        rid = self._open_order()
        old = D(str(self.client.get(f"{RECEIPTS}{rid}/").data["total_price"]))
        self._raise_prices()
        n_logs = AuditLog.objects.count()
        r = self.client.post(f"{RECEIPTS}{rid}/reprice/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        new = D(str(r.data["total_price"]))
        self.assertGreater(new, old)
        fresh = self.co([self.cut(self.acr3, "0.6", "0.8", "0.8")])
        self.assertEqual(r.data["total_price"], fresh.data["total_price"])   # как новый заказ
        self.assertEqual(D(str(r.data["reprice"]["before"])), old)
        self.assertEqual(D(str(r.data["reprice"]["after"])), new)
        self.assertEqual(Receipt.objects.get(pk=rid).debt, new)               # долг пересчитан
        self.assertGreater(AuditLog.objects.count(), n_logs)

    def test_manual_prices_are_left_alone(self):
        r = self.co([{"type": "MATERIAL", "material": self.acr3.id, "mode": "SQM", "quantity": "1",
                      "material_price": "2000"}], client_id=self.ivan.id, pay_full=False, amount_paid="0")
        rid = r.data["id"]
        self._raise_prices()
        out = self.client.post(f"{RECEIPTS}{rid}/reprice/", {}, format="json")
        self.assertEqual(D(str(out.data["total_price"])), D("2000"))
        self.assertEqual(len(out.data["reprice"]["skipped"]), 1)

    def test_paid_order_cannot_be_repriced(self):
        r = self.co([self.cut(self.acr3, "0.6", "0.8", "0.8")])
        self.assertEqual(self.client.post(f"{RECEIPTS}{r.data['id']}/reprice/", {}).status_code, 400)

    def test_partly_paid_order_cannot_be_repriced(self):
        rid = self._open_order()
        self.client.post(f"{RECEIPTS}{rid}/pay/", {"amount": 100}, format="json")
        self.assertEqual(self.client.post(f"{RECEIPTS}{rid}/reprice/", {}).status_code, 400)

    def test_only_admin(self):
        rid = self._open_order()
        self.client.force_authenticate(self.store)
        self.assertEqual(self.client.post(f"{RECEIPTS}{rid}/reprice/", {}).status_code, 403)
