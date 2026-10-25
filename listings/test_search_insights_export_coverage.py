# -*- coding: utf-8 -*-
"""守住「洞察页面板有的，导出也必须有」这条线：搜索洞察导出的表和屏幕上看的要是同一份。

搜索洞察有 15 个分区、17 个面板，CSV 导出是另一份手写的代码。两边各写各的，结果是
运营把文件下载回去，读到的表和屏幕上看的对不上——有的面板在导出里整节失踪，有的面板
换了个名字，同一个指标在两处叫不同的标题。这种偏差不会让任何测试变红，因为每一处单独
看都是对的：面板渲染得出来，导出也导得出来，只有把它们放在一起比才会发现少了一块。

这个模块就是那次比较。它把渲染后的洞察页里的面板标题抓出来，再和 CSV 里的节标题对：
默认要求两边同名，只有确实不能同名的情况才在下面两张表里显式登记。

三条边界比其余更要紧。空态标题不算面板：这页的空态都是一句 empty-inline，本来就不该
有导出节。别名表里的每一条都必须写明理由，写不出理由的差异应该去改代码而不是往表里加
一行。以及这里只做比对，不改写页面、不改写导出，也不为了让某个标题对上而调整任何一边
的文案。

历史上这里有过七处差异，全部是靠「把导出的节标题改成和面板一样」消掉的：搜索词表叫过
「搜索词分析」、周期对比叫过「需求变化」、供给缺口整节失踪、三张商品表和一整节运营
建议连节标题都没有。现在节标题直接等于面板标题，登记表是空的——哪天它又长出一条，
说明有人宁愿在两套名字之间记映射，也不愿意把节标题改成和面板一样。
"""

import csv
import io
import re

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Category, Item, SearchClick, SearchImpression, SearchQuery


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


# 面板标题 -> 导出节标题。七处历史差异已经逐一改完，节标题现在直接等于面板标题，
# 所以这张表是空的。保留它是为了让「加一条别名」这件事在代码里有一个明确的位置：
# 新面板漏接导出时，测试会失败，而正确的修法是补一节，不是往这里加一行。
# 每一条真的需要登记的差异，都得在注释里写明为什么面板和导出不能同名。
PANEL_TO_SECTION = {
}

# 本来就不该有导出节的面板：空态文案，只在无数据时渲染出来。
NOT_EXPORTED = {
}


