# -*- coding: utf-8 -*-
"""为「重新上架的那件商品，为什么一回来就沉底」钉住口径。

`edit_item` 的重新上架分支只改 `status`，不碰 `last_refreshed_at`，也不碰
`refresh_count`。所以重新上架这个动作在系统里没有留下任何时间戳，一件上学期发布、
这学期重新上架的商品，一回到在售列表就带着整个假期的新鲜度欠账。这些测试要钉住的
是：

1. 判据是一条纯字段比较：在售 + 未来截止时间 + 已沉底 + 一次都没刷新过。
2. 分母只收「在售且带着未来截止时间」的商品，不是全部在售商品。
3. 已售出、已预订、已过期的商品不进分母。
4. 空分母报 None 不报 0；样本不足时占比只报数字不给方向。
5. 刷新额度分布证明「问题最好解决的一批恰好最没被提醒」。
6. 无互动（浏览、收藏、成交订单三者皆零）单独数，不和「一刷就灵」混。
7. 清单只带商品标题、分类、欠账天数、刷新次数与等级，不带任何用户名。
8. 只报告：不重置任何字段、不加字段、不改等级判据。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import BrowsingHistory, CampusLocation, Category, Favorite, Item, Order
from .relist_freshness import (
    ARREARS_BUCKETS,
    DRIFT_LIST_LIMIT,
    MIN_MEASURED_ITEMS,
    REFRESH_COUNT_BUCKETS,
    build_relist_freshness,
)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class RelistFreshnessSetupTests(TestCase):
    def setUp(self):
        self.seller = make_user('relist-seller')
        self.buyer = make_user('relist-buyer')
        self.category = Category.objects.create(name='欠账测试分类')
        self.location = CampusLocation.objects.create(name='欠账测试地点')
        self._seq = 0
        self.now = timezone.now()

    def make_item(self, *, status='available', created_days_ago=3,
                  expires_in_days=30, refresh_count=0, category=None):
        self._seq += 1
        item = Item.objects.create(
            title=f'欠账测试商品 {self._seq}', description='用于新鲜度欠账统计',
            price='50.00', category=category or self.category, condition='良好',
            location=self.location, seller=self.seller,
            trade_mode='sale', status=status, refresh_count=refresh_count,
            expires_at=None if expires_in_days is None else self.now + timedelta(days=expires_in_days),
        )
        # `created_at` 是 auto_now_add，建行时拿到的是真实墙上时间。判据按
        # `created_at` 切沉底天数，所以必须回填，否则商品落在门槛之外被整条丢掉。
        Item.objects.filter(pk=item.pk).update(
            created_at=self.now - timedelta(days=created_days_ago),
        )
        item.refresh_from_db()
        return item

    def add_view(self, item):
        BrowsingHistory.objects.create(user=self.buyer, item=item)

    def add_favorite(self, item):
        Favorite.objects.create(user=self.buyer, item=item)

    def make_order(self, item, *, status='completed'):
        return Order.objects.create(
            item=item, buyer=self.buyer, seller=item.seller,
            meeting_location=self.location, agreed_price='50.00', status=status,
        )


class JudgementTests(RelistFreshnessSetupTests):
    """判据本身：四条字段比较，缺一不可。"""

    def test_all_four_conditions_met_is_reported(self):
        self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        result = build_relist_freshness(days=30)
        self.assertEqual(result['denominator']['drifted_count'], 1)
        self.assertTrue(result['has_data'])

    def test_no_deadline_items_are_not_reported(self):
        """没有截止时间的商品永远走不到 expire_items，也就没有重新上架这个动作。"""
        self.make_item(created_days_ago=60, expires_in_days=None, refresh_count=0)
        result = build_relist_freshness(days=30)
        self.assertEqual(result['denominator']['drifted_count'], 0)

    def test_past_deadline_items_are_not_reported(self):
        """截止时间已过的商品状态应当是 expired，不应当在售——这一条守住边界。"""
        self.make_item(created_days_ago=60, expires_in_days=-1, refresh_count=0)
        result = build_relist_freshness(days=30)
        self.assertEqual(result['denominator']['drifted_count'], 0)

    def test_fresh_items_are_not_reported(self):
        """没到沉底门槛的商品不欠账。"""
        self.make_item(created_days_ago=10, expires_in_days=30, refresh_count=0)
        result = build_relist_freshness(days=30)
        self.assertEqual(result['denominator']['drifted_count'], 0)

    def test_refreshed_items_are_not_reported(self):
        """刷过哪怕一次，新鲜度就已经被刷新动作接管，不算欠账。"""
        self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=1)
        result = build_relist_freshness(days=30)
        self.assertEqual(result['denominator']['drifted_count'], 0)


class DenominatorTests(RelistFreshnessSetupTests):
    """分母只收「在售且带着未来截止时间」的商品。"""

    def test_deadline_live_count_is_the_denominator(self):
        """分母是带着未来截止时间在售的商品，不是全部在售商品。

        一件没有截止时间的商品永远走不到 `expire_items`，把它算进分母只会稀释占比。
        """
        self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        self.make_item(created_days_ago=60, expires_in_days=None, refresh_count=0)
        result = build_relist_freshness(days=30)
        self.assertEqual(result['denominator']['live_count'], 2)
        self.assertEqual(result['denominator']['deadline_live_count'], 1)
        self.assertEqual(result['drifted_share'], 100.0)

    def test_non_available_items_are_not_in_the_denominator(self):
        """已售出、已预订、已过期的商品不进分母——额度对它们没有意义。"""
        self.make_item(status='sold', created_days_ago=60, expires_in_days=30, refresh_count=0)
        self.make_item(status='reserved', created_days_ago=60, expires_in_days=30, refresh_count=0)
        self.make_item(status='expired', created_days_ago=60, expires_in_days=30, refresh_count=0)
        result = build_relist_freshness(days=30)
        self.assertEqual(result['denominator']['deadline_live_count'], 0)
        self.assertEqual(result['denominator']['drifted_count'], 0)

    def test_empty_denominator_reports_none_not_zero(self):
        """空分母报 None，不报 0。"""
        result = build_relist_freshness(days=30)
        self.assertIsNone(result['drifted_share'])
        self.assertFalse(result['has_data'])
        self.assertEqual(result['denominator']['live_count'], 0)

    def test_small_sample_flagged(self):
        """在售商品不足 12 件时占比只报数字不给方向。"""
        self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        result = build_relist_freshness(days=30)
        self.assertTrue(result['arrears']['is_small_sample'])
        self.assertTrue(result['levels']['is_small_sample'])
        self.assertTrue(result['quota']['is_small_sample'])
        self.assertTrue(result['engagement']['is_small_sample'])

    def test_measured_items_threshold_is_shared(self):
        """门槛值和其余模块同一个量级。"""
        self.assertEqual(MIN_MEASURED_ITEMS, 12)


class ArrearsBucketTests(RelistFreshnessSetupTests):
    """欠账天数分档。"""

    def test_buckets_are_left_closed_right_open(self):
        self.make_item(created_days_ago=50, expires_in_days=30, refresh_count=0)
        self.make_item(created_days_ago=100, expires_in_days=30, refresh_count=0)
        self.make_item(created_days_ago=200, expires_in_days=30, refresh_count=0)
        self.make_item(created_days_ago=400, expires_in_days=30, refresh_count=0)
        result = build_relist_freshness(days=30)
        counts = [row['count'] for row in result['arrears']['bucket_rows']]
        self.assertEqual(counts, [1, 1, 1, 1])
        self.assertEqual(len(counts), len(ARREARS_BUCKETS))

    def test_bucket_labels_match_constants(self):
        result = build_relist_freshness(days=30)
        labels = [row['label'] for row in result['arrears']['bucket_rows']]
        self.assertEqual(labels, [label for _limit, label in ARREARS_BUCKETS])

    def test_first_bucket_starts_at_stale_days(self):
        """第一档的下界就是 STALE_DAYS，不另起一套。"""
        self.make_item(created_days_ago=45, expires_in_days=30, refresh_count=0)
        result = build_relist_freshness(days=30)
        self.assertEqual(result['stale_days'], 45)
        self.assertEqual(result['arrears']['bucket_rows'][0]['count'], 1)

    def test_bucket_share_uses_drifted_count_as_denominator(self):
        for index in range(4):
            self.make_item(created_days_ago=50, expires_in_days=30, refresh_count=0)
        self.make_item(created_days_ago=200, expires_in_days=30, refresh_count=0)
        result = build_relist_freshness(days=30)
        first = result['arrears']['bucket_rows'][0]
        self.assertEqual(first['count'], 4)
        self.assertEqual(first['share'], 80.0)


class LevelTests(RelistFreshnessSetupTests):
    """欠账商品现在的生命周期等级，复用 `_level_for`。"""

    def test_levels_reuse_lifecycle_judgement(self):
        """等级不是本模块自己算的，是从 lifecycle_diagnostics 复用的。"""
        self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        result = build_relist_freshness(days=30)
        labels = [row['label'] for row in result['levels']['rows']]
        self.assertIn('沉底较久', labels)
        stale_row = next(row for row in result['levels']['rows'] if row['label'] == '沉底较久')
        self.assertEqual(stale_row['count'], 1)
        self.assertEqual(stale_row['share'], 100.0)

    def test_only_attention_levels_are_listed(self):
        """只列需要卖家决策的三档，不列 fresh / active / archived。"""
        self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        result = build_relist_freshness(days=30)
        self.assertEqual(len(result['levels']['rows']), 3)
        levels = [row['level'] for row in result['levels']['rows']]
        self.assertEqual(levels, ['expiring', 'stale', 'slowing'])

    def test_expiring_wins_over_stale(self):
        """即将到期的商品按「即将到期」报，等级判据的优先级由 lifecycle 决定。"""
        self.make_item(created_days_ago=60, expires_in_days=1, refresh_count=0)
        result = build_relist_freshness(days=30)
        expiring_row = next(row for row in result['levels']['rows'] if row['level'] == 'expiring')
        self.assertEqual(expiring_row['count'], 1)


class QuotaTests(RelistFreshnessSetupTests):
    """刷新额度对这批商品是不是空的。"""

    def test_first_bucket_equals_all_items(self):
        """判据里限定了 refresh_count == 0，所以第一档必然等于全部。"""
        for index in range(3):
            self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        result = build_relist_freshness(days=30)
        first = result['quota']['bucket_rows'][0]
        self.assertEqual(first['label'], '一次都没刷新过')
        self.assertEqual(first['count'], 3)
        self.assertEqual(first['share'], 100.0)

    def test_ready_count_is_reported_separately(self):
        """额度没用尽、间隔也没占着的件数单独报——那才是运营现在就能推动的部分。"""
        self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        result = build_relist_freshness(days=30)
        self.assertEqual(result['quota']['ready_count'], 1)
        self.assertEqual(result['quota']['ready_share'], 100.0)

    def test_quota_limits_are_exposed_for_the_template(self):
        result = build_relist_freshness(days=30)
        self.assertEqual(result['quota']['max_refresh_per_month'], 4)
        self.assertEqual(result['quota']['min_refresh_gap_hours'], 24)

    def test_refresh_count_buckets_cover_the_quota(self):
        """分档覆盖到 MAX_REFRESH_PER_MONTH，最后一档兜底。"""
        self.assertEqual(REFRESH_COUNT_BUCKETS[-1][0], 5)


class EngagementTests(RelistFreshnessSetupTests):
    """欠账商品里还有没有互动。"""

    def test_silent_items_are_counted_separately(self):
        """三者皆无的单独数，不和「一刷就灵」混。"""
        self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        result = build_relist_freshness(days=30)
        self.assertEqual(result['engagement']['silent_count'], 2)
        self.assertEqual(result['engagement']['silent_share'], 100.0)
        self.assertEqual(result['engagement']['engaged_count'], 0)

    def test_any_one_signal_counts_as_engaged(self):
        """浏览、收藏、成交订单三者有其一即算有互动。"""
        viewed = self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        favorited = self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        ordered = self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        self.add_view(viewed)
        self.add_favorite(favorited)
        self.make_order(ordered)
        result = build_relist_freshness(days=30)
        self.assertEqual(result['engagement']['engaged_count'], 3)
        self.assertEqual(result['engagement']['silent_count'], 0)

    def test_cancelled_orders_do_not_count_as_engagement(self):
        """取消是失败，不是需求。"""
        item = self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        self.make_order(item, status='cancelled')
        result = build_relist_freshness(days=30)
        self.assertEqual(result['engagement']['engaged_count'], 0)
        self.assertEqual(result['engagement']['silent_count'], 1)


class DriftListTests(RelistFreshnessSetupTests):
    """欠账商品清单。"""

    def test_list_is_ordered_by_arrears_desc(self):
        """欠得最多的排前面——那是运营最该先看的一批。"""
        shallow = self.make_item(created_days_ago=50, expires_in_days=30, refresh_count=0)
        deep = self.make_item(created_days_ago=200, expires_in_days=30, refresh_count=0)
        result = build_relist_freshness(days=30)
        rows = result['drift_list']['rows']
        self.assertEqual([row.item.id for row in rows], [deep.id, shallow.id])
        self.assertEqual([row.arrears_days for row in rows], [200, 50])

    def test_list_is_truncated_and_says_so(self):
        for index in range(DRIFT_LIST_LIMIT + 3):
            self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        result = build_relist_freshness(days=30)
        self.assertEqual(len(result['drift_list']['rows']), DRIFT_LIST_LIMIT)
        self.assertTrue(result['drift_list']['truncated'])
        self.assertEqual(result['drift_list']['total_count'], DRIFT_LIST_LIMIT + 3)

    def test_list_carries_no_username(self):
        """清单不带任何用户名——那是一份指责清单，不是一份看板。"""
        self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        result = build_relist_freshness(days=30)
        row = result['drift_list']['rows'][0]
        self.assertNotIn('seller', row._fields)
        self.assertIn('category_label', row._fields)

    def test_empty_list_says_so(self):
        result = build_relist_freshness(days=30)
        self.assertEqual(result['drift_list']['rows'], [])
        self.assertFalse(result['drift_list']['truncated'])


class PeriodTests(RelistFreshnessSetupTests):
    """周期参数只影响说明文字，不参与筛选。"""

    def test_period_does_not_filter_the_judgement(self):
        """新鲜度是当前状态，按 created_at 切周期会漏掉上学期发布的商品。

        本模块刻意不带周期过滤——那批正是唯一要看的那批。
        """
        self.make_item(created_days_ago=400, expires_in_days=30, refresh_count=0)
        for days in (7, 30, 90, 365):
            result = build_relist_freshness(days=days)
            self.assertEqual(
                result['denominator']['drifted_count'], 1,
                f'{days} 天窗口不应改变判据结果',
            )

    def test_period_choices_are_exposed(self):
        result = build_relist_freshness(days=30)
        self.assertEqual(result['period_days'], 30)
        self.assertEqual(len(result['period_choices']), 4)


class ReportOnlyTests(RelistFreshnessSetupTests):
    """只报告：不重置任何字段、不改任何写入路径。"""

    def test_build_does_not_write_any_field(self):
        item = self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        before = Item.objects.get(pk=item.pk)
        build_relist_freshness(days=30)
        after = Item.objects.get(pk=item.pk)
        self.assertEqual(before.status, after.status)
        self.assertEqual(before.refresh_count, after.refresh_count)
        self.assertEqual(before.last_refreshed_at, after.last_refreshed_at)
        self.assertEqual(before.expires_at, after.expires_at)

    def test_build_does_not_create_or_delete_rows(self):
        self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        before = Item.objects.count()
        build_relist_freshness(days=30)
        self.assertEqual(Item.objects.count(), before)

    def test_summary_states_the_denominator(self):
        self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        result = build_relist_freshness(days=30)
        self.assertIn('1', result['summary'])
        self.assertIn('从没刷新过', result['summary'])


class ExportShapeTests(RelistFreshnessSetupTests):
    """导出到 CSV 时形状要稳定。"""

    def test_all_documented_keys_are_present(self):
        self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        result = build_relist_freshness(days=30)
        for key in (
            'period_days', 'period_choices', 'has_data', 'summary',
            'stale_days', 'max_refresh_per_month', 'min_measured_items',
            'denominator', 'drifted_share', 'arrears', 'levels',
            'quota', 'engagement', 'drift_list',
        ):
            self.assertIn(key, result)

    def test_empty_result_still_has_every_key(self):
        """空态也要有完整的键——模板和导出不能为 None 单独写分支。"""
        result = build_relist_freshness(days=30)
        for key in ('arrears', 'levels', 'quota', 'engagement', 'drift_list'):
            self.assertIn(key, result)
        self.assertEqual(result['arrears']['measured_count'], 0)
        self.assertEqual(result['levels']['measured_count'], 0)
        self.assertEqual(result['quota']['measured_count'], 0)
        self.assertEqual(result['engagement']['measured_count'], 0)

    def test_empty_bucket_rows_have_labels(self):
        """空态的分档表也要有行有标签，只是计数为零。"""
        result = build_relist_freshness(days=30)
        self.assertEqual(
            [row['label'] for row in result['arrears']['bucket_rows']],
            [label for _limit, label in ARREARS_BUCKETS],
        )
        self.assertTrue(all(row['count'] == 0 for row in result['arrears']['bucket_rows']))
        self.assertTrue(all(row['share'] is None for row in result['arrears']['bucket_rows']))

    def test_quota_empty_rows_have_labels(self):
        result = build_relist_freshness(days=30)
        self.assertEqual(
            [row['label'] for row in result['quota']['bucket_rows']],
            [label for _limit, label in REFRESH_COUNT_BUCKETS],
        )
        self.assertEqual(result['quota']['ready_count'], 0)
        self.assertIsNone(result['quota']['ready_share'])

    def test_empty_levels_have_labels(self):
        result = build_relist_freshness(days=30)
        self.assertEqual(
            [row['label'] for row in result['levels']['rows']],
            ['即将到期', '沉底较久', '关注放缓'],
        )


class RenderedPanelTests(RelistFreshnessSetupTests):
    """面板在带数据的真实渲染下不炸，而且该出现的数字都出现。"""

    def test_panel_renders_with_rows(self):
        from django.urls import reverse
        staff = User.objects.create_user(
            username='relist-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(staff)
        for index in range(3):
            self.make_item(created_days_ago=60, expires_in_days=30, refresh_count=0)
        response = self.client.get(reverse('operations_dashboard'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('重新上架的那件商品，为什么一回来就沉底', html)
        self.assertIn('带着新鲜度欠账', html)
        self.assertIn('现在就能刷新', html)
        self.assertIn('一次都没刷新过', html)
        self.assertIn('relist-freshness-panel', html)

    def test_panel_renders_empty_state(self):
        from django.urls import reverse
        staff = User.objects.create_user(
            username='relist-staff-empty', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(staff)
        response = self.client.get(reverse('operations_dashboard'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('relist-freshness-panel', html)

    def test_panel_is_not_visible_to_anonymous_users(self):
        from django.urls import reverse
        response = self.client.get(reverse('operations_dashboard'), {'days': 30})
        self.assertIn(response.status_code, (302, 403))
