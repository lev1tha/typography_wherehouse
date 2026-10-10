"""Выгрузки CSV волны 2: «Чеки со строками» и журнал действий.

Формат «в Excel»: «;», дробная часть через запятую, BOM, все страницы сразу;
себестоимость — только тем, кто видит закупку.
"""
import csv
import io
from decimal import Decimal as D

from accounts.models import Employee
from audit.models import AuditLog
from sales.models import TransactionItem
from sales.tests_cash_ops import CashOpsBase
from services.models import PrintingService


def parse(response):
    text = response.content.decode("utf-8")
    assert text.startswith("﻿"), "нет BOM"
    return list(csv.reader(io.StringIO(text[1:]), delimiter=";"))


class ReceiptsExportTests(CashOpsBase):
    def test_lines_executor_cost_and_all_pages(self):
        master = Employee.objects.create(full_name="Бакыт")
        svc = PrintingService.objects.create(name="Монтаж", kind=PrintingService.Kind.INSTALLATION,
                                             base_price=D("250.50"))
        r = self.sale(3, paid=D("300"), client=self.ivan)
        TransactionItem.objects.create(receipt=r, type="SERVICE", service=svc, quantity=D("1"),
                                       price_per_item=D("250.50"), executor=master)
        for _ in range(30):                                   # больше одной страницы
            self.sale(1, paid=D("100"))
        self.client.force_authenticate(self.admin)
        rows = parse(self.client.get("/api/sales/receipts/export/"))
        header, body = rows[0], rows[1:]
        self.assertIn("Себестоимость, сом", header)
        self.assertEqual(len(body), 32)                       # 31 чек, у одного две строки
        mine = [row for row in body if row[0] == str(r.order_number)]
        self.assertEqual(len(mine), 2)
        work = next(row for row in mine if row[6] == "Монтаж")
        self.assertEqual(work[header.index("Исполнитель")], "Бакыт")
        self.assertEqual(work[header.index("Цена, сом")], "250,50")
        self.assertEqual(work[header.index("Сумма, сом")], "251,00")
        material = next(row for row in mine if row[6] == "")
        self.assertEqual(material[header.index("Себестоимость, сом")], "120,00")

    def test_storekeeper_has_no_cost_and_filters_apply(self):
        self.sale(2, paid=D("200"), client=self.ivan)
        self.sale(1, paid=D("100"))
        self.client.force_authenticate(self.store)
        response = self.client.get("/api/sales/receipts/export/", {"client": self.ivan.id})
        rows = parse(response)
        self.assertNotIn("Себестоимость, сом", rows[0])
        self.assertEqual(len(rows) - 1, 1)


class AuditExportTests(CashOpsBase):
    def test_filters_and_formula_guard(self):
        AuditLog.record(self.admin, "=СУММ(A1) опасная запись", kind="cash")
        AuditLog.record(self.admin, "Обычная запись", kind="order")
        self.client.force_authenticate(self.admin)
        rows = parse(self.client.get("/api/audit/logs/export/", {"kind": "cash"}))
        self.assertEqual(rows[0], ["Дата и время", "Пользователь", "Тип", "Действие"])
        self.assertTrue(all(row[2] == "cash" for row in rows[1:]))
        self.assertTrue(any(row[3].startswith("'=") for row in rows[1:]))
        self.client.force_authenticate(self.store)
        self.assertEqual(self.client.get("/api/audit/logs/export/").status_code, 403)
