# -*- coding: utf-8 -*-
"""为「内容审核规则的误判与漏判」钉住口径。

`ModerationEvent` 的 `risk_score`、`matched_terms`、`reviewed_at` 一直在写，读路径
只有排序和筛选。这个模块把它们读出来，只报告不改写。这些测试要钉住的是：

1. 只统计已经复核完的记录，`pending` 不计入任何分布，但单独数出来。
2. 按规则拆开，一条同时命中多条规则的记录会出现在多个规则组里。
3. 误判率和漏判率分开报，不合成一个「准确率」。
4. 一条规则的样本不足 8 条时不给方向，误判率不满 15 个百分点不标黄。
5. 权重只报分布，不重新拟合——分数反推不出权重组合。
6. 漏判清单只收「确认违规且没命中高风险交易」的记录。
7. 复核时长按分钟和小时分档，负值被夹到零。
8. 窗口按 `reviewed_at` 取，不按 `created_at`。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from chat_messages.models import ModerationEvent
from .moderation_rule_quality import (
    FALSE_NEGATIVE_POINTS, FALSE_POSITIVE_POINTS, MIN_MEASURED_EVENTS,
    MIN_RULE_EVENTS, REVIEW_LIST_LIMIT, RULE_WEIGHTS, SCORE_BUCKETS,
    build_moderation_rule_quality,
)
from .models import Category, Item


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class ModerationRuleQualityTests(TestCase):
    def setUp(self):
        self.author = make_user('rule-author')
        self.category = Category.objects.create(name='规则质量测试分类')
        self.item = Item.objects.create(
            title='规则质量测试商品', description='用于规则质量统计',
            price='10.00', category=self.category, condition='良好',
            seller=make_user('rule-seller'),
        )
        self._seq = 0

    def make_event(
        self, *, rules=('外部链接',), status='dismissed', review_seconds=600,
        reviewed_days_ago=1, risk_score=None, risk_level=None,
        channel='comment', block_days_ago=3,
    ):
        """建一条审核记录，复核间隔按秒给，可以指定命中的规则和复核结果。"""
        self._seq += 1
        if risk_score is None:
            risk_score = min(
                sum(RULE_WEIGHTS.get(rule, 0) for rule in rules), 100,
            )
        if risk_level is None:
            risk_level = 'high' if risk_score >= 60 else (
                'medium' if risk_score >= 30 else 'low'
            )
        reviewed_at = timezone.now() - timedelta(days=reviewed_days_ago)
        created_at = reviewed_at - timedelta(seconds=review_seconds)
        # created_at 是 auto_now_add，不能直接在 create 里给，只能建完再改。
        row = ModerationEvent.objects.create(
            channel=channel,
            author=self.author,
            item=self.item,
            content=f'被拦截内容 {self._seq}',
            matched_terms='、'.join(rules),
            risk_score=risk_score,
            risk_level=risk_level,
            status=status,
            reviewed_by=make_user(f'rule-reviewer-{self._seq}'),
            reviewed_at=reviewed_at,
        )
        row.created_at = created_at - timedelta(days=block_days_ago)
        row.save(update_fields=['created_at'])
        return row

    def test_only_reviewed_events_are_measured(self):
        self.make_event(status='confirmed')
        self.make_event(status='dismissed')
        # pending 没有复核时刻，不该进任何分布。
        ModerationEvent.objects.create(
            channel='comment', author=self.author, item=self.item,
            content='还没复核', matched_terms='外部链接',
        )

        report = build_moderation_rule_quality(days=30)
        self.assertEqual(report['stats']['measured_count'], 2)
        # pending 单独数出来，不丢也不混。
        self.assertEqual(report['pending_count'], 1)

    def test_a_multi_rule_event_counts_in_every_rule_group(self):
        # 一条同时命中三条规则的记录，该在三个规则组里都算一条——要回答的是「微信导流
        # 这条规则准不准」，所有命中它的记录都是它的样本。
        self.make_event(rules=('微信导流', '手机号', '高风险交易'))
        report = build_moderation_rule_quality(days=30)
        by_name = {row['rule_name']: row for row in report['rule_rows']}
        self.assertEqual(by_name['微信导流']['hit_count'], 1)
        self.assertEqual(by_name['手机号']['hit_count'], 1)
        self.assertEqual(by_name['高风险交易']['hit_count'], 1)

    def test_false_positive_rate_is_dismissed_over_reviewed(self):
        for index in range(6):
            self.make_event(rules=('外部链接',), status='dismissed')
        for index in range(4):
            self.make_event(rules=('外部链接',), status='confirmed')

        report = build_moderation_rule_quality(days=30)
        by_name = {row['rule_name']: row for row in report['rule_rows']}
        row = by_name['外部链接']
        self.assertEqual(row['hit_count'], 10)
        self.assertEqual(row['dismissed_count'], 6)
        self.assertEqual(row['false_positive_rate'], 60)

    def test_a_rule_with_too_few_samples_is_not_flagged(self):
        # 一条规则只命中 7 条，误判率 100% 也不标黄：几条样本上的百分比不是方向。
        for index in range(MIN_RULE_EVENTS - 1):
            self.make_event(rules=('外部链接',), status='dismissed')
        report = build_moderation_rule_quality(days=30)
        by_name = {row['rule_name']: row for row in report['rule_rows']}
        row = by_name['外部链接']
        self.assertEqual(row['false_positive_rate'], 100)
        self.assertTrue(row['is_small_sample'])
        self.assertFalse(row['is_flagged'])

    def test_a_rule_with_a_high_false_positive_rate_is_flagged(self):
        for index in range(8):
            self.make_event(rules=('外部链接',), status='dismissed')
        report = build_moderation_rule_quality(days=30)
        by_name = {row['rule_name']: row for row in report['rule_rows']}
        self.assertTrue(by_name['外部链接']['is_flagged'])

    def test_false_positive_rate_below_the_threshold_is_not_flagged(self):
        # 误判率 25% 高过 15 个百分点的门槛，会被标黄。
        for index in range(2):
            self.make_event(rules=('外部链接',), status='dismissed')
        for index in range(6):
            self.make_event(rules=('外部链接',), status='confirmed')
        report = build_moderation_rule_quality(days=30)
        by_name = {row['rule_name']: row for row in report['rule_rows']}
        self.assertEqual(by_name['外部链接']['false_positive_rate'], 25)
        self.assertTrue(by_name['外部链接']['is_flagged'])

    def test_false_negative_rate_has_its_own_denominator(self):
        # 误判率和漏判率的分母不一样：前者是本规则命中的那批，后者是本规则没命中的
        # 那批。合成一个「准确率」会把两个问题压成一个说不清的数。
        for index in range(4):
            self.make_event(rules=('外部链接',), status='dismissed')
        for index in range(6):
            self.make_event(rules=('高风险交易',), status='confirmed')

        report = build_moderation_rule_quality(days=30)
        by_name = {row['rule_name']: row for row in report['rule_rows']}
        external = by_name['外部链接']
        risky = by_name['高风险交易']
        # 外部链接命中了 4 条，全是误判。
        self.assertEqual(external['false_positive_rate'], 100)
        # 它没命中的 6 条里，全部确认违规——漏判率 100。
        self.assertEqual(external['missed_count'], 6)
        self.assertEqual(external['false_negative_rate'], 100)
        # 高风险交易命中的 6 条全部确认违规，误判率 0；它没命中的 4 条全是误判，
        # 确认违规的是 0 条。
        self.assertEqual(risky['false_positive_rate'], 0)
        self.assertEqual(risky['missed_count'], 0)
        self.assertEqual(risky['false_negative_rate'], 0)

    def test_a_rule_that_never_fired_is_reported_as_such(self):
        self.make_event(rules=('外部链接',))
        report = build_moderation_rule_quality(days=30)
        by_name = {row['rule_name']: row for row in report['rule_rows']}
        never = by_name['手机号']
        self.assertTrue(never['never_hit'])
        self.assertEqual(never['hit_count'], 0)
        # 没命中过的规则不参与漏判计算：它的漏判率无从谈起。
        self.assertEqual(never['missed_count'], 0)

    def test_score_buckets_answer_whether_high_scores_confirm_more(self):
        # 分数只当参照：它回答「高分的档是不是真的更容易确认违规」。
        for index in range(4):
            self.make_event(rules=('高风险交易', '手机号'), status='confirmed')
        for index in range(4):
            self.make_event(rules=('外部链接',), status='dismissed')

        report = build_moderation_rule_quality(days=30)
        by_label = {row['label']: row for row in report['score_rows']}
        high = by_label['六十分档（60 及以上）']
        low = by_label['低分档（0-19）']
        self.assertEqual(high['confirmed_rate'], 100)
        self.assertEqual(low['confirmed_rate'], 0)

    def test_missed_list_only_covers_confirmed_without_the_risky_rule(self):
        # 确认违规、且没命中高风险交易——真正该拦的东西没有对应的规则。
        self.make_event(rules=('外部链接',), status='confirmed')
        self.make_event(rules=('外部链接',), status='dismissed')
        self.make_event(rules=('高风险交易', '手机号'), status='confirmed')

        report = build_moderation_rule_quality(days=30)
        self.assertEqual(report['missed_row_total'], 1)
        row = report['missed_rows'][0]
        self.assertEqual(row['rules'], '外部链接')
        self.assertEqual(row['risk_level_label'], '低风险')

    def test_channels_are_reported_separately(self):
        self.make_event(channel='comment')
        self.make_event(channel='private_message')
        self.make_event(channel='private_message')

        report = build_moderation_rule_quality(days=30)
        by_channel = {row['channel']: row for row in report['channel_rows']}
        self.assertEqual(by_channel['comment']['count'], 1)
        self.assertEqual(by_channel['private_message']['count'], 2)
        self.assertEqual(by_channel['comment']['channel_label'], '商品留言')
        self.assertEqual(by_channel['private_message']['channel_label'], '私信')

    def test_review_buckets_are_mutually_exclusive_and_clamped_at_zero(self):
        # 五条记录的复核间隔横跨四个档，created_at 都要落在 30 天窗口内，
        # 否则统计不到，分档就少算。
        self.make_event(review_seconds=300, block_days_ago=0)
        self.make_event(review_seconds=1200, block_days_ago=0)
        self.make_event(review_seconds=7200, block_days_ago=0)
        self.make_event(review_seconds=2 * 86400, block_days_ago=0)
        # 负值（复核早于拦截）被夹到零，归到最短的一档。
        self.make_event(review_seconds=-600, block_days_ago=0)

        report = build_moderation_rule_quality(days=30)
        buckets = {row['label']: row['count'] for row in report['stats']['rows']}
        self.assertEqual(buckets['十分钟内'], 2)
        self.assertEqual(buckets['十分钟到一小时'], 1)
        self.assertEqual(buckets['一小时到一天'], 1)
        self.assertEqual(buckets['超过一天'], 1)
        self.assertEqual(
            sum(row['count'] for row in report['stats']['rows']),
            report['stats']['measured_count'],
        )

    def test_window_follows_reviewed_at_not_created_at(self):
        # 一条 45 天前拦截、20 天后才复核的记录，reviewed_at 落在本周期里，该被收进来。
        # 按 created_at 取窗口会把它整条漏掉——而它验的正是窗口内的那批规则命中。
        self.make_event(review_seconds=20 * 86400, block_days_ago=45)
        # 两条 reviewed_at 早于窗口起点的记录，不该进来。
        self.make_event(review_seconds=3600, reviewed_days_ago=60)
        self.make_event(review_seconds=3600, reviewed_days_ago=90)

        report = build_moderation_rule_quality(days=30)
        self.assertEqual(report['stats']['measured_count'], 1)

    def test_empty_report_is_explicit(self):
        report = build_moderation_rule_quality(days=30)
        self.assertFalse(report['has_data'])
        self.assertEqual(report['stats']['measured_count'], 0)
        self.assertEqual(report['stats']['median_label'], '—')
        self.assertEqual(report['score_rows'][0]['count'], 0)
        self.assertTrue(report['stats']['is_small_sample'])
        # 空态下规则清单照样报出来：一条都没命中的规则也是事实。
        self.assertEqual(len(report['rule_rows']), len(RULE_WEIGHTS))
        self.assertTrue(all(row['never_hit'] for row in report['rule_rows']))

    def test_small_sample_is_flagged(self):
        for index in range(MIN_MEASURED_EVENTS - 1):
            self.make_event()
        report = build_moderation_rule_quality(days=30)
        self.assertTrue(report['stats']['is_small_sample'])
        self.assertEqual(report['min_measured_events'], MIN_MEASURED_EVENTS)
        self.assertEqual(report['min_rule_events'], MIN_RULE_EVENTS)

    def test_weights_are_reported_for_comparison(self):
        # 权重是 `_RULES` 里的常量，模块不复制它的正则，只把运营要对照的那个数摆出来。
        self.make_event(rules=('外部链接',))
        report = build_moderation_rule_quality(days=30)
        by_name = {row['rule_name']: row for row in report['rule_rows']}
        self.assertEqual(by_name['外部链接']['weight'], 20)
        self.assertEqual(by_name['高风险交易']['weight'], 50)

    def test_row_limits_are_reported(self):
        for index in range(REVIEW_LIST_LIMIT + 5):
            self.make_event(rules=('外部链接',), status='confirmed')
        report = build_moderation_rule_quality(days=30)
        self.assertEqual(len(report['missed_rows']), REVIEW_LIST_LIMIT)
        self.assertGreater(report['missed_row_total'], REVIEW_LIST_LIMIT)
        self.assertLessEqual(
            len(report['rule_rows']), report['rule_row_limit'],
        )

    def test_period_choices_are_the_only_accepted_windows(self):
        for days in (7, 30, 90, 365):
            report = build_moderation_rule_quality(days=days)
            self.assertEqual(report['period_days'], days)
        # 不认识的窗口退回 30 天，而不是当 0 天用。
        report = build_moderation_rule_quality(days=3)
        self.assertEqual(report['period_days'], 30)

    def test_thresholds_are_reported_for_the_panel(self):
        report = build_moderation_rule_quality(days=30)
        self.assertEqual(
            report['false_positive_points'], FALSE_POSITIVE_POINTS,
        )
        self.assertEqual(
            report['false_negative_points'], FALSE_NEGATIVE_POINTS,
        )
        self.assertEqual(len(report['score_rows']), len(SCORE_BUCKETS))
