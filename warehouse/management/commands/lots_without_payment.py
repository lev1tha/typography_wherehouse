"""Приходы без оплаты и без отметки «в долг» — их карточка считает долгом (D-195).

Решение владельца 11.10: ПРИХОД БЕЗ УКАЗАННОЙ ОПЛАТЫ — ЭТО ДОЛГ. Новые приходы
без способа оплаты встают «в долг» сами. Старые (одиночная кнопка до 11.10, в
том числе всё, что принято до 19.09, когда оплата в кассу ещё не писалась) в
базе остались как были — данные не мигрировались, — а карточка «Долг
поставщикам» теперь считает их долгом расчётом: сумма партии − заплаченное по
кассе.

    python manage.py lots_without_payment

показывает такие приходы: дата, материал, что пришло, сумма, производство и
маркировка (поставщика у одиночной партии нет — кто возил, пишут в
маркировку), кто принял. Отдельно — приходы без партии (быстрый приход
штучного до 27.08): у них нет документа, и оплатить их можно только тратой
«Закуп материала». Команда ничего не пишет.

Оплаченный когда-то мимо кассы приход оплачивают в «Финансах» → «Долг
поставщикам» → «Оплатить» датой, когда отдали деньги; оплаченный в прошлом
закрытом месяце — решает владелец (оплата датой закрытого месяца не пройдёт).
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from warehouse.supplier_debts import lot_debts, lot_purchase_logs, loose_purchases, standalone_lots


def _som(value) -> str:
    value = Decimal(value).quantize(Decimal("0.01"))
    whole = f"{int(value):,}".replace(",", " ")
    cents = abs(value) % 1
    return whole if not cents else f"{whole},{str(cents)[2:4]}"


class Command(BaseCommand):
    help = "Приходы без оплаты и без отметки «в долг»: карточка «Долг поставщикам» считает их долгом"

    def handle(self, *args, **options):
        lots = list(
            standalone_lots().select_related("material", "production", "created_by")
            .order_by("received_at", "id")
        )
        debts = lot_debts(lots)
        rows = [lot for lot in lots if debts[lot.id][1] and debts[lot.id][0] > 0]
        loose = loose_purchases(lot_purchase_logs())

        if not rows and not loose:
            self.stdout.write("Приходов без оплаты и без отметки «в долг» нет.")
            return

        total_lots = sum((lot.purchase_cost for lot in rows), Decimal("0"))
        if rows:
            self.stdout.write(f"Приходы партиями без оплаты и без отметки «в долг»: {len(rows)}")
            self.stdout.write(
                f"{'партия':>7}  {'дата':<10}  {'материал':<28} {'что пришло':<24} "
                f"{'сумма':>12}  производство / маркировка / принял"
            )
            for lot in rows:
                where = " / ".join(x for x in (
                    lot.production.name if lot.production_id else "",
                    lot.code,
                    lot.created_by.username if lot.created_by_id else "",
                ) if x) or "—"
                self.stdout.write(
                    f"{lot.id:>7}  {timezone.localtime(lot.received_at):%d.%m.%Y}  "
                    f"{lot.material.name[:28]:<28} {lot.dimensions_label[:24]:<24} "
                    f"{_som(lot.purchase_cost):>12}  {where}"
                )
            self.stdout.write(f"Итого партиями: {_som(total_lots)}")

        total_loose = sum((row["amount"] for row in loose), Decimal("0"))
        if loose:
            self.stdout.write("")
            self.stdout.write(
                f"Приходы без партии (оплатить можно только тратой «Закуп материала»): {len(loose)}"
            )
            for row in loose:
                qty = format(row["quantity"].normalize(), "f")
                self.stdout.write(
                    f"{'запись ' + str(row['id']):>12}  {row['day']:%d.%m.%Y}  {row['material'][:28]:<28} "
                    f"{qty} {row['unit']} × {row['price']}  {_som(row['amount']):>12}"
                    + (f"  {row['created_by']}" if row["created_by"] else "")
                )
            self.stdout.write(f"Итого без партии: {_som(total_loose)}")

        self.stdout.write("")
        self.stdout.write(f"Итого считается долгом поставщикам: {_som(total_lots + total_loose)}")
        self.stdout.write(
            "Карточка «Долг поставщикам» показывает эти приходы долгом; оплаченные когда-то мимо "
            "кассы оплатите в «Финансах» → «Долг поставщикам» → «Оплатить» датой оплаты."
        )
