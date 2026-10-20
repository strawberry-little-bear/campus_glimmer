# -*- coding: utf-8 -*-
"""两处「两路笛卡尔积」的查询预算与口径护栏。

这两处早先把两个**反向**关系的计数写在同一条 `annotate` 里：

* `campaign_list`：`items`（在售商品）与 `registrations`（生效报名）；
* 运营看板的地点排行：`items`（窗口内新增）与 `orders`（窗口内成交）；
* 搜索相关度排序：`search_clicks` 与 `search_impressions`。

数据库会先把两张表 JOIN 成「该对象下 A 的数量 x B 的数量」的乘积，再做
`COUNT(DISTINCT ...)`。数字因为带了 `distinct` 一直是对的——所以错得极难察觉，
但中间行数是乘积。实测（合成数据）：

  campaign_list    6 个专题 x 120 商品 x 40 报名            0.0197s -> 0.0057s
  地点排行         12 个地点 x 60 商品 x 30 订单             0.0612s -> 0.0032s
  搜索相关度       200 件商品，60 件各有 40 点击、80 曝光    0.55s   -> 0.025s

输出逐格相同。这个模块钉住两件事：拆开之后每个数字仍然对，且不能再合回去。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .analytics import _dashboard_location_stats
from .models import (
    CampusCampaign, CampusLocation, CampaignRegistration, Category, Item, Order,
    SearchClick, SearchImpression, SearchQuery,
)

ITEM_TABLE = Item._meta.db_table.upper()
REGISTRATION_TABLE = CampaignRegistration._meta.db_table.upper()
ORDER_TABLE = Order._meta.db_table.upper()
CLICK_TABLE = SearchClick._meta.db_table.upper()
IMPRESSION_TABLE = SearchImpression._meta.db_table.upper()


def top_level_tables(sql):
    """返回 SQL 里出现在**子查询之外**的表名。

    相关子查询整段都包在括号里，所以只统计括号深度为 0 处出现的表名。这样才能
    区分「真的把两张表 JOIN 成了乘积」和「两条各自独立的子查询出现在同一条
    SELECT 里」——后者的中间行数根本不是乘积。
    """
    depth = 0
    found = set()
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif depth == 0 and char == '"':
            end = sql.find('"', index + 1)
            if end == -1:
                break
            found.add(sql[index + 1:end].upper())
            index = end
        index += 1
    return found


def assert_no_join_between(test, sql, pairs):
    """pairs 是 [(中文名, 表名), ...]；任何两张表都不该在同一条查询里被 JOIN。"""
    tables = top_level_tables(sql)
    joined = [name for name, table in pairs if table in tables]
    test.assertLessEqual(
        len(joined), 1,
        '同一条查询里同时碰了 %s，笛卡尔积会跟着回来：%s'
        % ('/'.join(joined), sql[:200]),
    )



class CampaignListQueryBudgetTests(TestCase):
    """专题列表页：一条查询不许同时碰商品表和报名表。"""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='campaign-budget-seller', password='safe-password-123',
        )
        self.users = [
            User.objects.create_user(
                username='campaign-budget-user-%d' % index,
                password='safe-password-123',
            )
            for index in range(4)
        ]
        self.category = Category.objects.create(name='专题预算分类')
        self.campaign = CampusCampaign.objects.create(
            title='预算专题', slug='budget-campaign', description='预算测试',
            starts_at=timezone.now() - timedelta(days=1),
        )

    def _item(self, *, status='available', expires_at=None):
        return Item.objects.create(
            title='专题预算商品', description='预算测试', price='10.00',
            category=self.category, condition='九成新', seller=self.seller,
            campaign=self.campaign, status=status, expires_at=expires_at,
        )

    def _registration(self, index, *, is_active=True):
        return CampaignRegistration.objects.create(
            campaign=self.campaign, user=self.users[index], is_active=is_active,
            # 模型上有一条状态一致性约束：is_active=False 必须留下退出时间。
            left_at=None if is_active else timezone.now(),
        )

    def test_no_single_query_joins_items_and_registrations_together(self):
        """防的是「改回一条 annotate」这种退化。"""
        for index in range(3):
            self._item()
        self._registration(0)

        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse('campaign_list'))

        self.assertTrue(ctx.captured_queries)
        for query in ctx.captured_queries:
            assert_no_join_between(self, query['sql'], (
                ('商品', ITEM_TABLE), ('报名', REGISTRATION_TABLE),
            ))

    def test_counts_still_match_a_naive_recount(self):
        """oracle：逐条笨重数一遍，和页面上的数字比对。"""
        for index in range(5):
            self._item()
        self._item(status='sold')
        self._item(expires_at=timezone.now() - timedelta(hours=1))
        self._registration(0)
        self._registration(1)
        self._registration(2, is_active=False)

        response = self.client.get(reverse('campaign_list'))
        campaign = response.context['campaigns'][0]

        self.assertEqual(campaign.available_item_count, 5)
        self.assertEqual(campaign.active_registration_count, 2)
        self.assertContains(response, '5 件在售')
        self.assertContains(response, '2 人已加入')

    def test_a_campaign_without_items_or_registrations_still_renders(self):
        """空专题不该被过滤掉，两个计数都该是 0。"""
        response = self.client.get(reverse('campaign_list'))

        self.assertEqual(response.status_code, 200)
        campaign = response.context['campaigns'][0]
        self.assertEqual(campaign.available_item_count, 0)
        self.assertEqual(campaign.active_registration_count, 0)


class DashboardLocationStatsQueryBudgetTests(TestCase):
    """运营看板地点排行：一条查询不许同时碰商品表和订单表。"""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='location-budget-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='location-budget-buyer', password='safe-password-123',
        )
        self.category = Category.objects.create(name='地点预算分类')
        self.now = timezone.now()
        self.start = self.now - timedelta(days=30)

    def _item(self, location, *, created_at=None):
        # created_at 是 auto_now_add，创建时传不进去，只能建完再回填。
        item = Item.objects.create(
            title='地点预算商品', description='预算测试', price='10.00',
            category=self.category, condition='九成新', seller=self.seller,
            location=location, status='available',
        )
        if created_at is not None:
            Item.objects.filter(pk=item.pk).update(created_at=created_at)
        return item

    def _order(self, item, location, *, created_at=None):
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=location, agreed_price='10.00', status='completed',
        )
        if created_at is not None:
            Order.objects.filter(pk=order.pk).update(created_at=created_at)
        return order

    def test_no_single_query_joins_items_and_orders_together(self):
        busy = CampusLocation.objects.create(name='热闹地点', building='东校区')
        for index in range(4):
            self._item(busy, created_at=self.now - timedelta(days=1))
        for index in range(3):
            self._order(
                self._item(busy), busy, created_at=self.now - timedelta(days=1),
            )

        with CaptureQueriesContext(connection) as ctx:
            _dashboard_location_stats(self.start, self.now, limit=8)

        self.assertTrue(ctx.captured_queries)
        for query in ctx.captured_queries:
            assert_no_join_between(self, query['sql'], (
                ('商品', ITEM_TABLE), ('订单', ORDER_TABLE),
            ))

    def test_counts_and_ranking_match_a_naive_recount(self):
        """oracle：排序、切片、两个计数都逐格比对。"""
        locations = [
            CampusLocation.objects.create(
                name='地点%02d' % index, building='东校区', sort_order=index,
            )
            for index in range(10)
        ]
        # 每个地点窗口内新增的商品数递减，订单数刻意不同步，用来暴露串味。
        # 订单引用的商品刻意放在窗口外：它们的 created_at 不影响 order_count，
        # 但会让 new_count 保持「只有那 10-index 件」这个干净的预期。
        stale = self.now - timedelta(days=40)
        for index, location in enumerate(locations):
            for _ in range(10 - index):
                self._item(location, created_at=self.now - timedelta(days=1))
            for _ in range(index):
                self._order(
                    self._item(location, created_at=stale),
                    location,
                    created_at=self.now - timedelta(days=1),
                )
        # 一个地点只有订单、没有窗口内新增，本该被 new_count > 0 挡掉。
        quiet = CampusLocation.objects.create(name='只有订单的地点', building='东校区')
        self._order(
            self._item(quiet, created_at=stale), quiet,
            created_at=self.now - timedelta(days=1),
        )

        rows = _dashboard_location_stats(self.start, self.now, limit=8)

        self.assertEqual([row.name for row in rows], [
            '地点%02d' % index for index in range(8)
        ])
        for index, row in enumerate(rows):
            self.assertEqual(row.new_count, 10 - index)
            self.assertEqual(row.order_count, index)
        self.assertNotIn('只有订单的地点', [row.name for row in rows])

    def test_records_outside_the_window_do_not_count(self):
        location = CampusLocation.objects.create(name='窗口地点', building='东校区')
        fresh_item = self._item(location, created_at=self.now - timedelta(days=1))
        self._order(fresh_item, location, created_at=self.now - timedelta(days=1))
        stale_item = self._item(location)
        stale_order = self._order(self._item(location), location)
        Item.objects.filter(pk=stale_item.pk).update(
            created_at=self.now - timedelta(days=40),
        )
        Order.objects.filter(pk=stale_order.pk).update(
            created_at=self.now - timedelta(days=40),
        )

        rows = _dashboard_location_stats(self.start, self.now, limit=8)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].new_count, 1)
        self.assertEqual(rows[0].order_count, 1)


class SearchRelevanceQueryBudgetTests(TestCase):
    """搜索相关度排序：一条查询不许同时碰点击表和曝光表。"""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='relevance-budget-seller', password='safe-password-123',
        )
        self.category = Category.objects.create(name='相关度预算分类')
        self.query = '键盘'
        self.search = SearchQuery.objects.create(
            query=self.query, condition='', result_count=0,
        )
        self.items = [
            Item.objects.create(
                title='键盘配件 %02d' % index, description='预算测试', price='10.00',
                category=self.category, condition='九成新', seller=self.seller,
                status='available',
            )
            for index in range(3)
        ]

    def test_no_single_query_joins_clicks_and_impressions_together(self):
        """防的是「改回一条 annotate」这种退化。"""
        for item in self.items[:2]:
            for _ in range(3):
                SearchClick.objects.create(search_query=self.search, item=item, position=1)
            for _ in range(4):
                SearchImpression.objects.create(
                    search_query=self.search, item=item, position=1,
                )

        with CaptureQueriesContext(connection) as ctx:
            self.client.get(
                reverse('item_list'), {'q': self.query, 'sort': 'relevance'},
            )

        self.assertTrue(ctx.captured_queries)
        for query in ctx.captured_queries:
            assert_no_join_between(self, query['sql'], (
                ('点击', CLICK_TABLE), ('曝光', IMPRESSION_TABLE),
            ))

    def test_feedback_counts_still_match_a_naive_recount(self):
        """oracle：页面上的点击/曝光数，逐格对回数据库里的真实条数。"""
        from datetime import timedelta as td
        window = timezone.now() - td(days=90)
        for item in self.items[:2]:
            for _ in range(3):
                SearchClick.objects.create(search_query=self.search, item=item, position=1)
            for _ in range(4):
                SearchImpression.objects.create(
                    search_query=self.search, item=item, position=1,
                )

        # 搜索结果页自己会为本次展示补写 SearchImpression，所以预期值必须在发请求
        # 之前就取好，否则会把「这次请求刚产生的曝光」也算进去。
        expected = {
            item.pk: (
                SearchClick.objects.filter(
                    item=item, created_at__gte=window,
                    search_query__query__iexact=self.query,
                ).count(),
                SearchImpression.objects.filter(
                    item=item, created_at__gte=window,
                    search_query__query__iexact=self.query,
                ).count(),
            )
            for item in self.items
        }

        response = self.client.get(
            reverse('item_list'), {'q': self.query, 'sort': 'relevance'},
        )
        by_pk = {item.pk: item for item in response.context['items']}

        for item in self.items:
            expected_clicks, expected_impressions = expected[item.pk]
            self.assertEqual(by_pk[item.pk].feedback_click_count, expected_clicks)
            self.assertEqual(
                by_pk[item.pk].feedback_impression_count, expected_impressions,
            )
        # 没有反馈的那件必须是 0，不能是 NULL——模板会把它显示成空白。
        self.assertEqual(by_pk[self.items[2].pk].feedback_click_count, 0)
        self.assertEqual(by_pk[self.items[2].pk].feedback_impression_count, 0)
