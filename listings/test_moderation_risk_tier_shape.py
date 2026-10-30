# -*- coding: utf-8 -*-
"""为「复核分级的形状」钉住口径。

``ModerationEvent.risk_tier`` 配 ``RISK_TIER_REVIEW_TARGETS`` 是站内唯一一对
「写进去了、读者只有一个数」的规则：全站只有审核队列页读它，报的是「此刻超期几条」。
这个模块把三档的规模、结局、相对各自目标的节奏、两套尺子的分歧和规则的参与产出读出
来，只报告不改写。

这些测试要钉住的是九件事：

**分母是窗口内全部记录，按 created_at 切。** pending 也进分母——「还没有结局」是结局
的一种。窗口不按 ``reviewed_at`` 切：分级是拦截那一刻写下的。

**复核节奏按各自的目标归一化，不按绝对时长。** caution 的目标是 48 小时，躺 30 小时
是达标的；同一条 30 小时放到 critical 头上就是超过四倍。绝对时长会把达标读成积压。

**恰好等于目标算「目标内」。** 踩着目标线复核完的那一条是达标的。

**pending 不进任何分布。** 它没有复核时刻，此刻的倍数还在涨，是右删失样本；它单独数
成「当前待复核且已超各自目标」。

**两套尺子的分歧不是 bug 报告。** ``risk_level`` 由分数切，``risk_tier`` 由最高规则
档位决定，两者故意分开；两个方向的不一致各数一条，不给方向。

**规则按「参与产出」计数，不按独占计数。** 一条记录同时命中多条规则时取最高档，它同时
是那几条规则共同的产出，所以 elevated 规则那一行出现 critical 是正常事实。

**全列，含 0 样本。** 三档全列、交叉表 3×3 全列、0 格留着——「高风险里一条严重风险
都没有」是结论，不是表格的缺口。

**空分母报 None，不报 0。** 0 会被读成「有，占比很小」。

**不输出用户名、不读 content 正文。** 报告里不能出现任何一条被拦下来的内容。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from chat_messages.models import ModerationEvent

from .models import Category, Item
from .moderation_risk_tier_shape import (
    MIN_TIER_EVENTS, NOISE_POINTS, RHYTHM_BANDS, RULE_TABLE, TIER_ORDER,
    _band_index, _format_seconds, _target_hours,
    build_moderation_risk_tier_shape,
)


class ModerationRiskTierShapeTestCase(TestCase):
    """公共夹具：一个作者、一件商品、一个复核人。"""

    def setUp(self):
        self.author = User.objects.create_user(
            username='tier-author', password='safe-password-123',
        )
        self.reviewer = User.objects.create_user(
            username='tier-reviewer', password='safe-password-123',
        )
        category, _created = Category.objects.get_or_create(name='分级形状测试分类')
        self.item = Item.objects.create(
            title='分级形状测试商品', description='用于复核分级统计',
            price='10.00', category=category, condition='良好', seller=self.author,
        )

    def make_event(self, *, terms, tier, level='low', score=10,
                   status='pending', reviewed_at=None, created_at=None,
                   channel='comment'):
        """建一条审核记录，再把 created_at 拨到想要的位置。

        ``created_at`` 是 ``auto_now_add=True``，建的时候拨不动，只能建完再 update。
        """
        event = ModerationEvent.objects.create(
            channel=channel, author=self.author, item=self.item,
            content='被拦下来的内容', matched_terms=terms,
            risk_score=score, risk_level=level, risk_tier=tier,
            status=status, reviewed_by=self.reviewer if status != 'pending' else None,
            reviewed_at=reviewed_at,
        )
        if created_at is not None:
            ModerationEvent.objects.filter(pk=event.pk).update(created_at=created_at)
        return event


class EmptyWindowTests(ModerationRiskTierShapeTestCase):
    """空平台：报「没有」，不报一堆 0%。"""

    def test_empty_window_says_so_instead_of_reporting_zero_percentages(self):
        report = build_moderation_risk_tier_shape()
        self.assertFalse(report['has_data'])
        self.assertEqual(report['measured_count'], 0)
        self.assertIn('没有任何审核记录', report['summary'])

    def test_empty_window_reports_none_rather_than_zero(self):
        # 0 会被读成「有，占比很小」，而这里的事实是「分母不存在」。
        report = build_moderation_risk_tier_shape()
        for row in report['tier_rows']:
            self.assertIsNone(row['share'], row['key'])
            self.assertIsNone(row['confirmed_rate'], row['key'])
            self.assertIsNone(row['mean_score'], row['key'])
        for row in report['rhythm_rows']:
            self.assertIsNone(row['within_share'], row['key'])
            self.assertIsNone(row['median_seconds'], row['key'])
            self.assertIsNone(row['p90_seconds'], row['key'])
        for row in report['pending_rows']:
            self.assertIsNone(row['overdue_share'], row['key'])


class TierCompositionTests(ModerationRiskTierShapeTestCase):
    """第一节：三档的规模与结局。"""

    def test_denominator_is_every_event_in_the_window_including_pending(self):
        # pending 也进分母：「还没有结局」是结局的一种，剔掉它每档规模都会少一块。
        now = timezone.now()
        self.make_event(
            terms='高风险交易', tier='critical', level='high', score=50,
            status='confirmed', reviewed_at=now - timedelta(hours=1),
            created_at=now - timedelta(hours=6),
        )
        self.make_event(
            terms='外部链接', tier='caution', status='pending',
            created_at=now - timedelta(hours=2),
        )

        report = build_moderation_risk_tier_shape(now=now)
        self.assertEqual(report['measured_count'], 2)
        rows = {row['key']: row for row in report['tier_rows']}
        self.assertEqual(rows['critical']['count'], 1)
        self.assertEqual(rows['critical']['pending_count'], 0)
        self.assertEqual(rows['caution']['count'], 1)
        self.assertEqual(rows['caution']['pending_count'], 1)
        self.assertEqual(rows['critical']['share'], 50.0)
        self.assertEqual(rows['caution']['share'], 50.0)

    def test_every_tier_is_listed_even_with_zero_events(self):
        # 全列，含 0 样本档：一档一条都没有本身是结论，不是表格的缺口。
        report = build_moderation_risk_tier_shape()
        self.assertEqual([row['key'] for row in report['tier_rows']], list(TIER_ORDER))
        self.assertTrue(all(row['count'] == 0 for row in report['tier_rows']))

    def test_window_cuts_on_created_at_not_reviewed_at(self):
        # 窗口按拦截时刻切。一条上周拦截、这周才复核的记录不进这个窗口——它的拦截
        # 时刻不属于这个周期。这和 moderation_rule_quality 按 reviewed_at 切故意不同。
        now = timezone.now()
        self.make_event(
            terms='高风险交易', tier='critical', level='high', score=50,
            status='confirmed',
            created_at=now - timedelta(days=40),
            reviewed_at=now - timedelta(hours=1),
        )

        report = build_moderation_risk_tier_shape(days=30, now=now)
        self.assertEqual(report['measured_count'], 0)

        wide = build_moderation_risk_tier_shape(days=90, now=now)
        self.assertEqual(wide['measured_count'], 1)

    def test_confirmed_rate_denominator_excludes_pending(self):
        # 确认率的分母是已复核的那部分。把 pending 摊进去，一档的确认率会随积压量
        # 漂移，而那和规则准不准没有关系。
        now = timezone.now()
        for _index in range(3):
            self.make_event(
                terms='高风险交易', tier='critical', level='high', score=50,
                status='confirmed', reviewed_at=now - timedelta(hours=1),
                created_at=now - timedelta(hours=6),
            )
        self.make_event(
            terms='高风险交易', tier='critical', level='high', score=50,
            status='dismissed', reviewed_at=now - timedelta(hours=1),
            created_at=now - timedelta(hours=6),
        )
        self.make_event(
            terms='高风险交易', tier='critical', level='high', score=50,
            status='pending', created_at=now - timedelta(hours=1),
        )

        report = build_moderation_risk_tier_shape(now=now)
        row = {item['key']: item for item in report['tier_rows']}['critical']
        self.assertEqual(row['count'], 5)
        self.assertEqual(row['pending_count'], 1)
        self.assertEqual(row['confirmed_rate'], 75.0)

    def test_group_mean_score_is_reported_per_tier(self):
        now = timezone.now()
        for score in (50, 100):
            self.make_event(
                terms='高风险交易', tier='critical', level='high', score=score,
                status='pending', created_at=now - timedelta(hours=1),
            )
        report = build_moderation_risk_tier_shape(now=now)
        row = {item['key']: item for item in report['tier_rows']}['critical']
        self.assertEqual(row['mean_score'], 75.0)

    def test_each_tier_carries_its_own_review_target_hours(self):
        report = build_moderation_risk_tier_shape()
        rows = {row['key']: row for row in report['tier_rows']}
        self.assertEqual(rows['critical']['target_hours'], 4)
        self.assertEqual(rows['elevated']['target_hours'], 12)
        self.assertEqual(rows['caution']['target_hours'], 48)


class RhythmAgainstOwnTargetTests(ModerationRiskTierShapeTestCase):
    """第二节：复核节奏相对各自的目标，不按绝对时长。"""

    def test_the_same_absolute_wait_reads_differently_per_tier(self):
        # 30 小时对 critical 是超过四倍（目标 4 小时），对 caution 是目标内（目标 48
        # 小时）。绝对时长分档会把达标读成积压，这正是要归一化的原因。
        now = timezone.now()
        self.make_event(
            terms='高风险交易', tier='critical', level='high', score=50,
            status='confirmed', created_at=now - timedelta(hours=31),
            reviewed_at=now - timedelta(hours=1),
        )
        self.make_event(
            terms='外部链接', tier='caution', status='dismissed',
            created_at=now - timedelta(hours=31),
            reviewed_at=now - timedelta(hours=1),
        )

        report = build_moderation_risk_tier_shape(now=now)
        rows = {row['key']: row for row in report['rhythm_rows']}
        self.assertEqual(rows['critical']['bands'][3]['count'], 1)
        self.assertEqual(rows['critical']['bands'][3]['key'], '超过四倍')
        self.assertEqual(rows['critical']['within_count'], 0)
        self.assertEqual(rows['caution']['bands'][0]['count'], 1)
        self.assertEqual(rows['caution']['within_share'], 100.0)

    def test_exactly_at_target_counts_as_within_target(self):
        # 踩着目标线复核完的那一条是达标的，不能推到「一到两倍」。
        now = timezone.now()
        self.make_event(
            terms='高风险交易', tier='critical', level='high', score=50,
            status='confirmed', created_at=now - timedelta(hours=5),
            reviewed_at=now - timedelta(hours=1),
        )
        report = build_moderation_risk_tier_shape(now=now)
        row = {item['key']: item for item in report['rhythm_rows']}['critical']
        self.assertEqual(row['bands'][0]['count'], 1)
        self.assertEqual(row['within_share'], 100.0)
        self.assertEqual(row['median_seconds'], 4 * 3600)

    def test_rhythm_bands_are_mutually_exclusive_and_fully_listed(self):
        report = build_moderation_risk_tier_shape()
        for row in report['rhythm_rows']:
            self.assertEqual(
                [band['key'] for band in row['bands']],
                ['目标内', '一到两倍', '两到四倍', '超过四倍'],
            )

    def test_band_boundaries_put_the_edge_in_the_lower_band(self):
        self.assertEqual(_band_index(1.0, RHYTHM_BANDS), 0)
        self.assertEqual(_band_index(1.5, RHYTHM_BANDS), 1)
        self.assertEqual(_band_index(2.0, RHYTHM_BANDS), 1)
        self.assertEqual(_band_index(3.9, RHYTHM_BANDS), 2)
        self.assertEqual(_band_index(4.0, RHYTHM_BANDS), 2)
        self.assertEqual(_band_index(40.0, RHYTHM_BANDS), 3)

    def test_pending_events_stay_out_of_the_rhythm_distribution(self):
        # pending 没有复核时刻，它此刻的倍数还在涨，报进任何一档都是一个会自己变的数。
        now = timezone.now()
        self.make_event(
            terms='高风险交易', tier='critical', level='high', score=50,
            status='pending', created_at=now - timedelta(days=30),
        )
        report = build_moderation_risk_tier_shape(now=now)
        row = {item['key']: item for item in report['rhythm_rows']}['critical']
        self.assertEqual(row['reviewed_count'], 0)
        self.assertEqual(sum(band['count'] for band in row['bands']), 0)
        self.assertIsNone(row['within_share'])

    def test_p90_is_reported_next_to_the_median(self):
        # 五条一小时复核完、五条拖到八小时（目标 4 小时的两倍）。中位数落在快的那一半，
        # P90 落在慢的那一半——两个数分开报，才能看出「一半快一半慢」和「普遍慢」的
        # 区别，而那正是中位数和 P90 要在同一行出现的原因。
        now = timezone.now()
        for index in range(5):
            self.make_event(
                terms='高风险交易', tier='critical', level='high', score=50,
                status='confirmed', created_at=now - timedelta(hours=2),
                reviewed_at=now - timedelta(hours=1),
            )
            self.make_event(
                terms='高风险交易', tier='critical', level='high', score=50,
                status='confirmed', created_at=now - timedelta(hours=9),
                reviewed_at=now - timedelta(hours=1),
            )
        report = build_moderation_risk_tier_shape(now=now)
        row = {item['key']: item for item in report['rhythm_rows']}['critical']
        self.assertEqual(row['median_seconds'], 3600)
        self.assertEqual(row['p90_seconds'], 8 * 3600)
        self.assertEqual(row['within_share'], 50.0)


class PendingBacklogTests(ModerationRiskTierShapeTestCase):
    """第二节末尾：当前待复核且已超过各自目标的那一批。"""

    def test_overdue_is_measured_against_each_tiers_own_target(self):
        # 同一条 30 小时：对 caution 还没超期（目标 48 小时），对 critical 早就超了。
        now = timezone.now()
        self.make_event(
            terms='高风险交易', tier='critical', level='high', score=50,
            status='pending', created_at=now - timedelta(hours=30),
        )
        self.make_event(
            terms='外部链接', tier='caution', status='pending',
            created_at=now - timedelta(hours=30),
        )

        report = build_moderation_risk_tier_shape(now=now)
        rows = {row['key']: row for row in report['pending_rows']}
        self.assertEqual(rows['critical']['pending_count'], 1)
        self.assertEqual(rows['critical']['overdue_count'], 1)
        self.assertEqual(rows['caution']['pending_count'], 1)
        self.assertEqual(rows['caution']['overdue_count'], 0)
        self.assertEqual(report['overdue_total'], 1)
        self.assertEqual(report['pending_total'], 2)

    def test_reviewed_events_are_not_counted_as_pending_backlog(self):
        now = timezone.now()
        self.make_event(
            terms='高风险交易', tier='critical', level='high', score=50,
            status='confirmed', created_at=now - timedelta(hours=40),
            reviewed_at=now - timedelta(hours=1),
        )
        report = build_moderation_risk_tier_shape(now=now)
        rows = {row['key']: row for row in report['pending_rows']}
        self.assertEqual(rows['critical']['pending_count'], 0)
        self.assertEqual(rows['critical']['overdue_count'], 0)
        self.assertEqual(report['overdue_total'], 0)

    def test_pending_outside_the_window_is_not_counted(self):
        # 这一节的分母和第一节一样是「窗口内拦截的记录」。窗口之前拦截、现在还挂着
        # 的，拦截时刻不属于这个周期。
        now = timezone.now()
        self.make_event(
            terms='高风险交易', tier='critical', level='high', score=50,
            status='pending', created_at=now - timedelta(days=40),
        )
        report = build_moderation_risk_tier_shape(days=30, now=now)
        self.assertEqual(report['pending_total'], 0)

    def test_longest_wait_is_reported_so_the_backlog_has_a_scale(self):
        now = timezone.now()
        self.make_event(
            terms='外部链接', tier='caution', status='pending',
            created_at=now - timedelta(hours=5),
        )
        self.make_event(
            terms='外部链接', tier='caution', status='pending',
            created_at=now - timedelta(hours=50),
        )
        report = build_moderation_risk_tier_shape(now=now)
        row = {item['key']: item for item in report['pending_rows']}['caution']
        self.assertEqual(row['longest_wait_label'], '2 天')


class TwoScalesCrossTableTests(ModerationRiskTierShapeTestCase):
    """第三节：两套尺子的交叉表与分歧点。"""

    def test_cross_table_is_always_three_by_three_including_empty_cells(self):
        now = timezone.now()
        self.make_event(
            terms='高风险交易', tier='critical', level='high', score=50,
            status='pending', created_at=now - timedelta(hours=1),
        )
        report = build_moderation_risk_tier_shape(now=now)
        cross = report['cross']
        self.assertEqual(len(cross['columns']), 3)
        self.assertEqual(len(cross['rows']), 3)
        self.assertEqual(cross['grand_total'], 1)
        cells = {
            (row['key'], column): value
            for row in cross['rows'] for column, value in zip(TIER_ORDER, row['cells'])
        }
        self.assertEqual(cells[('high', 'critical')], 1)
        self.assertEqual(cells[('high', 'elevated')], 0)
        self.assertEqual(cells[('low', 'caution')], 0)
        self.assertEqual(cross['column_totals'], [1, 0, 0])

    def test_a_high_score_can_sit_below_the_critical_tier(self):
        # 两条中档规则叠加把分数推过 60，档位仍停在 elevated。这是设计，不是 bug：
        # moderation.py 的注释写着 risk_tier 是复核节奏、risk_score 是可解释加权和。
        now = timezone.now()
        self.make_event(
            terms='微信导流、手机号', tier='elevated', level='high', score=75,
            status='pending', created_at=now - timedelta(hours=1),
        )
        report = build_moderation_risk_tier_shape(now=now)
        rows = {row['key']: row for row in report['mismatch_rows']}
        self.assertEqual(rows['level_high_tier_not_critical']['count'], 1)
        self.assertEqual(rows['tier_critical_level_not_high']['count'], 0)
        self.assertEqual(rows['aligned']['count'], 0)
        self.assertTrue(rows['level_high_tier_not_critical']['is_mismatch'])
        self.assertFalse(rows['aligned']['is_mismatch'])

    def test_a_critical_tier_can_sit_below_the_high_score_line(self):
        # 单条高风险交易 50 分，够不着 60 的高风险线，档位却是 critical。
        now = timezone.now()
        self.make_event(
            terms='高风险交易', tier='critical', level='medium', score=50,
            status='pending', created_at=now - timedelta(hours=1),
        )
        report = build_moderation_risk_tier_shape(now=now)
        rows = {row['key']: row for row in report['mismatch_rows']}
        self.assertEqual(rows['tier_critical_level_not_high']['count'], 1)
        self.assertEqual(rows['level_high_tier_not_critical']['count'], 0)

    def test_mismatch_rows_carry_no_direction_or_verdict(self):
        # 这一节只报「分开得有多远」，不标红、不建议改任何一边。
        now = timezone.now()
        self.make_event(
            terms='微信导流、手机号', tier='elevated', level='high', score=75,
            status='pending', created_at=now - timedelta(hours=1),
        )
        report = build_moderation_risk_tier_shape(now=now)
        serialised = repr(report['mismatch_rows'])
        for banned in ('bug', '错误', '应该', '建议', '修正'):
            self.assertNotIn(banned, serialised)


class RuleTierContributionTests(ModerationRiskTierShapeTestCase):
    """第四节：每条规则参与产出的事件落在哪一档。"""

    def test_every_rule_is_listed_even_when_it_never_hit(self):
        report = build_moderation_risk_tier_shape()
        names = [row['rule_name'] for row in report['rule_rows']]
        self.assertEqual(names, [name for name, _w, _t in RULE_TABLE])
        for row in report['rule_rows']:
            self.assertEqual(row['hit_count'], 0)
            self.assertEqual(
                sum(item['count'] for item in row['tier_counts']), 0,
            )

    def test_a_record_with_two_rules_counts_in_both(self):
        # 按「参与」计数：一条记录同时是那两条规则共同的产出。各行相加大于总数，
        # 这是故意的。
        now = timezone.now()
        self.make_event(
            terms='微信导流、手机号', tier='elevated', level='medium', score=75,
            status='pending', created_at=now - timedelta(hours=1),
        )
        report = build_moderation_risk_tier_shape(now=now)
        rows = {row['rule_name']: row for row in report['rule_rows']}
        self.assertEqual(rows['微信导流']['hit_count'], 1)
        self.assertEqual(rows['手机号']['hit_count'], 1)
        self.assertEqual(rows['外部链接']['hit_count'], 0)
        self.assertEqual(report['measured_count'], 1)

    def test_an_elevated_rule_can_produce_a_critical_event(self):
        # 取最高档意味着「高风险交易」把一条同时命中它的记录抬到 critical，而微信导流
        # 自己也参与了那条记录。这正是第四节要让它看见的事实。
        now = timezone.now()
        self.make_event(
            terms='微信导流、高风险交易', tier='critical', level='high', score=90,
            status='pending', created_at=now - timedelta(hours=1),
        )
        report = build_moderation_risk_tier_shape(now=now)
        rows = {row['rule_name']: row for row in report['rule_rows']}
        self.assertEqual(rows['微信导流']['own_tier'], 'elevated')
        self.assertEqual(rows['微信导流']['tier_counts'][0]['count'], 1)
        self.assertEqual(rows['微信导流']['tier_counts'][0]['key'], 'critical')
        self.assertEqual(rows['高风险交易']['tier_counts'][0]['count'], 1)

    def test_rules_are_ordered_by_own_tier_then_hit_count(self):
        now = timezone.now()
        for _index in range(3):
            self.make_event(
                terms='外部链接', tier='caution', status='pending',
                created_at=now - timedelta(hours=1),
            )
        self.make_event(
            terms='手机号', tier='elevated', level='medium', score=35,
            status='pending', created_at=now - timedelta(hours=1),
        )
        report = build_moderation_risk_tier_shape(now=now)
        # 自带档位优先于命中次数：这一节要读的是「最急的那条规则产出过什么」，
        # 所以一条从没命中过的 critical 规则仍然排在一条命中三次的 caution 规则前面。
        self.assertEqual(
            [row['rule_name'] for row in report['rule_rows']],
            ['高风险交易', '手机号', '微信导流', '外部链接'],
        )

    def test_rule_tiers_match_the_moderation_module(self):
        # 权重与档位是 _RULES 里的常量，这里只把运营要对照的那两列摆出来。
        report = build_moderation_risk_tier_shape()
        rows = {row['rule_name']: row for row in report['rule_rows']}
        for name, weight, tier in RULE_TABLE:
            self.assertEqual(rows[name]['weight'], weight)
            self.assertEqual(rows[name]['own_tier'], tier)


class ReportingBoundaryTests(ModerationRiskTierShapeTestCase):
    """边界：不输出什么、样本门槛、未知取值。"""

    def test_report_never_leaks_usernames_or_content(self):
        now = timezone.now()
        self.make_event(
            terms='微信导流', tier='elevated', level='medium', score=40,
            status='confirmed', created_at=now - timedelta(hours=6),
            reviewed_at=now - timedelta(hours=1),
        )
        report = build_moderation_risk_tier_shape(now=now)
        serialised = repr(report)
        self.assertNotIn('tier-author', serialised)
        self.assertNotIn('tier-reviewer', serialised)
        self.assertNotIn('被拦下来的内容', serialised)
        self.assertNotIn('微信导流、手机号', serialised)

    def test_small_tier_is_flagged_below_the_threshold(self):
        now = timezone.now()
        for index in range(MIN_TIER_EVENTS - 1):
            self.make_event(
                terms='外部链接', tier='caution', status='pending',
                created_at=now - timedelta(hours=1),
            )
        report = build_moderation_risk_tier_shape(now=now)
        row = {item['key']: item for item in report['tier_rows']}['caution']
        self.assertTrue(row['is_small_sample'])
        self.assertIn('样本', report['summary'])

    def test_unknown_tier_is_reported_separately_not_folded_in(self):
        # 档位是带 choices 的字段，历史数据或直接写库可能落在三档之外。单独一行，
        # 不静默并进任何一档。
        now = timezone.now()
        self.make_event(
            terms='外部链接', tier='legacy_tier', status='pending',
            created_at=now - timedelta(hours=1),
        )
        report = build_moderation_risk_tier_shape(now=now)
        keys = [row['key'] for row in report['tier_rows']]
        self.assertEqual(keys[:3], list(TIER_ORDER))
        other = report['tier_rows'][-1]
        self.assertTrue(other['is_other'])
        self.assertEqual(other['count'], 1)
        self.assertIsNone(other['target_hours'])

    def test_unknown_tier_stays_out_of_the_rhythm_distribution(self):
        # 没有目标的档位除不出倍数来，不进任何一档。
        now = timezone.now()
        self.make_event(
            terms='外部链接', tier='legacy_tier', status='confirmed',
            created_at=now - timedelta(hours=6),
            reviewed_at=now - timedelta(hours=1),
        )
        report = build_moderation_risk_tier_shape(now=now)
        for row in report['rhythm_rows']:
            self.assertEqual(row['reviewed_count'], 0)

    def test_period_days_outside_the_choices_falls_back_to_the_default(self):
        # 非法周期不报错也不静默按原值算，回落到默认值，和其余面板一个行为。
        self.assertEqual(build_moderation_risk_tier_shape(days=3)['days'], 30)
        self.assertEqual(build_moderation_risk_tier_shape(days='abc')['days'], 30)
        self.assertEqual(build_moderation_risk_tier_shape(days=90)['days'], 90)

    def test_report_carries_the_noise_and_sample_thresholds_it_used(self):
        report = build_moderation_risk_tier_shape()
        self.assertEqual(report['min_tier_events'], MIN_TIER_EVENTS)
        self.assertEqual(report['noise_points'], NOISE_POINTS)
        self.assertEqual(
            report['rhythm_bands'],
            ['目标内', '一到两倍', '两到四倍', '超过四倍'],
        )

    def test_target_hours_come_from_the_model_constant(self):
        self.assertEqual(_target_hours('critical'), 4)
        self.assertEqual(_target_hours('elevated'), 12)
        self.assertEqual(_target_hours('caution'), 48)
        self.assertIsNone(_target_hours('legacy_tier'))

    def test_elapsed_seconds_are_formatted_for_humans(self):
        self.assertEqual(_format_seconds(None), '—')
        self.assertEqual(_format_seconds(30), '30 秒')
        self.assertEqual(_format_seconds(600), '10 分钟')
        self.assertEqual(_format_seconds(4 * 3600), '4 小时')
        self.assertEqual(_format_seconds(50 * 3600), '2 天')
