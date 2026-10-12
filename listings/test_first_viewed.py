# -*- coding: utf-8 -*-
"""从第一次心动到下一步，中间隔了多久。

``first_viewed_at`` 一直在写，而运营侧一条统计都没有。这个模块把这件事补上，
这些测试要钉住的不是四舍五入，而是六件更根本的事。

第一件是窗口的分母。只有「至少回访过一次」的浏览记录才谈得上考虑窗口；一条
只看过一次的记录，``first_viewed_at`` 和 ``last_viewed_at`` 是同一次写入的产物，
差是零，而零和「当天就走了」不是一回事。把前者算成后者，会让「当天就回访」
的比例虚高。

第二件是零和「当天」必须分开。零说明压根没有第二次访问，「当天」说明第二次
访问真的发生了、只是发生在同一天。

第三件是下一步动作的先后。只认 ``first_viewed_at`` 之后的收藏和下单：一件先
被收藏、后来才第一次被浏览的商品，收藏不是浏览的结果，不能算成心动的下一步。

第四件是先下单后浏览必须单独一列。它的考虑窗口是负的，算进「没有任何下一步」
会让这批本已成交的样本看起来像失败的样本。

第五件是统计窗口按 ``first_viewed_at`` 取，不按 ``last_viewed_at``。窗口要回答
「这段时间里新产生的心动考虑了多久」，起点必须是第一次心动。

第六件是「想了很久却没有下一步」这条清单的口径：窗口拖过一周、且至今没有
任何下一步。一周是「正常节奏」的上界，比这更短的样本不该进清单。
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .first_viewed import (
    MIN_MEASURED_HISTORIES,
    WINDOW_BUCKETS,
    build_first_viewed,
)
from .models import BrowsingHistory, Category, CampusLocation, Favorite, Item, Order


def local_noon():
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class FirstViewedSetupTests(TestCase):
    """夹具、空态和统计窗口。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('fv-seller')
        self.buyer = make_user('fv-buyer')
        self.category = Category.objects.create(name='心动分类')
        self.location = CampusLocation.objects.create(name='心动地点')

    def make_item(self, title='心动商品', status='available'):
        return Item.objects.create(
            title=title, description='用于考虑窗口统计', price=Decimal('30.00'),
            status=status, trade_mode='sale', seller=self.seller,
            category=self.category, location=self.location,
        )

    def make_history(self, *, item=None, user=None, view_count=1,
                     first_days_ago=0, last_days_ago=None, last_offset_days=None):
        """一条浏览记录。同一（用户，商品）只能有一条，所以每件商品配一个用户。

        ``first_viewed_at`` 是 ``auto_now_add``，测试里要用 ``update()`` 改；
        ``last_viewed_at`` 是 ``auto_now``，同样只能 ``update()``。
        """
        item = item or self.make_item()
        user = user or make_user(f'fv-viewer-{item.id}')
        history = BrowsingHistory.objects.create(
            user=user, item=item, view_count=view_count,
        )
        first_at = self.now - timedelta(days=first_days_ago)
        BrowsingHistory.objects.filter(pk=history.pk).update(
            first_viewed_at=first_at,
            last_viewed_at=(
                first_at + timedelta(days=last_offset_days)
                if last_offset_days is not None
                else self.now - timedelta(days=last_days_ago or 0)
            ),
        )
        history.refresh_from_db()
        return history

    def make_favorite(self, history, days_after_first=1):
        favorite = Favorite.objects.create(user=history.user, item=history.item)
        Favorite.objects.filter(pk=favorite.pk).update(
            created_at=history.first_viewed_at + timedelta(days=days_after_first),
        )
        favorite.refresh_from_db()
        return favorite

    def make_order(self, history, days_after_first=2, status='completed'):
        """一笔订单。一件商品同时只能有一笔进行中的订单，已完成的不占位。"""
        order = Order.objects.create(
            item=history.item, buyer=history.user, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status=status,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=history.first_viewed_at + timedelta(days=days_after_first),
        )
        order.refresh_from_db()
        return order

    def test_an_empty_period_reports_no_data_rather_than_zeroes(self):
        report = build_first_viewed(days=30, now=self.now)

        self.assertFalse(report['has_data'])
        self.assertEqual(report['window_stats']['measured_count'], 0)
        self.assertIn('还没有新的商品浏览记录', report['summary'])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        report = build_first_viewed(days=999, now=self.now)

        self.assertEqual(report['period_days'], 30)

    def test_a_history_first_viewed_before_the_window_is_excluded(self):
        self.make_history(view_count=5, first_days_ago=45, last_offset_days=3)

        report = build_first_viewed(days=30, now=self.now)

        self.assertFalse(report['has_data'])

    def test_the_window_is_taken_from_first_viewed_not_last_viewed(self):
        """40 天前第一次心动、昨天还在回访的记录，属于 40 天前那一批。

        按 ``last_viewed_at`` 取窗口会把这条横跨多个周期的长考虑塞进最后一个
        周期，而那个周期的其他记录都是当天心动的，混在一起两个数都不能用。
        """
        self.make_history(view_count=5, first_days_ago=45, last_days_ago=1)

        report = build_first_viewed(days=30, now=self.now)

        self.assertFalse(report['has_data'])
        self.assertEqual(report['history_total'], 0)


