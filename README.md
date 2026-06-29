# Cloude — складской учёт и продажи для типографии

Бэкенд: **Django 4.2 + DRF + JWT**. Фронтенд: **React 18 + Vite** (`./frontend`).

## Запуск бэкенда

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # заполнить реальные токены при необходимости
python manage.py migrate
python manage.py seed         # 2 аккаунта + базовый каталог
python manage.py runserver
```

### Аккаунты по умолчанию (из `seed`)
| Роль | Логин | Пароль |
|------|-------|--------|
| Администратор | `admin` | `admin12345` |
| Складовщик | `storekeeper` | `store12345` |

> Смените пароли перед продакшеном.

## Структура (Django-приложения)

| App | Назначение |
|-----|------------|
| `accounts` | Кастомный `User` (роли Admin/Storekeeper), `Profile`, JWT-авторизация |
| `warehouse` | `Material`, `MaterialImage`, `InventoryLog` + сервис остатков `stock.py` |
| `services` | `PrintingService`, `ServiceRecipe` (техкарты, наценки) |
| `clients` | `Client` (CRM, живой поиск по телефону, LTV) |
| `sales` | `Receipt`, `TransactionItem` + бизнес-логика `sale_service.py` |
| `audit` | `AuditLog` + финансовый дашборд |
| `integrations` | Telegram (алерты + чеки), платёжный шлюз, вебхуки |

## Ключевые эндпоинты API

| Метод | Путь | Описание |
|-------|------|----------|
| POST | `/api/token/` | Вход (логин+пароль) → JWT + роль |
| GET | `/api/me/` | Текущий пользователь |
| GET/POST | `/api/warehouse/materials/` | Каталог (`?search=`, `?ordering=`, `?category=`) |
| PATCH | `/api/warehouse/materials/{id}/update-price/` | Изменение розничной цены (только админ) |
| POST | `/api/warehouse/materials/supply/` | Поступление товара |
| POST | `/api/warehouse/materials/adjust/` | Инвентаризация |
| POST | `/api/warehouse/materials/write-off/` | Списание (порча/брак/утеря) |
| GET/POST | `/api/services/services/` | Услуги и тарификация |
| GET/POST | `/api/clients/clients/` | CRM (`?search=<телефон>` для автоподстановки) |
| POST | `/api/sales/receipts/checkout/` | Оформление продажи |
| POST | `/api/sales/receipts/{id}/refund/` | Возврат (со списанием обратно на склад) |
| POST | `/api/sales/receipts/{id}/mark-ready/` | Услуга «Готово к выдаче» → уведомление клиента |
| POST | `/api/sales/receipts/{id}/mark-issued/` | Услуга «Выдан» → уведомление клиента |
| GET | `/api/audit/dashboard/` | Финансовая сводка (только админ) |
| GET | `/api/audit/logs/` | Аудит-лог (только админ) |
| POST | `/api/integrations/payments/webhook/` | Вебхук платёжного шлюза |
| POST | `/api/integrations/telegram/customer/webhook/` | Привязка клиента по контакту |

## Логика продаж

- **Наличные** → чек сразу `PAID`, материалы списываются мгновенно.
- **Онлайн** → чек `PENDING` + платёжная ссылка; списание со склада **только** после
  подтверждения оплаты (вебхук `confirm_payment`).
- **Услуги** списывают сырьё автоматически по техкартам (`ServiceRecipe`).
- **Возврат** возвращает метры/кг обратно на склад и меняет статусы позиций/чека.
- Падение остатка ниже `critical_balance` шлёт алерт в Telegram персонала.

## Интеграции

Токены и ключи читаются из окружения (`.env`) — см. `.env.example`.
Платёжный шлюз абстрагирован (`integrations/payments.py`): по умолчанию `mock`
для разработки; реальный провайдер регистрируется в `get_gateway()`.

## Запуск фронтенда (React + Vite)

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173 (проксирует /api на Django :8000)
```

Сначала поднимите Django на `127.0.0.1:8000` — Vite проксирует туда `/api` и
`/media`. Вход: `admin / admin12345` (Админ) или `storekeeper / store12345`.

**Реализовано:** JWT-вход с роль-маршрутизацией, переключатель языков (RU/KY/EN,
i18next), адаптивный layout (бургер-меню, таблицы→карточки <600px, тач-цели 48px
на телефонах / плотные контролы на десктопе). **Дизайн-язык — Notion** (`DESIGN.md`,
сгенерирован `getdesign`): бумажный фон `#f6f5f4`, Inter, единственный акцент —
синий `#0075de`, hairline-границы, мягкие тени. Панель Админа (дашборд, каталог+галерея, тарификация,
CRM, чеки+аудит) и Складовщика (витрина, продажа, история+возвраты, поступление+
инвентаризация). Axios-инстанс с JWT-перехватчиком — `frontend/src/api/api.js`.

## Telegram-боты

```bash
# Бот клиентов (long polling): верификация по контакту, «Мои чеки/заказы»
python manage.py run_customer_bot
```

Алерты персонала о низком остатке отправляются автоматически из бэкенда
(`integrations/telegram.py`) — нужен `TELEGRAM_STAFF_BOT_TOKEN` и
`TELEGRAM_STAFF_CHAT_IDS` в `.env`. Без токенов вызовы тихо логируются (no-op).

## Платёжный шлюз

`PAYMENT_GATEWAY=mock` (по умолчанию, для разработки) или `freedompay` —
реальная интеграция FreedomPay/PayBox (подпись + XML-ack вебхука). Для
`freedompay` укажите `PAYMENT_API_KEY` (pg_merchant_id) и `PAYMENT_API_SECRET`.

## Мультиязычность

`django-modeltranslation` для динамического текста (название/категория материала,
название услуги) на RU / KY / EN. Статика интерфейса — на фронтенде (i18next).
