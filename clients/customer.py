"""Customer self-service portal: a Client (not a staff User) logs in by phone
and views only their own orders (status + debt). Uses a dedicated JWT scope so
staff tokens and customer tokens can never cross into each other's endpoints.
"""
import re

from django.contrib.auth.hashers import check_password, make_password
from rest_framework import exceptions, serializers, status
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import AccessToken

from accounts.authentication import token_version
from accounts.throttling import (
    CustomerLoginThrottle,
    LoginAccountThrottle,
    login_not_an_attempt,
    login_succeeded,
)
from accounts.views import throttled_response
from sales.models import Receipt

from .models import Client
from .phones import find_client_by_phone


def _digits(value) -> str:
    return re.sub(r"\D", "", value if isinstance(value, str) else "")


_DUMMY_HASH = None


def _burn_password_check(raw: str) -> None:
    """Проверка пароля «в холостую» — чтобы ответ не выдавал, есть ли пароль.

    PBKDF2 занимает ~0,3 с, и если проверять его только у клиентов с выданным
    паролем, время ответа (0,3 против 0,002 с) подсказывает: номер известен и
    пароль ему выдан. Для неизвестного номера и для клиента без пароля сверяем
    с постоянным хешем — те же затраты, результат отбрасываем. Хеш считаем один
    раз при первом обращении, текущим хешером настроек.
    """
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = make_password("холостой-хеш-портала")
    check_password(raw, _DUMMY_HASH)


class CustomerIdentity:
    """Lightweight ``request.user`` for an authenticated customer."""

    is_authenticated = True
    is_staff = False
    is_admin_role = False

    def __init__(self, client: Client):
        self.client = client
        self.id = client.id

    def __str__(self) -> str:
        return f"customer:{self.client.display_name}"


class CustomerJWTAuthentication(JWTAuthentication):
    """Authenticates the customer-portal token (scope=customer, client_id)."""

    def get_user(self, validated_token):
        if validated_token.get("scope") != "customer":
            raise exceptions.AuthenticationFailed("Не клиентский токен")
        try:
            client = Client.objects.get(pk=validated_token.get("client_id"))
        except Client.DoesNotExist:
            raise exceptions.AuthenticationFailed("Клиент не найден")
        # Новый пароль или смена телефона отзывают выданные раньше токены.
        # Токен без клейма — версия 0 (выдан до введения версий).
        if token_version(validated_token) != client.credentials_version:
            raise exceptions.AuthenticationFailed(
                "Пароль изменён — войдите заново.", code="credentials_changed"
            )
        return CustomerIdentity(client)


class IsCustomer(BasePermission):
    def has_permission(self, request, view):
        return bool(getattr(request.user, "client", None))


def mint_customer_token(client: Client) -> str:
    token = AccessToken()
    token["scope"] = "customer"
    token["client_id"] = client.id
    token["cv"] = client.credentials_version
    token["name"] = client.display_name
    return str(token)


class CustomerItemSerializer(serializers.Serializer):
    """Строка заказа для клиента: название с описанием работы, количество с
    единицей. «Гравировка × 0.48» без описания и единицы ничего не говорила —
    теперь «Гравировка — логотип на двери», 0.48 кв.м (единицу и её код считает
    та же логика, что у сотрудника: `TransactionItemSerializer`)."""

    title = serializers.SerializerMethodField()
    note = serializers.CharField(read_only=True)
    own_material = serializers.BooleanField(read_only=True)
    quantity = serializers.DecimalField(max_digits=12, decimal_places=2)
    line_total = serializers.DecimalField(max_digits=14, decimal_places=2)
    # Код единицы (SQM / METER / PIECE / KG / LITER) — для перевода на фронте.
    unit = serializers.SerializerMethodField()
    # Готовая русская подпись («кв.м», «пог.м», «шт»).
    unit_label = serializers.SerializerMethodField()
    is_returned = serializers.BooleanField(read_only=True)

    def _staff(self):
        from sales.serializers import TransactionItemSerializer

        return TransactionItemSerializer()

    def get_unit(self, obj):
        return self._staff().get_unit_code(obj)

    def get_unit_label(self, obj):
        return self._staff().get_unit_label(obj)

    def get_title(self, obj):
        if obj.material_id:
            base = obj.material.name
        elif obj.service_id:
            base = obj.service.name
        else:
            base = "—"
        # Описание работы — как у сотрудника (`itemTitle`): «Резка — акрил 3 мм».
        return f"{base} — {obj.note}" if obj.note else base


