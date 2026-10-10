"""Исправить цену закупки у уже принятой партии — из командной строки.

Цену при приёмке путают: вбивают цену за лист в поле за кв.м, промахиваются
нулём, берут прайс другой поставки. На проде 20.09 нашлись две такие партии —
форекс 8мм принят по 1 100 вместо 900, а лист золота за 1 сом вместо 2 000.

С 10.10 это обёртка над «Исправить приход» (`warehouse/lot_correction.py`, та
же кнопка в интерфейсе): двигается всё разом — партия, запись прихода в
журнале (закуп), цена карточки, строка накладной и долг поставщику, А ТАКЖЕ
себестоимость уже проданного из партии (по записям партий строк чеков).
Закрытый месяц — отказ.

    python manage.py fix_lot_cost 14 9000            # посмотреть, что изменится
    python manage.py fix_lot_cost 14 9000 --yes      # и применить
"""
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from warehouse.models import Roll


def _som(v) -> str:
    return f"{Decimal(v).quantize(Decimal('0.01')):,}".replace(",", " ")


class Command(BaseCommand):
    help = "Исправить стоимость закупки партии (склад, закуп и цена материала разом)"

    def add_arguments(self, parser):
        parser.add_argument("roll_id", type=int, help="номер партии (Roll.id)")
        parser.add_argument("new_cost", help="правильная сумма закупки партии, сом")
        parser.add_argument(
            "--yes", action="store_true",
            help="применить; без него команда только показывает, что изменится",
        )

    def handle(self, *args, **options):
        try:
            new_cost = Decimal(str(options["new_cost"]))
        except (InvalidOperation, ValueError):
            raise CommandError("Сумма должна быть числом, например 9000 или 2400.50")
        if new_cost < 0:
            raise CommandError("Сумма закупки не может быть отрицательной.")

        try:
            roll = Roll.objects.select_related("material").get(pk=options["roll_id"])
        except Roll.DoesNotExist:
            raise CommandError(f"Партии №{options['roll_id']} нет.")

        from warehouse.lot_correction import CorrectionError, apply, preview

        data = {"purchase_cost": new_cost}
        try:
            plan = preview(roll=roll, data=data)
        except CorrectionError as e:
            raise CommandError(str(e))
        before, after = plan["before"], plan["after"]
        self.stdout.write(f"Партия №{roll.id} · {roll.material.name}")
        self.stdout.write(f"  принято {roll.initial_area} кв.м, осталось {roll.remaining_area}")
        self.stdout.write(
            f"  сумма закупки:  {_som(before['purchase_cost'])} → {_som(after['purchase_cost'])}"
        )
        self.stdout.write(
            f"  цена за кв.м:   {_som(before['cost_per_sqm'])} → {_som(after['cost_per_sqm'])}"
        )
        stock = plan["stock"]
        if stock["purchase_price_before"] != stock["purchase_price_after"]:
            self.stdout.write(
                f"  цена в карточке: {_som(stock['purchase_price_before'])} → "
                f"{_som(stock['purchase_price_after'])}"
            )
        else:
            self.stdout.write("  цена в карточке не меняется — партия не последняя")
        if Decimal(plan["used"]) > 0:
            self.stdout.write(self.style.WARNING(
                f"  из партии уже ушло {plan['used']} кв.м; себестоимость продаж "
                f"меняется на {plan['cogs_delta']} сом (чеков: {len(plan['receipts'])}), "
                f"не пересчитаны старые продажи без партий: {plan['legacy_count']}"
            ))
        for w in plan["warnings"]:
            self.stdout.write(self.style.WARNING(f"  {w['message']}"))
        if plan["closed_months"]:
            self.stdout.write(self.style.ERROR(
                "  закрытые месяцы: " + ", ".join(plan["closed_months"])
            ))
        if not options["yes"]:
            self.stdout.write(self.style.NOTICE("\nНичего не изменено. Повторите с --yes."))
            return
        try:
            apply(roll=roll, data=data)
        except CorrectionError as e:
            raise CommandError(str(e))
        self.stdout.write(self.style.SUCCESS("Готово."))
