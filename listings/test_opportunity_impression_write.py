from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    CampusLocation, Category, DemandPost, Item, LostFoundPost, OpportunityImpression,
)


class OpportunityImpressionWriteTests(TestCase):
    """机会流渲染一次，就把「被看见」落一行。

    这张表是忽略率的分母。写入点只许成功、不许影响渲染：bulk_create 挂了，
    页面照常 200，只是这一轮没有分母。
    """

    def setUp(self):
        self.user = User.objects.create_user(username='impression-owner', password='safe-password-123')
        self.other = User.objects.create_user(username='impression-other', password='safe-password-123')
        self.category = Category.objects.create(name='曝光写入测试分类')
        self.location = CampusLocation.objects.create(name='曝光写入测试地点')
        self.item = Item.objects.create(
            seller=self.user,
            title='线性代数教材',
            description='本学期使用过的教材',
            category=self.category,
            location=self.location,
            condition='九成新',
            price='25.00',
        )

    def _make_demand(self, title='求购线性代数教材'):
        return DemandPost.objects.create(
            requester=self.other,
            title=title,
            description='希望在测试地点附近找到一本',
            category=self.category,
            location=self.location,
            max_price='40.00',
        )

    def _make_lost_found(self):
        # 机会流的失物线索要求用户自己先有一条相反类型的记录：这里给用户发一条「寻物」，
        # 再去对别人的「招领」。
        LostFoundPost.objects.create(
            reporter=self.user,
            post_type='lost',
            title='遗失线性代数教材',
            description='在测试地点附近遗失教材',
            category=self.category,
            location=self.location,
            occurred_at=timezone.now() - timedelta(hours=2),
        )
        return LostFoundPost.objects.create(
            reporter=self.other,
            post_type='found',
            title='捡到线性代数教材',
            description='在测试地点捡到教材',
            category=self.category,
            location=self.location,
            occurred_at=timezone.now() - timedelta(hours=1),
        )

    def test_rendering_the_feed_writes_one_row_per_shown_opportunity(self):
        demand = self._make_demand()
        lost = self._make_lost_found()
        self.client.force_login(self.user)

        response = self.client.get(reverse('opportunity_feed'))

        self.assertEqual(response.status_code, 200)
        rows = list(OpportunityImpression.objects.filter(user=self.user))
        self.assertEqual(len(rows), 2)
        by_kind = {row.kind: row for row in rows}
        self.assertEqual(by_kind['demand'].demand, demand)
        self.assertIsNone(by_kind['demand'].lost_found_post)
        self.assertEqual(by_kind['lost_found'].lost_found_post, lost)
        self.assertIsNone(by_kind['lost_found'].demand)
        for row in rows:
            self.assertLessEqual(row.shown_at, timezone.now())

    def test_no_opportunity_writes_no_row(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('opportunity_feed'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(OpportunityImpression.objects.count(), 0)

    def test_the_same_opportunity_shown_twice_writes_two_rows(self):
        demand = self._make_demand()
        self.client.force_login(self.user)

        self.client.get(reverse('opportunity_feed'))
        self.client.get(reverse('opportunity_feed'))

        rows = OpportunityImpression.objects.filter(user=self.user, demand=demand)
        self.assertEqual(rows.count(), 2)

    def test_a_broken_write_does_not_break_the_page(self):
        self._make_demand()
        self.client.force_login(self.user)

        with mock.patch.object(OpportunityImpression.objects, 'bulk_create', side_effect=RuntimeError('曝光表写不进去')):
            response = self.client.get(reverse('opportunity_feed'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(OpportunityImpression.objects.count(), 0)

    def test_rows_are_attached_to_the_viewing_user(self):
        self._make_demand()
        self.client.force_login(self.other)

        # 别人没有在售商品，机会流对他就是空的：没有机会就没有分母。
        response = self.client.get(reverse('opportunity_feed'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(OpportunityImpression.objects.count(), 0)

        self.client.logout()
        self.client.force_login(self.user)
        self.client.get(reverse('opportunity_feed'))
        rows = OpportunityImpression.objects.all()
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows[0].user, self.user)
        self.assertEqual(rows[0].kind, 'demand')
