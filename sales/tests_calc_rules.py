"""Правила прайса: режим минимума, округление итога, неизвестные поля
(2026-10-10, CALC-01, G4-N2).

Владелец считал в Excel `=МАКС(500; рез + материал)` и `=ОКРВВЕРХ(сумма·1,25·0,95)`.
Система умела минимум только на строку работы и округляла каждую строку
отдельно: шильдик выходил на 508 вместо 500, а заказ из нескольких деталей
«плыл» на 1–3 сома. Теперь режим минимума («работа» / «деталь» / «заказ») и
режим округления («по строкам» / «итог заказа») — настройки владельца.
"""
from decimal import ROUND_CEILING, Decimal as D

from sales.pricing_rules import allocate_order_total, price_for_target
from sales.tests_calc_base import CalcBase
from warehouse.models import Roll
from warehouse.rolls import receive_lot


def _ceil(v):
    return D(str(v)).quantize(D("1"), rounding=ROUND_CEILING)


class AllocationTests(CalcBase):
    def test_remainder_goes_to_last_nonzero_line(self):
        shares = allocate_order_total([D("100.4"), D("100.4")])
        self.assertEqual(shares, [D("100"), D("101")])
        self.assertEqual(sum(shares), _ceil(D("200.8")))

    def test_gift_line_stays_zero(self):
        shares = allocate_order_total([D("10.2"), D("0"), D("5.5")])
        self.assertEqual(shares[1], D("0"))
        self.assertEqual(sum(shares), _ceil(D("15.7")))

    def test_price_for_target_hits_exactly(self):
        for qty, target in ((D("2.4"), D("3000")), (D("0.123"), D("500")), (D("37"), D("1234"))):
            price = price_for_target(qty, target)
            self.assertEqual(_ceil(qty * price), target)


class MinimumModeTests(CalcBase):
    """Шильдик: рез 0.05×0.10 акрила 3 мм, пог.м реза 0.1 — работа 6.5, материал 7.75."""

    def _plate(self):
        return self.cut(self.acr3, "0.05", "0.10", "0.10")

    def test_default_mode_is_part_and_changes_nothing_while_minimum_is_zero(self):
        s = self.settings_()
        self.assertEqual(s.min_mode, "PART")
        self.assertEqual(s.rounding_mode, "LINE")
        r = self.co([self._plate()])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertLess(D(str(r.data["total_price"])), D("30"))

    def test_work_mode_keeps_the_old_behaviour_508(self):
        # Старое поведение: минимум 500 только на работу → работа 500 + материал 8.
        self.settings_(min_line_amount=500, min_mode="WORK")
        r = self.co([self._plate()])
        self.assertEqual(D(str(r.data["total_price"])), D("508"))

    def test_part_mode_plate_costs_exactly_500(self):
        self.settings_(min_line_amount=500, min_mode="PART")
        r = self.co([self._plate()])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(str(r.data["total_price"])), D("500"))
        work, material = r.data["items"]
        self.assertTrue(work["min_applied"])
        # Работа дотянула до 500 за вычетом материала, материал — по своей цене.
        self.assertEqual(D(str(material["line_total"])), D("8"))
        self.assertEqual(D(str(work["line_total"])), D("492"))

    def test_part_mode_with_urgency_and_discount_matches_excel(self):
        # Excel: МАКС(500; рез+материал) · 1,25 · 0,95 = 593.75 → 594.
        self.settings_(min_line_amount=500, min_mode="PART", urgency_percent=25, rounding_mode="ORDER")
        r = self.co([self._plate()], client_id=self.regular.id, is_urgent=True)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(str(r.data["total_price"])), D("594"))

    def test_part_mode_material_dearer_than_minimum_leaves_work_alone(self):
        self.settings_(min_line_amount=500, min_mode="PART")
        # 1×1 м акрила = 1550 за материал — минимум 500 уже перекрыт.
        r = self.co([self.cut(self.acr3, "1", "1", "0.2")])
        work, material = r.data["items"]
        self.assertFalse(work["min_applied"])
        self.assertEqual(D(str(work["line_total"])), D("13"))   # 0.2 пог.м × 65

    def test_order_mode_lifts_the_whole_order_once(self):
        # Три таблички по ~14 сом: «на заказ» = МАКС(500; Σ), а не 3 × 500.
        self.settings_(min_line_amount=500, min_mode="ORDER")
        cart = [
            self.cut(self.acr3, "0.10", "0.05", "0.3"),
            self.cut(self.forex3, "0.2", "0.1", "0.6"),
            self.cut(self.forex8, "0.15", "0.15", "0.6"),
        ]
        r = self.co(cart)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(D(str(r.data["total_price"])), D("500"))
        rc = self.receipt(r)
        self.assertEqual(sum(i.sold_total for i in rc.items.all()), rc.total_price)

    def test_order_mode_does_not_touch_an_order_above_the_minimum(self):
        self.settings_(min_line_amount=500, min_mode="ORDER")
        r = self.co([self.cut(self.acr3, "1", "1", "4")])
        self.assertEqual(D(str(r.data["total_price"])), D("1550") + D("260"))

    def test_order_mode_ignores_per_service_minimum(self):
        self.settings_(min_line_amount=500, min_mode="ORDER")
        self.cnc.min_line_amount = D("900")
        self.cnc.save()
        r = self.co([self._plate()])
        self.assertEqual(D(str(r.data["total_price"])), D("500"))


