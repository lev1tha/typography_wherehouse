"""Загрузка фото материала: потолки по размеру/пикселям и безопасное имя."""
import io
import os
import re
import tempfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework.test import APITestCase

from accounts.models import User
from warehouse import serializers as wh_serializers
from warehouse.models import Material, MaterialImage

URL = "/api/warehouse/material-images/"


def png(size=(10, 10), mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, size).save(buf, "PNG")
    return buf.getvalue()


class MaterialImageUploadTests(APITestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.media = override_settings(MEDIA_ROOT=self.tmp.name)
        self.media.enable()
        self.addCleanup(self.media.disable)
        self.admin = User.objects.create_user(username="img_admin", password="x", role=User.Role.ADMIN)
        self.client.force_authenticate(self.admin)
        self.material = Material.objects.create(name="Форекс", unit=Material.Unit.SQM)

    def _upload(self, content, name="photo.png"):
        return self.client.post(
            URL,
            {"material": self.material.pk, "image": SimpleUploadedFile(name, content, "image/png")},
            format="multipart",
        )

    def test_normal_photo_is_stored_under_a_generated_name(self):
        r = self._upload(png(), name="../../evil name.PHP.png")
        self.assertEqual(r.status_code, 201, r.data)
        stored = MaterialImage.objects.get().image.name
        self.assertRegex(stored, r"^materials/[0-9a-f]{32}\.png$")
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, stored)))

    def test_extension_follows_the_real_format_not_the_filename(self):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buf, "JPEG")
        r = self._upload(buf.getvalue(), name="pic.png")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(MaterialImage.objects.get().image.name.endswith(".jpg"))

    def test_huge_pixel_count_is_rejected_before_processing(self):
        # 1-бит PNG 9000×8000 = 72 млн пикселей: весит килобайты, а в памяти — сотни МБ.
        r = self._upload(png((9000, 8000), mode="1"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("слишком большое", str(r.data))
        self.assertEqual(MaterialImage.objects.count(), 0)

    def test_oversized_file_is_rejected(self):
        with mock.patch.object(wh_serializers, "MAX_IMAGE_BYTES", 10):
            r = self._upload(png((64, 64)))
        self.assertEqual(r.status_code, 400)
        self.assertIn("слишком большой", str(r.data))

    def test_not_an_image_is_rejected(self):
        r = self._upload(b"<?php echo 1; ?>", name="x.png")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(MaterialImage.objects.count(), 0)

    def test_unlisted_format_is_rejected(self):
        buf = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buf, "BMP")
        r = self._upload(buf.getvalue(), name="x.bmp")
        self.assertEqual(r.status_code, 400)
        self.assertIn("JPEG, PNG", str(r.data))

    def test_storekeeper_cannot_upload(self):
        keeper = User.objects.create_user(username="img_keeper", password="x", role=User.Role.STOREKEEPER)
        self.client.force_authenticate(keeper)
        self.assertEqual(self._upload(png()).status_code, 403)
