# -*- coding: utf-8 -*-
"""积分等级与徽章门槛形状的测试。

这个模块读的三张表都在 ``contributions.py`` 里，而它们各有一个坑。

等级门槛的坑是**第 0 级同时是默认值和第一级**：``CONTRIBUTION_LEVELS`` 的第一项门槛是
0，一个从没有任何记录的用户按 ``_level_for_points`` 也落在这一级。测试必须分开造
「有记录但积分低」和「没有任何记录」两种用户，否则分不清分母收得对不对。

徽章的坑是**两枚看事件总数、五枚看特定类型**：``first_contribution`` 和
``community_builder`` 吃 ``metric == 'events'``，其余五枚吃 ``metric == 'kind'``。一个
用户在某种类型下有一条记录和总共有五条记录，点亮的是不同的徽章。

幂等键的坑是**前缀不是类型**：``order:12:trade_completed:seller`` 的前缀是 ``order``，
而 ``trade_completed`` 只是中间一段。一条写入路径可以写出三种类型，所以前缀分布和类型
分布是两个不同的分布，测试必须分开断言。
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .contributions import (
    CONTRIBUTION_BADGE_RULES, CONTRIBUTION_LEVELS, record_contribution,
)
from .contribution_tier_shape import (
    LEVEL_ZERO, MIN_RANKED_USERS, NOISE_POINTS, build_contribution_tier_shape,
)
from .models import CommunityContribution


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def add_contribution(user, *, kind, points, source_key):
    """造一条贡献记录，source_key 显式传入——它是唯一约束，也是本模块第三节的观测对象。"""
    return record_contribution(
        user=user,
        kind=kind,
        points=points,
        title=f'{kind} 测试记录',
        description='用于测试等级与徽章统计',
        source_key=source_key,
    )


class ContributionTierShapeTests(TestCase):
    def setUp(self):
        self.now = timezone.localtime().replace(hour=23, minute=59, second=0, microsecond=0)

    # ------------------------------------------------------------ 空库与分母

    def test_empty_library_reports_none_not_zero(self):
        """空库时报 None 不报 0，五级全列出来。

        0 会被读成「有，占比很小」，而这里的事实是「分母不存在」。
        """
        result = build_contribution_tier_shape(days=30, now=self.now)

        self.assertFalse(result['has_data'])
        self.assertIn('没有任何贡献记录', result['summary'])
        self.assertEqual(len(result['level_rows']), len(CONTRIBUTION_LEVELS))
        for row in result['level_rows']:
            self.assertEqual(row.user_count, 0)
            self.assertIsNone(row.share)
        for row in result['badge_rows']:
            self.assertEqual(row.earned_count, 0)
            self.assertIsNone(row.share)
        # 四级跨度和六种类型全列出来，只是每一格都是 0。「一条记录都没有」和
        # 「这一级不存在」是两件事，后者要靠空行让读者看见。
        self.assertEqual(len(result['gap_rows']), len(CONTRIBUTION_LEVELS) - 1)
        self.assertTrue(all(g.distance_count == 0 for g in result['gap_rows']))
        self.assertTrue(all(g.mean_distance_label == '—' for g in result['gap_rows']))
        self.assertTrue(all(g.max_distance is None for g in result['gap_rows']))
        self.assertEqual(len(result['type_rows']), len(CommunityContribution.KIND_CHOICES))
        self.assertTrue(all(r.event_count == 0 for r in result['type_rows']))
        self.assertTrue(all(r.point_share is None for r in result['type_rows']))
        self.assertEqual(result['repeat_rows'], [])

    def test_denominator_is_users_with_records_not_all_users(self):
        """分母只收有过记录的用户。

        第 0 级门槛是 0，一个没有任何记录的用户按 ``_level_for_points`` 也落在第 0 级。
        把全体用户当分母，第 0 级的占比会被大量从没参与过的用户撑起来，而那个数说明的是
        产品渗透率，不是等级门槛的形状。
        """
        ranked = make_user('ranked-user')
        make_user('never-contributed-user')
        # 10 分正好踩在第一级门槛上，用户从第 0 级升到第 1 级。造 5 分会留在第 0 级，
        # 但那样测不出「有记录却停在最低级」这个最容易和「从没参与过」混淆的形态。
        add_contribution(
            ranked, kind='trade_completed', points=10, source_key='order:1:trade_completed:seller',
        )

        result = build_contribution_tier_shape(days=30, now=self.now)

        self.assertEqual(result['user_count'], 1)
        zero_row = result['level_rows'][0]
        self.assertEqual(zero_row.threshold, LEVEL_ZERO)
        self.assertEqual(zero_row.user_count, 0)  # 有记录的人已经升走
        # 分母存在（1 个有记录的用户），分子是 0，所以是 0.0 而不是 None。
        self.assertEqual(zero_row.share, 0.0)
        first_row = result['level_rows'][1]
        self.assertEqual(first_row.threshold, 10)
        self.assertEqual(first_row.user_count, 1)
        self.assertEqual(first_row.share, 100.0)

    # ------------------------------------------------------------ 等级分布

    def test_levels_are_mapped_by_the_same_rule_as_the_product_page(self):
        """等级的判定必须和产品页同一套规则。

        本地复制一份 ``_level_for_points`` 的代价是：门槛表将来改一个数，看板会报出一套
        和产品页不一样的分布。这里直接按产品侧同一套规则造数据、按它断言。
        """
        user = make_user('level-user')
        # 五条 10 分的记录 = 50 分，落在第 2 级（30 分）而不是第 3 级（80 分）。
        for index in range(5):
            add_contribution(
                user, kind='trade_completed', points=10,
                source_key=f'order:{index}:trade_completed:seller',
            )

        result = build_contribution_tier_shape(days=30, now=self.now)

        row = next(r for r in result['level_rows'] if r.threshold == 30)
        self.assertEqual(row.user_count, 1)
        self.assertEqual(row.label, '可靠交易伙伴')
        row = next(r for r in result['level_rows'] if r.threshold == 80)
        self.assertEqual(row.user_count, 0)

    def test_gap_distance_uses_next_threshold_minus_current_points(self):
        """卡点距离 = 下一级门槛 − 当前积分，不是跨度本身。

        跨度是 ``CONTRIBUTION_LEVELS`` 相邻两项的差，对停在同一级的人都一样；距离才因
        人而异，才是「这批人离下一级还有多远」。
        """
        user = make_user('gap-user')
        add_contribution(
            user, kind='trade_completed', points=45, source_key='order:1:trade_completed:seller',
        )
        # 另一个人也停在第三级，差 25 分到第四级：同一个距离分组里放两个人，
        # 中位数和最大值才有的可算。
        add_contribution(
            make_user('gap-user-2'), kind='trade_completed', points=55,
            source_key='order:2:trade_completed:seller',
        )

        result = build_contribution_tier_shape(days=30, now=self.now)

        # 45 分和 55 分都落在第三级（门槛 30），前两级一个人都没停。
        gap = result['gap_rows'][0]
        self.assertEqual(gap.label, '新加入校园社区 → 热心参与者')
        self.assertEqual(gap.distance_count, 0)  # 没人停在门槛 0
        self.assertEqual(gap.mean_distance_label, '—')
        self.assertIsNone(gap.max_distance)  # 空样本报 None 不报 0
        gap = result['gap_rows'][1]
        self.assertEqual(gap.label, '热心参与者 → 可靠交易伙伴')
        self.assertEqual(gap.distance_count, 0)  # 没人停在门槛 10
        self.assertEqual(gap.mean_distance_label, '—')
        # 用前缀匹配，不能用 in：第二跨的标签「热心参与者 → 可靠交易伙伴」的下一级名
        # 里也含「可靠交易伙伴」，in 会先命中那一跨。
        gap = next(g for g in result['gap_rows'] if g.label.startswith('可靠交易伙伴 →'))
        self.assertEqual(gap.distance_count, 2)  # 45 分和 55 分都停在这一级
        # 两个人分别差 35 分（80-45）和 25 分（80-55），中位数取中间两个的平均。
        self.assertEqual(gap.mean_distance_label, '30.0 分')
        self.assertEqual(gap.max_distance, 35)
        self.assertEqual(result['user_count'], 2)

    def test_gap_rows_skip_the_top_level(self):
        """最高一级没有下一级，不进卡点一节。

        给它造一行「距离 None」出来，读者会以为还有一个没列出来的等级。
        """
        add_contribution(
            make_user('top-user'), kind='trade_completed', points=200,
            source_key='order:1:trade_completed:seller',
        )

        result = build_contribution_tier_shape(days=30, now=self.now)

        self.assertEqual(len(result['gap_rows']), len(CONTRIBUTION_LEVELS) - 1)
        self.assertTrue(all('循环倡导者 →' not in g.label for g in result['gap_rows']))

    # ------------------------------------------------------------ 徽章

    def test_badges_cover_all_seven_rules(self):
        """七枚全列出来，包括一个人都没拿到的那枚。"""
        result = build_contribution_tier_shape(days=30, now=self.now)

        self.assertEqual(len(result['badge_rows']), len(CONTRIBUTION_BADGE_RULES))
        self.assertEqual(
            [row.code for row in result['badge_rows']],
            [rule['code'] for rule in CONTRIBUTION_BADGE_RULES],
        )

    def test_event_badges_and_kind_badges_are_different_signals(self):
        """看总数的徽章和看类型的徽章点亮条件不同。

        ``community_builder`` 要 5 次事件，``gift_giver`` 要 1 次免费赠送。一个做了
        四笔交易、没送过任何东西的用户，前者差一次、后者一次都没有。
        """
        user = make_user('badge-user')
        for index in range(4):
            add_contribution(
                user, kind='trade_completed', points=10,
                source_key=f'order:{index}:trade_completed:seller',
            )

        result = build_contribution_tier_shape(days=30, now=self.now)

        community = next(r for r in result['badge_rows'] if r.code == 'community_builder')
        self.assertEqual(community.threshold, 5)
        self.assertEqual(community.earned_count, 0)
        gift = next(r for r in result['badge_rows'] if r.code == 'gift_giver')
        self.assertEqual(gift.earned_count, 0)
        first = next(r for r in result['badge_rows'] if r.code == 'first_contribution')
        self.assertEqual(first.earned_count, 1)  # 4 次事件已经够 1 次

    def test_badge_threshold_and_earned_count_sit_on_the_same_row(self):
        """门槛和获取数摆在同一行。

        分开看读不出「这枚徽章是不是太难过」——一个 5 次门槛拿到 4 个人，和一个 1 次
        门槛拿到 4 个人，是两件不同的事。
        """
        for index in range(3):
            user = make_user(f'badge-row-{index}')
            add_contribution(
                user, kind='trade_completed', points=10,
                source_key=f'order:{index}:trade_completed:seller',
            )

        result = build_contribution_tier_shape(days=30, now=self.now)

        row = next(r for r in result['badge_rows'] if r.code == 'community_builder')
        self.assertEqual(row.threshold, 5)
        self.assertEqual(row.earned_count, 0)
        row = next(r for r in result['badge_rows'] if r.code == 'first_contribution')
        self.assertEqual(row.threshold, 1)
        self.assertEqual(row.earned_count, 3)

    # ------------------------------------------------------------ 类型与积分

    def test_type_rows_report_actual_points_not_the_weight_constant(self):
        """单次平均分报实际值，不报代码里的权重常数。

        两者在有补记或改过历史分值时可能不同。报常数等于把「规则」当成「事实」。
        """
        user = make_user('type-user')
        add_contribution(
            user, kind='trade_completed', points=10, source_key='order:1:trade_completed:seller',
        )
        add_contribution(
            user, kind='lost_found_help', points=12, source_key='lost-found-lead:1:accepted',
        )

        result = build_contribution_tier_shape(days=30, now=self.now)

        trade = next(r for r in result['type_rows'] if r.kind == 'trade_completed')
        self.assertEqual(trade.event_count, 1)
        self.assertEqual(trade.points, 10)
        self.assertEqual(trade.mean_points_label, '10.0 分')
        self.assertTrue(trade.is_small_sample)  # 1 条 < 12，应标小样本
        lost = next(r for r in result['type_rows'] if r.kind == 'lost_found_help')
        self.assertEqual(lost.points, 12)
        self.assertEqual(lost.mean_points_label, '12.0 分')
        self.assertTrue(lost.is_small_sample)

    def test_point_share_denominator_is_all_points(self):
        """积分占比的分母是全部积分，不是该类型自己的事件数。"""
        user = make_user('share-user')
        add_contribution(
            user, kind='trade_completed', points=30, source_key='order:1:trade_completed:seller',
        )
        add_contribution(
            user, kind='lost_found_help', points=10, source_key='lost-found-lead:1:accepted',
        )

        result = build_contribution_tier_shape(days=30, now=self.now)

        trade = next(r for r in result['type_rows'] if r.kind == 'trade_completed')
        self.assertEqual(trade.point_share, 75.0)
        lost = next(r for r in result['type_rows'] if r.kind == 'lost_found_help')
        self.assertEqual(lost.point_share, 25.0)

    def test_types_are_listed_even_with_no_records(self):
        """六种类型全列出来，包括一条记录都没有的类型。

        漏掉空类型，读者会以为这套体系里只有出现过的几种行为。
        """
        user = make_user('only-trade')
        add_contribution(
            user, kind='trade_completed', points=10, source_key='order:1:trade_completed:seller',
        )

        result = build_contribution_tier_shape(days=30, now=self.now)

        self.assertEqual(
            len(result['type_rows']), len(CommunityContribution.KIND_CHOICES),
        )

    # ------------------------------------------------------------ 幂等键

    def test_key_prefix_is_not_the_same_distribution_as_kind(self):
        """幂等键前缀和贡献类型是两个不同的分布。

        ``order:12:trade_completed:seller`` 的前缀是 ``order``，而 ``trade_completed``
        只是中间一段。一条写入路径可以写出三种类型，所以两节摆在一起才能看出某条路径是
        不是整条没在跑。
        """
        user = make_user('prefix-user')
        add_contribution(
            user, kind='trade_completed', points=10, source_key='order:1:trade_completed:seller',
        )
        add_contribution(
            user, kind='borrow_returned', points=12, source_key='order:1:borrow_returned:seller',
        )
        add_contribution(
            user, kind='demand_helped', points=8, source_key='demand-response:1:accepted',
        )

        result = build_contribution_tier_shape(days=30, now=self.now)

        prefixes = {row.prefix: row for row in result['key_prefix_rows']}
        self.assertEqual(set(prefixes), {'order', 'demand-response'})
        self.assertEqual(prefixes['order'].event_count, 2)
        self.assertEqual(prefixes['demand-response'].event_count, 1)
        # 类型一节六种全列，所以「出现过哪些类型」要从事件数里读，不能从行数里读。
        active = {row.kind for row in result['type_rows'] if row.event_count}
        self.assertEqual(active, {'trade_completed', 'borrow_returned', 'demand_helped'})

    def test_key_prefix_user_count_counts_distinct_users(self):
        """前缀的用户数去重。

        按 source_key 分组时每组只有一个用户，``Count('user', distinct=True)`` 会恒为 1，
        那个字段就没有信息量——这是本模块第一版真实踩到的坑。
        """
        first = make_user('prefix-a')
        second = make_user('prefix-b')
        add_contribution(
            first, kind='trade_completed', points=10, source_key='order:1:trade_completed:seller',
        )
        add_contribution(
            second, kind='trade_completed', points=10, source_key='order:2:trade_completed:seller',
        )

        result = build_contribution_tier_shape(days=30, now=self.now)

        row = next(r for r in result['key_prefix_rows'] if r.prefix == 'order')
        self.assertEqual(row.event_count, 2)
        self.assertEqual(row.user_count, 2)

    def test_repeat_rows_count_pairs_not_users(self):
        """重复分布数的是「用户 × 类型」组合，不是用户数。

        一个用户可以在六种类型下各出现一次，那是六个组合、一个用户。两个数混用一个
        字段名，同一张表里就会出现两个口径。
        """
        user = make_user('repeat-user')
        add_contribution(
            user, kind='trade_completed', points=10, source_key='order:1:trade_completed:seller',
        )
        add_contribution(
            user, kind='lost_found_help', points=12, source_key='lost-found-lead:1:accepted',
        )
        add_contribution(
            user, kind='trade_completed', points=10, source_key='order:2:trade_completed:seller',
        )

        result = build_contribution_tier_shape(days=30, now=self.now)

        once = next(r for r in result['repeat_rows'] if r.times == 1)
        twice = next(r for r in result['repeat_rows'] if r.times == 2)
        self.assertEqual(once.pair_count, 1)   # lost_found_help 出现一次
        self.assertEqual(twice.pair_count, 1)  # trade_completed 出现两次
        # 分母是有记录的用户数：1 个用户，2 个组合。pair_count 数组合，share 的分母
        # 是用户数，两个数分开报——混用一个字段名，同一张表里就会出现两个口径。
        self.assertEqual(result['user_count'], 1)
        self.assertEqual(once.share, 100.0)
        self.assertEqual(twice.share, 100.0)

    def test_total_records_matches_the_database(self):
        """总记录数和库里对得上，不按周期过滤。"""
        user = make_user('total-user')
        for index in range(3):
            add_contribution(
                user, kind='trade_completed', points=10,
                source_key=f'order:{index}:trade_completed:seller',
            )

        result = build_contribution_tier_shape(days=30, now=self.now)

        self.assertEqual(result['total_records'], 3)
        self.assertEqual(result['total_records'], CommunityContribution.objects.count())

    # ------------------------------------------------------------ 只读与门槛

    def test_module_does_not_write_anything(self):
        """只报告不改写：跑一次统计，库里一条记录都不多。"""
        user = make_user('readonly-user')
        add_contribution(
            user, kind='trade_completed', points=10, source_key='order:1:trade_completed:seller',
        )
        before = CommunityContribution.objects.count()

        build_contribution_tier_shape(days=30, now=self.now)

        self.assertEqual(CommunityContribution.objects.count(), before)

    def test_invalid_days_falls_back_to_the_default_window(self):
        """周期参数不对时回落到默认值，不抛异常。

        本模块其实不按周期过滤任何数据——等级是累计量。这个参数只为和看板其余面板的
        口径字段保持一致而收下。
        """
        user = make_user('days-user')
        add_contribution(
            user, kind='trade_completed', points=10, source_key='order:1:trade_completed:seller',
        )

        result = build_contribution_tier_shape(days=7, now=self.now)
        self.assertEqual(result['period_days'], 7)
        result = build_contribution_tier_shape(days=999, now=self.now)
        self.assertEqual(result['period_days'], 30)

    def test_thresholds_are_exposed_for_the_panel(self):
        """门槛常量必须出现在返回值里，面板才能把它们显示出来。

        一个不给读者看门槛的比例，等于让读者猜这个数是怎么来的。
        """
        result = build_contribution_tier_shape(days=30, now=self.now)

        self.assertEqual(result['min_ranked_users'], MIN_RANKED_USERS)
        self.assertEqual(result['noise_points'], NOISE_POINTS)
        self.assertEqual(result['level_zero'], LEVEL_ZERO)
