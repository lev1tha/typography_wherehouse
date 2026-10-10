"""Перепроверка владельца 10.10 (docs/OWNER_RECHECK_2026-10-10.md): строки чека.

- S1 №1 (RU-N1, RU-N2): договорная цена клиента подставляется, а ставка,
  присланная кассой без правки, не делает строку «ценой вручную»;
- S1 №2 (RP-N1): правка «деталей» у работы или материала реза пересчитывает
  пару строк — площадь, пог.м, цену, склад, себестоимость;
- S2 «минимум заказа» (RP-N2, RP-N3, G4-N3, RP-N7): после правки состава и
  пересчёта по прайсу минимум и округление заказа применяются заново; в режиме
  «деталь» лист, проданный своей строкой, входит в материал детали.

Цифры — как в Excel владельца (сценарии `scratchpad/recheck/price`).
"""
from decimal import ROUND_CEILING, Decimal as D

from clients.models import ClientPrice
from sales.models import TransactionItem
from sales.sale_service import writeoff_total
from sales.tests_calc_base import RECEIPTS, CalcBase
from sales.tests_recheck_money import MoneyCase
from services.models import ThicknessCoefficient
from warehouse.models import Material


def ceil1(x):
    return D(str(x)).quantize(D("1"), rounding=ROUND_CEILING)


def eng(w, length, **kw):
    item = {"type": "SERVICE", "width": str(w), "length": str(length)}
    item.update(kw)
    return item


