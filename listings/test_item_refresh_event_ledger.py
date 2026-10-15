# -*- coding: utf-8 -*-
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .item_refresh_event_ledger import (
    DEFAULT_PERIOD_DAYS, build_item_refresh_event_ledger,
)
from .models import Category, Item, ItemRefreshEvent


class RefreshEventWriteTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username='ledger-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='刷新台账分类')
        self.item = Item.objects.create(
            title='台账商品', description='逐次刷新', price='30.00',
            category=self.category, condition='9成新', seller=self.seller,
        )
        self.url = reverse('refresh_item', args=[self.item.pk])

    def test_accepted_refresh_writes_one_ledger_event_in_the_same_transaction(self):
        self.client.force_login(self.seller)

        response = self.client.post(self.url)

        self.assertRedirects(response, reverse('my_items'))
        self.item.refresh_from_db()
        event = ItemRefreshEvent.objects.get()
        self.assertEqual(event.item, self.item)
        self.assertEqual(event.seller, self.seller)
        self.assertEqual(event.refreshed_at, self.item.last_refreshed_at)
        self.assertEqual(event.refresh_count_after, 1)

    def test_get_and_rejected_refresh_do_not_create_events(self):
        self.client.force_login(self.seller)

        self.client.get(self.url)
        self.client.post(self.url)
        self.client.post(self.url)

        self.assertEqual(ItemRefreshEvent.objects.count(), 1)
        self.item.refresh_from_db()
        self.assertEqual(self.item.refresh_count, 1)

    def test_ledger_failure_rolls_back_the_refresh_counter(self):
        self.client.force_login(self.seller)

        with mock.patch.object(
            ItemRefreshEvent.objects, 'create', side_effect=RuntimeError,
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(self.url)

        self.item.refresh_from_db()
        self.assertEqual(self.item.refresh_count, 0)
        self.assertIsNone(self.item.last_refreshed_at)
        self.assertEqual(ItemRefreshEvent.objects.count(), 0)

    def test_the_same_item_and_ordinal_cannot_be_written_twice(self):
        event = ItemRefreshEvent.objects.create(
            item=self.item, seller=self.seller,
            refreshed_at=timezone.now(), refresh_count_after=1,
        )

        with self.assertRaises(Exception):
            ItemRefreshEvent.objects.create(
                item=event.item, seller=event.seller,
                refreshed_at=timezone.now() + timedelta(minutes=5),
                refresh_count_after=1,
            )


class RefreshEventLedgerPanelTests(TestCase):
    def setUp(self):
        self.now = timezone.make_aware(timezone.datetime(2026, 10, 15, 12, 0))
        self.seller = User.objects.create_user(username='ledger-owner')
        self.other_seller = User.objects.create_user(username='ledger-other')
        self.category = Category.objects.create(name='台账统计分类')
        self.full_item = self.make_item('完整覆盖', self.seller, refresh_count=3)
        self.legacy_item = self.make_item('历史缺口', self.seller, refresh_count=5)
        self.mismatch_item = self.make_item('计数不一致', self.other_seller, refresh_count=3)
        self.historical_item = self.make_item('只有字段', self.other_seller, refresh_count=2)
        self.make_event(self.full_item, 1, self.now - timedelta(days=3))
        self.make_event(self.full_item, 2, self.now - timedelta(days=2))
        self.make_event(self.full_item, 3, self.now - timedelta(days=1))
        self.make_event(self.legacy_item, 3, self.now - timedelta(days=2))
        self.make_event(self.legacy_item, 4, self.now - timedelta(days=2, hours=1))
        self.make_event(self.legacy_item, 5, self.now - timedelta(days=1))
        self.make_event(self.mismatch_item, 1, self.now - timedelta(days=1))
        self.make_event(self.mismatch_item, 2, self.now - timedelta(hours=6))

    def make_item(self, title, seller, *, refresh_count):
        return Item.objects.create(
            title=title, description='台账测试', price='20.00',
            category=self.category, condition='9成新', seller=seller,
            refresh_count=refresh_count,
            last_refreshed_at=self.now - timedelta(days=1),
        )

    def make_event(self, item, count_after, refreshed_at):
        return ItemRefreshEvent.objects.create(
            item=item, seller=item.seller,
            refreshed_at=refreshed_at, refresh_count_after=count_after,
        )

    def test_period_and_daily_shape_are_read_from_events(self):
        report = build_item_refresh_event_ledger(days=7, now=self.now)

        self.assertEqual(report['event_count'], 8)
        self.assertEqual(report['item_count'], 3)
        self.assertEqual(report['seller_count'], 2)
        self.assertEqual(report['daily_row_count'], 7)
        self.assertEqual(
            [row['event_count'] for row in report['daily_rows']],
            [0, 0, 0, 1, 3, 3, 1],
        )
        self.assertEqual(report['coverage']['full_count'], 1)
        self.assertEqual(report['coverage']['legacy_gap_count'], 1)
        self.assertEqual(report['coverage']['historical_only_count'], 1)
        self.assertEqual(report['coverage']['inconsistent_count'], 1)
        self.assertEqual(report['coverage']['legacy_missing_total'], 4)
        self.assertIn('需要先核对台账', report['summary'])

    def test_future_events_are_separate_from_current_period(self):
        self.make_event(self.full_item, 4, self.now + timedelta(seconds=30))
        self.make_event(self.full_item, 5, self.now + timedelta(minutes=2))

        report = build_item_refresh_event_ledger(days=7, now=self.now)

        self.assertEqual(report['event_count'], 9)
        self.assertEqual(report['future_event_count'], 1)

    def test_a_clock_tolerance_event_stays_in_the_daily_shape(self):
        edge_now = timezone.make_aware(timezone.datetime(2026, 10, 15, 23, 59, 50))
        self.make_event(self.full_item, 4, edge_now + timedelta(seconds=30))

        report = build_item_refresh_event_ledger(days=7, now=edge_now)

        self.assertEqual(report['event_count'], 9)
        self.assertEqual(report['future_event_count'], 0)
        self.assertEqual(report['daily_row_count'], 8)
        self.assertEqual(report['daily_rows'][-1]['event_count'], 1)
        self.assertEqual(
            sum(row['event_count'] for row in report['daily_rows']),
            report['event_count'],
        )

    def test_unknown_period_falls_back_to_the_default_window(self):
        report = build_item_refresh_event_ledger(days=11, now=self.now)

        self.assertEqual(report['days'], DEFAULT_PERIOD_DAYS)
        self.assertEqual(report['daily_row_count'], DEFAULT_PERIOD_DAYS)

    def test_dashboard_and_export_expose_the_same_ledger_contract(self):
        staff = User.objects.create_user(
            username='ledger-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(staff)

        page = self.client.get(reverse('operations_dashboard'), {'days': 7})
        export = self.client.get(reverse('operations_dashboard_export'), {'days': 7})

        self.assertEqual(page.status_code, 200)
        self.assertEqual(export.status_code, 200)
        self.assertContains(page, '商品刷新事件台账')
        self.assertContains(page, '台账上线前的历史缺口照实单列')
        self.assertContains(page, '事件序号断档')
        csv_text = export.content.decode('utf-8-sig')
        self.assertIn('商品刷新事件台账', csv_text)
        self.assertIn('周期内事件', csv_text)
        self.assertIn('完整事件覆盖', csv_text)
        self.assertIn('序列不一致', csv_text)
        self.assertIn('不按最新时间戳伪造事件', csv_text)