class CustomerOrderSerializer(serializers.ModelSerializer):
    debt = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    items = serializers.SerializerMethodField()
    # Есть ли в заказе работа, которую вообще нужно ждать. Таблица чеков у цеха
    # прячет колонку выполнения при `has_service=false`, а кабинет показывал
    # «Готовится» любому заказу — и купленная пачка бумаги висела у клиента
    # «в работе» вечно: переключить статус там некому, кнопки на такой заказ нет.
    has_service = serializers.BooleanField(read_only=True)

    class Meta:
        model = Receipt
        fields = [
            "id",
            "order_number",
            "created_at",
            "payment_status",
            "fulfillment_status",
            "total_price",
            "amount_paid",
            # Сколько вернули клиенту деньгами/товаром: вместе с `amount_paid`
            # показывает «оплачено» без пересчёта на фронте.
            "refunded_amount",
            "debt",
            # Сдача, которую цех клиенту ещё не отдал. В кабинете её не было
            # вовсе: он видел, сколько должен ОН, но не видел, сколько должны
            # ЕМУ, — при том что эта сдача идёт в оплату его следующего заказа.
            "change_due",
            "status",
            "has_service",
            "items",
        ]

    def get_items(self, obj):
        """ВСЕ позиции, включая возвращённые.

        Раньше возвращённые отфильтровывались, и полностью возвращённый заказ
        приезжал клиенту пустым: номер, «0 сом» и ни одной строки — выглядит
        как сбой системы, а не как «мы вам всё вернули». Возврат помечается
        флагом, интерфейс показывает его зачёркнутым.
        """
        return CustomerItemSerializer(obj.items.all(), many=True).data


MIN_PORTAL_PASSWORD = 4


