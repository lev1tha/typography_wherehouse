"""Общая оснастка для тестов калькулятора и кассы (2026-10-10).

Не содержит тестов: здесь листовые материалы с партиями, услуги и помощники
запросов, на которых стоят `tests_calc_*.py`, `tests_quotes.py`,
`tests_cash_ops.py` и `tests_issue_warranty.py`.
"""
from decimal import Decimal as D

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt
from services.models import PricingSettings, PrintingService
from warehouse.models import Material, Roll
from warehouse.rolls import receive_lot

CHECKOUT = "/api/sales/receipts/checkout/"
PREVIEW = "/api/sales/receipts/preview/"
RECEIPTS = "/api/sales/receipts/"


class CalcBase(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="c_admin", password="x", role=User.Role.ADMIN)
        self.store = User.objects.create_user(username="c_store", password="x", role=User.Role.STOREKEEPER)
        self.accountant = User.objects.create_user(username="c_acc", password="x", role=User.Role.ACCOUNTANT)
        self.today = timezone.localdate()

        def sheet(name, ps, cut, thickness, cost_sheet, n=10, w="1.22", h="2.44"):
            m = Material.objects.create(
                name=name, unit=Material.Unit.SQM, is_roll_material=True, thickness_mm=D(thickness),
                sheet_width=D(w), sheet_height=D(h), price_per_sqm=D(ps), cut_rate_per_pm=D(cut),
                piece_price=(D(ps) * D(w) * D(h)).quantize(D("1")),
            )
            lot = receive_lot(
                m, form=Roll.Form.SHEET, width=D(w), height=D(h), sheet_count=D(n),
                purchase_cost=D(cost_sheet) * n,
            )
            return m, lot

        self.acr3, self.acr3_lot = sheet("белый акрил 3 мм", 1550, 65, 3, 2768)
        self.forex3, _ = sheet("форекс 3 мм", 500, 35, 3, 900)
        self.forex8, _ = sheet("форекс 8 мм", 800, 45, 8, 1600)
        # Миграции заводят свои услуги резки; в тестах — ровно по одной на станок.
        PrintingService.objects.filter(kind="CUTTING").delete()
        self.cnc = PrintingService.objects.create(name="Резка ЧПУ", kind="CUTTING", machine="CNC")
        self.laser = PrintingService.objects.create(name="Резка лазер", kind="CUTTING", machine="LASER")
        self.engr = PrintingService.objects.create(name="Гравировка", kind="ENGRAVING", rate_flat=D("3000"))
        self.mont = PrintingService.objects.create(name="Монтаж", kind="OTHER", base_price=D("2854"))
        self.letters = PrintingService.objects.create(
            name="Наружные буквы", kind="INSTALL_EXTERIOR", rate_per_piece=D("150")
        )
        self.regular = Client.objects.create(full_name="Постоянный", phone="+996555000111", discount_percent=D("5"))
        self.ivan = Client.objects.create(full_name="Иванов", phone="+996555000222")

    # --- помощники ----------------------------------------------------------
    def settings_(self, **kw):
        s = PricingSettings.load()
        for key, value in kw.items():
            setattr(s, key, D(str(value)) if isinstance(value, (int, float)) else value)
        s.save()
        return s

    def co(self, items, user=None, **extra):
        """Оформить заказ «заплатил ровно сколько вышло»."""
        self.client.force_authenticate(user or self.admin)
        body = {"payment_method": "CASH", "pay_full": True, "items": items}
        body.update(extra)
        return self.client.post(CHECKOUT, body, format="json")

    def preview(self, items, user=None, **extra):
        self.client.force_authenticate(user or self.admin)
        body = {"payment_method": "CASH", "pay_full": True, "items": items}
        body.update(extra)
        return self.client.post(PREVIEW, body, format="json")

    def cut(self, mat, w, length, rm, svc=None, **kw):
        item = {
            "type": "SERVICE", "service": (svc or self.cnc).id, "material": mat.id,
            "width": str(w), "length": str(length), "running_meters": str(rm),
        }
        item.update(kw)
        return item

    def lines(self, response):
        """[(тип, количество, цена за единицу, итог строки)] ответа."""
        return [
            (i["type"], D(str(i["quantity"])), D(str(i["price_per_item"])), D(str(i["line_total"])))
            for i in response.data["items"]
        ]

    def receipt(self, response):
        return Receipt.objects.get(pk=response.data["id"])
