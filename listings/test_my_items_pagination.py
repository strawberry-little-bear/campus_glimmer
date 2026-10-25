from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Category, Item


class MyItemsPaginationTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username='my-items-pager', password='safe-password-123',
        )
        self.other = User.objects.create_user(
            username='my-items-other', password='safe-password-123',
        )
        self.category = Category.objects.create(name='卖家工作台分页分类')
        self.client.force_login(self.seller)

    def make_item(self, title, *, status='available', seller=None, created_at=None):
        item = Item.objects.create(
            title=title,
            description='卖家工作台分页测试商品',
            price='12.00',
            category=self.category,
            condition='9成新',
            seller=seller or self.seller,
            status=status,
        )
        if created_at is not None:
            Item.objects.filter(pk=item.pk).update(created_at=created_at)
            item.refresh_from_db()
        return item

    def test_workspace_uses_twelve_items_per_page_and_out_of_range_falls_back(self):
        for index in range(13):
            self.make_item(f'分页商品 {index:02d}')

        first = self.client.get(reverse('my_items'))
        second = self.client.get(reverse('my_items'), {'page': 2})
        too_high = self.client.get(reverse('my_items'), {'page': 99})
        invalid = self.client.get(reverse('my_items'), {'page': 'not-a-number'})

        self.assertEqual(first.context['page_obj'].paginator.count, 13)
        self.assertEqual(len(first.context['items']), 12)
        self.assertEqual(second.context['page_obj'].number, 2)
        self.assertEqual(len(second.context['items']), 1)
        self.assertEqual(too_high.context['page_obj'].number, 2)
        self.assertEqual(invalid.context['page_obj'].number, 1)
        self.assertContains(first, '第 1–12 件 · 共 13 件')
        self.assertContains(second, '第 13–13 件 · 共 13 件')

    def test_unknown_tab_falls_back_to_all(self):
        self.make_item('在售教材')
        self.make_item('已预订台灯', status='reserved')

        response = self.client.get(reverse('my_items'), {'tab': 'not-a-status'})

        self.assertEqual(response.context['selected_tab'], 'all')
        self.assertEqual(len(response.context['items']), 2)
        self.assertContains(response, 'aria-current="page"')

    def test_status_tabs_are_isolated_while_counts_stay_workspace_wide(self):
        for index in range(13):
            self.make_item(f'在售商品 {index:02d}')
        reserved = self.make_item('已预订商品', status='reserved')
        sold = self.make_item('已售出商品', status='sold')
        expired = self.make_item('已过期商品', status='expired')

        reserved_page = self.client.get(reverse('my_items'), {'tab': 'reserved'})
        sold_page = self.client.get(reverse('my_items'), {'tab': 'sold'})
        expired_page = self.client.get(reverse('my_items'), {'tab': 'expired'})

        self.assertEqual([item.pk for item in reserved_page.context['items']], [reserved.pk])
        self.assertEqual([item.pk for item in sold_page.context['items']], [sold.pk])
        self.assertEqual([item.pk for item in expired_page.context['items']], [expired.pk])
        self.assertEqual(
            reserved_page.context['item_counts'],
            {'all': 16, 'available': 13, 'reserved': 1, 'sold': 1, 'expired': 1},
        )

    def test_other_sellers_items_never_appear_in_any_tab(self):
        self.make_item('我的商品')
        self.make_item('别人的商品', seller=self.other)

        for tab in ('all', 'available', 'reserved', 'sold', 'expired'):
            response = self.client.get(reverse('my_items'), {'tab': tab})
            self.assertNotContains(response, '别人的商品')
            self.assertEqual(response.context['item_counts']['all'], 1)

    def test_single_page_has_no_pagination_controls(self):
        for index in range(12):
            self.make_item(f'单页商品 {index:02d}')

        response = self.client.get(reverse('my_items'))

        self.assertNotContains(response, 'my-items-pagination')
        self.assertContains(response, '第 1–12 件 · 共 12 件')

    def test_pagination_links_preserve_the_active_status_tab(self):
        for index in range(13):
            self.make_item(f'分页预订商品 {index:02d}', status='reserved')

        first = self.client.get(reverse('my_items'), {'tab': 'reserved'})
        second = self.client.get(reverse('my_items'), {'tab': 'reserved', 'page': 2})

        self.assertContains(first, '?tab=reserved&amp;page=2', html=False)
        self.assertContains(second, '?tab=reserved&amp;page=1', html=False)
        self.assertEqual(second.context['selected_tab'], 'reserved')

    def test_bulk_tools_only_appear_on_all_tab(self):
        for index in range(13):
            self.make_item(f'批量测试商品 {index:02d}', status='reserved')

        all_page = self.client.get(reverse('my_items'), {'tab': 'all'})
        reserved_page = self.client.get(reverse('my_items'), {'tab': 'reserved'})

        self.assertContains(all_page, '批量管理勾选商品')
        self.assertContains(all_page, 'class="form-check-input bulk-item-checkbox"')
        self.assertNotContains(reserved_page, '批量管理勾选商品')
        self.assertNotContains(reserved_page, 'class="form-check-input bulk-item-checkbox"')

    def test_forms_keep_the_current_page_and_reject_external_redirects(self):
        old_time = timezone.now() - timedelta(days=60)
        target = self.make_item('沉底刷新商品', created_at=old_time)
        for index in range(12):
            self.make_item(f'普通分页商品 {index:02d}')

        page_two = self.client.get(reverse('my_items'), {'page': 2})
        expected_next = f"{reverse('my_items')}?tab=all&page=2"

        self.assertContains(
            page_two,
            f'name="next" value="{expected_next.replace("&", "&amp;")}"',
            count=2,
            html=False,
        )

        valid = self.client.post(
            reverse('refresh_item', args=[target.pk]), {'next': expected_next},
        )
        self.assertRedirects(valid, expected_next, fetch_redirect_response=False)

        for view_name, payload in (
            ('bulk_assign_campaign', {'item_ids': [target.pk], 'campaign_id': 'none'}),
            ('bulk_renew_items', {}),
        ):
            payload['next'] = 'https://example.com/not-local'
            response = self.client.post(reverse(view_name), payload)
            self.assertRedirects(
                response,
                reverse('my_items'),
                fetch_redirect_response=False,
            )
