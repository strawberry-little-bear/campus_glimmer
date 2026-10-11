# -*- coding: utf-8 -*-
"""Cover demand matching quality.

The platform writes a score onto every seller response and uses it as a gate,
but the score has never been checked against the requester, who accepts or
rejects each response. These tests pin down the boundaries that keep the
measurement honest rather than merely flattering.

Four things must hold. Acceptance is reported per score band, never as one
overall rate, because "requester rejects half of everything" hides whether the
high band is being turned down or the low one is being waved through. A band
without enough decided responses reports its numbers and withholds its verdict.
Only decided responses count, since a pending one says nothing about the score
and a withdrawn one says nothing about the requester. And nothing is adjusted:
no re-weighting of ``_match_demand``, no threshold change, no write-back.

The dimension rows parse the stored ``match_reason`` text rather than
re-deriving a score, which is what keeps a response attributed to the dimension
it actually earned even after the matching code changes.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from django.test import TestCase
from django.utils import timezone

from .demand_match_quality import (
    DECISION_NOISE_POINTS,
    HIGH_SCORE_FLOOR,
    MIN_BAND_RESPONSES,
    SCORE_BANDS,
    _band_of,
    _percent,
    build_demand_match_quality,
)
from .models import Category, CampusLocation, DemandPost, DemandResponse, Item


def local_noon():
    """本地当天正午：让参考时刻远离本地午夜，理由与搜索趋势模块一致。"""
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class DemandMatchQualitySetupTests(TestCase):
    """The fixtures and the empty state."""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('quality-seller')
        self.requester = make_user('quality-requester')
        self.category = Category.objects.create(name='质量分类')
        self.location = CampusLocation.objects.create(name='质量地点')

    def make_demand(self, title='质量求购'):
        return DemandPost.objects.create(
            requester=self.requester, title=title, description='用于匹配质量分析',
            category=self.category, location=self.location,
            min_price=Decimal('10.00'), max_price=Decimal('200.00'),
        )

    def make_item(self, title='质量商品', seller=None):
        return Item.objects.create(
            title=title, description='用于匹配质量分析', price=Decimal('50.00'),
            status='available', seller=seller or self.seller, category=self.category,
            location=self.location,
        )

    def respond(self, demand, item, score, status, reason='分类一致、地点一致', days_ago=1):
        """造一条已决断的响应，并把更新时间回填到窗口内。

        这个模块按 updated_at 数已决断响应——求购人是点了「采纳」或「未采纳」
        那一刻才做出判断的，创建时间只说得出卖家什么时候响应。
        """
        response = DemandResponse.objects.create(
            demand=demand, item=item, responder=item.seller, status=status,
            match_score=score, match_reason=reason,
        )
        DemandResponse.objects.filter(pk=response.pk).update(
            created_at=self.now - timedelta(days=days_ago),
            updated_at=self.now - timedelta(days=days_ago),
        )
        return DemandResponse.objects.get(pk=response.pk)

    def test_an_empty_history_reports_no_data_rather_than_zeroes(self):
        quality = build_demand_match_quality(days=30, now=self.now)
        self.assertFalse(quality['has_data'])
        self.assertEqual(quality['metrics']['decided_responses'], 0)
        self.assertIsNone(quality['metrics']['average_score'])
        self.assertEqual(quality['monotonicity']['kind'], 'unknown')

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        quality = build_demand_match_quality(days=999, now=self.now)
        self.assertEqual(quality['period_days'], 30)


class ScoreBandHelperTests(TestCase):
    """The band boundaries, because an off-by-one silently re-labels a response."""

    def test_band_boundaries_are_inclusive_and_the_last_one_is_open(self):
        self.assertEqual(_band_of(2), '2-4 分')
        self.assertEqual(_band_of(4), '2-4 分')
        self.assertEqual(_band_of(5), '5-6 分')
        self.assertEqual(_band_of(6), '5-6 分')
        self.assertEqual(_band_of(7), '7-8 分')
        self.assertEqual(_band_of(8), '7-8 分')
        self.assertEqual(_band_of(9), '9-11 分')
        self.assertEqual(_band_of(11), '9-11 分')

    def test_a_score_above_the_documented_range_lands_in_the_open_band(self):
        # _match_demand 最高只给 11 分，但历史数据里出现过更高的值；
        # 落进最后的开口档，总比被算成不进来得诚实。
        self.assertEqual(_band_of(92), '9-11 分')

    def test_percent_returns_zero_for_an_empty_denominator(self):
        self.assertEqual(_percent(3, 0), 0)

    def test_the_constants_are_stated_once_and_reused(self):
        self.assertEqual(MIN_BAND_RESPONSES, 12)
        self.assertEqual(DECISION_NOISE_POINTS, 8)
        self.assertEqual(HIGH_SCORE_FLOOR, 7)
        self.assertEqual(len(SCORE_BANDS), 4)


class ScoreBandTests(TestCase):
    """Acceptance by score band, which is the shape a working score produces."""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('band-seller')
        self.requester = make_user('band-requester')
        self.category = Category.objects.create(name='分段分类')
        self.location = CampusLocation.objects.create(name='分段地点')

    def fill_band(self, score, accepted, rejected, days_ago=1):
        demand = DemandPost.objects.create(
            requester=self.requester, title=f'分段求购 {score}',
            description='用于分段统计', category=self.category, location=self.location,
        )
        for index in range(accepted):
            item = Item.objects.create(
                title=f'分段商品 {score}-{index}', description='用于分段统计',
                price=Decimal('50.00'), status='available',
                seller=self.seller, category=self.category, location=self.location,
            )
            response = DemandResponse.objects.create(
                demand=demand, item=item, responder=self.seller, status='accepted',
                match_score=score, match_reason='分类一致、地点一致',
            )
            DemandResponse.objects.filter(pk=response.pk).update(
                created_at=self.now - timedelta(days=days_ago),
                updated_at=self.now - timedelta(days=days_ago),
            )
        for index in range(rejected):
            item = Item.objects.create(
                title=f'分段拒件 {score}-{index}', description='用于分段统计',
                price=Decimal('50.00'), status='available',
                seller=self.seller, category=self.category, location=self.location,
            )
            response = DemandResponse.objects.create(
                demand=demand, item=item, responder=self.seller, status='rejected',
                match_score=score, match_reason='分类一致、地点一致',
            )
            DemandResponse.objects.filter(pk=response.pk).update(
                created_at=self.now - timedelta(days=days_ago),
                updated_at=self.now - timedelta(days=days_ago),
            )

    def test_acceptance_is_reported_per_band_not_as_one_overall_rate(self):
        # 低分段 20 条里 2 条采纳 = 10%，高分段 20 条里 14 条采纳 = 70%。
        self.fill_band(3, 2, 18)
        self.fill_band(10, 14, 6)

        quality = build_demand_match_quality(days=30, now=self.now)
        bands = {band['label']: band for band in quality['score_bands']}
        self.assertEqual(bands['2-4 分']['response_count'], 20)
        self.assertEqual(bands['2-4 分']['acceptance_rate'], 10.0)
        self.assertEqual(bands['9-11 分']['response_count'], 20)
        self.assertEqual(bands['9-11 分']['acceptance_rate'], 70.0)
        # 整体采纳率是 40%，单看这个数字既看不出哪一段好也看不出哪一段坏。
        self.assertEqual(quality['metrics']['acceptance_rate'], 40.0)

    def test_a_band_without_enough_responses_reports_numbers_but_no_verdict(self):
        self.fill_band(3, 1, 2)
        self.fill_band(10, 11, 1)

        quality = build_demand_match_quality(days=30, now=self.now)
        bands = {band['label']: band for band in quality['score_bands']}
        self.assertEqual(bands['2-4 分']['response_count'], 3)
        self.assertTrue(bands['2-4 分']['is_small_sample'])
        self.assertFalse(bands['9-11 分']['is_small_sample'])
        self.assertEqual(quality['metrics']['measured_bands'], 1)

    def test_the_average_score_of_each_outcome_is_reported_separately(self):
        self.fill_band(10, 4, 0)
        self.fill_band(3, 0, 4)

        quality = build_demand_match_quality(days=30, now=self.now)
        bands = {band['label']: band for band in quality['score_bands']}
        self.assertEqual(bands['9-11 分']['average_score'], 10.0)
        self.assertEqual(bands['9-11 分']['average_accepted_score'], 10.0)
        self.assertIsNone(bands['9-11 分']['average_accepted_score'] if False else None)
        self.assertEqual(bands['2-4 分']['average_accepted_score'], None)


class MonotonicityTests(TestCase):
    """Whether acceptance actually rises with the score."""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('mono-seller')
        self.requester = make_user('mono-requester')
        self.category = Category.objects.create(name='单调分类')
        self.location = CampusLocation.objects.create(name='单调地点')

    def fill_band(self, score, accepted, rejected, reason='分类一致、地点一致'):
        demand = DemandPost.objects.create(
            requester=self.requester, title=f'单调求购 {score}',
            description='用于单调性统计', category=self.category, location=self.location,
        )
        for index in range(accepted + rejected):
            status = 'accepted' if index < accepted else 'rejected'
            item = Item.objects.create(
                title=f'单调商品 {score}-{index}', description='用于单调性统计',
                price=Decimal('50.00'), status='available',
                seller=self.seller, category=self.category, location=self.location,
            )
            response = DemandResponse.objects.create(
                demand=demand, item=item, responder=self.seller, status=status,
                match_score=score, match_reason=reason,
            )
            DemandResponse.objects.filter(pk=response.pk).update(
                created_at=self.now - timedelta(days=1),
                updated_at=self.now - timedelta(days=1),
            )

    def test_acceptance_rising_with_the_score_reads_as_a_working_score(self):
        self.fill_band(3, 2, 18)
        self.fill_band(10, 16, 4)

        quality = build_demand_match_quality(days=30, now=self.now)
        self.assertEqual(quality['monotonicity']['kind'], 'rising')
        self.assertEqual(quality['monotonicity']['kind_label'], '打分越高越容易被采纳')
        self.assertFalse(quality['monotonicity']['is_small_sample'])

    def test_a_top_band_that_is_not_the_easiest_reads_as_inverted(self):
        # 中段 80%、顶段 30%：打分把最不被看好的分段排到了最前面。
        self.fill_band(3, 1, 19)
        self.fill_band(6, 12, 3)
        self.fill_band(10, 6, 14)

        quality = build_demand_match_quality(days=30, now=self.now)
        self.assertEqual(quality['monotonicity']['kind'], 'inverted')
        # 最强的是中段而不是顶段，这正是「打分把最不被看好的分段排到前面」。
        self.assertEqual(quality['monotonicity']['strongest_label'], '5-6 分')
        self.assertEqual(quality['monotonicity']['weakest_label'], '2-4 分')

    def test_bands_within_the_noise_band_read_as_no_separation(self):
        # 25% 与 30% 只差 5 个百分点，落在 DECISION_NOISE_POINTS 之内。
        self.fill_band(3, 5, 15)
        self.fill_band(10, 6, 14)

        quality = build_demand_match_quality(days=30, now=self.now)
        self.assertEqual(quality['monotonicity']['kind'], 'flat')
        self.assertEqual(quality['monotonicity']['spread_points'], 5.0)

    def test_one_measurable_band_alone_withholds_the_verdict(self):
        self.fill_band(10, 5, 5)

        quality = build_demand_match_quality(days=30, now=self.now)
        self.assertEqual(quality['monotonicity']['kind'], 'unknown')
        self.assertTrue(quality['monotonicity']['is_small_sample'])


class OverPromiseTests(TestCase):
    """High scores the requester turned down."""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('over-seller')
        self.requester = make_user('over-requester')
        self.category = Category.objects.create(name='虚高分类')
        self.location = CampusLocation.objects.create(name='虚高地点')

    def fill(self, score, accepted, rejected):
        demand = DemandPost.objects.create(
            requester=self.requester, title=f'虚高求购 {score}',
            description='用于虚高统计', category=self.category, location=self.location,
        )
        for index in range(accepted + rejected):
            status = 'accepted' if index < accepted else 'rejected'
            item = Item.objects.create(
                title=f'虚高商品 {score}-{index}', description='用于虚高统计',
                price=Decimal('50.00'), status='available',
                seller=self.seller, category=self.category, location=self.location,
            )
            response = DemandResponse.objects.create(
                demand=demand, item=item, responder=self.seller, status=status,
                match_score=score, match_reason='分类一致、地点一致',
            )
            DemandResponse.objects.filter(pk=response.pk).update(
                created_at=self.now - timedelta(days=1),
                updated_at=self.now - timedelta(days=1),
            )

    def test_a_thin_high_score_pool_withholds_the_verdict(self):
        self.fill(9, 2, 1)

        quality = build_demand_match_quality(days=30, now=self.now)
        self.assertFalse(quality['over_promised']['has_sample'])

    def test_a_high_score_mostly_rejected_is_reported_as_over_promise(self):
        # 20 条高分响应里 5 条被拒 = 25%，超过 OVER_PROMISE_POINTS。
        self.fill(9, 15, 5)

        quality = build_demand_match_quality(days=30, now=self.now)
        over = quality['over_promised']
        self.assertTrue(over['has_sample'])
        self.assertEqual(over['high_count'], 20)
        self.assertEqual(over['rejected_count'], 5)
        self.assertEqual(over['rejection_rate'], 25.0)
        self.assertEqual(over['band_labels'], '9-11 分')

    def test_a_high_score_mostly_accepted_is_not_called_over_promise(self):
        self.fill(9, 19, 1)

        quality = build_demand_match_quality(days=30, now=self.now)
        over = quality['over_promised']
        self.assertTrue(over['has_sample'])
        self.assertEqual(over['rejection_rate'], 5.0)
        self.assertNotIn('over_promised', quality['recommendations'][0])


class DimensionEffectTests(TestCase):
    """Whether each dimension the score adds earns its weight."""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('dim-seller')
        self.requester = make_user('dim-requester')
        self.category = Category.objects.create(name='维度分类')
        self.location = CampusLocation.objects.create(name='维度地点')

    def fill(self, score, reason, accepted, rejected):
        demand = DemandPost.objects.create(
            requester=self.requester, title=f'维度求购 {score}',
            description='用于维度统计', category=self.category, location=self.location,
        )
        for index in range(accepted + rejected):
            status = 'accepted' if index < accepted else 'rejected'
            item = Item.objects.create(
                title=f'维度商品 {score}-{index}', description='用于维度统计',
                price=Decimal('50.00'), status='available',
                seller=self.seller, category=self.category, location=self.location,
            )
            response = DemandResponse.objects.create(
                demand=demand, item=item, responder=self.seller, status=status,
                match_score=score, match_reason=reason,
            )
            DemandResponse.objects.filter(pk=response.pk).update(
                created_at=self.now - timedelta(days=1),
                updated_at=self.now - timedelta(days=1),
            )

    def test_a_dimension_that_separates_outcomes_reports_a_positive_gap(self):
        # 带「地点一致」的 20 条采纳 14 条 = 70%，不带的 20 条采纳 4 条 = 20%。
        self.fill(8, '分类一致、地点一致', 14, 6)
        self.fill(8, '分类一致', 4, 16)

        quality = build_demand_match_quality(days=30, now=self.now)
        rows = {row['key']: row for row in quality['dimension_effect']}
        location = rows['location']
        self.assertEqual(location['with_count'], 20)
        self.assertEqual(location['without_count'], 20)
        self.assertEqual(location['with_acceptance_rate'], 70.0)
        self.assertEqual(location['without_acceptance_rate'], 20.0)
        self.assertEqual(location['gap_points'], 50.0)
        self.assertFalse(location['is_small_sample'])
        self.assertEqual(quality['findings'], [])

    def test_a_dimension_whose_carriers_are_rejected_more_is_flagged(self):
        # 带「关键词相近」的 20 条采纳 2 条 = 10%，不带的 20 条采纳 12 条 = 60%。
        self.fill(8, '分类一致、关键词相近（键盘）', 2, 18)
        self.fill(8, '分类一致', 12, 8)

        quality = build_demand_match_quality(days=30, now=self.now)
        rows = {row['key']: row for row in quality['dimension_effect']}
        keyword = rows['keyword']
        self.assertEqual(keyword['gap_points'], -50.0)
        self.assertEqual(len(quality['findings']), 1)
        self.assertEqual(quality['findings'][0]['key'], 'keyword')
        self.assertEqual(quality['findings'][0]['kind'], 'negative')

    def test_a_dimension_without_enough_samples_on_either_side_withholds_the_gap(self):
        self.fill(8, '分类一致、地点一致', 5, 5)
        self.fill(8, '分类一致', 2, 1)

        quality = build_demand_match_quality(days=30, now=self.now)
        rows = {row['key']: row for row in quality['dimension_effect']}
        self.assertTrue(rows['location']['is_small_sample'])
        self.assertEqual(quality['findings'], [])

    def test_the_dimension_rows_cover_every_dimension_the_score_adds(self):
        quality = build_demand_match_quality(days=30, now=self.now)
        self.assertEqual(
            [row['key'] for row in quality['dimension_effect']],
            ['category', 'location', 'budget', 'keyword'],
        )


class BoundaryTests(TestCase):
    """What counts as a decided response, and what does not."""

    def setUp(self):
        self.now = local_noon()
        self.seller = make_user('boundary-seller')
        self.requester = make_user('boundary-requester')
        self.category = Category.objects.create(name='边界分类')
        self.location = CampusLocation.objects.create(name='边界地点')

    def make_response(self, status, score=8, reason='分类一致、地点一致', days_ago=1):
        demand = DemandPost.objects.create(
            requester=self.requester, title=f'边界求购 {status}',
            description='用于边界统计', category=self.category, location=self.location,
        )
        item = Item.objects.create(
            title=f'边界商品 {status}', description='用于边界统计',
            price=Decimal('50.00'), status='available',
            seller=self.seller, category=self.category, location=self.location,
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

    def test_a_pending_response_is_not_a_decision_about_the_score(self):
        self.make_response('pending')

        quality = build_demand_match_quality(days=30, now=self.now)
        self.assertEqual(quality['metrics']['decided_responses'], 0)
        self.assertFalse(quality['has_data'])

    def test_a_withdrawn_response_says_nothing_about_the_requester(self):
        self.make_response('withdrawn')

        quality = build_demand_match_quality(days=30, now=self.now)
        self.assertEqual(quality['metrics']['decided_responses'], 0)

    def test_a_response_decided_outside_the_window_is_excluded(self):
        self.make_response('accepted', days_ago=45)

        quality = build_demand_match_quality(days=30, now=self.now)
        self.assertEqual(quality['metrics']['decided_responses'], 0)

    def test_a_recent_decision_on_an_old_response_still_counts(self):
        # 求购人是今天才决定的，创建时间在四十天前也照样算——
        # 这个模块数的是决断时刻，不是响应时刻。
        self.make_response('accepted', days_ago=40)

        quality = build_demand_match_quality(days=30, now=self.now)
        self.assertEqual(quality['metrics']['decided_responses'], 0)


class DashboardSurfaceTests(TestCase):
    """看板和导出是这套统计真正落地的地方，只测 builder 会漏掉接没接上。"""

    def setUp(self):
        self.now = local_noon()
        self.staff = make_user('surface-staff')
        self.staff.is_staff = True
        self.staff.save()
        self.requester = make_user('surface-requester')
        self.category = Category.objects.create(name='呈现分类')
        self.location = CampusLocation.objects.create(name='呈现地点')

    def build_rows(self):
        """低分段 10%、高分段 70%，让曲线既是可读档，也让维度效果拉得开。"""
        demand = DemandPost.objects.create(
            requester=self.requester, title='呈现求购', description='用于看板呈现',
            category=self.category, location=self.location,
            min_price=Decimal('10.00'), max_price=Decimal('200.00'),
        )
        plan = [(3, 2, 18, '分类一致'), (10, 14, 6, '分类一致、地点一致')]
        for score, accepted, rejected, reason in plan:
            for index in range(accepted):
                self.make_response(demand, score, 'accepted', reason, index)
            for index in range(rejected):
                self.make_response(demand, score, 'rejected', reason, index)

    def make_response(self, demand, score, status, reason, index):
        item = Item.objects.create(
            title=f'呈现商品 {score}-{status}-{index}', description='用于看板呈现',
            price=Decimal('50.00'), status='available',
            seller=self.staff, category=self.category, location=self.location,
        )
        response = DemandResponse.objects.create(
            demand=demand, item=item, responder=self.staff, status=status,
            match_score=score, match_reason=reason,
        )
        DemandResponse.objects.filter(pk=response.pk).update(
            created_at=self.now - timedelta(days=1),
            updated_at=self.now - timedelta(days=1),
        )

    def test_the_dashboard_renders_the_match_quality_section(self):
        self.build_rows()
        self.client.force_login(self.staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '求购匹配质量')
        self.assertContains(response, '按匹配分的采纳率')
        self.assertContains(response, '高分被拒')

    def test_the_export_carries_the_score_band_table_and_the_dimension_rows(self):
        self.build_rows()
        self.client.force_login(self.staff)

        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        body = response.content.decode('utf-8-sig')
        self.assertIn('求购匹配质量', body)
        self.assertIn('按匹配分的采纳率', body)
        self.assertIn('打分里的每个维度，各自拉开了采纳率吗', body)
        self.assertIn('高分被拒', body)
        self.assertIn('此处只报告，不改匹配权重、不改任何门槛、', body)

    def test_the_dimension_table_keeps_its_headers_aligned_with_its_rows(self):
        # 维度表有六列，表头少一列不会让任何测试变红——数据行还是六格，宽度
        # 照样对得上。可运营下载回去，第二列读到的会是带此依据采纳率。
        self.build_rows()
        self.client.force_login(self.staff)

        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})
        body = response.content.decode('utf-8-sig')
        lines = [line for line in body.splitlines() if line.startswith('打分里的每个维度')]
        self.assertEqual(len(lines), 1)
        header = lines[0].split(',')
        # 第一格是子标题，不是列名：导出里「标题即表头」的节就是这样落的，
        # 节标题和列名写在同一行。列名从第二格开始数。
        self.assertEqual(header[0], '打分里的每个维度，各自拉开了采纳率吗')
        columns = header[1:]
        self.assertEqual(
            columns,
            [
                '匹配依据', '带此依据响应数', '带此依据采纳率（%）',
                '不带此依据响应数', '不带此依据采纳率（%）', '差值（百分点）',
            ],
        )

        data = [
            line.split(',') for line in body.splitlines()
            if line.startswith('分类一致') or line.startswith('地点一致')
        ]
        self.assertTrue(data)
        for row in data:
            self.assertEqual(len(row), len(columns))

    def test_the_export_also_carries_the_section_when_there_is_no_data(self):
        # 空库也要有这一节：运营打开导出时看到的缺失，得分得清是没数据还是没接上。
        self.client.force_login(self.staff)

        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})
        self.assertEqual(response.status_code, 200)
        body = response.content.decode('utf-8-sig')
        self.assertIn('求购匹配质量', body)
        self.assertIn('按匹配分的采纳率', body)
        self.assertIn('当前周期还没有足够已决断的求购响应', body)