class OrderRoundingTests(CalcBase):
    def _cart(self):
        # Две детали с «рваными» копейками: по строкам вверх даёт запас в 1–2 сома.
        return [
            self.cut(self.forex3, "0.33", "0.37", "0.37"),
            self.cut(self.forex8, "0.41", "0.29", "0.29"),
        ]

    def test_line_mode_is_the_default_and_rounds_each_line_up(self):
        r = self.co(self._cart())
        rc = self.receipt(r)
        self.assertEqual(rc.total_price, sum(i.sold_total for i in rc.items.all()))

    def test_order_mode_rounds_the_order_sum_once(self):
        self.settings_(urgency_percent=25, rounding_mode="ORDER")
        base = self.co(self._cart())
        exact = sum(
            (i.quantity * i.catalog_price for i in self.receipt(base).items.all()), D("0")
        )
        self.settings_(urgency_percent=25, rounding_mode="ORDER")
        r = self.co(self._cart(), client_id=self.regular.id, is_urgent=True)
        self.assertEqual(r.status_code, 201, r.data)
        rc = self.receipt(r)
        want = _ceil(exact * D("1.25") * D("0.95"))
        self.assertEqual(rc.total_price, want)
        # Итог чека по-прежнему сумма строк, а каждая строка — целые сомы.
        self.assertEqual(rc.total_price, sum(i.sold_total for i in rc.items.all()))
        self.assertTrue(all(i.sold_total == i.sold_total.to_integral_value() for i in rc.items.all()))

    def test_line_mode_may_cost_more_than_order_mode(self):
        self.settings_(urgency_percent=25)
        line = self.co(self._cart(), client_id=self.regular.id, is_urgent=True)
        self.settings_(rounding_mode="ORDER")
        order = self.co(self._cart(), client_id=self.regular.id, is_urgent=True)
        self.assertLessEqual(D(str(order.data["total_price"])), D(str(line.data["total_price"])))

    def test_single_line_order_is_the_same_in_both_modes(self):
        one = [self.cut(self.acr3, "0.6", "0.8", "0.8")]
        a = self.co(one)
        self.settings_(rounding_mode="ORDER")
        b = self.co(one)
        self.assertEqual(a.data["total_price"], b.data["total_price"])


class UnknownFieldsTests(CalcBase):
    def test_unknown_item_field_is_rejected_not_ignored(self):
        item = self.cut(self.acr3, "0.1", "0.05", "0.3")
        item["materials"] = [self.forex3.id]
        r = self.co([item])
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("materials", str(r.data))

    def test_price_per_item_on_a_service_is_rejected(self):
        r = self.co([{"type": "SERVICE", "service": self.mont.id, "quantity": 1, "price_per_item": "3800"}])
        self.assertEqual(r.status_code, 400, r.data)

    def test_the_same_cart_without_the_stray_field_is_fine(self):
        r = self.co([self.cut(self.acr3, "0.1", "0.05", "0.3")])
        self.assertEqual(r.status_code, 201, r.data)


class OrderRoundingInvariantTests(CalcBase):
    """На случайных корзинах: итог = вверх до сома от суммы точных стоимостей
    строк, строки — целые сомы, итог чека — сумма строк."""

    def test_random_carts(self):
        import random

        rnd = random.Random(2610)
        self.settings_(urgency_percent=25, rounding_mode="ORDER")
        mats = [self.acr3, self.forex3, self.forex8]
        for m in mats:      # запаса листов хватит на все случайные корзины
            receive_lot(m, form=Roll.Form.SHEET, width=D("1.22"), height=D("2.44"),
                        sheet_count=D("300"), purchase_cost=D("300000"))
        for _ in range(40):
            cart = []
            for _ in range(rnd.randint(1, 4)):
                w = D(rnd.randint(5, 110)) / 100
                length = D(rnd.randint(5, 220)) / 100
                cart.append(self.cut(rnd.choice(mats), w, length, D(rnd.randint(1, 400)) / 100,
                                     parts_count=rnd.randint(1, 4)))
            kw = {"is_urgent": True} if rnd.random() < 0.5 else {}
            if rnd.random() < 0.5:
                kw["client_id"] = self.regular.id
            r = self.co(cart, **kw)
            self.assertEqual(r.status_code, 201, r.data)
            rc = self.receipt(r)
            items = list(rc.items.all())
            exact = sum(
                (i.quantity * i.catalog_price * (D(100) + (i.urgency_percent or 0)) / 100
                 * (D(100) - (i.discount_percent or 0)) / 100 for i in items),
                D("0"),
            )
            self.assertEqual(rc.total_price, _ceil(exact), (cart, kw))
            self.assertEqual(rc.total_price, sum(i.sold_total for i in items))
            self.assertTrue(all(i.sold_total == i.sold_total.to_integral_value() for i in items))
