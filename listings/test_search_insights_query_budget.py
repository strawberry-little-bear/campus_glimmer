# -*- coding: utf-8 -*-
"""搜索词分析（`build_search_insights`）的口径护栏和查询预算。

早先 `term_rows` 把搜索次数、点击、曝光写在**同一条** `annotate` 里。多表聚合不是
「顺手多 JOIN 两张表」那么无害：数据库会先把 SearchQuery、SearchClick、SearchImpression
三张表 JOIN 成笛卡尔积，再对中间结果的每一行 `COUNT`。一条搜索只要同时被点过、也被
曝光过，它的每一次点击都会按曝光条数重复出现，反之亦然。

实测（同一个搜索词下 2 次搜索、3 次点击、4 次曝光，全部挂在同一条搜索记录上）：

    search_count      13  → 2
    click_count       12  → 3
    impression_count  12  → 4

带 `distinct` 的 `clicked_searches` / `clicked_items` / `unique_users` 反而一直是对的，
所以这几个数字错得极难看出来——页面和 CSV 导出都在展示放大后的值。这不是性能问题，
是**真实的数据错误**。

修法是把点击和曝光各自拆成一次「按搜索词分组」的聚合，再并回内存里的行。这个模块钉住
两件事：

1. **拆开之后每一个数字仍然对**——尤其是点击/曝光分散在同一个词的多条搜索记录上时；
2. **不能再合回去**——任何一条查询都不允许同时 JOIN 点击表和曝光表。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .analytics import build_search_insights
from .models import (
    Category, CampusLocation, Item, SearchClick, SearchImpression, SearchQuery,
)

CLICK_TABLE = SearchClick._meta.db_table.upper()
IMPRESSION_TABLE = SearchImpression._meta.db_table.upper()


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


class SearchInsightsFixtureMixin:
    """一套共用的种子数据，形状刻意做成「一个词压着多条搜索、多条点击、多条曝光」。"""

    def setUp(self):
        self.user = make_user('insight-user')
        self.category = Category.objects.create(name='搜索洞察分类')
        self.location = CampusLocation.objects.create(
            name='图书馆东门', building='东校区', address='图书馆一层东侧',
        )
        self.items = [
            Item.objects.create(
                title='键盘 %d' % index, description='用于搜索洞察测试', price='10.00',
                category=self.category, location=self.location, condition='九成新',
                seller=self.user,
            )
            for index in range(4)
        ]
        self.now = timezone.now()
        self.start, self.end = self.now - timedelta(days=30), timezone.now()


def seed_fanout_term(user, items, now):
    """播种一个会触发笛卡尔积的搜索词：3 条搜索、5 次点击、7 条曝光。

    点击和曝光刻意**分散**在三条搜索记录上（而不是全挂在同一条上），这样 JOIN 之后
    中间行数会是每条搜索的「点击数 x 曝光数」之和：2x3 + 2x2 + 1x2 = 12 行，
    而真实值只有 3 次搜索 / 5 次点击 / 7 条曝光。点击的商品也刻意有交叉：五
    次点击只落在三件不同商品上，所以 `clicked_items` 是 3 而不是 5。
    """
    searches = [
        SearchQuery.objects.create(
            user=user, query='键盘', result_count=2, condition='',
        )
        for _ in range(3)
    ]
    # 每条搜索各自的点击落在哪些商品上、曝光几条，两个数字故意不相等。
    clicks_per_search = ((0, 1), (1, 2), (2,))
    impressions_per_search = (3, 2, 2)
    for search, clicked in zip(searches, clicks_per_search):
        for index in clicked:
            SearchClick.objects.create(
                search_query=search, item=items[index], user=user, position=index + 1,
            )
    for search, impression_count in zip(searches, impressions_per_search):
        for index in range(impression_count):
            SearchImpression.objects.create(
                search_query=search, item=items[index], user=user, position=index + 1,
            )
    return searches


class SearchInsightsFanoutTests(SearchInsightsFixtureMixin, TestCase):
    """第一件：拆开之后，每一个数字都还是对的。"""

    def setUp(self):
        super().setUp()

    def test_counts_are_not_amplified_by_the_click_and_impression_join(self):
        searches = seed_fanout_term(self.user, self.items, self.now)

        report = build_search_insights(30)
        rows = {row['query']: row for row in report['term_rows']}
        row = rows['键盘']

        self.assertEqual(row['search_count'], 3)
        self.assertEqual(row['result_search_count'], 3)
        self.assertEqual(row['zero_result_count'], 0)
        self.assertEqual(row['unique_users'], 1)
        self.assertEqual(row['average_results'], 2)
        # 这三个就是原先被笛卡尔积放大的字段。
        self.assertEqual(row['click_count'], 5)
        self.assertEqual(row['impression_count'], 7)
        self.assertEqual(row['clicked_searches'], len(searches))
        # 五次点击只落在三件不同商品上（第 0、1、2 件，其中第 1、2 件被重复点击）。
        self.assertEqual(row['clicked_items'], 3)
        self.assertEqual(row['zero_result_rate'], 0)
        self.assertEqual(row['click_rate'], 100.0)

    def test_the_most_searched_term_still_sorts_first(self):
        """排序按真实的搜索次数，不按被放大后的次数。"""
        seed_fanout_term(self.user, self.items, self.now)
        SearchQuery.objects.create(
            user=self.user, query='台灯', result_count=2, condition='',
        )
        SearchQuery.objects.create(
            user=self.user, query='台灯', result_count=2, condition='',
        )

        report = build_search_insights(30)

        self.assertEqual(
            [row['query'] for row in report['term_rows']],
            ['键盘', '台灯'],
        )

    def test_a_zero_result_term_keeps_its_gap_numbers(self):
        """没有结果的词：缺口照报，点击类字段全部归零而不报 None。"""
        SearchQuery.objects.create(
            user=self.user, query='台灯', result_count=0, condition='',
        )
        SearchQuery.objects.create(
            user=self.user, query='台灯', result_count=0, condition='',
        )
        SearchQuery.objects.create(
            user=self.user, query='台灯', result_count=2, condition='',
        )

        report = build_search_insights(30)
        rows = {row['query']: row for row in report['term_rows']}
        row = rows['台灯']

        self.assertEqual(row['search_count'], 3)
        self.assertEqual(row['zero_result_count'], 2)
        self.assertEqual(row['zero_result_rate'], 66.7)
        self.assertEqual(row['click_count'], 0)
        self.assertEqual(row['impression_count'], 0)
        self.assertEqual(row['clicked_searches'], 0)
        self.assertEqual(row['clicked_items'], 0)
        self.assertEqual(row['click_rate'], 0)
        self.assertEqual(report['gap_terms'][0]['query'], '台灯')

    def test_clicks_outside_the_window_are_excluded(self):
        search = SearchQuery.objects.create(
            user=self.user, query='键盘', result_count=2, condition='',
        )
        SearchClick.objects.create(
            search_query=search, item=self.items[0], user=self.user, position=1,
        )
        stale_click = SearchClick.objects.create(
            search_query=search, item=self.items[1], user=self.user, position=2,
        )
        SearchClick.objects.filter(pk=stale_click.pk).update(
            created_at=self.now - timedelta(days=40),
        )
        stale_impression = SearchImpression.objects.create(
            search_query=search, item=self.items[1], user=self.user, position=2,
        )
        SearchImpression.objects.filter(pk=stale_impression.pk).update(
            created_at=self.now - timedelta(days=40),
        )

        report = build_search_insights(30)
        rows = {row['query']: row for row in report['term_rows']}

        row = rows['键盘']
        self.assertEqual(row['search_count'], 1)
        self.assertEqual(row['click_count'], 1)
        self.assertEqual(row['impression_count'], 0)

    def test_clicks_on_searches_outside_the_window_are_excluded(self):
        """点击发生在窗口内，但它挂在一条窗口外的搜索记录上，不该算进任何搜索词。"""
        stale_search = SearchQuery.objects.create(
            user=self.user, query='键盘', result_count=2, condition='',
        )
        SearchQuery.objects.filter(pk=stale_search.pk).update(
            created_at=self.now - timedelta(days=40),
        )
        SearchClick.objects.create(
            search_query=stale_search, item=self.items[0], user=self.user, position=1,
        )
        SearchImpression.objects.create(
            search_query=stale_search, item=self.items[0], user=self.user, position=1,
        )
        fresh_search = SearchQuery.objects.create(
            user=self.user, query='键盘', result_count=2, condition='',
        )
        SearchClick.objects.create(
            search_query=fresh_search, item=self.items[1], user=self.user, position=1,
        )

        report = build_search_insights(30)
        rows = {row['query']: row for row in report['term_rows']}

        row = rows['键盘']
        self.assertEqual(row['search_count'], 1)
        self.assertEqual(row['click_count'], 1)
        self.assertEqual(row['clicked_searches'], 1)
        self.assertEqual(row['impression_count'], 0)

    def test_an_anonymous_search_counts_as_a_search_but_not_as_a_user(self):
        """`SearchQuery.user` 可空：匿名搜索要计入搜索次数，但不能凭空造出一个用户。"""
        SearchQuery.objects.create(
            user=None, query='鼠标垫', result_count=5, condition='',
        )

        report = build_search_insights(30)
        rows = {row['query']: row for row in report['term_rows']}

        row = rows['鼠标垫']
        self.assertEqual(row['search_count'], 1)
        self.assertEqual(row['unique_users'], 0)
        self.assertEqual(row['click_rate'], 0)

    def test_the_query_filter_keeps_click_stats_scoped_to_the_same_terms(self):
        """按词过滤时，并回去的点击统计也必须只来自过滤后的那些搜索。"""
        seed_fanout_term(self.user, self.items, self.now)
        other = make_user('insight-other')
        other_search = SearchQuery.objects.create(
            user=other, query='显示器支架', result_count=2, condition='',
        )
        SearchClick.objects.create(
            search_query=other_search, item=self.items[3], user=other, position=1,
        )
        SearchImpression.objects.create(
            search_query=other_search, item=self.items[3], user=other, position=1,
        )

        report = build_search_insights(30, query='键盘')
        rows = {row['query']: row for row in report['term_rows']}

        # 「显示器支架」不含「键盘」，整条都被 icontains 挡掉，所以只剩一个词。
        self.assertEqual(rows['键盘']['search_count'], 3)
        self.assertEqual(rows['键盘']['click_count'], 5)
        self.assertEqual(rows['键盘']['impression_count'], 7)


class SearchInsightsQueryBudgetTests(SearchInsightsFixtureMixin, TestCase):
    """第二件：拆分必须保持拆分。"""

    def setUp(self):
        super().setUp()

    def test_no_single_query_joins_clicks_and_impressions_together(self):
        """防的是「改回一条 annotate」这种退化。

        一条查询里同时出现点击表和曝光表，就意味着数据库要算这两张表的笛卡尔积。
        这是本次修正的根因，所以直接把它当成结构性禁令写下来。
        """
        seed_fanout_term(self.user, self.items, self.now)
        with CaptureQueriesContext(connection) as ctx:
            build_search_insights(30)

        self.assertTrue(ctx.captured_queries)
        for query in ctx.captured_queries:
            sql = query['sql'].upper()
            if CLICK_TABLE in sql and IMPRESSION_TABLE in sql:
                self.fail(
                    '同一条查询里又同时 JOIN 了点击表和曝光表，三表笛卡尔积会跟着回来：%s'
                    % query['sql'][:200],
                )

    def test_each_merge_helper_issues_one_grouped_aggregate(self):
        """两个合并helper各发一条按搜索词分组的聚合，一条都不许多。"""
        from listings.analytics import (
            _merge_term_click_stats, _merge_term_impression_stats,
        )
        seed_fanout_term(self.user, self.items, self.now)
        search_period = SearchQuery.objects.all()
        term_rows = list(search_period.values('query'))

        with CaptureQueriesContext(connection) as ctx:
            _merge_term_click_stats(term_rows, search_period, self.start, self.end)
        self.assertEqual(len(ctx.captured_queries), 1)

        term_rows = list(SearchQuery.objects.values('query'))
        with CaptureQueriesContext(connection) as ctx:
            _merge_term_impression_stats(term_rows, search_period, self.start, self.end)
        self.assertEqual(len(ctx.captured_queries), 1)

    def test_a_click_aggregate_never_joins_the_impression_table(self):
        from listings.analytics import _merge_term_click_stats
        seed_fanout_term(self.user, self.items, self.now)
        search_period = SearchQuery.objects.all()
        term_rows = list(search_period.values('query'))

        with CaptureQueriesContext(connection) as ctx:
            _merge_term_click_stats(term_rows, search_period, self.start, self.end)

        sql = ctx.captured_queries[0]['sql'].upper()
        self.assertIn(CLICK_TABLE, sql)
        self.assertNotIn(IMPRESSION_TABLE, sql)
