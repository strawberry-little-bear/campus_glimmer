# -*- coding: utf-8 -*-
"""需求雷达的可用供给查询预算，和一份逐候选重算的口径 oracle。

`build_demand_radar` 给每个候选信号算一次「当前有多少件在售且未过期的商品能对上」。
这个数会进三个地方：面板上的「当前匹配供给 N 件」、证据行、以及机会分里的
``max(0, 3 - available_supply)``——也就是说它直接决定排序和「优先处理/值得补给/
持续观察」三档分级。供给数算错，看板上的排序和分级会一起错，而且看不出来。

原先每个候选各发一次 COUNT。候选数上限是 ``max(limit * 4, limit)``，默认 48，运营
看板一开光是这一个地方就几十次往返；而这些 COUNT 查的是同一张表、同一批「在售且
未过期」的行，差别只在分类、地点、关键词三个收窄条件。现在合成一次
``Count(..., filter=...)`` aggregate，查询数从 50 降到 3。

收敛只改查询的形状，不改任何一个数字。这个模块钉住三件事：

1. **口径不能因为收敛而变**——用一套刻意写得笨拙的「一个候选一次 COUNT」当 oracle，
   逐个候选比对；再配一组照着播种计划手推出来的期望值，防止两边同时改错；
2. **预算不能被拆回去**——把查询数上界写下来。谁觉得「循环里各查一次更清楚」，
   测试先红；
3. **查询数不能随候选数增长**——这是这一轮真正要防的退化：把 aggregate 拆回循环，
   单次看数字没错，候选一多就又是几十次往返。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.db.models import Q
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .demand_radar import build_demand_radar
from .models import CampusLocation, Category, Item, SearchQuery


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def naive_available_supply(term, category_id, location_id, now):
    """一个候选一次 COUNT，最笨也最不容易和实现一起错。"""
    items = Item.objects.filter(status='available').filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now),
    )
    if category_id:
        items = items.filter(category_id=category_id)
    if location_id:
        items = items.filter(location_id=location_id)
    if term:
        items = items.filter(
            Q(title__icontains=term) | Q(description__icontains=term)
        )
    return items.count()


# (搜索词, 分类, 地点, 无结果搜索次数, 可用供给数)。
# 最后一列是照着 `seed_supply_scenarios` 的播种注释手推的，不是跑出来的。
SUPPLY_PLAN = (
    ('台灯', 'a', 'gate', 2, 3),
    ('台灯', 'a', None, 1, 4),
    ('台灯', 'b', None, 1, 1),
    ('充电宝', 'a', 'gate', 3, 5),
    ('充电宝', 'b', 'north', 1, 2),
    ('耳机', 'a', 'gate', 1, 2),
    ('不存在的玩意儿', 'a', 'gate', 1, 0),
)


def seed_supply_scenarios():
    """按 `SUPPLY_PLAN` 播种，返回 ``(anchor, 对象表)``。

    对象表把计划里的 ``'a'`` / ``'gate'`` 这类记号换回真实的模型对象，测试据此拼出
    `radar_key` 去认领自己那一行——**不能按 `label` 认领**：三个「台灯」候选共用同一个
    标签，按标签建字典会互相覆盖，拿到的到底是谁纯看排序。

    ``anchor`` 只是播种时的时间锚点，真实数据都落在它之后，所以调用方要自己再取一次
    ``timezone.now()`` 当窗口上界。
    """
    anchor = timezone.now()
    category_a = Category.objects.create(name='数码设备')
    category_b = Category.objects.create(name='书籍教材')
    gate = CampusLocation.objects.create(
        name='图书馆东门', building='东校区', address='图书馆一层东侧',
    )
    north = CampusLocation.objects.create(name='北门外', building='北侧', address='北门停车场')
    seller = make_user('radar-supply-seller')

    def make_item(title, description='', *, category, location, **extra):
        return Item.objects.create(
            title=title, description=description, price='10.00',
            category=category, location=location, condition='九成新', seller=seller,
            **extra
        )

    # 「台灯」：A 类东门 3 件在售，A 类北门外 1 件在售，B 类东门 1 件在售。
    # 同一批里再混两件不该算的：一件已过期、一件已预订。它们和那 3 件同类同地同
    # 关键词，少任何一条过滤条件，A 类东门那一格就会从 3 变成 5。
    for index in range(3):
        make_item('宿舍台灯 %d' % index, category=category_a, location=gate)
    make_item('床头台灯', category=category_a, location=north)
    make_item('教材台灯笔记', category=category_b, location=gate)
    make_item(
        '过期台灯', category=category_a, location=gate,
        expires_at=anchor - timedelta(days=1),
    )
    make_item('已预订台灯', category=category_a, location=gate, status='reserved')

    # 「充电宝」：A 类东门 5 件，B 类北门外 2 件。
    for index in range(5):
        make_item('充电宝 %d' % index, category=category_a, location=gate)
    for index in range(2):
        make_item('充电宝 %d' % index, category=category_b, location=north)

    # 「耳机」：一件标题命中，一件只写在描述里。描述分支漏掉的话这一格会是 1。
    make_item('降噪耳机', category=category_a, location=gate)
    make_item('头戴听音设备', description='头戴式耳机', category=category_a, location=gate)

    # 一件和任何搜索词都不匹配的商品，保证「搜不到」不是全表为空造成的。
    make_item('无关商品', category=category_b, location=north)

    # 搜索信号：每个计划的信号给足够多的无结果搜索，保证都进得了可见区
    # （limit 默认 12，候选一共 7 个）。
    for index, (term, category_key, location_key, searches, _supply) in enumerate(SUPPLY_PLAN):
        category = {'a': category_a, 'b': category_b, None: None}[category_key]
        location = {'gate': gate, 'north': north, None: None}[location_key]
        for repeat in range(searches):
            SearchQuery.objects.create(
                user=make_user('radar-search-%02d-%d' % (index, repeat)),
                query=term, category=category, location=location, result_count=0,
            )
    objects = {'a': category_a, 'b': category_b, 'gate': gate, 'north': north}
    return anchor, objects


def radar_key_of(term, category, location):
    """和 `demand_radar._radar_key` 同一个拼法，用真实主键而不是记号。"""
    return (
        f'{term}|category:{category.id if category else 0}'
        f'|location:{location.id if location else 0}'
    )


def seeded_rows():
    """跑一次雷达，按 `radar_key` 索引返回。"""
    anchor, objects = seed_supply_scenarios()
    now = timezone.now()
    radar = build_demand_radar(30, now=now)
    return now, objects, {row['radar_key']: row for row in radar['rows']}


class DemandRadarSupplyOracleTests(TestCase):
    """第一件：每个候选的可用供给数，和一次一查的笨实现逐个相同。"""

    def test_every_visible_row_matches_a_naive_per_candidate_count(self):
        now, objects, rows = seeded_rows()
        for (term, category_key, location_key, _searches, _supply) in SUPPLY_PLAN:
            row = rows[radar_key_of(
                term,
                {'a': objects['a'], 'b': objects['b'], None: None}[category_key],
                {'gate': objects['gate'], 'north': objects['north'], None: None}[location_key],
            )]
            expected = naive_available_supply(
                row['term'], row['category_id'], row['location_id'], now,
            )
            self.assertEqual(
                row['available_supply'], expected,
                '%s 的可用供给数和逐次 COUNT 不一致' % row['radar_key'],
            )

    def test_supply_counts_match_the_hand_computed_plan(self):
        """把 oracle 再钉成一组写死的期望值。

        只有 oracle 一致还不够：万一批量版和 oracle 同时改错，测试还是绿的。
        这组数字是照着 `seed_supply_scenarios` 的注释手推出来的。
        """
        _now, objects, rows = seeded_rows()
        self.assertEqual(len(rows), len(SUPPLY_PLAN))
        for term, category_key, location_key, _searches, expected_supply in SUPPLY_PLAN:
            category = {'a': objects['a'], 'b': objects['b'], None: None}[category_key]
            location = {'gate': objects['gate'], 'north': objects['north'], None: None}[location_key]
            row = rows[radar_key_of(term, category, location)]
            self.assertEqual(row['available_supply'], expected_supply, term)
            self.assertEqual(row['term'], term)
            self.assertEqual(row['category_id'], category.id if category else None)
            self.assertEqual(row['location_id'], location.id if location else None)

    def test_expired_and_reserved_items_stay_out_of_the_supply(self):
        """过期和已预订的两件台灯不能进 A 类东门那一格。

        这两件和那 3 件在售的同类同地同关键词，是最容易跟着一起被算进来的。
        """
        _now, objects, rows = seeded_rows()
        row = rows[radar_key_of('台灯', objects['a'], objects['gate'])]
        self.assertEqual(row['available_supply'], 3)
        self.assertNotIn('过期台灯', [row['term']])

    def test_description_only_match_still_counts(self):
        """关键词只出现在描述里的商品也要算进来。

        `title__icontains` 单独一条会把「头戴听音设备」漏掉，耳机那一格从 2 变 1。
        """
        _now, objects, rows = seeded_rows()
        row = rows[radar_key_of('耳机', objects['a'], objects['gate'])]
        self.assertEqual(row['available_supply'], 2)

    def test_supply_drives_the_level_not_just_the_displayed_number(self):
        """供给数同时决定分级和推荐动作，不只决定面板上那个数字。

        「不存在的玩意儿」没有任何供给，``max(0, 3 - available_supply)`` 给它补满 3 分，
        分数因此跨过 6 分线变成「值得补给」。只对显示数字收敛、忘了它参与打分的话，
        这一行会静静地掉回「持续观察」。
        """
        _now, objects, rows = seeded_rows()
        gap = rows[radar_key_of('不存在的玩意儿', objects['a'], objects['gate'])]
        self.assertEqual(gap['available_supply'], 0)
        self.assertEqual(gap['level'], 'high')
        self.assertEqual(gap['level_label'], '值得补给')
        self.assertIn('补充相关供给', gap['recommendations'])
        # 有 5 件供给的「充电宝」不该拿到这条推荐。
        supplied = rows[radar_key_of('充电宝', objects['a'], objects['gate'])]
        self.assertEqual(supplied['available_supply'], 5)
        self.assertNotIn('补充相关供给', supplied['recommendations'])


class DemandRadarQueryBudgetTests(TestCase):
    """第二件：查询预算必须保持，而且不能随候选数增长。"""

    def test_a_full_candidate_list_costs_one_aggregate(self):
        seed_supply_scenarios()
        now = timezone.now()
        with CaptureQueriesContext(connection) as ctx:
            build_demand_radar(30, now=now)

        # 两次取值（无结果搜索、有效求购）加一次供给聚合，一次都不许多。
        # 收敛前这里默认是 2 + 48 = 50 次。
        self.assertLessEqual(
            len(ctx.captured_queries), 3,
            '供给查询又变成逐候选 COUNT 了：%s' % [q['sql'][:80] for q in ctx.captured_queries],
        )

    def test_the_query_count_does_not_grow_with_the_candidate_count(self):
        """候选从几个涨到几十个，查询数必须原地不动。

        单次看 48 次 COUNT 和一次 aggregate 都能跑完，所以只有把候选数量压上去，
        「拆回循环」这种退化才会露出尾巴。这是本轮真正要防的那件事。
        """
        category = Category.objects.create(name='预算分类')
        location = CampusLocation.objects.create(name='图书馆东门')
        for index in range(60):
            Item.objects.create(
                title='预算商品 %d' % index, description='查询预算', price='10.00',
                category=category, location=location, condition='九成新',
                seller=make_user('budget-seller-%02d' % index),
            )

        def query_count_for(round_index, candidate_count):
            for index in range(candidate_count):
                SearchQuery.objects.create(
                    user=make_user('budget-signal-%02d-%02d' % (round_index, index)),
                    query='关键词 %03d' % index, category=category, location=location,
                    result_count=0,
                )
            now = timezone.now()
            with CaptureQueriesContext(connection) as ctx:
                radar = build_demand_radar(30, now=now)
            return len(ctx.captured_queries), radar

        small_count, small_radar = query_count_for(0, 4)
        large_count, large_radar = query_count_for(1, 40)

        self.assertEqual(small_count, large_count)
        self.assertLessEqual(small_count, 3)
        # 两个规模都要真的产出候选，否则这组断言是空转的。
        self.assertEqual(small_radar['summary']['opportunity_count'], 4)
        self.assertEqual(large_radar['summary']['opportunity_count'], 40)

    def test_no_candidates_issue_no_supply_query_at_all(self):
        """一个候选都没有时，连供给聚合都不该发。

        空数据集仍然会为「无结果搜索」和「有效求购」各发一次取值，但不该有第三次。
        """
        now = timezone.now()
        with CaptureQueriesContext(connection) as ctx:
            radar = build_demand_radar(30, now=now)

        self.assertEqual(len(ctx.captured_queries), 2)
        self.assertFalse(radar['has_data'])
        self.assertEqual(radar['rows'], [])

    def test_the_supply_stays_a_single_query(self):
        """供给聚合必须是一条查询，不是按分类或地点拆开的几条。

        按分类拆意味着「多一个分类多一次查询」，而分类表是会随运营增长的那一种。
        """
        seed_supply_scenarios()
        now = timezone.now()
        with CaptureQueriesContext(connection) as ctx:
            build_demand_radar(30, now=now)

        supply_queries = [
            query for query in ctx.captured_queries
            if 'listings_item' in query['sql'] and 'COUNT' in query['sql'].upper()
        ]
        self.assertEqual(len(supply_queries), 1)
