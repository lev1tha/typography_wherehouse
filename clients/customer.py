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

from sales.models import Receipt

from .models import Client


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
            "created_at",
            "payment_status",
            "fulfillment_status",
            "total_price",
            "amount_paid",
            "debt",
            "items",
        ]

    def get_items(self, obj):
        rows = [i for i in obj.items.all() if not i.is_returned]
        return CustomerItemSerializer(rows, many=True).data


MIN_PORTAL_PASSWORD = 4


class CustomerLoginView(APIView):
    """POST /api/customer/login/ — вход клиента по телефону + собственному паролю.

    Шаг 1: клиент присылает только `phone`. Отвечаем, узнан ли он и что делать:
      - `status=set_password` — пароль ещё не задан, пусть придумает (первый вход);
      - `status=need_password` — пароль есть, пусть введёт.
    Шаг 2: `phone` + `password`. Если пароля не было — задаём его; если был —
    проверяем. Успех → выдаём клиентский токен. Так чужой номер уже не откроет
    заказы: нужен ещё и пароль, который клиент задал себе сам.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        phone = _digits(request.data.get("phone"))
        password = (request.data.get("password") or "").strip()
        if not phone:
            return Response({"detail": "Введите номер телефона"}, status=status.HTTP_400_BAD_REQUEST)
        client = next((c for c in Client.objects.all() if _digits(c.phone) == phone), None)
        if not client:
            return Response(
                {"detail": "Клиент с таким номером не найден"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not client.has_password:
            # Первый вход — клиент задаёт себе пароль.
            if not password:
                return Response({"status": "set_password", "name": client.display_name})
            if len(password) < MIN_PORTAL_PASSWORD:
                return Response(
                    {"detail": f"Пароль минимум {MIN_PORTAL_PASSWORD} символа."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            client.set_password(password)
            client.save(update_fields=["portal_password"])
            return self._token_response(client)

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
