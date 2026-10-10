"""Выдача по позициям, гарантийные переделки, лимит долга, видимость закупа,
поиск и онлайн-оплата (2026-10-10, G1-N4, G2-N3, CLI-03, CLI-09, STAFF-07, XL-10).
"""
from datetime import timedelta
from decimal import Decimal as D

from django.test import override_settings

from audit.models import AuditLog
from finance.models import CashEntry
from sales.models import Receipt
from sales.sale_service import create_sale, day_to_moment
from sales.tests_calc_base import CalcBase, RECEIPTS
from warehouse.models import Material


class TwoLineBase(CalcBase):
    def setUp(self):
        super().setUp()
        self.bolts = Material.objects.create(
            name="Крепёж", unit=Material.Unit.PIECE, quantity=D("1000"),
            price_per_unit=D("100"), piece_price=D("100"), purchase_price=D("40"),
        )
        self.glue = Material.objects.create(
            name="Клей", unit=Material.Unit.PIECE, quantity=D("100"),
            price_per_unit=D("500"), piece_price=D("500"), purchase_price=D("200"),
        )

    def order(self, **kw):
        return create_sale(
            client=kw.pop("client", self.ivan), cashier=self.admin, payment_method="CASH",
            items_data=[
                {"type": "MATERIAL", "material": self.bolts, "quantity": 10, "mode": "PIECE"},
                {"type": "MATERIAL", "material": self.glue, "quantity": 2, "mode": "PIECE"},
            ],
            amount_paid=kw.pop("paid", D("2000")), **kw,
        )

    def api(self, receipt, action, body=None, user=None):
        self.client.force_authenticate(user or self.admin)
        return self.client.post(f"{RECEIPTS}{receipt.id}/{action}/", body or {}, format="json")


class PartialIssueTests(TwoLineBase):
    def test_partial_then_full_issue_moves_the_status(self):
        r = self.order()
        bolts, glue = r.items.get(material=self.bolts), r.items.get(material=self.glue)
        out = self.api(r, "issue", {"items": [{"id": bolts.id, "quantity": 4}]})
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(out.data["fulfillment_status"], "PARTIALLY_ISSUED")
        line = next(i for i in out.data["items"] if i["id"] == bolts.id)
        self.assertEqual(D(str(line["issued_qty"])), D("4"))
        out = self.api(r, "issue", {"items": [{"id": bolts.id, "quantity": 6}]})
        self.assertEqual(out.data["fulfillment_status"], "PARTIALLY_ISSUED")   # клей ещё не отдан
        out = self.api(r, "issue", {"items": [{"id": glue.id, "quantity": 2}]})
        self.assertEqual(out.data["fulfillment_status"], "ISSUED")
        self.assertTrue(AuditLog.objects.filter(action__contains="Выдан частично").exists())

    def test_cannot_issue_more_than_is_left(self):
        r = self.order()
        bolts = r.items.get(material=self.bolts)
        self.assertEqual(self.api(r, "issue", {"items": [{"id": bolts.id, "quantity": 11}]}).status_code, 400)
        self.api(r, "issue", {"items": [{"id": bolts.id, "quantity": 8}]})
        self.assertEqual(self.api(r, "issue", {"items": [{"id": bolts.id, "quantity": 3}]}).status_code, 400)

    def test_bad_payloads_are_400(self):
        r = self.order()
        bolts = r.items.get(material=self.bolts)
        for body in ({}, {"items": []}, {"items": [{"id": bolts.id}]},
                     {"items": [{"id": bolts.id, "quantity": 0}]},
                     {"items": [{"id": bolts.id, "quantity": 1, "price": 5}]},
                     {"items": [{"id": 999999, "quantity": 1}]}):
            self.assertEqual(self.api(r, "issue", body).status_code, 400, body)

    def test_mark_issued_marks_every_line_and_rollback_clears_them(self):
        r = self.order()
        self.api(r, "mark-issued")
        for item in r.items.all():
            self.assertEqual(item.issued_qty, item.quantity)
        self.api(r, "mark-ready")
        self.assertEqual(r.items.filter(issued_qty__gt=0).count(), 0)

    def test_partial_status_is_not_settable_by_hand(self):
        r = self.order()
        out = self.api(r, "set-fulfillment", {"status": "PARTIALLY_ISSUED"})
        self.assertEqual(out.status_code, 400, out.data)

    def test_partially_issued_order_still_takes_more_items_but_issued_one_does_not(self):
        r = self.order()
        bolts = r.items.get(material=self.bolts)
        self.api(r, "issue", {"items": [{"id": bolts.id, "quantity": 4}]})
        more = self.api(r, "add-items", {"items": [{"type": "MATERIAL", "material": self.bolts.id,
                                                    "mode": "PIECE", "quantity": 1}]})
        self.assertEqual(more.status_code, 200, more.data)
        r2 = self.order()
        self.api(r2, "mark-issued")
        self.assertEqual(self.api(r2, "add-items", {"items": []}).status_code, 400)

    def test_storekeeper_can_issue_accountant_cannot(self):
        r = self.order()
        bolts = r.items.get(material=self.bolts)
        body = {"items": [{"id": bolts.id, "quantity": 1}]}
        self.assertEqual(self.api(r, "issue", body, user=self.store).status_code, 200)
        self.assertEqual(self.api(r, "issue", body, user=self.accountant).status_code, 403)


