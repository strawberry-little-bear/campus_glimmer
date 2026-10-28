# -*- coding: utf-8 -*-
"""订单详情页「下一步」卡片的标题来源，以及模板拆分后的形状守卫。

两件事凑在一起，才值得单独开一个测试文件：

1. **侧栏那张行动卡曾经是全页唯一没有标题层级的卡片。** 它只有一个
   ``eyebrow`` 眉标，下面直接跟正文和按钮。读屏软件在 h1「订单详情」之后
   一路跳到交付安排的 h2，侧栏整段被并进上一个区块——用户听不到「这里
   是你现在要做的事」，而这里恰恰是全页最该被听见的部分。标题文案原本散在
   8 个 ``{% if %}`` 分支里各写一句，改一句话要翻遍半个文件，也没法逐条
   断言，于是收进纯函数 ``order_next_step``。
2. **order_detail.html 一度长到 368 行 / 35 KB，是全站最大模板。** 运营看板
   和搜索洞察都已经按分区拆成片段（``templates/listings/operations/``、
   ``search_insights/``），订单详情页是最后一个还挤在一整个文件里的大页面。
   拆分本身不改变任何渲染结果，但「拆回去」是 silently 会发生的：往主模板里
   贴一段分区代码比新建一个片段省事，页面看着也完全一样。

所以这里盯两条：

* ``OrderNextStepTests`` 逐条分支核对标题与图标，含买家和卖家看到不同下一步
  的情形，以及状态表将来新增一档时的兜底分支；
* ``OrderDetailTemplateTests`` 真的渲染订单详情页，断言侧栏卡片里出现 h2；
  同时盯住主模板的形状——行数超阈值、或者某个分区片段不再被 include，
  都直接失败。
"""
import os
import re
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .models import CampusLocation, Category, Item, Order
from .order_presentation import order_next_step

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_PATH = os.path.join(PROJECT_ROOT, 'templates', 'listings', 'order_detail.html')
PARTIALS_DIR = os.path.join(PROJECT_ROOT, 'templates', 'listings', 'orders')

# 拆分后主模板只负责把分区片段拼起来，38 行。阈值留到 60 是允许补注释和空行，
# 不是允许把分区内容搬回来——368 行的文件就是这么一段段长回来的。
MAX_MAIN_TEMPLATE_LINES = 60

PARTIALS = (
    '_overview.html',
    '_actions.html',
    '_appointment.html',
    '_safety.html',
    '_rating_panel.html',
    '_timeline.html',
    '_dispute.html',
    '_ratings.html',
    '_scripts.html',
)

# 借用单和交易单的进度条节点数不同，两种交易方式都要渲染一遍，
# 否则只有一半的分支被真正看过。
TRADE_MODES = ('sale', 'borrow')

# 覆盖 order_next_step 的每一个分支。借用链上的状态放在一起，
# 因为它们的标题与角色无关。
STATUS_TITLES = (
    ('pending', '等待你确认预约', 'bi-hourglass-split'),
    ('confirmed', '约定见面时间', 'bi-calendar2-week'),
    ('meeting', '当面交付确认', 'bi-hand-thumbs-up'),
    ('borrowed', '借用进行中', 'bi-arrow-repeat'),
    ('returned', '借用已归还', 'bi-check-circle'),
    ('completed', '订单已结束', 'bi-flag'),
    ('cancelled', '订单已结束', 'bi-flag'),
)

ASIDE_BLOCK = re.compile(r'<aside\b[^>]*>(.*?)</aside>', re.S)


def _person(username):
    """一个只带身份字段的替身。

    ``order_next_step`` 只比较 ``viewer == order.seller``，用真对象要连库，
    而这里要断言的是纯分支逻辑，不是 ORM。SimpleNamespace 没有 ``__eq__``，
    所以「同一个人」必须复用同一个实例。
    """
    return SimpleNamespace(username=username, pk=1, id=1)


def _order(status, seller):
    return SimpleNamespace(status=status, seller=seller)


