"""Отменить приход: такой поставки не было.

Приход вносят ошибочно — дважды провели одну накладную, вбили чужую строку,
приняли то, что так и не приехало. Правка остатка («инвентаризация») здесь не
помогает: она убирает МАТЕРИАЛ, а деньги остаются в закупе, и финотчёт до
конца месяца показывает поставку, которой не было. На проде 20.09 так висели
102 960 сомов по акрилу салатовому: три прихода вместо одного, два из них
инвентаризация обнулила 8 сентября.

Команда убирает сам приход — партию и её запись в журнале, — и снимает с
остатка ровно то, что от партии ещё не ушло.

ЧТО НЕ МЕНЯЕТСЯ. Себестоимость уже проданного: она снята в момент продажи
(`TransactionItem.cost_total`) и относится к закрытым заказам. Строки чеков,
которые ссылались на эту партию, остаются на месте — ссылка просто обнуляется
(`on_delete=SET_NULL`). То есть прибыль прошедших дней не поедет.

Отмена накладной — это другое (`SupplyViewSet.destroy`): там документ целиком,
и он отменяется только нетронутым. Здесь отменяется ОДИН приход, в том числе
такой, из которого уже успели продать, — потому что ошибку ввода иначе не
убрать, а деньги в закупе врут каждый день.

    python manage.py cancel_lot 63          # посмотреть, что изменится
    python manage.py cancel_lot 63 --yes    # и применить
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from audit.models import AuditLog
from warehouse.models import InventoryLog, Roll


def _som(v) -> str:
    return f"{Decimal(v).quantize(Decimal('0.01')):,}".replace(",", " ")


class Command(BaseCommand):
    help = "Отменить ошибочный приход: убрать партию, её запись журнала и остаток"

    def add_arguments(self, parser):
        parser.add_argument("roll_id", type=int, help="номер партии (Roll.id)")
        parser.add_argument(
            "--yes", action="store_true",
            help="применить; без него команда только показывает, что изменится",
        )

    def handle(self, *args, **options):
        try:
            roll = Roll.objects.select_related("material").get(pk=options["roll_id"])
        except Roll.DoesNotExist:
            raise CommandError(f"Партии №{options['roll_id']} нет.")

        material = roll.material
        if roll.stocktakes.exists():
            raise CommandError(
                f"По партии №{roll.id} есть акт промера — сначала разберитесь с ним."
            )

        # Запись прихода — та же, что находит «Исправить приход»: по ссылке на
        # партию, а у старых записей — по материалу, площади и дате.
        from warehouse.lot_correction import supply_log_for_roll

        log = supply_log_for_roll(roll)
        if log is None:
            raise CommandError(
                f"У партии №{roll.id} не нашлось однозначной записи журнала "
                "— уберите вручную, иначе закуп разойдётся со складом."
            )
        # Закуп одиночной партии в отчётах — её сумма (D-195), у прихода из
        # накладной — как раньше, по журналу.
        if log.supply_id is None:
            purchase = roll.purchase_cost
        else:
            purchase = log.quantity_changed * (log.actual_price or Decimal("0"))
        gone = roll.initial_area - roll.remaining_area
        sold_items = roll.sold_items.count()

        self.stdout.write(f"Партия №{roll.id} · {material.name}")
        self.stdout.write(
            f"  принята {timezone.localtime(roll.received_at).date()}: "
            f"{roll.initial_area} кв.м за {_som(roll.purchase_cost)}"
        )
        self.stdout.write(f"  закуп в отчёте: −{_som(purchase)}")
        self.stdout.write(
            f"  остаток материала: {material.quantity} → "
            f"{material.quantity - roll.remaining_area} кв.м "
            f"(снимаем {roll.remaining_area} — то, что от партии ещё не ушло)"
        )
        if gone > 0:
            self.stdout.write(self.style.WARNING(
                f"  из партии уже ушло {gone} кв.м — продажами или списанием. "
                "Их себестоимость останется прежней: она снята в момент движения."
            ))
        if sold_items:
            self.stdout.write(self.style.WARNING(
                f"  на партию ссылается строк чеков: {sold_items}. Строки останутся, "
                "ссылка на партию у них обнулится."
            ))
        if not options["yes"]:
            self.stdout.write(self.style.NOTICE("\nНичего не изменено. Повторите с --yes."))
            return

        label = (
            f"Отменён приход №{roll.id} «{material.name}» от "
            f"{timezone.localtime(roll.received_at).date()}: "
            f"{roll.initial_area} кв.м на {_som(roll.purchase_cost)} сом"
        )
        with transaction.atomic():
            material.quantity = (material.quantity or Decimal("0")) - roll.remaining_area
            material.save(update_fields=["quantity", "updated_at"])
            log.delete()
            # Исправления этой партии («Исправление прихода») — тоже: прихода нет.
            InventoryLog.objects.filter(roll=roll, type=InventoryLog.Type.CORRECTION).delete()
            # Оплату партии не стираем: встречная запись сегодняшним днём,
            # исходная остаётся в книге (аудит Б-13).
            from finance import cash

            cash.reverse_supplier_payments(roll=roll, note=f"Отменён приход №{roll.id}")
            roll.delete()
            # Цена в карточке — цена последнего прихода. Убрали его — берём ту,
            # что осталась, иначе остаток сверх партий оценивался бы ценой
            # поставки, которой больше нет.
            last = material.rolls.order_by("-received_at", "-id").first()
            if last is not None:
                material.purchase_price = last.cost_per_sqm
                material.save(update_fields=["purchase_price", "updated_at"])
            AuditLog.record(None, label)
        self.stdout.write(self.style.SUCCESS("Готово."))
