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
import re
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
    MaterialType,
    ProductionSite,
    Roll,
)
from warehouse.rolls import consume_area, receive_lot
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
#          сколько заплатили: None = весь заказ в долг, "all" = ровно по счёту,
#          число = столько и принесли — меньше счёта уйдёт в долг, больше в сдачу)
ORDERS = [
    (2,  "Айбек",   "форекс 8мм",              "cut",   D("1.22"), D("2.44"), 14, "all"),
    (3,  "ОсОО Ак", "белый акрил 2,5 мм",      "piece", 4, None, None, "all"),
    (4,  "Нурлан",  "форекс 4,5мм",            "cut",   D("0.60"), D("1.20"), 7,  "all"),
    (7,  "Айбек",   "орг стекло 1,3мм",        "piece", 12, None, None, "all"),
    (9,  "ОсОО Ак", "форекс 8мм",              "cut",   D("1.22"), D("1.22"), 9,  D("1000")),
    # Дал две тысячи с заказа под тысячу — сдачи в кассе не нашлось. Ровно тот
    # случай, ради которого сдача и считается: деньги у цеха, и он их должен.
    (11, "Нурлан",  "ТЕМНО ЖЕЛТЫЙ  2,5ММ",     "cut",   D("0.80"), D("0.60"), 4,  D("2000")),
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


def _site(name: str):
    """Производство из справочника: у заказчика это Бишкек или Глобал."""
    return ProductionSite.objects.filter(name=name).first()


def _aware(day: date):
    return timezone.make_aware(datetime.combine(day, datetime.min.time()))


def _backdate(receipt, moment):
    """Чек и его складские движения — задним числом.

    Журнал пишется в момент списания, то есть «сейчас». Без этого демо-месяц
    выглядел бы странно: чеки за июль, а движения по ним — сегодняшним числом.
    В бою так и надо: материал уходит со склада тогда, когда его отдали.
    """
    Receipt.objects.filter(pk=receipt.pk).update(created_at=moment)
    InventoryLog.objects.filter(receipt=receipt).update(happened_at=moment)


def _backdate_return(receipt, moment):
    """Возврат случился позже продажи — двигаем только записи возврата."""
    InventoryLog.objects.filter(
        receipt=receipt, type=InventoryLog.Type.RETURN
    ).update(happened_at=moment)


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
                    "type": self._type(name),
                    "thickness_mm": self._thickness(name),
                    "color": self._color(name),
                    "article": self._article(name),
                    "sheet_width": width,
                    "sheet_height": height,
                    "unit": Material.Unit.SQM,
                    "is_roll_material": True,
                    "production": _site(production),
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
    def _type(name: str):
        """Тип из справочника. У заказчика он зашит в название, но сама система
        держит его отдельным полем — иначе ни отфильтровать, ни сгруппировать."""
        low = name.lower()
        code = "ACRYL"
        if "форекс" in low:
            code = "FOREX"
        elif "стекло" in low:
            code = "ORGGLASS"
        elif "ромарк" in low:
            code = "ROMARK"
        return MaterialType.objects.filter(code=code).first()

    @staticmethod
    def _thickness(name: str):
        """Толщина из названия — только для сида; в системе это отдельное поле."""
        m = re.search(r"(\d+[.,]?\d*)\s*мм", name, re.IGNORECASE)
        return D(m.group(1).replace(",", ".")) if m else None

    @staticmethod
    def _color(name: str) -> str:
        for color in ("белый", "прозрачный", "жёлтый", "желтый", "красный", "салатовый",
                      "бирюзовый", "синий", "чёрный", "черный", "голубой", "оранжевый",
                      "зеленый", "зелёный", "серебро", "темно жёлтый", "темно желтый"):
            if color in name.lower():
                return color.capitalize()
        return ""

    @staticmethod
    def _article(name: str) -> str:
        """Артикул — три цифры В КОНЦЕ названия («ЖЕЛТЫЙ лимон 2,5ММ 237»).

        Искать их где угодно нельзя: в «Орг стекло 2мм 180*121см» первым
        совпадением идёт 180 из размера, и лист получает артикул из ширины.
        """
        m = re.search(r"(\d{3})\s*$", name)
        return m.group(1) if m else ""

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
            receive_lot(
                material, form=Roll.Form.SHEET,
                purchase_cost=(material.purchase_price * material.piece_area * sheets).quantize(D("0.01")),
                width=width, height=height, sheet_count=D(sheets),
                user=user, received_at=_aware(last_prev),
            )
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

        Дата передаётся прямо в приёмку: поставку задним числом система теперь
        принимает нормально, обходить модель UPDATE-ом больше не нужно.
        """
        for day, name, sheets in INTAKES:
            material = materials[name]
            width, height = self._sheet_size(name)
            receive_lot(
                material, form=Roll.Form.SHEET,
                purchase_cost=(material.purchase_price * material.piece_area * sheets).quantize(D("0.01")),
                width=width, height=height, sheet_count=D(sheets),
                user=user, received_at=_aware(month.replace(day=day)),
            )
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

            # «Оплачено полностью» — флагом, а не заведомо большим числом:
            # сумма чека известна только внутри create_sale, а переплата теперь
            # запоминается сдачей, и прежний сентинел 9 999 999 превращался в
            # девять миллионов сдачи клиенту.
            receipt = sale_service.create_sale(
                client=clients[client_key], cashier=cashier,
                payment_method=Receipt.PaymentMethod.CASH,
                items_data=entries, title=title,
                pay_full=(paid == "all"),
                amount_paid=(paid if paid not in ("all", None) else None),
            )
            _backdate(receipt, _aware(month.replace(day=day)))
            made += 1
        self.stdout.write(f"Заказов: {made} (есть долги и частичные оплаты)")

    def _write_off(self, materials, month, user):
        """Брак — то, что в складском листе заказчика теряется: его формула
        знает только «начало + поступление − проданные»."""
        material = materials["форекс 8мм"]
        # Через consume_area, а НЕ apply_stock_change: форекс рулонный, и его
        # остаток хранится ещё и площадями партий. Прямое изменение числа
        # оставляло партии нетронутыми — демо-база приезжала с расхождением
        # 3 листа между остатком и партиями, то есть показывала заказчику
        # цифры, которые сами с собой не сходятся. Боевое списание брака
        # (POST /materials/write-off/) так и делает.
        consume_area(
            material, material.piece_area * 3,
            log_type=InventoryLog.Type.WRITE_OFF,
            reason="Списание: Брак. Повело при резке, 3 листа.", user=user,
            happened_at=_aware(month.replace(day=23)),
        )
        self.stdout.write("Списание: 3 листа форекса 8мм в брак")

    def _supply_sale_and_refund(self, clients, month, user):
        """Продажа и возврат ШТУЧНОГО материала.

        Ради него сид и держит крепёж: штучный материал ходит мимо партий и
        FIFO, и раньше именно на нём было видно, что журнал не сходится сам с
        собой — возврат он записывал, а продажу нет.
        """
        material, _ = Material.objects.update_or_create(
            name=SUPPLY_NAME,
            defaults={
                "type": MaterialType.objects.filter(code="OTHER").first(),
                "unit": Material.Unit.PIECE,
                "is_roll_material": False, "production": _site("Бишкек"),
                "purchase_price": D(SUPPLY_PRICE), "price_per_unit": D(SUPPLY_RETAIL),
                "critical_balance": D("50"),
            },
        )
        apply_stock_change(
            material, D(SUPPLY_QTY), log_type=InventoryLog.Type.SUPPLY,
            actual_price=D(SUPPLY_PRICE), reason="Поступление от поставщика", user=user,
            happened_at=_aware(month.replace(day=2)),
        )

        receipt = sale_service.create_sale(
            client=clients["Нурлан"], cashier=user,
            payment_method=Receipt.PaymentMethod.CASH,
            items_data=[{
                "type": TransactionItem.Type.MATERIAL, "material": material,
                "mode": TransactionItem.SaleMode.PIECE, "quantity": D("120"),
            }],
            pay_full=True, title=f"{SUPPLY_NAME} — 120 шт",
        )
        _backdate(receipt, _aware(month.replace(day=20)))
        sale_service.refund_receipt(receipt, user=user)
        _backdate_return(receipt, _aware(month.replace(day=21)))
        self.stdout.write(f"Штучный расходник: продажа 120 шт и возврат (заказ №{receipt.order_number})")

    def _refund(self, month, user):
        """Возврат целого заказа: в журнале приход ВОЗВРАТ рядом с расходом
        ПРОДАЖА по тому же чеку — обе стороны сходятся."""
        receipt = (
            Receipt.objects.filter(items__sale_mode=TransactionItem.SaleMode.PIECE)
            .order_by("-created_at").first()
        )
        if not receipt:
            return
        sale_service.refund_receipt(receipt, user=user)
        _backdate_return(receipt, _aware(month.replace(day=25)))
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

        # 1. Журнал движений: сходится ли он с чеками.
        by_type = {
            t: InventoryLog.objects.filter(type=t).count()
            for t in ("SUPPLY", "SALE", "RETURN", "ADJUSTMENT", "WRITE_OFF")
        }
        sold_lines = TransactionItem.objects.filter(
            type=TransactionItem.Type.MATERIAL, material__isnull=False
        ).count()
        returned_lines = TransactionItem.objects.filter(
            type=TransactionItem.Type.MATERIAL, material__isnull=False, is_returned=True
        ).count()
        w(f"1. Складской журнал: приход {by_type['SUPPLY']}, продажа {by_type['SALE']}, "
          f"возврат {by_type['RETURN']}, списание {by_type['WRITE_OFF']}, "
          f"корректировка {by_type['ADJUSTMENT']}.")
        ok = by_type["SALE"] == sold_lines and by_type["RETURN"] == returned_lines
        w(f"   Товарных строк в чеках {sold_lines}, из них возвращено {returned_lines} — "
          f"{'сходится' if ok else 'НЕ СХОДИТСЯ'}.")
        if ok:
            w("   Каждая продажа теперь пишется со ссылкой на чек, и у возврата есть")
            w("   парный расход. Раньше продажи не писались вовсе: _deduct вызывал")
            w("   списание без log_type, а логирование стояло именно под ним, — и")
            w("   на вопрос «куда делся материал» журнал ответить не мог.")
        else:
            w("   Расхождение: движений по продажам не столько, сколько товарных строк.")

        # 2. Приходы датируются моментом ввода.
        w("")
        days = sorted({d for d, *_ in INTAKES})
        w(f"2. Приходы датированы задним числом штатно: дни {days} идут вразнобой,")
        w("   как в Excel заказчика. Раньше InventoryLog и Roll были auto_now_add,")
        w("   и вся июльская поставка, внесённая в августе, уезжала в август —")
        w("   складской лист расходился с его таблицей. Теперь дата передаётся")
        w("   в приёмку, и по ней же выстраивается FIFO.")

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

        # 5. Целые листы должны оставаться целыми после пути через кв.м.
        w("")
        w("5. Целое число листов после пересчёта через кв.м:")
        bad = []
        for name in materials:
            material = fresh[name]
            sheets = sum((D(n) for d, mat, n in INTAKES if mat == name), D("0"))
            if not sheets:
                continue
            got = received_by.get(material.id, {}).get(key, D("0"))
            mark = "ok" if got == sheets else "РАСХОЖДЕНИЕ"
            if got != sheets:
                bad.append(name)
            w(f"   {name:26} принято {sheets:>5} -> показано {got:8.2f}  {mark}")
        if bad:
            w("   Склад хранится в кв.м; если площадь листа округлена грубее, чем")
            w("   нужно, целые листы превращаются в дробь именно в той колонке,")
            w("   которую заказчик сверяет со своим Excel.")
        else:
            w("   Сходится. Площадь листа считается из размера и хранится с")
            w("   точностью до десятитысячных: 1.22 × 2.44 = 2.9768, и 50 листов")
            w("   возвращаются как ровно 50. При двух знаках выходило 49.95.")

        w("")
        w(f"Открыть: Склад → «Остатки по месяцам», выбрать {month.strftime('%m.%Y')};")
        w(f"         Финансы, тот же месяц. Следующий месяц покажет перенос остатка.")
