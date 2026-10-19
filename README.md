# ЧПУ Система — склад, продажи и финансы для мастерской ЧПУ-резки

Бэкенд: **Django 6.0 + DRF + JWT**. Фронтенд: **React 18 + Vite** (`./frontend`).
Прод: https://chpucenter.com (Docker + PostgreSQL + nginx, см. [DEPLOY.md](DEPLOY.md)).

## Карта документации

| Файл | Для кого и зачем |
|------|------------------|
| [CONTINUE_HERE.md](CONTINUE_HERE.md) | Текущий статус: что выложено, что ждёт выкладки, что делать дальше. Начинать с него |
| [HANDOVER.md](HANDOVER.md) | Полное техническое описание: архитектура по приложениям, бизнес-правила, открытые вопросы, хронология |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Журнал решений владельца и следствий из них (D-1…D-205). Окончательные — не пересматривать без владельца |
| [DEPLOY.md](DEPLOY.md) | Сервер, выкладка, бэкапы, откат, восстановление |
| [ИНСТРУКЦИЯ.md](ИНСТРУКЦИЯ.md) | Инструкция для владельца: как вести склад, продажи и деньги |
| [ТЕСТИРОВАНИЕ.md](ТЕСТИРОВАНИЕ.md) | Ручные сценарии проверки по разделам и ролям |
| [frontend/DESIGN.md](frontend/DESIGN.md) | Дизайн-система и правила интерфейса |
| [GEMINI.md](GEMINI.md) | Контекст для ИИ-ассистента (Gemini CLI): правила работы с проектом в одном файле |
| `docs/*AUDIT*.md`, `docs/OWNER_RECHECK_*.md`, `docs/FINANCE_*.md` | Отчёты аудитов и план переделки финансов — история, почему сделано так |

