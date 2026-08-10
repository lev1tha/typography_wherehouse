"""«Резка по станкам»: сколько наработал каждый станок и в каком объёме.

Одна сумма не отвечает на вопрос об объёме работы: 12 000 сом — это много
мелких резов или один большой лист. Площадь берём у материала того же чека;
сама работа считается в ПОГОННЫХ метрах (длина реза), складывать их с
площадью нельзя.

Строки блока — СТАНКИ (ЧПУ / лазер), а не типы материала: заказчик считает
работу цеха станками. Резка в разрезе материалов осталась в складском листе.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from sales.models import Receipt
from sales.sale_service import create_sale
from services.models import PrintingService
from warehouse.models import Material, MaterialType


class CuttingAreaTests(APITestCase):
    URL = "/api/finance/report/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin_cut", password="x", role=User.Role.ADMIN
        )
        self.acryl = MaterialType.objects.create(name="Акрил тест")
        # Рулонный: продаётся по кв.м, количество строки — это уже площадь.
        self.roll = Material.objects.create(
            name="Акрил рулон",
            type=self.acryl,
            unit=Material.Unit.SQM,
            quantity=Decimal("500"),
            is_roll_material=True,
            price_per_sqm=Decimal("1000"),
            purchase_price=Decimal("600"),
            cut_rate_per_pm=Decimal("50"),
        )
        # Листовой: продаётся листами, площадь — через площадь листа.
        self.sheet = Material.objects.create(
            name="Акрил лист",
            type=self.acryl,
            unit=Material.Unit.SQM,
            quantity=Decimal("500"),
            price_per_unit=Decimal("0"),
            piece_price=Decimal("3000"),
            piece_area=Decimal("3"),
            cut_rate_per_pm=Decimal("50"),
        )
        # Штучный: площади не имеет вообще.
        self.bolts = Material.objects.create(
            name="Крепёж",
            unit=Material.Unit.PIECE,
            quantity=Decimal("500"),
            price_per_unit=Decimal("18"),
            piece_price=Decimal("18"),
        )
        self.cutting = PrintingService.objects.create(
            name="Резка", kind=PrintingService.Kind.CUTTING,
            machine=PrintingService.Machine.CNC,
        )

    def _cut_sale(self, *, material, width="2", length="3", running_meters="10", extra=None):
        items = [{
            "type": "SERVICE",
            "service": self.cutting,
            "material": material,
            "width": width,
            "length": length,
            "running_meters": running_meters,
        }]
        if extra:
            items.append(extra)
        return create_sale(
            client=None,
            cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=items,
            amount_paid=None,
        )

    def _cutting(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp.data["cutting"]

    def test_area_and_running_metres_are_separate_values(self):
        """Площадь = 2 × 3 кв.м, длина реза = 10 пог.м. Это разные величины:
        по пог.м считается сумма (10 × 50), кв.м описывают сам кусок."""
        self._cut_sale(material=self.roll)
        cutting = self._cutting()
        self.assertEqual(Decimal(str(cutting["area"])), Decimal("6"))
        self.assertEqual(Decimal(str(cutting["running_meters"])), Decimal("10"))
        self.assertEqual(Decimal(str(cutting["total"])), Decimal("500"))

    def test_running_metres_sum_across_orders(self):
        self._cut_sale(material=self.roll, running_meters="10")
        self._cut_sale(material=self.roll, running_meters="4.5")
        self.assertEqual(Decimal(str(self._cutting()["running_meters"])), Decimal("14.5"))

    def test_running_metres_explain_the_amount(self):
        """Сумма = пог.м × ставка материала: цифры под суммой должны её объяснять."""
        self._cut_sale(material=self.roll, running_meters="12")
        cutting = self._cutting()
        pm = Decimal(str(cutting["running_meters"]))
        self.assertEqual(pm * self.roll.cut_rate_per_pm, Decimal(str(cutting["total"])))

    def test_area_sums_across_orders(self):
        self._cut_sale(material=self.roll, width="2", length="3")
        self._cut_sale(material=self.roll, width="1", length="1.5")
        self.assertEqual(Decimal(str(self._cutting()["area"])), Decimal("7.5"))

    def test_whole_sheets_count_by_sheet_area(self):
        """Лист продан целиком — площадь берётся из площади листа."""
        self._cut_sale(
            material=self.sheet,
            width="",
            length="",
            running_meters="8",
            extra={"type": "MATERIAL", "material": self.sheet, "quantity": 2, "mode": "PIECE"},
        )
        # 2 листа × 3 кв.м. Строки работы по резу площади не добавляют.
        self.assertEqual(Decimal(str(self._cutting()["area"])), Decimal("6"))

    def test_piece_material_without_area_is_not_counted(self):
        """Крепёж в кв.м не превращается: у штучного материала площади нет."""
        self._cut_sale(
            material=self.roll,
            width="2",
            length="2",
            extra={"type": "MATERIAL", "material": self.bolts, "quantity": 30, "mode": "PIECE"},
        )
        self.assertEqual(Decimal(str(self._cutting()["area"])), Decimal("4"))

    def test_area_is_broken_down_by_machine(self):
        """Строка отчёта — станок, а не тип материала: заказчик считает работу
        цеха станками. Площадь и пог.м при этом остаются у своей строки."""
        self._cut_sale(material=self.roll, width="2", length="3")
        cutting = self._cutting()
        row = next(r for r in cutting["rows"] if r["name"] == "ЧПУ")
        self.assertEqual(Decimal(str(row["area"])), Decimal("6"))
        self.assertEqual(Decimal(str(row["running_meters"])), Decimal("10"))
        self.assertEqual(Decimal(str(row["amount"])), Decimal("500"))

    def test_two_machines_split_area_by_cut_length(self):
        """В одном чеке два станка — площадь у чека одна, делим её по длине реза.

        Складывать площадь дважды нельзя: материал через цех прошёл один раз, и
        сумма строк должна сойтись с «Резка, всего» и по кв.м тоже.
        """
        laser = PrintingService.objects.create(
            name="Резка лазером",
            kind=PrintingService.Kind.CUTTING,
            machine=PrintingService.Machine.LASER,
        )
        create_sale(
            client=None,
            cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[
                {"type": "SERVICE", "service": self.cutting, "material": self.roll,
                 "width": "2", "length": "3", "running_meters": "6"},
                {"type": "SERVICE", "service": laser, "material": self.roll,
                 "running_meters": "2"},
            ],
            amount_paid=None,
        )
        cutting = self._cutting()
        by_name = {r["name"]: r for r in cutting["rows"]}
        # 8 пог.м всего: ЧПУ 6, лазер 2 → площадь 6 кв.м делится 4.5 / 1.5.
        self.assertEqual(Decimal(str(by_name["ЧПУ"]["running_meters"])), Decimal("6"))
        self.assertEqual(Decimal(str(by_name["Лазер"]["running_meters"])), Decimal("2"))
        self.assertEqual(Decimal(str(by_name["ЧПУ"]["area"])), Decimal("4.5"))
        self.assertEqual(Decimal(str(by_name["Лазер"]["area"])), Decimal("1.5"))
        self.assertEqual(Decimal(str(cutting["area"])), Decimal("6"))

    def test_machine_rate_wins_over_material_rate(self):
        """У станка своя ставка — она и применяется. Иначе выбор «ЧПУ / лазер»
        не менял бы цену, и это читалось бы как поломка."""
        laser = PrintingService.objects.create(
            name="Резка лазером",
            kind=PrintingService.Kind.CUTTING,
            machine=PrintingService.Machine.LASER,
            rate_per_pm=Decimal("90"),
        )
        create_sale(
            client=None,
            cashier=self.admin,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": "SERVICE", "service": laser, "material": self.roll,
                "width": "1", "length": "1", "running_meters": "3",
            }],
            amount_paid=None,
        )
        # 3 пог.м × 90 (ставка станка), а не × 50 (ставка материала).
        self.assertEqual(Decimal(str(self._cutting()["total"])), Decimal("270"))

    def test_area_reaches_the_machine_row_without_running_metres(self):
        """Погонные метры вводятся вручную и могут быть не введены. Площадь всё
        равно должна дойти до строки станка.

        Раньше доля считалась как «пог.м строки / пог.м чека», и при нулевых
        метрах она обнулялась: площадь попадала в «Резка, всего», но ни в одну
        строку станка — «всего 1,56 кв.м», а под ним «ЧПУ 0 кв.м».
        """
        self._cut_sale(material=self.roll, width="1", length="2", running_meters="")
        cutting = self._cutting()
        self.assertEqual(Decimal(str(cutting["area"])), Decimal("2"))
        row = next(r for r in cutting["rows"] if r["name"] == "ЧПУ")
        self.assertEqual(Decimal(str(row["area"])), Decimal("2"))
        self.assertEqual(Decimal(str(row["running_meters"])), Decimal("0"))

    def test_area_is_broken_down_by_employee(self):
        """«Кто сколько отрезал» — вопрос про людей, поэтому в квадратных метрах.

        Считается по тому, КТО ОФОРМИЛ заказ: отдельного поля «мастер за
        станком» в системе нет, и выдавать одно за другое нельзя.
        """
        other = User.objects.create_user(
            username="skl_cut", password="x", role=User.Role.STOREKEEPER
        )
        self._cut_sale(material=self.roll, width="2", length="3")  # admin, 6 кв.м
        create_sale(
            client=None,
            cashier=other,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": "SERVICE", "service": self.cutting, "material": self.roll,
                "width": "1", "length": "2", "running_meters": "4",
            }],
            amount_paid=None,
        )
        by_name = {u["name"]: u for u in self._cutting()["by_user"]}
        self.assertEqual(Decimal(str(by_name["admin_cut"]["area"])), Decimal("6"))
        self.assertEqual(Decimal(str(by_name["skl_cut"]["area"])), Decimal("2"))
        self.assertEqual(Decimal(str(by_name["skl_cut"]["running_meters"])), Decimal("4"))

    def test_employee_area_sums_to_the_total(self):
        """Разбивка по людям обязана сойтись с общей площадью, иначе на вопрос
        «а чьи остальные метры» отвечать нечем."""
        self._cut_sale(material=self.roll, width="2", length="3")
        self._cut_sale(material=self.roll, width="1", length="1")
        cutting = self._cutting()
        total = sum((Decimal(str(u["area"])) for u in cutting["by_user"]), Decimal("0"))
        self.assertEqual(total, Decimal(str(cutting["area"])))

    def test_no_cutting_gives_zero_area(self):
        cutting = self._cutting()
        self.assertEqual(Decimal(str(cutting["total"])), Decimal("0"))
        self.assertEqual(Decimal(str(cutting["area"])), Decimal("0"))
        self.assertEqual(Decimal(str(cutting["running_meters"])), Decimal("0"))
