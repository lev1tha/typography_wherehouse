"""Наполняет систему реалистичным месяцем работы цеха ЧПУ-резки.

Зачем: почти все проблемы, о которых можно спорить теоретически — дрейф
остатков, асимметрия складского журнала, потеря списаний, приходы задним
числом — проявляются ТОЛЬКО на данных. На пустой базе система выглядит
исправной и, заодно, «слишком простой»: у заказчика в Excel сотни строк, а
здесь везде «нет данных».

Данные взяты с реального складского листа заказчика: его номенклатура со всей
её грязью («Орг стекло 2мм 180*121см», «ЖЕЛТЫЙ лимон 2,5ММ 237»), остатки на
начало месяца, приходы теми же датами и в том же беспорядке (01, 10, 14, 19,
05, 06 — он вносит задним числом).

Использование:
    python manage.py seed_demo                # прошлый месяц
    python manage.py seed_demo --month 2026-07
    python manage.py seed_demo --reset        # снести прежние демо-данные

В конце команда печатает диагностику: где цифры уже разъезжаются и почему.
Это не украшение, а главный смысл — увидеть расхождения на данных.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from clients.models import Client
from finance.material_sheet import collect_flows, collect_manual, opening_for, to_units
from finance.models import ExpenseEntry, ExpenseKind
from sales import sale_service
from sales.models import Receipt, TransactionItem
from services.models import PrintingService
from warehouse.models import (
    InventoryLog,
    Material,
    MaterialMonthOpening,
    Roll,
)
from warehouse.rolls import receive_lot
from warehouse.stock import apply_stock_change

D = Decimal
SHEET_122 = D("1.22"), D("2.44")   # стандартный лист форекса/акрила
SHEET_180 = D("1.80"), D("1.21")   # орг стекло 180*121

# Номенклатура заказчика — как он её пишет сам. Цвет, толщина, артикул и размер
# зашиты в название: отдельных полей под них нет, поэтому ни отфильтровать по
# толщине, ни вывести площадь листа из размера система не может.
# (название, производство, ширина, высота, закуп/кв.м, розница/кв.м, ставка резки)
MATERIALS = [
    ("форекс 8мм",               "Бишкек", *SHEET_122, 620, 900, 45),
    ("форекс 4,5мм",             "Бишкек", *SHEET_122, 430, 640, 40),
    ("форекс 3мм",               "Бишкек", *SHEET_122, 330, 500, 35),
    ("форекс 10мм",              "Бишкек", *SHEET_122, 780, 1150, 50),
    ("белый акрил 2,5 мм",       "Бишкек", *SHEET_122, 900, 1350, 60),
    ("белый акрил 3 мм",         "Бишкек", *SHEET_122, 1050, 1550, 65),
    ("белый акрил 1,8 мм",       "Бишкек", *SHEET_122, 720, 1080, 55),
    ("прозрачный акрил 2мм",     "Бишкек", *SHEET_122, 810, 1220, 58),
    ("прозрачный акрил 2,5 мм",  "Бишкек", *SHEET_122, 950, 1420, 60),
    ("ТЕМНО ЖЕЛТЫЙ  2,5ММ",      "Бишкек", *SHEET_122, 980, 1470, 60),
    ("ЖЕЛТЫЙ лимон 2,5ММ 237",   "Бишкек", *SHEET_122, 980, 1470, 60),
    ("ЖЕЛТЫЙ лимон 2,5ММ 235",   "Бишкек", *SHEET_122, 980, 1470, 60),
    ("красный",                  "Бишкек", *SHEET_122, 960, 1440, 60),
    ("салатовый",                "Бишкек", *SHEET_122, 960, 1440, 60),
    ("бирюзовый",                "Бишкек", *SHEET_122, 960, 1440, 60),
    ("синий бишкек",             "Бишкек", *SHEET_122, 940, 1410, 60),
    ("день ночь",                "Бишкек", *SHEET_122, 1100, 1650, 65),
    ("Черный акрил",             "Бишкек", *SHEET_122, 900, 1350, 60),
    ("голубой бишкек",           "Бишкек", *SHEET_122, 940, 1410, 60),
    ("Оранжевый",                "Бишкек", *SHEET_122, 960, 1440, 60),
    ("зеленый",                  "Бишкек", *SHEET_122, 960, 1440, 60),
    ("Орг стекло 3мм",           "Бишкек", *SHEET_122, 1200, 1800, 70),
    ("Орг стекло 2мм 180*121см", "Бишкек", *SHEET_180, 1050, 1580, 65),
    ("орг стекло 1,3мм",         "Бишкек", *SHEET_180, 780, 1170, 55),
    ("ромарк серебро",           "Глобал", *SHEET_122, 2400, 3600, 90),
]

# Остаток на начало месяца, в листах — колонка «остаток в начале месяца».
OPENINGS = {
    "форекс 8мм": 42, "форекс 4,5мм": 43, "форекс 3мм": 14, "форекс 10мм": 30,
    "белый акрил 2,5 мм": 36, "белый акрил 3 мм": 5, "белый акрил 1,8 мм": 4,
    "прозрачный акрил 2мм": 3, "прозрачный акрил 2,5 мм": 10,
    "ТЕМНО ЖЕЛТЫЙ  2,5ММ": 11, "ЖЕЛТЫЙ лимон 2,5ММ 237": 10,
    "ЖЕЛТЫЙ лимон 2,5ММ 235": 1, "салатовый": 10, "бирюзовый": 5,
    "синий бишкек": 2, "день ночь": 3, "голубой бишкек": 2, "Оранжевый": 5,
    "зеленый": 7, "Орг стекло 3мм": 4, "Орг стекло 2мм 180*121см": 3,
    "орг стекло 1,3мм": 17, "ромарк серебро": 5,
}

# Приходы: (день месяца, материал, листов). Дни идут НЕ по порядку — ровно как
# в Excel заказчика: он вносит поставки задним числом, когда доходят руки.
INTAKES = [
    (10, "форекс 8мм", 50),
    (14, "форекс 8мм", 50),
    (19, "форекс 8мм", 5),
    (10, "форекс 4,5мм", 30),
    (10, "форекс 3мм", 15),
    (1,  "белый акрил 2,5 мм", 50),
    (5,  "орг стекло 1,3мм", 59),
    (6,  "ромарк серебро", 10),
]

# Заказы: (день, клиент, материал, режим, количество, погонных метров реза,
#          сколько заплатили: None = весь заказ в долг, "all" = полностью)
ORDERS = [
    (2,  "Айбек",   "форекс 8мм",              "cut",   D("1.22"), D("2.44"), 14, "all"),
    (3,  "ОсОО Ак", "белый акрил 2,5 мм",      "piece", 4, None, None, "all"),
    (4,  "Нурлан",  "форекс 4,5мм",            "cut",   D("0.60"), D("1.20"), 7,  "all"),
    (7,  "Айбек",   "орг стекло 1,3мм",        "piece", 12, None, None, "all"),
    (9,  "ОсОО Ак", "форекс 8мм",              "cut",   D("1.22"), D("1.22"), 9,  D("5000")),
    (11, "Нурлан",  "ТЕМНО ЖЕЛТЫЙ  2,5ММ",     "cut",   D("0.80"), D("0.60"), 4,  "all"),
    (15, "Айбек",   "форекс 8мм",              "piece", 20, None, None, "all"),
    (17, "ОсОО Ак", "прозрачный акрил 2,5 мм", "cut",   D("1.00"), D("2.00"), 11, "all"),
    (18, "Нурлан",  "салатовый",               "cut",   D("0.50"), D("0.50"), 3,  None),
    (22, "Айбек",   "форекс 4,5мм",            "piece", 15, None, None, "all"),
    (24, "ОсОО Ак", "белый акрил 2,5 мм",      "cut",   D("1.22"), D("2.44"), 16, "all"),
    (26, "Нурлан",  "орг стекло 1,3мм",        "cut",   D("0.90"), D("1.10"), 6,  "all"),
    (29, "Айбек",   "форекс 3мм",              "piece", 8, None, None, D("3000")),
]

# Штучный расходник. Нужен не для красоты: возврат штучного материала пишет в
# журнал ADJUSTMENT, а продажа не пишет ничего — на нём и видно асимметрию
# журнала. У рулонных её не увидеть: там молчат обе стороны.
SUPPLY_NAME = "Крепёж"
SUPPLY_QTY = 500
SUPPLY_PRICE = 10
SUPPLY_RETAIL = 18

CLIENTS = [
    ("Айбек", "Асанов Айбек", "+996700111222", Client.Type.PHYSICAL, ""),
    ("Нурлан", "Токтогулов Нурлан", "+996555333444", Client.Type.PHYSICAL, ""),
    ("ОсОО Ак", "Жолдошев Марат", "+996312556677", Client.Type.OSOO, "ОсОО «Ак Жол»"),
]

# Расходы месяца: (код вида, за что, день, сумма)
EXPENSES = [
    ("RENT",        "Аренда цеха за месяц",     5,  45000),
    ("UTILITIES",   "Свет и вода",              8,  9800),
    ("INTERNET",    "Интернет",                 8,  1200),
    ("SALARY",      "Азамат (резчик)",          15, 32000),
    ("SALARY",      "Мирлан (мастер)",          15, 28000),
    ("SALARY",      "Азамат (резчик)",          30, 32000),
    ("CUTTER",      "Фрезы 3.175 мм, 4 шт",     12, 7400),
    ("TRANSPORT",   "Доставка форекса",         10, 3500),
    ("TRANSPORT",   "Доставка акрила",          1,  2800),
    ("FIXED_OTHER", "Вывоз мусора",             20, 1500),
    ("IMPROVEMENT", "Стеллаж под листы",        21, 18000),
]


def _aware(day: date):
    return timezone.make_aware(datetime.combine(day, datetime.min.time()))


class Command(BaseCommand):
    help = "Наполняет систему реалистичным месяцем работы цеха (демо-данные)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--month", help="Месяц в формате YYYY-MM. По умолчанию — прошлый."
        )
        parser.add_argument(
            "--reset", action="store_true",
            help="Снести прежние чеки, приходы, остатки и траты перед наполнением.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        month = self._parse_month(options.get("month"))
        if options["reset"]:
            self._reset()

        admin = User.objects.filter(role=User.Role.ADMIN).first()
        cutting = PrintingService.objects.filter(kind="CUTTING", is_active=True).first()
        if not admin or not cutting:
            raise CommandError("Сначала выполните `python manage.py seed` — нужны админ и услуга резки.")

        materials = self._materials()
        clients = self._clients()
        self._opening_stock(materials, month, admin)
        self._openings(materials, month)
        self._intakes(materials, month, admin)
        self._orders(materials, clients, cutting, month, admin)
        self._write_off(materials, month, admin)
        self._supply_sale_and_refund(clients, month, admin)
        self._refund(month, admin)
        self._expenses(month)

        self._report(materials, month)

    # ---- подготовка ---------------------------------------------------------

    def _parse_month(self, raw) -> date:
        """Первое число целевого месяца. По умолчанию — прошлый месяц: тогда
        текущий остаётся пустым и на нём сразу видно перенос остатка."""
        if raw:
            try:
                year, mon = (int(x) for x in raw.split("-"))
                return date(year, mon, 1)
            except (ValueError, TypeError):
                raise CommandError("Месяц указывается как YYYY-MM, например 2026-07.")
        first_of_this = timezone.localdate().replace(day=1)
        return (first_of_this - timedelta(days=1)).replace(day=1)

    def _reset(self):
        """Сносим оборот и чужой каталог, оставляя виды расхода и аккаунты.

        Материалы из базового `seed` (Бумага офсетная, Алюкобонд и прочее)
        удаляем: в складском листе они висят строками из нулей и превращают
        демо в мусор. Заказчику показывают ЕГО номенклатуру.
        """
        TransactionItem.objects.all().delete()
        Receipt.objects.all().delete()
        InventoryLog.objects.all().delete()
        Roll.objects.all().delete()
        MaterialMonthOpening.objects.all().delete()
        ExpenseEntry.objects.all().delete()
        keep = {name for name, *_ in MATERIALS} | {SUPPLY_NAME}
        removed, _ = Material.objects.exclude(name__in=keep).delete()
        Material.objects.all().update(quantity=D("0"))
        self.stdout.write(f"Прежние демо-данные снесены (материалов удалено: {removed}).")

    def _materials(self) -> dict:
        out = {}
        for name, production, width, height, cost, retail, cut_rate in MATERIALS:
            area = (width * height).quantize(D("0.01"))
            material, _ = Material.objects.update_or_create(
                name=name,
                defaults={
                    "category": self._category(name),
                    "unit": Material.Unit.SQM,
                    "is_roll_material": True,
                    "production": production,
                    "piece_area": area,
                    "purchase_price": D(cost),
                    "price_per_sqm": D(retail),
                    "piece_price": (D(retail) * area).quantize(D("1")),
                    "cut_rate_per_pm": D(cut_rate),
                    "critical_balance": D("3"),
                },
            )
            out[name] = material
        self.stdout.write(f"Материалов в каталоге: {len(out)}")
        return out

    @staticmethod
    def _category(name: str) -> str:
        low = name.lower()
        if "форекс" in low:
            return "Форекс"
        if "акрил" in low:
            return "Акрил"
        if "стекло" in low:
            return "Оргстекло"
        return "Прочее"

    def _clients(self) -> dict:
        out = {}
        for key, full_name, phone, ctype, company in CLIENTS:
            client, _ = Client.objects.update_or_create(
                phone=phone,
                defaults={"full_name": full_name, "type": ctype, "company_name": company},
            )
            out[key] = client
        return out

    # ---- наполнение ---------------------------------------------------------

    def _opening_stock(self, materials, month, user):
        """Реальные партии под остаток на начало месяца, датированные последним
        днём предыдущего.

        Без них склад пуст: `MaterialMonthOpening` — это цифра для отчёта, она
        НЕ создаёт товар. Вписать в лист 11 листов при нулевом складе можно, и
        отчёт покажет 11, а касса на первой же продаже ответит «недостаточно».
        Два независимых числа, ничем не связанные.
        """
        last_prev = month - timedelta(days=1)
        for name, sheets in OPENINGS.items():
            material = materials[name]
            width, height = self._sheet_size(name)
            known = set(InventoryLog.objects.values_list("id", flat=True))
            lot = receive_lot(
                material, form=Roll.Form.SHEET,
                purchase_cost=(material.purchase_price * material.piece_area * sheets).quantize(D("0.01")),
                markup_percent=D("0"),
                width=width, height=height, sheet_count=D(sheets),
                user=user,
            )
            stamp = _aware(last_prev)
            fresh = set(InventoryLog.objects.values_list("id", flat=True)) - known
            InventoryLog.objects.filter(id__in=fresh).update(created_at=stamp)
            Roll.objects.filter(pk=lot.pk).update(received_at=stamp)
        self.stdout.write(f"Складской запас на начало: {len(OPENINGS)} партий ({last_prev})")

    def _openings(self, materials, month):
        for name, sheets in OPENINGS.items():
            MaterialMonthOpening.objects.update_or_create(
                material=materials[name], year=month.year, month=month.month,
                defaults={"quantity": D(sheets)},
            )
        self.stdout.write(f"Остатки на начало месяца в листе: {len(OPENINGS)} материалов")

    def _intakes(self, materials, month, user):
        """Приход партиями, датами не по порядку — как в Excel заказчика.

        Дату приходится проставлять UPDATE-ом: `InventoryLog.created_at` и
        `Roll.received_at` объявлены `auto_now_add`, то есть через интерфейс
        внести поставку задним числом сейчас НЕЛЬЗЯ вообще.
        """
        for day, name, sheets in INTAKES:
            material = materials[name]
            width, height = self._sheet_size(name)
            known_logs = set(InventoryLog.objects.values_list("id", flat=True))
            lot = receive_lot(
                material, form=Roll.Form.SHEET,
                purchase_cost=(material.purchase_price * material.piece_area * sheets).quantize(D("0.01")),
                markup_percent=D("0"),
                width=width, height=height, sheet_count=D(sheets),
                user=user,
            )
            stamp = _aware(month.replace(day=day))
            fresh = set(InventoryLog.objects.values_list("id", flat=True)) - known_logs
            InventoryLog.objects.filter(id__in=fresh).update(created_at=stamp)
            Roll.objects.filter(pk=lot.pk).update(received_at=stamp)
        self.stdout.write(f"Приходов: {len(INTAKES)} (даты вразнобой, как у заказчика)")

    @staticmethod
    def _sheet_size(name):
        for row in MATERIALS:
            if row[0] == name:
                return row[2], row[3]
        return SHEET_122

    def _orders(self, materials, clients, cutting, month, cashier):
        made = 0
        for day, client_key, name, mode, qty, length, running_m, paid in ORDERS:
            material = materials[name]
            if mode == "piece":
                entries = [{
                    "type": TransactionItem.Type.MATERIAL, "material": material,
                    "mode": TransactionItem.SaleMode.PIECE, "quantity": D(qty),
                }]
                title = f"{name} — {qty} листов"
            else:
                entries = [{
                    "type": TransactionItem.Type.SERVICE, "service": cutting,
                    "material": material, "width": qty, "length": length,
                    "running_meters": D(running_m),
                }]
                title = f"Резка: {name}"

            total_guess = None
            if paid == "all":
                total_guess = D("9999999")   # обрежется до суммы чека
            elif paid is not None:
                total_guess = paid

            receipt = sale_service.create_sale(
                client=clients[client_key], cashier=cashier,
                payment_method=Receipt.PaymentMethod.CASH,
                items_data=entries, amount_paid=total_guess, title=title,
            )
            Receipt.objects.filter(pk=receipt.pk).update(created_at=_aware(month.replace(day=day)))
            made += 1
        self.stdout.write(f"Заказов: {made} (есть долги и частичные оплаты)")

    def _write_off(self, materials, month, user):
        """Брак — то, что в складском листе заказчика теряется: его формула
        знает только «начало + поступление − проданные»."""
        material = materials["форекс 8мм"]
        known = set(InventoryLog.objects.values_list("id", flat=True))
        apply_stock_change(
            material, -material.piece_area * 3,
            log_type=InventoryLog.Type.WRITE_OFF,
            reason="Списание: Брак. Повело при резке, 3 листа.", user=user,
        )
        fresh = set(InventoryLog.objects.values_list("id", flat=True)) - known
        InventoryLog.objects.filter(id__in=fresh).update(created_at=_aware(month.replace(day=23)))
        self.stdout.write("Списание: 3 листа форекса 8мм в брак")

    def _supply_sale_and_refund(self, clients, month, user):
        """Продажа и возврат ШТУЧНОГО материала.

        Ради него сид и держит крепёж: продажа штучного лога не пишет, а
        возврат пишет ADJUSTMENT. В журнале появляется приход, которому не
        соответствует ни один расход — журнал не сходится сам с собой.
        """
        material, _ = Material.objects.update_or_create(
            name=SUPPLY_NAME,
            defaults={
                "category": "Прочее", "unit": Material.Unit.PIECE,
                "is_roll_material": False, "production": "Бишкек",
                "purchase_price": D(SUPPLY_PRICE), "price_per_unit": D(SUPPLY_RETAIL),
                "critical_balance": D("50"),
            },
        )
        known = set(InventoryLog.objects.values_list("id", flat=True))
        apply_stock_change(
            material, D(SUPPLY_QTY), log_type=InventoryLog.Type.SUPPLY,
            actual_price=D(SUPPLY_PRICE), reason="Поступление от поставщика", user=user,
        )
        fresh = set(InventoryLog.objects.values_list("id", flat=True)) - known
        InventoryLog.objects.filter(id__in=fresh).update(created_at=_aware(month.replace(day=2)))

        receipt = sale_service.create_sale(
            client=clients["Нурлан"], cashier=user,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": TransactionItem.Type.MATERIAL, "material": material,
                "mode": TransactionItem.SaleMode.PIECE, "quantity": D("120"),
            }],
            amount_paid=D("9999999"), title=f"{SUPPLY_NAME} — 120 шт",
        )
        Receipt.objects.filter(pk=receipt.pk).update(created_at=_aware(month.replace(day=20)))
        sale_service.refund_receipt(receipt, user=user)
        self.stdout.write(f"Штучный расходник: продажа 120 шт и возврат (заказ №{receipt.order_number})")

    def _refund(self, month, user):
        """Возврат — из-за него в журнале появляется приход без парного расхода:
        продажа лога не пишет, а возврат штучного материала пишет."""
        receipt = (
            Receipt.objects.filter(items__sale_mode=TransactionItem.SaleMode.PIECE)
            .order_by("-created_at").first()
        )
        if not receipt:
            return
        sale_service.refund_receipt(receipt, user=user)
        self.stdout.write(f"Возврат: заказ №{receipt.order_number} целиком")

    def _expenses(self, month):
        for code, what, day, amount in EXPENSES:
            kind = ExpenseKind.objects.filter(code=code).first()
            if not kind:
                continue
            ExpenseEntry.objects.create(
                kind=kind, name=what, amount=D(amount),
                spent_at=month.replace(day=day),
            )
        self.stdout.write(f"Расходов: {len(EXPENSES)} записей")

    # ---- диагностика --------------------------------------------------------

    def _report(self, materials, month):
        """Показать, где цифры уже разъезжаются. Это главный смысл команды:
        на пустой базе ни одну из этих проблем не видно."""
        w = self.stdout.write
        w("")
        w(self.style.SUCCESS(f"Готово. Наполнен {month.strftime('%m.%Y')}."))
        w("")
        w(self.style.WARNING("── Что теперь видно на данных ──"))

        # 1. Журнал не знает о продажах.
        by_type = {
            t: InventoryLog.objects.filter(type=t).count()
            for t in ("SUPPLY", "ADJUSTMENT", "WRITE_OFF")
        }
        sales = TransactionItem.objects.filter(
            type=TransactionItem.Type.MATERIAL, material__isnull=False
        ).count()
        w(f"1. Складской журнал: приход {by_type['SUPPLY']}, списание {by_type['WRITE_OFF']}, "
          f"корректировка {by_type['ADJUSTMENT']} — на {sales} товарных строк в чеках.")
        w("   Ни одной записи о продаже: sale_service._deduct вызывает списание без")
        w("   log_type и без reason, а логирование в stock.py/rolls.py стоит именно")
        w("   под ними. «Куда делся материал» журнал не отвечает.")
        w(f"   При этом корректировок {by_type['ADJUSTMENT']} — это возврат штучного крепежа:")
        w("   возврат штучного пишется в журнал, а его продажа нет, и приходу не")
        w("   соответствует ни один расход. У рулонных молчат обе стороны, поэтому")
        w("   на них перекос не виден вовсе.")

        # 2. Приходы датируются моментом ввода.
        w("")
        w("2. Даты приходов пришлось проставлять UPDATE-ом в обход модели:")
        w("   InventoryLog.created_at и Roll.received_at — auto_now_add, то есть")
        w("   через интерфейс поставку задним числом внести НЕЛЬЗЯ. У заказчика")
        w("   в Excel даты идут вразнобой (01, 10, 14, 19, 05, 06) — он так и работает.")

        # 3. Списание выпадает из складского листа. Считаем ровно тем же кодом,
        # которым считает отчёт, и сверяем с фактическим остатком склада.
        w("")
        w("3. Складской лист против фактического остатка (конец месяца):")
        fresh = {m.name: m for m in Material.objects.all()}
        received_by, sold_by = collect_flows(list(fresh.values()))
        key = (month.year, month.month)
        drift = []
        for name in materials:
            material = fresh[name]
            opening, _ = opening_for(
                material.id, key,
                manual=collect_manual([material]), received=received_by, sold=sold_by,
            )
            got = received_by.get(material.id, {}).get(key, D("0"))
            gone = sold_by.get(material.id, {}).get(key, D("0"))
            by_sheet = opening + got - gone
            actual = to_units(material, material.quantity)
            if abs(by_sheet - actual) > D("0.05"):
                drift.append((name, by_sheet, actual, by_sheet - actual))
        if drift:
            for name, by_sheet, actual, gap in drift:
                w(f"   {name:26} лист={by_sheet:8.2f}  склад={actual:8.2f}  разница={gap:+.2f}")
            w("   Это списанный брак: формула заказчика знает только «начало + приход −")
            w("   продано», колонки под брак в его Excel нет. Следующий месяц начнётся")
            w("   с завышенного остатка, пока цифру не поправят руками.")
        else:
            w("   расхождений нет")

        # 4. Лист и склад — два независимых числа.
        w("")
        w("4. Остаток на начало в листе НЕ создаёт товар на складе.")
        w("   MaterialMonthOpening — цифра для отчёта, и только. Вписать 11 листов")
        w("   при пустом складе можно: отчёт покажет 11, а касса на первой продаже")
        w("   ответит «недостаточно». Этот сид пришлось чинить именно так —")
        w("   заводить настоящие партии под остаток на начало.")

        # 5. Целые листы не переживают путешествие через кв.м.
        w("")
        w("5. Приняли целое число листов — в таблице дробь:")
        for name in ("белый акрил 2,5 мм", "орг стекло 1,3мм", "ромарк серебро"):
            material = fresh[name]
            sheets = sum((D(n) for d, mat, n in INTAKES if mat == name), D("0"))
            if not sheets:
                continue
            got = received_by.get(material.id, {}).get(key, D("0"))
            w(f"   {name:26} принято {sheets:>5} -> показано {got:8.2f}")
        w("   Склад хранится в кв.м, а площадь листа округлена до сотых")
        w("   (1.22 × 2.44 = 2.9768, в базе 2.98 — piece_area/initial_area имеют")
        w("   decimal_places=2). Пересчёт листы → кв.м → листы теряет ~0.1%.")
        w("   Заказчик сверяет эту колонку со своим Excel и видит 49,95 вместо 50.")

        w("")
        w(f"Открыть: Склад → «Остатки по месяцам», выбрать {month.strftime('%m.%Y')};")
        w(f"         Финансы, тот же месяц. Следующий месяц покажет перенос остатка.")