## Запуск бэкенда

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # заполнить реальные токены при необходимости
python manage.py migrate
python manage.py createcachetable                           # без неё вход отдаёт 500
SEED_PASSWORD='<придумайте пароль>' python manage.py seed   # 3 учётки + базовый каталог + услуги
python manage.py runserver                                  # сам встаёт на 127.0.0.1:8710
```

### Учётные записи (`seed`)

Паролей по умолчанию в коде нет. `seed` создаёт учётки (`admin` — администратор,
`storekeeper` — складовщик, `accountant` — бухгалтер) **только если таблица
пользователей пуста** и только с паролем, который вы задали сами
(`SEED_PASSWORD` или `--password`). Повторный запуск ничего не перезаписывает:
ни пароли, ни ставки и цены каталога, ни техкарты. Нужны недостающие учётки в
непустой базе — `--create-users`; только каталог без учёток — `--no-users`.

> На боевой базе `seed` не запускают: там данные настоящие, учётки заведены
> отдельно (см. `CONTINUE_HERE.md`). Пароли храните в менеджере паролей, не в
> репозитории.

Отдельно от логина, экраны **«Финансы»**, **«Касса»**, **«Ведомость»** и **«Обзор»**
закрыты вторым паролем (`FINANCE_PASSWORD` в `.env`, по умолчанию `finance123` — на проде
обязательно сменить, иначе при старте в лог идёт ERROR) — проверяется на бэкенде
(`POST /api/finance/unlock/`), в бандл фронтенда не попадает. Разблокировка
держится 30 минут (метка времени в `localStorage`).

## Роли

| Роль | Раздел | Что может |
|------|--------|-----------|
| Администратор (`ADMIN`) | `/admin/*` | Всё. Меню «Каждый день»: Продажа, Чеки, Клиенты, Склад, Финансы, Касса, Ведомость; «Реже»: Обзор, Цены и услуги, Сотрудники, Входящие остатки |
| Складовщик (`STOREKEEPER`) | `/app/*` | Продажа, Чеки, Клиенты, Склад, Приём товара, Полка остатков. Закупочных цен не видит (в API вместо них `null`) |
| Бухгалтер (`ACCOUNTANT`) | `/acc/*` | Проверяющий, всё только на просмотр: чеки с себестоимостью, клиенты и акт сверки, финансы, касса, ведомость, обзор, полка |
| Клиент | `/me` | Портал: свои заказы, долг, выписка. Вход по телефону + паролю, который выдаёт админ |

Разграничение держит сервер, а не меню: `python manage.py check_roles` проверяет
его запросами к API.

## Структура (Django-приложения)

| App | Назначение |
|-----|------------|
| `accounts` | `User` (роли ADMIN/STOREKEEPER/ACCOUNTANT), `Profile`, `Employee` (сотрудник ≠ учётка: ФИО, должность, станок), JWT с версией учётных данных, лимит входа, команды `seed`, `seed_demo`, `check_roles` |
| `warehouse` | Каталог (`Material`, `MaterialType`, ступени опта `MaterialPriceTier`), партии `Roll` (лист/рулон/штучный), журнал `InventoryLog`, поставщики и накладные (`Supplier`, `Supply`, `SupplierPayment`, `SupplierReturn`, `SupplierOpeningDebt`), площадки и перемещения (`ProductionSite`, `StockTransfer`), снимки склада (`StockSnapshot`), полка остатков (`Leftover`). Сервисы: `stock.py`, `rolls.py` (FIFO), `supplies.py`, `supplier_ledger.py`, `supplier_returns.py`, `lot_correction.py`, `fifo_recalc.py`, `snapshots.py`, `sites.py`, `reorder.py`, `reprice.py`, `leftovers.py` |
| `services` | `PrintingService` (резка по станкам, гравировка, «Отходы»), `ServiceRecipe` (техкарты), `PricingSettings` (минимум, округление, срочность), `RateMatrixEntry`, `ThicknessCoefficient` |
| `clients` | `Client` (CRM, рефералы, скидка, лимит долга), авансы (`ClientAdvance`), входящие остатки (`OpeningBalance`), договорные цены (`ClientPrice`), реферальные бонусы, акт сверки (`statement.py`), портал клиента (`customer.py`) |
| `sales` | `Receipt`, `TransactionItem`, `Payment`, `TransactionItemLot` (из каких партий взята строка), `Quote` (КП), `IdempotencyRecord`. Вся логика — `sale_service.py`, правила прайса — `pricing_rules.py` |
| `finance` | Виды и записи расходов (`ExpenseKind`, `ExpenseEntry`), касса (`CashEntry`), замок периода, ставки налога, повторяющиеся траты, ведомость зарплаты (`payroll*.py`). Отчёты ОПиУ/ОДДС/сверка/«Обзор» — слой `finance/reports/`, справочник статей — `chart.py` |
| `audit` | `AuditLog` — журнал действий «было → стало» с типами, финансовый дашборд/аналитика |
| `integrations` | Telegram (алерты персоналу + бот клиентов), платёжный шлюз (mock / FreedomPay), вебхуки |

## Ключевые эндпоинты API

Полный список — `config/urls.py` и `*/urls.py`. Здесь — то, с чего начинать.

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/api/token/`, `/api/token/refresh/` | Вход персонала → JWT + роль; обновление (refresh ротируется с чёрным списком) |
| GET | `/api/me/`, `/api/health/` | Текущий пользователь; проверка живости для мониторинга |
| GET/POST | `/api/staff/users/`, `/api/staff/employees/` | Учётки и сотрудники (админ) |
| GET/POST | `/api/warehouse/materials/` | Каталог (`?search=`, `?ordering=`, `?category=`); `bulk/` — ввод пачкой |
| POST | `/api/warehouse/materials/receive-roll/`, `supply/` | Приход партии (лист/рулон/штучный) |
| POST | `/api/warehouse/materials/adjust/`, `write-off/`, `/api/warehouse/waste/` | Инвентаризация, списание, отход (брак) |
| GET | `/api/warehouse/materials/on-date/`, `reorder/` | Склад на дату; «К заказу» |
| POST | `/api/warehouse/materials/reprice/` | Цены пачкой (× %) |
| GET/POST | `/api/warehouse/supplies/` (+ `{id}/pay/`, `{id}/return/`) | Накладные, оплата, возврат поставщику |
| GET/POST | `/api/warehouse/suppliers/` (+ `{id}/statement/`), `supplier-payments/`, `supplier-opening-debts/` | Поставщики: сальдо, платежи, начальный долг |
| POST | `/api/warehouse/lot-correction/preview/`, `apply/` | «Исправить приход» (опечатка в цене/количестве партии) |
| GET/POST | `/api/warehouse/transfers/`, `production-sites/` | Площадки хранения и перемещение |
| GET/POST | `/api/warehouse/leftovers/` (+ `from-receipt/`, `{id}/write-off/`, `summary/`, `sales/`, `export/`) | Полка остатков |
| GET/POST | `/api/services/services/`, `settings/`, `rate-matrix/`, `thickness-coefficients/` | Услуги, правила прайса, матрица ставок |
| GET/POST | `/api/clients/clients/` | CRM (`?search=<имя/телефон>`); `aging/`, `export/` |
| GET | `/api/clients/clients/{id}/statement/` | Акт сверки клиента |
| POST | `/api/clients/clients/{id}/pay-debt/`, `advances/`, `set-password/`, `merge/` | Общая выплата, аванс, пароль портала, склейка карточек |
| GET/POST | `/api/clients/opening-balances/` (+ `preview/`) | Входящие остатки клиентов (переезд из Excel) |
| POST | `/api/sales/receipts/preview/`, `checkout/` | Расчёт без записи; оформление продажи (`Idempotency-Key`) |
| POST | `/api/sales/receipts/{id}/pay/`, `cancel-payment/`, `write-off/` | Оплата, отмена оплаты, списание долга |
| POST | `/api/sales/receipts/{id}/add-items/`, `edit-items/` | Дозаказ; правка состава (`dry_run` — предпросмотр) |
| POST | `/api/sales/receipts/{id}/refund/`, `undo-refund/` | Возврат (полный или по количеству) и его отмена |
| POST | `/api/sales/receipts/{id}/issue/`, `mark-ready/`, `mark-issued/` | Выдача по позициям; статусы выполнения |
| GET | `/api/sales/receipts/stats/`, `export/`, `titles/` | Сводка над списком; CSV; подсказки наименований |
| GET/POST | `/api/sales/quotes/` | Коммерческие предложения (КП) |
| GET | `/api/finance/report/`, `pnl/`, `cash-flow/`, `bridge/` | Сводка за месяц, ОПиУ, ОДДС, сверка «прибыль → поток» |
| GET/POST | `/api/finance/expense-kinds/`, `expense-entries/`, `recurring/`, `tax-rates/` | Виды и записи расходов, повторяющиеся траты, ставки налога |
| GET/POST | `/api/finance/cash/` (+ `balance/`, `count/`, `export/`) | Касса |
| GET/POST | `/api/finance/payroll/`, `pay-schemes/` | Ведомость зарплаты, схемы оплаты |
| POST | `/api/finance/unlock/` | Проверка пароля доступа к финансовым экранам |
| GET | `/api/audit/logs/` (+ `export/`), `/api/audit/dashboard/` | Журнал действий; аналитика (админ) |
| POST | `/api/integrations/payments/webhook/`, `/api/integrations/telegram/customer/webhook/` | Вебхуки шлюза и бота клиентов |
| POST/GET | `/api/customer/login/`, `orders/`, `summary/`, `statement/` | Портал клиента (свой JWT-scope, изолирован от персонала) |

