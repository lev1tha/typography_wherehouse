"""E2E API tests for the checkout flow, focused on inline-client handling.

Covers the reported bugs around "nothing saves":
- A sale for a client whose phone already exists (typed, not picked from the
  live search) must NOT fail with a unique-phone 400 — it reuses the client.
- A referrer chosen for a brand-new client must persist.
- A referrer chosen for an already-existing client fills in the referrer only
  if it was empty, and never lets the client refer themselves.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from accounts.models import User
from clients.models import Client
from sales.models import Receipt, TransactionItem
from services.models import PricingSettings, PrintingService
from warehouse.models import Material


class CheckoutClientAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="store_t", password="x", role=User.Role.STOREKEEPER
        )
        self.client.force_authenticate(self.user)
        self.material = Material.objects.create(
            name="Акрил", category="Пластик", unit="SQM",
            quantity=Decimal("100"), price_per_unit=Decimal("360"),
        )
        self.referrer = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Реферер", phone="+711101"
        )

    def _payload(self, client_dict):
        return {
            "payment_method": "CASH",
            "items": [{"type": "MATERIAL", "material": self.material.id, "quantity": 1}],
            "client": client_dict,
        }

    def test_new_client_with_referrer_persists(self):
        r = self.client.post(
            "/api/sales/receipts/checkout/",
            self._payload({
                "type": "PHYSICAL", "full_name": "Новый", "phone": "+711200",
                "referred_by": self.referrer.id,
            }),
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        created = Client.objects.get(phone="+711200")
        self.assertEqual(created.referred_by_id, self.referrer.id)

    def test_existing_phone_does_not_400(self):
        existing = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Уже Был", phone="+711102"
        )
        r = self.client.post(
            "/api/sales/receipts/checkout/",
            self._payload({
                "type": "PHYSICAL", "full_name": "Уже Был", "phone": "+711102",
                "referred_by": self.referrer.id,
            }),
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        existing.refresh_from_db()
        # Referrer was empty → now filled from the checkout payload.
        self.assertEqual(existing.referred_by_id, self.referrer.id)
        # No duplicate client was created.
        self.assertEqual(Client.objects.filter(phone="+711102").count(), 1)

    def test_existing_client_self_referral_ignored(self):
        existing = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Сам Себе", phone="+711103"
        )
        r = self.client.post(
            "/api/sales/receipts/checkout/",
            self._payload({
                "type": "PHYSICAL", "full_name": "Сам Себе", "phone": "+711103",
                "referred_by": existing.id,
            }),
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        existing.refresh_from_db()
        self.assertIsNone(existing.referred_by_id)

    def test_existing_referral_not_overwritten(self):
        other = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Другой", phone="+711104"
        )
        existing = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="С Рефером", phone="+711105",
            referred_by=other,
        )
        r = self.client.post(
            "/api/sales/receipts/checkout/",
            self._payload({
                "type": "PHYSICAL", "full_name": "С Рефером", "phone": "+711105",
                "referred_by": self.referrer.id,
            }),
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        existing.refresh_from_db()
        self.assertEqual(existing.referred_by_id, other.id)  # locked, unchanged


class CuttingPricingAPITests(APITestCase):
    """Cutting splits into work + material lines; whole-piece sales use piece_price."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="store_p", password="x", role=User.Role.STOREKEEPER
        )
        self.admin = User.objects.create_user(
            username="admin_p", password="x", role=User.Role.ADMIN
        )
        self.client.force_authenticate(self.user)
        # Area material sold both by кв.м and whole sheet.
        self.acrylic = Material.objects.create(
            name="Акрил 3мм", category="Акрил", unit="SQM",
            quantity=Decimal("100"), price_per_unit=Decimal("0"),
            price_per_sqm=Decimal("1400"), piece_price=Decimal("3700"),
            piece_area=Decimal("2.98"), cut_rate_per_pm=Decimal("20"),
        )
        self.cutting = PrintingService.objects.create(
            name="Резка букв", kind=PrintingService.Kind.CUTTING, rate_flat=Decimal("200"),
        )

    def _checkout(self, items):
        return self.client.post(
            "/api/sales/receipts/checkout/",
            {"payment_method": "CASH", "items": items}, format="json",
        )

    def test_cutting_splits_into_work_and_material_lines(self):
        # 0.5 × 0.5 = 0.25 кв.м cut. Work is computed from area × cut rate.
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "material": self.acrylic.id, "width": "0.5", "length": "0.5",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        items = list(receipt.items.all())
        self.assertEqual(len(items), 2)
        work = next(i for i in items if i.type == TransactionItem.Type.SERVICE)
        mat = next(i for i in items if i.type == TransactionItem.Type.MATERIAL)
        self.assertEqual(work.quantity, Decimal("0.25"))          # площадь (авто)
        self.assertEqual(work.price_per_item, Decimal("20"))      # ставка резки материала
        self.assertEqual(mat.quantity, Decimal("0.25"))           # площадь
        self.assertEqual(mat.price_per_item, Decimal("1400"))      # материал за кв.м
        # Total = 0.25×20 + 0.25×1400 = 5 + 350 = 355
        self.assertEqual(receipt.total_price, Decimal("355.00"))
        # Material stock deducted by area.
        self.acrylic.refresh_from_db()
        self.assertEqual(self.acrylic.quantity, Decimal("99.75"))

    def test_admin_price_overrides_applied(self):
        # Admin overrides catalogue prices at sale time: material 1400/кв.м, cut 50/кв.м.
        r = self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "material": self.acrylic.id, "width": "1.2", "length": "0.51",
            "material_price": "1400", "cut_rate": "50",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        work = receipt.items.get(type=TransactionItem.Type.SERVICE)
        mat = receipt.items.get(type=TransactionItem.Type.MATERIAL)
        self.assertEqual(work.price_per_item, Decimal("50"))       # overridden cut rate (/кв.м)
        self.assertEqual(mat.price_per_item, Decimal("1400"))      # overridden material price
        self.assertEqual(mat.quantity, Decimal("0.612"))          # 3-decimal area precision
        # Total = 0.612×50 + 0.612×1400 = 30.6 + 856.8 = 887.4
        self.assertEqual(receipt.total_price, Decimal("887.40"))

    def test_whole_sheet_sale_uses_piece_price_and_area(self):
        r = self._checkout([{
            "type": "MATERIAL", "material": self.acrylic.id, "quantity": 2, "mode": "PIECE",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        item = receipt.items.get()
        self.assertEqual(item.price_per_item, Decimal("3700"))
        self.assertEqual(receipt.total_price, Decimal("7400.00"))  # 2 × 3700
        # Stock deducted by piece_area × qty = 2.98 × 2 = 5.96
        self.acrylic.refresh_from_db()
        self.assertEqual(self.acrylic.quantity, Decimal("94.04"))

    def test_wholesale_price_kicks_in_at_min_qty(self):
        # Опт: акрил по 3700/лист, оптом 3000/лист от 3 листов.
        self.acrylic.wholesale_price = Decimal("3000")
        self.acrylic.wholesale_min_qty = Decimal("3")
        self.acrylic.save()
        # 2 листа — ниже минимума → обычная цена.
        r = self._checkout([{
            "type": "MATERIAL", "material": self.acrylic.id, "quantity": 2, "mode": "PIECE",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        item = Receipt.objects.get(pk=r.data["id"]).items.get()
        self.assertEqual(item.price_per_item, Decimal("3700"))      # розница
        # 3 листа — достигнут минимум → оптовая цена за каждый лист.
        r = self._checkout([{
            "type": "MATERIAL", "material": self.acrylic.id, "quantity": 3, "mode": "PIECE",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        receipt = Receipt.objects.get(pk=r.data["id"])
        item = receipt.items.get()
        self.assertEqual(item.price_per_item, Decimal("3000"))      # опт
        self.assertEqual(receipt.total_price, Decimal("9000.00"))   # 3 × 3000

    def test_no_wholesale_when_price_unset(self):
        # Минимум стоит по умолчанию (2), но оптовая цена не задана → опт не включается.
        r = self._checkout([{
            "type": "MATERIAL", "material": self.acrylic.id, "quantity": 5, "mode": "PIECE",
        }])
        self.assertEqual(r.status_code, 201, r.data)
        item = Receipt.objects.get(pk=r.data["id"]).items.get()
        self.assertEqual(item.price_per_item, Decimal("3700"))      # розница, опта нет

    def test_pay_debt_partial_then_full(self):
        # Продаём 1 лист за 3700 с предоплатой 1000 → долг 2700, статус PENDING.
        r = self.client.post(
            "/api/sales/receipts/checkout/",
            {
                "payment_method": "CASH",
                "amount_paid": 1000,
                "items": [{"type": "MATERIAL", "material": self.acrylic.id, "quantity": 1, "mode": "PIECE"}],
            },
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        rid = r.data["id"]
        receipt = Receipt.objects.get(pk=rid)
        self.assertEqual(receipt.payment_status, "PENDING")
        self.assertEqual(receipt.debt, Decimal("2700"))
        # Частичная оплата 700 → долг 2000, всё ещё PENDING.
        r = self.client.post(f"/api/sales/receipts/{rid}/pay/", {"amount": 700}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.debt, Decimal("2000"))
        self.assertEqual(receipt.payment_status, "PENDING")
        # Оплата без суммы → гасит весь остаток, статус PAID, долг 0.
        r = self.client.post(f"/api/sales/receipts/{rid}/pay/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        receipt.refresh_from_db()
        self.assertEqual(receipt.payment_status, "PAID")
        self.assertEqual(receipt.debt, Decimal("0"))
        # Повторная оплата уже закрытого чека → 400.
        r = self.client.post(f"/api/sales/receipts/{rid}/pay/", {"amount": 100}, format="json")
        self.assertEqual(r.status_code, 400, r.data)

    def test_pay_overpay_is_capped_to_debt(self):
        # Долг 3700; пытаемся внести 10000 → принимается только 3700, статус PAID.
        r = self.client.post(
            "/api/sales/receipts/checkout/",
            {
                "payment_method": "CASH",
                "amount_paid": 0,
                "items": [{"type": "MATERIAL", "material": self.acrylic.id, "quantity": 1, "mode": "PIECE"}],
            },
            format="json",
        )
        rid = r.data["id"]
        r = self.client.post(f"/api/sales/receipts/{rid}/pay/", {"amount": 10000}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        receipt = Receipt.objects.get(pk=rid)
        self.assertEqual(receipt.payment_status, "PAID")
        self.assertEqual(receipt.amount_paid, Decimal("3700"))

    def test_dashboard_splits_work_and_material_and_master_wage(self):
        PricingSettings.objects.update_or_create(
            pk=1, defaults={"master_commission_percent": Decimal("5")}
        )
        # 1×1 cut: work = 1×20 = 20, material = 1×1400 = 1400.
        self._checkout([{
            "type": "SERVICE", "service": self.cutting.id,
            "material": self.acrylic.id, "width": "1", "length": "1",
        }])
        self.client.force_authenticate(self.admin)
        data = self.client.get("/api/audit/dashboard/").data
        b = data["breakdown"]
        self.assertEqual(Decimal(b["work_revenue"]), Decimal("20"))
        self.assertEqual(Decimal(b["material_revenue"]), Decimal("1400"))
        self.assertEqual(Decimal(b["master_wage"]), Decimal("1.00"))  # 5% of 20

    def test_client_purchases_endpoint_admin_only(self):
        client = Client.objects.create(
            type=Client.Type.PHYSICAL, full_name="Покупатель", phone="+712001"
        )
        # Purchase tied to a client:
        self.client.post(
            "/api/sales/receipts/checkout/",
            {"payment_method": "CASH", "client_id": client.id,
             "items": [{"type": "MATERIAL", "material": self.acrylic.id, "quantity": 1, "mode": "PIECE"}]},
            format="json",
        )
        # Storekeeper forbidden.
        self.assertEqual(self.client.get("/api/audit/client-purchases/").status_code, 403)
        # Admin sees the client's material spend.
        self.client.force_authenticate(self.admin)
        rows = self.client.get("/api/audit/client-purchases/").data
        mine = next(x for x in rows if x["client_id"] == client.id)
        self.assertEqual(Decimal(mine["material_spend"]), Decimal("3700"))
