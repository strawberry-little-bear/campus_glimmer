# -*- coding: utf-8 -*-
"""Keep the demand-match-quality aggregates honest about what they count.

The builders behind this panel were collapsed from a handful of counts each
into one conditional aggregate, which is the kind of change that is easy to
get subtly wrong: a filter moved inside ``Count(..., filter=...)`` can quietly
drop a row, and a total reused from a different scope can quietly double one.
Nothing in the rendered panel would look broken.

So these tests do not assert the numbers a second time. They rebuild every
reported figure from a plain ``values_list`` of the decided rows and pure
Python -- no aggregation at all -- and require the two to agree. If a future
refactor moves a filter, the oracle disagrees and the test fails with the two
numbers side by side.

The budget tests then pin the query count so the collapse cannot silently
un-collapse.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .demand_match_quality import (
    HIGH_SCORE_FLOOR,
    MIN_BAND_RESPONSES,
    SCORE_BANDS,
    _band_of,
    _percent,
    _period_start,
    build_demand_match_quality,
)
from .models import Category, CampusLocation, DemandPost, DemandResponse, Item


def local_noon():
    """Local noon, so the reference moment stays far from local midnight."""
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def mean(values):
    return round(sum(values) / len(values), 1) if values else None


class DemandMatchQualityOracleTests(TestCase):
    """Every reported number, recomputed in Python from the same rows."""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('oracle-seller')
        self.requester = make_user('oracle-requester')
        self.category = Category.objects.create(name='Oracle 分类')
        self.location = CampusLocation.objects.create(name='Oracle 地点')

    def respond(self, score, status, reason, days_ago=1):
        demand = DemandPost.objects.create(
            requester=self.requester, title=f'Oracle 求购 {score}',
            description='用于查询收敛比对', category=self.category,
            location=self.location,
        )
        item = Item.objects.create(
            title=f'Oracle 商品 {score}-{status}-{days_ago}',
            description='用于查询收敛比对', price=Decimal('50.00'),
            status='available', seller=self.seller, category=self.category,
            location=self.location,
        )
        response = DemandResponse.objects.create(
            demand=demand, item=item, responder=self.seller, status=status,
            match_score=score, match_reason=reason,
        )
        DemandResponse.objects.filter(pk=response.pk).update(
            created_at=self.now - timedelta(days=days_ago),
            updated_at=self.now - timedelta(days=days_ago),
        )
        return response

    def seed_a_mixed_window(self):
        """A window that exercises every branch the aggregates have to cover.

        Deliberately includes a blank reason, a score below the lowest band and
        one above the highest: the "without a dimension" cell has to account
        for blank reasons, and the band lookup has to route out-of-range
        scores somewhere predictable rather than dropping them.
        """
        self.respond(2, 'accepted', '分类一致')
        self.respond(2, 'rejected', '分类一致、地点一致')
        self.respond(4, 'accepted', '分类一致、价格符合预算')
        self.respond(5, 'accepted', '分类一致、关键词相近（键盘）')
        self.respond(5, 'rejected', '')
        self.respond(6, 'accepted', '分类一致、地点一致、价格符合预算')
        self.respond(7, 'rejected', '分类一致、地点一致')
        self.respond(7, 'rejected', '')
        self.respond(8, 'accepted', '分类一致、地点一致、关键词相近（键盘）')
        self.respond(9, 'accepted', '分类一致、地点一致、价格符合预算、关键词相近（键盘）')
        self.respond(9, 'rejected', '分类一致')
        self.respond(11, 'accepted', '分类一致、地点一致')
        self.respond(0, 'accepted', '')
        self.respond(12, 'rejected', '分类一致、地点一致')
        # 落在窗口外：只应被 updated_at 条件排除。
        self.respond(8, 'accepted', '分类一致、地点一致', days_ago=45)
        # 未决断状态不进任何分母。
        self.respond(8, 'pending', '分类一致、地点一致')
        self.respond(8, 'withdrawn', '分类一致、地点一致')

    def decided_rows(self):
        """The decided rows as plain tuples, with no aggregation anywhere."""
        start = _period_start(30, timezone.localtime(self.now).date())
        return list(
            DemandResponse.objects.filter(
                updated_at__gte=start, updated_at__lte=self.now,
                status__in=('accepted', 'rejected'),
            ).values_list('match_score', 'match_reason', 'status')
        )

    def test_headline_counts_match_a_plain_recount(self):
        self.seed_a_mixed_window()
        quality = build_demand_match_quality(days=30, now=self.now)
        rows = self.decided_rows()

        accepted = [row for row in rows if row[2] == 'accepted']
        rejected = [row for row in rows if row[2] == 'rejected']
        self.assertEqual(quality['metrics']['decided_responses'], len(rows))
        self.assertEqual(quality['metrics']['accepted_count'], len(accepted))
        self.assertEqual(quality['metrics']['rejected_count'], len(rejected))
        self.assertEqual(
            quality['metrics']['acceptance_rate'],
            _percent(len(accepted), len(rows)),
        )
        self.assertEqual(
            quality['metrics']['average_score'],
            mean([row[0] for row in rows]),
        )
        self.assertTrue(quality['has_data'])

    def test_score_bands_match_a_plain_recount(self):
        self.seed_a_mixed_window()
        quality = build_demand_match_quality(days=30, now=self.now)
        rows = self.decided_rows()

        by_band = {}
        for score, _reason, status in rows:
            entry = by_band.setdefault(
                _band_of(score),
                {'count': 0, 'accepted': 0, 'scores': [], 'accepted_scores': []},
            )
            entry['count'] += 1
            entry['scores'].append(score)
            if status == 'accepted':
                entry['accepted'] += 1
                entry['accepted_scores'].append(score)

        reported = {band['label']: band for band in quality['score_bands']}
        for low, high, label in SCORE_BANDS:
            entry = by_band.get(
                label, {'count': 0, 'accepted': 0, 'scores': [], 'accepted_scores': []},
            )
            self.assertEqual(reported[label]['response_count'], entry['count'], label)
            self.assertEqual(reported[label]['accepted_count'], entry['accepted'], label)
            self.assertEqual(
                reported[label]['rejected_count'],
                entry['count'] - entry['accepted'], label,
            )
            self.assertEqual(
                reported[label]['acceptance_rate'],
                _percent(entry['accepted'], entry['count']), label,
            )
            self.assertEqual(
                reported[label]['average_score'], mean(entry['scores']), label,
            )
            self.assertEqual(
                reported[label]['average_accepted_score'],
                mean(entry['accepted_scores']), label,
            )
            self.assertEqual(
                reported[label]['is_small_sample'],
                entry['count'] < MIN_BAND_RESPONSES, label,
            )

    def test_dimension_rows_match_a_plain_recount(self):
        self.seed_a_mixed_window()
        quality = build_demand_match_quality(days=30, now=self.now)
        rows = self.decided_rows()

        markers = {
            'category': '分类一致',
            'location': '地点一致',
            'budget': '价格符合预算',
            'keyword': '关键词相近',
        }
        reported = {row['key']: row for row in quality['dimension_effect']}
        for key, marker in markers.items():
            with_rows = [row for row in rows if marker in row[1]]
            without_rows = [row for row in rows if marker not in row[1]]
            with_accepted = [row for row in with_rows if row[2] == 'accepted']
            without_accepted = [row for row in without_rows if row[2] == 'accepted']
            with_rate = _percent(len(with_accepted), len(with_rows))
            without_rate = _percent(len(without_accepted), len(without_rows))

            self.assertEqual(reported[key]['with_count'], len(with_rows), key)
            self.assertEqual(reported[key]['without_count'], len(without_rows), key)
            self.assertEqual(
                reported[key]['with_accepted_count'], len(with_accepted), key,
            )
            self.assertEqual(
                reported[key]['without_accepted_count'], len(without_accepted), key,
            )
            self.assertEqual(
                reported[key]['with_acceptance_rate'], with_rate, key,
            )
            self.assertEqual(
                reported[key]['without_acceptance_rate'], without_rate, key,
            )
            self.assertEqual(
                reported[key]['gap_points'],
                round(with_rate - without_rate, 1)
                if with_rows and without_rows else None,
                key,
            )
            self.assertEqual(
                reported[key]['is_small_sample'],
                len(with_rows) < MIN_BAND_RESPONSES
                or len(without_rows) < MIN_BAND_RESPONSES,
                key,
            )
            # 两边必须正好铺满已决断集合，不能有行被两边同时算或两边都不算。
            self.assertEqual(len(with_rows) + len(without_rows), len(rows), key)
            self.assertEqual(
                len(with_accepted) + len(without_accepted),
                sum(1 for row in rows if row[2] == 'accepted'), key,
            )

    def test_over_promised_withholds_its_counts_below_the_sample_floor(self):
        self.seed_a_mixed_window()
        quality = build_demand_match_quality(days=30, now=self.now)
        rows = self.decided_rows()
        high = [row for row in rows if row[0] >= HIGH_SCORE_FLOOR]

        reported = quality['over_promised']
        self.assertLess(len(high), MIN_BAND_RESPONSES)
        self.assertFalse(reported['has_sample'])
        # 样本不足时这一节只报一句说明，一个计数都不报——把几条高分响应算出来的
        # 拒绝率摆上运营看板，比不摆更糟。合成聚合之后这个分支的键集合不能变。
        self.assertNotIn('high_count', reported)
        self.assertNotIn('rejected_count', reported)
        self.assertNotIn('rejection_rate', reported)

    def test_over_promised_reports_its_counts_once_the_pool_is_big_enough(self):
        self.seed_a_mixed_window()
        for index in range(MIN_BAND_RESPONSES):
            self.respond(7 + (index % 5), 'rejected', '分类一致、地点一致')
        quality = build_demand_match_quality(days=30, now=self.now)
        rows = self.decided_rows()
        high = [row for row in rows if row[0] >= HIGH_SCORE_FLOOR]
        high_rejected = [row for row in high if row[2] == 'rejected']

        reported = quality['over_promised']
        self.assertTrue(reported['has_sample'])
        self.assertEqual(reported['high_count'], len(high))
        self.assertEqual(reported['rejected_count'], len(high_rejected))
        self.assertEqual(
            reported['rejection_rate'], _percent(len(high_rejected), len(high)),
        )

    def test_an_empty_window_matches_a_plain_recount(self):
        quality = build_demand_match_quality(days=30, now=self.now)
        rows = self.decided_rows()

        self.assertEqual(rows, [])
        self.assertFalse(quality['has_data'])
        self.assertEqual(quality['metrics']['decided_responses'], 0)
        self.assertIsNone(quality['metrics']['average_score'])
        self.assertEqual(quality['monotonicity']['kind'], 'unknown')
        self.assertFalse(quality['over_promised']['has_sample'])
        self.assertEqual(quality['findings'], [])
        for band in quality['score_bands']:
            self.assertEqual(band['response_count'], 0)
            self.assertIsNone(band['average_score'])
            self.assertIsNone(band['average_accepted_score'])


class DemandMatchQueryBudgetTests(TestCase):
    """The collapse must stay collapsed."""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('budget-seller')
        self.requester = make_user('budget-requester')
        self.category = Category.objects.create(name='预算分类')
        self.location = CampusLocation.objects.create(name='预算地点')

    def seed_enough_to_be_measurable(self):
        for index in range(24):
            demand = DemandPost.objects.create(
                requester=self.requester, title=f'预算求购 {index}',
                description='用于查询预算', category=self.category,
                location=self.location,
            )
            item = Item.objects.create(
                title=f'预算商品 {index}', description='用于查询预算',
                price=Decimal('50.00'), status='available', seller=self.seller,
                category=self.category, location=self.location,
            )
            response = DemandResponse.objects.create(
                demand=demand, item=item, responder=self.seller,
                status='accepted' if index % 2 else 'rejected',
                match_score=2 + (index % 10), match_reason='分类一致、地点一致',
            )
            DemandResponse.objects.filter(pk=response.pk).update(
                created_at=self.now - timedelta(days=1),
                updated_at=self.now - timedelta(days=1),
            )

    def test_a_populated_window_stays_within_budget(self):
        self.seed_enough_to_be_measurable()
        with CaptureQueriesContext(connection) as ctx:
            build_demand_match_quality(days=30, now=self.now)

        # 头条聚合 1 + 分段聚合 1 + 维度聚合 1 + 高分聚合 1 + 平均分 1。
        # 每一个曾经独立发出的 COUNT 都已经并进这五次。
        self.assertLessEqual(len(ctx.captured_queries), 5)

    def test_an_empty_history_still_stays_within_budget(self):
        with CaptureQueriesContext(connection) as ctx:
            build_demand_match_quality(days=30, now=self.now)
        self.assertLessEqual(len(ctx.captured_queries), 5)