## Логика продаж (коротко)

Подробно — `HANDOVER.md`, раздел 5, и `docs/DECISIONS.md`.

- Способы оплаты: **Наличные / MBank / DemirBank** — чек сразу принимает деньги
  (склад списывается сразу, предоплата необязательна — остаток становится
  долгом). **Онлайн** — чек `PENDING` + ссылка/QR; склад списывается после
  подтверждения оплаты. Отдельных счетов MBank/Demir в кассе нет (исключено
  владельцем): касса — наличные и банк.
- **Долг** = сумма − оплачено − возвраты. Гасится `pay` (по чеку) или «Общей
  выплатой» в карточке клиента; ошибочная оплата отменяется встречной записью
  (`cancel-payment`), прошлое не переписывается. Безнадёжный долг списывает админ
  (`WRITE_OFF`, с причиной).
- **Резка** — две строки: материал (площадь × цена/кв.м) + работа (погонный метр
  × ставка станка). Рулон продаётся погонными метрами или по кв.м изделия, лист —
  площадью или целиком.
- **Правила прайса** (минимум, округление заказа, срочность, скидка клиента,
  договорные цены) по умолчанию выключены — пока владелец их не задаст, цена
  считается как раньше.
- **Возврат** возвращает материал в те партии, откуда он был взят.
- **Себестоимость** — FIFO по партиям, фиксируется при продаже.
- Все денежные строки чека округляются **вверх до целого сома**.
- Закрытый месяц (замок периода) не правится задним числом — ни деньги, ни склад.

## Запуск фронтенда (React + Vite)

```bash
cd frontend
npm install
npm run dev          # http://localhost:5710 (проксирует /api и /media на Django :8710)
```

Порты у проекта свои, не 8000/5173: рядом работают другие проекты. Заняты и
эти — `BACKEND_PORT=… python manage.py runserver` и `BACKEND_PORT=… npm run dev`
(фронт найдёт бэкенд по той же переменной), фронт — `FRONTEND_PORT=…`.

Интерфейс: JWT-вход с роль-маршрутизацией, языки RU/KY/EN (i18next), адаптивная
вёрстка (таблицы → карточки на телефоне), тёмная тема, PWA-манифест и баннер
«нет связи». Дизайн — `frontend/DESIGN.md`, токены цвета — `frontend/src/index.css`.
Axios-инстанс с JWT-перехватчиком — `frontend/src/api/api.js`.

> Django-админка живёт на **`/django-admin/`**, не на `/admin/`: префикс `/admin/*`
> занят экранами самой системы. Почему и где ещё прописан — `HANDOVER.md`, раздел 2.

## Команды обслуживания

