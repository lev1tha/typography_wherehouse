"""Проверить, что роли разграничены на СЕРВЕРЕ, а не только кнопками.

Ничего не меняет — только читает (одни GET-запросы).

    python manage.py check_roles

Зачем. Интерфейс можно спрятать, данные — нет. Складовщик, знающий закупочную
цену, знает наценку цеха — это первое, что заказчик просил закрыть, и держится
оно на правах в API, а не на том, нарисована ли кнопка. Раздел 10.6
ТЕСТИРОВАНИЯ.md проверяет то же самое через `curl` с боевым токеном; эта
команда делает то же локально и разом, чтобы гонять после каждого обновления.

Что проверяется:
  * какие разделы отвечают 200, а какие 403 каждой роли;
  * что складовщику закупочная цена и стоимость склада приходят ПУСТЫМИ;
  * что складовщику себестоимость и маржа в чеке приходят ПУСТЫМИ.

Чего команда НЕ проверяет: состав меню, наличие кнопок и переброс с /admin/… —
это фронт, его смотрят глазами. «Кнопка, которая гарантированно ответит
"Произошла ошибка", хуже отсутствующей» — но увидеть её можно только в браузере.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

# Кого ждём в каждом разделе. Ключ — имя учётки на боевой и локальной базе.
ROLES = [("admin", "админ"), ("Чпу", "складовщик"), ("Бухгалтер", "бухгалтер")]

# Раздел → ожидаемый код ответа для админа, складовщика, бухгалтера.
SECTIONS = [
    ("/api/finance/report/", "Финансы — отчёт", (200, 403, 200)),
    ("/api/finance/cash/", "Касса", (200, 403, 200)),
    ("/api/audit/logs/", "Журнал действий", (200, 403, 200)),
    ("/api/audit/dashboard/", "Обзор", (200, 403, 200)),
    ("/api/sales/receipts/", "Чеки", (200, 200, 200)),
    ("/api/warehouse/materials/", "Склад — материалы", (200, 200, 200)),
]

# Поля, которых складовщик видеть не должен ни при каких условиях.
HIDDEN_FROM_STOREKEEPER = ("purchase_price", "stock_value", "cost_total", "margin")


class Command(BaseCommand):
    help = "Проверить разграничение ролей запросами к API (только чтение)"

    def handle(self, *args, **options):
        # APIClient ходит через полный стек middleware и представляется хостом
        # `testserver`, которого нет в ALLOWED_HOSTS вне тестов. Правка живёт
        # только в этом процессе и запущенного сервера не касается.
        if "testserver" not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + ["testserver"]

        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        from sales.models import Receipt
        from warehouse.models import Material

        User = get_user_model()
        clients, missing = {}, []
        for username, label in ROLES:
            user = User.objects.filter(username=username).first()
            if user is None:
                missing.append(f"{username} ({label})")
                continue
            api = APIClient()
            api.force_authenticate(user)
            clients[username] = api

        if missing:
            self.stdout.write(self.style.WARNING(
                "Нет учёток: " + ", ".join(missing)
                + "\nНа базе с другими именами проверять нечего — заведите их "
                  "или поправьте ROLES в этой команде."
            ))
        if not clients:
            return

        problems = []

        # ---- разделы ------------------------------------------------------
        self.stdout.write(self.style.MIGRATE_HEADING("\nДОСТУП К РАЗДЕЛАМ"))
        header = f"{'раздел':26}" + "".join(f"{lbl:>14}" for _, lbl in ROLES)
        self.stdout.write(header)
        for url, title, expected in SECTIONS:
            cells = []
            for (username, label), want in zip(ROLES, expected):
                api = clients.get(username)
                if api is None:
                    cells.append(f"{'—':>14}")
                    continue
                got = api.get(url).status_code
                cells.append(f"{got:>14}" if got == want else f"{f'{got}!={want}':>14}")
                if got != want:
                    problems.append(
                        f"{title}: {label} получил {got}, ожидался {want}"
                    )
            self.stdout.write(f"{title:26}" + "".join(cells))

        # ---- закупочные цифры в карточке материала ------------------------
        material = Material.objects.filter(purchase_price__gt=0).first()
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\nЗАКУПКА И СТОИМОСТЬ СКЛАДА В КАРТОЧКЕ МАТЕРИАЛА"
        ))
        if material is None:
            self.stdout.write(
                "  пропущено: нет ни одного материала с закупочной ценой > 0"
            )
        else:
            self.stdout.write(f"  материал: «{material.name}»")
            problems += self._check_hidden(
                clients, f"/api/warehouse/materials/{material.id}/",
                ("purchase_price", "stock_value"), "карточка материала",
            )

        # ---- себестоимость и маржа в чеке ---------------------------------
        receipt = Receipt.objects.order_by("created_at").first()
        self.stdout.write(self.style.MIGRATE_HEADING(
            "\nСЕБЕСТОИМОСТЬ И МАРЖА В ЧЕКЕ"
        ))
        if receipt is None:
            self.stdout.write("  пропущено: чеков нет")
        else:
            self.stdout.write(f"  чек №{receipt.order_number or receipt.id}")
            problems += self._check_hidden(
                clients, f"/api/sales/receipts/{receipt.id}/",
                ("cost_total", "margin"), "чек",
            )

        # ---- итог ---------------------------------------------------------
        if problems:
            self.stdout.write(self.style.ERROR(
                f"\nНЕ СХОДИТСЯ — {len(problems)}:"
            ))
            for line in problems:
                self.stdout.write(self.style.ERROR(f"  • {line}"))
            self.stdout.write(
                "\nЛюбая строка здесь означает, что право проверяется не там, "
                "где нужно. Разбираться до того, как обновлять прод."
            )
        else:
            self.stdout.write(self.style.SUCCESS(
                "\nВСЁ СХОДИТСЯ: складовщик не видит ни закупки, ни маржи, "
                "ни финансов; бухгалтер читает всё."
            ))

    def _check_hidden(self, clients, url, fields, where):
        """Показать поля глазами каждой роли; складовщику они должны быть пусты."""
        problems = []
        for username, label in ROLES:
            api = clients.get(username)
            if api is None:
                continue
            resp = api.get(url)
            if resp.status_code != 200:
                self.stdout.write(f"  {label:12} {resp.status_code} — не прочитал")
                continue
            shown = {f: resp.data.get(f) for f in fields}
            self.stdout.write(
                f"  {label:12} "
                + "  ".join(f"{f}={v!r}" for f, v in shown.items())
            )
            if username != "Чпу":
                continue
            for field, value in shown.items():
                if value is not None:
                    problems.append(
                        f"{where}: складовщик видит {field}={value!r} — "
                        f"он знает наценку цеха"
                    )
        return problems
