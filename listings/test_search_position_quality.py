# -*- coding: utf-8 -*-
"""Cover search result position quality.

The point of this module is that the platform ships its own relevance sort and,
until now, nothing measured whether that sort helps. So these tests pin down
the boundaries that keep the measurement honest rather than merely flattering.

Five things must hold. Click-through is reported per position bucket, never as
one platform-wide number, because "nobody clicks" hides whether the front of
the page is unappealing or the good listings are buried. A bucket without
enough exposures reports its numbers and withholds its verdict, since twelve
exposures turn one click into an eight-point swing. Clicked listings are
compared against exposed listings by average position, which is the only way
to see the ranking working. A click with no usable position is dropped rather
than counted as position zero, because a row that does not say where it sat
says nothing about where it sat. And nothing is adjusted: no re-weighting of
the relevance sort, no moving items between pages, no write-back.

The last two classes cover the two surfaces this actually reaches an operator
through: the search insights page and its CSV export. A module that computes
correctly but never renders is the same gap the module was written to close.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Category, Item, SearchClick, SearchImpression, SearchQuery
from .search_position_quality import (
    DECAY_FLAG_POINTS,
    MIN_BUCKET_SAMPLE,
    POSITION_BUCKETS,
    _bucket_of,
    _percent,
    build_search_position_quality,
)


def local_noon():
    """本地当天正午，理由与搜索趋势模块一致：让参考时刻远离本地午夜。"""
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def make_item(seller, category, title):
    return Item.objects.create(
        title=title, description='用于位置分析', price=Decimal('20.00'),
        status='available', seller=seller, category=category,
    )


def make_search(user, term='键盘', result_count=12, days_ago=1, now=None):
    """造一条有结果的搜索，并把时间回填到窗口内。"""
    query = SearchQuery.objects.create(
        user=user, query=term, result_count=result_count,
    )
    SearchQuery.objects.filter(pk=query.pk).update(
        created_at=(now or local_noon()) - timedelta(days=days_ago),
    )
    return SearchQuery.objects.get(pk=query.pk)


def expose(search, item, position):
    return SearchImpression.objects.create(search_query=search, item=item, position=position)


def click(search, item, position):
    """记一次点击，时间跟着搜索回填。

    和搜索趋势模块同一条理由：这个模块按窗口数点击，
    留在运行刻的点击落在窗口上界之外，等于没点。
    """
    record = SearchClick.objects.create(
        search_query=search, item=item, position=position,
    )
    SearchClick.objects.filter(pk=record.pk).update(
        created_at=search.created_at + timedelta(minutes=5),
    )
    return SearchClick.objects.get(pk=record.pk)


class PositionBucketHelperTests(TestCase):
    """The bucket boundaries, because an off-by-one here silently re-labels rows."""

    def test_bucket_boundaries_are_inclusive_and_the_last_one_is_open(self):
        self.assertEqual(_bucket_of(1), '第 1-3 位')
        self.assertEqual(_bucket_of(3), '第 1-3 位')
        self.assertEqual(_bucket_of(4), '第 4-6 位')
        self.assertEqual(_bucket_of(6), '第 4-6 位')
        self.assertEqual(_bucket_of(7), '第 7-9 位')
        self.assertEqual(_bucket_of(9), '第 7-9 位')
        self.assertEqual(_bucket_of(10), '第 10 位及以后')
        # 分页每页 12 条，第二页之后的位置会一直往上长，最后一档必须是开口的。
        self.assertEqual(_bucket_of(240), '第 10 位及以后')

    def test_positions_are_mapped_in_the_documented_order(self):
        labels = [label for _, _, label in POSITION_BUCKETS]
        self.assertEqual(labels, ['第 1-3 位', '第 4-6 位', '第 7-9 位', '第 10 位及以后'])

    def test_percent_returns_zero_for_an_empty_denominator(self):
        self.assertEqual(_percent(0, 0), 0)
        self.assertEqual(_percent(1, 4), 25.0)


class PositionQualitySetupTests(TestCase):
    """Shared fixture helpers and the empty case."""

    def setUp(self):
        self.user = make_user('position-user')
        self.category = Category.objects.create(name='位置分析分类')
        self.now = local_noon()

    def test_an_empty_history_reports_no_data_rather_than_zeroes(self):
        quality = build_search_position_quality(days=30, now=self.now)
        self.assertFalse(quality['has_data'])
        self.assertEqual(quality['metrics']['impressions'], 0)
        self.assertEqual(quality['metrics']['clicks'], 0)
        self.assertEqual(quality['metrics']['overall_click_through_rate'], 0)
        self.assertEqual(quality['depth_diagnosis']['kind'], 'unknown')
        self.assertIn('积累样本', quality['summary'])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        quality = build_search_position_quality(days=13, now=self.now)
        self.assertEqual(quality['period_days'], 30)


class DecayCurveTests(TestCase):
    """Click-through by position bucket, which is the shape a healthy sort makes."""

    def setUp(self):
        self.user = make_user('decay-user')
        self.category = Category.objects.create(name='衰减分类')
        self.now = local_noon()

    def test_clicks_are_attributed_to_the_bucket_their_position_falls_in(self):
        search = make_search(self.user)
        front = make_item(self.user, self.category, '前排商品')
        deep = make_item(self.user, self.category, '深页商品')
        for position in (1, 2, 3):
            self.expose = SearchImpression.objects.create(
                search_query=search, item=front, position=position,
            )
        for position in (10, 11, 12):
            SearchImpression.objects.create(search_query=search, item=deep, position=position)
        click(search, front, 1)
        click(search, deep, 11)

        quality = build_search_position_quality(days=30, now=self.now)
        curve = {row['label']: row for row in quality['decay_curve']}
        self.assertEqual(curve['第 1-3 位']['impression_count'], 3)
        self.assertEqual(curve['第 1-3 位']['click_count'], 1)
        self.assertEqual(curve['第 1-3 位']['click_through_rate'], 33.3)
        self.assertEqual(curve['第 10 位及以后']['impression_count'], 3)
        self.assertEqual(curve['第 10 位及以后']['click_count'], 1)
        self.assertEqual(curve['第 10 位及以后']['click_through_rate'], 33.3)

    def test_a_bucket_without_enough_exposure_reports_numbers_but_no_verdict(self):
        search = make_search(self.user)
        item = make_item(self.user, self.category, '薄样本商品')
        SearchImpression.objects.create(search_query=search, item=item, position=1)
        click(search, item, 1)

        quality = build_search_position_quality(days=30, now=self.now)
        curve = {row['label']: row for row in quality['decay_curve']}
        front = curve['第 1-3 位']
        # 一次曝光一次点击是 100%，这个数字说明不了任何事，所以这一档只报数字。
        self.assertEqual(front['impression_count'], 1)
        self.assertEqual(front['click_through_rate'], 100.0)
        self.assertTrue(front['is_small_sample'])
        self.assertEqual(quality['depth_diagnosis']['kind'], 'unknown')
        self.assertEqual(quality['metrics']['measured_buckets'], 0)

    def test_a_click_without_a_usable_position_is_dropped_not_counted_as_zero(self):
        search = make_search(self.user)
        item = make_item(self.user, self.category, '没有位置的商品')
        for position in range(MIN_BUCKET_SAMPLE):
            SearchImpression.objects.create(search_query=search, item=item, position=1)
        # position=0 的点击说不清它点的是第几位，算进任何一档都是编数据。
        click(search, item, 0)

        quality = build_search_position_quality(days=30, now=self.now)
        curve = {row['label']: row for row in quality['decay_curve']}
        self.assertEqual(curve['第 1-3 位']['impression_count'], MIN_BUCKET_SAMPLE)
        self.assertEqual(curve['第 1-3 位']['click_count'], 0)
        self.assertEqual(curve['第 1-3 位']['click_through_rate'], 0)
        self.assertEqual(quality['metrics']['clicks'], 0)


class RealDecayFlagTests(TestCase):
    """A bucket only earns the decay flag when the gap is too wide to be one click."""

    def setUp(self):
        self.user = make_user('decay-flag-user')
        self.category = Category.objects.create(name='衰减标记分类')
        self.now = local_noon()

    def curve(self, quality):
        return {row['label']: row for row in quality['decay_curve']}

    def test_a_bucket_trailing_the_front_by_the_flag_points_is_flagged(self):
        search = make_search(self.user)
        front = make_item(self.user, self.category, '前排商品')
        deep = make_item(self.user, self.category, '深页商品')
        # 前排 20 次曝光 6 次点击 = 30%，深页 20 次曝光 3 次点击 = 15%。
        # 差 15 个百分点，正好等于 DECAY_FLAG_POINTS，应当算真实衰减。
        for _ in range(20):
            SearchImpression.objects.create(search_query=search, item=front, position=1)
        for _ in range(20):
            SearchImpression.objects.create(search_query=search, item=deep, position=20)
        for _ in range(6):
            click(search, front, 1)
        for _ in range(3):
            click(search, deep, 20)

        quality = build_search_position_quality(days=30, now=self.now)
        rows = self.curve(quality)
        self.assertTrue(rows['第 1-3 位']['decay_flag'] is False)
        self.assertTrue(rows['第 10 位及以后']['decay_flag'] is True)
        self.assertEqual(quality['metrics']['decayed_buckets'], 1)

    def test_a_gap_of_one_point_below_the_flag_is_noise_not_decay(self):
        search = make_search(self.user)
        front = make_item(self.user, self.category, '前排商品')
        deep = make_item(self.user, self.category, '深页商品')
        # 前排 30%，深页 16%：只差 14 个百分点，一次点击就能造成这个差，
        # 所以只报数字，不插旗。
        for _ in range(20):
            SearchImpression.objects.create(search_query=search, item=front, position=1)
        for _ in range(20):
            SearchImpression.objects.create(search_query=search, item=deep, position=20)
        for _ in range(6):
            click(search, front, 1)
        for _ in range(4):
            click(search, deep, 20)

        quality = build_search_position_quality(days=30, now=self.now)
        rows = self.curve(quality)
        self.assertTrue(rows['第 1-3 位']['decay_flag'] is False)
        self.assertTrue(rows['第 10 位及以后']['decay_flag'] is False)
        self.assertEqual(quality['metrics']['decayed_buckets'], 0)

    def test_a_thin_bucket_never_earns_a_flag_even_when_the_rate_is_zero(self):
        search = make_search(self.user)
        front = make_item(self.user, self.category, '前排商品')
        deep = make_item(self.user, self.category, '深页商品')
        for _ in range(MIN_BUCKET_SAMPLE):
            SearchImpression.objects.create(search_query=search, item=front, position=1)
        for _ in range(6):
            click(search, front, 1)
        # 深页只有 6 次曝光，0 点击 = 0%，和前排差了 50 个百分点，
        # 但样本不够就不给结论。
        for _ in range(MIN_BUCKET_SAMPLE - 6):
            SearchImpression.objects.create(search_query=search, item=deep, position=20)

        quality = build_search_position_quality(days=30, now=self.now)
        rows = self.curve(quality)
        self.assertEqual(rows['第 10 位及以后']['impression_count'], 6)
        self.assertTrue(rows['第 10 位及以后']['is_small_sample'])
        self.assertTrue(rows['第 10 位及以后']['decay_flag'] is False)

    def test_nothing_is_flagged_when_the_front_bucket_is_too_thin_to_read(self):
        search = make_search(self.user)
        front = make_item(self.user, self.category, '前排商品')
        deep = make_item(self.user, self.category, '深页商品')
        # 前排本身读不出来，拿它当基准比较后面的档毫无意义。
        for _ in range(3):
            SearchImpression.objects.create(search_query=search, item=front, position=1)
        click(search, front, 1)
        for _ in range(20):
            SearchImpression.objects.create(search_query=search, item=deep, position=20)

        quality = build_search_position_quality(days=30, now=self.now)
        rows = self.curve(quality)
        self.assertTrue(rows['第 1-3 位']['is_small_sample'])
        self.assertTrue(rows['第 10 位及以后']['decay_flag'] is False)
        self.assertEqual(quality['metrics']['decayed_buckets'], 0)

class RankingEffectTests(TestCase):
    """Whether the listings people click sit higher than the ones we show."""

    def setUp(self):
        self.user = make_user('ranking-user')
        self.category = Category.objects.create(name='排序分类')
        self.now = local_noon()

    def test_a_sort_that_puts_clicked_listings_first_reports_a_positive_lift(self):
        search = make_search(self.user)
        front = make_item(self.user, self.category, '排在前面的商品')
        deep = make_item(self.user, self.category, '排在后面的商品')
        for position in range(1, MIN_BUCKET_SAMPLE + 1):
            expose(search, front, 1)
        for position in range(1, MIN_BUCKET_SAMPLE + 1):
            expose(search, deep, 10 + position)
        for _ in range(MIN_BUCKET_SAMPLE):
            click(search, front, 1)

        quality = build_search_position_quality(days=30, now=self.now)
        effect = quality['ranking_effect']
        # 位置数字越小越靠前，所以被点的商品平均位置更低就是排序在帮忙。
        self.assertLess(effect['click_average_position'], effect['exposure_average_position'])
        self.assertGreaterEqual(effect['lift'], 1)
        self.assertEqual(effect['direction'], 'helping')

    def test_a_sort_that_buries_the_clicked_listings_reports_a_negative_lift(self):
        search = make_search(self.user)
        buried = make_item(self.user, self.category, '被埋住的商品')
        shown_first = make_item(self.user, self.category, '排在前面但没人点的商品')
        # 曝光平均位置必须落在中间：如果曝光也全在第 12 位，lift 恒为 0，
        # 这个用例就什么都证明不了。
        for position in range(1, MIN_BUCKET_SAMPLE + 1):
            expose(search, shown_first, 1)
        for position in range(1, MIN_BUCKET_SAMPLE + 1):
            expose(search, buried, 12)
        for _ in range(MIN_BUCKET_SAMPLE):
            click(search, buried, 12)

        quality = build_search_position_quality(days=30, now=self.now)
        effect = quality['ranking_effect']
        self.assertLessEqual(effect['lift'], -1)
        self.assertEqual(effect['direction'], 'hurting')
        self.assertFalse(effect['is_small_sample'])
        # 只报告，不改 relevance 权重——这条断言守住的是“诊断不反悔”。
        self.assertTrue(any('不改 relevance' in note for note in quality['recommendations']))

    def test_a_handful_of_clicks_reports_numbers_without_a_direction(self):
        search = make_search(self.user)
        item = make_item(self.user, self.category, '点击很少的商品')
        expose(search, item, 1)
        click(search, item, 1)

        quality = build_search_position_quality(days=30, now=self.now)
        effect = quality['ranking_effect']
        self.assertTrue(effect['is_small_sample'])
        self.assertEqual(effect['direction'], 'unknown')
        self.assertIn('只报数字', effect['direction_label'])


class DepthDiagnosisTests(TestCase):
    """Separating a weak first screen from clicks that only happen deeper in."""

    def setUp(self):
        self.user = make_user('depth-user')
        self.category = Category.objects.create(name='深度分类')
        self.now = local_noon()

    def seed(self, front_rate, deep_rate):
        search = make_search(self.user)
        front = make_item(self.user, self.category, '前排商品')
        deep = make_item(self.user, self.category, '深页商品')
        for position in range(1, MIN_BUCKET_SAMPLE + 1):
            SearchImpression.objects.create(search_query=search, item=front, position=1)
            SearchImpression.objects.create(search_query=search, item=deep, position=10)
        for index in range(round(MIN_BUCKET_SAMPLE * front_rate)):
            click(search, front, 1)
        for index in range(round(MIN_BUCKET_SAMPLE * deep_rate)):
            click(search, deep, 10)
        return build_search_position_quality(days=30, now=self.now)

    def test_clicks_concentrated_at_the_front_read_as_a_ranking_problem(self):
        quality = self.seed(front_rate=0.0, deep_rate=0.5)
        self.assertEqual(quality['depth_diagnosis']['kind'], 'ranking')
        self.assertIn('标题', quality['depth_diagnosis']['summary'])

    def test_clicks_that_only_happen_deeper_read_as_a_depth_problem(self):
        quality = self.seed(front_rate=0.5, deep_rate=0.2)
        self.assertEqual(quality['depth_diagnosis']['kind'], 'depth')
        self.assertIn('分页', quality['depth_diagnosis']['summary'])

    def test_a_front_screen_nobody_clicks_past_reads_as_thin_deep_supply(self):
        quality = self.seed(front_rate=0.5, deep_rate=0.0)
        self.assertEqual(quality['depth_diagnosis']['kind'], 'shallow')
        self.assertIn('补商品', quality['recommendations'][0])

    def test_a_healthy_overall_rate_is_not_flagged_as_a_problem(self):
        quality = self.seed(front_rate=0.6, deep_rate=0.5)
        self.assertEqual(quality['depth_diagnosis']['kind'], 'healthy')



class SearchInsightsSurfaceTests(TestCase):
    """The page and the export are where this measurement actually lands."""

    def setUp(self):
        self.user = make_user('surface-user')
        self.category = Category.objects.create(name='位置质量呈现分类')
        self.now = local_noon()

    def build_rows(self):
        """一排位置 1、一排位置 20，让曲线既有可读档也有衰减档。"""
        search = make_search(self.user, now=self.now)
        front = make_item(self.user, self.category, '前排商品')
        deep = make_item(self.user, self.category, '深页商品')
        for _ in range(20):
            SearchImpression.objects.create(search_query=search, item=front, position=1)
        for _ in range(20):
            SearchImpression.objects.create(search_query=search, item=deep, position=20)
        for _ in range(6):
            click(search, front, 1)
        for _ in range(3):
            click(search, deep, 20)

    def test_the_insights_page_renders_the_position_quality_section(self):
        self.build_rows()
        self.user.is_staff = True
        self.user.save()
        self.client.force_login(self.user)

        response = self.client.get(reverse('search_insights'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '搜索结果位置质量')
        self.assertContains(response, '位置衰减曲线')
        self.assertContains(response, '第 1-3 位')
        self.assertContains(response, '第 10 位及以后')

    def test_the_insights_export_carries_the_decay_curve_columns(self):
        self.build_rows()
        self.user.is_staff = True
        self.user.save()
        self.client.force_login(self.user)

        response = self.client.get(reverse('search_insights_export'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        body = response.content.decode('utf-8-sig')
        self.assertIn('搜索结果位置质量', body)
        self.assertIn('位置衰减曲线', body)
        self.assertIn('点击率相对前排明显衰减', body)
        self.assertIn('此处只报告，不改 relevance 权重', body)

class BoundaryTests(TestCase):
    """The two windows, and the rule that a zero-result search never contributes."""

    def setUp(self):
        self.user = make_user('boundary-user')
        self.category = Category.objects.create(name='边界分类')
        self.now = local_noon()

    def test_a_search_with_no_results_contributes_no_positions(self):
        # 没有结果就没有位置，把它算进来只会把点击率的分母弄脏。
        query = SearchQuery.objects.create(
            user=self.user, query='不存在的商品', result_count=0,
        )
        SearchQuery.objects.filter(pk=query.pk).update(created_at=self.now - timedelta(days=1))
        item = make_item(self.user, self.category, '凭空出现的商品')
        SearchImpression.objects.create(search_query=query, item=item, position=1)
        click = SearchClick.objects.create(search_query=query, item=item, user=self.user, position=1)
        SearchClick.objects.filter(pk=click.pk).update(
            created_at=query.created_at + timedelta(minutes=5),
        )

        quality = build_search_position_quality(days=30, now=self.now)
        self.assertEqual(quality['metrics']['impressions'], 0)
        self.assertEqual(quality['metrics']['clicks'], 0)
        self.assertFalse(quality['has_data'])

    def test_an_older_search_falls_outside_the_window(self):
        query = SearchQuery.objects.create(
            user=self.user, query='键盘', result_count=6,
        )
        SearchQuery.objects.filter(pk=query.pk).update(
            created_at=self.now - timedelta(days=45),
        )
        item = make_item(self.user, self.category, '很久以前的商品')
        SearchImpression.objects.create(search_query=query, item=item, position=1)

        quality = build_search_position_quality(days=30, now=self.now)
        self.assertEqual(quality['metrics']['impressions'], 0)

    def test_the_decay_flag_constant_is_stated_once_and_reused(self):
        # 这个常量是“衰减是否算显著”的唯一口径，测试里直接引用它，
        # 免得阈值在实现里悄悄变了一个方向而没人发现。
        self.assertEqual(DECAY_FLAG_POINTS, 15)
        self.assertEqual(MIN_BUCKET_SAMPLE, 12)
