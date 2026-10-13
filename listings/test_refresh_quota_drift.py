# -*- coding: utf-8 -*-
"""刷新额度为什么是一次性的，第一次被运营读到。

`_refresh_state` 的第二条判据读的是 `refresh_count`，一个从不清零的累计计数器；
而它写给卖家看的文案是「**本月**刷新次数已达上限」。本模块把这个分歧量出来，
这些测试要钉住的不是四舍五入，而是五件更根本的事。

第一件是取数范围。额度是一个**当前状态**，不是周期状态。按发布周期切会把
「上学期发布、现在仍然刷不动」的商品整批排除——而那正是口径分歧的主要受害者。
测试要钉住：把 `days` 从 30 改成 7，被累计口径吃掉的商品数一个都不变。

第二件是「本周期撞墙数」为什么精确。限流判据是 `refresh_count >= 4`，而
`refresh_count` 只会被 `refresh_item` 加一，所以它最多到 4、永远不可能到 5。
于是 `count == 4` 时 `last_refreshed_at` 就是撞墙那一刻。测试要钉住这个恒等式，
以及它成立的前提（没有任何地方按月归零）。

第三件是「被累计口径吃掉」的判据。它要求额度已用尽**且**最后一次刷新距今超过
30 天。测试要钉住两个边界：刚刷过一个月内的不进（文案对它还是真的），
从未刷新过的不进（它压根没到「用尽」）。

第四件是两条限流必须分开报。24 小时间隔和额度用尽给用户的提示长得一模一样，
但只有一条会犯「本月」这个错。测试要钉住一件既用尽额度又刚刷过的商品被算成
额度，不是间隔。

第五件是右删失。已下架、已成交的商品也可能额度用尽，但它们「还能不能刷新」
已经没有意义。测试要钉住它们被单独数，不混进在售分母。
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .lifecycle_diagnostics import (
    MAX_REFRESH_PER_MONTH,
    MIN_REFRESH_GAP_HOURS,
    STALE_DAYS,
)
from .models import Category, Item
from .refresh_quota_drift import (
    MIN_MEASURED_ITEMS,
    QUOTA_MONTH_DAYS,
    SINCE_REFRESH_BUCKETS,
    _quota_state,
    build_refresh_quota_drift,
)


def local_noon():
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


class QuotaSetupTests(TestCase):
    """夹具、空态和统计窗口。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = User.objects.create_user(
            username='quota-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='额度分类1')

        self.counter = 0

    def make_item(self, title, **kwargs):
        self.counter += 1
        defaults = {
            'title': title,
            'description': '用于刷新额度统计',
            'price': Decimal('30.00'),
            'status': 'available',
            'seller': self.seller,
            'category': self.category,
            'refresh_count': 0,
        }
        defaults.update(kwargs)
        item = Item.objects.create(**defaults)
        if defaults.get('last_refreshed_at'):
            Item.objects.filter(pk=item.pk).update(
                last_refreshed_at=defaults['last_refreshed_at'],
            )
        return item

    def exhausted(self, title, *, since_days, **kwargs):
        """造一件额度已用尽、最后一次刷新在 since_days 天前的商品。

        `refresh_count` 直接写成上限值：限流判据是 `>= 4`，所以到 4 就已经
        刷不动了，5 这个值在真实数据里不可能出现。测试里也不该造它。
        """
        refreshed_at = self.now - timedelta(days=since_days)
        return self.make_item(
            title, refresh_count=MAX_REFRESH_PER_MONTH,
            last_refreshed_at=refreshed_at, **kwargs,
        )


class EvidenceTests(TestCase):
    """额度是当前状态，不按发布周期切。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = User.objects.create_user(
            username='evi-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='额度分类2')

    def test_an_old_listing_still_counts_when_the_period_is_short(self):
        """上学期发布的商品不能因为周期短而被排除。

        这是本模块和按 `created_at` 取数的模块最根本的区别：口径分歧的受害者
        恰恰是发布得早、搁得久的那一批。按发布周期切会把它们整批漏掉。
        """
        Item.objects.create(
            title='上学期发布的商品', description='d', price=Decimal('10.00'),
            status='available', seller=self.seller, category=self.category, refresh_count=MAX_REFRESH_PER_MONTH,
        )
        Item.objects.filter(refresh_count=MAX_REFRESH_PER_MONTH).update(
            last_refreshed_at=self.now - timedelta(days=200),
        )

        for days in (7, 30, 90, 365):
            report = build_refresh_quota_drift(days=days, now=self.now)
            self.assertEqual(
                report['quota']['drifted_count'], 1,
                f'周期 {days} 天把发布已久的商品漏掉了',
            )

    def test_period_only_changes_the_wall_counter(self):
        """周期参数只影响「本周期撞墙数」这一个数。"""
        refreshed_at = self.now - timedelta(days=40)
        Item.objects.create(
            title='周期商品', description='d', price=Decimal('10.00'),
            status='available', seller=self.seller, category=self.category,
            refresh_count=MAX_REFRESH_PER_MONTH, last_refreshed_at=refreshed_at,
        )

        long_report = build_refresh_quota_drift(days=90, now=self.now)
        short_report = build_refresh_quota_drift(days=7, now=self.now)

        # 额度、间隔、清单都与周期无关。
        for key in ('quota', 'cooldown', 'since_refresh', 'drifted_state', 'drift_list'):
            self.assertEqual(
                long_report[key], short_report[key],
                f'{key} 不该随周期变化',
            )
        # 只有撞墙数会变：40 天前那次刷新落在 90 天窗口内，不落在 7 天窗口内。
        self.assertEqual(long_report['wall']['hit_wall_count'], 1)
        self.assertEqual(short_report['wall']['hit_wall_count'], 0)

    def test_wall_hit_is_exact_because_count_cannot_pass_the_limit(self):
        """撞墙数之所以精确，是因为 refresh_count 到 4 就封顶了。

        限流判据是 `refresh_count >= 4`，而 `refresh_item` 只会把它加一，所以
        真实数据里不可能出现 5。于是 count 等于 4 时，`last_refreshed_at` 就是
        撞上额度墙的那一刻——不需要一张刷新历史表。这条测试钉住那个前提。
        """
        refreshed_at = self.now - timedelta(days=3)
        Item.objects.create(
            title='刚撞墙', description='d', price=Decimal('10.00'),
            status='available', seller=self.seller, category=self.category,
            refresh_count=MAX_REFRESH_PER_MONTH, last_refreshed_at=refreshed_at,
        )

        report = build_refresh_quota_drift(days=30, now=self.now)

        self.assertEqual(report['wall']['refreshed_in_period'], 1)
        self.assertEqual(report['wall']['hit_wall_count'], 1)
        # 本周期撞墙的商品，最后一次刷新就在 3 天前，所以它没有被累计口径吃掉。
        self.assertEqual(report['quota']['drifted_count'], 0)

    def test_refresh_count_is_never_reset_anywhere(self):
        """项目里没有任何一处把 refresh_count 归零。

        这是整个模块的前提。如果哪天有人加了按月清零，这个测试会失败，而那时
        该做的是把模块改掉，不是把这个测试删掉。
        """
        import io
        import os

        touched = []
        for root, dirs, files in os.walk('.'):
            if any(part in root for part in ('.git', '__pycache__', 'migrations')):
                continue
            for name in files:
                if not name.endswith('.py'):
                    continue
                path = os.path.join(root, name)
                text = io.open(path, encoding='utf-8', errors='ignore').read()
                if path.endswith('test_refresh_quota_drift.py'):
                    continue
                if 'refresh_count=0' in text or 'refresh_count = 0' in text:
                    touched.append(path)
        self.assertEqual(touched, [], '有人把 refresh_count 归零了，本模块的前提变了')


class DenominatorTests(TestCase):
    """分母只收仍在展示的商品，右删失样本单独数。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = User.objects.create_user(
            username='den-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='额度分类3')
        self.refreshed_at = self.now - timedelta(days=45)

    def make(self, title, status):
        return Item.objects.create(
            title=title, description='d', price=Decimal('10.00'),
            status=status, seller=self.seller, category=self.category,
            refresh_count=MAX_REFRESH_PER_MONTH, last_refreshed_at=self.refreshed_at,
        )

    def test_ended_listings_are_counted_separately(self):
        self.make('在售', 'available')
        self.make('已售出', 'sold')
        self.make('已下架', 'expired')

        report = build_refresh_quota_drift(days=30, now=self.now)

        self.assertEqual(report['quota']['live_count'], 1)
        self.assertEqual(report['quota']['ended_count'], 2)
        self.assertEqual(report['quota']['exhausted_live_count'], 1)
        # 已结束的两件也额度用尽，但单独数，不混进在售分母。
        self.assertEqual(report['quota']['exhausted_ended_count'], 2)
        self.assertEqual(report['quota']['exhausted_share'], 100.0)

    def test_a_never_refreshed_listing_is_not_drifted(self):
        """从未刷新过的商品 refresh_count 是 0，压根没到「用尽」。"""
        Item.objects.create(
            title='从未刷新', description='d', price=Decimal('10.00'),
            status='available', seller=self.seller, category=self.category, refresh_count=0,
        )

        report = build_refresh_quota_drift(days=30, now=self.now)

        self.assertEqual(report['quota']['exhausted_live_count'], 0)
        self.assertEqual(report['quota']['drifted_count'], 0)
        self.assertEqual(report['since_refresh']['measured_count'], 0)
        self.assertIsNone(report['since_refresh']['bucket_rows'][0]['share'])


class DriftBoundaryTests(TestCase):
    """「被累计口径吃掉」的判据：用尽 且 最后一次刷新超过一个月。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = User.objects.create_user(
            username='bnd-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='额度分类4')

    def make(self, title, since_days):
        return Item.objects.create(
            title=title, description='d', price=Decimal('10.00'),
            status='available', seller=self.seller, category=self.category,
            refresh_count=MAX_REFRESH_PER_MONTH,
            last_refreshed_at=self.now - timedelta(days=since_days),
        )

    def test_a_recent_refresh_keeps_the_monthly_claim_true(self):
        """一个月内刷过，「本月 4 次」这句文案对它还是真的。"""
        self.make('上周刷过', 6)

        report = build_refresh_quota_drift(days=30, now=self.now)

        self.assertEqual(report['quota']['exhausted_live_count'], 1)
        self.assertEqual(report['quota']['drifted_count'], 0)
        self.assertIn('每一件的最后一次刷新都在一个月内', report['summary'])

    def test_an_old_refresh_makes_the_monthly_claim_empty(self):
        self.make('几个月前刷过', 100)

        report = build_refresh_quota_drift(days=30, now=self.now)

        self.assertEqual(report['quota']['exhausted_live_count'], 1)
        self.assertEqual(report['quota']['drifted_count'], 1)
        self.assertEqual(report['quota']['drifted_share'], 100.0)
        self.assertIn('这句文案对它们是空的', report['summary'])

    def test_the_boundary_itself_is_not_drifted(self):
        """正好 30 天不算「超过 30 天」——判据是严格大于。"""
        self.make('正好一个月', QUOTA_MONTH_DAYS)

        report = build_refresh_quota_drift(days=30, now=self.now)

        self.assertEqual(report['quota']['drifted_count'], 0)


class CooldownTests(TestCase):
    """两条限流必须分开报：24 小时间隔不吃累计口径，它不犯「本月」这个错。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = User.objects.create_user(
            username='cool-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='额度分类5')

    def test_a_recently_refreshed_listing_is_on_cooldown_not_drifted(self):
        """刚刷过、额度未用尽：卡的是 24 小时间隔，不是额度。"""
        Item.objects.create(
            title='一小时前刷过', description='d', price=Decimal('10.00'),
            status='available', seller=self.seller, category=self.category, refresh_count=1,
            last_refreshed_at=self.now - timedelta(hours=1),
        )

        report = build_refresh_quota_drift(days=30, now=self.now)

        self.assertEqual(report['cooldown']['cooldown_count'], 1)
        self.assertEqual(report['quota']['exhausted_live_count'], 0)

    def test_an_exhausted_listing_is_never_counted_as_cooldown(self):
        """既用尽额度又刚刷过：它卡的是额度，不是间隔。

        少了「额度未用尽」这个条件，两批会被混成一个数，而运营该改的是另一条。
        """
        Item.objects.create(
            title='刚撞墙又刚刷过', description='d', price=Decimal('10.00'),
            status='available', seller=self.seller, category=self.category,
            refresh_count=MAX_REFRESH_PER_MONTH,
            last_refreshed_at=self.now - timedelta(hours=1),
        )

        report = build_refresh_quota_drift(days=30, now=self.now)

        self.assertEqual(report['cooldown']['cooldown_count'], 0)
        self.assertEqual(report['quota']['exhausted_live_count'], 1)
        # 一小时前刷过，所以它也没有被累计口径吃掉。
        self.assertEqual(report['quota']['drifted_count'], 0)

    def test_cooldown_needs_an_actual_refresh(self):
        """没刷过的商品不可能卡在「两次刷新至少间隔 24 小时」上。"""
        Item.objects.create(
            title='没刷过', description='d', price=Decimal('10.00'),
            status='available', seller=self.seller, category=self.category, refresh_count=0,
        )

        report = build_refresh_quota_drift(days=30, now=self.now)

        self.assertEqual(report['cooldown']['cooldown_count'], 0)


class SinceRefreshTests(TestCase):
    """「距上次刷新多久」只收额度用尽且刷新过的商品。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = User.objects.create_user(
            username='since-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='额度分类6')

    def make(self, title, since_days):
        return Item.objects.create(
            title=title, description='d', price=Decimal('10.00'),
            status='available', seller=self.seller, category=self.category,
            refresh_count=MAX_REFRESH_PER_MONTH,
            last_refreshed_at=self.now - timedelta(days=since_days),
        )

    def test_buckets_are_exclusive_and_cover_the_over_month_span(self):
        self.make('不到一天', 0)
        self.make('一周', 6)
        self.make('一个月内', 20)
        self.make('三个月', 60)
        self.make('半年', 200)

        report = build_refresh_quota_drift(days=30, now=self.now)
        rows = report['since_refresh']['bucket_rows']

        counts = {row['label']: row['count'] for row in rows}
        self.assertEqual(counts['不到 1 天'], 1)
        self.assertEqual(counts['1-7 天'], 1)
        self.assertEqual(counts['7-30 天'], 1)
        self.assertEqual(counts['30-90 天'], 1)
        self.assertEqual(counts['90 天以上'], 1)
        self.assertEqual(report['since_refresh']['measured_count'], 5)
        # 后两档合计就是「本月 4 次」这句文案说谎的范围。
        self.assertEqual(report['since_refresh']['over_month_count'], 2)
        self.assertEqual(report['since_refresh']['over_month_share'], 40.0)

    def test_a_listing_with_no_refresh_never_enters_the_buckets(self):
        """没刷过的商品没有「距上次刷新」，放进来会让 90 天以上虚高。"""
        Item.objects.create(
            title='没刷过', description='d', price=Decimal('10.00'),
            status='available', seller=self.seller, category=self.category, refresh_count=0,
        )

        report = build_refresh_quota_drift(days=30, now=self.now)

        self.assertEqual(report['since_refresh']['measured_count'], 0)
        self.assertIsNone(report['since_refresh']['over_month_share'])


class DriftedStateTests(TestCase):
    """被累计口径吃掉的商品，现在处于什么生命周期等级。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = User.objects.create_user(
            username='state-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='额度分类7')

    def make(self, title, *, last_active_days_ago, refresh_days_ago):
        return Item.objects.create(
            title=title, description='d', price=Decimal('10.00'),
            status='available', seller=self.seller, category=self.category,
            refresh_count=MAX_REFRESH_PER_MONTH,
            last_refreshed_at=self.now - timedelta(days=refresh_days_ago),
            created_at=self.now - timedelta(days=last_active_days_ago),
        )

    def test_stale_drifted_listings_are_reported_by_level(self):
        """一件额度用尽且已沉底的商品，和一件额度用尽但状态良好的，该做的事相反。"""
        self.make('沉底的', last_active_days_ago=100, refresh_days_ago=60)
        # 等级只按 last_active_at 算，与额度无关：刷新只改最近一次活跃时间。
        # 两件最后一次刷新都是 60 天前，所以都被累计口径吃掉，也都被判成「沉底较久」。
        self.make('一起沉底的', last_active_days_ago=10, refresh_days_ago=60)

        report = build_refresh_quota_drift(days=30, now=self.now)
        rows = {row['label']: row['count'] for row in report['drifted_state']['rows']}

        self.assertEqual(rows['沉底较久'], 2)
        self.assertEqual(rows['等级正常'], 0)
        # 等级与「能不能刷新」是两条独立的判据。
        self.assertEqual(report['drifted_state']['measured_count'], 2)

    def test_the_level_comes_from_the_same_rule_the_seller_sees(self):
        """等级判据必须和卖家页面同源，不能另写一套。"""
        # 20 天前刚刷过：文案没有说谎，所以压根不进这一档。
        self.make('刚刷过的', last_active_days_ago=10, refresh_days_ago=20)

        report = build_refresh_quota_drift(days=30, now=self.now)
        rows = {row['label']: row['count'] for row in report['drifted_state']['rows']}

        self.assertEqual(report['drifted_state']['measured_count'], 0)
        self.assertTrue(all(count == 0 for count in rows.values()))
        self.assertTrue(all(row['share'] is None for row in report['drifted_state']['rows']))

    def test_the_rows_add_up_to_the_total(self):
        """等级档必须把样本分完，否则运营会以为漏了。"""
        self.make('甲', last_active_days_ago=100, refresh_days_ago=60)
        self.make('乙', last_active_days_ago=10, refresh_days_ago=60)
        self.make('丙', last_active_days_ago=30, refresh_days_ago=60)

        report = build_refresh_quota_drift(days=30, now=self.now)

        total = sum(row['count'] for row in report['drifted_state']['rows'])
        self.assertEqual(total, report['drifted_state']['measured_count'])


class DriftListTests(TestCase):
    """清单按沉底天数排，标题截断。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = User.objects.create_user(
            username='list-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='额度分类8')

    def test_the_list_is_ordered_by_how_long_the_listing_has_sunk(self):
        """运营要先去处理搁得最久的，不是刷得最勤的。"""
        for index, (published_ago, refreshed_ago) in enumerate((
            (5, 60), (90, 60), (40, 60),
        )):
            Item.objects.create(
                title=f'商品{index}', description='d', price=Decimal('10.00'),
                status='available', seller=self.seller, category=self.category,
                refresh_count=MAX_REFRESH_PER_MONTH,
                last_refreshed_at=self.now - timedelta(days=refreshed_ago),
                created_at=self.now - timedelta(days=published_ago),
            )

        report = build_refresh_quota_drift(days=30, now=self.now)
        rows = report['drift_list']['rows']

        # age_days 走的是 freshness_age_days——距最近一次把商品顶回前排的天数
        # （刷新优先），三件都刷过，所以都等于 60，而不是它们的发布年龄。
        self.assertEqual([row['age_days'] for row in rows], [60, 60, 60])
        self.assertEqual(report['drift_list']['total_count'], 3)
        self.assertFalse(report['drift_list']['truncated'])

    def test_long_titles_are_truncated(self):
        """清单是给「现在去核实」用的，商品标题不该整段进看板。"""
        Item.objects.create(
            title='超' * 60, description='d', price=Decimal('10.00'),
            status='available', seller=self.seller, category=self.category,
            refresh_count=MAX_REFRESH_PER_MONTH,
            last_refreshed_at=self.now - timedelta(days=60),
        )

        report = build_refresh_quota_drift(days=30, now=self.now)

        self.assertEqual(len(report['drift_list']['rows'][0]['title']), 20)


class ReportOnlyTests(TestCase):
    """本模块只报告，不写任何字段。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = User.objects.create_user(
            username='ro-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='额度分类9')
        self.item = Item.objects.create(
            title='只读商品', description='d', price=Decimal('10.00'),
            status='available', seller=self.seller, category=self.category,
            refresh_count=MAX_REFRESH_PER_MONTH,
            last_refreshed_at=self.now - timedelta(days=60),
        )

    def test_building_the_report_changes_nothing(self):
        before = Item.objects.get(pk=self.item.pk)

        build_refresh_quota_drift(days=30, now=self.now)

        after = Item.objects.get(pk=self.item.pk)
        self.assertEqual(after.refresh_count, before.refresh_count)
        self.assertEqual(after.last_refreshed_at, before.last_refreshed_at)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.updated_at, before.updated_at)

    def test_quota_state_agrees_with_the_live_rule(self):
        """`_quota_state` 必须和 `_refresh_state` 说同一件事。

        这个模块如果自己重算一遍上限，看板上就会出现两个真相，而卖家在自己
        页面上看到的是第三个。判据只允许有一份。
        """
        from .lifecycle_diagnostics import _refresh_state

        for count in (0, 1, MAX_REFRESH_PER_MONTH - 1, MAX_REFRESH_PER_MONTH):
            self.item.refresh_count = count
            self.item.last_refreshed_at = self.now - timedelta(days=60)
            exhausted, _drifted, _cooldown, can_refresh = _quota_state(
                self.item, now=self.now,
            )
            rule_allows, _reason = _refresh_state(self.item, now=self.now)
            self.assertEqual(exhausted, count >= MAX_REFRESH_PER_MONTH)
            # 额度未用尽且最后一次刷新在 60 天前，所以这里卡的不是 24 小时间隔。
            self.assertEqual(can_refresh, rule_allows)


class ExportShapeTests(TestCase):
    """导出键的形状。"""

    def setUp(self):
        self.now = local_noon()
        self.seller = User.objects.create_user(
            username='shape-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='额度分类10')
        Item.objects.create(
            title='形状商品', description='d', price=Decimal('10.00'),
            status='available', seller=self.seller, category=self.category,
            refresh_count=MAX_REFRESH_PER_MONTH,
            last_refreshed_at=self.now - timedelta(days=60),
        )

    def test_the_report_exposes_the_constants_the_panel_quotes(self):
        """面板把常量写进文案，所以常量必须原样出现在返回里。

        否则运营看到的「本月 4 次」和代码里的上限哪天对不上，看板不会知道。
        """
        report = build_refresh_quota_drift(days=30, now=self.now)

        self.assertEqual(report['max_refresh_per_month'], MAX_REFRESH_PER_MONTH)
        self.assertEqual(report['min_refresh_gap_hours'], MIN_REFRESH_GAP_HOURS)
        self.assertEqual(report['quota_month_days'], QUOTA_MONTH_DAYS)
        self.assertEqual(report['stale_days'], STALE_DAYS)
        self.assertEqual(report['min_measured_items'], MIN_MEASURED_ITEMS)

    def test_bucket_upper_bounds_are_ordered_and_last_one_open(self):
        """分档上界必须递增，最后一档必须开放。"""
        limits = [limit for limit, _label in SINCE_REFRESH_BUCKETS]
        finite = [limit for limit in limits if limit is not None]
        self.assertEqual(finite, sorted(finite))
        self.assertIsNone(limits[-1])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        report = build_refresh_quota_drift(days=999, now=self.now)
        self.assertEqual(report['period_days'], 30)

    def test_empty_platform_reports_no_data(self):
        Item.objects.all().delete()
        report = build_refresh_quota_drift(days=30, now=self.now)
        self.assertFalse(report['has_data'])
        self.assertIn('当前没有仍在展示的商品', report['summary'])