class CustomerLoginView(APIView):
    """POST /api/customer/login/ — вход клиента: телефон + пароль от админа.

    Шаг 1: клиент присылает только `phone`. Отвечаем, узнан ли он:
      - `status=need_password` — пароль выдан, пусть введёт;
      - `status=no_password` — пароля ещё нет, надо обратиться к администратору.
    Шаг 2: `phone` + `password` → проверяем и выдаём клиентский токен.

    Пароль клиент себе НЕ заводит: его выдаёт админ из карточки клиента
    (`ClientViewSet.set_password`). Иначе кабинет доставался бы тому, кто первым
    вошёл по чужому номеру, — а пароль как раз от этого и защищает.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    # Пароль кабинета выдаёт админ, он короткий, а портал открыт на публичном
    # домене — без предела попыток его подбирают перебором. Считаем и по
    # адресу, и по самому номеру: перебор одного клиента с разных адресов иначе
    # не ловится вовсе.
    throttle_classes = [CustomerLoginThrottle, LoginAccountThrottle]

    def throttled(self, request, wait):
        login_not_an_attempt(request)
        raise throttled_response(wait)

    def post(self, request):
        phone = _digits(request.data.get("phone"))
        raw_password = request.data.get("password")
        password = raw_password.strip() if isinstance(raw_password, str) else ""
        if not phone:
            login_not_an_attempt(request)
            return Response({"detail": "Введите номер телефона"}, status=status.HTTP_400_BAD_REQUEST)
        # Тот же поиск, что и в кассе: клиент набирает свой номер как привык, а
        # в базе он лежит в том написании, в каком его записал кассир. Пароль
        # по-прежнему обязателен — послаблений тут нет, только формат номера.
        raw_phone = request.data.get("phone")
        client = find_client_by_phone(raw_phone if isinstance(raw_phone, str) else "")

        # ОТВЕТ ОДИНАКОВЫЙ для любого номера — и для чужого, и для нашего, и для
        # того, кому пароль ещё не выдали. Раньше портал отвечал по-разному:
        # неизвестный номер получал «Клиент с таким номером не найден», а
        # известный — «С возвращением, Бакыт Осмонов!». Портал открыт на
        # публичном домене, и перебором номеров с него собиралась клиентская
        # база цеха вместе с именами. Имя показываем только ПОСЛЕ пароля.
        if not password:
            # Первый шаг («только телефон») — не попытка угадать пароль: клиент
            # делает два запроса подряд, и под лимитом в 10 в минуту ему
            # доставалось бы пять входов вместо десяти.
            login_not_an_attempt(request)
            return Response({"status": "need_password"})
        if client is None or not client.has_password:
            _burn_password_check(password)
            ok = False
        else:
            ok = client.check_password(password)
        if not ok:
            return Response(
                {"detail": "Неверный номер или пароль."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        login_succeeded(request)
        return self._token_response(client)

    @staticmethod
    def _token_response(client: Client) -> Response:
        return Response(
            {
                "access": mint_customer_token(client),
                "client": {"id": client.id, "name": client.display_name, "phone": client.phone},
            }
        )


class CustomerOrdersView(APIView):
    """GET /api/customer/orders/ — the logged-in customer's own orders only."""

    authentication_classes = [CustomerJWTAuthentication]
    permission_classes = [IsCustomer]

    def get(self, request):
        receipts = (
            Receipt.objects.filter(client=request.user.client)
            .prefetch_related("items__material", "items__service")
            .order_by("-created_at")
        )
        return Response(CustomerOrderSerializer(receipts, many=True).data)


class CustomerStatementView(APIView):
    """GET /api/customer/statement/?date_from=&date_to= — акт сверки клиента сам себе.

    Тот же расчёт, что у сотрудников (`clients.statement`), только про этого
    клиента и без внутренних примечаний к платежам.
    """

    authentication_classes = [CustomerJWTAuthentication]
    permission_classes = [IsCustomer]

    def get(self, request):
        from datetime import date

        from .statement import statement_payload

        try:
            d_from = date.fromisoformat(request.query_params["date_from"]) if request.query_params.get("date_from") else None
            d_to = date.fromisoformat(request.query_params["date_to"]) if request.query_params.get("date_to") else None
        except ValueError:
            return Response({"detail": "Некорректная дата."}, status=status.HTTP_400_BAD_REQUEST)
        if d_from and d_to and d_from > d_to:
            return Response({"detail": "Начало периода позже конца."}, status=status.HTTP_400_BAD_REQUEST)
        data = statement_payload(request.user.client, d_from, d_to)
        for row in data["rows"]:
            row.pop("note", None)
        return Response(data)


class CustomerSummaryView(APIView):
    """GET /api/customer/summary/ — долг, сдача, аванс и сальдо клиента одним ответом."""

    authentication_classes = [CustomerJWTAuthentication]
    permission_classes = [IsCustomer]

    def get(self, request):
        from decimal import Decimal

        from .advances import advance_available

        client = request.user.client
        from .serializers import client_debt

        receipts = list(Receipt.objects.filter(client=client))
        # Та же функция, что карточка: чеки + входящий долг (волна 2).
        debt = client_debt(client)
        change = sum((r.change_due for r in receipts), Decimal("0"))
        advance = advance_available(client)
        return Response({
            "debt": debt, "change_due": change, "advance_balance": advance,
            "balance": debt - change - advance,
        })