class WarrantyTests(TwoLineBase):
    def _warranty(self, original, user=None, **extra):
        self.client.force_authenticate(user or self.admin)
        body = {
            "payment_method": "CASH", "pay_full": True, "is_warranty": True,
            "warranty_of": str(original.id), "warranty_reason": "кривая резка",
            "warranty_culprit": "Мастер Азат",
            "items": [{"type": "MATERIAL", "material": self.bolts.id, "mode": "PIECE", "quantity": 5}],
        }
        body.update(extra)
        return self.client.post("/api/sales/receipts/checkout/", body, format="json")

    def test_warranty_order_is_free_but_consumes_material(self):
        original = self.order()
        r = self._warranty(original)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(str(r.data["total_price"])), D("0"))
        self.assertTrue(r.data["is_warranty"])
        self.assertEqual(r.data["warranty_of_number"], original.order_number)
        self.assertEqual((r.data["warranty_reason"], r.data["warranty_culprit"]), ("кривая резка", "Мастер Азат"))
        self.assertEqual(D(str(r.data["cost_total"])), D("200"))            # 5 × 40
        self.bolts.refresh_from_db()
        self.assertEqual(self.bolts.quantity, D("985"))
        self.assertEqual(r.data["payment_status"], "PAID")
        self.assertEqual(CashEntry.objects.filter(receipt_id=r.data["id"]).count(), 0)
        self.assertEqual(Receipt.objects.get(pk=r.data["id"]).debt, D("0"))

    def test_original_margin_shows_the_warranty_cost(self):
        original = self.order()
        self._warranty(original)
        self.client.force_authenticate(self.admin)
        card = self.client.get(f"{RECEIPTS}{original.id}/").data
        self.assertEqual(D(str(card["warranty_cost"])), D("200"))
        self.assertEqual(D(str(card["margin_net"])), D(str(card["margin"])) - D("200"))
        # Сама маржа исходного заказа (на ней стоят отчёты) не меняется.
        # Заказ 1000 + 1000, себестоимость 400 + 400.
        self.assertEqual(D(str(card["margin"])), D("1200"))

    def test_storekeeper_cannot_open_a_warranty_and_cost_is_hidden(self):
        original = self.order()
        self.assertEqual(self._warranty(original, user=self.store).status_code, 403)
        self._warranty(original)
        self.client.force_authenticate(self.store)
        card = self.client.get(f"{RECEIPTS}{original.id}/").data
        self.assertIsNone(card["warranty_cost"])
        self.assertIsNone(card["margin_net"])

    def test_reason_is_required_and_warranty_fields_need_the_flag(self):
        original = self.order()
        self.assertEqual(self._warranty(original, warranty_reason="").status_code, 400)
        self.client.force_authenticate(self.admin)
        stray = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True, "warranty_reason": "x",
            "items": [{"type": "MATERIAL", "material": self.bolts.id, "mode": "PIECE", "quantity": 1}],
        }, format="json")
        self.assertEqual(stray.status_code, 400, stray.data)

    def test_warranty_is_not_a_below_cost_sale_and_is_logged(self):
        original = self.order()
        r = self._warranty(original)
        self.assertNotIn("below_cost", [w["code"] for w in r.data["warnings"]])
        self.assertTrue(AuditLog.objects.filter(action__contains="гарантийная переделка").exists())

    def test_warranty_prices_stay_zero_even_with_rules(self):
        self.settings_(min_line_amount=500, urgency_percent=25)
        original = self.order()
        r = self._warranty(original, is_urgent=True)
        self.assertEqual(D(str(r.data["total_price"])), D("0"))


