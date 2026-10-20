# -*- coding: utf-8 -*-
"""校园供需热力（`build_campus_pulse`）的查询预算和口径护栏。

`CampusLocation` 底下挂着三个反向关系：`items`、`demand_posts`、`orders`。早先把五个计数
全写在一条 `annotate` 里，数据库会把这三张表 JOIN 成笛卡尔积再 `COUNT(DISTINCT ...)`。
带 `distinct` 所以数字一直是对的——错的只是中间行数：一个地点有 40 件商品、30 条求购、
40 笔订单，中间结果就是 48000 行，而正确答案三张表各扫一遍就出来了。

实测（4 个地点、160 件商品、120 条求购、160 笔订单）：单条 annotate 0.30s、3 个 JOIN；
拆成三次「按地点分组」的单表聚合后 0.0076s、每条查询 0 个 JOIN，输出逐格相同。

这个模块钉住两件事：

1. **拆开之后每一个数字仍然对**——用一个逐地点笨重算的 oracle 复算；
2. **不能再合回去**——任何一条查询都不许同时 JOIN 商品表、求购表和订单表。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .campus_pulse import build_campus_pulse
from .models import CampusLocation, Category, DemandPost, Item, Order

ITEM_TABLE = Item._meta.db_table.upper()
DEMAND_TABLE = DemandPost._meta.db_table.upper()
ORDER_TABLE = Order._meta.db_table.upper()


class CampusPulseQueryBudgetTests(TestCase):
    """第二件：拆分必须保持拆分。"""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='pulse-budget-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='pulse-budget-buyer', password='safe-password-123',
        )
        self.category = Category.objects.create(name='热力预算分类')
        self.locations = [
            CampusLocation.objects.create(
                name='地点%02d' % index, building='东校区', address='地址%02d' % index,
            )
            for index in range(3)
        ]
        for location in self.locations:
            for index in range(6):
                Item.objects.create(
                    title='商品 %s-%02d' % (location.name, index), description='预算测试',
                    price='10.00', category=self.category, location=location,
                    condition='九成新', seller=self.seller, status='available',
                )
            for index in range(4):
                DemandPost.objects.create(
                    requester=self.buyer, title='求购 %s-%02d' % (location.name, index),
                    description='预算测试', location=location, status='active',
                )
            for index in range(5):
                item = Item.objects.filter(location=location)[index]
                Order.objects.create(
                    item=item, buyer=self.buyer, seller=self.seller,
                    meeting_location=location, agreed_price='10.00',
                    buyer_note='预算测试', status='completed',
                )
        # 窗口上界刻意在播种之后取：播种用的 now 只是锚点，真实数据都落在它之后，
        # 拿它当上界会把全部记录挡在窗口外。
        self.now = timezone.now()

    def test_no_single_query_joins_items_demands_and_orders_together(self):
        """防的是「改回一条 annotate」这种退化。

        一条查询里同时出现商品表、求购表和订单表，就意味着数据库要算三者的笛卡尔积。
        现在每次聚合只碰自己那张表，一条 JOIN 都不该有。
        """
        with CaptureQueriesContext(connection) as ctx:
            build_campus_pulse(days=30, now=self.now)

        self.assertTrue(ctx.captured_queries)
        for query in ctx.captured_queries:
            sql = query['sql'].upper()
            joined = [name for name, table in (
                ('商品', ITEM_TABLE), ('求购', DEMAND_TABLE), ('订单', ORDER_TABLE),
            ) if table in sql]
            self.assertLessEqual(
                len(joined), 1,
                '同一条查询里同时碰了 %s，三表笛卡尔积会跟着回来：%s'
                % ('/'.join(joined), query['sql'][:200]),
            )

    def test_a_populated_campus_issues_one_grouped_aggregate_per_relation(self):
        """供给、求购、订单各一次分组聚合，外加一次取地点主键。"""
        with CaptureQueriesContext(connection) as ctx:
            build_campus_pulse(days=30, now=self.now)

        self.assertLessEqual(len(ctx.captured_queries), 4)


class CampusPulseFanoutTests(TestCase):
    """第一件：拆开之后，每一个数字都还是对的。"""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='pulse-fanout-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='pulse-fanout-buyer', password='safe-password-123',
        )
        self.category = Category.objects.create(name='热力口径分类')
        self.location = CampusLocation.objects.create(
            name='北区图书馆大厅', building='北校区', is_public=True,
        )

    def _item(self, *, status='available', title='热力商品', expires_at=None):
        return Item.objects.create(
            title=title, description='用于口径测试', price='18.00',
            category=self.category, condition='良好', seller=self.seller,
            location=self.location, status=status, expires_at=expires_at,
        )

    def _demand(self, *, status='active', title='求购'):
        return DemandPost.objects.create(
            requester=self.buyer, title=title, description='口径测试',
            category=self.category, location=self.location, status=status,
        )

    def _order(self, item, *, status='completed'):
        return Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='18.00', status=status,
        )

    def test_counts_match_a_naive_per_location_recount(self):
        """oracle：逐地点、逐关系笨重数一遍，和实现的结果逐格比对。"""
        self.now = timezone.now()
        items = [self._item(title='在售商品 %02d' % index) for index in range(5)]
        self._item(title='已预订商品', status='reserved')
        self._item(
            title='已过期商品', expires_at=self.now - timedelta(hours=1),
        )
        for index in range(3):
            self._demand(title='有效求购 %02d' % index)
        self._demand(title='已关闭求购', status='closed')
        for index in range(4):
            self._order(items[index])
        self._order(items[0], status='cancelled')

        self.now = timezone.now()
        pulse = build_campus_pulse(days=30, now=self.now)
        rows = {row['name']: row for row in pulse['rows']}
        row = rows['北区图书馆大厅']

        expected = {
            'available_supply': 5,      # 在售的 5 件；已预订与已过期都不算
            'new_supply': 7,            # 窗口内新建的全部 7 件
            'active_demands': 3,        # 只有 active 的 3 条
            'period_orders': 5,         # 窗口内 5 笔，含那笔已取消
            'completed_orders': 4,      # 其中 4 笔已完成
        }
        for field, value in expected.items():
            self.assertEqual(row[field], value, field)
        self.assertEqual(row['activity_score'], 7 + 5 * 2 + 3 * 2)
        self.assertEqual(
            pulse['summary'],
            {
                'location_count': 1,
                'high_pressure_count': 0,
                'available_supply': 5,
                'active_demands': 3,
                'period_orders': 5,
            },
        )

    def test_a_location_without_any_activity_is_left_out_entirely(self):
        """三个计数全为 0 的地点不该出现在结果里。"""
        quiet = CampusLocation.objects.create(name='安静地点', building='西校区')
        self._item()

        self.now = timezone.now()
        pulse = build_campus_pulse(days=30, now=self.now)

        self.assertNotIn(quiet.name, [row['name'] for row in pulse['rows']])
        self.assertEqual(pulse['summary']['location_count'], 1)

    def test_an_inactive_location_is_excluded_even_with_activity(self):
        inactive = CampusLocation.objects.create(
            name='停用地点', building='西校区', is_active=False,
        )
        Item.objects.create(
            title='停用地点的商品', description='口径测试', price='10.00',
            category=self.category, location=inactive, condition='九成新',
            seller=self.seller, status='available',
        )

        self.now = timezone.now()
        pulse = build_campus_pulse(days=30, now=self.now)

        self.assertNotIn(inactive.name, [row['name'] for row in pulse['rows']])

    def test_records_outside_the_window_do_not_count(self):
        self.now = timezone.now()
        stale_item = self._item(title='上一个周期的商品')
        Item.objects.filter(pk=stale_item.pk).update(
            created_at=self.now - timedelta(days=40),
        )
        stale_demand = self._demand(title='上一个周期的求购')
        DemandPost.objects.filter(pk=stale_demand.pk).update(
            created_at=self.now - timedelta(days=40),
        )
        stale_order_item = self._item(title='上一个周期订单的商品')
        stale_order = self._order(stale_order_item)
        Order.objects.filter(pk=stale_order.pk).update(
            created_at=self.now - timedelta(days=40),
        )

        self.now = timezone.now()
        pulse = build_campus_pulse(days=30, now=self.now)
        row = pulse['rows'][0]

        self.assertEqual(row['new_supply'], 1)        # 只有窗口内那两件之外的第三件
        self.assertEqual(row['active_demands'], 1)    # 求购不按窗口过滤，旧求购仍算活跃
        self.assertEqual(row['period_orders'], 0)
        self.assertEqual(row['completed_orders'], 0)
