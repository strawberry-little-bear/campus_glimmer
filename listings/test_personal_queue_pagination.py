from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import BrowsingHistory, Category, GiftApplication, Item


class PersonalQueuePaginationTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username='personal-queue-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='personal-queue-buyer', password='safe-password-123',
        )
        self.category = Category.objects.create(name='个人队列分页分类')

    def make_item(self, title, *, seller=None, trade_mode='sale'):
        return Item.objects.create(
            title=title,
            description='个人队列服务端分页测试商品',
            trade_mode=trade_mode,
            price=Decimal('0.00') if trade_mode == 'free' else Decimal('12.00'),
            category=self.category,
            condition='9成新',
            seller=seller or self.seller,
        )

    def make_application(self, item, applicant, *, status='pending', created_at=None):
        application = GiftApplication.objects.create(
            item=item,
            applicant=applicant,
            status=status,
            applicant_note=f'{applicant.username} 的领取说明',
        )
        if created_at is not None:
            GiftApplication.objects.filter(pk=application.pk).update(created_at=created_at)
            application.refresh_from_db()
        return application

    def test_browsing_history_uses_24_item_pages_and_clamps_invalid_pages(self):
        base = timezone.now() - timedelta(days=2)
        records = []
        for index in range(25):
            item = self.make_item(f'浏览足迹商品 {index:02d}')
            record = BrowsingHistory.objects.create(user=self.buyer, item=item)
            BrowsingHistory.objects.filter(pk=record.pk).update(
                last_viewed_at=base + timedelta(minutes=index),
            )
            records.append(record)
        self.client.force_login(self.buyer)

        first = self.client.get(reverse('browsing_history'))
        second = self.client.get(reverse('browsing_history'), {'page': 2})
        too_high = self.client.get(reverse('browsing_history'), {'page': 99})
        invalid = self.client.get(reverse('browsing_history'), {'page': 'not-a-number'})

        first_items = [record.item_id for record in first.context['history']]
        second_items = [record.item_id for record in second.context['history']]
        self.assertEqual(first.context['page_obj'].paginator.count, 25)
        self.assertEqual(len(first_items), 24)
        self.assertEqual(second_items, [records[0].item_id])
        self.assertEqual(too_high.context['page_obj'].number, 2)
        self.assertEqual(invalid.context['page_obj'].number, 1)
        self.assertContains(first, '第 1–24 件 · 共 25 件浏览记录')
        self.assertContains(second, '第 25–25 件 · 共 25 件浏览记录')
        self.assertContains(first, 'aria-label="最近浏览分页"')

    def test_my_applications_are_paginated_and_selected_ranks_before_rejected(self):
        base = timezone.now() - timedelta(days=1)
        applications = []
        for index in range(10):
            applications.append(self.make_application(
                self.make_item(f'待处理领取 {index:02d}', trade_mode='free'),
                self.buyer,
                created_at=base + timedelta(minutes=index),
            ))
        selected = self.make_application(
            self.make_item('已选中领取', trade_mode='free'), self.buyer,
            status='selected', created_at=base + timedelta(minutes=20),
        )
        rejected = self.make_application(
            self.make_item('未选中领取', trade_mode='free'), self.buyer,
            status='rejected', created_at=base + timedelta(minutes=21),
        )
        withdrawn = self.make_application(
            self.make_item('已撤回领取', trade_mode='free'), self.buyer,
            status='withdrawn', created_at=base + timedelta(minutes=22),
        )
        self.client.force_login(self.buyer)

        first = self.client.get(reverse('my_gift_applications'))
        second = self.client.get(reverse('my_gift_applications'), {'page': 2})

        first_statuses = [application.status for application in first.context['applications']]
        second_statuses = [application.status for application in second.context['applications']]
        self.assertEqual(len(first_statuses), 12)
        self.assertEqual(first_statuses, ['pending'] * 10 + ['selected', 'rejected'])
        self.assertEqual(second_statuses, ['withdrawn'])
        self.assertIn(selected.pk, [application.pk for application in first.context['applications']])
        self.assertIn(rejected.pk, [application.pk for application in first.context['applications']])
        self.assertEqual(second.context['applications'][0].pk, withdrawn.pk)
        self.assertEqual(first.context['page_obj'].paginator.count, 13)
        self.assertContains(first, '第 1–12 条 · 共 13 条领取申请')
        self.assertContains(second, '第 13–13 条 · 共 13 条领取申请')

    def test_manage_queue_pages_keep_workspace_wide_pending_count(self):
        item = self.make_item('多人竞争免费台灯', trade_mode='free')
        base = timezone.now() - timedelta(days=1)
        applications = []
        for index in range(13):
            applicant = User.objects.create_user(username=f'gift-applicant-{index:02d}')
            applications.append(self.make_application(
                item, applicant, created_at=base + timedelta(minutes=index),
            ))
        self.client.force_login(self.seller)

        first = self.client.get(reverse('manage_gift_applications', args=[item.pk]))
        second = self.client.get(
            reverse('manage_gift_applications', args=[item.pk]), {'page': 2},
        )

        self.assertEqual(first.context['pending_count'], 13)
        self.assertEqual(second.context['pending_count'], 13)
        self.assertEqual(len(first.context['applications']), 12)
        self.assertEqual(len(second.context['applications']), 1)
        self.assertEqual(second.context['applications'][0].pk, applications[-1].pk)
        self.assertContains(second, '13 待处理')
        self.assertContains(second, '第 13–13 条 · 共 13 条申请')

    def test_non_seller_cannot_read_the_application_queue(self):
        item = self.make_item('只属于发布者的免费商品', trade_mode='free')
        self.client.force_login(self.buyer)

        response = self.client.get(reverse('manage_gift_applications', args=[item.pk]))

        self.assertRedirects(response, reverse('item_detail', args=[item.pk]))

    def test_withdraw_returns_to_the_safe_queue_page(self):
        base = timezone.now() - timedelta(days=1)
        applications = []
        for index in range(13):
            applications.append(self.make_application(
                self.make_item(f'待撤回领取 {index:02d}', trade_mode='free'),
                self.buyer,
                created_at=base + timedelta(minutes=index),
            ))
        self.client.force_login(self.buyer)
        page_two_url = f"{reverse('my_gift_applications')}?page=2"

        response = self.client.post(
            reverse('withdraw_gift_application', args=[applications[0].pk]),
            {'next': page_two_url},
        )

        self.assertRedirects(response, page_two_url)
        applications[0].refresh_from_db()
        self.assertEqual(applications[0].status, 'withdrawn')
        self.assertContains(self.client.get(page_two_url), 'aria-label="我的领取申请分页"')

    def test_withdraw_rejects_external_next_url(self):
        application = self.make_application(
            self.make_item('防开放跳转免费商品', trade_mode='free'), self.buyer,
        )
        self.client.force_login(self.buyer)

        response = self.client.post(
            reverse('withdraw_gift_application', args=[application.pk]),
            {'next': 'https://example.com/phishing'},
        )

        self.assertRedirects(response, reverse('my_gift_applications'))
        application.refresh_from_db()
        self.assertEqual(application.status, 'withdrawn')