class DebtWarningTests(TwoLineBase):
    def test_order_in_debt_over_the_client_limit_needs_confirmation(self):
        self.ivan.credit_limit = D("1500")
        self.ivan.save()
        item = [{"type": "MATERIAL", "material": self.bolts.id, "mode": "PIECE", "quantity": 20}]
        r = self.co(item, client_id=self.ivan.id, pay_full=False, amount_paid="0")
        self.assertEqual(r.status_code, 409, r.data)
        w = r.data["warnings"][0]
        self.assertEqual((w["code"], w["reason"]), ("debt_over_limit", "limit"))
        self.assertEqual(Receipt.objects.count(), 0)
        ok = self.co(item, client_id=self.ivan.id, pay_full=False, amount_paid="0",
                     confirmed_warnings=["debt_over_limit"])
        self.assertEqual(ok.status_code, 201, ok.data)

    def test_a_fully_paid_order_never_asks(self):
        self.ivan.credit_limit = D("10")
        self.ivan.save()
        item = [{"type": "MATERIAL", "material": self.bolts.id, "mode": "PIECE", "quantity": 20}]
        self.assertEqual(self.co(item, client_id=self.ivan.id).status_code, 201)

    def test_old_debt_warning_is_a_setting(self):
        item = [{"type": "MATERIAL", "material": self.bolts.id, "mode": "PIECE", "quantity": 1}]
        old = day_to_moment(self.today - timedelta(days=60))
        create_sale(client=self.ivan, cashier=self.admin, payment_method="CASH",
                    items_data=[{"type": "MATERIAL", "material": self.bolts, "quantity": 5, "mode": "PIECE"}],
                    amount_paid=D("0"), created_at=old)
        again = self.co(item, client_id=self.ivan.id, pay_full=False, amount_paid="0")
        self.assertEqual(again.status_code, 201, again.data)                # настройка выключена
        self.settings_(debt_warn_days=30)
        r = self.co(item, client_id=self.ivan.id, pay_full=False, amount_paid="0")
        self.assertEqual(r.status_code, 409, r.data)
        self.assertEqual(r.data["warnings"][0]["reason"], "age")

    def test_debt_without_a_client_needs_a_buyer_name(self):
        item = [{"type": "MATERIAL", "material": self.bolts.id, "mode": "PIECE", "quantity": 2}]
        no = self.co(item, pay_full=False, amount_paid="50")
        self.assertEqual(no.status_code, 400, no.data)
        self.assertEqual(Receipt.objects.count(), 0)
        yes = self.co(item, pay_full=False, amount_paid="50", buyer_name="Тахир с рынка")
        self.assertEqual(yes.status_code, 201, yes.data)
        self.assertEqual(yes.data["buyer_name"], "Тахир с рынка")
        # Оплаченный заказ имени не требует.
        self.assertEqual(self.co(item).status_code, 201)


