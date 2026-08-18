"""«% ЗП мастера» показывается в финотчёте (аудит 2026-08-18, п. 17).

Настройка `PricingSettings.master_commission_percent` существовала и правилась
в «Ценах и услугах», но нигде не считалась: владелец ставил 4 % и ждал цифру,
которой не было. Теперь финотчёт отдаёт расчётную долю от стоимости работы
резки за период — общую и по сотрудникам. Справочно: в прибыль не входит,
зарплаты вносятся записями.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from services.models import PricingSettings, PrintingService
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot


class MasterShareTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="ms_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="ms_store", password="x", role=User.Role.STOREKEEPER)
        self.sheet = Material.objects.create(
            name="Акрил", unit=Material.Unit.SQM, is_roll_material=True,
            sheet_width=Decimal("1.22"), sheet_height=Decimal("2.44"),
            price_per_sqm=Decimal("1500"), cut_rate_per_pm=Decimal("100"),
        )
        receive_lot(self.sheet, form=Roll.Form.SHEET, width=Decimal("1.22"), height=Decimal("2.44"),
                    sheet_count=Decimal("5"), purchase_cost=Decimal("17500"))
        self.cutting = PrintingService.objects.create(name="Резка", kind=PrintingService.Kind.CUTTING, machine="CNC")
        settings = PricingSettings.load()
        settings.master_commission_percent = Decimal("4")
        settings.save()

    def _cut(self, user, metres):
        self.client.force_authenticate(user)
        r = self.client.post("/api/sales/receipts/checkout/", {
            "payment_method": "CASH", "pay_full": True,
            "items": [{"type": "SERVICE", "service": self.cutting.id, "material": self.sheet.id,
                       "width": "0.5", "length": "1", "running_meters": str(metres)}],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def test_report_shows_the_master_share_overall_and_per_user(self):
        self._cut(self.admin, 10)   # работа 1 000
        self._cut(self.store, 5)    # работа 500
        self.client.force_authenticate(self.admin)
        cutting = self.client.get("/api/finance/report/").data["cutting"]
        self.assertEqual(Decimal(str(cutting["total"])), Decimal("1500"))
        self.assertEqual(Decimal(str(cutting["master_commission_percent"])), Decimal("4"))
        self.assertEqual(Decimal(str(cutting["master_share"])), Decimal("60"))     # 4 % от 1 500
        by_user = {u["name"]: u for u in cutting["by_user"]}
        self.assertEqual(Decimal(str(by_user["ms_admin"]["amount"])), Decimal("1000"))
        self.assertEqual(Decimal(str(by_user["ms_admin"]["master_share"])), Decimal("40"))
        self.assertEqual(Decimal(str(by_user["ms_store"]["master_share"])), Decimal("20"))
        # Прибыль от этого не меняется: доля справочная.
        report = self.client.get("/api/finance/report/").data
        self.assertEqual(Decimal(str(report["profit"])), Decimal(str(report["revenue"])) - Decimal(str(report["total_expenses"])))
