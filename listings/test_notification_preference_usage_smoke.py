# -*- coding: utf-8 -*-
"""冒烟：确认通知偏好开关面板与 CSV 都能渲染/导出，不报 KeyError。

这一节的关键不在「能打开」，而在两个只在渲染期才会炸的形状：

1. 模板里用 `{{ notification_preference_usage.rows|length }}` 报开关个数，空平台时
   `rows` 是空列表，`|length` 给 0——所以摘要里那句「其余 N 个」会变成负数，
   页面照样渲染，只是文案变成一句错话；
2. `by_group` 空列表走 `{% empty %}` 分支，`share` 为 None 走 `&mdash;` 分支。
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Category, CampusLocation, Item, Notification, NotificationPreference


class NotificationPreferenceUsageSmokeTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='pref-smoke-admin', password='safe-password-123', email='a@b.c',
        )
        self.category = Category.objects.create(name='冒烟分类')
        self.location = CampusLocation.objects.create(name='冒烟地点', building='楼', is_public=True)

    def _make_items(self, count):
        seller = User.objects.create_user(username='pref-smoke-seller')
        return [
            Item.objects.create(
                title=f'冒烟商品 {index}', description='d', price='20.00',
                category=self.category, location=self.location, condition='良好',
                seller=seller, status='available',
            )
            for index in range(count)
        ]

    def test_dashboard_and_export_render_with_preference_rows(self):
        self._make_items(1)
        # 造出足够的偏好行，让分组占比和逐开关两节都有东西可报。
        for index in range(20):
            user = User.objects.create_user(username=f'pref-{index}')
            NotificationPreference.objects.create(
                user=user,
                order_created=(index % 4 != 0),
                message_received=(index % 5 != 0),
                operations_digest=(index % 2 == 0),
            )
            if index % 3 == 0:
                Notification.objects.create(
                    recipient=user, kind='order_created', title='t', message='m',
                )

        self.client.force_login(self.admin)
        response = self.client.get(reverse('operations_dashboard'))
        self.assertEqual(response.status_code, 200)
        usage = response.context['notification_preference_usage']
        self.assertTrue(usage['has_data'])
        self.assertEqual(len(usage['rows']), 25)
        self.assertEqual(len(usage['by_group']), 4)
        order_row = next(r for r in usage['rows'] if r['field'] == 'order_created')
        self.assertEqual(order_row['off'], 5)
        self.assertEqual(order_row['on'], 15)

        export = self.client.get(reverse('operations_dashboard_export'))
        self.assertEqual(export.status_code, 200)
        text = export.content.decode('utf-8-sig')
        self.assertIn('通知偏好开关：25 个开关各自的形状', text)
        self.assertIn('按「关掉它有没有交易后果」分组', text)
        self.assertIn('25 个开关逐个看', text)
        self.assertIn('关着却仍收到过', text)

    def test_dashboard_and_export_render_on_an_empty_campus(self):
        # 空平台：rows 是空列表，模板的 |length 给 0，两节都要走 % empty 分支。
        self.client.force_login(self.admin)
        response = self.client.get(reverse('operations_dashboard'))
        self.assertEqual(response.status_code, 200)
        usage = response.context['notification_preference_usage']
        self.assertFalse(usage['has_data'])
        self.assertEqual(usage['rows'], [])

        export = self.client.get(reverse('operations_dashboard_export'))
        self.assertEqual(export.status_code, 200)
        text = export.content.decode('utf-8-sig')
        self.assertIn('当前平台还没有任何偏好行', text)

    def test_dashboard_and_export_render_with_notifications_but_no_preference_rows(self):
        # 收到过通知却一条偏好行都没有：覆盖「没有偏好行的用户」那一格，
        # 以及 rows 空、coverage 非空这个最容易被漏掉的组合。
        buyer = User.objects.create_user(username='pref-smoke-buyer')
        Notification.objects.create(recipient=buyer, kind='order_created', title='t', message='m')

        self.client.force_login(self.admin)
        response = self.client.get(reverse('operations_dashboard'))
        self.assertEqual(response.status_code, 200)
        usage = response.context['notification_preference_usage']
        self.assertFalse(usage['has_data'])
        self.assertEqual(usage['coverage']['users_without_row'], 1)

        export = self.client.get(reverse('operations_dashboard_export'))
        self.assertEqual(export.status_code, 200)
        text = export.content.decode('utf-8-sig')
        self.assertIn('收到过通知却没有偏好行', text)