class CostVisibilityTests(CalcBase):
    def test_offcut_cost_is_hidden_from_the_storekeeper(self):
        mat = Material.objects.create(
            name="Оракал", unit=Material.Unit.SQM, is_roll_material=True,
            intake_form=Material.IntakeForm.ROLL, roll_width=D("0.9"), price_per_pm=D("300"),
        )
        self.client.force_authenticate(self.admin)
        self.client.post("/api/warehouse/materials/receive-roll/", {
            "material": mat.id, "form": "ROLL", "width": "0.9", "length": "20", "purchase_cost": "1800",
        }, format="json")
        r = self.co([{"type": "MATERIAL", "material": mat.id, "mode": "METER", "quantity": "2",
                      "used_width": "0.5"}])
        self.assertEqual(r.status_code, 201, r.data)
        admin_line = r.data["items"][0]
        self.assertEqual(D(str(admin_line["offcut_cost"])), D("80"))
        self.client.force_authenticate(self.store)
        store_line = self.client.get(f"{RECEIPTS}{r.data['id']}/").data["items"][0]
        self.assertIsNone(store_line["offcut_cost"])
        self.assertIsNone(store_line["cost_total"])
        self.assertEqual(D(str(store_line["offcut_area"])), D("0.8"))     # площадь — не деньги
        self.client.force_authenticate(self.accountant)
        self.assertEqual(D(str(self.client.get(f"{RECEIPTS}{r.data['id']}/").data["items"][0]["offcut_cost"])), D("80"))


class SearchAndOrderingTests(CalcBase):
    def test_search_finds_orders_by_material_service_and_note(self):
        a = self.co([self.cut(self.acr3, "0.5", "0.5", "1", note="логотип кафе")])
        b = self.co([{"type": "SERVICE", "service": self.mont.id, "quantity": 1}])
        self.client.force_authenticate(self.admin)

        def found(q):
            data = self.client.get(RECEIPTS, {"search": q}).data["results"]
            return sorted(row["id"] for row in data)

        self.assertEqual(found("акрил"), [a.data["id"]])              # материал
        self.assertEqual(found("Монтаж"), [b.data["id"]])              # услуга
        self.assertEqual(found("логотип"), [a.data["id"]])             # комментарий к строке
        self.assertEqual(found("форекс"), [])

    def test_search_does_not_duplicate_an_order_with_two_matching_lines(self):
        self.co([self.cut(self.acr3, "0.5", "0.5", "1")])              # работа + материал: «акрил» в одной, «Резка» в другой
        self.client.force_authenticate(self.admin)
        rows = self.client.get(RECEIPTS, {"search": "Резка"}).data["results"]
        self.assertEqual(len(rows), 1)

    def test_ordering_by_order_number_works_both_ways(self):
        for _ in range(3):
            self.co([{"type": "SERVICE", "service": self.mont.id, "quantity": 1}])
        self.client.force_authenticate(self.admin)
        up = [r["order_number"] for r in self.client.get(RECEIPTS, {"ordering": "order_number"}).data["results"]]
        down = [r["order_number"] for r in self.client.get(RECEIPTS, {"ordering": "-order_number"}).data["results"]]
        self.assertEqual(up, sorted(up))
        self.assertEqual(down, sorted(down, reverse=True))
        self.assertEqual(up, down[::-1])


class OnlineGatewayFlagTests(CalcBase):
    def test_online_is_hidden_while_the_gateway_is_a_mock(self):
        self.client.force_authenticate(self.store)
        self.assertFalse(self.client.get("/api/services/rules/").data["online_payments_enabled"])

    @override_settings(PAYMENT_GATEWAY="freedompay")
    def test_online_is_shown_with_a_real_gateway(self):
        self.client.force_authenticate(self.store)
        self.assertTrue(self.client.get("/api/services/rules/").data["online_payments_enabled"])
