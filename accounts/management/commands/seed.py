"""Наполняет пустую систему: учётные записи и базовый каталог.

Команда ИДЕМПОТЕНТНА и безопасна к повторному запуску на живой базе:

* Учётные записи создаются, только если таблица пользователей ПУСТА (или явно
  указан ``--create-users``), и только те логины, которых ещё нет. Существующих
  пользователей команда не трогает — ни пароль, ни роль. Пароль обязателен и
  берётся из ``--password`` либо переменной окружения ``SEED_PASSWORD``;
  паролей по умолчанию в коде нет. Удалённая учётка сама не воскреснет.
* Каталог (материалы, услуги, переводы) только ДОПОЛНЯЕТСЯ: недостающие позиции
  создаются, а у существующих ни ставки, ни цены, ни техкарты не меняются и
  ничего не удаляется. Если позицию намеренно удалили, а потом снова запустили
  seed — она вернётся: каталог ищется по названию.

Использование:
    SEED_PASSWORD='…' python manage.py seed          # чистая база: учётки + каталог
    python manage.py seed --no-users                 # только каталог
    SEED_PASSWORD='…' python manage.py seed --create-users   # добавить недостающие учётки

На боевой базе (там данные настоящие) запускать не нужно.
"""
import os
from decimal import Decimal

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import User
from services.models import PricingSettings, PrintingService
from warehouse.models import Material, MaterialType

# (логин, роль, суперпользователь, подпись для вывода)
DEFAULT_USERS = [
    ("admin", User.Role.ADMIN, True, "администратор"),
    ("storekeeper", User.Role.STOREKEEPER, False, "складовщик"),
    ("accountant", User.Role.ACCOUNTANT, False, "бухгалтер"),
]


def _type(key):
    """Тип материала из справочника — по коду или по названию категории."""
    return (
        MaterialType.objects.filter(code=key).first()
        or MaterialType.objects.filter(name=key).first()
        or MaterialType.objects.filter(code="OTHER").first()
    )


