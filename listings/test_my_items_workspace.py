from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Category, Item


class MyItemsWorkspaceTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='workspace-seller', password='safe-password-123')
        self.other = User.objects.create_user(username='workspace-other', password='safe-password-123')
        self.category = Category.objects.create(name='工作台分类')

    def make_item(self, title, *, seller=None, status='available', trade_mode='sale', **kwargs):
        return Item.objects.create(
            title=title,
            description='卖家工作台测试商品',
            price='18.00',
            category=self.category,
            condition='9成新',
            seller=seller or self.seller,
            status=status,
            trade_mode=trade_mode,
            **kwargs,
        )

    def test_status_tabs_are_server_rendered_and_keep_full_workspace_counts(self):
        items = {
            'available': self.make_item('在售教材'),
            'reserved': self.make_item('预订台灯', status='reserved'),
            'sold': self.make_item('售出单车', status='sold'),
            'expired': self.make_item('过期收纳箱', status='expired'),
        }
        self.client.force_login(self.seller)

        response = self.client.get(reverse('my_items'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context['item_counts'],
            {'all': 4, 'available': 1, 'reserved': 1, 'sold': 1, 'expired': 1},
        )
        self.assertEqual(response.context['selected_tab'], 'all')
        self.assertEqual({item.status for item in response.context['items']}, set(items))
        for status, item in items.items():
            tab_response = self.client.get(reverse('my_items'), {'tab': status})
            self.assertEqual(tab_response.context['selected_tab'], status)
            self.assertEqual([row.pk for row in tab_response.context['items']], [item.pk])
            self.assertEqual(tab_response.context['item_counts']['all'], 4)
            self.assertContains(tab_response, f'id="{status}"', html=False)
            for other_status, other_item in items.items():
                if other_status != status:
                    self.assertNotContains(tab_response, other_item.title)

        self.assertContains(response, 'id="all-tab"', html=False)
        self.assertContains(response, 'id="available-tab"', html=False)
        self.assertContains(response, 'id="reserved-tab"', html=False)
        self.assertContains(response, 'id="sold-tab"', html=False)
        self.assertContains(response, 'id="expired-tab"', html=False)

    def test_workspace_only_renders_current_sellers_items(self):
        self.make_item('我的教材')
        self.make_item('别人的教材', seller=self.other)
        self.client.force_login(self.seller)

        response = self.client.get(reverse('my_items'))

        self.assertContains(response, '我的教材')
        self.assertNotContains(response, '别人的教材')
        self.assertNotContains(response, 'style="width: 50px; height: 50px;')

    def test_trade_modes_keep_free_and_borrow_specific_information(self):
        self.make_item('免费赠送的衣架', trade_mode='free')
        self.make_item(
            '借用投影仪',
            trade_mode='borrow',
            deposit_amount='120.00',
            borrow_days=5,
        )
        self.client.force_login(self.seller)

        response = self.client.get(reverse('my_items'))

        self.assertContains(response, '免费赠送')
        self.assertContains(response, '押金 ¥120.00')
        self.assertContains(response, '5 天借用')

    def test_empty_workspace_keeps_tabs_and_publish_entry_visible(self):
        self.client.force_login(self.seller)

        response = self.client.get(reverse('my_items'))

        self.assertEqual(
            response.context['item_counts'],
            {'all': 0, 'available': 0, 'reserved': 0, 'sold': 0, 'expired': 0},
        )
        self.assertContains(response, 'id="expired-tab"', html=False)
        self.assertContains(response, '您还没有发布任何商品')
        self.assertContains(response, '立即发布')
