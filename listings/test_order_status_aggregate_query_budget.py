# -*- coding: utf-8 -*-
"""交易状态分布的查询预算，和一份逐状态重算的口径 oracle。

运营看板要展示每一种订单状态各有多少笔。原来是按 ``Order.STATUS_CHOICES`` 逐个
``filter(status=...).count()``，七个状态就是七次 COUNT；运营看板是全站数据量最大的
页面，每开一次就多扫七遍订单表。现在换成一次 ``GROUP BY status`` 聚合，查询数从
七次降到一次。

收敛只改查询的形状，不改任何一个数字。这个模块钉住四件事：

1. **口径不能因为收敛而变**——用一套刻意写得笨拙的「一个状态一次 COUNT」当 oracle，
   逐个状态比对；再配一组照着播种计划手推出来的期望值，防止两边同时改错；
2. **窗口边界**——起止两端都含，早一秒的订单不进任何状态；
3. **没有订单的状态也要显示 0**——新增的状态在 choices 里排好了、却还没有订单时，
   面板上必须有一行 0，不能就此消失；
4. **预算不能被拆回去**——把查询数上界写下来，并且要求它不随状态数增长。谁觉得
   「循环里各查一次更清楚」，测试先红。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .analytics import _build_order_status_breakdown
from .models import CampusLocation, Category, Item, Order


# 和 Order.STATUS_CHOICES 一致的一份清单，只给 oracle 和播种用。
# 故意在测试里重抄一份：oracle 必须能独立于被测实现存在，抄一份才可能在实现被改错时红。
STATUS_CHOICES = (
    ('pending', '待卖家确认'),
    ('confirmed', '卖家已确认'),
    ('meeting', '待当面交付'),
    ('completed', '交易完成'),
    ('cancelled', '已取消'),
    ('borrowed', '借用中'),
    ('returned', '已归还'),
)

# (状态, 周期内订单数)。最后一列是照着 `seed_orders` 的播种注释手推的，不是跑出来的。
STATUS_PLAN = (
    ('pending', 2),
    ('confirmed', 1),
    ('meeting', 1),
    ('completed', 3),
    ('cancelled', 1),
    ('borrowed', 1),
    ('returned', 2),
)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def make_scaffold(label):
    """一套彼此独立的分类、地点和买卖双方，标签保证同一测试内不重名。"""
    category = Category.objects.create(name='%s分类' % label)
    location = CampusLocation.objects.create(
        name='%s地点' % label, building='东校区', address='图书馆一层东侧',
    )
    return (
        category,
        location,
        make_user('%s-seller' % label),
        make_user('%s-buyer' % label),
    )


def make_item(category, location, seller, title):
    return Item.objects.create(
        title=title, description='用于状态聚合测试', price='10.00',
        category=category, location=location, condition='九成新', seller=seller,
    )


def make_order(item, buyer, seller, status):
    return Order.objects.create(
        item=item, buyer=buyer, seller=seller,
        agreed_price='10.00', buyer_note='状态聚合测试', status=status,
    )


def naive_status_counts(start, end):
    """一个状态一次 COUNT，最笨也最不容易和实现一起错。"""
    period = Order.objects.filter(created_at__gte=start, created_at__lte=end)
    return {
        status: period.filter(status=status).count()
        for status, _ in STATUS_CHOICES
    }


def period_for(start, end):
    return Order.objects.filter(created_at__gte=start, created_at__lte=end)


def seed_orders(now):
    """按 `STATUS_PLAN` 播种，另外放一笔周期外的订单。

    返回 ``(start, end)``，oracle 用它复算同一个窗口。``end`` 刻意在播种之后取：
    播种用的 ``now`` 只是窗口的锚点，真实订单都落在它之后，拿它当上界会把全部订单
    挡在窗口外。

    待确认、已确认、待交付、借用中四类会命中 ``unique_active_order_per_item`` 的
    唯一约束，所以各占一件商品；已完成、已取消、已归还可以共用商品。
    """
    category, location, seller, buyer = make_scaffold('状态聚合')

    def add(title, status, count=1):
        item = make_item(category, location, seller, title)
        for _ in range(count):
            make_order(item, buyer, seller, status)

    # 七种状态各来一些：2 + 1 + 1 + 3 + 1 + 1 + 2 = 11 笔。
    add('待确认商品一', 'pending')
    add('待确认商品二', 'pending')
    add('已确认商品', 'confirmed')
    add('待交付商品', 'meeting')
    add('已完成商品', 'completed', count=3)
    add('已取消商品', 'cancelled')
    add('借用中商品', 'borrowed')
    add('已归还商品', 'returned', count=2)

    # 周期外的一笔：状态和周期内的一模一样，但只要窗口边界错开就会多算一笔。
    stale = make_order(
        make_item(category, location, seller, '周期外商品'), buyer, seller, 'completed',
    )
    Order.objects.filter(pk=stale.pk).update(created_at=now - timedelta(days=40))

    return now - timedelta(days=30), timezone.now()


class OrderStatusAggregateOracleTests(TestCase):
    """第一件：收敛之后的每一个数字，和逐状态笨重算的结果逐格相同。"""

    def test_every_status_matches_a_naive_per_status_recount(self):
        start, end = seed_orders(timezone.now())
        rows = _build_order_status_breakdown(period_for(start, end))

        self.assertEqual(
            {row['key']: row['count'] for row in rows},
            naive_status_counts(start, end),
        )

    def test_the_seeded_scenario_lands_on_the_planned_counts(self):
        """把 oracle 再钉成一组写死的期望值。

        只有 oracle 一致还不够：万一两边同时改错，测试还是绿的。这组数字是照着
        `seed_orders` 的播种注释手推出来的。
        """
        start, end = seed_orders(timezone.now())
        rows = _build_order_status_breakdown(period_for(start, end))

        self.assertEqual(
            {row['key']: row['count'] for row in rows},
            dict(STATUS_PLAN),
        )
        # 七种状态一行不少，一种也不多，顺序也和 choices 一致。
        self.assertEqual([row['key'] for row in rows], [status for status, _ in STATUS_PLAN])

    def test_orders_outside_the_window_are_excluded(self):
        """周期外那笔已完成的订单不能计入，否则 completed 会从 3 变成 4。"""
        start, end = seed_orders(timezone.now())
        rows = _build_order_status_breakdown(period_for(start, end))

        counts = {row['key']: row['count'] for row in rows}
        self.assertEqual(counts['completed'], 3)
        self.assertEqual(sum(counts.values()), 11)

    def test_a_status_without_orders_reports_zero(self):
        """choices 里排了、但还没有订单的状态，面板上必须有一行 0。

        这是收敛时最容易顺手改丢的一条：直接按 GROUP BY 的结果渲染，没订单的状态
        就整行消失，运营会以为这个状态不存在。

        这个用例自己起一套数据，窗口也从播种那一刻才起算，不和别的用例共用
        `seed_orders` 的那 11 笔。
        """
        category, location, seller, buyer = make_scaffold('零订单')
        start = timezone.now()
        make_order(make_item(category, location, seller, '唯一商品'), buyer, seller, 'completed')

        rows = _build_order_status_breakdown(period_for(start, timezone.now()))

        counts = {row['key']: row['count'] for row in rows}
        self.assertEqual(len(rows), len(STATUS_CHOICES))
        self.assertEqual(counts['completed'], 1)
        self.assertEqual(counts['pending'], 0)
        self.assertEqual(counts['returned'], 0)
        self.assertEqual(counts['borrowed'], 0)

    def test_keys_and_labels_come_from_the_choices_tuple(self):
        """key 和中文标签都取自 STATUS_CHOICES，不是从数据库里现翻的。"""
        start, end = seed_orders(timezone.now())
        rows = _build_order_status_breakdown(period_for(start, end))

        self.assertEqual(
            [(row['key'], row['label']) for row in rows],
            [(status, label) for status, label in STATUS_CHOICES],
        )

    def test_a_status_outside_the_choices_is_not_surfaced(self):
        """库里出现 choices 之外的状态时不炸，也不冒出一行没有中文标签的数据。

        这是收敛前后一致的行为：两条路都只遍历 ``STATUS_CHOICES``。写成断言是为了
        让这个取舍是显式的——真要显示脏状态，该改的是 choices 本身。
        """
        category, location, seller, buyer = make_scaffold('脏状态')
        start = timezone.now()
        make_order(
            make_item(category, location, seller, '脏状态商品'), buyer, seller,
            'some_future_status',
        )

        rows = _build_order_status_breakdown(period_for(start, timezone.now()))

        keys = [row['key'] for row in rows]
        self.assertEqual(keys, [status for status, _ in STATUS_CHOICES])
        self.assertNotIn('some_future_status', keys)


class OrderStatusQueryBudgetTests(TestCase):
    """第二件：一次聚合必须保持是一次，而且不能随状态数增长。"""

    def test_the_breakdown_costs_a_single_grouped_aggregate(self):
        start, end = seed_orders(timezone.now())
        with CaptureQueriesContext(connection) as ctx:
            _build_order_status_breakdown(period_for(start, end))

        # 七种状态一次 GROUP BY 就够，一次都不许多。收敛前这里是七次 COUNT。
        self.assertEqual(
            len(ctx.captured_queries), 1,
            '状态分布又变成逐个 COUNT 了：%s' % [q['sql'][:80] for q in ctx.captured_queries],
        )

    def test_the_query_count_does_not_grow_with_the_status_count(self):
        """状态从两种涨到七种，查询数必须原地不动。

        单次看七次 COUNT 和一次聚合都能跑完，所以只有把状态数压上去，「拆回循环」
        这种退化才会露出尾巴。这是本轮真正要防的那件事。
        """
        def query_count_for(round_index, plan):
            category, location, seller, buyer = make_scaffold('预算%d' % round_index)
            # 窗口从这一轮播种前一刻起算，把上一轮留下的订单隔在外面。
            start = timezone.now()
            order_index = 0
            for status, count in plan:
                for _ in range(count):
                    # 每笔订单独占一件商品：待确认、已确认、待交付、借用中四类会命中
                    # unique_active_order_per_item，共用商品会直接撞唯一约束。
                    item = make_item(
                        category, location, seller,
                        '预算商品 %d-%d' % (round_index, order_index),
                    )
                    order_index += 1
                    make_order(item, buyer, seller, status)
            with CaptureQueriesContext(connection) as ctx:
                rows = _build_order_status_breakdown(period_for(start, timezone.now()))
            return len(ctx.captured_queries), rows

        small_count, small_rows = query_count_for(0, [('pending', 1), ('completed', 1)])
        large_count, large_rows = query_count_for(1, STATUS_PLAN)

        self.assertEqual(small_count, large_count)
        self.assertEqual(small_count, 1)
        # 两个规模都要真的产出非零计数，否则这组断言是空转的。
        self.assertEqual(
            {row['key']: row['count'] for row in small_rows if row['count']},
            {'pending': 1, 'completed': 1},
        )
        self.assertEqual(
            {row['key']: row['count'] for row in large_rows if row['count']},
            dict(STATUS_PLAN),
        )

    def test_the_single_query_is_a_group_by(self):
        """那唯一的一条查询必须是 GROUP BY，不是把七个子查询拼成一条 UNION。

        拼成一条 UNION 查询数也等于 1，但数据库仍然要跑七遍聚合，省下的只是往返。
        """
        start, end = seed_orders(timezone.now())
        with CaptureQueriesContext(connection) as ctx:
            _build_order_status_breakdown(period_for(start, end))

        sql = ctx.captured_queries[0]['sql'].upper()
        self.assertIn('GROUP BY', sql)
        self.assertNotIn('UNION', sql)
        self.assertIn('LISTINGS_ORDER', sql)

    def test_an_empty_window_still_costs_one_query(self):
        start = timezone.now() - timedelta(days=30)
        with CaptureQueriesContext(connection) as ctx:
            rows = _build_order_status_breakdown(period_for(start, timezone.now()))

        self.assertEqual(len(ctx.captured_queries), 1)
        self.assertEqual([row['count'] for row in rows], [0] * len(STATUS_CHOICES))