class SearchInsightsExportCoverageTests(TestCase):
    """每个面板都得在导出里有同名的节；同名是默认，登记是例外。"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='si-coverage-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(self.user)

    def fetch(self):
        html = self.client.get(reverse('search_insights'), {'days': 30}).content.decode('utf-8')
        text = self.client.get(
            reverse('search_insights_export'), {'days': 30},
        ).content.decode('utf-8-sig')
        return panel_titles(html), csv_first_cells(text), text

    def _seed(self):
        seller = User.objects.create_user(username='si-coverage-seller')
        category = Category.objects.create(name='覆盖测试分类')
        hot = Item.objects.create(
            seller=seller, category=category, title='覆盖测试热商品',
            price='10.00', status='available',
        )
        cold = Item.objects.create(
            seller=seller, category=category, title='覆盖测试冷商品',
            price='20.00', status='available',
        )
        searched = SearchQuery.objects.create(query='覆盖测试词', result_count=4)
        SearchQuery.objects.create(query='覆盖测试词', result_count=4)
        SearchQuery.objects.create(query='覆盖测试词', result_count=4)
        # 有结果、没人点：有搜索但未点击 只收这类词，带点击的词不进去。
        SearchQuery.objects.create(query='覆盖测试无点击词', result_count=4)
        for _ in range(2):
            SearchImpression.objects.create(search_query=searched, item=hot, position=1)
        SearchImpression.objects.create(search_query=searched, item=cold, position=2)
        for _ in range(2):
            SearchClick.objects.create(search_query=searched, item=hot, position=1)
        return hot, cold

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
        # 七处历史差异已经清完，这张表该是空的。哪天它又长出一条，说明有人宁愿
        # 在两套名字之间记映射，也不愿意把节标题改成和面板一样——那正是这个模块
        # 当初要拦的事，所以空表本身要作为契约钉住。
        titles, _, _ = self.fetch()

        self.assertTrue(titles)
        self.assertEqual(PANEL_TO_SECTION, {})

    def test_every_panel_title_appears_verbatim_in_the_export(self):
        # 上一条测试允许「面板不在导出里，但登记过」这条路；这条更严：没有任何
        # 登记时，每个面板标题都必须原样出现在导出的节标题里。
        titles, sections, _ = self.fetch()

        missing = [
            title for title in titles
            if title not in NOT_EXPORTED and title not in sections
        ]
        self.assertEqual(missing, [])

    def test_every_alias_points_at_a_section_the_export_really_writes(self):
        # 别名表指向一个导出里根本不存在的节，等于这条登记是空的：面板依旧是
        # 漏接的，而测试还会绿。所以每一条都要反过来验一遍。
        titles, sections, _ = self.fetch()

        broken = [
            '%s -> %s' % (panel, section)
            for panel, section in PANEL_TO_SECTION.items()
            if section not in sections
        ]
        self.assertEqual(broken, [])

    def test_every_alias_corresponds_to_a_panel_that_renders(self):
        # 面板被删了、被改名了，别名表里的那一条就成了孤儿：它守着一个不存在的面板。
        titles, _, _ = self.fetch()

        orphaned = [panel for panel in PANEL_TO_SECTION if panel not in titles]
        self.assertEqual(orphaned, [])

    def test_the_item_panels_have_their_own_sections_and_rows(self):
        # 这三块曾经整节失踪：页面上「曝光最多的商品」「被点击最多的商品」「有搜索但
        # 未点击」三块都在，导出去以后运营只能看到聚合指标，看不出是哪几件商品在
        # 承量、哪个词有结果却没人点。现在各自占一节，列名和面板列名一致。
        hot, cold = self._seed()
        _, sections, text = self.fetch()
        rows = list(csv.reader(io.StringIO(text)))

        for title in ('曝光最多的商品', '被点击最多的商品', '有搜索但未点击'):
            self.assertIn(title, sections)
        self.assertIn('出现在搜索次数', text)
        self.assertIn('来自搜索次数', text)

        start = next(i for i, row in enumerate(rows) if row and row[0] == '曝光最多的商品')
        self.assertEqual(
            rows[start],
            ['曝光最多的商品', '商品', '曝光次数', '出现在搜索次数'],
        )
        data = [row for row in rows[start + 1:] if row and row[0] == '曝光最多的商品']
        # 热商品两次曝光挂在同一条搜索上：曝光 2 次、出现在 1 次搜索里。
        self.assertEqual(data[0][1:], [hot.title, '2', '1'])
        self.assertEqual(data[1][1:], [cold.title, '1', '1'])

        start = next(i for i, row in enumerate(rows) if row and row[0] == '被点击最多的商品')
        self.assertEqual(
            rows[start],
            ['被点击最多的商品', '商品', '点击次数', '来自搜索次数'],
        )
        data = [row for row in rows[start + 1:] if row and row[0] == '被点击最多的商品']
        self.assertEqual(data[0][1:], [hot.title, '2', '1'])

        start = next(i for i, row in enumerate(rows) if row and row[0] == '有搜索但未点击')
        self.assertEqual(
            rows[start],
            ['有搜索但未点击', '搜索词', '搜索次数', '平均结果数'],
        )
        data = [row for row in rows[start + 1:] if row and row[0] == '有搜索但未点击']
        # 带点击的那个词不进这一节；无点击词搜一次、平均四条结果。
        self.assertEqual(data, [['有搜索但未点击', '覆盖测试无点击词', '1', '4.0']])

    def test_the_facet_gap_section_keeps_both_subheadings(self):
        # 面板里分类和地点是一节两个小标题；导出原来把它拆成两张没有归属的裸表。
        # 现在导出一节带两个子标题，和面板同一副样子。
        _, sections, text = self.fetch()

        self.assertIn('分类与地点缺口', sections)
        self.assertIn('分类需求', sections)
        self.assertIn('地点需求', sections)

    def test_the_export_carries_the_period_and_query_scope(self):
        # 统计周期和搜索词筛选是这一页的输入。下载回去的表要能自证自己算的是
        # 哪段时间、哪个词，否则运营对不上屏幕上的数字。
        _, _, text = self.fetch()

        self.assertIn('统计周期', text)
        self.assertIn('最近 30 天', text)
        self.assertIn('数据范围', text)
        self.assertIn('搜索词筛选', text)
