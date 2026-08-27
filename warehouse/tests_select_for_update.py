"""`select_for_update()` обязан жить внутри транзакции — иначе прод падает, а дев нет.

Это ЕДИНСТВЕННЫЙ тест в проекте, который читает исходники вместо того, чтобы
дёргать систему, и на то есть причина: обычным прогоном эту ошибку поймать
нельзя.

Django бросает `TransactionManagementError` только когда у бэкенда
`has_select_for_update = True`:

    if self.query.select_for_update and features.has_select_for_update:
        if self.connection.get_autocommit() and features.supports_transactions:
            raise TransactionManagementError(...)

У PostgreSQL этот флаг `True`, у SQLite — `False` (наследует из базового
класса). Значит на проде вызов вне транзакции падает с 500, а локально проходит
молча. Мало того: `APITestCase` оборачивает КАЖДЫЙ тест в транзакцию, так что
даже на Postgres обычный тест этого не увидит.

Так и случилось 27.08: `restore_area` осталась без `@transaction.atomic` —
единственная из десяти таких функций. `POST /api/warehouse/materials/adjust/`
отдавал 500 на проде каждый раз, когда пересчитанный остаток БОЛЬШЕ учётного
(инвентаризация в плюс идёт через `restore_area`), а все 852 локальных теста
были зелёными.

Правило простое: функция, которая зовёт `select_for_update()`, сама несёт
`@transaction.atomic`. Не «её вызывают из атомарной» — это верно ровно до
первого нового вызова, и проверить это глазами при следующей правке никто не
станет. Вложенный `atomic` — просто точка сохранения, он ничего не стоит.
"""
import ast
import pathlib

from django.test import SimpleTestCase

REPO = pathlib.Path(__file__).resolve().parent.parent

# Приложения с денежными и складскими операциями. Тесты и миграции не смотрим.
PACKAGES = ["warehouse", "sales", "clients", "finance", "accounts", "audit", "services"]


def _functions_using_select_for_update():
    """(файл, строка, имя, обёрнута ли в atomic) по всем функциям проекта."""
    found = []
    for package in PACKAGES:
        for path in sorted((REPO / package).rglob("*.py")):
            name = path.name
            if name.startswith("tests") or "/migrations/" in str(path):
                continue
            src = path.read_text(encoding="utf-8")
            if "select_for_update" not in src:
                continue
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                segment = ast.get_source_segment(src, node) or ""
                if "select_for_update" not in segment:
                    continue
                # Вложенные функции считаем по внешней: декоратор снаружи тоже
                # открывает транзакцию.
                decorated = any(
                    "atomic" in ast.unparse(d) for d in node.decorator_list
                )
                found.append((
                    path.relative_to(REPO), node.lineno, node.name, decorated,
                ))
    return found


class SelectForUpdateNeedsTransactionTests(SimpleTestCase):
    def test_every_such_function_is_atomic(self):
        rows = _functions_using_select_for_update()
        naked = [r for r in rows if not r[3]]
        self.assertFalse(
            naked,
            "select_for_update() вне транзакции — на Postgres это 500, "
            "а локально на SQLite молчит. Поставьте @transaction.atomic на:\n"
            + "\n".join(f"  {p}:{ln} {fn}()" for p, ln, fn, _ in naked),
        )

    def test_the_check_actually_finds_something(self):
        """Страховка от «зелено, потому что ничего не нашли».

        Если разбор сломается (переименовали пакет, поменяли структуру), тест
        выше пройдёт на пустом списке и будет врать.
        """
        rows = _functions_using_select_for_update()
        self.assertGreaterEqual(
            len(rows), 8,
            f"разбор нашёл всего {len(rows)} функций — похоже, он сломался, "
            f"а не код стал чище",
        )
        self.assertIn(
            "restore_area", {fn for _, _, fn, _ in rows},
            "не найдена restore_area — та самая функция, из-за которой "
            "27.08 падала инвентаризация на проде",
        )
