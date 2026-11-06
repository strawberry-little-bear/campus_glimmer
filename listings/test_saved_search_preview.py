"""关注搜索卡片中的最近命中预览。

预览只用于让用户确认提醒内容，不改变 SavedSearchMatch 的缓冲、通知和清理规则。
因此这里重点钉住三件事：只展示当前用户、只展示仍可查看的商品、预览数量受单次
提醒上限约束。已下架或过期的命中仍计入原始待提醒数量，但绝不能变成可点击链接。
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Category, Item, SavedSearch, SavedSearchMatch


class SavedSearchPreviewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='preview-user', password='safe-password-123',
        )
        self.seller = User.objects.create_user(
            username='preview-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='预览测试分类')
        self.saved_search = SavedSearch.objects.create(
            user=self.user,
            name='预览测试关注',
            query='预览',
            max_matches_per_notice=3,
        )
        self.client.force_login(self.user)

    def make_item(self, title, **kwargs):
        defaults = {
            'seller': self.seller,
            'title': title,
            'description': '预览测试描述',
            'price': Decimal('28.00'),
            'condition': '九成新',
            'status': 'available',
            'category': self.category,
        }
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def make_match(self, item, *, matched_at=None, notified_at=None, saved_search=None):
        match = SavedSearchMatch.objects.create(
            saved_search=saved_search or self.saved_search,
            item=item,
            notified_at=notified_at,
        )
        if matched_at is not None:
            SavedSearchMatch.objects.filter(pk=match.pk).update(matched_at=matched_at)
            match.refresh_from_db()
        return match

    def card(self, response):
        return next(
            row for row in response.context['saved_searches']
            if row.pk == self.saved_search.pk
        )

    def test_preview_shows_newest_three_available_matches_and_remaining_count(self):
        now = timezone.now()
        items = [self.make_item(f'预览商品 {index}') for index in range(5)]
        for index, item in enumerate(items):
            self.make_match(item, matched_at=now - timedelta(minutes=index))

        response = self.client.get(reverse('saved_search_list'))
        card = self.card(response)

        self.assertEqual(card.pending_match_count, 5)
        self.assertEqual(card.available_pending_match_count, 5)
        self.assertEqual(
            [match.item.title for match in card.pending_preview_matches],
            ['预览商品 0', '预览商品 1', '预览商品 2'],
        )
        self.assertEqual(card.remaining_pending_match_count, 2)
        self.assertContains(response, '最近命中')
        self.assertContains(response, '另有 2 条仍可查看的命中')
        for item in items[:3]:
            self.assertContains(response, reverse('item_detail', args=[item.pk]))
        self.assertNotContains(response, reverse('item_detail', args=[items[4].pk]))

    def test_preview_respects_each_searches_notice_limit(self):
        self.saved_search.max_matches_per_notice = 2
        self.saved_search.save(update_fields=['max_matches_per_notice', 'updated_at'])
        now = timezone.now()
        items = [self.make_item(f'限额商品 {index}') for index in range(3)]
        for index, item in enumerate(items):
            self.make_match(item, matched_at=now - timedelta(minutes=index))

        response = self.client.get(reverse('saved_search_list'))
        card = self.card(response)

        self.assertEqual(card.pending_preview_limit, 2)
        self.assertEqual(len(card.pending_preview_matches), 2)
        self.assertEqual(card.remaining_pending_match_count, 1)
        self.assertContains(response, '通知最多列出 2 条')

    def test_sold_expired_and_notified_matches_never_become_links(self):
        available = self.make_item('仍可查看的命中')
        sold = self.make_item('已经卖出的命中', status='sold')
        expired = self.make_item(
            '已经过期的命中', expires_at=timezone.now() - timedelta(hours=1),
        )
        notified = self.make_item('已经提醒过的命中')
        self.make_match(available)
        self.make_match(sold)
        self.make_match(expired)
        self.make_match(notified, notified_at=timezone.now())

        response = self.client.get(reverse('saved_search_list'))
        card = self.card(response)

        self.assertEqual(card.pending_match_count, 3)
        self.assertEqual(card.available_pending_match_count, 1)
        self.assertEqual(
            [match.item.title for match in card.pending_preview_matches],
            ['仍可查看的命中'],
        )
        self.assertContains(response, reverse('item_detail', args=[available.pk]))
        self.assertNotContains(response, reverse('item_detail', args=[sold.pk]))
        self.assertNotContains(response, reverse('item_detail', args=[expired.pk]))
        self.assertNotContains(response, reverse('item_detail', args=[notified.pk]))

    def test_all_pending_matches_unavailable_shows_clear_empty_state(self):
        sold = self.make_item('不能承诺的失效商品', status='sold')
        self.make_match(sold)

        response = self.client.get(reverse('saved_search_list'))

        self.assertContains(response, '待提醒里暂时没有仍可查看的商品')
        self.assertContains(response, '待提醒 1 条')
        self.assertNotContains(response, reverse('item_detail', args=[sold.pk]))

    def test_no_pending_matches_uses_the_regular_empty_state(self):
        self.make_item('只在售但尚未命中')

        response = self.client.get(reverse('saved_search_list'))

        self.assertContains(response, '暂无待提醒命中，新商品出现后会在这里预览')

    def test_another_users_pending_match_does_not_leak_into_the_preview(self):
        other = User.objects.create_user(
            username='preview-other', password='safe-password-123',
        )
        other_search = SavedSearch.objects.create(user=other, name='别人的预览关注')
        other_item = self.make_item('别人的命中商品')
        self.make_match(other_item, saved_search=other_search)

        response = self.client.get(reverse('saved_search_list'))

        self.assertNotContains(response, '别人的预览关注')
        self.assertNotContains(response, '别人的命中商品')
        self.assertNotContains(response, reverse('item_detail', args=[other_item.pk]))