class ContractPriceAtCheckoutTests(CalcBase):
    """S1 №1: договор 2 000/кв.м на гравировку, каталог 3 000."""

    def setUp(self):
        super().setUp()
        ClientPrice.objects.create(client=self.ivan, service=self.engr, price=D("2000"))

    def _engr(self, **kw):
        return eng("1", "1", service=self.engr.id, **kw)

    def test_contract_price_wins_when_the_window_sends_it(self):
        # Окно позиции подставило договорную ставку и касса её прислала.
        r = self.co([self._engr(cut_rate="2000")], client_id=self.ivan.id)
        self.assertEqual(r.status_code, 201, r.data)
        line = r.data["items"][0]
        self.assertEqual(D(str(line["line_total"])), D("2000"))
        self.assertFalse(line["price_is_manual"])
        self.assertTrue(line["client_price"])

    def test_contract_price_wins_when_nothing_is_sent(self):
        r = self.co([self._engr()], client_id=self.ivan.id)
        line = r.data["items"][0]
        self.assertEqual(D(str(line["line_total"])), D("2000"))
        self.assertFalse(line["price_is_manual"])
        self.assertTrue(line["client_price"])

    def test_catalog_rate_typed_over_the_contract_is_a_manual_price(self):
        # Окно показало договорные 2 000, человек вписал 3 000 — это его цена.
        r = self.co([self._engr(cut_rate="3000")], client_id=self.ivan.id)
        line = r.data["items"][0]
        self.assertEqual(D(str(line["line_total"])), D("3000"))
        self.assertTrue(line["price_is_manual"])
        self.assertFalse(line["client_price"])

    def test_a_really_changed_rate_stays_manual(self):
        r = self.co([self._engr(cut_rate="2500")], client_id=self.ivan.id)
        line = r.data["items"][0]
        self.assertEqual(D(str(line["line_total"])), D("2500"))
        self.assertTrue(line["price_is_manual"])
        self.assertFalse(line["client_price"])

    def test_plain_sale_without_editing_the_rate_is_not_manual(self):
        r = self.co([self._engr(cut_rate="3000")])
        line = r.data["items"][0]
        self.assertEqual(D(str(line["line_total"])), D("3000"))
        self.assertFalse(line["price_is_manual"])
        cut = self.co([self.cut(self.acr3, "0.5", "0.5", "2", cut_rate="65", material_price="1550")])
        self.assertEqual(cut.status_code, 201, cut.data)
        self.assertEqual([i["price_is_manual"] for i in cut.data["items"]], [False, False])

    def test_cut_work_and_material_under_contract(self):
        ClientPrice.objects.create(client=self.ivan, service=self.cnc, material=self.acr3, price=D("40"))
        ClientPrice.objects.create(client=self.ivan, material=self.acr3, sale_mode="SQM", price=D("1400"))
        # Касса прислала то, что подставило окно: договорные 40 и 1 400.
        r = self.co([self.cut(self.acr3, "0.5", "0.5", "2", cut_rate="40", material_price="1400")],
                    client_id=self.ivan.id)
        self.assertEqual(r.status_code, 201, r.data)
        work, mat = r.data["items"]
        self.assertEqual((D(str(work["line_total"])), D(str(mat["line_total"]))), (D("80"), D("350")))
        self.assertEqual((work["price_is_manual"], mat["price_is_manual"]), (False, False))
        self.assertEqual((work["client_price"], mat["client_price"]), (True, True))

    def test_storekeeper_sending_the_contract_rate_is_not_a_manual_price(self):
        # Граница складовщика 80 % от каталога (2 400) ниже договора не пускала бы.
        self.settings_(staff_min_price_percent=80)
        r = self.co([self._engr(cut_rate="2000")], user=self.store, client_id=self.ivan.id)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(r.data["items"][0]["client_price"])
        # Ставка реза, равная каталожной, — не «ручная цена складовщика».
        cut = self.co([self.cut(self.acr3, "0.5", "0.5", "2", cut_rate="65")], user=self.store)
        self.assertEqual(cut.status_code, 201, cut.data)

    def test_rate_endpoint_names_the_contract_rate_for_the_client(self):
        self.client.force_authenticate(self.store)
        r = self.client.get("/api/services/rate/", {"service": self.engr.id, "client": self.ivan.id})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(D(str(r.data["rate"])), D("2000"))
        self.assertEqual(r.data["source"], "client")
        plain = self.client.get("/api/services/rate/", {"service": self.engr.id, "client": self.regular.id})
        self.assertEqual(D(str(plain.data["rate"])), D("3000"))

    def test_reprice_keeps_the_contract_line(self):
        r = self.co([self._engr(cut_rate="2000")], client_id=self.ivan.id, pay_full=False, amount_paid="0")
        self.engr.rate_flat = D("3500")
        self.engr.save()
        self.client.force_authenticate(self.admin)
        out = self.client.post(f"{RECEIPTS}{r.data['id']}/reprice/", {}, format="json")
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(D(str(out.data["total_price"])), D("2000"))


class MachineEditOnContractLineTests(CalcBase):
    """S3 (C3b): смена станка у строки с договорной ставкой."""

    def _order(self):
        ClientPrice.objects.create(client=self.regular, service=self.cnc, material=self.acr3, price=D("40"))
        r = self.co([self.cut(self.acr3, "0.5", "0.5", "10")], client_id=self.regular.id,
                    pay_full=False, amount_paid="0")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data["id"], r.data["items"][0]["id"]

    def _machine(self, rid, wid):
        self.client.force_authenticate(self.admin)
        out = self.client.post(f"{RECEIPTS}{rid}/edit-items/", {"items": [{"id": wid, "machine": "LASER"}]},
                               format="json")
        self.assertEqual(out.status_code, 200, out.data)
        return TransactionItem.objects.get(pk=wid)

    def test_without_a_contract_on_the_new_machine_the_catalog_gets_the_client_discount(self):
        work = self._machine(*self._order())
        self.assertEqual(work.sold_total, D("618"))          # 10 × 65 × 0,95 = 617,5 → 618
        self.assertFalse(work.client_price)
        self.assertEqual(work.discount_percent, D("5"))

    def test_contract_of_the_new_machine_is_used(self):
        ClientPrice.objects.create(client=self.regular, service=self.laser, material=self.acr3, price=D("50"))
        work = self._machine(*self._order())
        self.assertEqual(work.sold_total, D("500"))
        self.assertTrue(work.client_price)


