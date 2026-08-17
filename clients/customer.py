"""Customer self-service portal: a Client (not a staff User) logs in by phone
and views only their own orders (status + debt). Uses a dedicated JWT scope so
staff tokens and customer tokens can never cross into each other's endpoints.
"""
import re

from rest_framework import exceptions, serializers, status
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import AccessToken

from accounts.throttling import CustomerLoginThrottle, LoginAccountThrottle
from accounts.views import throttled_response
from sales.models import Receipt

from .models import Client
from .phones import find_client_by_phone


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


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
        return CustomerIdentity(client)


class IsCustomer(BasePermission):
    def has_permission(self, request, view):
        return bool(getattr(request.user, "client", None))


def mint_customer_token(client: Client) -> str:
    token = AccessToken()
    token["scope"] = "customer"
    token["client_id"] = client.id
    token["name"] = client.display_name
    return str(token)


class CustomerItemSerializer(serializers.Serializer):
    title = serializers.SerializerMethodField()
    quantity = serializers.DecimalField(max_digits=12, decimal_places=2)
    line_total = serializers.DecimalField(max_digits=14, decimal_places=2)
    unit = serializers.SerializerMethodField()
    is_returned = serializers.BooleanField(read_only=True)

    def get_unit(self, obj):
        """Единица рядом с количеством: клиенту «× 0.99» ни о чём не говорит."""
        if obj.material_id:
            return obj.material.unit
        return "METER" if getattr(obj.service, "uses_running_meter", False) else ""

    def get_title(self, obj):
        if obj.material_id:
            return obj.material.name
        if obj.service_id:
            return obj.service.name
        return "—"


class CustomerOrderSerializer(serializers.ModelSerializer):
    debt = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    items = serializers.SerializerMethodField()

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
            "debt",
            # Сдача, которую цех клиенту ещё не отдал. В кабинете её не было
            # вовсе: он видел, сколько должен ОН, но не видел, сколько должны
            # ЕМУ, — при том что эта сдача идёт в оплату его следующего заказа.
            "change_due",
            "status",
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
        raise throttled_response(wait)

    def post(self, request):
        phone = _digits(request.data.get("phone"))
        password = (request.data.get("password") or "").strip()
        if not phone:
            return Response({"detail": "Введите номер телефона"}, status=status.HTTP_400_BAD_REQUEST)
        # Тот же поиск, что и в кассе: клиент набирает свой номер как привык, а
        # в базе он лежит в том написании, в каком его записал кассир. Пароль
        # по-прежнему обязателен — послаблений тут нет, только формат номера.
        client = find_client_by_phone(request.data.get("phone"))
        if not client:
            return Response(
                {"detail": "Клиент с таким номером не найден"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not client.has_password:
            # Пароль ещё не выдан — вход невозможен, отправляем к администратору.
            return Response(
                {"status": "no_password", "name": client.display_name}
            )

        # Пароль уже задан — просим ввести и проверяем.
        if not password:
            return Response({"status": "need_password", "name": client.display_name})
        if not client.check_password(password):
            return Response({"detail": "Неверный пароль."}, status=status.HTTP_400_BAD_REQUEST)
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
