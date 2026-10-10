"""Команда `seed` не должна ломать живую базу при повторном запуске.

Аудит 2026-10: seed безусловно ставил «Резке букв» ставку 200, стирал её
техкарты и воскрешал удалённые учётки с известными паролями. Здесь фиксируется
обратное: повторный запуск ничего не меняет и не возвращает.
"""
import os
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from accounts.models import User
from services.models import PrintingService, ServiceRecipe
from warehouse.models import Material

PWD = "Seed-test-Kx93mQ"


def run_seed(*args, **kwargs):
    out = StringIO()
    call_command("seed", *args, stdout=out, **kwargs)
    return out.getvalue()


class SeedUsersTests(TestCase):
    def test_empty_db_requires_password(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SEED_PASSWORD", None)
            with self.assertRaises(CommandError):
                run_seed()
        # ошибка до записи: ни учёток, ни каталога
        self.assertFalse(User.objects.exists())
        self.assertFalse(Material.objects.filter(name="Бумага офсетная").exists())

    def test_weak_password_rejected(self):
        with self.assertRaises(CommandError):
            run_seed(password="123")
        self.assertFalse(User.objects.exists())

    def test_creates_users_on_empty_table_with_given_password(self):
        run_seed(password=PWD)
        self.assertEqual(
            set(User.objects.values_list("username", flat=True)),
            {"admin", "storekeeper", "accountant"},
        )
        admin = User.objects.get(username="admin")
        self.assertTrue(admin.check_password(PWD))
        self.assertTrue(admin.is_superuser)
        self.assertFalse(User.objects.get(username="storekeeper").is_superuser)

    def test_password_from_environment(self):
        with mock.patch.dict(os.environ, {"SEED_PASSWORD": PWD}):
            run_seed()
        self.assertTrue(User.objects.get(username="accountant").check_password(PWD))

    def test_default_passwords_are_gone(self):
        run_seed(password=PWD)
        for username, old in [("admin", "admin12345"), ("storekeeper", "store12345"),
                              ("accountant", "acc12345")]:
            self.assertFalse(User.objects.get(username=username).check_password(old))

    def test_rerun_does_not_resurrect_deleted_user(self):
        run_seed(password=PWD)
        User.objects.filter(username="accountant").delete()
        out = run_seed(password=PWD)
        self.assertFalse(User.objects.filter(username="accountant").exists())
        self.assertIn("не создаю", out)

    def test_rerun_does_not_touch_existing_password_or_role(self):
        run_seed(password=PWD)
        admin = User.objects.get(username="admin")
        admin.set_password("Changed-by-owner-77")
        admin.save()
        run_seed(password=PWD)
        run_seed(password=PWD, create_users=True)
        admin.refresh_from_db()
        self.assertTrue(admin.check_password("Changed-by-owner-77"))

    def test_nonempty_user_table_skips_users_without_password_error(self):
        User.objects.create_user(username="owner", password="x-Owner-pass-1")
        run_seed()  # пароль не нужен: учётки не создаются
        self.assertEqual(User.objects.count(), 1)

    def test_explicit_flag_creates_only_missing(self):
        User.objects.create_user(username="admin", password="x-Owner-pass-1")
        run_seed(password=PWD, create_users=True)
        self.assertTrue(User.objects.get(username="admin").check_password("x-Owner-pass-1"))
        self.assertTrue(User.objects.filter(username="storekeeper").exists())
        self.assertTrue(User.objects.filter(username="accountant").exists())

    def test_no_users_flag(self):
        run_seed(no_users=True)
        self.assertFalse(User.objects.exists())
        self.assertTrue(Material.objects.filter(name="Бумага офсетная").exists())


class SeedCatalogueTests(TestCase):
    def setUp(self):
        run_seed(no_users=True)

    def test_rerun_keeps_owner_rates(self):
        cutting = PrintingService.objects.get(name="Резка букв")
        cutting.rate_flat = Decimal("350")
        cutting.save()
        acrylic = Material.objects.get(name="Белый акрил")
        acrylic.price_per_sqm = Decimal("0")
        acrylic.cut_rate_per_pm = Decimal("33")
        acrylic.save()
        sheet = Material.objects.get(name="Форекс 3мм")
        sheet.sheet_width = None
        sheet.sheet_height = None
        sheet.save()

        run_seed(no_users=True)

        cutting.refresh_from_db()
        self.assertEqual(cutting.rate_flat, Decimal("350"))
        acrylic.refresh_from_db()
        self.assertEqual(acrylic.price_per_sqm, Decimal("0"))
        self.assertEqual(acrylic.cut_rate_per_pm, Decimal("33"))
        sheet.refresh_from_db()
        self.assertFalse(sheet.sheet_width)

    def test_rerun_does_not_delete_recipes(self):
        cutting = PrintingService.objects.get(name="Резка букв")
        paper = Material.objects.get(name="Бумага офсетная")
        ServiceRecipe.objects.create(service=cutting, material=paper, consumption_per_unit=Decimal("1"))
        run_seed(no_users=True)
        self.assertEqual(cutting.recipes.count(), 1)

    def test_renamed_service_is_not_duplicated(self):
        cutting = PrintingService.objects.get(name="Резка букв")
        cutting.name = "Фрезеровка ЧПУ"
        cutting.save()
        run_seed(no_users=True)
        self.assertEqual(
            PrintingService.objects.filter(
                kind=PrintingService.Kind.CUTTING, machine=PrintingService.Machine.CNC
            ).count(),
            1,
        )
        self.assertFalse(PrintingService.objects.filter(name="Резка букв").exists())

    def test_rerun_keeps_owner_translations_and_active_flag(self):
        cutting = PrintingService.objects.get(name="Резка букв")
        cutting.name_en = "Owner's cutting"
        cutting.is_active = False
        cutting.save()
        run_seed(no_users=True)
        cutting.refresh_from_db()
        self.assertEqual(cutting.name_en, "Owner's cutting")
        self.assertFalse(cutting.is_active)

    def test_rerun_creates_no_duplicates(self):
        before = (Material.objects.count(), PrintingService.objects.count())
        run_seed(no_users=True)
        run_seed(no_users=True)
        self.assertEqual((Material.objects.count(), PrintingService.objects.count()), before)
