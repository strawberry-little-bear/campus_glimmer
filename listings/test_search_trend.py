# -*- coding: utf-8 -*-
"""Cover the cross-period search trend and filter-preference comparison.

The aggregate comparison already reports total volume, so what these tests pin
down is everything the aggregate cannot see, and every boundary the module
needs to stay trustworthy.

Four things must hold. Shares are compared, never raw counts, because a
platform-wide rise in searches lifts every facet's absolute number and would
otherwise report every facet as rising. Thin evidence is held back instead of
being guessed at, because a term searched twice can swing its zero-result rate
across the whole scale on one search. "No results" and "results nobody clicks"
stay separated, since they call for opposite actions. And nothing is adjusted:
the module reports a comparison and leaves the occurrence threshold that gates
synonym candidates exactly where it was.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import Category, CampusLocation, Item, SearchClick, SearchQuery
from .search_trend import (
    DEFAULT_LIMIT,
    MIN_WINDOW_SEARCHES,
    PRICE_BANDS,
    TREND_DELTA_POINTS,
    _classify_delta,
    _price_band_filter,
    _rate,
    build_search_trend,
)


def _local_noon_today():
    """参考时刻锹在本地当天正午，而不是运行刻。

    跨周期面板按本地午夜切窗口，而测试里的时间戳往前推了几天。
    如果参考时刻就取运行刻，那么每当本地时间走过午夜、UTC
    还停在前一天的那几个小时，“今天”的搜索就会落到窗口上界之外。
    正午既跑完了当天该有的搜索，也远远跑不出本地这一天，
    于是无论什么时候跑这个测试，结果都一样。
    """
    from django.utils import timezone
    today = timezone.localdate()
    noon = timezone.datetime.combine(today, timezone.datetime.min.time()) + timedelta(hours=12)
    return timezone.make_aware(noon)


class SearchTrendSetupTests(TestCase):
    """Window construction and the boundaries the comparison is built on."""

    def setUp(self):
        self.user = User.objects.create_user(username='trend-user', password='safe-password-123')
        self.now = _local_noon_today()

    def test_windows_cover_the_same_number_of_calendar_days(self):
        trend = build_search_trend(days=7, now=self.now)
        current_span = trend['current_period_end'].date() - trend['current_period_start'].date()
        previous_span = trend['current_period_start'].date() - trend['previous_period_start'].date()
        # 两个窗口覆盖的日期数相等，且首尾相接。本周期还没过完，
        # 末尾是一个未完成的当天，因此它按小时数短一点——这不能当成缺陷，
        # 否则每个当前周期都会因为“少了几个小时”而被报告成下降。
        self.assertEqual(current_span.days, 6)
        self.assertEqual(previous_span.days, 7)
        self.assertEqual(trend['previous_period_start'].time(), time.min)
        self.assertEqual(trend['current_period_start'].time(), time.min)
        self.assertEqual(trend['period_days'], 7)

    def test_the_current_window_runs_up_to_now_not_to_midnight(self):
        trend = build_search_trend(days=7, now=self.now)
        # 上界就是现在，不是当天零点：否则今天已经发生的搜索会整块丢掉。
        self.assertEqual(trend['current_period_end'], self.now)
        self.assertGreater(trend['current_period_end'], trend['current_period_start'])

    def test_a_search_in_the_previous_window_is_not_counted_as_current(self):
        self.make_search('台灯', days_ago=10, result_count=3)
        trend = build_search_trend(days=7, now=self.now)
        self.assertEqual(trend['current_searches'], 0)
        self.assertEqual(trend['previous_searches'], 1)
        self.assertEqual(trend['previous_terms'], 1)

    def test_an_empty_history_reports_no_data_rather_than_zeroes(self):
        trend = build_search_trend(days=30, now=self.now)
        self.assertFalse(trend['has_data'])
        self.assertEqual(trend['term_trends'], [])
        self.assertEqual(trend['lead_comparison'], [])
        self.assertEqual(trend['volume_change']['change_display'], '\u2014')

    def test_a_brand_new_period_reports_growth_without_a_previous_baseline(self):
        for offset in range(3):
            self.make_search('显示器', days_ago=offset, result_count=5)
        trend = build_search_trend(days=7, now=self.now)
        # 上一周期完全没有搜索时，报“新增”而不是“上涨 100%”——除以零得到的
        # 百分比是一个没有意义的数字。
        self.assertEqual(trend['volume_change']['change_display'], '新增')
        self.assertEqual(trend['volume_change']['direction'], 'up')

    def test_flat_volume_is_reported_as_flat_not_as_zero_percent(self):
        for offset in range(3):
            self.make_search('键盘', days_ago=offset, result_count=2)
            self.make_search('键盘', days_ago=offset + 7, result_count=2)
        trend = build_search_trend(days=7, now=self.now)
        self.assertEqual(trend['volume_change']['direction'], 'flat')
        self.assertEqual(trend['volume_change']['change_display'], '持平')

    def test_direction_counts_only_cover_terms_that_can_be_judged(self):
        for offset in range(4):
            self.make_search('课本', days_ago=offset, result_count=1)
            self.make_search('课本', days_ago=offset + 7, result_count=1)
        trend = build_search_trend(days=7, now=self.now)
        # 样本足够的词进 flat，样本不足的词单独计数，两个桶不混。
        self.assertEqual(trend['direction_counts'].get('flat'), 1)
        self.assertEqual(trend['direction_counts'].get('insufficient', 0), 0)

    def make_search(self, term, *, days_ago, result_count=0, **kwargs):
        query = SearchQuery.objects.create(
            user=self.user, query=term, result_count=result_count, **kwargs,
        )
        SearchQuery.objects.filter(pk=query.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        return query


class TermTrendTests(TestCase):
    """Per-term volume and quality directions."""

    def setUp(self):
        self.user = User.objects.create_user(username='trend-term-user', password='safe-password-123')
        self.now = _local_noon_today()
        # 模块读到 now 为止，而“今天”的搜索时间戳等于 setUp 取到的 now，
        # 它的点击又在五分钟之后，会超过上界。把参考时刻往后推一段，
        # 同一天的点击才落在窗口内，这也是其他测试已经在用的做法。
        self.reference = self.now + timedelta(minutes=30)
        self.click_category = Category.objects.create(name='趋势点击分类')

    def search(self, term, *, days_ago, result_count=0):
        query = SearchQuery.objects.create(
            user=self.user, query=term, result_count=result_count,
        )
        SearchQuery.objects.filter(pk=query.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        return query

    def test_a_term_that_gains_traffic_is_marked_rising(self):
        # 基线搜索落在上一周期（7 天前开始的那一段），本周期再搜 4 次；
        # 若基线跑到本周期里，这个词就会被读成“新出现”而不是“上升”。
        # 上一周期 3 次、本周期 4 次，两边都超过最小样本量才能判方向；
        # 否则正确行为是报“样本不足”，而不是报“上升”。
        for offset in range(3):
            self.search('台灯', days_ago=10 - offset, result_count=2)
        for offset in range(4):
            self.search('台灯', days_ago=offset, result_count=2)
        trend = build_search_trend(days=7, now=self.now)
        row = self.row_for(trend, '台灯')
        self.assertEqual(row['direction'], 'rising')
        self.assertEqual(row['current_searches'], 4)
        self.assertEqual(row['previous_searches'], 3)
        self.assertEqual(row['delta'], 1)

    def test_a_term_that_only_appears_now_is_new_not_rising(self):
        for offset in range(4):
            self.search('露营椅', days_ago=offset, result_count=1)
        trend = build_search_trend(days=7, now=self.now)
        # “本期新出现”和“上升”不是一回事：新词没有基线，拿它算百分比会得到
        # 一个从零除出来的数字，所以单独一类。
        self.assertEqual(self.row_for(trend, '露营椅')['direction'], 'new')

    def test_a_term_that_stops_being_searched_is_gone_not_falling(self):
        for offset in range(4):
            self.search('旧书', days_ago=offset + 7, result_count=1)
        trend = build_search_trend(days=7, now=self.now)
        self.assertEqual(self.row_for(trend, '旧书')['direction'], 'gone')
        self.assertEqual(self.row_for(trend, '旧书')['current_searches'], 0)

    def test_a_term_below_the_sample_floor_is_held_back(self):
        self.search('手办', days_ago=9, result_count=0)
        self.search('手办', days_ago=1, result_count=0)
        trend = build_search_trend(days=7, now=self.now)
        row = self.row_for(trend, '手办')
        # 上一周期 1 次、本周期 1 次，两边都没达到最小样本量；
        # 此时无结果率可以是 0% 也可以是 100%，判方向等于把噪声当信号。
        self.assertEqual(row['direction'], 'insufficient')
        self.assertEqual(row['current_searches'], 1)
        self.assertEqual(row['previous_searches'], 1)

    def test_zero_result_rate_change_is_reported_in_percentage_points(self):
        for offset in range(3):
            self.search('耳机', days_ago=10 - offset, result_count=0)
        for offset in range(3):
            self.search('耳机', days_ago=offset, result_count=4)
        trend = build_search_trend(days=7, now=self.now)
        row = self.row_for(trend, '耳机')
        # 从 100% 无结果到 0%，变化是 -100 个百分点，不是 -100%。
        self.assertEqual(row['previous_zero_result_rate'], 100.0)
        self.assertEqual(row['current_zero_result_rate'], 0.0)
        self.assertEqual(row['zero_result_delta'], -100.0)

    def test_click_rate_is_measured_only_among_searches_that_had_results(self):
        for offset in range(3):
            query = self.search('鼠标', days_ago=10 - offset, result_count=4)
            self.click(query)
        for offset in range(3):
            query = self.search('鼠标', days_ago=offset, result_count=4)
        trend = build_search_trend(days=7, now=self.reference)
        row = self.row_for(trend, '鼠标')
        self.assertEqual(row['previous_click_rate'], 100.0)
        self.assertEqual(row['current_click_rate'], 0.0)
        self.assertEqual(row['click_delta'], -100.0)

    def test_a_term_with_no_clicks_at_all_reports_none_rather_than_zero(self):
        self.search('路由器', days_ago=9, result_count=0)
        self.search('路由器', days_ago=1, result_count=0)
        trend = build_search_trend(days=7, now=self.now)
        row = self.row_for(trend, '路由器')
        # 上一周期这次搜索没有结果，点击率无从计算。报 0% 会让一个没被展示过
        # 的词看起来“展示了但没人点”，这是两种完全不同的情况。
        self.assertIsNone(row['previous_click_rate'])
        self.assertIsNone(row['click_delta'])

    def test_terms_are_capped_by_the_limit(self):
        for index in range(DEFAULT_LIMIT + 6):
            self.search(f'词{index:02d}', days_ago=1, result_count=2)
            self.search(f'词{index:02d}', days_ago=8, result_count=2)
        trend = build_search_trend(days=7, now=self.now)
        self.assertEqual(len(trend['term_trends']), DEFAULT_LIMIT)
        self.assertEqual(trend['term_total'], DEFAULT_LIMIT + 6)

    def click(self, query):
        """Record a click for this search, dated to sit next to the search.

        The click is back-dated to the search's own moment rather than left at
        "now", because the module counts clicks inside each window: a click
        stamped now belongs to the current window even when the search it
        belongs to was made last week.
        """
        item = Item.objects.create(
            title=f'测试商品 {query.pk}', description='用于点击统计',
            price=Decimal('10.00'), seller=self.user, status='available',
            category=self.click_category,
        )
        click = SearchClick.objects.create(search_query=query, item=item, user=self.user)
        SearchClick.objects.filter(pk=click.pk).update(
            created_at=SearchQuery.objects.get(pk=query.pk).created_at + timedelta(minutes=5),
        )
        return item

    def row_for(self, trend, term):
        for row in trend['term_trends']:
            if row['query'] == term:
                return row
        raise AssertionError(f'{term} not in term_trends')


class FacetShiftTests(TestCase):
    """Filter-preference migration, measured as shares rather than counts."""

    def setUp(self):
        self.user = User.objects.create_user(username='trend-facet-user', password='safe-password-123')
        self.now = _local_noon_today()
        self.category_a = Category.objects.create(name='趋势分类甲')
        self.category_b = Category.objects.create(name='趋势分类乙')
        self.location_a = CampusLocation.objects.create(name='趋势地点甲')
        self.location_b = CampusLocation.objects.create(name='趋势地点乙')

    def search(self, *, days_ago, condition='', category=None, location=None,
               min_price=None, max_price=None, result_count=3):
        query = SearchQuery.objects.create(
            user=self.user, query='查询', condition=condition,
            category=category, location=location,
            min_price=min_price, max_price=max_price, result_count=result_count,
        )
        SearchQuery.objects.filter(pk=query.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        return query

    def test_shares_are_compared_not_raw_counts(self):
        # 上一周期 4 次带分类筛选的搜索里甲占一半；本周期总量翻倍，甲仍是 4 次。
        # 若按绝对次数比，甲的“占比上升”会被误报；按占比比，甲其实是下降的。
        for _ in range(2):
            self.search(days_ago=8, category=self.category_a)
            self.search(days_ago=8, category=self.category_b)
        for _ in range(4):
            self.search(days_ago=1, category=self.category_b)
        for _ in range(2):
            self.search(days_ago=1, category=self.category_a)
        trend = build_search_trend(days=7, now=self.now)
        category = self.facet_for(trend, 'category')
        row_a = self.value_for(category, self.category_a.id)
        row_b = self.value_for(category, self.category_b.id)
        self.assertEqual(row_a['current_count'], 2)
        self.assertEqual(row_b['current_count'], 4)
        self.assertEqual(row_a['current_share'], 33.3)
        self.assertEqual(row_a['previous_share'], 50.0)
        self.assertEqual(row_a['direction'], 'falling')
        self.assertEqual(row_b['direction'], 'rising')

    def test_a_platform_wide_rise_does_not_make_every_facet_rise(self):
        # 这正是占比口径要防的情况：总量涨三倍，每个取值的绝对次数都涨，
        # 但占比没变，就不能说筛选偏好迁移了。
        for _ in range(2):
            self.search(days_ago=8, condition='九成新')
            self.search(days_ago=8, condition='全新')
        for _ in range(4):
            self.search(days_ago=1, condition='九成新')
            self.search(days_ago=1, condition='全新')
        trend = build_search_trend(days=7, now=self.now)
        condition = self.facet_for(trend, 'condition')
        self.assertEqual(condition['current_total'], 8)
        self.assertEqual(condition['previous_total'], 4)
        self.assertEqual(
            [(row['direction']) for row in condition['rows']],
            ['flat', 'flat'],
        )

    def test_price_bands_are_derived_from_the_bounds_the_student_set(self):
        self.search(days_ago=8, max_price=Decimal('0'))
        self.search(days_ago=1, min_price=Decimal('0'), max_price=Decimal('0'))
        self.search(days_ago=1, min_price=Decimal('10.00'), max_price=Decimal('40.00'))
        trend = build_search_trend(days=7, now=self.now)
        price = self.facet_for(trend, 'price')
        free = self.value_for(price, 'free')
        under_50 = self.value_for(price, 'under_50')
        # 只设上限 0 和上下限都设 0，都算免费带——判据是“设了的界落在带内”，
        # 而不是“两个界都必须设”。
        self.assertEqual(free['current_count'], 1)
        self.assertEqual(under_50['current_count'], 1)

    def test_a_search_with_no_price_bounds_belongs_to_no_band(self):
        self.search(days_ago=1)
        self.search(days_ago=8)
        trend = build_search_trend(days=7, now=self.now)
        price = self.facet_for(trend, 'price')
        # 没有价格条件的搜索不提供任何价格偏好证据，不计入任何价格带，因此
        # 价格带总量为 0。
        self.assertEqual(price['current_total'], 0)

    def test_a_facet_with_too_few_searches_is_held_back(self):
        self.search(days_ago=1, condition='九成新')
        self.search(days_ago=8, condition='九成新')
        trend = build_search_trend(days=7, now=self.now)
        condition = self.facet_for(trend, 'condition')
        # 分母只有 1 次，占比只能是 0% 或 100%，此时判方向同样是噪声。
        self.assertEqual(condition['rows'][0]['direction'], 'insufficient')

    def test_a_facet_used_only_this_period_is_new(self):
        for _ in range(5):
            self.search(days_ago=1, location=self.location_a)
        for _ in range(5):
            self.search(days_ago=8, location=self.location_b)
        trend = build_search_trend(days=7, now=self.now)
        location = self.facet_for(trend, 'location')
        self.assertEqual(self.value_for(location, self.location_a.id)['direction'], 'new')
        self.assertEqual(self.value_for(location, self.location_b.id)['direction'], 'gone')

    def test_foreign_key_facets_carry_a_readable_label(self):
        self.search(days_ago=1, category=self.category_a)
        trend = build_search_trend(days=7, now=self.now)
        category = self.facet_for(trend, 'category')
        # 地点与分类都要显示名字而不是主键，否则运营读不懂这一行。
        self.assertEqual(self.value_for(category, self.category_a.id)['label'], '趋势分类甲')

    def facet_for(self, trend, kind):
        for facet in trend['facet_shifts']:
            if facet['kind'] == kind:
                return facet
        raise AssertionError(f'{kind} not in facet_shifts')

    def value_for(self, facet, key):
        for row in facet['rows']:
            if row['key'] == key:
                return row
        raise AssertionError(f'{key} not in {facet["kind"]} rows')


class LeadComparisonTests(TestCase):
    """Separating "we do not stock it" from "we stock it and nobody clicks"."""

    def setUp(self):
        self.user = User.objects.create_user(username='trend-lead-user', password='safe-password-123')
        self.now = _local_noon_today()
        self.reference = self.now + timedelta(minutes=30)
        self.click_category = Category.objects.create(name='线索点击分类')

    def search(self, term, *, days_ago, result_count):
        query = SearchQuery.objects.create(
            user=self.user, query=term, result_count=result_count,
        )
        SearchQuery.objects.filter(pk=query.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        return query

    def click(self, query):
        item = Item.objects.create(
            title=f'测试商品 {query.pk}', description='用于点击统计',
            price=Decimal('10.00'), seller=self.user, status='available',
            category=self.click_category,
        )
        SearchClick.objects.create(search_query=query, item=item, user=self.user)

    def test_a_term_with_no_results_is_reported_as_a_supply_gap(self):
        for offset in range(3):
            self.search('露营椅', days_ago=offset, result_count=0)
        trend = build_search_trend(days=7, now=self.now)
        row = self.lead_for(trend, '露营椅')
        self.assertEqual(row['kind'], 'gap')
        self.assertEqual(row['kind_label'], '搜不到')
        self.assertEqual(row['zero_result_count'], 3)
        self.assertEqual(row['no_click_count'], 0)

    def test_a_term_with_results_nobody_clicks_is_reported_separately(self):
        for offset in range(3):
            self.search('旧书', days_ago=offset, result_count=6)
        trend = build_search_trend(days=7, now=self.now)
        row = self.lead_for(trend, '旧书')
        # 有结果却没人点，是排序、价格或标题的问题，补货解决不了，所以不能和
        # “搜不到”混在同一类里。
        self.assertEqual(row['kind'], 'no_click')
        self.assertEqual(row['kind_label'], '搜到了不点')
        self.assertEqual(row['zero_result_count'], 0)
        self.assertEqual(row['no_click_count'], 3)

    def test_a_term_with_both_problems_is_marked_as_both(self):
        for offset in range(2):
            self.search('相机', days_ago=offset, result_count=0)
        for offset in range(2):
            self.search('相机', days_ago=offset, result_count=5)
        trend = build_search_trend(days=7, now=self.now)
        row = self.lead_for(trend, '相机')
        self.assertEqual(row['kind'], 'both')
        self.assertEqual(row['zero_result_count'], 2)
        self.assertEqual(row['no_click_count'], 2)

    def test_a_term_with_results_and_clicks_is_not_a_lead(self):
        for offset in range(3):
            query = self.search('键盘', days_ago=offset, result_count=4)
            self.click(query)
        trend = build_search_trend(days=7, now=self.reference)
        # 搜到了也点了，这不是失败线索，不该出现在对比里。
        self.assertEqual(trend['lead_comparison'], [])

    def test_a_click_outside_the_window_does_not_rescue_a_term(self):
        for offset in range(3):
            query = self.search('显示器', days_ago=offset + 7, result_count=4)
            self.click(query)
        for offset in range(3):
            self.search('显示器', days_ago=offset, result_count=4)
        trend = build_search_trend(days=7, now=self.reference)
        row = self.lead_for(trend, '显示器')
        # 上一周期的点击不能算到本周期头上，否则“搜到不点”会被历史点击洗掉。
        self.assertEqual(row['no_click_count'], 3)

    def test_a_click_outside_the_window_does_not_pollute_the_previous_period(self):
        # 本周期前两次搜索都有点击，第三次没有；上一周期的三次全都没有点击。
        # 若把本周期的点击算到上一周期头上，上一周期就会被洗成“有人点”。
        for offset in range(2):
            query = self.search('鼠标', days_ago=offset, result_count=4)
            self.click(query)
        self.search('鼠标', days_ago=2, result_count=4)
        for offset in range(3):
            self.search('鼠标', days_ago=offset + 7, result_count=4)
        trend = build_search_trend(days=7, now=self.reference)
        row = self.lead_for(trend, '鼠标')
        # 本周期只有一次没被点击，上一周期的三次仍然全都算“没人点”。
        self.assertEqual(row['kind'], 'no_click')
        self.assertEqual(row['no_click_count'], 1)

    def lead_for(self, trend, term):
        for row in trend['lead_comparison']:
            if row['query'] == term:
                return row
        raise AssertionError(f'{term} not in lead_comparison')


class HelperTests(TestCase):
    """The small shared helpers, so the thresholds are pinned in one place."""

    def test_rate_returns_none_for_an_empty_window(self):
        self.assertIsNone(_rate(0, 0))
        self.assertEqual(_rate(1, 4), 25.0)

    def test_share_delta_thresholds_match_the_radar_verdicts(self):
        self.assertEqual(_classify_delta(TREND_DELTA_POINTS), 'rising')
        self.assertEqual(_classify_delta(-TREND_DELTA_POINTS), 'falling')
        self.assertEqual(_classify_delta(0), 'flat')
        # 没有可比的差值时返回“样本不足”而不是 None，
        # 因为调用方要拿这个值当方向标签用，None 会让它们在模板里四处报错。
        self.assertEqual(_classify_delta(None), 'insufficient')

    def test_a_price_band_matches_a_search_that_set_only_one_bound(self):
        # 只设上限时，上限落在带内就算命中；两个界都设了却超出带范围就不命中。
        only_upper = SearchQuery.objects.filter(
            _price_band_filter(None, Decimal('50')),
        ).values('pk')
        both_bounds = SearchQuery.objects.filter(
            _price_band_filter(Decimal('0.01'), Decimal('50')),
        ).values('pk')
        # 只设上限的带只在 SQL 上加一个条件，两个界都设的带加两个；
        # 这就是“设了的界落在带内”这句话的具体含义。
        self.assertEqual(only_upper.query.where.children.__len__(), 1)
        self.assertEqual(both_bounds.query.where.children.__len__(), 2)

    def test_minimum_sample_matches_the_radar_outcome_floor(self):
        from .demand_radar_outcome import MIN_WINDOW_SEARCHES as RADAR_FLOOR
        # 两个模块必须在同一个样本量上拒绝下结论，否则同一批数据在两个页面
        # 会得到一个说“够”、一个说“不够”的答案。
        self.assertEqual(MIN_WINDOW_SEARCHES, RADAR_FLOOR)