class PartsEditTests(CalcBase):
    """S1 №2: 12 → 10 деталей форекса 8 мм 0,2×0,3, рез 1 пог.м на деталь."""

    def setUp(self):
        super().setUp()
        self.cnc.rate_per_pm = D("35")
        self.cnc.save()
        ThicknessCoefficient.objects.create(kind="CUTTING", thickness_from=D("6"), coefficient=D("1.35"))
        self.settings_(urgency_percent=25)
        r = self.co([self.cut(self.forex8, "0.2", "0.3", "1.0", parts_count=12)],
                    client_id=self.regular.id, is_urgent=True, pay_full=False, amount_paid="0")
        self.assertEqual(r.status_code, 201, r.data)
        self.rid = r.data["id"]
        self.work_id, self.mat_id = [i["id"] for i in r.data["items"]]
        self.q0 = Material.objects.get(pk=self.forex8.pk).quantity
        self.cost0 = TransactionItem.objects.get(pk=self.mat_id).cost_total

    def edit(self, changes):
        self.client.force_authenticate(self.admin)
        return self.client.post(f"{RECEIPTS}{self.rid}/edit-items/", {"items": changes}, format="json")

    def _check_ten_parts(self, out):
        self.assertEqual(out.status_code, 200, out.data)
        work = TransactionItem.objects.get(pk=self.work_id)
        mat = TransactionItem.objects.get(pk=self.mat_id)
        self.assertEqual((work.parts_count, mat.parts_count), (10, 10))
        self.assertEqual(work.quantity, D("10.000"))           # 10 × 1 пог.м
        self.assertEqual(mat.quantity, D("0.600"))             # 10 × 0,2 × 0,3
        # Excel: 10 × 47,25 × 1,1875 + 0,6 × 800 × 1,1875 (срочно 25 %, скидка 5 %).
        excel = ceil1(D("10") * D("47.25") * D("1.1875")) + ceil1(D("0.6") * 800 * D("1.1875"))
        self.assertEqual(D(str(out.data["total_price"])), excel)
        # Склад: вернулись 0,12 кв.м, себестоимость материала — за 0,6 кв.м.
        self.assertEqual(Material.objects.get(pk=self.forex8.pk).quantity - self.q0, D("0.12"))
        self.assertAlmostEqual(float(mat.cost_total), float(self.cost0) * 10 / 12, delta=0.02)

    def test_parts_on_the_work_line_move_the_material_line(self):
        self._check_ten_parts(self.edit([{"id": self.work_id, "parts_count": 10}]))

    def test_parts_on_the_material_line_move_the_work_line(self):
        self._check_ten_parts(self.edit([{"id": self.mat_id, "parts_count": 10}]))

    def test_sizes_on_the_work_line_move_the_material_line(self):
        out = self.edit([{"id": self.work_id, "width": "0.25"}])
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(TransactionItem.objects.get(pk=self.mat_id).quantity, D("0.900"))  # 12 × 0,25 × 0,3

    def test_parts_of_a_material_without_sizes_are_refused_not_ignored(self):
        r = self.co([{"type": "MATERIAL", "material": self.acr3.id, "mode": "SQM", "quantity": "0.5"}])
        line = r.data["items"][0]["id"]
        self.client.force_authenticate(self.admin)
        out = self.client.post(f"{RECEIPTS}{r.data['id']}/edit-items/",
                               {"items": [{"id": line, "parts_count": 2}]}, format="json")
        self.assertEqual(out.status_code, 400, out.data)
        self.assertIn("детал", out.data["detail"])
        self.assertEqual(TransactionItem.objects.get(pk=line).parts_count, 1)