| Команда | Зачем |
|---------|-------|
| `seed`, `seed_demo` | Заготовка каталога и учёток / демо-месяц. **Не на боевой базе** |
| `check_roles` | Проверить разграничение ролей запросами к API (только чтение) |
| `run_customer_bot` | Telegram-бот клиентов (long polling) |
| `generate_recurring` | Завести траты по расписанию (аренда каждого 10-го и т.п.); повтор не задваивает |
| `stock_snapshot --month ГГГГ-ММ` | Снимок склада на конец месяца |
| `lots_without_payment` | Приходы без оплаты и без отметки «в долг» (перед выкладкой 10–11.10) |
| `supplies_unpaid_cash` | Накладные с оплатой, которой нет в кассе (`--fix` дописывает расход) |
| `fix_lot_cost`, `cancel_lot` | Исправить цену партии / отменить ошибочный приход |
| `find_duplicate_clients` | Клиенты-двойники (номер или имя) |
| `flushexpiredtokens` | Чистка просроченных JWT из чёрного списка (simplejwt) |

## Переменные окружения

Читаются из `.env` (`django-environ`); образцы — `.env.example` (разработка) и
`.env.prod.example` (прод, с пояснениями к каждой строке).

| Переменная | Зачем |
|------------|-------|
| `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS` | Базовые настройки Django. Оставленные по умолчанию `SECRET_KEY` и `ALLOWED_HOSTS=*` дают ERROR в лог при старте |
| `DATABASE_URL` | PostgreSQL на проде; пусто — SQLite (`db.sqlite3`) |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | База в `docker-compose.prod.yml` |
| `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS`, `SITE_BASE_URL` | Адреса фронта; базовый адрес для ссылок на оплату и QR |
| `FINANCE_PASSWORD` | Второй пароль финансовых экранов |
| `TRUSTED_PROXY_COUNT` | Сколько прокси перед приложением — для настоящего адреса клиента в лимите входа. Включать только вместе с новым nginx (`DEPLOY.md`) |
| `SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS` | На проде `False` и `0` намеренно: редирект и HSTS делает nginx |
| `TELEGRAM_STAFF_BOT_TOKEN`, `TELEGRAM_STAFF_CHAT_IDS` | Алерты персоналу |
| `TELEGRAM_CUSTOMER_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` | Бот клиентов; пустой секрет — вебхук закрыт |
| `PAYMENT_GATEWAY`, `PAYMENT_API_KEY`, `PAYMENT_API_SECRET`, `PAYMENT_WEBHOOK_SECRET` | Платёжный шлюз и подпись его вебхука |

## Интеграции

Без токенов Telegram молча логирует (no-op).

- **Telegram:** алерты персоналу о низком остатке (`TELEGRAM_STAFF_BOT_TOKEN`,
  `TELEGRAM_STAFF_CHAT_IDS`); бот клиентов — `run_customer_bot`, привязка только к
  своему контакту.
- **Платёжный шлюз:** `PAYMENT_GATEWAY=mock` (по умолчанию) или `freedompay`
  (FreedomPay/PayBox: подпись + XML-ack вебхука; `PAYMENT_API_KEY`,
  `PAYMENT_API_SECRET`). Заглушка «оплата прошла» живёт только при `DEBUG`.

## Мультиязычность

`django-modeltranslation` для динамического текста (название/категория материала,
название услуги) на RU / KY / EN. Статика интерфейса — на фронтенде (i18next,
`frontend/src/i18n/locales/{ru,ky,en}.json`).

## Тесты

```bash
python manage.py test                       # SQLite, ~60 с
python manage.py test --parallel 4          # ~35 с
python manage.py test --shuffle 1           # случайный порядок: тесты не должны зависеть друг от друга

# На PostgreSQL (как на проде; двухпоточные тесты гонок идут только тут):
DATABASE_URL=postgres://user@127.0.0.1:5432/postgres python manage.py test --parallel 4
```

На 2026-10-19 — **2406 тестов, все зелёные** (на SQLite 7 тестов гонок
пропускаются, на PostgreSQL — ни одного). Число проверяйте прогоном, а не этой
строкой. Золотой набор операций за квартал с итогами, посчитанными руками, —
`finance/tests_golden.py`.

Тесты не зависят от текущей даты: даты, привязанные к «сегодня», либо считаются
от середины месяца, либо часы фиксируются в самом тесте (`freeze_now` в
`warehouse/tests_stock_on_date.py`).

**CI** (`.github/workflows/ci.yml`): на каждый push и pull request — миграции
совпадают с моделями, тесты на PostgreSQL, сборка фронтенда, `shellcheck` скриптов
выкладки; отдельная необязательная job гоняет тесты в случайном порядке.
Раз в неделю `.github/workflows/audit.yml` проверяет зависимости (`pip-audit`,
`npm audit --omit=dev`) — нефатально.
