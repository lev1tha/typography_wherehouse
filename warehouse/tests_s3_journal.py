"""S3 перепроверки владельца 10.10 — журнал действий (XL-07, часть склада):
остаток склада на начало месяца (`MaterialMonthOpening`) пишется «было → стало»."""
from audit.models import AuditLog
from warehouse.models import MaterialMonthOpening
from warehouse.tests_recheck_stock import Base

URL = "/api/warehouse/month-openings/"


class MonthOpeningJournalTests(Base):
    def entries(self):
        return list(AuditLog.objects.filter(action__contains="на начало").order_by("id"))

    def test_new_cell_changed_cell_and_deleted_cell(self):
        r = self.client.post(URL, {"material": self.m.id, "year": 2026, "month": 9, "quantity": "10"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        r = self.client.post(URL, {"material": self.m.id, "year": 2026, "month": 9, "quantity": "12,5"},
                             format="json")
        self.assertEqual(r.status_code, 200, r.data)
        row = MaterialMonthOpening.objects.get()
        r = self.client.patch(f"{URL}{row.id}/", {"quantity": "11"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.client.delete(f"{URL}{row.id}/").status_code, 204)
        texts = [e.action for e in self.entries()]
        self.assertEqual(len(texts), 4, texts)
        self.assertIn("«акрил прозрачный 3 мм»", texts[0])
        self.assertIn("09.2026", texts[0])
        self.assertIn("— → 10", texts[0])
        self.assertIn("10 → 12,5", texts[1])
        self.assertIn("12,5 → 11", texts[2])
        self.assertIn("11 → —", texts[3])
        self.assertTrue(all(e.kind == "stock" and e.user_id == self.admin.id for e in self.entries()))

    def test_same_value_writes_nothing(self):
        for _ in range(2):
            self.client.post(URL, {"material": self.m.id, "year": 2026, "month": 9, "quantity": "10"}, format="json")
        self.assertEqual(len(self.entries()), 1)
