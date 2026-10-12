# -*- coding: utf-8 -*-
"""为「关注搜索命中时延」钉住口径。

`SavedSearchMatch` 一直在写 `matched_at` 和 `notified_at` 两个时间戳，但差值从来没被
算过。这个模块把它算出来，只报告不改写。这些测试要钉住的是：

1. 只统计已经送达的命中，缓冲区里的不计入等待分布，但单独数出来。
2. 只统计日度和周度，即时节奏压根不该出现——它的两个时间戳差约等于零。
3. 等待时长按天分档，分档左闭右开，边界值落在正确的一档。
4. 日度和周度各用自己的一套阈值，不合成一个平均数。
5. 时延要和送达时商品是否还可交易交叉，逐档对照而不是只看总数。
6. 「送达时已不可交易」是按当前状态回看的，所以偏高——这条口径要被写进报告。
7. 待核实清单按等待时长倒序，且只列商品已不可交易的。
8. 样本不足时要说出来，不假装有方向。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Category, Item, SavedSearch, SavedSearchMatch
from .saved_search_latency import (
    DAILY_LATENCY_BUCKETS, MIN_MEASURED_MATCHES, STALE_LIST_LIMIT,
    build_saved_search_latency,
)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class SavedSearchLatencyTests(TestCase):
    def setUp(self):
        self.owner = make_user('latency-owner')
        self.category = Category.objects.create(name='时延测试分类')
        # 用户名和关注条件名都要唯一。id(self) 在同一次测试里是固定的，
        # 连续建两条参数相同的命中会当场撞唯一约束，所以用实例计数器。
        self._seq = 0

    def make_match(self, *, cadence='daily', wait_days=0, item_available=True, matched_days_ago=2):
        """建一条已经送达的命中，等待时长按天给，商品状态可指定。"""
        self._seq += 1
        saved_search = SavedSearch.objects.create(
            user=self.owner, name=f'时延条件-{self._seq}',
            notify_frequency=cadence,
        )
        item = Item.objects.create(
            seller=make_user(f'seller-{self._seq}'),
            title=f'商品 {self._seq}', description='描述', price='10.00',
            condition='九成新', status='available', category=self.category,
        )
        if not item_available:
            item.status = 'sold'
            item.save(update_fields=['status'])
        matched_at = timezone.now() - timedelta(days=matched_days_ago)
        row = SavedSearchMatch.objects.create(saved_search=saved_search, item=item)
        # matched_at 是 auto_now_add，只能.update() 改。
        SavedSearchMatch.objects.filter(pk=row.pk).update(
            matched_at=matched_at,
            notified_at=matched_at + timedelta(days=wait_days),
        )
        return row

    def test_only_delivered_matches_are_measured(self):
        self.make_match(cadence='daily', wait_days=0)
        pending = self.make_match(cadence='daily', wait_days=5)
        SavedSearchMatch.objects.filter(pk=pending.pk).update(notified_at=None)

        report = build_saved_search_latency(days=30)
        self.assertEqual(report['daily']['measured_count'], 1)
        self.assertEqual(report['pending_count'], 1)

    def test_instant_cadence_is_never_measured(self):
        self.make_match(cadence='instant', wait_days=0)
        report = build_saved_search_latency(days=30)
        self.assertEqual(report['measured_total'], 0)
        self.assertFalse(report['has_data'])

    def test_wait_is_bucketed_by_days_not_hours(self):
        # 差 6 小时：按天分档必须落「当天送达」，不能单独成一档。
        self.make_match(cadence='daily', wait_days=0)
        SavedSearchMatch.objects.update(
            notified_at=timezone.now() - timedelta(days=1, hours=18),
        )
        report = build_saved_search_latency(days=30)
        self.assertEqual(report['daily']['rows'][0]['count'], 1)
        self.assertEqual(report['daily']['mean_label'], '当天')

    def test_bucket_boundaries_are_left_closed(self):
        # 正好 1 天：左闭右开，落「1 天内」不落「当天送达」。
        row = self.make_match(cadence='daily', wait_days=1)
        # matched_at 是 auto_now_add，只能 .update() 改，改完内存里的实例不会跟着变，
        # 所以断言那两个时间戳之前必须从库里读回来。
        row.refresh_from_db()
        report = build_saved_search_latency(days=30)
        self.assertEqual(report['daily']['rows'][0]['count'], 0)
        self.assertEqual(report['daily']['rows'][1]['count'], 1)
        self.assertEqual(row.notified_at - row.matched_at, timedelta(days=1))

    def test_daily_and_weekly_use_separate_thresholds(self):
        # 等 10 天：日度已经越过最后一档的边界（「超过 7 天」），
        # 周度还落在中间档（「7-14 天」）。同一个等待，两套阈值给出两种判断，
        # 这正是要分开报的原因。
        self.make_match(cadence='daily', wait_days=10)
        self.make_match(cadence='weekly', wait_days=10)
        report = build_saved_search_latency(days=30)
        self.assertEqual(report['daily']['rows'][-1]['count'], 1)
        self.assertEqual(report['daily']['rows'][-1]['label'], '超过 7 天')
        self.assertEqual(report['weekly']['rows'][3]['count'], 1)
        self.assertEqual(report['weekly']['rows'][3]['label'], '7-14 天')
        # 两套分档不是同一份常量，档位数也不同。
        self.assertEqual(len(DAILY_LATENCY_BUCKETS), len(report['daily']['rows']))
        self.assertNotEqual(DAILY_LATENCY_BUCKETS[3][1], report['weekly']['rows'][3]['label'])

    def test_latency_is_crossed_with_item_availability_per_bucket(self):
        self.make_match(cadence='daily', wait_days=0, item_available=True)
        self.make_match(cadence='daily', wait_days=0, item_available=False)
        report = build_saved_search_latency(days=30)
        cross = report['daily']['cross'][0]
        self.assertEqual(cross['count'], 2)
        self.assertEqual(cross['stale_count'], 1)
        self.assertEqual(cross['stale_rate'], 50.0)

    def test_report_says_the_stale_number_is_an_overestimate(self):
        self.make_match(cadence='daily', wait_days=0, item_available=False)
        report = build_saved_search_latency(days=30)
        # 口径必须写在报告里：按当前状态回看，送达之后才下架的也会算进来。
        self.assertIn('偏高', report['summary'])

    def test_stale_rows_are_sorted_by_wait_descending(self):
        self.make_match(cadence='daily', wait_days=1, item_available=False)
        self.make_match(cadence='daily', wait_days=6, item_available=False)
        self.make_match(cadence='daily', wait_days=3, item_available=True)
        report = build_saved_search_latency(days=30)
        waits = [row['wait_seconds'] for row in report['stale_rows']]
        self.assertEqual(waits, sorted(waits, reverse=True))
        # 可交易的命中不进这份清单。
        self.assertEqual(len(report['stale_rows']), 2)

    def test_stale_rows_respect_the_list_limit(self):
        for index in range(STALE_LIST_LIMIT + 3):
            self.make_match(cadence='daily', wait_days=index % 5, item_available=False)
        report = build_saved_search_latency(days=30)
        self.assertEqual(len(report['stale_rows']), STALE_LIST_LIMIT)

    def test_small_sample_is_flagged(self):
        for index in range(MIN_MEASURED_MATCHES - 1):
            self.make_match(cadence='daily', wait_days=0)
        report = build_saved_search_latency(days=30)
        self.assertTrue(report['daily']['is_small_sample'])
        self.assertIn('样本不足', report['summary'])

    def test_matches_outside_the_window_are_excluded(self):
        self.make_match(cadence='daily', wait_days=0, matched_days_ago=40)
        report = build_saved_search_latency(days=30)
        self.assertEqual(report['daily']['measured_count'], 0)

    def test_negative_latency_is_clamped_to_zero(self):
        row = self.make_match(cadence='daily', wait_days=0)
        row.refresh_from_db()
        # 送达早于命中：这是数据里的一处矛盾，不该让分布变成负数。
        SavedSearchMatch.objects.filter(pk=row.pk).update(
            notified_at=row.matched_at - timedelta(days=1),
        )
        report = build_saved_search_latency(days=30)
        self.assertEqual(report['daily']['measured_count'], 1)
        self.assertEqual(report['daily']['mean_label'], '当天')

    def test_empty_report_is_still_well_formed(self):
        report = build_saved_search_latency(days=30)
        self.assertFalse(report['has_data'])
        self.assertEqual(report['daily']['measured_count'], 0)
        self.assertEqual(report['weekly']['measured_count'], 0)
        self.assertEqual(report['stale_rows'], [])
        self.assertIn('没有已送达的命中', report['summary'])


class SavedSearchLatencyDashboardTests(TestCase):
    """时延面板要真的出现在看板上，而且导出的列名要对得上。"""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='latency-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_panel_is_rendered_on_the_operations_dashboard(self):
        response = self.client.get(reverse('operations_dashboard'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '一条命中等了多久才送到')
        # 空态下面板只留一句说明，表格整块折起来。所以这里只断言节标题和口径说明，
        # 不断言表格里的子标题——那一条由下面那条有数据的测试来钉。
        self.assertContains(response, 'Saved search latency')

    def test_export_carries_the_panel_section(self):
        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        text = response.content.decode('utf-8-sig')
        self.assertIn('一条命中等了多久才送到', text)
        # 导出不折叠：空态下表头也要在，下载回去的表才有列名可用。
        self.assertIn('等待档', text)
        self.assertIn('该档里商品已不可交易', text)
