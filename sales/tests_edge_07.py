from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from finance.models import Expense, FinanceSettings
from sales.models import Receipt, TransactionItem
from services.models import PrintingService
from warehouse.models import Material


class EdgeFinanceTests(APITestCase):
    """Edge-cases for FinanceReportView (/api/finance/report/) and
    MaterialReportView (/api/finance/material-report/).

    Чеки строятся напрямую через ORM, чтобы точно контролировать
    payment_status / status / amount_paid / refunded_amount, минуя бизнес-логику
    чекаута. Отчёты дёргаются по HTTP под админом.
    """

    REPORT_URL = "/api/finance/report/"
    MATERIAL_REPORT_URL = "/api/finance/material-report/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="fin_admin", password="x", role=User.Role.ADMIN
        )
        self.store = User.objects.create_user(
            username="fin_store", password="x", role=User.Role.STOREKEEPER
        )
        self.client.force_authenticate(self.admin)

        self.client_obj = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Заказчик", phone="+700001"
        )

        # Материалы разных категорий (категория определяется по name в _material_category).
        self.forex = Material.objects.create(
            name="Форекс 3мм", category="Пластик", unit="SQM",
            quantity=Decimal("10"), purchase_price=Decimal("50"),
            price_per_unit=Decimal("0"), piece_area=Decimal("2.98"),
        )
        self.acryl = Material.objects.create(
            name="Акрил 5мм", category="Акрил", unit="SQM",
            quantity=Decimal("3"), purchase_price=Decimal("100"),
            price_per_unit=Decimal("0"), piece_area=Decimal("2.98"),
        )

        self.cutting = PrintingService.objects.create(
            name="Резка", kind=PrintingService.Kind.CUTTING, rate_flat=Decimal("20"),
        )

    # ---- helpers -----------------------------------------------------------
    def _receipt(self, *, payment_status, status=Receipt.Status.COMPLETED,
                 total="0", amount_paid="0", refunded="0", client=True):
        return Receipt.objects.create(
            client=self.client_obj if client else None,
            payment_status=payment_status,
            status=status,
            total_price=Decimal(total),
            amount_paid=Decimal(amount_paid),
            refunded_amount=Decimal(refunded),
        )

    def _material_item(self, receipt, material, *, qty, price="0",
                       mode=TransactionItem.SaleMode.SQM, is_returned=False):
        return TransactionItem.objects.create(
            receipt=receipt,
            type=TransactionItem.Type.MATERIAL,
            material=material,
            quantity=Decimal(qty),
            price_per_item=Decimal(price),
            sale_mode=mode,
            is_returned=is_returned,
        )

    def _cutting_item(self, receipt, *, qty="1", price="200", is_returned=False):
        return TransactionItem.objects.create(
            receipt=receipt,
            type=TransactionItem.Type.SERVICE,
            service=self.cutting,
            quantity=Decimal(qty),
            price_per_item=Decimal(price),
            is_returned=is_returned,
        )

    def _report(self):
        resp = self.client.get(self.REPORT_URL)
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp.data

    def _material_rows(self):
        resp = self.client.get(self.MATERIAL_REPORT_URL)
        self.assertEqual(resp.status_code, 200, resp.data)
        return {row["id"]: row for row in resp.data["rows"]}

    # ---- revenue -----------------------------------------------------------
    def test_revenue_paid_plus_pending_prepay_excludes_cancelled(self):
        self._receipt(payment_status=Receipt.PaymentStatus.PAID, total="1000",
                      amount_paid="1000")
        self._receipt(payment_status=Receipt.PaymentStatus.PENDING, total="800",
                      amount_paid="300")
        # CANCELLED, даже если был PAID — НЕ должен входить в выручку.
        self._receipt(payment_status=Receipt.PaymentStatus.PAID,
                      status=Receipt.Status.CANCELLED, total="5000", amount_paid="5000")

        data = self._report()
        # 1000 (PAID total) + 300 (PENDING предоплата) = 1300.
        self.assertEqual(Decimal(str(data["revenue"])), Decimal("1300"))

    def test_pending_contributes_prepay_not_total(self):
        self._receipt(payment_status=Receipt.PaymentStatus.PENDING, total="1000",
                      amount_paid="300")
        data = self._report()
        # В выручку идёт только предоплата 300, не вся сумма чека.
        self.assertEqual(Decimal(str(data["revenue"])), Decimal("300"))

    # ---- materials block removed -------------------------------------------
    def test_report_has_no_materials_block(self):
        # Раздел «Материалы» убран из отчёта (транспорт входит в цену закупки),
        # поэтому ключа "materials" в ответе больше нет.
        data = self._report()
        self.assertNotIn("materials", data)

    # ---- client_debt -------------------------------------------------------
    def test_client_debt_only_positive_pending(self):
        # PENDING: 1000 - 300 - 0 = 700 долга.
        self._receipt(payment_status=Receipt.PaymentStatus.PENDING, total="1000",
                      amount_paid="300")
        # PAID не создаёт долг.
        self._receipt(payment_status=Receipt.PaymentStatus.PAID, total="500",
                      amount_paid="500")
        # Переплаченный PENDING → долг 0, без отрицательного вклада.
        self._receipt(payment_status=Receipt.PaymentStatus.PENDING, total="200",
                      amount_paid="500")
        # CANCELLED PENDING не учитывается.
        self._receipt(payment_status=Receipt.PaymentStatus.PENDING,
                      status=Receipt.Status.CANCELLED, total="9000", amount_paid="0")
        data = self._report()
        self.assertEqual(Decimal(str(data["client_debt"])), Decimal("700"))

    def test_client_debt_subtracts_refund(self):
        # 1000 - 200 - 300 = 500.
        self._receipt(payment_status=Receipt.PaymentStatus.PENDING, total="1000",
                      amount_paid="200", refunded="300")
        data = self._report()
        self.assertEqual(Decimal(str(data["client_debt"])), Decimal("500"))

    # ---- cutting split by material category --------------------------------
    def test_cutting_split_by_material_category(self):
        # Чек с резкой + материал Форекс → резка идёт в forex.
        r1 = self._receipt(payment_status=Receipt.PaymentStatus.PAID, total="555",
                           amount_paid="555")
        self._cutting_item(r1, qty="1", price="200")
        self._material_item(r1, self.forex, qty="1", price="355")
        # Чек с резкой + материал Акрил → резка в acryl.
        r2 = self._receipt(payment_status=Receipt.PaymentStatus.PAID, total="150",
                           amount_paid="150")
        self._cutting_item(r2, qty="1", price="150")
        self._material_item(r2, self.acryl, qty="1", price="0")
        # Чек с резкой БЕЗ материала → other.
        r3 = self._receipt(payment_status=Receipt.PaymentStatus.PAID, total="90",
                           amount_paid="90")
        self._cutting_item(r3, qty="1", price="90")

        data = self._report()
        cutting = data["cutting"]
        self.assertEqual(Decimal(str(cutting["forex"])), Decimal("200"))
        self.assertEqual(Decimal(str(cutting["acryl"])), Decimal("150"))
        self.assertEqual(Decimal(str(cutting["other"])), Decimal("90"))
        self.assertEqual(Decimal(str(cutting["alukobond"])), Decimal("0"))
        self.assertEqual(Decimal(str(cutting["total"])), Decimal("440"))

    def test_cutting_excludes_cancelled_receipt(self):
        r = self._receipt(payment_status=Receipt.PaymentStatus.PAID,
                          status=Receipt.Status.CANCELLED, total="200", amount_paid="200")
        self._cutting_item(r, qty="1", price="200")
        self._material_item(r, self.forex, qty="1", price="0")
        data = self._report()
        self.assertEqual(Decimal(str(data["cutting"]["total"])), Decimal("0"))
        self.assertEqual(Decimal(str(data["cutting"]["forex"])), Decimal("0"))

    # ---- material-report: PIECE vs SQM ------------------------------------
    def test_material_report_piece_mode_area_and_sheets(self):
        r = self._receipt(payment_status=Receipt.PaymentStatus.PAID, total="0")
        # PIECE: 2 листа, piece_area=2.98 → sheets=2, area=5.96, mat_rev=2×3700.
        self._material_item(r, self.forex, qty="2", price="3700",
                            mode=TransactionItem.SaleMode.PIECE)
        rows = self._material_rows()
        row = rows[self.forex.id]
        self.assertEqual(Decimal(str(row["sold_sheets"])), Decimal("2"))
        self.assertEqual(Decimal(str(row["sold_area"])), Decimal("5.96"))
        self.assertEqual(Decimal(str(row["material_revenue"])), Decimal("7400"))
        self.assertEqual(row["orders"], 1)
        self.assertEqual(row["category"], "forex")

    def test_material_report_sqm_mode_area_and_sheets(self):
        r = self._receipt(payment_status=Receipt.PaymentStatus.PAID, total="0")
        # SQM: 5.96 кв.м, piece_area=2.98 → area=5.96, sheets=5.96/2.98=2.
        self._material_item(r, self.forex, qty="5.96", price="1400",
                            mode=TransactionItem.SaleMode.SQM)
        rows = self._material_rows()
        row = rows[self.forex.id]
        self.assertEqual(Decimal(str(row["sold_area"])), Decimal("5.96"))
        self.assertEqual(Decimal(str(row["sold_sheets"])), Decimal("2"))

    def test_material_report_sqm_zero_piece_area_no_division(self):
        no_area = Material.objects.create(
            name="Прочий", category="Прочее", unit="SQM",
            quantity=Decimal("5"), purchase_price=Decimal("10"),
            piece_area=Decimal("0"),
        )
        r = self._receipt(payment_status=Receipt.PaymentStatus.PAID, total="0")
        self._material_item(r, no_area, qty="4", price="100",
                            mode=TransactionItem.SaleMode.SQM)
        rows = self._material_rows()
        row = rows[no_area.id]
        self.assertEqual(Decimal(str(row["sold_area"])), Decimal("4"))
        # piece_area=0 → sheets не считаются (нет деления на ноль).
        self.assertEqual(Decimal(str(row["sold_sheets"])), Decimal("0"))

    def test_material_report_orders_counts_unique_receipts(self):
        r1 = self._receipt(payment_status=Receipt.PaymentStatus.PAID, total="0")
        r2 = self._receipt(payment_status=Receipt.PaymentStatus.PAID, total="0")
        # Две позиции одного материала в одном чеке + один в другом → orders=2.
        self._material_item(r1, self.forex, qty="1", price="10",
                            mode=TransactionItem.SaleMode.PIECE)
        self._material_item(r1, self.forex, qty="1", price="10",
                            mode=TransactionItem.SaleMode.PIECE)
        self._material_item(r2, self.forex, qty="1", price="10",
                            mode=TransactionItem.SaleMode.PIECE)
        rows = self._material_rows()
        self.assertEqual(rows[self.forex.id]["orders"], 2)

    def test_material_report_ignores_returned_and_cancelled(self):
        r_ok = self._receipt(payment_status=Receipt.PaymentStatus.PAID, total="0")
        self._material_item(r_ok, self.forex, qty="1", price="100",
                            mode=TransactionItem.SaleMode.PIECE)
        # Возвращённая позиция в нормальном чеке.
        self._material_item(r_ok, self.forex, qty="5", price="999",
                            mode=TransactionItem.SaleMode.PIECE, is_returned=True)
        # Позиция в отменённом чеке.
        r_cancel = self._receipt(payment_status=Receipt.PaymentStatus.PAID,
                                 status=Receipt.Status.CANCELLED, total="0")
        self._material_item(r_cancel, self.forex, qty="7", price="999",
                            mode=TransactionItem.SaleMode.PIECE)
        rows = self._material_rows()
        row = rows[self.forex.id]
        # Учитывается только одна валидная позиция: 1 лист, 100.
        self.assertEqual(Decimal(str(row["sold_sheets"])), Decimal("1"))
        self.assertEqual(Decimal(str(row["material_revenue"])), Decimal("100"))
        self.assertEqual(row["orders"], 1)

    # ---- empty DB ----------------------------------------------------------
    def test_empty_db_report_no_zero_division(self):
        # Удаляем материалы, чтобы склад был пуст. Чеков нет.
        Material.objects.all().delete()
        data = self._report()
        self.assertEqual(Decimal(str(data["revenue"])), Decimal("0"))
        self.assertEqual(Decimal(str(data["client_debt"])), Decimal("0"))
        self.assertEqual(Decimal(str(data["cutting"]["total"])), Decimal("0"))

    def test_empty_db_material_report_returns_empty_rows(self):
        Material.objects.all().delete()
        resp = self.client.get(self.MATERIAL_REPORT_URL)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["rows"], [])

    # ---- expenses feed totals ---------------------------------------------
    def test_variable_expenses_and_profit(self):
        # Singleton row is created lazily by FinanceSettings.load(); use
        # update_or_create so the values persist even on first touch — a bare
        # filter(pk=1).update() is a no-op when the row does not exist yet.
        FinanceSettings.objects.update_or_create(
            pk=1,
            defaults={"material_purchase": Decimal("100"), "rent": Decimal("50")},
        )
        Expense.objects.create(category=Expense.Category.CUTTER, amount=Decimal("30"))
        Expense.objects.create(category=Expense.Category.OTHER, amount=Decimal("20"))
        self._receipt(payment_status=Receipt.PaymentStatus.PAID, total="1000",
                      amount_paid="1000")
        data = self._report()
        self.assertEqual(Decimal(str(data["variable"]["cutter"])), Decimal("30"))
        self.assertEqual(Decimal(str(data["variable"]["other"])), Decimal("20"))
        self.assertEqual(Decimal(str(data["variable"]["total"])), Decimal("50"))
        # «Материалы» убраны из расходов: material_purchase=100 в настройках
        # НЕ должен попадать в total_expenses.
        self.assertEqual(Decimal(str(data["fixed"]["total"])), Decimal("50"))
        # total_expenses = fixed(50) + variable(50) = 100 (без материалов).
        self.assertEqual(Decimal(str(data["total_expenses"])), Decimal("100"))
        # profit = revenue(1000) - total_expenses(100) = 900.
        self.assertEqual(Decimal(str(data["profit"])), Decimal("900"))

    # ---- permissions -------------------------------------------------------
    def test_report_admin_only(self):
        self.client.force_authenticate(self.store)
        self.assertEqual(self.client.get(self.REPORT_URL).status_code, 403)
        self.assertEqual(self.client.get(self.MATERIAL_REPORT_URL).status_code, 403)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(self.REPORT_URL).status_code, 200)
        self.assertEqual(self.client.get(self.MATERIAL_REPORT_URL).status_code, 200)
