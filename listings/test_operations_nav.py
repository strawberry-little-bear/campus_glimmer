# -*- coding: utf-8 -*-
"""运营看板分区跳转条的契约测试。

看板由 32 个 partial 顺序拼成，一屏滚不到头。``operations_nav.py`` 描述「有哪
些分区、怎么分组」，模板负责把每个 include 包进带 id 的 ``<section>``。两边分开
书写，只改一边就会静默失效——chip 还在，点下去却不动；或者分区还在，却完全
没有入口。这个文件就是拦住这种漂移的。
"""
import io
import os
import re

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .operations_nav import OPERATIONS_DASHBOARD_SECTIONS

# 看板的 partial 里另有 14 个 <section id="...">（图表、折叠块之类），所以两条
# 正则都必须带上 class，否则会把无关元素算进分区。
SECTION_ID_RE = re.compile(r'<section[^>]*\bclass="[^"]*\bops-section\b[^"]*"[^>]*\bid="([^"]+)"')
CHIP_RE = re.compile(r'<a[^>]*\bclass="[^"]*\bops-jump-chip\b[^"]*"[^>]*\bhref="#([^"]+)"')
TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'templates', 'listings', 'operations_dashboard.html',
)

GROUP_LABEL_RE = re.compile(r'ops-jump-group-label[^>]*>([^<]+)<')


def nav_anchors():
    return [anchor for _label, items in OPERATIONS_DASHBOARD_SECTIONS for anchor, _text in items]


class OperationsDashboardSectionNavTests(TestCase):
    """分区导航：锚点必须真的落到页面上，数量和顺序都不能漂。"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='ops-nav-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.user)
        self.html = self.client.get(
            reverse('operations_dashboard'), {'days': 30},
        ).content.decode('utf-8')
        self.section_ids = SECTION_ID_RE.findall(self.html)

    def test_every_declared_anchor_lands_on_a_real_section(self):
        missing = [anchor for anchor in nav_anchors() if anchor not in set(self.section_ids)]

        self.assertEqual(missing, [], '导航里有锚点在页面上找不到对应分区')

    def test_every_section_is_reachable_from_the_nav(self):
        # 反方向：新增了分区却忘了登记，用户就只能靠一路滚动找到它。
        orphans = sorted(set(self.section_ids) - set(nav_anchors()))

        self.assertEqual(orphans, [], '页面上有分区没有出现在跳转条里')

    def test_the_nav_covers_every_section_exactly_once(self):
        anchors = nav_anchors()

        self.assertEqual(len(anchors), len(set(anchors)), '导航里有重复锚点')
        self.assertEqual(len(anchors), len(self.section_ids))

    def test_each_group_keeps_the_page_order_of_its_own_sections(self):
        # 组与组之间按主题归类，不跟页面顺序走；但组内必须跟页面顺序一致，
        # 否则用户在组内从左往右扫，方向和他往下滚是打架的。
        position = {anchor: index for index, anchor in enumerate(self.section_ids)}
        for label, items in OPERATIONS_DASHBOARD_SECTIONS:
            indexes = [position[anchor] for anchor, _text in items]
            self.assertEqual(indexes, sorted(indexes), '「%s」组内的顺序和页面不一致' % label)

    def test_all_sections_are_grouped_and_labelled(self):
        groups = OPERATIONS_DASHBOARD_SECTIONS

        self.assertEqual(len(groups), 6)
        self.assertEqual(len(nav_anchors()), 32)
        for label, items in groups:
            self.assertTrue(label, '分组名不能为空')
            self.assertTrue(items, '空分组会渲染出一个孤立的分组标题')
            for anchor, text in items:
                self.assertTrue(text, '%s 缺少显示文案' % anchor)

    def test_every_group_label_and_chip_is_rendered(self):
        self.assertEqual(
            GROUP_LABEL_RE.findall(self.html),
            [label for label, _items in OPERATIONS_DASHBOARD_SECTIONS],
        )
        self.assertEqual(len(CHIP_RE.findall(self.html)), 32)

    def test_the_page_has_no_duplicated_ids(self):
        # 加分区导航时踩过的坑：32 个 include 被包进 <section> 之后，原来那批
        # 裸 include 还留在模板里，于是 31 个面板渲染了两遍，页面里出现重复
        # id，锚点开始乱跳。这类问题在页面上不仔细翻很难看出来，所以在这一点名。
        seen, repeated = set(), []
        for node_id in re.findall(r'\sid="([^"]+)"', self.html):
            if node_id in seen:
                repeated.append(node_id)
            seen.add(node_id)

        self.assertEqual(repeated, [], '页面里有重复的 DOM id')

    def test_each_partial_is_included_exactly_once(self):
        # 渲染结果里看不出「同一个 partial 被包了两次」，因为两遍内容是一样的；
        # 只有回到模片源码数 include 才能发现。
        source = io.open(TEMPLATE_PATH, encoding='utf-8').read()
        includes = re.findall(r"{% include 'listings/operations/([^']+)' %}", source)

        self.assertEqual(len(includes), len(set(includes)), '同一个 partial 被包含了多次')
        self.assertEqual(len(includes), 32)

    def test_anonymous_visitors_never_see_the_nav(self):
        self.client.logout()
        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        self.assertEqual(response.status_code, 302)
