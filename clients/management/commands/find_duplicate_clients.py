"""Показать клиентов, которые похожи на одного и того же человека.

Ничего не меняет — только смотрит. Нужна, чтобы разобрать то, что уже успело
накопиться: до появления поиска по цифрам номера касса заводила нового клиента
на каждое новое написание телефона, и заказы одного человека расходились по
двум карточкам («Тахир ака» и «ака Тахир»).

    python manage.py find_duplicate_clients

Ищет по двум признакам:
  * ОДИН НОМЕР в разном написании — это наверняка один человек;
  * ОДНО ИМЯ при разных номерах (в том числе слова переставлены местами) —
    это только повод посмотреть глазами: у отца и сына бывает одно имя.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand

from clients.models import Client
from clients.phones import phone_key


def name_key(client) -> str:
    """Имя, приведённое к сравнимому виду: регистр и порядок слов не важны.

    «ака Тахир» и «Тахир ака» — одно и то же, а сортировка слов делает их
    одинаковой строкой.
    """
    raw = f"{client.full_name or ''} {client.company_name or ''}".strip().casefold()
    return " ".join(sorted(part for part in raw.split() if part))


class Command(BaseCommand):
    help = "Найти клиентов-двойников (одинаковый номер или одинаковое имя)"

    def handle(self, *args, **options):
        clients = list(Client.objects.all())
        if not clients:
            self.stdout.write("Клиентов нет.")
            return

        by_phone = defaultdict(list)
        by_name = defaultdict(list)
        for client in clients:
            key = phone_key(client.phone)
            if key:
                by_phone[key].append(client)
            nkey = name_key(client)
            if nkey:
                by_name[nkey].append(client)

        phone_dupes = [group for group in by_phone.values() if len(group) > 1]
        # Совпавшие по имени, но НЕ попавшие уже в список по номеру.
        seen = {c.pk for group in phone_dupes for c in group}
        name_dupes = [
            group
            for group in by_name.values()
            if len(group) > 1 and not all(c.pk in seen for c in group)
        ]

        def show(group):
            for client in sorted(group, key=lambda c: c.created_at):
                orders = client.receipts.count()
                debt = sum((r.debt for r in client.receipts.all()), 0)
                self.stdout.write(
                    f"    id={client.pk:<5} {client.display_name:<28} "
                    f"{client.phone:<20} заказов: {orders:<4} долг: {debt}"
                )

        if phone_dupes:
            self.stdout.write(self.style.WARNING("\nОДИН НОМЕР, РАЗНЫЕ КАРТОЧКИ — почти наверняка один человек:"))
            for group in phone_dupes:
                self.stdout.write(f"  • номер …{phone_key(group[0].phone)}")
                show(group)

        if name_dupes:
            self.stdout.write(self.style.WARNING("\nОДНО ИМЯ, РАЗНЫЕ НОМЕРА — посмотреть глазами:"))
            for group in name_dupes:
                self.stdout.write(f"  • «{group[0].display_name}»")
                show(group)

        if not phone_dupes and not name_dupes:
            self.stdout.write(self.style.SUCCESS("Двойников не нашлось."))
            return

        self.stdout.write(
            "\nСклеивать карточки эта команда не умеет — она только показывает. "
            "Слияние переносит чужие заказы, долг и рефералов, и делать это "
            "вслепую нельзя: решает владелец, какая карточка остаётся."
        )
