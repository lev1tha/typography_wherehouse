from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _


class User(AbstractUser):
    """Staff account: Admin, Storekeeper and Accountant.

    Login uses only username + password (see the auth endpoint); email is
    optional. Roles drive both API permissions and frontend routing.
    """

    class Role(models.TextChoices):
        ADMIN = "ADMIN", _("Администратор")
        STOREKEEPER = "STOREKEEPER", _("Складовщик")
        # Проверяющий, а не участник: смотрит журнал действий, финансы и чеки
        # (с себестоимостью и маржой), но НИЧЕГО не меняет. Заводить продажи,
        # принимать деньги и трогать склад он не может — иначе он проверял бы
        # собственную работу.
        ACCOUNTANT = "ACCOUNTANT", _("Бухгалтер")

    role = models.CharField(
        _("роль"),
        max_length=20,
        choices=Role.choices,
        default=Role.STOREKEEPER,
    )

    # Версия учётных данных. Входит в токен (клейм `cv`); смена пароля её
    # увеличивает, и все выданные до этого токены перестают приниматься.
    # Токен без клейма считается версией 0 — выданные до введения поля токены
    # живут, пока пароль не меняли (массового разлогина нет).
    credentials_version = models.PositiveIntegerField(
        _("версия учётных данных"), default=0, editable=False
    )

    _loaded_password = None

    @classmethod
    def from_db(cls, db, field_names, values):
        user = super().from_db(db, field_names, values)
        user._loaded_password = user.__dict__.get("password")
        return user

    def refresh_from_db(self, *args, **kwargs):
        super().refresh_from_db(*args, **kwargs)
        fields = kwargs.get("fields") or (args[1] if len(args) > 1 else None)
        if fields is None or "password" in fields:
            self._loaded_password = self.__dict__.get("password")

    def save(self, *args, **kwargs):
        """Смена пароля отзывает выданные токены.

        Пароль сотрудника меняют в Django-админке и командой `changepassword` —
        оба сохраняют объект целиком. Исключение — перехеширование при входе
        (Django сам обновляет хеш на новые параметры, `update_fields=["password"]`):
        пароль тот же, токены других устройств не трогаем.
        """
        update_fields = kwargs.get("update_fields")
        rehash_only = update_fields is not None and set(update_fields) == {"password"}
        if (
            self.pk
            and self._loaded_password is not None
            and self.password != self._loaded_password
            and not rehash_only
        ):
            self.credentials_version = (self.credentials_version or 0) + 1
            if update_fields is not None:
                kwargs["update_fields"] = {*update_fields, "credentials_version"}
        super().save(*args, **kwargs)
        self._loaded_password = self.__dict__.get("password")

    @property
    def is_admin_role(self) -> bool:
        return self.role == self.Role.ADMIN

    @property
    def is_accountant_role(self) -> bool:
        return self.role == self.Role.ACCOUNTANT

    @property
    def sees_money(self) -> bool:
        """Кому видны закупочные цифры: себестоимость, маржа, финансовый отчёт.

        Складовщик оформляет и выдаёт заказы, но почём цех купил материал, ему
        знать незачем. Бухгалтеру — ровно наоборот: цифры это его работа.
        """
        return self.is_admin_role or self.is_accountant_role

    def __str__(self) -> str:
        return f"{self.username} ({self.get_role_display()})"


class Profile(models.Model):
    """Extra staff details kept separate from the auth-critical User fields."""

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="profile"
    )
    phone = models.CharField(_("телефон"), max_length=32, blank=True)
    telegram_chat_id = models.CharField(
        _("Telegram chat id"), max_length=64, blank=True
    )

    def __str__(self) -> str:
        return f"Профиль {self.user.username}"


class Employee(models.Model):
    """Сотрудник цеха — человек, которому считают зарплату.

    Не то же самое, что `User`. Учётная запись — это ВХОД в систему, и на цех из
    трёх мастеров их две-три на всех («Чпу», «Лазер»): мастера работают под
    общими логинами по станкам. Зарплату же считают по человеку, поэтому
    сотрудник — отдельная запись; связь с учёткой необязательна (мастер, у
    которого своего входа нет, — обычный случай).

    Из цеха сотрудников не удаляют, а отключают (`is_active`): выплаты и
    начисления прошлых месяцев ссылаются на них и должны остаться читаемыми.
    """

    class Machine(models.TextChoices):
        # Те же коды, что у `services.PrintingService.Machine`: сотрудник — «за
        # ЧПУ» или «за лазером» — получает выработку этого станка, пока в строке
        # заказа нет своего поля «исполнитель».
        CNC = "CNC", _("ЧПУ")
        LASER = "LASER", _("Лазер")

    full_name = models.CharField(_("ФИО"), max_length=255)
    position = models.CharField(_("должность"), max_length=120, blank=True)
    default_machine = models.CharField(
        _("станок по умолчанию"), max_length=10, choices=Machine.choices, blank=True, default="",
        help_text=_("Выработка станка идёт этому сотруднику, если он на станке один."),
    )
    is_active = models.BooleanField(_("работает"), default=True)
    user = models.OneToOneField(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="employee", verbose_name=_("учётная запись"),
        help_text=_("Заказы, оформленные под этим логином, считаются его выработкой."),
    )
    note = models.CharField(_("примечание"), max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("сотрудник")
        verbose_name_plural = _("сотрудники")
        ordering = ["-is_active", "full_name", "id"]

    def __str__(self) -> str:
        return self.full_name
