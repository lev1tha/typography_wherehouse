"""Seed the Cloude system with the two default staff accounts and a small
baseline catalogue so the system is usable immediately after migration.

Usage:
    python manage.py seed
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import User
from services.models import PricingSettings, PrintingService, ServiceRecipe
from warehouse.models import Material


class Command(BaseCommand):
    help = "Создаёт аккаунты по умолчанию и базовый каталог."

    @transaction.atomic
    def handle(self, *args, **options):
        admin, created = User.objects.get_or_create(
            username="admin",
            defaults={"role": User.Role.ADMIN, "is_staff": True, "is_superuser": True},
        )
        if created:
            admin.set_password("admin12345")
            admin.save()
            self.stdout.write(self.style.SUCCESS("Создан администратор: admin / admin12345"))
        else:
            self.stdout.write("Администратор уже существует — пропускаю.")

        keeper, created = User.objects.get_or_create(
            username="storekeeper",
            defaults={"role": User.Role.STOREKEEPER, "is_staff": True},
        )
        if created:
            keeper.set_password("store12345")
            keeper.save()
            self.stdout.write(
                self.style.SUCCESS("Создан складовщик: storekeeper / store12345")
            )
        else:
            self.stdout.write("Складовщик уже существует — пропускаю.")

        # Baseline catalogue
        paper, _ = Material.objects.get_or_create(
            name="Бумага офсетная",
            defaults={
                "category": "Бумага",
                "quantity": Decimal("500"),
                "critical_balance": Decimal("50"),
                "purchase_price": Decimal("30"),
                "price_per_unit": Decimal("50"),
            },
        )
        Material.objects.get_or_create(
            name="Картон матовый",
            defaults={
                "category": "Картон",
                "quantity": Decimal("3"),
                "critical_balance": Decimal("10"),
                "purchase_price": Decimal("80"),
                "price_per_unit": Decimal("120"),
            },
        )
        Material.objects.get_or_create(
            name="Краска чёрная",
            defaults={
                "category": "Краска",
                "quantity": Decimal("40"),
                "critical_balance": Decimal("5"),
                "purchase_price": Decimal("250"),
                "price_per_unit": Decimal("0"),
            },
        )

        # Consumable materials (non-area): glue for volumetric letters, fasteners.
        glue, _ = Material.objects.get_or_create(
            name="Клей",
            defaults={"category": "Расходники", "unit": Material.Unit.LITER,
                      "quantity": Decimal("20"), "critical_balance": Decimal("3"),
                      "purchase_price": Decimal("400"), "price_per_unit": Decimal("0")},
        )
        fasteners, _ = Material.objects.get_or_create(
            name="Крепёж",
            defaults={"category": "Расходники", "unit": Material.Unit.PIECE,
                      "quantity": Decimal("500"), "critical_balance": Decimal("50"),
                      "purchase_price": Decimal("10"), "price_per_unit": Decimal("0")},
        )

        # CUTTING / «работа мастера»: master's labour priced per кв.м. The cut
        # material is billed as a separate line at sale time (see sale_service).
        cutting, _ = PrintingService.objects.get_or_create(
            name="Резка букв",
            defaults={"kind": PrintingService.Kind.CUTTING},
        )
        cutting.kind = PrintingService.Kind.CUTTING
        cutting.rate_flat = Decimal("200")  # работа мастера, сом/кв.м
        cutting.base_price = Decimal("0")
        cutting.save()
        # Cutting no longer auto-consumes recipe materials — the cut material is a
        # separate sale line; drop legacy paper/glue recipes.
        cutting.recipes.all().delete()

        # EXTERIOR installation (per letter). Reuse a legacy "Установка" if present
        # so we don't create a duplicate.
        exterior = (
            PrintingService.objects.filter(name="Установка").first()
            or PrintingService.objects.filter(name="Наружная установка").first()
            or PrintingService(name="Наружная установка")
        )
        exterior.name = "Наружная установка"
        exterior.kind = PrintingService.Kind.INSTALL_EXTERIOR
        exterior.rate_per_piece = Decimal("150")
        exterior.save()
        # Drop any duplicate exterior installs (deactivate if they have sales).
        for dup in PrintingService.objects.filter(name="Наружная установка").exclude(pk=exterior.pk):
            if dup.transaction_items.exists():
                dup.is_active = False
                dup.save()
            else:
                dup.recipes.all().delete()
                dup.delete()

        # INTERIOR installation: priced per кв.м + the chosen material (vinyl/substrate).
        interior, _ = PrintingService.objects.get_or_create(
            name="Внутренняя установка",
            defaults={"kind": PrintingService.Kind.INSTALL_INTERIOR},
        )
        interior.kind = PrintingService.Kind.INSTALL_INTERIOR
        interior.rate_flat = Decimal("100")
        interior.save()

        # A self-adhesive roll material for interior mounting demos.
        film, _ = Material.objects.get_or_create(
            name="Самоклейка",
            defaults={"category": "Плёнка", "unit": Material.Unit.SQM,
                      "is_roll_material": True, "critical_balance": Decimal("5")},
        )
        if not film.price_per_sqm:
            film.price_per_sqm = Decimal("600")
            film.save(update_fields=["price_per_sqm"])

        # Real ЧПУ catalogue (prices from the dealer report, median per кв.м and
        # cutting rate per погонный метр). Area materials: sold by кв.м (вырезка)
        # or whole sheet (piece_price), cut work billed per пог.м at cut_rate.
        # (category, name, sqm_price, cut_rate_per_pm, piece_price, piece_area)
        D = Decimal
        SHEET_AREA = D("2.98")  # лист 1.22×2.44
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
            m, created = Material.objects.get_or_create(
                name=name,
                defaults={
                    "category": cat, "unit": Material.Unit.SQM,
                    "is_roll_material": True, "critical_balance": D("2"),
                    "purchase_price": D("0"),
                    "price_per_sqm": D(sqm), "cut_rate_per_pm": D(cut),
                    "piece_price": D(piece), "piece_area": area,
                },
            )
            if not created and not m.price_per_sqm:
                m.category = cat
                m.price_per_sqm = D(sqm)
                m.cut_rate_per_pm = D(cut)
                m.piece_price = D(piece)
                m.piece_area = area
                m.save(update_fields=["category", "price_per_sqm", "cut_rate_per_pm", "piece_price", "piece_area"])

        # Shop-wide pricing settings (master's wage % of cutting work).
        PricingSettings.objects.get_or_create(pk=1, defaults={"master_commission_percent": Decimal("4")})

        self._fill_translations()
        self.stdout.write(self.style.SUCCESS("Сидинг завершён."))

    def _fill_translations(self):
        """Populate KY / EN translations for the baseline catalogue so the
        language switcher translates dynamic content, not just the chrome."""
        materials = {
            "Бумага офсетная": {
                "name_ky": "Офсеттик кагаз", "name_en": "Offset paper",
                "category_ky": "Кагаз", "category_en": "Paper",
            },
            "Картон матовый": {
                "name_ky": "Күңүрт картон", "name_en": "Matte cardboard",
                "category_ky": "Картон", "category_en": "Cardboard",
            },
            "Краска чёрная": {
                "name_ky": "Кара боёк", "name_en": "Black ink",
                "category_ky": "Боёк", "category_en": "Ink",
            },
        }
        for name_ru, tr in materials.items():
            m = Material.objects.filter(name_ru=name_ru).first()
            if m:
                for field, value in tr.items():
                    setattr(m, field, value)
                m.save()

        svc = PrintingService.objects.filter(name_ru="Резка букв").first()
        if svc:
            svc.name_ky = "Тамгаларды кесүү"
            svc.name_en = "Letter cutting"
            svc.save()
        self.stdout.write("Переводы каталога (KY/EN) обновлены.")