class OrderMinimumAfterChangesTests(CalcBase):
    """S2 «минимум заказа» — цифры владельца из сценариев C1, C2, C4, C5."""

    def nameplate(self):
        return self.cut(self.acr3, "0.05", "0.10", "0.30")

    def test_removing_the_line_that_carried_the_order_minimum_keeps_the_minimum(self):
        # C5: минимум «на заказ» 500, округление «итог», срочно 25 %, скидка 5 %.
        self.settings_(min_line_amount=500, min_mode="ORDER", rounding_mode="ORDER", urgency_percent=25)
        cart = [self.nameplate(), eng("0.1", "0.1", service=self.engr.id),
                {"type": "SERVICE", "service": self.letters.id, "quantity": 2}]
        r = self.co(cart, client_id=self.regular.id, is_urgent=True, pay_full=False, amount_paid="0")
        self.assertEqual(D(str(r.data["total_price"])), D("594"))
        letters = next(i["id"] for i in r.data["items"] if i.get("service_name") == "Наружные буквы")
        self.client.force_authenticate(self.admin)
        out = self.client.post(f"{RECEIPTS}{r.data['id']}/edit-items/",
                               {"items": [{"id": letters, "remove": True}]}, format="json")
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(D(str(out.data["total_price"])), D("594"))     # не 67

    def test_whole_sheet_cut_is_part_of_the_sheet_detail(self):
        # C4: 2 листа форекса + рез 12 пог.м по целому листу, минимум «деталь» 500.
        self.settings_(min_line_amount=500, min_mode="PART")
        r = self.co([{"type": "MATERIAL", "material": self.forex3.id, "mode": "PIECE", "quantity": "2"},
                     self.cut(self.forex3, "0", "0", "12.0")])
        self.assertEqual(r.status_code, 201, r.data)
        # Excel: МАКС(500; 2 × 1 488 + 12 × 35) = 3 396.
        self.assertEqual(D(str(r.data["total_price"])), D("3396"))

    def test_whole_sheet_cut_without_the_sheet_still_gets_the_minimum(self):
        self.settings_(min_line_amount=500, min_mode="PART")
        r = self.co([self.cut(self.forex3, "0", "0", "2.0")])
        self.assertEqual(D(str(r.data["total_price"])), D("500"))

    def test_reprice_takes_the_part_minimum_from_the_new_material_price(self):
        # C1: шильдик «деталь» 500; акрил 1 550 → 3 100; пересчёт — снова 500.
        self.settings_(min_line_amount=500, min_mode="PART")
        r = self.co([self.nameplate()], client_id=self.ivan.id, pay_full=False, amount_paid="0")
        self.assertEqual(D(str(r.data["total_price"])), D("500"))
        Material.objects.filter(pk=self.acr3.pk).update(price_per_sqm=D("3100"))
        self.client.force_authenticate(self.admin)
        out = self.client.post(f"{RECEIPTS}{r.data['id']}/reprice/", {}, format="json")
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(D(str(out.data["total_price"])), D("500"))     # не 508
        more = self.client.post(f"{RECEIPTS}{r.data['id']}/add-items/", {"items": [self.nameplate()]},
                                format="json")
        self.assertEqual(D(str(more.data["total_price"])), D("1000"))

    def test_removing_the_material_of_a_detail_raises_the_work_to_the_minimum(self):
        self.settings_(min_line_amount=500, min_mode="PART")
        r = self.co([self.nameplate()], client_id=self.ivan.id, pay_full=False, amount_paid="0")
        mat = next(i["id"] for i in r.data["items"] if i["type"] == "MATERIAL")
        self.client.force_authenticate(self.admin)
        out = self.client.post(f"{RECEIPTS}{r.data['id']}/edit-items/",
                               {"items": [{"id": mat, "remove": True}]}, format="json")
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(D(str(out.data["total_price"])), D("500"))     # работа одна — минимум целиком

    def test_order_rounding_survives_reprice_and_edit(self):
        # C2: «итог одной формулой», срочно 25 %, скидка 5 %.
        self.settings_(rounding_mode="ORDER", urgency_percent=25)
        cart = [self.cut(self.acr3, "1.0", "0.5", "14.2"), eng("0.15", "0.2", service=self.engr.id),
                {"type": "SERVICE", "service": self.letters.id, "quantity": 14}]
        r = self.co(cart, client_id=self.regular.id, is_urgent=True, pay_full=False, amount_paid="0")
        self.assertEqual(D(str(r.data["total_price"])),
                         ceil1((D("923") + D("775") + D("90") + D("2100")) * D("1.1875")))
        self.letters.rate_per_piece = D("160")
        self.letters.save()
        self.client.force_authenticate(self.admin)
        out = self.client.post(f"{RECEIPTS}{r.data['id']}/reprice/", {}, format="json")
        self.assertEqual(D(str(out.data["total_price"])),
                         ceil1((D("923") + D("775") + D("90") + D("2240")) * D("1.1875")))   # 4 784
        letters = next(i["id"] for i in r.data["items"] if i.get("service_name") == "Наружные буквы")
        out = self.client.post(f"{RECEIPTS}{r.data['id']}/edit-items/",
                               {"items": [{"id": letters, "quantity": "12"}]}, format="json")
        self.assertEqual(out.status_code, 200, out.data)
        self.assertEqual(D(str(out.data["total_price"])),
                         ceil1((D("923") + D("775") + D("90") + D("1920")) * D("1.1875")))   # 4 404


