"""Исправить цену закупки у уже принятой партии.

Цену при приёмке путают: вбивают цену за лист в поле за кв.м, промахиваются
нулём, берут прайс другой поставки. На проде 20.09 нашлись две такие партии —
форекс 8мм принят по 1 100 вместо 900, а лист золота за 1 сом вместо 2 000.

Почему командой, а не руками в админке. Цена партии лежит в ТРЁХ местах, и
править их по одному — значит развести склад с финотчётом:

  * `Roll.purchase_cost` — по нему считаются стоимость склада и FIFO;
  * `InventoryLog.actual_price` — по нему считается ЗАКУП в финотчёте
    (`purchases_from_stock`), и это отдельная запись;
  * `Material.purchase_price` — цена последнего прихода, ею оценивается
    остаток сверх партий.

Поправить одно и забыть другое — обычное дело, а расходится потом «Склад
(оборот)» с «Не объяснено», и ищи. Здесь все три двигаются разом, в одной
транзакции, и в журнал действий уходит запись.

ЧЕГО КОМАНДА НЕ ДЕЛАЕТ: не переписывает себестоимость того, что из партии уже
ушло. Она снята в момент движения (`TransactionItem.cost_total` у продажи,
`InventoryLog.cost` у списания) и относится к закрытым заказам и к уже
посчитанной прибыли тех дней. Если из партии уже брали, команда об этом скажет.

    python manage.py fix_lot_cost 14 9000            # посмотреть, что изменится
    python manage.py fix_lot_cost 14 9000 --yes      # и применить
"""
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from audit.models import AuditLog
from warehouse.models import InventoryLog, Roll


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

        material = roll.material
        old_cost = roll.purchase_cost
        old_per_sqm = roll.cost_per_sqm
        new_per_sqm = (
            (new_cost / roll.initial_area).quantize(Decimal("0.01"))
            if roll.initial_area else Decimal("0")
        )

        # Приход в журнале — по нему считается закуп. Своей ссылки на партию у
        # записи нет, поэтому ищем по материалу и площади; если таких приходов
        # несколько (одинаковую поставку приняли дважды), разводим их по дате.
        # `receive_lot` пишет партию и запись вместе, датой поступления.
        logs = list(InventoryLog.objects.filter(
            material=material, type=InventoryLog.Type.SUPPLY,
            quantity_changed=roll.initial_area,
        ))
        if len(logs) > 1:
            same_moment = [l for l in logs if l.happened_at == roll.received_at]
            if same_moment:
                logs = same_moment
        if len(logs) != 1:
            raise CommandError(
                f"У партии №{roll.id} нашлось {len(logs)} подходящих записей журнала "
                "вместо одной — поправьте вручную, иначе закуп разойдётся со складом."
            )
        log = logs[0]

        # Цена материала — это цена ПОСЛЕДНЕГО прихода. Двигаем её, только если
        # правим именно его: иначе затрём более свежую.
        last = material.rolls.order_by("-received_at", "-id").first()
        touch_material = last is not None and last.id == roll.id

        sold = roll.initial_area - roll.remaining_area
        self.stdout.write(f"Партия №{roll.id} · {material.name}")
        self.stdout.write(f"  принято {roll.initial_area} кв.м, осталось {roll.remaining_area}")
        self.stdout.write(f"  сумма закупки:  {_som(old_cost)} → {_som(new_cost)}")
        self.stdout.write(f"  цена за кв.м:   {_som(old_per_sqm)} → {_som(new_per_sqm)}")
        self.stdout.write(
            f"  закуп в отчёте: {_som(log.quantity_changed * (log.actual_price or 0))}"
            f" → {_som(log.quantity_changed * new_per_sqm)}"
        )
        if touch_material:
            self.stdout.write(
                f"  цена в карточке: {_som(material.purchase_price)} → {_som(new_per_sqm)}"
            )
        else:
            self.stdout.write("  цена в карточке не меняется — партия не последняя")
        if sold > 0:
            self.stdout.write(self.style.WARNING(
                f"  ВНИМАНИЕ: из партии уже ушло {sold} кв.м — продажами или "
                "списанием. Их себестоимость снята в момент движения и останется "
                "прежней: она относится к закрытым заказам и к уже посчитанной "
                "прибыли тех дней."
            ))
        if not options["yes"]:
            self.stdout.write(self.style.NOTICE("\nНичего не изменено. Повторите с --yes."))
            return

        with transaction.atomic():
            roll.purchase_cost = new_cost
            roll.save(update_fields=["purchase_cost"])
            log.actual_price = new_per_sqm
            log.save(update_fields=["actual_price"])
            if touch_material:
                material.purchase_price = new_per_sqm
                material.save(update_fields=["purchase_price", "updated_at"])
            AuditLog.record(
                None,
                f"Исправлена цена партии №{roll.id} «{material.name}»: "
                f"{_som(old_cost)} → {_som(new_cost)} сом "
                f"({_som(old_per_sqm)} → {_som(new_per_sqm)} за кв.м)",
            )
        self.stdout.write(self.style.SUCCESS("Готово."))