class OrderNextStepTests(SimpleTestCase):
    """八个分支的标题与图标。"""

    def test_seller_sees_a_title_and_icon_for_every_status(self):
        seller = _person('detail-seller')
        for status, title, icon in STATUS_TITLES:
            with self.subTest(status=status):
                step = order_next_step(_order(status, seller), seller)
                self.assertEqual(step.title, title)
                self.assertEqual(step.icon, icon)

    def test_buyer_waits_for_the_seller_on_a_pending_order(self):
        seller = _person('detail-seller')
        buyer = _person('detail-buyer')
        step = order_next_step(_order('pending', seller), buyer)
        self.assertEqual(step.title, '等待卖家确认')
        self.assertEqual(step.icon, 'bi-hourglass-split')

    def test_buyer_and_seller_share_the_confirmed_title(self):
        # 确认后的分工写在正文里（一个「请继续沟通」，一个「请沟通后确认」），
        # 标题层只说同一件事：约定见面时间。这里把差别钉住，将来看要不要
        # 让标题也分角色，改的是断言而不是模板。
        seller = _person('detail-seller')
        buyer = _person('detail-buyer')
        from_seller = order_next_step(_order('confirmed', seller), seller)
        from_buyer = order_next_step(_order('confirmed', seller), buyer)
        self.assertEqual(from_seller, from_buyer)
        self.assertEqual(from_seller.title, '约定见面时间')

    def test_unknown_status_falls_back_to_waiting_for_the_seller(self):
        # 状态表将来加一档时，这里先给出一个不会把卡片变成空标题的兜底。
        seller = _person('detail-seller')
        buyer = _person('detail-buyer')
        step = order_next_step(_order('some_future_status', seller), buyer)
        self.assertEqual(step.title, '等待卖家确认')
        self.assertEqual(step.icon, 'bi-hourglass-split')

    def test_every_title_is_a_non_empty_sentence(self):
        seller = _person('detail-seller')
        for status, title, icon in STATUS_TITLES:
            with self.subTest(status=status):
                step = order_next_step(_order(status, seller), seller)
                self.assertTrue(step.title.strip())
                self.assertTrue(step.icon.startswith('bi-'))


class OrderDetailTemplateTests(TestCase):
    """渲染真实页面，盯标题层级与拆分形状。"""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='detail-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(
            username='detail-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='订单详情测试分类')
        self.location = CampusLocation.objects.create(name='订单详情测试地点')

    def make_order(self, status='pending', trade_mode='sale'):
        item = Item.objects.create(
            title='订单详情测试商品',
            description='用于验证订单详情页的标题层级与分区结构。',
            category=self.category,
            location=self.location,
            seller=self.seller,
            price='12.00',
            trade_mode=trade_mode,
            status='reserved',
        )
        return Order.objects.create(
            item=item,
            buyer=self.buyer,
            seller=self.seller,
            meeting_location=self.location,
            agreed_price='12.00',
            status=status,
        )

    def aside(self, response):
        match = ASIDE_BLOCK.search(response.content.decode('utf-8'))
        self.assertIsNotNone(match, '页面上找不到侧栏 <aside>')
        return match.group(1)

    def test_action_card_has_a_heading_for_every_status_and_trade_mode(self):
        for trade_mode in TRADE_MODES:
            for status, title, _icon in STATUS_TITLES:
                with self.subTest(trade_mode=trade_mode, status=status):
                    order = self.make_order(status=status, trade_mode=trade_mode)
                    self.client.login(
                        username='detail-seller', password='safe-password-123')
                    response = self.client.get(
                        reverse('order_detail', args=[order.id]))
                    self.assertEqual(response.status_code, 200)
                    aside = self.aside(response)
                    heading = re.search(r'<h2[^>]*>(.*?)</h2>', aside, re.S)
                    self.assertIsNotNone(
                        heading,
                        '侧栏行动卡里没有 h2：卡片标题层级断档，读屏软件会把它'
                        '并进上一个区块')
                    self.assertEqual(heading.group(1).strip(), title)
                    self.client.logout()

    def test_action_card_icon_follows_the_status(self):
        order = self.make_order(status='meeting')
        self.client.login(username='detail-seller', password='safe-password-123')
        response = self.client.get(reverse('order_detail', args=[order.id]))
        self.assertIn('bi-hand-thumbs-up', self.aside(response))

    def test_main_template_only_wires_up_the_partials(self):
        with open(TEMPLATE_PATH, encoding='utf-8') as handle:
            template = handle.read()
        self.assertLessEqual(
            len(template.splitlines()), MAX_MAIN_TEMPLATE_LINES,
            '主模板又长回来了：把分区内容搬回 order_detail.html，'
            '而不是新建一个 templates/listings/orders/ 下的片段')
        for partial in PARTIALS:
            self.assertIn(
                "listings/orders/%s" % partial, template,
                '分区片段 %s 没有出现在主模板里，成了没人用的死文件' % partial)

    def test_every_partial_exists_and_is_wired_up(self):
        # 反向约束：目录里多出一个没有被 include 的片段，说明上一轮拆分
        # 删了 include 却留下了文件，页面少一块而没有任何测试会红。
        wired = set(PARTIALS)
        on_disk = {
            name for name in os.listdir(PARTIALS_DIR)
            if name.endswith('.html')
        }
        self.assertEqual(
            on_disk, wired,
            'templates/listings/orders/ 里的片段和主模板的 include 对不上：'
            '多余 %s，缺失 %s' % (sorted(on_disk - wired), sorted(wired - on_disk)))
