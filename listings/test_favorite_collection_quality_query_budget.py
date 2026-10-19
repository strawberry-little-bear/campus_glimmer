# -*- coding: utf-8 -*-
"""给「先收藏后归类」这一节钉住查询预算，并核对四个计数各是什么。

这一节的四个总数（归过类、从未归类、归过又取消、当前全部）原来是四条独立的
`COUNT(*)`，每条都要全表扫一遍。它们是同一张表上的四组筛选，合成一条
`aggregate` 之后从 5 次查询降到 2 次——剩下那 1 次是把 `created_at` 和
`grouped_at` 两列整表读出来的那次，那是故意的，见模块 docstring：时间差必须在
Python 里算，SQLite 是 julianday、PostgreSQL 是 interval，写不进 `extra()`。

这类改动最容易错的地方不是报错，而是**悄悄多数或少数一条**：一个筛选条件在搬进
`Count(..., filter=...)` 时写漏一个字段，四个数照样是四个数、照样能加起来，面板上看
不出任何异常。所以这里的口径测试不重复断言一遍数字，而是用一条 `values_list` 把相关
列原样读出来，在 Python 里重新数一遍，要求两边相等。改错了聚合，oracle 会立刻和它对
不上。

预算测试则把查询数钉死，防止这次合并哪天又悄悄散开成四条。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .favorite_collection_quality import (
    INSTANT_GROUPING_GRACE_MINUTES,
    _build_grouping_latency_rows,
    build_favorite_collection_quality,
)
from .models import CampusLocation, Category, Favorite, FavoriteCollection, Item

# 这一节的查询预算：1 次读 created_at/grouped_at 两列（时间差在 Python 里算），
# 1 次 aggregate 问完四个计数。四类收藏各加多少条都不该多出查询来。
LATENCY_QUERY_BUDGET = 2

# 整个面板的预算：分组 1、窗口内分组与收藏 2、当前快照 4、这一节 2、其余内存计算。
# 实测在 4/16/40 条收藏三种规模下都是这个数。
PANEL_QUERY_BUDGET = 10

INSTANT_COUNT = 5
DELAYED_COUNT = 4
RELEASED_COUNT = 3
UNGROUPED_COUNT = 7


def local_noon():
    """本地正午，参照点远离午夜，免得窗口切在时刻边缘上。"""
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


class FavoriteGroupingLatencyBudgetTests(TestCase):
    """查询数不随收藏条数增长，且四条计数真的合成了一条 aggregate。"""

    def setUp(self):
        self.now = local_noon()
        self.user = User.objects.create_user(
            username='latency-budget', password='safe-password-123',
        )
        self.category = Category.objects.create(name='归类查询预算分类')
        self.location = CampusLocation.objects.create(
            name='归类查询预算地点', building='归类查询预算楼', is_public=True,
        )
        self.collection = self.make_collection(name='宿舍')

    # --- 造数据的帮手 ---

    def make_item(self, title):
        return Item.objects.create(
            title=title, description='用于归类查询预算', price='30.00',
            category=self.category, location=self.location,
            condition='良好', seller=self.user, status='available',
        )

    def make_collection(self, name):
        collection = FavoriteCollection.objects.create(
            user=self.user, name=name,
        )
        FavoriteCollection.objects.filter(pk=collection.pk).update(
            created_at=timezone.now() - timedelta(days=3),
        )
        collection.refresh_from_db()
        return collection

    def make_favorite(self, title, *, collection=None, created_at=None,
                      grouped_at=None):
        """建一条收藏，把 `created_at` 和 `grouped_at` 都写回指定值。

        两列都是 `auto_now_add` 或模型自己盖的章，建行时拿到的是真实墙上时间；不回填
        的话窗口归属和「当场 / 事后」的分档都会跟着墙上时间漂。回填时两列一起给，
        夹具产出的行才自洽——只回填一列会造出「归类早于收藏」这种数据库里不可能有的
        形状。
        """
        favorite = Favorite.objects.create(
            user=self.user, item=self.make_item(title), collection=collection,
        )
        if created_at is None:
            created_at = timezone.now() - timedelta(days=2)
        Favorite.objects.filter(pk=favorite.pk).update(
            created_at=created_at, grouped_at=grouped_at,
        )
        favorite.refresh_from_db()
        return favorite

    def seed(self, *, instant=0, delayed=0, released=0, ungrouped=0):
        """按四类各自的条数铺一份自洽的收藏数据。"""
        created = timezone.now() - timedelta(days=2)
        grace = timedelta(minutes=INSTANT_GROUPING_GRACE_MINUTES)
        for index in range(instant):
            self.make_favorite(
                '当场%d' % index, collection=self.collection,
                created_at=created, grouped_at=created + grace,
            )
        for index in range(delayed):
            self.make_favorite(
                '事后%d' % index, collection=self.collection,
                created_at=created, grouped_at=created + grace + timedelta(minutes=1),
            )
        for index in range(released):
            # 归过类又取消：collection_id 先真的有值再被清空，grouped_at 停在最后一次
            # 变化那一刻。那一时刻在宽限之外，所以这一行进「事后」那一档——四类互斥，
            # 一条收藏只出现在一类里。
            self.make_favorite(
                '归过又取消%d' % index, collection=self.collection,
                created_at=created, grouped_at=created + grace + timedelta(days=5),
            )
            Favorite.objects.filter(item__title='归过又取消%d' % index).update(
                collection=None,
            )
        for index in range(ungrouped):
            self.make_favorite('从未归类%d' % index, grouped_at=None)

    # --- 查询预算 ---

    def test_latency_stays_inside_its_query_budget(self):
        """四类都非空时，这一节仍然只发 2 次查询。"""
        self.seed(instant=INSTANT_COUNT, delayed=DELAYED_COUNT,
                  released=RELEASED_COUNT, ungrouped=UNGROUPED_COUNT)

        with CaptureQueriesContext(connection) as captured:
            latency = _build_grouping_latency_rows(self.now)

        self.assertLessEqual(
            len(captured.captured_queries), LATENCY_QUERY_BUDGET,
            '这一节多发了查询：%s' % [
                query['sql'] for query in captured.captured_queries
            ],
        )
        self.assertEqual(latency['grouped_ever_total'],
                         INSTANT_COUNT + DELAYED_COUNT + RELEASED_COUNT)

    def test_query_count_does_not_grow_with_the_favorite_count(self):
        """查询数只和「问几个问题」有关，和库里有多少条收藏无关。"""
        self.seed(instant=2, delayed=2, released=1, ungrouped=2)
        with CaptureQueriesContext(connection) as small:
            _build_grouping_latency_rows(self.now)

        self.seed(instant=INSTANT_COUNT * 4, delayed=DELAYED_COUNT * 4,
                  released=RELEASED_COUNT * 4, ungrouped=UNGROUPED_COUNT * 4)
        with CaptureQueriesContext(connection) as large:
            _build_grouping_latency_rows(self.now)

        self.assertEqual(
            len(small.captured_queries), len(large.captured_queries),
            '查询数随收藏条数增长了：%d -> %d' % (
                len(small.captured_queries), len(large.captured_queries),
            ),
        )

    def test_the_four_counts_are_a_single_aggregate(self):
        """四个计数必须合成一条 aggregate，不能退回四条 `COUNT(*)`。

        单独看每条查询都是合法的，加起来也对；只有把 SQL 文本摊开看，才能发现合并被
        撤掉了。所以这条测试盯的是查询的形状，不是数字。
        """
        self.seed(instant=3, delayed=3, released=2, ungrouped=4)

        with CaptureQueriesContext(connection) as captured:
            _build_grouping_latency_rows(self.now)

        sqls = [query['sql'] for query in captured.captured_queries]
        aggregate_sqls = [
            sql for sql in sqls
            if 'count' in sql.lower() and 'filter (where' in sql.lower()
        ]
        bare_counts = [
            sql for sql in sqls
            if sql.lower().startswith('select count(*)')
        ]
        self.assertEqual(
            len(aggregate_sqls), 1,
            '四个计数应当合成一条带 filter 的 aggregate，实际：%s' % sqls,
        )
        self.assertEqual(
            bare_counts, [],
            '又出现了不带条件的整表 COUNT：%s' % bare_counts,
        )

    def test_full_panel_stays_inside_its_budget(self):
        """整张面板的查询数也钉住：这一节收敛了，别处不要偷偷加回来。"""
        self.seed(instant=INSTANT_COUNT, delayed=DELAYED_COUNT,
                  released=RELEASED_COUNT, ungrouped=UNGROUPED_COUNT)

        with CaptureQueriesContext(connection) as captured:
            report = build_favorite_collection_quality(days=30, now=self.now)

        self.assertLessEqual(
            len(captured.captured_queries), PANEL_QUERY_BUDGET,
            '面板多发了查询：%s' % [
                query['sql'] for query in captured.captured_queries
            ],
        )
        self.assertEqual(report['grouping_latency']['current_total'],
                         INSTANT_COUNT + DELAYED_COUNT + RELEASED_COUNT
                         + UNGROUPED_COUNT)


class FavoriteGroupingLatencyEquivalenceTests(TestCase):
    """四个计数各自等于一次独立的 Python 重算。"""

    def setUp(self):
        self.now = local_noon()
        self.user = User.objects.create_user(
            username='latency-oracle', password='safe-password-123',
        )
        self.category = Category.objects.create(name='归类口径分类')
        self.location = CampusLocation.objects.create(
            name='归类口径地点', building='归类口径楼', is_public=True,
        )
        self.collection = FavoriteCollection.objects.create(
            user=self.user, name='宿舍',
        )

    def make_item(self, title):
        return Item.objects.create(
            title=title, description='用于归类口径', price='30.00',
            category=self.category, location=self.location,
            condition='良好', seller=self.user, status='available',
        )

    def make_favorite(self, title, *, collection=None, created_at=None,
                      grouped_at=None):
        favorite = Favorite.objects.create(
            user=self.user, item=self.make_item(title), collection=collection,
        )
        if created_at is None:
            created_at = timezone.now() - timedelta(days=2)
        Favorite.objects.filter(pk=favorite.pk).update(
            created_at=created_at, grouped_at=grouped_at,
        )
        favorite.refresh_from_db()
        return favorite

    def seed(self, *, instant, delayed, released, ungrouped, grace_exact=False):
        created = timezone.now() - timedelta(days=2)
        grace = timedelta(minutes=INSTANT_GROUPING_GRACE_MINUTES)
        for index in range(instant):
            self.make_favorite(
                '当场%d' % index, collection=self.collection,
                created_at=created, grouped_at=created + grace,
            )
        for index in range(delayed):
            self.make_favorite(
                '事后%d' % index, collection=self.collection,
                created_at=created, grouped_at=created + grace + timedelta(minutes=1),
            )
        for index in range(released):
            self.make_favorite(
                '归过又取消%d' % index, collection=self.collection,
                created_at=created, grouped_at=created + grace + timedelta(days=5),
            )
            Favorite.objects.filter(item__title='归过又取消%d' % index).update(
                collection=None,
            )
        for index in range(ungrouped):
            self.make_favorite('从未归类%d' % index, grouped_at=None)
        if grace_exact:
            # 边界：时间差正好等于宽限。条件是「小于等于」，所以这一行进当场。
            self.make_favorite(
                '宽限边界', collection=self.collection,
                created_at=created, grouped_at=created + grace,
            )

    def recompute(self):
        """不用任何聚合，把四个数在 Python 里重数一遍。"""
        rows = Favorite.objects.values_list(
            'collection_id', 'grouped_at', 'created_at',
        )
        grace = timedelta(minutes=INSTANT_GROUPING_GRACE_MINUTES)
        grouped_ever = 0
        instant = 0
        ungrouped_now = 0
        released = 0
        for collection_id, grouped_at, created_at in rows:
            if grouped_at is not None:
                grouped_ever += 1
                if grouped_at - created_at <= grace:
                    instant += 1
            if collection_id is None:
                if grouped_at is None:
                    ungrouped_now += 1
                else:
                    released += 1
        return {
            'grouped_ever_total': grouped_ever,
            'instant_total': instant,
            'delayed_total': grouped_ever - instant,
            'ungrouped_total': ungrouped_now,
            'released_total': released,
            'current_total': Favorite.objects.count(),
        }

    def assertMatchesOracle(self, **counts):
        self.seed(**counts)
        latency = _build_grouping_latency_rows(self.now)
        expected = self.recompute()
        for key, value in expected.items():
            self.assertEqual(
                latency[key], value,
                '%s：模块报 %r，Python 重算 %r' % (key, latency[key], value),
            )
        return latency

    def test_every_count_matches_a_python_recount(self):
        latency = self.assertMatchesOracle(
            instant=INSTANT_COUNT, delayed=DELAYED_COUNT,
            released=RELEASED_COUNT, ungrouped=UNGROUPED_COUNT,
        )
        self.assertEqual(latency['grace_minutes'], INSTANT_GROUPING_GRACE_MINUTES)

    def test_counts_stay_matched_when_one_kind_is_empty(self):
        """某一类是空的时也要对得上：0 必须是真零，不能是「没问」。"""
        self.assertMatchesOracle(instant=0, delayed=0, released=0, ungrouped=0)
        self.assertMatchesOracle(instant=0, delayed=0, released=0, ungrouped=5)
        self.assertMatchesOracle(instant=3, delayed=0, released=0, ungrouped=0)

    def test_the_grace_boundary_counts_as_instant(self):
        """时间差正好等于宽限的那一条算当场——分界必须可复现。"""
        latency = self.assertMatchesOracle(
            instant=2, delayed=2, released=1, ungrouped=1, grace_exact=True,
        )
        self.assertEqual(latency['instant_total'], 3)
        self.assertEqual(latency['delayed_total'], 3)

    def test_the_reported_kinds_partition_their_own_totals(self):
        """报出来的几组数必须各自对得上，不能重复也不能漏。

        两个真不变式：

        1. 「当场 + 事后 = 归过类」。归过类的收藏按时间差切成当场和事后，这两档是
           一个分割。
        2. 「归过类 + 从未归类 = 当前全部」。所有收藏要么盖过章，要么没盖过。

        「归过类又取消」**不是**和这两组并列的第四档——它是「事后」的一个子集：这批
        人确实盖过章，只是最后 collection_id 是空的。把它当成并列的一档去加总就会多数
        一遍，而这正是四个数里最容易读错的地方：面板上四个数并排摆着，看着像四类。
        """
        latency = self.assertMatchesOracle(
            instant=INSTANT_COUNT, delayed=DELAYED_COUNT,
            released=RELEASED_COUNT, ungrouped=UNGROUPED_COUNT,
        )
        self.assertEqual(
            latency['instant_total'] + latency['delayed_total'],
            latency['grouped_ever_total'],
        )
        self.assertEqual(
            latency['grouped_ever_total'] + latency['ungrouped_total'],
            latency['current_total'],
        )
        # 归过类又取消是「事后」的子集，不会超出它。
        self.assertLessEqual(
            latency['released_total'], latency['delayed_total'],
        )

    def test_a_released_favorite_is_not_counted_as_ungrouped(self):
        """归过类又取消的不能并进「从未归类」。

        这两类在 SQL 里的差别只有一个 `grouped_at IS NULL`，把条件写串就会把它们合
        成一群——而那正是最不该混的一群：「试过了又退回去」说明分组对他没价值，「从
        来没试过」只是还没走到。
        """
        latency = self.assertMatchesOracle(
            instant=0, delayed=0, released=3, ungrouped=0,
        )
        self.assertEqual(latency['released_total'], 3)
        self.assertEqual(latency['ungrouped_total'], 0)
        self.assertEqual(latency['grouped_ever_total'], 3)