class RevisitWindowTests(TestCase):
    """考虑窗口：只有回访过的记录才有窗口，零和「当天」必须分开。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('rw-seller')
        self.category = Category.objects.create(name='窗口分类')
        self.location = CampusLocation.objects.create(name='窗口地点')

    def make_history(self, view_count=2, first_days_ago=5, offset_days=3):
        item = Item.objects.create(
            title=f'窗口商品 {view_count} {first_days_ago} {offset_days}',
            description='x', price=Decimal('1.00'), status='available',
            trade_mode='sale', seller=self.seller,
            category=self.category, location=self.location,
        )
        user = make_user(f'rw-viewer-{item.id}')
        history = BrowsingHistory.objects.create(
            user=user, item=item, view_count=view_count,
        )
        first_at = self.now - timedelta(days=first_days_ago)
        BrowsingHistory.objects.filter(pk=history.pk).update(
            first_viewed_at=first_at,
            last_viewed_at=first_at + timedelta(days=offset_days),
        )
        history.refresh_from_db()
        return history

    def test_a_single_view_has_no_window_at_all(self):
        """只看过一次：两个时间戳是同一次写入的产物，差是零，而零不是「当天」。"""
        self.make_history(view_count=1, offset_days=0)

        report = build_first_viewed(days=30, now=self.now)

        stats = report['window_stats']
        self.assertEqual(stats['measured_count'], 0)
        self.assertEqual(report['single_view_count'], 1)
        self.assertEqual(report['revisit_count'], 0)
        # 它不能被丢掉：它是「看了就走」的规模，是前者的对照组。
        self.assertIn('只看了一次', report['summary'])

    def test_a_single_view_with_a_forged_window_is_not_measured(self):
        """``view_count`` 是 1 而两个时间戳不同——这不该发生，但不许当成零天窗口。

        后台可以改计数器，测试也会改。窗口的分母同时看两个条件：回访过一次，
        且两个时间戳都写上了。只有两个都满足才谈得上窗口。
        """
        self.make_history(view_count=1, offset_days=9)

        report = build_first_viewed(days=30, now=self.now)

        self.assertEqual(report['window_stats']['measured_count'], 0)
        self.assertEqual(report['window_stats']['same_day_count'], 0)
        self.assertEqual(report['revisit_count'], 0)

    def test_a_revisited_history_is_measured_in_days(self):
        self.make_history(view_count=4, first_days_ago=5, offset_days=3)

        report = build_first_viewed(days=30, now=self.now)

        stats = report['window_stats']
        self.assertEqual(stats['measured_count'], 1)
        self.assertEqual(stats['median_label'], '3 天')
        self.assertEqual(stats['p90_label'], '3 天')
        self.assertEqual(stats['fastest_label'], '3 天')
        self.assertEqual(stats['slowest_label'], '3 天')

    def test_the_same_day_revisit_is_a_real_window_of_its_own(self):
        """当天回访是第二次访问真的发生了，不是没有窗口。"""
        self.make_history(view_count=3, first_days_ago=5, offset_days=0)

        report = build_first_viewed(days=30, now=self.now)

        stats = report['window_stats']
        self.assertEqual(stats['measured_count'], 1)
        self.assertEqual(stats['same_day_count'], 1)
        self.assertEqual(stats['bucket_rows'][0]['label'], '当天')
        self.assertEqual(stats['bucket_rows'][0]['count'], 1)
        self.assertEqual(stats['median_label'], '当天')

    def test_buckets_are_left_closed_and_right_open(self):
        """分档左闭右开：正好 3 天落在「3-7 天」档，不在「1-3 天」档。"""
        self.make_history(view_count=2, first_days_ago=5, offset_days=3)

        report = build_first_viewed(days=30, now=self.now)

        rows = report['window_stats']['bucket_rows']
        by_label = {row['label']: row['count'] for row in rows}
        self.assertEqual(by_label['1-3 天'], 0)
        self.assertEqual(by_label['3-7 天'], 1)

    def test_bucket_rows_cover_every_bucket_with_its_own_hint(self):
        report = build_first_viewed(days=30, now=self.now)

        rows = report['window_stats']['bucket_rows']
        self.assertEqual(len(rows), len(WINDOW_BUCKETS))
        for row, (_limit, label, hint) in zip(rows, WINDOW_BUCKETS):
            self.assertEqual(row['label'], label)
            self.assertEqual(row['hint'], hint)

    def test_the_long_tail_is_counted_separately(self):
        """拖过两周的是长尾，最该被看见，所以单独一档而不是被平均掉。"""
        # 心动必须落在窗口内：窗口按 first_viewed_at 取，30 天前那一刻
        # 已经在「最近 30 天」之外了，所以第一次心动放在 25 天前。
        self.make_history(view_count=9, first_days_ago=25, offset_days=21)

        report = build_first_viewed(days=30, now=self.now)

        stats = report['window_stats']
        self.assertEqual(stats['over_two_weeks_count'], 1)
        self.assertEqual(stats['over_week_count'], 1)
        by_label = {row['label']: row['count'] for row in stats['bucket_rows']}
        self.assertEqual(by_label['超过两周'], 1)

    def test_single_view_share_uses_both_piles_as_its_denominator(self):
        """单看一次占比要拿两堆合起来做分母，只看回访过的那一堆会永远是零。"""
        self.make_history(view_count=1, first_days_ago=4, offset_days=0)
        self.make_history(view_count=2, first_days_ago=4, offset_days=2)

        report = build_first_viewed(days=30, now=self.now)

        stats = report['window_stats']
        self.assertEqual(report['single_view_count'], 1)
        self.assertEqual(report['revisit_count'], 1)
        self.assertEqual(stats['single_view_share'], 50.0)
        self.assertIn('占 50.0%', report['summary'])

    def test_a_thin_sample_says_so_instead_of_grading_itself(self):
        self.make_history(view_count=2, first_days_ago=4, offset_days=2)

        report = build_first_viewed(days=30, now=self.now)

        stats = report['window_stats']
        self.assertTrue(stats['is_small_sample'])
        self.assertEqual(stats['min_measured_histories'], MIN_MEASURED_HISTORIES)
        self.assertIn('只作参考', report['summary'])

    def test_a_fat_sample_does_not_hedge(self):
        for index in range(MIN_MEASURED_HISTORIES + 1):
            self.make_history(
                view_count=3, first_days_ago=10, offset_days=2,
            )

        report = build_first_viewed(days=30, now=self.now)

        stats = report['window_stats']
        self.assertFalse(stats['is_small_sample'])
        self.assertNotIn('只作参考', report['summary'])


class NextStepTests(TestCase):
    """反复回访之后，有没有走到下一步——以及先后顺序为什么重要。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('ns-seller')
        self.category = Category.objects.create(name='下一步分类')
        self.location = CampusLocation.objects.create(name='下一步地点')

    def make_history(self, *, view_count=3, first_days_ago=6, offset_days=4,
                     status='available'):
        item = Item.objects.create(
            title=f'下一步商品 {first_days_ago} {offset_days} {status}',
            description='x', price=Decimal('1.00'), status=status,
            trade_mode='sale', seller=self.seller,
            category=self.category, location=self.location,
        )
        user = make_user(f'ns-viewer-{item.id}')
        history = BrowsingHistory.objects.create(
            user=user, item=item, view_count=view_count,
        )
        first_at = self.now - timedelta(days=first_days_ago)
        BrowsingHistory.objects.filter(pk=history.pk).update(
            first_viewed_at=first_at,
            last_viewed_at=first_at + timedelta(days=offset_days),
        )
        history.refresh_from_db()
        return history

    def test_a_revisit_with_no_next_step_is_the_headline(self):
        """反复回访却既没有收藏也没有预约——本模块要指出的正是这一批。"""
        self.make_history()

        report = build_first_viewed(days=30, now=self.now)

        stats = report['next_step_stats']
        self.assertEqual(stats['measured_total'], 1)
        self.assertEqual(stats['none_count'], 1)
        self.assertEqual(stats['none_share'], 100.0)
        self.assertEqual(stats['acted_count'], 0)
        by_label = {row['step']: row['count'] for row in stats['rows']}
        self.assertEqual(by_label['none'], 1)
        self.assertIn('既没有收藏也没有下单', report['summary'])

    def test_a_favorite_after_the_first_view_counts_as_a_next_step(self):
        history = self.make_history()
        Favorite.objects.create(user=history.user, item=history.item)

        report = build_first_viewed(days=30, now=self.now)

        stats = report['next_step_stats']
        self.assertEqual(stats['favorite_count'], 1)
        self.assertEqual(stats['none_count'], 0)
        self.assertEqual(stats['acted_count'], 1)
        by_label = {row['step']: row['count'] for row in stats['rows']}
        self.assertEqual(by_label['favorite'], 1)
        self.assertIn('只收藏了没预约', report['summary'])

    def test_a_favorite_before_the_first_view_is_not_a_next_step(self):
        """先收藏、后浏览：收藏不是浏览的结果，不能算成心动的下一步。"""
        history = self.make_history()
        favorite = Favorite.objects.create(user=history.user, item=history.item)
        Favorite.objects.filter(pk=favorite.pk).update(
            created_at=history.first_viewed_at - timedelta(days=1),
        )

        report = build_first_viewed(days=30, now=self.now)

        stats = report['next_step_stats']
        self.assertEqual(stats['favorite_count'], 0)
        self.assertEqual(stats['none_count'], 1)

    def test_an_order_after_the_first_view_counts_as_a_next_step(self):
        history = self.make_history()
        Order.objects.create(
            item=history.item, buyer=history.user, seller=self.seller,
            meeting_location=self.location, agreed_price='1.00', status='completed',
        )

        report = build_first_viewed(days=30, now=self.now)

        stats = report['next_step_stats']
        self.assertEqual(stats['ordered_count'], 1)
        self.assertEqual(stats['none_count'], 0)

    def test_an_order_before_the_first_view_is_reported_separately(self):
        """先下单、后浏览：考虑窗口是负的，不许算进「没有任何下一步」。

        一个负的考虑窗口说明这条记录的顺序和「心动—考虑—下单」的叙事不一致。
        把它算成「没有任何下一步」，会让这批本已成交的样本看起来像失败的样本。
        """
        history = self.make_history()
        order = Order.objects.create(
            item=history.item, buyer=history.user, seller=self.seller,
            meeting_location=self.location, agreed_price='1.00', status='completed',
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=history.first_viewed_at - timedelta(days=2),
        )

        report = build_first_viewed(days=30, now=self.now)

        stats = report['next_step_stats']
        self.assertEqual(stats['ordered_before_count'], 1)
        self.assertEqual(stats['ordered_count'], 0)
        self.assertIn('先下单、后浏览', report['summary'])

    def test_an_order_before_the_first_view_never_enters_the_decision_window(self):
        history = self.make_history()
        order = Order.objects.create(
            item=history.item, buyer=history.user, seller=self.seller,
            meeting_location=self.location, agreed_price='1.00', status='completed',
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=history.first_viewed_at - timedelta(days=2),
        )

        report = build_first_viewed(days=30, now=self.now)

        self.assertEqual(report['decision_stats']['measured_count'], 0)

    def test_favorite_and_order_together_is_its_own_row(self):
        """收藏是留待观察，预约是准备成交，两者都有是第三条路，不是前两条的和。"""
        history = self.make_history()
        Favorite.objects.create(user=history.user, item=history.item)
        Order.objects.create(
            item=history.item, buyer=history.user, seller=self.seller,
            meeting_location=self.location, agreed_price='1.00', status='completed',
        )

        report = build_first_viewed(days=30, now=self.now)

        stats = report['next_step_stats']
        self.assertEqual(stats['both_count'], 1)
        self.assertEqual(stats['acted_count'], 1)
        by_label = {row['step']: row['count'] for row in stats['rows']}
        self.assertEqual(by_label['both'], 1)
        self.assertEqual(by_label['favorite'], 0)
        self.assertEqual(by_label['ordered'], 0)

    def test_a_single_view_never_enters_the_next_step_denominator(self):
        """只看一次的记录连窗口都没有，问它「之后做了什么」没有意义。"""
        item = Item.objects.create(
            title='单次商品', description='x', price=Decimal('1.00'),
            status='available', trade_mode='sale', seller=self.seller,
            category=self.category, location=self.location,
        )
        user = make_user('ns-single-viewer')
        BrowsingHistory.objects.create(user=user, item=item, view_count=1)

        report = build_first_viewed(days=30, now=self.now)

        self.assertEqual(report['next_step_stats']['measured_total'], 0)

    def test_next_step_rows_carry_their_own_hint(self):
        report = build_first_viewed(days=30, now=self.now)

        rows = report['next_step_stats']['rows']
        self.assertEqual(
            [row['step'] for row in rows],
            ['none', 'favorite', 'ordered', 'both'],
        )
        for row in rows:
            self.assertTrue(row['hint'])


