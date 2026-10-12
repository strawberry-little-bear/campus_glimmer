# -*- coding: utf-8 -*-
"""为「等一个有货的人等了多久」钉住口径。

`ItemAvailabilityWatch` 只有 `user`、`item`、`created_at` 三个字段，唯一的消费路径在
发出通知的那一刻就把记录删掉了。这个模块把还在等的关注读出来，只报告不改写。这些测试
要钉住的是：

1. 只统计当前仍挂着的关注，被取消和被消费的是右删失样本，数不出来也不编一个数冒充。
2. 「从点下关注到商品真的回来隔了多久」对已消费的样本数不出来，本模块不报。
3. 死信和白等分开判：商品已恢复在售却还挂着的是死信，商品已售出或过期的是白等。
4. 一件商品上有多少人在等，分母是商品数，不是关注数。
5. 商品状态一律按当前状态回看，「不可交易」这一栏系统性偏高，报告里写清楚。
6. 已经送达的提醒只补「送达时商品还可不可交易」，不重复 notification_response。
7. 不读任何自由文本，不自动清理任何一条关注，不补发漏掉的通知。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .availability_watch_quality import (
    DEAD_LETTER_LIST_LIMIT, LONG_WAIT_LIST_LIMIT, MIN_MEASURED_WATCHES,
    build_availability_watch_quality,
)
from .models import Category, Item, ItemAvailabilityWatch, Notification


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class AvailabilityWatchQualityTests(TestCase):
    def setUp(self):
        self.seller = make_user('watch-seller')
        self.category = Category.objects.create(name='关注质量测试分类')

    def make_item(self, *, status='sold', title='等在 sold 的商品'):
        return Item.objects.create(
            title=title, description='用于关注质量统计',
            price='10.00', category=self.category, condition='良好',
            seller=self.seller, status=status,
        )

    def make_watch(self, item=None, *, user=None, days_ago=1):
        """建一条还挂着的关注，年龄按天给。

        `created_at` 是 auto_now_add，不能直接在 create 里给，只能建完再改。
        """
        row = ItemAvailabilityWatch.objects.create(
            user=user or make_user(f'watcher-{ItemAvailabilityWatch.objects.count() + 1}'),
            item=item or self.make_item(),
        )
        row.created_at = timezone.now() - timedelta(days=days_ago)
        row.save(update_fields=['created_at'])
        return row

    def test_only_live_watches_are_measured(self):
        # 一条刚点下的关注在窗口内；一条被取消（delete）的关注不该进来，也不该有
        # 任何一栏替它记数——被取消的是右删失样本，数不出来。
        self.make_watch(days_ago=1)
        consumed = self.make_watch(days_ago=2)
        consumed.delete()

        report = build_availability_watch_quality(days=30)
        self.assertEqual(report['stats']['measured_count'], 1)

    def test_consumed_watches_are_not_measured_anywhere(self):
        # 消费即删除：已经被通知到的那批关注不在表里，所以「从点下关注到商品真的
        # 回来隔了多久」这个分布对它们数不出来。报告里不能有一个栏位冒充它。
        report = build_availability_watch_quality(days=30)
        self.assertFalse(report['has_data'])
        self.assertEqual(report['stats']['median_label'], '—')
        self.assertNotIn('consumed', report)
        self.assertNotIn('wait_to_delivery', report)

    def test_wait_buckets_are_mutually_exclusive(self):
        # 五条关注的年龄横跨四个档，created_at 都要落在 30 天窗口内，否则统计不到。
        for days_ago in (0, 2, 5, 10, 20):
            self.make_watch(days_ago=days_ago)

        report = build_availability_watch_quality(days=30)
        buckets = {row['label']: row['count'] for row in report['stats']['rows']}
        self.assertEqual(buckets['1 天内'], 1)
        self.assertEqual(buckets['1-3 天'], 1)
        self.assertEqual(buckets['3-7 天'], 1)
        self.assertEqual(buckets['7-14 天'], 1)
        self.assertEqual(buckets['超过 14 天'], 1)
        self.assertEqual(
            sum(row['count'] for row in report['stats']['rows']),
            report['stats']['measured_count'],
        )

    def test_dead_letter_and_long_wait_are_judged_apart(self):
        # 商品已经恢复在售、关注却还挂着：死信。商品已售出、关注还挂着：白等。
        # 两条判据不同，不能混成一栏。
        back = self.make_item(status='available', title='回来了的商品')
        self.make_watch(item=back, days_ago=4)
        gone = self.make_item(status='sold', title='卖掉的商品')
        self.make_watch(item=gone, days_ago=9)

        report = build_availability_watch_quality(days=30)
        self.assertEqual(report['stats']['dead_letter_count'], 1)
        self.assertEqual(report['dead_letter_row_total'], 1)
        self.assertEqual(report['dead_letter_rows'][0]['item_title'], '回来了的商品')
        # 白等只收「商品不可交易」的那些，死信商品是在售的，不进这一栏。
        self.assertEqual(report['long_wait_row_total'], 1)
        self.assertEqual(report['long_wait_rows'][0]['item_title'], '卖掉的商品')

    def test_expired_items_are_not_dead_letters(self):
        # 过期商品不在「已恢复在售」里，等它的人是白等，不是死信——卖家随时可以
        # 重新上架，平台没有能力判定这个人被漏掉了。
        item = self.make_item(status='available', title='过期商品')
        item.expires_at = timezone.now() - timedelta(days=1)
        item.save(update_fields=['expires_at', 'updated_at'])
        self.make_watch(item=item, days_ago=6)

        report = build_availability_watch_quality(days=30)
        self.assertEqual(report['stats']['dead_letter_count'], 0)
        self.assertEqual(report['long_wait_row_total'], 1)

    def test_item_watch_buckets_count_items_not_watches(self):
        # 一件商品被八个人关注，按关注数走会在分布里出现八次。分母必须是商品数。
        crowded = self.make_item(title='八人在等的商品')
        for index in range(8):
            self.make_watch(item=crowded, days_ago=3)
        # 另两件商品各一个人等。
        for index in range(2):
            self.make_watch(days_ago=3)

        report = build_availability_watch_quality(days=30)
        buckets = {row['label']: row['count'] for row in report['item_watch_rows']}
        self.assertEqual(buckets['1 人在等'], 2)
        self.assertEqual(buckets['8 人及以上'], 1)
        # 分母是三件商品，不是十条关注。
        self.assertEqual(sum(row['count'] for row in report['item_watch_rows']), 3)

    def test_item_status_is_read_back_from_the_current_value(self):
        # 商品状态只记当前值。一条关注点下时商品在售、之后才被卖掉，也算「商品已经
        # 不可交易」——这一栏系统性偏高，宁可高估。
        item = self.make_item(status='sold', title='先售后卖的')
        self.make_watch(item=item, days_ago=2)

        report = build_availability_watch_quality(days=30)
        self.assertEqual(report['stats']['unavailable_count'], 1)
        self.assertEqual(report['stats']['unavailable_rate'], 100)

    def test_trade_modes_are_reported_apart(self):
        # 出售会重新上架，免费赠送等不到就是真的没有了。两种处境不能稀释成一个数。
        sale = self.make_item(title='出售中的商品')
        free = self.make_item(title='免费赠送的商品')
        free.trade_mode = 'free'
        free.save(update_fields=['trade_mode', 'updated_at'])
        self.make_watch(item=sale, days_ago=2)
        self.make_watch(item=free, days_ago=2)

        report = build_availability_watch_quality(days=30)
        by_mode = {row['trade_mode']: row for row in report['trade_mode_rows']}
        self.assertEqual(by_mode['sale']['count'], 1)
        self.assertEqual(by_mode['free']['count'], 1)
        self.assertEqual(by_mode['free']['trade_mode_label'], '免费赠送')

    def test_delivered_reminders_only_report_item_status(self):
        # 已经送达的提醒只补「送达时商品还可不可交易」，不重复 notification_response
        # 已经报过的「发了多久被读」。
        item = self.make_item(status='sold', title='提醒过的商品')
        Notification.objects.create(
            recipient=make_user('reminded'), kind='item_available', item=item,
            title='关注的商品重新有货了', message='x',
        )

        report = build_availability_watch_quality(days=30)
        self.assertEqual(report['delivery']['measured_count'], 1)
        self.assertEqual(report['delivery']['stale_count'], 1)
        self.assertEqual(report['delivery']['read_count'], 0)

    def test_read_reminders_are_counted_separately(self):
        item = self.make_item(status='available', title='已读的提醒')
        row = Notification.objects.create(
            recipient=make_user('reminded-read'), kind='item_available', item=item,
            title='关注的商品重新有货了', message='x',
        )
        row.is_read = True
        row.read_at = timezone.now()
        row.save(update_fields=['is_read', 'read_at'])

        report = build_availability_watch_quality(days=30)
        self.assertEqual(report['delivery']['read_count'], 1)
        self.assertEqual(report['delivery']['read_rate'], 100)
        self.assertEqual(report['delivery']['stale_count'], 0)

    def test_window_follows_created_at_of_the_watch(self):
        # 一条 45 天前点下、现在还挂着的关注，created_at 落在 30 天窗口外，不该进来。
        self.make_watch(days_ago=45)
        self.make_watch(days_ago=2)

        report = build_availability_watch_quality(days=30)
        self.assertEqual(report['stats']['measured_count'], 1)

    def test_empty_report_is_explicit(self):
        report = build_availability_watch_quality(days=30)
        self.assertFalse(report['has_data'])
        self.assertEqual(report['stats']['measured_count'], 0)
        self.assertEqual(report['stats']['p90_label'], '—')
        self.assertEqual(report['dead_letter_row_total'], 0)
        self.assertEqual(report['long_wait_row_total'], 0)
        self.assertEqual(report['delivery']['measured_count'], 0)
        self.assertTrue(report['stats']['is_small_sample'])

    def test_small_sample_is_flagged(self):
        for index in range(MIN_MEASURED_WATCHES - 1):
            self.make_watch(days_ago=1)
        report = build_availability_watch_quality(days=30)
        self.assertTrue(report['stats']['is_small_sample'])
        self.assertEqual(
            report['min_measured_watches'], MIN_MEASURED_WATCHES,
        )
        self.assertEqual(report['min_item_watches'], 3)

    def test_row_limits_are_reported(self):
        for index in range(DEAD_LETTER_LIST_LIMIT + 5):
            item = self.make_item(
                status='available', title=f'死信商品 {index}',
            )
            self.make_watch(item=item, days_ago=1)

        report = build_availability_watch_quality(days=30)
        self.assertEqual(len(report['dead_letter_rows']), DEAD_LETTER_LIST_LIMIT)
        self.assertGreater(
            report['dead_letter_row_total'], DEAD_LETTER_LIST_LIMIT,
        )

    def test_long_wait_list_limit_is_reported(self):
        for index in range(LONG_WAIT_LIST_LIMIT + 3):
            self.make_watch(days_ago=1)

        report = build_availability_watch_quality(days=30)
        self.assertEqual(len(report['long_wait_rows']), LONG_WAIT_LIST_LIMIT)
        self.assertGreater(
            report['long_wait_row_total'], LONG_WAIT_LIST_LIMIT,
        )

    def test_period_choices_are_the_only_accepted_windows(self):
        for days in (7, 30, 90, 365):
            report = build_availability_watch_quality(days=days)
            self.assertEqual(report['period_days'], days)
        # 不认识的窗口退回 30 天，而不是当 0 天用。
        report = build_availability_watch_quality(days=3)
        self.assertEqual(report['period_days'], 30)

    def test_the_module_never_rewrites_a_watch(self):
        # 只报告不改写：跑完之后关注、商品、通知的数量一条都不能少。
        item = self.make_item(status='available', title='不改写的商品')
        self.make_watch(item=item, days_ago=3)

        build_availability_watch_quality(days=30)

        self.assertEqual(ItemAvailabilityWatch.objects.count(), 1)
        item.refresh_from_db()
        self.assertEqual(item.status, 'available')
        self.assertFalse(Notification.objects.exists())
