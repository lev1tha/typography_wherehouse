"""Реквизиты организации — шапка печатных документов.

Права здесь нестандартные и потому проверяются отдельно: ЧИТАТЬ их должен
любой сотрудник (накладную и товарный чек печатает складовщик, а без шапки
документ выдавать нельзя), а МЕНЯТЬ — только админ. Остальные настройки
финотчёта складовщику вообще закрыты, поэтому реквизиты и живут на своём
эндпоинте, а не в `/finance/settings/`.
"""
from rest_framework.test import APITestCase

from accounts.models import User
from finance.models import CompanyProfile


class CompanyProfileTests(APITestCase):
    URL = "/api/finance/company/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="co_admin", password="x", role=User.Role.ADMIN
        )
        self.keeper = User.objects.create_user(
            username="co_keeper", password="x", role=User.Role.STOREKEEPER
        )
        self.accountant = User.objects.create_user(
            username="co_acc", password="x", role=User.Role.ACCOUNTANT
        )

    def test_storekeeper_reads_requisites(self):
        """Накладную печатает он — шапка нужна ему так же, как админу."""
        self.client.force_authenticate(self.keeper)
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIn("name", resp.data)

    def test_accountant_reads_requisites(self):
        self.client.force_authenticate(self.accountant)
        self.assertEqual(self.client.get(self.URL).status_code, 200)

    def test_storekeeper_cannot_change_them(self):
        self.client.force_authenticate(self.keeper)
        resp = self.client.patch(self.URL, {"name": "Не моё"}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(CompanyProfile.load().name, "")

    def test_accountant_cannot_change_them(self):
        """Бухгалтер — проверяющий: смотрит всё, не меняет ничего."""
        self.client.force_authenticate(self.accountant)
        self.assertEqual(
            self.client.patch(self.URL, {"name": "Не моё"}, format="json").status_code, 403
        )

    def test_admin_saves_and_reads_back(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.patch(
            self.URL,
            {
                "name": "ОсОО «ЧПУ Центр»",
                "inn": "01234567890123",
                "bank_name": "ОАО «Оптима Банк»",
                "bank_account": "1090820000123456",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["name"], "ОсОО «ЧПУ Центр»")
        self.assertEqual(CompanyProfile.load().inn, "01234567890123")

    def test_profile_is_a_singleton(self):
        """Сколько ни сохраняй — запись одна: это реквизиты цеха, а не список."""
        self.client.force_authenticate(self.admin)
        self.client.patch(self.URL, {"name": "Раз"}, format="json")
        self.client.patch(self.URL, {"name": "Два"}, format="json")
        self.assertEqual(CompanyProfile.objects.count(), 1)
        self.assertEqual(CompanyProfile.load().name, "Два")

    def test_has_bank_gates_the_invoice(self):
        """Счёт на оплату без банка и расчётного счёта клиенту бесполезен —
        интерфейс прячет его именно по этому флагу."""
        self.client.force_authenticate(self.admin)
        self.assertFalse(self.client.get(self.URL).data["has_bank"])
        self.client.patch(
            self.URL, {"bank_name": "Банк", "bank_account": "123"}, format="json"
        )
        self.assertTrue(self.client.get(self.URL).data["has_bank"])

    def test_anonymous_gets_nothing(self):
        self.assertEqual(self.client.get(self.URL).status_code, 401)
