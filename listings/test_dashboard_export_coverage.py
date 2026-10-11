# -*- coding: utf-8 -*-
"""守住「面板有的，导出也必须有」这条线：看板导出的表和屏幕上看的要是同一份。

运营看板上有三十多个面板，CSV 导出是另一份手写的代码。两边各写各的，结果是运营把
文件下载回去，读到的表和屏幕上看的对不上——有的面板在导出里整节失踪，有的面板换了
个名字，同一个指标在两处叫不同的标题。这种偏差不会让任何测试变红，因为每一处单独看
都是对的：面板渲染得出来，导出也导得出来，只有把它们放在一起比才会发现少了一块。

这个模块就是那次比较。它把渲染后的看板 HTML 里的面板标题抓出来，再和 CSV 里的节
标题对：每个面板要么在导出里有同名的节，要么在下面两张表里被显式登记过。登记而不是
放过，是因为历史上面板和导出本来就存在合理的差异，一律判失败会让这张表变成一张永远
改不动的告示；而不登记又会让下一块漏接的面板悄悄溜过去。

三条边界比其余更要紧。空态标题不算面板：暂时没有高优先级提醒这种文案只在无数据时
出现，它本来就不该有导出节。别名表里的每一条都必须写明理由，写不出理由的差异应该去
改代码而不是往表里加一行——这张表的长度本身就是一处技术债的度量。以及这里只做比对，
不改写看板、不改写导出，也不为了让某个标题对上而调整任何一边的文案。
"""

import csv
import io
import re

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse


PANEL_TITLE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
TAG = re.compile(r"<[^>]+>")


def panel_titles(html):
    """渲染后的面板标题：剥标签、去空白，按文档顺序去重。"""
    titles = []
    for raw in PANEL_TITLE.findall(html):
        title = TAG.sub("", raw).strip()
        if title and title not in titles:
            titles.append(title)
    return titles


def csv_first_cells(text):
    """CSV 里每一行第一个非空格子的集合，节标题都是这么落的。"""
    return {row[0].strip() for row in csv.reader(io.StringIO(text)) if row and row[0].strip()}


# 面板标题 -> 导出节标题。每一条都得能说清为什么两边不同名。
# 这些是历史上先有面板、后补导出时留下的差异：数据一直都在，只是节标题另起了名字。
PANEL_TO_SECTION = {
    # 面板叫「通知触达与聚合」，导出的节按通知类型表命名。
    '通知触达与聚合': '通知类型',
    # 面板叫「求购匹配转化」，导出按它下面的三块表分节，各起各的表头名。
    '求购匹配转化': '求购匹配转化漏斗',
    # 面板叫「用户回访率」，导出的表头用的是指标名。
    '用户回访率': '用户回访',
    # 面板叫「用户行为转化漏斗」，导出按漏斗表命名。
    '用户行为转化漏斗': '转化漏斗',
    # 面板叫「交易状态分布」，导出的节按数据源命名。
    '交易状态分布': '订单状态',
    # 面板叫「分类与地点」，导出把它拆成供给两张表，各起各的表头名。
    '分类与地点': '分类供给',
    # 面板叫「校园需求机会」，导出的节用数据源里的字段名。
    '校园需求机会': '需求主题',
    # 面板叫「订单全链路阶段时长」，导出按表内容命名。
    '订单全链路阶段时长': '订单阶段耗时',
    # 面板叫「借用节律与学期阶段交叉」，导出把它挂在学期节律主节下面。
    '借用节律与学期阶段交叉': '借用节奏与学期阶段交叉',
}

# 本来就不该有导出节的面板：空态文案，只在无数据时渲染出来。
NOT_EXPORTED = {
    '暂时没有高优先级提醒',
}


