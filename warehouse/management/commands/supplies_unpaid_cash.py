"""Накладные, оплата которых не попала в кассу (аудит 10.10, F1).

Форма накладной раньше отправляла «Оплачено поставщику» без счёта, а расход в
кассу пишется только при заданном счёте: на проде в книге 100 000 при реальных
68 000. С 10.10 сервер такую оплату не принимает (400), но уже проведённые
накладные остались как были — миграцией данных их не трогали, потому что
задним числом касса меняется только решением владельца.

    python manage.py supplies_unpaid_cash
        список: номер, дата, поставщик, оплачено, лежит в кассе, не хватает

    python manage.py supplies_unpaid_cash --fix --account BANK --ids 12 15
        дописать недостающий расход по выбранным накладным на счёт (CASH|BANK)
        датой накладной; в журнал действий пишется каждая запись

Без `--fix` команда ничего не пишет. Дата в закрытом периоде пропускается
(закрытый месяц не меняют), пока не указана `--on ГГГГ-ММ-ДД`.
"""
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from warehouse.models import SupplierPayment, Supply

TINY = Decimal("0.005")


def missing_cash(supply: Supply) -> tuple[Decimal, Decimal]:
    """(лежит в кассе по старому полю накладной, не хватает до «оплачено»).

    В кассовые записи накладной входят и платежи-строки; их деньги считаются
    отдельно и из «старого» вычитаются.
    """
    from finance.models import CashEntry

    net = Decimal("0")
    for kind, amount in CashEntry.objects.filter(
        supply=supply, article=CashEntry.Article.SUPPLY
    ).values_list("kind", "amount"):
        net += amount if kind == CashEntry.Kind.OUT else -amount
    rows = Decimal("0")
    for p in supply.payments.all():
        if p.kind == SupplierPayment.Kind.PAYMENT:
            rows += p.settled if (p.fx_diff or 0) > 0 else p.amount
        elif p.kind == SupplierPayment.Kind.REFUND:
            rows -= p.amount
    recorded = net - rows
    return recorded, (supply.paid_amount or Decimal("0")) - recorded


class Command(BaseCommand):
    help = "Накладные с оплатой, которой нет в кассе: список и (с --fix) дописать расход"

    def add_arguments(self, parser):
        parser.add_argument("--fix", action="store_true",
                            help="дописать недостающие кассовые записи (без флага — только список)")
        parser.add_argument("--account", choices=["CASH", "BANK"],
                            help="счёт, с которого заплатили (обязателен с --fix)")
        parser.add_argument("--ids", nargs="+",
                            help="номера накладных (Supply.id) через пробел или запятую; обязательны с --fix")
        parser.add_argument("--on", help="дата записи ГГГГ-ММ-ДД вместо даты накладной")

    def handle(self, *args, **options):
        qs = (
            Supply.objects.filter(paid_amount__gt=0, is_opening=False)
            .select_related("supplier").prefetch_related("payments").order_by("received_on", "id")
        )
        rows = []
        for supply in qs:
            recorded, missing = missing_cash(supply)
            if missing > TINY:
                rows.append((supply, recorded, missing))

        if not rows:
            self.stdout.write("Все оплаты накладных есть в кассе — дописывать нечего.")
            return
        self.stdout.write(f"{'id':>5}  {'номер':<14} {'дата':<11} {'поставщик':<22} "
                          f"{'оплачено':>12} {'в кассе':>12} {'не хватает':>12}  счёт")
        for supply, recorded, missing in rows:
            self.stdout.write(
                f"{supply.id:>5}  {(supply.number or '—')[:14]:<14} {supply.received_on:%d.%m.%Y}  "
                f"{(supply.supplier.name if supply.supplier_id else '—')[:22]:<22} "
                f"{supply.paid_amount:>12} {recorded:>12} {missing:>12}  {supply.paid_account or '—'}"
            )
        self.stdout.write(f"Итого не хватает в кассе: {sum(m for *_, m in rows)}")

        if not options["fix"]:
            self.stdout.write("Только список. Дописать: --fix --account CASH|BANK --ids <номера>")
            return

        account = options.get("account")
        if account not in ("CASH", "BANK"):
            raise CommandError("С --fix укажите счёт: --account CASH или --account BANK.")
        raw_ids = options.get("ids") or []
        ids = {int(x) for chunk in raw_ids for x in str(chunk).replace(",", " ").split() if x.strip()}
        if not ids:
            raise CommandError("С --fix укажите накладные: --ids 12 15 (номера из списка выше).")
        day_override = None
        if options.get("on"):
            try:
                day_override = date.fromisoformat(options["on"])
            except ValueError:
                raise CommandError("--on: дата в формате ГГГГ-ММ-ДД.")
        by_id = {s.id: (s, m) for s, _, m in rows}
        unknown = sorted(ids - set(by_id))
        if unknown:
            raise CommandError(f"Нет в списке (оплата уже в кассе или накладной нет): {unknown}")

        from audit.models import AuditLog
        from finance import cash
        from finance.periods import is_closed

        done = 0
        with transaction.atomic():
            for sid in sorted(ids):
                supply, missing = by_id[sid]
                day = day_override or supply.received_on
                if is_closed(day):
                    self.stdout.write(
                        f"Накладная {supply.number or sid}: {day:%d.%m.%Y} в закрытом периоде — "
                        "пропущена (укажите --on с открытой датой)."
                    )
                    continue
                label = supply.number or f"#{supply.id}"
                cash.supplier_paid(
                    missing, account, supply=supply, happened_on=day,
                    note=f"Оплата накладной {label} (дописано: в кассу не попала)",
                )
                if not supply.paid_account:
                    supply.paid_account = account
                    supply.save(update_fields=["paid_account"])
                AuditLog.record(
                    None,
                    f"Дописана оплата в кассу по накладной {label} от {supply.received_on:%d.%m.%Y}: "
                    f"{missing} сом, {'наличные' if account == 'CASH' else 'банк'}, датой {day:%d.%m.%Y} "
                    "(команда supplies_unpaid_cash)",
                )
                done += 1
                self.stdout.write(f"Накладная {label}: записано {missing} сом, {account}, {day:%d.%m.%Y}.")
        self.stdout.write(f"Готово: дописано накладных — {done}.")
