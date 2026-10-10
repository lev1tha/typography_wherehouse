"""Правила прайса (2026-10-10, CALC-01, CLI-02, CALC-08): минимальная сумма
строки, наценка за срочность, скидка клиента и предупреждение «ниже
себестоимости».

Правила применяются ПОСТРОЧНО, в порядке: расчёт → минимум → ×(1+срочность)
→ ×(1−скидка) → вверх до сома. Итог чека — сумма строк, поэтому долг, касса
и отчёты согласованы без правок.
"""
import random
from decimal import ROUND_CEILING, Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from audit.models import AuditLog
from clients.models import Client
from sales.models import Receipt, TransactionItem
from sales.pricing_rules import LineRules, price_for, target_total
from services.models import PricingSettings, PrintingService
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot

CHECKOUT = "/api/sales/receipts/checkout/"


def _ceil(v):
    return Decimal(v).quantize(Decimal("1"), rounding=ROUND_CEILING)


class PriceForTests(APITestCase):
    """Подбор цены за единицу: строка выходит ровно в целевую сумму."""

    def test_noop_keeps_price(self):
        self.assertEqual(price_for(Decimal("2.4"), Decimal("1250"), LineRules()), (Decimal("1250"), False))

    def test_line_hits_target_exactly(self):
        rnd = random.Random(10)
        for _ in range(2000):
            qty = Decimal(rnd.randint(1, 100_000)) / 1000  # 0.001 … 100
            base = Decimal(rnd.randint(1, 500_000)) / 100
            rules = LineRules(
                minimum=Decimal(rnd.choice([0, 0, 300, 500])),
                urgency=Decimal(rnd.choice([0, 10, 25, 33.33])).quantize(Decimal("0.01")),
                discount=Decimal(rnd.choice([0, 5, 7.5, 12])).quantize(Decimal("0.01")),
            )
            price, _ = price_for(qty, base, rules)
            target, _ = target_total(qty, base, rules)
            self.assertEqual(_ceil(qty * price), target, (qty, base, rules))
            self.assertEqual(price, price.quantize(Decimal("0.01")))

    def test_large_quantity_never_cheaper_than_target(self):
        # 300 штук по 1.50 при минимуме 500: копейка на штуку — это 3 сома на
        # строку, ровно 500 не собрать; строка не дешевле минимума.
        price, applied = price_for(Decimal("300"), Decimal("1.50"), LineRules(minimum=Decimal("500")))
        self.assertTrue(applied)
        self.assertGreaterEqual(_ceil(Decimal("300") * price), Decimal("500"))
        self.assertLess(_ceil(Decimal("300") * price), Decimal("504"))

    def test_zero_price_line_is_not_lifted_to_minimum(self):
        self.assertEqual(
            price_for(Decimal("1"), Decimal("0"), LineRules(minimum=Decimal("500"))),
            (Decimal("0"), False),
        )


class PricingRulesApiTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="pr_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="pr_store", password="x", role=User.Role.STOREKEEPER)
        self.sheet = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
            price_per_sqm=Decimal("1250"), piece_price=Decimal("3700"),
        )
        # 5 листов по 3 500 — себестоимость листа 3 500.
        receive_lot(self.sheet, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=Decimal("5"), purchase_cost=Decimal("17500"))
        self.cutting = PrintingService.objects.create(
            name="Резка ЧПУ", kind=PrintingService.Kind.CUTTING, rate_per_pm=Decimal("600"),
        )
        self.install = PrintingService.objects.create(
            name="Монтаж", kind=PrintingService.Kind.OTHER, base_price=Decimal("580"),
        )
        self.engraving = PrintingService.objects.create(
            name="Гравировка", kind=PrintingService.Kind.ENGRAVING, rate_flat=Decimal("3000"),
        )
        self.regular = Client.objects.create(full_name="Постоянный", phone="+996555000111",
                                             discount_percent=Decimal("5"))

    # --- помощники ---------------------------------------------------------
    def _settings(self, **values):
        s = PricingSettings.load()
        for k, v in values.items():
            setattr(s, k, Decimal(str(v)))
        s.save()

    def _sign_items(self):
        """Вывеска: работа реза 3 пог.м × 600 = 1 800, акрил 1.2 × 2 = 2.4 кв.м
        × 1 250 = 3 000, монтаж 580. Всего по каталогу 5 380."""
        return [
            {"type": "SERVICE", "service": self.cutting.id, "material": self.sheet.id,
             "width": "1.2", "length": "2", "running_meters": "3"},
            {"type": "SERVICE", "service": self.install.id, "quantity": 1},
        ]

    def _checkout(self, items, user=None, **extra):
        self.client.force_authenticate(user or self.admin)
        body = {"payment_method": "CASH", "pay_full": True, "items": items, **extra}
        return self.client.post(CHECKOUT, body, format="json")

    def _assert_total_is_sum_of_lines(self, receipt_id):
        receipt = Receipt.objects.get(pk=receipt_id)
        lines = sum((i.sold_total for i in receipt.items.all()), Decimal("0"))
        self.assertEqual(receipt.total_price, lines)
        return receipt

    # --- регрессия: всё выключено — как раньше --------------------------
    def test_rules_off_prices_unchanged(self):
        r = self._checkout(self._sign_items(), client_id=self.regular.id, discount_percent=0)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(r.data["total_price"]), Decimal("5380"))
        self.assertEqual(Decimal(r.data["catalog_total"]), Decimal("5380"))
        for item in TransactionItem.objects.all():
            self.assertEqual(item.price_per_item, item.catalog_price)
            self.assertFalse(item.min_applied)
        self.assertFalse(r.data["is_urgent"])

    def test_old_lines_without_rules_keep_working(self):
        r = self._checkout(self._sign_items(), client_id=self.regular.id, discount_percent=0)
        # Строки «до правил» — как на проде до миграции: полей правил нет.
        TransactionItem.objects.update(catalog_price=None, urgency_percent=None, discount_percent=None)
        work = TransactionItem.objects.get(service=self.cutting)
        r = self.client.post(f"/api/sales/receipts/{r.data['id']}/edit-items/",
                             {"items": [{"id": work.id, "quantity": "4"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(r.data["total_price"]), Decimal("5980"))
        item = next(i for i in r.data["items"] if i["id"] == work.id)
        self.assertEqual(Decimal(item["catalog_total"]), Decimal("2400"))

    # --- сценарий вывески из аудита ---------------------------------------
    def test_sign_urgent_with_client_discount(self):
        """5 380 по каталогу, срочно +25 %, скидка 5 %.

        Одной формулой по итогу: 5 380 × 1.25 × 0.95 = 6 388.75 → 6 389. Но
        правила применяются построчно и каждая строка округляется вверх сама
        (прежнее правило строки): 1 800 → 2 137.50 → 2 138; 3 000 → 3 562.50 →
        3 563; 580 → 688.75 → 689. Итог — 6 390: на сом больше, потому что
        округлены три строки, а не одна сумма.
        """
        self._settings(urgency_percent=25)
        r = self._checkout(self._sign_items(), client_id=self.regular.id, is_urgent=True)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(r.data["total_price"]), Decimal("6390"))
        self.assertEqual(Decimal(r.data["catalog_total"]), Decimal("5380"))
        self.assertTrue(r.data["is_urgent"])
        self.assertEqual(Decimal(r.data["urgency_percent"]), Decimal("25"))
        self.assertEqual(Decimal(r.data["discount_percent"]), Decimal("5"))
        by_kind = {
            (i["service"], i["material"]): Decimal(i["line_total"]) for i in r.data["items"]
        }
        self.assertEqual(by_kind[(self.cutting.id, None)], Decimal("2138"))
        self.assertEqual(by_kind[(None, self.sheet.id)], Decimal("3563"))
        self.assertEqual(by_kind[(self.install.id, None)], Decimal("689"))
        receipt = self._assert_total_is_sum_of_lines(r.data["id"])
        # Оплачено целиком — долг 0, касса приняла ровно итог.
        self.assertEqual(receipt.debt, Decimal("0"))
        self.assertEqual(receipt.amount_paid, Decimal("6390"))

    def test_sign_report_of_discounts_and_urgency(self):
        self._settings(urgency_percent=25)
        self._checkout(self._sign_items(), client_id=self.regular.id, is_urgent=True)
        r = self.client.get("/api/finance/report/")
        self.assertEqual(r.status_code, 200)
        rules = r.data["pricing_rules"]
        # Срочность: 25 % от 5 380 = 1 345; скидка: 5 % от 6 725 = 336.25.
        self.assertEqual(rules["urgency"]["amount"], Decimal("1345.00"))
        self.assertEqual(rules["discount"]["amount"], Decimal("336.25"))
        self.assertEqual(rules["minimum"]["amount"], Decimal("0"))
        self.assertEqual(rules["urgency"]["orders"], 1)
        self.assertEqual(rules["discount"]["lines"], 3)

    # --- минимальная сумма строки -----------------------------------------
    def test_nameplate_minimum(self):
        """Шильдик 10 × 10 см гравировкой: 0.01 кв.м × 3 000 = 30 → минимум 500."""
        self._settings(min_line_amount=500)
        r = self._checkout([{"type": "SERVICE", "service": self.engraving.id, "width": "0.1", "length": "0.1"}])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(r.data["total_price"]), Decimal("500"))
        item = r.data["items"][0]
        self.assertTrue(item["min_applied"])
        self.assertEqual(Decimal(item["catalog_total"]), Decimal("30"))
        self.assertEqual(Decimal(item["min_amount"]), Decimal("500"))
        self._assert_total_is_sum_of_lines(r.data["id"])
        rules = self.client.get("/api/finance/report/").data["pricing_rules"]
        self.assertEqual(rules["minimum"]["amount"], Decimal("470.00"))

    def test_minimum_not_for_material_and_service_override(self):
        self._settings(min_line_amount=500)
        # Материал минимумом не облагается: 1 лист — 3 700, кусок — по площади.
        r = self._checkout([{"type": "MATERIAL", "material": self.sheet.id, "quantity": "0.1", "mode": "SQM"}])
        self.assertEqual(Decimal(r.data["total_price"]), Decimal("125"))
        # Своя сумма услуги перекрывает общую: 0 — минимума нет.
        self.engraving.min_line_amount = Decimal("0")
        self.engraving.save()
        r = self._checkout([{"type": "SERVICE", "service": self.engraving.id, "width": "0.1", "length": "0.1"}])
        self.assertEqual(Decimal(r.data["total_price"]), Decimal("30"))
        self.engraving.min_line_amount = Decimal("200")
        self.engraving.save()
        r = self._checkout([{"type": "SERVICE", "service": self.engraving.id, "width": "0.1", "length": "0.1"}])
        self.assertEqual(Decimal(r.data["total_price"]), Decimal("200"))

    def test_minimum_then_urgency_then_discount(self):
        """Порядок: минимум 500 → +20 % = 600 → −10 % = 540."""
        self._settings(min_line_amount=500, urgency_percent=20)
        r = self._checkout(
            [{"type": "SERVICE", "service": self.engraving.id, "width": "0.1", "length": "0.1"}],
            is_urgent=True, discount_percent=10,
        )
        self.assertEqual(Decimal(r.data["total_price"]), Decimal("540"))

    # --- срочность ----------------------------------------------------------
    def test_urgent_refused_while_percent_is_zero(self):
        r = self._checkout(self._sign_items(), is_urgent=True)
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(Receipt.objects.count(), 0)

    def test_rules_endpoint_for_staff(self):
        self._settings(min_line_amount=300, urgency_percent=15)
        self.client.force_authenticate(self.store)
        r = self.client.get("/api/services/rules/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Decimal(r.data["urgency_percent"]), Decimal("15"))
        self.assertNotIn("master_commission_percent", r.data)
        # Править настройки складовщик не может.
        r = self.client.patch("/api/services/settings/", {"urgency_percent": 50}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_settings_validation_and_audit(self):
        self.client.force_authenticate(self.admin)
        r = self.client.patch("/api/services/settings/", {"urgency_percent": -1}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.patch("/api/services/settings/",
                              {"urgency_percent": 30, "min_line_amount": 500}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(AuditLog.objects.filter(action__contains="срочность").exists())

    # --- скидка клиента -----------------------------------------------------
    def test_storekeeper_applies_configured_discount(self):
        r = self._checkout(
            [{"type": "SERVICE", "service": self.install.id, "quantity": 1}],
            user=self.store, client_id=self.regular.id,
        )
        self.assertEqual(r.status_code, 201, r.data)
        # 580 × 0.95 = 551
        self.assertEqual(Decimal(r.data["total_price"]), Decimal("551"))
        self.assertEqual(Decimal(r.data["discount_percent"]), Decimal("5"))
        # Явно ту же скидку — тоже можно.
        r = self._checkout(
            [{"type": "SERVICE", "service": self.install.id, "quantity": 1}],
            user=self.store, client_id=self.regular.id, discount_percent="5",
        )
        self.assertEqual(r.status_code, 201, r.data)

    def test_storekeeper_cannot_set_own_discount(self):
        for value in ("20", "0"):
            r = self._checkout(
                [{"type": "SERVICE", "service": self.install.id, "quantity": 1}],
                user=self.store, client_id=self.regular.id, discount_percent=value,
            )
            self.assertEqual(r.status_code, 403, r.data)
        r = self._checkout(
            [{"type": "SERVICE", "service": self.install.id, "quantity": 1}],
            user=self.store, discount_percent="10",
        )
        self.assertEqual(r.status_code, 403, r.data)
        self.assertEqual(Receipt.objects.count(), 0)

    def test_admin_removes_or_changes_discount_for_order(self):
        r = self._checkout([{"type": "SERVICE", "service": self.install.id, "quantity": 1}],
                           client_id=self.regular.id, discount_percent=0)
        self.assertEqual(Decimal(r.data["total_price"]), Decimal("580"))
        r = self._checkout([{"type": "SERVICE", "service": self.install.id, "quantity": 1}],
                           client_id=self.regular.id, discount_percent="12.5")
        # 580 × 0.875 = 507.5 → 508
        self.assertEqual(Decimal(r.data["total_price"]), Decimal("508"))
        r = self._checkout([{"type": "SERVICE", "service": self.install.id, "quantity": 1}],
                           discount_percent="101")
        self.assertEqual(r.status_code, 400)

    def test_only_admin_sets_client_discount(self):
        self.client.force_authenticate(self.store)
        r = self.client.patch(f"/api/clients/clients/{self.regular.id}/", {"discount_percent": "50"}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        # Новый клиент из кассы со скидкой — тоже нет.
        r = self._checkout(
            [{"type": "SERVICE", "service": self.install.id, "quantity": 1}], user=self.store,
            client={"type": "PHYSICAL", "full_name": "Новый", "phone": "+996555000222",
                    "discount_percent": "30"},
        )
        self.assertEqual(r.status_code, 400, r.data)
        self.client.force_authenticate(self.admin)
        r = self.client.patch(f"/api/clients/clients/{self.regular.id}/", {"discount_percent": "7"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(r.data["discount_percent"]), Decimal("7"))
        r = self.client.patch(f"/api/clients/clients/{self.regular.id}/", {"discount_percent": "120"}, format="json")
        self.assertEqual(r.status_code, 400)

    # --- правка состава и дозаказ сохраняют правила ------------------------
    def test_edit_items_keeps_order_rules(self):
        self._settings(urgency_percent=25)
        r = self._checkout(self._sign_items(), client_id=self.regular.id, is_urgent=True)
        rid = r.data["id"]
        # Владелец меняет настройки и скидку клиента — заказ это не трогает.
        self._settings(urgency_percent=50)
        self.regular.discount_percent = Decimal("0")
        self.regular.save()
        work = TransactionItem.objects.get(receipt_id=rid, service=self.cutting)
        r = self.client.post(f"/api/sales/receipts/{rid}/edit-items/",
                             {"items": [{"id": work.id, "quantity": "4"}]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        # 4 × 600 = 2 400 × 1.25 × 0.95 = 2 850
        work.refresh_from_db()
        self.assertEqual(work.sold_total, Decimal("2850"))
        self.assertEqual(Decimal(r.data["total_price"]), Decimal("2850") + 3563 + 689)
        # Цена в правке — цена до правил.
        r = self.client.post(f"/api/sales/receipts/{rid}/edit-items/",
                             {"items": [{"id": work.id, "price_per_item": "500"}]}, format="json")
        work.refresh_from_db()
        self.assertEqual(work.catalog_price, Decimal("500"))
        # 4 × 500 = 2 000 × 1.1875 = 2 375
        self.assertEqual(work.sold_total, Decimal("2375"))
        receipt = self._assert_total_is_sum_of_lines(rid)
        # Итог вырос после «Вся сумма» — разница стала долгом, как и раньше.
        self.assertEqual(receipt.debt, receipt.total_price - Decimal("6390"))

    def test_add_items_uses_order_rules(self):
        self._settings(urgency_percent=25)
        r = self._checkout([{"type": "SERVICE", "service": self.install.id, "quantity": 1}],
                           client_id=self.regular.id, is_urgent=True)
        self.assertEqual(Decimal(r.data["total_price"]), Decimal("689"))
        rid = r.data["id"]
        self._settings(urgency_percent=0)
        r = self.client.post(f"/api/sales/receipts/{rid}/add-items/",
                             {"items": [{"type": "SERVICE", "service": self.install.id, "quantity": 1}]},
                             format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(r.data["total_price"]), Decimal("1378"))
        self._assert_total_is_sum_of_lines(rid)

    # --- ниже себестоимости -------------------------------------------------
    def test_below_cost_warning_admin_sees_cost(self):
        r = self._checkout([{"type": "MATERIAL", "material": self.sheet.id, "quantity": 1, "mode": "PIECE",
                             "material_price": 3000}])
        self.assertEqual(r.status_code, 201, r.data)
        warn = [w for w in r.data["warnings"] if w["code"] == "below_cost"]
        self.assertEqual(len(warn), 1)
        self.assertEqual(Decimal(warn[0]["cost_total"]), Decimal("3500.00"))
        self.assertTrue(AuditLog.objects.filter(action__contains="ниже себестоимости").exists())

    def test_below_cost_warning_hides_cost_from_storekeeper(self):
        self.regular.discount_percent = Decimal("10")
        self.regular.save()
        # 3 700 − 10 % = 3 330 < 3 500
        r = self._checkout([{"type": "MATERIAL", "material": self.sheet.id, "quantity": 1, "mode": "PIECE"}],
                           user=self.store, client_id=self.regular.id)
        self.assertEqual(r.status_code, 201, r.data)
        warn = [w for w in r.data["warnings"] if w["code"] == "below_cost"]
        self.assertEqual(len(warn), 1)
        self.assertNotIn("cost_total", warn[0])

    def test_no_warning_at_catalog_price(self):
        r = self._checkout([{"type": "MATERIAL", "material": self.sheet.id, "quantity": 1, "mode": "PIECE"}])
        self.assertEqual([w for w in r.data["warnings"] if w["code"] == "below_cost"], [])