class Command(BaseCommand):
    help = (
        "Создаёт учётные записи (только на пустой базе или с --create-users) "
        "и дополняет базовый каталог, ничего не перезаписывая."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            help="Пароль для создаваемых учёток. Лучше переменной SEED_PASSWORD: "
            "аргумент виден в списке процессов.",
        )
        parser.add_argument(
            "--create-users",
            action="store_true",
            help="Создать недостающие учётки, даже если пользователи уже есть "
            "(существующие не меняются).",
        )
        parser.add_argument(
            "--no-users",
            action="store_true",
            help="Учётки не создавать вообще — только каталог.",
        )

    def _users_to_create(self, options):
        """Список логинов, которые надо создать, и пароль. Проверка ДО записи в
        базу: ошибка пароля не должна оставить после себя полусозданный seed."""
        if options["no_users"]:
            return None
        if options["create_users"]:
            wanted = list(DEFAULT_USERS)
            missing = [u for u in wanted if not User.objects.filter(username=u[0]).exists()]
        elif User.objects.exists():
            self.stdout.write(
                "Пользователи уже есть — учётки не создаю "
                "(нужны недостающие — запустите с --create-users)."
            )
            return None
        else:
            missing = list(DEFAULT_USERS)
        if not missing:
            self.stdout.write("Все стандартные учётки уже существуют — пропускаю.")
            return None
        password = options.get("password") or os.environ.get("SEED_PASSWORD", "")
        if not password:
            raise CommandError(
                "Для создания учёток нужен пароль: переменная окружения "
                "SEED_PASSWORD или --password. Стандартных паролей в коде нет. "
                "Только каталог, без учёток — флаг --no-users."
            )
        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError("Пароль не подходит: " + " ".join(exc.messages))
        return missing, password

    @transaction.atomic
    def handle(self, *args, **options):
        plan = self._users_to_create(options)
        if plan:
            missing, password = plan
            for username, role, is_super, label in missing:
                user, created = User.objects.get_or_create(
                    username=username,
                    defaults={"role": role, "is_staff": True, "is_superuser": is_super},
                )
                if created:
                    user.set_password(password)
                    user.save()
                    self.stdout.write(self.style.SUCCESS(f"Создан {label}: {username}"))
                else:
                    self.stdout.write(f"{label.capitalize()} уже существует — пропускаю.")

        # Baseline catalogue
        paper, _ = Material.objects.get_or_create(
            name="Бумага офсетная",
            defaults={
                "type": _type("OTHER"),
                "quantity": Decimal("500"),
                "critical_balance": Decimal("50"),
                "purchase_price": Decimal("30"),
                "price_per_unit": Decimal("50"),
            },
        )
        Material.objects.get_or_create(
            name="Картон матовый",
            defaults={
                "type": _type("OTHER"),
                "quantity": Decimal("3"),
                "critical_balance": Decimal("10"),
                "purchase_price": Decimal("80"),
                "price_per_unit": Decimal("120"),
            },
        )
        Material.objects.get_or_create(
            name="Краска чёрная",
            defaults={
                "type": _type("OTHER"),
                "quantity": Decimal("40"),
                "critical_balance": Decimal("5"),
                "purchase_price": Decimal("250"),
                "price_per_unit": Decimal("0"),
            },
        )

        # Consumable materials (non-area): glue for volumetric letters, fasteners.
        glue, _ = Material.objects.get_or_create(
            name="Клей",
            defaults={"type": _type("OTHER"), "unit": Material.Unit.LITER,
                      "quantity": Decimal("20"), "critical_balance": Decimal("3"),
                      "purchase_price": Decimal("400"), "price_per_unit": Decimal("0")},
        )
        fasteners, _ = Material.objects.get_or_create(
            name="Крепёж",
            defaults={"type": _type("OTHER"), "unit": Material.Unit.PIECE,
                      "quantity": Decimal("500"), "critical_balance": Decimal("50"),
                      "purchase_price": Decimal("10"), "price_per_unit": Decimal("0")},
        )

        # CUTTING / «работа мастера»: master's labour priced per кв.м. The cut
        # material is billed as a separate line at sale time (see sale_service).
        # Ищем по (вид, станок), а не по названию: владелец мог переименовать
        # услугу, и тогда по имени seed завёл бы вторую. Существующую услугу
        # НЕ ТРОГАЕМ — ставку владелец задаёт сам («Цены и услуги»).
        if not PrintingService.objects.filter(
            kind=PrintingService.Kind.CUTTING, machine=PrintingService.Machine.CNC
        ).exists():
            PrintingService.objects.create(
                name="Резка букв",
                kind=PrintingService.Kind.CUTTING,
                machine=PrintingService.Machine.CNC,
                rate_flat=Decimal("200"),  # работа мастера, сом/кв.м
                base_price=Decimal("0"),
            )
        # Второй станок — лазер. Резка считается по станкам (ЧПУ / лазер), и на
        # чистой базе второй должен быть сразу: иначе отчёт покажет одну строку
        # и разделение выглядит несделанным. Ставка 0 — берётся у материала.
        PrintingService.objects.get_or_create(
            machine=PrintingService.Machine.LASER,
            kind=PrintingService.Kind.CUTTING,
            defaults={"name": "Резка лазером", "base_price": Decimal("0")},
        )
        # Гравировка — цена за кв.м, материал отдельной строкой не идёт. Ставку
        # владелец задаёт сам («Цены и услуги»), в кассе её правят по заказу.
        PrintingService.objects.get_or_create(
            kind=PrintingService.Kind.ENGRAVING,
            defaults={"name": "Гравировка", "rate_flat": Decimal("0")},
        )
        # Отходы — мерку (кв.м / пог.м / шт) и цену называют в кассе: отходы
        # бывают от любого товара, и цена на них всегда договорная.
        PrintingService.objects.get_or_create(
            kind=PrintingService.Kind.WASTE,
            defaults={"name": "Отходы", "rate_flat": Decimal("0")},
        )

        # A self-adhesive roll material (was used for interior mounting demos).
        Material.objects.get_or_create(
            name="Самоклейка",
            defaults={"type": _type("FILM"), "unit": Material.Unit.SQM,
                      "is_roll_material": True, "critical_balance": Decimal("5"),
                      "price_per_sqm": Decimal("600")},
        )

        # Real ЧПУ catalogue (prices from the dealer report, median per кв.м and
        # cutting rate per погонный метр). Area materials: sold by кв.м (вырезка)
        # or whole sheet (piece_price), cut work billed per пог.м at cut_rate.
        # (тип, name, sqm_price, cut_rate_per_pm, piece_price, piece_area)
        D = Decimal
        # Стандартный лист 1.22 × 2.44 м. Размер задаём ЯВНО, а не одной
        # площадью: по размеру касса и приход подставляют ширину/высоту, а
        # площадь считается из него с четырьмя знаками (2.9768), как везде.
        # Без размеров накладная на «Лист» отвечала «не из чего посчитать
        # количество», и приход приходилось вбивать руками.
        SHEET_W, SHEET_H = D("1.22"), D("2.44")
        SHEET_AREA = SHEET_W * SHEET_H  # 2.9768
        catalogue = [
            # Акрил (цвет = отдельный товар), резка 20 сом/пог.м
            ("Акрил", "Белый акрил", "1250", "20", "3700", SHEET_AREA),
            ("Акрил", "Прозрачный акрил", "1250", "15", "3700", SHEET_AREA),
            ("Акрил", "Жёлтый акрил", "1250", "20", "3700", SHEET_AREA),
            ("Акрил", "Красный акрил", "1250", "20", "3700", SHEET_AREA),
            ("Акрил", "Зелёный акрил", "1250", "20", "3700", SHEET_AREA),
            ("Акрил", "Чёрный акрил", "1250", "15", "3700", SHEET_AREA),
            ("Акрил", "Синий акрил", "1250", "20", "3700", SHEET_AREA),
            ("Акрил", "Золото акрил 1мм", "950", "20", "2800", SHEET_AREA),
            ("Акрил", "Золото акрил 2мм", "1650", "20", "4900", SHEET_AREA),
            # Форекс (толщина = отдельный товар), резка 15 сом/пог.м
            ("Форекс", "Форекс 3мм", "226", "15", "0", SHEET_AREA),
            ("Форекс", "Форекс 4.5мм", "278", "15", "0", SHEET_AREA),
            ("Форекс", "Форекс 8мм", "385", "15", "0", SHEET_AREA),
            # Алюкобонд, резка 15
            ("Алюкобонд", "Белый алюкобонд", "1250", "15", "0", SHEET_AREA),
            # Прочее
            ("Оргстекло", "Оргстекло", "550", "20", "0", SHEET_AREA),
            ("Пластик", "Золото пластик", "1000", "15", "0", SHEET_AREA),
        ]
        for cat, name, sqm, cut, piece, area in catalogue:
            # Только создание: у существующего материала цены и размеры
            # принадлежат владельцу, seed их не «дописывает» и не «чинит».
            Material.objects.get_or_create(
                name=name,
                defaults={
                    "type": _type(cat), "unit": Material.Unit.SQM,
                    "is_roll_material": True, "critical_balance": D("2"),
                    "purchase_price": D("0"),
                    "price_per_sqm": D(sqm), "cut_rate_per_pm": D(cut),
                    "piece_price": D(piece), "piece_area": area,
                    "sheet_width": SHEET_W, "sheet_height": SHEET_H,
                },
            )

        # Shop-wide pricing settings (master's wage % of cutting work).
        PricingSettings.objects.get_or_create(pk=1, defaults={"master_commission_percent": Decimal("4")})

        self._fill_translations()
        self.stdout.write(self.style.SUCCESS("Сидинг завершён."))

    def _fill_translations(self):
        """Populate KY / EN translations for the baseline catalogue so the
        language switcher translates dynamic content, not just the chrome.
        Заполняются только ПУСТЫЕ поля: правки владельца не затираются."""
        materials = {
            "Бумага офсетная": {
                "name_ky": "Офсеттик кагаз", "name_en": "Offset paper",
            },
            "Картон матовый": {
                "name_ky": "Күңүрт картон", "name_en": "Matte cardboard",
            },
            "Краска чёрная": {
                "name_ky": "Кара боёк", "name_en": "Black ink",
            },
        }
        for name_ru, tr in materials.items():
            m = Material.objects.filter(name_ru=name_ru).first()
            if m:
                changed = [f for f, v in tr.items() if not getattr(m, f, None)]
                for field in changed:
                    setattr(m, field, tr[field])
                if changed:
                    m.save(update_fields=changed)

        svc = PrintingService.objects.filter(name_ru="Резка букв").first()
        if svc:
            changed = []
            if not svc.name_ky:
                svc.name_ky = "Тамгаларды кесүү"
                changed.append("name_ky")
            if not svc.name_en:
                svc.name_en = "Letter cutting"
                changed.append("name_en")
            if changed:
                svc.save(update_fields=changed)
        self.stdout.write("Переводы каталога (KY/EN) дополнены.")