class EditOfWrittenOffOrderTests(MoneyCase):
    """S1 (координатор): заказ 7 400 списан целиком, состав правят до 3 700.

    Лишнее после правки — не переплата клиента: сначала уменьшается списание
    (и его расход «Безнадёжные долги»), сдачей — только остаток настоящих денег.
    """

    def _edit_to(self, receipt, qty):
        line = receipt.items.get()
        return self.act(receipt, "edit-items", {"items": [{"id": line.id, "quantity": str(qty)}]})

    def test_written_off_order_cut_in_half_gives_no_change(self):
        r = self.sale(7400, paid=0)
        self.write_off(r)
        self.assertEqual(self.bad_debt(), D("7400"))
        cash0 = self.cash()
        self._edit_to(r, 3700)
        r.refresh_from_db()
        self.assertEqual(r.change_due, D("0"))          # не 3 700 «сдачи» из ничего
        self.assertEqual(r.debt, D("0"))
        self.assertEqual(r.amount_paid, D("3700"))
        self.assertEqual(writeoff_total(r), D("3700"))
        self.assertEqual(self.bad_debt(), D("3700"))
        self.assertEqual(self.cash(), cash0)
        self.check("edit written-off")

    def test_paid_part_stays_money_written_off_part_goes_first(self):
        r = self.sale(7400, paid=3000)
        self.write_off(r)                               # 4 400 списано
        self._edit_to(r, 3700)
        r.refresh_from_db()
        # Лишние 3 700 забирает списание; 3 000 настоящих денег остаются оплатой.
        self.assertEqual((r.change_due, r.debt), (D("0"), D("0")))
        self.assertEqual(writeoff_total(r), D("700"))
        self.assertEqual(self.bad_debt(), D("700"))
        self.check("edit partly written-off")

    def test_change_only_from_real_money(self):
        r = self.sale(7400, paid=5000)
        self.write_off(r)                               # 2 400 списано
        self._edit_to(r, 3700)
        r.refresh_from_db()
        # Лишние 3 700: 2 400 — списание, 1 300 — сдача из настоящих денег.
        self.assertEqual(r.change_due, D("1300"))
        self.assertEqual(writeoff_total(r), D("0"))
        self.assertEqual(self.bad_debt(), D("0"))
        self.check("edit written-off with change")
