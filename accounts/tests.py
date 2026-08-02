"""Разметка адресов: чей префикс `/admin/`.

На `/admin/*` живут экраны самой системы (React-роутер: `/admin/finance`,
`/admin/catalog`, …). Пока там же стояла Django-админка, Django узнавал свой
префикс раньше фронтенда, и ПОЛНАЯ загрузка такого адреса — переход после
входа, F5, ссылка из чата — уводила человека на `/admin/login/?next=…`, то есть
на форму входа Django вместо системы.

На деве это не воспроизводилось: Vite проксирует на Django только `/api` и
`/media`, поэтому `/admin/finance` там всегда отдавал фронтенд. Поймали только
на проде, глазами. Эти тесты — чтобы не поймать второй раз.
"""
from django.test import TestCase


class AdminUrlSpaceTests(TestCase):
    def test_django_admin_moved_off_spa_prefix(self):
        r = self.client.get("/django-admin/login/", HTTP_HOST="localhost")
        self.assertEqual(r.status_code, 200)

    def test_spa_routes_are_not_captured_by_django(self):
        """Django не должен отвечать на адреса экранов системы.

        В проде на них nginx отдаёт index.html, и фронтенд разбирается сам;
        задача Django — не перехватить их раньше.
        """
        for path in ("/admin/finance", "/admin/catalog", "/admin/receipts"):
            with self.subTest(path=path):
                r = self.client.get(path, HTTP_HOST="localhost")
                self.assertEqual(r.status_code, 404, f"{path} перехвачен Django")
                self.assertNotIn("/admin/login/", r.headers.get("Location", ""))
