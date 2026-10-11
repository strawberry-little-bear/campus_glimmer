# -*- coding: utf-8 -*-
"""守住「面板有的，导出也必须有」这条线：看板导出的表和屏幕上看的要是同一份。

运营看板上有三十多个面板，CSV 导出是另一份手写的代码。两边各写各的，结果是运营把
文件下载回去，读到的表和屏幕上看的对不上——有的面板在导出里整节失踪，有的面板换了
个名字，同一个指标在两处叫不同的标题。这种偏差不会让任何测试变红，因为每一处单独看
都是对的：面板渲染得出来，导出也导得出来，只有把它们放在一起比才会发现少了一块。

这个模块就是那次比较。它把渲染后的看板 HTML 里的面板标题抓出来，再和 CSV 里的节
标题对：默认要求两边同名，只有确实不能同名的情况才在下面两张表里显式登记。历史上
先后有九处「面板叫这个、导出叫那个」的差异，都靠登记活着；这些差异已经逐一改完，
现在节标题直接等于面板标题，登记从常态退回了例外。

三条边界比其余更要紧。空态标题不算面板：暂时没有高优先级提醒这种文案只在无数据时
出现，它本来就不该有导出节。别名表里的每一条都必须写明理由，写不出理由的差异应该去
改代码而不是往表里加一行。以及这里只做比对，不改写看板、不改写导出，也不为了让某个
标题对上而调整任何一边的文案。
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
    """CSV 里每一行第一个非空格子的集合，节标题和子标题都是这么落的。"""
    return {row[0].strip() for row in csv.reader(io.StringIO(text)) if row and row[0].strip()}


# 面板标题 -> 导出节标题。九处历史差异已经逐一改完，节标题现在直接等于面板标题，
# 所以这张表是空的。保留它是为了让「加一条别名」这件事在代码里有一个明确的位置：
# 新面板漏接导出时，测试会失败，而正确的修法是补一节，不是往这里加一行。
# 每一条真的需要登记的差异，都得在注释里写明为什么面板和导出不能同名。
PANEL_TO_SECTION = {
}

# 本来就不该有导出节的面板：空态文案，只在无数据时渲染出来。
NOT_EXPORTED = {
    '暂时没有高优先级提醒',
}


class DashboardExportCoverageTests(TestCase):
    """每个面板都得在导出里有同名的节；同名是默认，登记是例外。"""

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

    def test_the_alias_table_is_empty_now(self):
        # 九处历史差异已经清完，这张表该是空的。哪天它又长出一条，说明有人宁愿
        # 在两套名字之间记映射，也不愿意把节标题改成和面板一样——那正是这个模块
        # 当初要拦的事，所以空表本身要作为契约钉住。
        titles, _, _ = self.fetch()

        self.assertTrue(titles)
        self.assertEqual(PANEL_TO_SECTION, {})

    def test_every_panel_title_appears_verbatim_in_the_export(self):
        # 上一条测试允许「面板不在导出里，但登记过」这条路；这条更严：没有任何
        # 登记时，每个面板标题都必须原样出现在导出的节标题里。空态文案是唯一例外。
        titles, sections, _ = self.fetch()

        missing = [
            title for title in titles
            if title not in NOT_EXPORTED and title not in sections
        ]
        self.assertEqual(missing, [])

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

    def test_the_demand_radar_opportunities_have_their_own_section(self):
        # 校园需求机会原来没有节标题，表格裸跟在待处理交易争议后面，下载回去的
        # 运营得靠数行才知道这块表是谁的。现在它自己占一节，列名也一并钉住：
        # 需求雷达的测试断言过第一列叫需求主题，改列名会一起红。
        _, sections, text = self.fetch()

        self.assertIn('校园需求机会', sections)
        self.assertIn('需求主题', text)

    def test_category_and_location_live_in_one_section_with_subheadings(self):
        # 分类与地点在面板里是一节，下面两个小标题；导出原来把它拆成两张没有
        # 归属的裸表。现在导出一节带两个子标题，和面板同一副样子。
        _, sections, text = self.fetch()

        self.assertIn('分类与地点', sections)
        self.assertIn('分类', sections)
        self.assertIn('地点', sections)

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