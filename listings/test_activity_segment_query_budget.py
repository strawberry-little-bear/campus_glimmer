# -*- coding: utf-8 -*-
"""用户活跃分层的查询预算，和一份逐用户重算的口径 oracle。

`_build_user_activity_segments` 早先把八种行为塞进同一条查询的八个 ``annotate`` 里。
多表聚合不是「顺手多 JOIN 几张表」那么无害：数据库要先算出这八张表的笛卡尔积，再对
中间结果里每一行执行 ``COUNT(DISTINCT ...)``，中间行数是每个用户各项行为数量的**乘积**。
演示库数据小所以完全看不出，用户稍有规模就从亚秒退化到数分钟。现在拆成八次「按用户
分组」的聚合，再在内存里合并，代价是 O(行数) 而不是 O(乘积)。

拆分只改查询的形状，不该改任何一个数字。这个模块钉住四件事：

1. **口径不能因为拆分而变**——用一套刻意写得笨拙的「逐用户、逐关系各 count 一次」的
   实现当 oracle，复算活跃用户数和四个分层的人数，和拆分后的结果逐格比对；
2. **阈值和优先级本身**——四条分层路径各守一条边界，尤其是「订单压过发布件数」；
3. **窗口边界与匿名行为**——起止两端都含、匿名搜索不造用户、零行为用户不活跃；
4. **不能再合回去**——把查询数上界和单条查询的 JOIN 数都写下来。谁觉得「一条
   annotate 更省事」，测试先红，然后回来读上面那段关于笛卡尔积的注释。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from chat_messages.models import PrivateMessage

from .analytics import _build_user_activity_segments
from .models import BrowsingHistory, Category, CampusLocation, Favorite, Item, Order, SearchQuery


# 和 analytics.py 里的 behaviour_specs 一致的一份清单，只给 oracle 用。
# 故意在测试里重抄一遍：oracle 必须能独立于被测实现存在，抄一遍才可能在实现被改错时红。
BEHAVIOUR_SPECS = (
    ('listing_count', Item, 'seller', 'created_at'),
    ('search_count', SearchQuery, 'user', 'created_at'),
    ('browse_count', BrowsingHistory, 'user', 'last_viewed_at'),
    ('favorite_count', Favorite, 'user', 'created_at'),
    ('purchase_count', Order, 'buyer', 'created_at'),
    ('sale_count', Order, 'seller', 'created_at'),
    ('sent_message_count', PrivateMessage, 'sender', 'created_at'),
    ('received_message_count', PrivateMessage, 'receiver', 'created_at'),
)

EMPTY_SEGMENTS = {'trader': 0, 'supplier': 0, 'explorer': 0, 'light': 0}


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def segments_as_dict(segments):
    return {row['key']: row['count'] for row in segments}


def naive_counts_per_user(start, end):
    """逐用户、逐关系各 count 一次的笨实现。

    故意不写聚合：每个用户、每种行为都单独发一次查询。它慢，但它和「先 JOIN 再
    COUNT DISTINCT」的旧实现是同一套口径，所以能用来验证新拆分没有改动任何数字。
    """
    counts = {}
    for user in User.objects.all():
        per_user = {}
        for field, model, owner_field, date_field in BEHAVIOUR_SPECS:
            per_user[field] = model.objects.filter(**{
                owner_field: user,
                date_field + '__gte': start,
                date_field + '__lte': end,
            }).count()
        counts[user.id] = per_user
    return counts


def oracle_segments(start, end):
    """用笨计数复算一遍活跃用户数和四个分层的人数。"""
    active_user_count = 0
    segment_counts = dict(EMPTY_SEGMENTS)
    for per_user in naive_counts_per_user(start, end).values():
        if not sum(per_user.values()):
            continue
        active_user_count += 1
        if per_user['purchase_count'] or per_user['sale_count']:
            segment_counts['trader'] += 1
        elif per_user['listing_count'] >= 2:
            segment_counts['supplier'] += 1
        elif per_user['search_count'] + per_user['browse_count'] >= 5:
            segment_counts['explorer'] += 1
        else:
            segment_counts['light'] += 1
    return active_user_count, segment_counts


def seed_scenarios(now):
    """一批刚好覆盖四条分层路径，外加全部边界情况的数据。

    覆盖到的形状：交易参与者（买过也卖过）、供给贡献者（两件商品、无订单）、高频
    探索者（三次搜索加两次浏览，刚好压在阈值 5 上）、轻度活跃者（一次收藏加一条私信）、
    八项全为 0 的注册用户、窗口之外的行为，以及一条 user 为空的匿名搜索。

    返回 ``(start, end)``，oracle 用它复算同一个窗口。`end` 刻意在播种之后取：
    播种用的 `now` 只是窗口的锚点，真实数据都落在它之后，拿它当上界会把全部行为
    挡在窗口外。
    """
    category = Category.objects.create(name='活跃分层分类')
    location = CampusLocation.objects.create(name='图书馆东门', building='东校区', address='图书馆一层东侧')

    def make_item(seller, title):
        return Item.objects.create(
            title=title, description='用于活跃分层测试', price='10.00',
            category=category, location=location, condition='九成新', seller=seller,
        )

    # 交易参与者：一笔作为买家、一笔作为卖家。优先级最高的路径，其他行为再多也不改判。
    trader = make_user('seg-trader')
    counterparty = make_user('seg-counterparty')
    bought_item = make_item(counterparty, '买家视角的商品')
    sold_item = make_item(trader, '卖家视角的商品')
    Order.objects.create(
        item=bought_item, buyer=trader, seller=counterparty,
        agreed_price='10.00', buyer_note='分层测试', status='completed',
    )
    Order.objects.create(
        item=sold_item, buyer=counterparty, seller=trader,
        agreed_price='10.00', buyer_note='分层测试', status='completed',
    )

    # 供给贡献者：两件在窗口内的商品，没有任何订单。
    supplier = make_user('seg-supplier')
    make_item(supplier, '供给商品一')
    make_item(supplier, '供给商品二')

    # 高频探索者：三次搜索加两次浏览，合起来刚好等于阈值 5。
    explorer = make_user('seg-explorer')
    for index in range(3):
        SearchQuery.objects.create(
            user=explorer, query='探索搜索 %d' % index, result_count=1, condition='',
        )
    for index in range(2):
        browsed_item = make_item(counterparty, '被浏览的商品 %d' % index)
        BrowsingHistory.objects.create(user=explorer, item=browsed_item)

    # 轻度活跃者：一次收藏加一条发出的私信，够不上前面任何一条路径。
    light = make_user('seg-light')
    Favorite.objects.create(user=light, item=bought_item)
    PrivateMessage.objects.create(sender=light, receiver=trader, content='这个还在吗')

    # 八项全为 0 的用户：注册了但什么都没做，不能计入活跃用户。
    make_user('seg-silent')

    # 窗口之外的行为：一条 40 天前的商品和一条 40 天前的搜索，都不该被算进来。
    stale_item = make_item(supplier, '上一个周期的商品')
    Item.objects.filter(pk=stale_item.pk).update(created_at=now - timedelta(days=40))
    stale_search = SearchQuery.objects.create(
        user=explorer, query='上一个周期的搜索', result_count=0, condition='',
    )
    SearchQuery.objects.filter(pk=stale_search.pk).update(created_at=now - timedelta(days=40))

    # 匿名搜索：user 为空。旧实现靠 JOIN 用户表天然把它排除在外，新实现必须显式跳过，
    # 否则会凭一条匿名搜索凭空造出一个活跃用户。
    SearchQuery.objects.create(user=None, query='匿名搜索', result_count=3, condition='')

    return now - timedelta(days=30), timezone.now()


class ActivitySegmentOracleTests(TestCase):
    """第一件：拆分之后的每一个数字，和逐用户笨重算的结果逐格相同。"""

    def setUp(self):
        self.now = timezone.now()
        self.start, self.end = seed_scenarios(self.now)

    def test_active_users_and_segments_match_a_naive_per_user_recount(self):
        active_user_count, segments = _build_user_activity_segments(self.start, self.end)
        expected_active, expected_segments = oracle_segments(self.start, self.end)

        self.assertEqual(active_user_count, expected_active)
        self.assertEqual(segments_as_dict(segments), expected_segments)

    def test_the_seeded_scenario_lands_on_the_expected_segments(self):
        """把 oracle 再钉成一组写死的期望值。

        只有 oracle 一致还不够：万一两边同时改错，测试还是绿的。这组数字是照着
        `seed_scenarios` 的注释手推出来的，改错任何一条路径都会在这里露出来。
        """
        active_user_count, segments = _build_user_activity_segments(self.start, self.end)

        # 六个人里有一个八项全为 0，那条匿名搜索不计入任何用户。
        self.assertEqual(active_user_count, 5)
        self.assertEqual(segments_as_dict(segments), {
            'trader': 2, 'supplier': 1, 'explorer': 1, 'light': 1,
        })

    def test_shares_are_relative_to_active_users_only(self):
        active_user_count, segments = _build_user_activity_segments(self.start, self.end)

        self.assertEqual({row['key']: row['share'] for row in segments}, {
            'trader': 40.0, 'supplier': 20.0, 'explorer': 20.0, 'light': 20.0,
        })

    def test_every_behaviour_table_feeds_the_oracle(self):
        """确认 oracle 自己看得见八种行为，不会因为抄错字段名而恒等于 0。"""
        totals = {
            field: sum(per_user[field] for per_user in naive_counts_per_user(self.start, self.end).values())
            for field, _, _, _ in BEHAVIOUR_SPECS
        }
        self.assertEqual(totals, {
            # 交易双方各 1、供给方 2、被浏览的 2 件；那件 40 天前的商品不计。
            'listing_count': 6,
            'search_count': 3,
            'browse_count': 2,
            'favorite_count': 1,
            # 两笔订单各产生一次购买和一次出售，所以两边各记 2。
            'purchase_count': 2,
            'sale_count': 2,
            'sent_message_count': 1,
            'received_message_count': 1,
        })


class ActivitySegmentBoundaryTests(TestCase):
    """第二件：窗口边界、匿名行为和空窗口，三种容易悄悄漏掉用户的形状。"""

    def setUp(self):
        self.now = timezone.now()
        self.start = self.now - timedelta(days=30)

    def test_both_window_edges_are_inclusive(self):
        """起点和终点那一刻的行为都要算进来，早一秒的不要。"""
        at_start = SearchQuery.objects.create(
            user=make_user('boundary-start'), query='起点搜索', result_count=0, condition='',
        )
        at_end = SearchQuery.objects.create(
            user=make_user('boundary-end'), query='终点搜索', result_count=0, condition='',
        )
        too_early = SearchQuery.objects.create(
            user=make_user('boundary-early'), query='窗口外搜索', result_count=0, condition='',
        )
        SearchQuery.objects.filter(pk=at_start.pk).update(created_at=self.start)
        SearchQuery.objects.filter(pk=at_end.pk).update(created_at=self.now)
        SearchQuery.objects.filter(pk=too_early.pk).update(
            created_at=self.start - timedelta(seconds=1),
        )

        active_user_count, segments = _build_user_activity_segments(self.start, self.now)

        self.assertEqual(active_user_count, 2)
        self.assertEqual(segments_as_dict(segments), {
            'trader': 0, 'supplier': 0, 'explorer': 0, 'light': 2,
        })

    def test_anonymous_search_does_not_create_an_active_user(self):
        """user 为空的搜索不能计入任何用户，也不能把活跃用户数抬起来。"""
        SearchQuery.objects.create(user=None, query='匿名搜索甲', result_count=1, condition='')
        SearchQuery.objects.create(user=None, query='匿名搜索乙', result_count=1, condition='')

        active_user_count, segments = _build_user_activity_segments(self.start, self.now)

        self.assertEqual(active_user_count, 0)
        self.assertEqual(segments_as_dict(segments), EMPTY_SEGMENTS)
        self.assertEqual([row['share'] for row in segments], [0, 0, 0, 0])

    def test_a_user_with_no_behaviour_is_not_active(self):
        """注册了但一条行为都没有的用户，不进活跃用户数，也不进任何分层。"""
        make_user('silent-one')
        make_user('silent-two')

        active_user_count, segments = _build_user_activity_segments(self.start, self.now)

        self.assertEqual(active_user_count, 0)
        self.assertEqual(segments_as_dict(segments), EMPTY_SEGMENTS)

    def test_an_empty_window_reports_nothing_without_dividing_by_zero(self):
        active_user_count, segments = _build_user_activity_segments(self.now, self.now)

        self.assertEqual(active_user_count, 0)
        self.assertEqual(segments_as_dict(segments), EMPTY_SEGMENTS)
        self.assertEqual([row['share'] for row in segments], [0, 0, 0, 0])


class ActivitySegmentClassificationTests(TestCase):
    """第三件：四条分层路径的阈值和优先级本身。"""

    def setUp(self):
        self.now = timezone.now()
        self.start = self.now - timedelta(days=30)
        self.category = Category.objects.create(name='阈值分类')
        self.location = CampusLocation.objects.create(name='阈值地点')

    def make_item(self, seller, title, in_window=True):
        item = Item.objects.create(
            title=title, description='用于阈值测试', price='10.00',
            category=self.category, location=self.location, condition='九成新', seller=seller,
        )
        if not in_window:
            Item.objects.filter(pk=item.pk).update(created_at=self.start - timedelta(days=5))
        return item

    def add_searches(self, user, count):
        for index in range(count):
            SearchQuery.objects.create(
                user=user, query='阈值搜索 %d' % index, result_count=1, condition='',
            )

    def add_browses(self, user, count):
        # 被浏览的商品刻意放到窗口外：这个用例要看的只有 browse_count，
        # 不想让商品卖家顺带变成一个供给贡献者。
        seller = make_user('browse-seller-%s' % user.pk)
        for index in range(count):
            browsed = self.make_item(seller, '阈值浏览 %s %d' % (user.pk, index), in_window=False)
            BrowsingHistory.objects.create(user=user, item=browsed)

    def segments(self):
        # 上界取调用时刻：setUp 里的 self.now 早于本用例播种的数据。
        active_user_count, segments = _build_user_activity_segments(
            self.start, timezone.now(),
        )
        return active_user_count, segments_as_dict(segments)

    def test_one_listing_is_light(self):
        single = make_user('threshold-single')
        self.make_item(single, '只有一件商品')

        active_user_count, segments = self.segments()

        self.assertEqual(active_user_count, 1)
        self.assertEqual(segments, {'trader': 0, 'supplier': 0, 'explorer': 0, 'light': 1})

    def test_two_listings_reach_supplier(self):
        double = make_user('threshold-double')
        self.make_item(double, '两件商品之一')
        self.make_item(double, '两件商品之二')

        active_user_count, segments = self.segments()

        self.assertEqual(active_user_count, 1)
        self.assertEqual(segments, {'trader': 0, 'supplier': 1, 'explorer': 0, 'light': 0})

    def test_four_search_and_browse_is_light(self):
        under = make_user('threshold-under')
        self.add_searches(under, 2)
        self.add_browses(under, 2)

        active_user_count, segments = self.segments()

        self.assertEqual(active_user_count, 1)
        self.assertEqual(segments, {'trader': 0, 'supplier': 0, 'explorer': 0, 'light': 1})

    def test_five_search_and_browse_reaches_explorer(self):
        over = make_user('threshold-over')
        self.add_searches(over, 3)
        self.add_browses(over, 2)

        active_user_count, segments = self.segments()

        self.assertEqual(active_user_count, 1)
        self.assertEqual(segments, {'trader': 0, 'supplier': 0, 'explorer': 1, 'light': 0})

    def test_a_purchase_outranks_a_supplier_shaped_user(self):
        """订单优先于发布件数：买了东西、又发了很多商品的用户算交易参与者。"""
        both = make_user('threshold-both')
        self.make_item(both, '自己发的第一件')
        self.make_item(both, '自己发的第二件')
        self.make_item(both, '自己发的第三件')
        other = make_user('threshold-other')
        # 对方的商品放到窗口外，只让这笔订单本身进入统计。
        self.make_item(other, '买来的商品', in_window=False)
        Order.objects.create(
            item=Item.objects.get(title='买来的商品'), buyer=both, seller=other,
            agreed_price='10.00', buyer_note='阈值测试', status='completed',
        )

        active_user_count, segments = self.segments()

        # both 有三件商品却被判成交易参与者；other 只有一笔成交，同样是交易参与者。
        self.assertEqual(active_user_count, 2)
        self.assertEqual(segments, {'trader': 2, 'supplier': 0, 'explorer': 0, 'light': 0})

    def test_a_sale_alone_is_enough_for_trader(self):
        """一件商品都没发、只卖出去一笔，也算交易参与者。"""
        seller = make_user('threshold-seller')
        buyer = make_user('threshold-buyer')
        self.make_item(seller, '卖出的商品', in_window=False)
        Order.objects.create(
            item=Item.objects.get(title='卖出的商品'), buyer=buyer, seller=seller,
            agreed_price='10.00', buyer_note='阈值测试', status='completed',
        )

        active_user_count, segments = self.segments()

        self.assertEqual(active_user_count, 2)
        self.assertEqual(segments, {'trader': 2, 'supplier': 0, 'explorer': 0, 'light': 0})


class ActivitySegmentQueryBudgetTests(TestCase):
    """第四件：拆分必须保持拆分。"""

    def setUp(self):
        self.now = timezone.now()

    def test_a_populated_window_issues_one_grouped_aggregate_per_behaviour(self):
        self.start, self.end = seed_scenarios(self.now)
        with CaptureQueriesContext(connection) as ctx:
            _build_user_activity_segments(self.start, self.end)

        # 八种行为各一次按用户分组的聚合，一次都不许多。
        # 合回一条八重 annotate 会把这里降到 1，但那正是要防的事情，
        # 所以真正的护栏是下面那个 JOIN 数的断言。
        self.assertLessEqual(len(ctx.captured_queries), 8)

    def test_no_single_query_joins_across_behaviour_tables(self):
        """防的是「改回一条 annotate」这种退化。

        一条查询里出现八次 JOIN，就意味着数据库要先算八张表的笛卡尔积。
        现在每次聚合只碰自己那张表，一条 JOIN 都不该有；留一次余量是给
        「按用户分组时顺带过滤一下」这类正常写法用的。
        """
        self.start, self.end = seed_scenarios(self.now)
        with CaptureQueriesContext(connection) as ctx:
            _build_user_activity_segments(self.start, self.end)

        self.assertTrue(ctx.captured_queries)
        for query in ctx.captured_queries:
            joins = query['sql'].upper().count(' JOIN ')
            self.assertLessEqual(
                joins, 1,
                '单条查询又开始跨表 JOIN 了，八表笛卡尔积会跟着回来：%s' % query['sql'][:200],
            )

    def test_an_empty_database_stays_within_budget(self):
        with CaptureQueriesContext(connection) as ctx:
            active_user_count, segments = _build_user_activity_segments(
                self.now - timedelta(days=30), self.now,
            )

        self.assertLessEqual(len(ctx.captured_queries), 8)
        self.assertEqual(active_user_count, 0)
        self.assertEqual(segments_as_dict(segments), EMPTY_SEGMENTS)