class DecisionWindowTests(TestCase):
    """从第一次心动到下单拖了多久——分档比回访窗口粗，以周为单位。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('dw-seller')
        self.category = Category.objects.create(name='决策分类')
        self.location = CampusLocation.objects.create(name='决策地点')

    def make_purchase(self, *, days_after_first=2, first_days_ago=20):
        item = Item.objects.create(
            title=f'决策商品 {days_after_first} {first_days_ago}',
            description='x', price=Decimal('1.00'), status='completed',
            trade_mode='sale', seller=self.seller,
            category=self.category, location=self.location,
        )
        user = make_user(f'dw-viewer-{item.id}')
        history = BrowsingHistory.objects.create(user=user, item=item, view_count=3)
        first_at = self.now - timedelta(days=first_days_ago)
        BrowsingHistory.objects.filter(pk=history.pk).update(
            first_viewed_at=first_at,
            last_viewed_at=first_at + timedelta(days=1),
        )
        history.refresh_from_db()
        order = Order.objects.create(
            item=item, buyer=user, seller=self.seller,
            meeting_location=self.location, agreed_price='1.00', status='completed',
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=first_at + timedelta(days=days_after_first),
        )
        order.refresh_from_db()
        return history

    def test_the_decision_window_runs_from_the_first_view_to_the_order(self):
        self.make_purchase(days_after_first=4)

        report = build_first_viewed(days=30, now=self.now)

        stats = report['decision_stats']
        self.assertEqual(stats['measured_count'], 1)
        self.assertEqual(stats['median_label'], '4 天')
        by_label = {row['label']: row['count'] for row in stats['bucket_rows']}
        self.assertEqual(by_label['1-7 天'], 1)

    def test_decision_buckets_are_counted_in_weeks_not_days(self):
        """从心动到掏钱中间隔着一整套预约、见面、议价，它天然比回访长。"""
        self.make_purchase(days_after_first=45)

        report = build_first_viewed(days=30, now=self.now)

        stats = report['decision_stats']
        self.assertEqual(stats['over_month_count'], 1)
        by_label = {row['label']: row['count'] for row in stats['bucket_rows']}
        self.assertEqual(by_label['30-90 天'], 1)
        self.assertEqual(by_label['7-30 天'], 0)

    def test_an_order_without_a_browse_before_it_is_not_measured(self):
        """没有「先浏览」就没有考虑窗口可言，压根不进这一节。"""
        item = Item.objects.create(
            title='无浏览商品', description='x', price=Decimal('1.00'),
            status='completed', trade_mode='sale', seller=self.seller,
            category=self.category, location=self.location,
        )
        Order.objects.create(
            item=item, buyer=make_user('dw-nobrowse'), seller=self.seller,
            meeting_location=self.location, agreed_price='1.00', status='completed',
        )

        report = build_first_viewed(days=30, now=self.now)

        self.assertEqual(report['decision_stats']['measured_count'], 0)

    def test_a_revisit_that_never_ordered_does_not_enter_the_decision_window(self):
        item = Item.objects.create(
            title='未下单商品', description='x', price=Decimal('1.00'),
            status='available', trade_mode='sale', seller=self.seller,
            category=self.category, location=self.location,
        )
        user = make_user('dw-noorder')
        history = BrowsingHistory.objects.create(user=user, item=item, view_count=5)
        first_at = self.now - timedelta(days=10)
        BrowsingHistory.objects.filter(pk=history.pk).update(
            first_viewed_at=first_at, last_viewed_at=first_at + timedelta(days=5),
        )

        report = build_first_viewed(days=30, now=self.now)

        self.assertEqual(report['decision_stats']['measured_count'], 0)
        self.assertEqual(report['window_stats']['measured_count'], 1)


class StalledListTests(TestCase):
    """「想了很久却没有下一步」清单：一周是正常节奏的上界。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('sl-seller')
        self.category = Category.objects.create(name='清单分类')
        self.location = CampusLocation.objects.create(name='清单地点')

    def make_history(self, *, offset_days=9, status='available', view_count=6):
        item = Item.objects.create(
            title=f'清单商品 {offset_days} {status}',
            description='x', price=Decimal('1.00'), status=status,
            trade_mode='sale', seller=self.seller,
            category=self.category, location=self.location,
        )
        user = make_user(f'sl-viewer-{item.id}')
        history = BrowsingHistory.objects.create(
            user=user, item=item, view_count=view_count,
        )
        first_at = self.now - timedelta(days=20)
        BrowsingHistory.objects.filter(pk=history.pk).update(
            first_viewed_at=first_at,
            last_viewed_at=first_at + timedelta(days=offset_days),
        )
        history.refresh_from_db()
        return history

    def test_a_long_window_without_a_next_step_lands_on_the_list(self):
        self.make_history(offset_days=9)

        report = build_first_viewed(days=30, now=self.now)

        rows = report['stalled_rows']
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['window_label'], '9 天')
        self.assertEqual(row['view_count'], 6)
        self.assertEqual(row['item_status_label'], '在售')
        self.assertTrue(row['is_still_available'])
        self.assertFalse(row['has_favorite'])
        self.assertIn('已逐条列在下面', report['summary'])

    def test_a_window_under_a_week_stays_off_the_list(self):
        """一周之内还在比价也说得过去，不该进「现在去核实」的清单。"""
        self.make_history(offset_days=6)

        report = build_first_viewed(days=30, now=self.now)

        self.assertEqual(report['stalled_rows'], [])

    def test_a_long_window_that_reached_a_next_step_stays_off_the_list(self):
        """想了很久但已经收藏了——那是考虑清楚了，不是卡住。"""
        history = self.make_history(offset_days=9)
        Favorite.objects.create(user=history.user, item=history.item)

        report = build_first_viewed(days=30, now=self.now)

        self.assertEqual(report['stalled_rows'], [])

    def test_a_long_window_that_ordered_stays_off_the_list(self):
        history = self.make_history(offset_days=9)
        Order.objects.create(
            item=history.item, buyer=history.user, seller=self.seller,
            meeting_location=self.location, agreed_price='1.00', status='completed',
        )

        report = build_first_viewed(days=30, now=self.now)

        self.assertEqual(report['stalled_rows'], [])

    def test_the_list_says_whether_the_item_is_still_on_the_shelf(self):
        """商品还在不在架上，决定了这条记录还能不能被补救。"""
        self.make_history(offset_days=9, status='completed')

        report = build_first_viewed(days=30, now=self.now)

        row = report['stalled_rows'][0]
        self.assertEqual(row['item_status_label'], '已完成')
        self.assertFalse(row['is_still_available'])

    def test_the_longest_window_sorts_first(self):
        self.make_history(offset_days=9)
        self.make_history(offset_days=21)
        self.make_history(offset_days=14)

        report = build_first_viewed(days=30, now=self.now)

        rows = report['stalled_rows']
        self.assertEqual(
            [row['window_label'] for row in rows], ['21 天', '14 天', '9 天'],
        )

    def test_a_single_view_never_reaches_the_list(self):
        """没有窗口就没有「想了很久」可言。"""
        item = Item.objects.create(
            title='单次清单商品', description='x', price=Decimal('1.00'),
            status='available', trade_mode='sale', seller=self.seller,
            category=self.category, location=self.location,
        )
        BrowsingHistory.objects.create(
            user=make_user('sl-single'), item=item, view_count=1,
        )

        report = build_first_viewed(days=30, now=self.now)

        self.assertEqual(report['stalled_rows'], [])
