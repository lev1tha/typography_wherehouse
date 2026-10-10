from django.core.management.base import BaseCommand

from finance import recurring


class Command(BaseCommand):
    help = (
        "Завести траты по расписанию повторяющихся трат (аренда каждого 10-го и т.п.). "
        "Безопасно запускать сколько угодно раз: внесённые месяцы не задваиваются."
    )

    def handle(self, *args, **options):
        result = recurring.generate()
        self.stdout.write(
            f"Внесено трат: {len(result['created'])}; пропущено (закрытый период): {len(result['skipped'])}"
        )
