"""Склейка карточек не замыкает реферальное кольцо (CLI-12).

`ClientSerializer.validate_referred_by` не пускает «А привёл Б, Б привёл А», а
склейка переносила рефереров мимо этой проверки: остающаяся карточка
усыновляла реферера удаляемой — и если тот был приведён самой остающейся,
получалось кольцо, а по такой паре бонусы считаются в обе стороны.
"""
from clients.merge import MergeRejected, merge_clients, merge_summary
from clients.models import Client
from clients.testkit import ShopCase


def make(name, phone, **kw):
    return Client.objects.create(full_name=name, phone=phone, **kw)


class MergeRingTests(ShopCase):
    def test_adopting_a_referrer_who_descends_from_keep_is_refused(self):
        keep = make("Остаётся", "+996700000101")
        w = make("Приведён остающимся", "+996700000102", referred_by=keep)
        drop = make("Удаляется", "+996700000103", referred_by=w)       # drop ← w ← keep
        with self.assertRaises(MergeRejected) as ctx:
            merge_clients(keep, drop, user=self.admin)
        self.assertIn("кольцо", str(ctx.exception))
        drop.refresh_from_db()
        self.assertEqual(drop.referred_by_id, w.id)       # ничего не поехало
        self.assertTrue(Client.objects.filter(pk=drop.pk).exists())

    def test_referee_of_drop_that_is_an_ancestor_of_keep_is_refused(self):
        top = make("Верхний", "+996700000104")
        keep = make("Остаётся", "+996700000105", referred_by=top)       # keep ← top
        drop = make("Удаляется", "+996700000106")
        top.referred_by = drop                                            # top ← drop
        top.save()
        # после склейки: top ← keep ← top
        with self.assertRaises(MergeRejected):
            merge_clients(keep, drop, user=self.admin)

    def test_preview_warns_before_the_click(self):
        keep = make("Остаётся", "+996700000101")
        w = make("Приведён остающимся", "+996700000102", referred_by=keep)
        drop = make("Удаляется", "+996700000103", referred_by=w)
        self.assertTrue(merge_summary(keep, drop)["ring"])
        r = self.client.get(f"/api/clients/clients/{keep.id}/merge-preview/", {"from": drop.id})
        self.assertTrue(r.data["ring"])
        r = self.client.post(f"/api/clients/clients/{keep.id}/merge/", {"from": drop.id}, format="json")
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("кольцо", r.data["detail"])

    def test_ordinary_merge_still_works(self):
        keep = make("Остаётся", "+996700000101")
        boss = make("Босс", "+996700000102")
        drop = make("Удаляется", "+996700000103", referred_by=boss)
        kid = make("Ребёнок", "+996700000104", referred_by=drop)
        self.assertFalse(merge_summary(keep, drop)["ring"])
        merge_clients(keep, drop, user=self.admin)
        keep.refresh_from_db(); kid.refresh_from_db()
        self.assertEqual(keep.referred_by_id, boss.id)
        self.assertEqual(kid.referred_by_id, keep.id)

    def test_keep_referred_by_drop_is_cleared_not_a_ring(self):
        drop = make("Удаляется", "+996700000103")
        keep = make("Остаётся", "+996700000101", referred_by=drop)
        merge_clients(keep, drop, user=self.admin)
        keep.refresh_from_db()
        self.assertIsNone(keep.referred_by_id)