class DashboardExportCoverageTests(TestCase):
    """每个面板都得在导出里找得到，或者在上面的两张表里登记过。"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='coverage-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.user)

    def fetch(self):
        html = self.client.get(reverse('operations_dashboard'), {'days': 30}).content.decode('utf-8')
        text = self.client.get(
            reverse('operations_dashboard_export'), {'days': 30},
        ).content.decode('utf-8-sig')
        return panel_titles(html), csv_first_cells(text), text

    def test_every_panel_is_either_exported_or_registered(self):
        titles, sections, _ = self.fetch()

        self.assertTrue(titles)
        unexplained = [
            title for title in titles
            if title not in sections
            and title not in PANEL_TO_SECTION
            and title not in NOT_EXPORTED
        ]
        self.assertEqual(unexplained, [])

    def test_every_alias_points_at_a_section_the_export_really_writes(self):
        # 别名表指向一个导出里根本不存在的节，等于这条登记是空的：
        # 面板依旧是漏接的，而测试还会绿。所以每一条都要反过来验一遍。
        titles, sections, _ = self.fetch()

        broken = [
            f'{panel} -> {section}'
            for panel, section in PANEL_TO_SECTION.items()
            if section not in sections
        ]
        self.assertEqual(broken, [])

    def test_every_alias_corresponds_to_a_panel_that_renders(self):
        # 面板被删了、被改名了，别名表里的那一条就成了孤儿：它守着一个不存在的面板。
        titles, _, _ = self.fetch()

        orphaned = [
            panel for panel in PANEL_TO_SECTION
            if panel not in titles
        ]
        self.assertEqual(orphaned, [])

    def test_the_empty_state_titles_are_not_panels(self):
        # 空态文案不是面板。运营提醒在没有数据时渲染成一句提示，把它算进面板集合，
        # 就会要求导出为一句提示单开一节。
        titles, _, _ = self.fetch()

        self.assertIn('暂时没有高优先级提醒', titles)
        self.assertNotIn('运营提醒', titles)

    def test_the_export_carries_the_newly_covered_sections(self):
        # 这两节是刚补的，也是这个模块存在的直接理由：面板上有、导出里整节失踪。
        _, _, text = self.fetch()

        self.assertIn('热门搜索', text)
        self.assertIn('借用逾期催收阶梯', text)
        self.assertIn('待催收', text)
        self.assertIn('累计升级次数', text)

    def test_the_export_carries_the_escalation_ladder_note(self):
        # 三级阶梯的边界要随表导出：下载回去的表要能自证平台只记录和通知。
        _, _, text = self.fetch()

        self.assertIn('不取消订单', text)
        self.assertIn('不代扣押金', text)

    def test_top_search_rows_carry_the_zero_result_share(self):
        # 无结果占比是面板上看不出的一列：面板只报次数，导出把它补成比例，
        # 因为下载回去的表通常会被按占比排序。
        seller = User.objects.create_user(username='coverage-seller')
        from .models import Category, Item, SearchQuery

        category = Category.objects.create(name='覆盖测试分类')
        Item.objects.create(
            seller=seller, category=category, title='覆盖测试商品',
            price='10.00', status='available',
        )
        SearchQuery.objects.create(query='覆盖测试词', result_count=0)
        SearchQuery.objects.create(query='覆盖测试词', result_count=0)
        SearchQuery.objects.create(query='覆盖测试词', result_count=4)

        text = self.client.get(
            reverse('operations_dashboard_export'), {'days': 30},
        ).content.decode('utf-8-sig')

        rows = list(csv.reader(io.StringIO(text)))
        start = next(i for i, row in enumerate(rows) if row and row[0] == '热门搜索')
        header = rows[start]
        self.assertEqual(header, ['热门搜索', '次数', '无结果', '平均结果', '无结果占比（%）'])
        data = [row for row in rows[start + 1:] if row and row[0] == '覆盖测试词']
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0][1], '3')
        self.assertEqual(data[0][2], '2')
        # 两次无结果、一次四条结果：平均 1.3，无结果占比 66.7%。
        self.assertEqual(data[0][3], '1.3')
        self.assertEqual(data[0][4], '66.7')