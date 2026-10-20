# -*- coding: utf-8 -*-
"""分页条在页数很多时不许把整页撑出横向滚动条。

商品、求购、失物招领三个列表页都是 12 条一页，数据一多就是几十页。原先模板
直接遍历 ``page_obj.paginator.page_range``，把每一页都渲染成一个按钮：实测
45 页时发现页的 ``scrollWidth`` 被撑到 1800px，而视口只有 1424px，整页凭空
多出一条横向滚动条，手机端还要更糟。

这里把四件事钉死：

1. 三个列表页都从 ``page_numbers`` 取页码，不再直接遍历 ``page_range``；
2. 页数越过阈值后分页条出现省略号，且首末页始终在场、当前页保持高亮；
3. 翻页链接原样带上筛选条件，翻页不丢筛选；
4. ``.pagination`` 自身允许换行，兜住将来新加的长分页条。
"""

import io
import os
import re
import uuid
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import CampusLocation, Category, DemandPost, Item, LostFoundPost

# 12 条一页，造 121 条是 11 页：既越过 (on_each_side + on_ends) * 2 = 4 的
# 省略阈值，也让「全量渲染 11 个按钮」和「省略渲染」的差异显出来。
PAGE_COUNT = 121
PER_PAGE = 12

PAGINATION_RE = re.compile(r'<ul class="pagination[^"]*">(.*?)</ul>', re.S)
PAGE_LINK_RE = re.compile(
    r'<a class="page-link" href="\?page=(\d+)([^"]*)"[^>]*>(.*?)</a>', re.S
)
GAP_RE = re.compile(r'class="page-link page-link-gap"')
ACTIVE_RE = re.compile(r'<li class="page-item active"><a class="page-link" href="\?page=(\d+)')

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS_PATH = os.path.join(PROJECT_ROOT, 'static', 'css', 'style.css')


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def make_category():
    return Category.objects.create(name=f'分页夹具 {uuid.uuid4().hex[:8]}')


def pagination_block(html):
    match = PAGINATION_RE.search(html)
    assert match is not None, '页面上没有分页条'
    return match.group(1)


def page_numbers(html):
    """按渲染顺序取回数字页码，排除上一页/下一页两个文字链接。"""
    numbers = []
    for match in PAGE_LINK_RE.finditer(pagination_block(html)):
        label = match.group(3).strip()
        if label in ('上一页', '下一页'):
            continue
        numbers.append(int(match.group(1)))
    return numbers


class PaginationElisionTests(TestCase):
    """三个列表页的长分页条都走省略渲染，并且不丢筛选条件。"""

    @classmethod
    def setUpTestData(cls):
        cls.user = make_user('pager')
        cls.category = make_category()
        cls.location = CampusLocation.objects.create(name='分页东门')
        cls.expected_pages = -(-PAGE_COUNT // PER_PAGE)  # 向上取整

        Item.objects.bulk_create([
            Item(
                title=f'分页商品 {index:03d}', description='分页夹具描述',
                price=Decimal('10.00'), condition='九成新', status='available',
                category=cls.category, location=cls.location, seller=cls.user,
            )
            for index in range(PAGE_COUNT)
        ])
        DemandPost.objects.bulk_create([
            DemandPost(
                title=f'分页求购 {index:03d}', description='分页求购描述',
                category=cls.category, location=cls.location, requester=cls.user,
            )
            for index in range(PAGE_COUNT)
        ])
        LostFoundPost.objects.bulk_create([
            LostFoundPost(
                title=f'分页失物 {index:03d}', description='分页失物描述',
                post_type='lost', category=cls.category, location=cls.location,
                reporter=cls.user, occurred_at=timezone.now(),
            )
            for index in range(PAGE_COUNT)
        ])

    def test_item_list_elides_page_range(self):
        response = self.client.get(reverse('item_list'), {'page': 6})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertTrue(GAP_RE.search(html), '中间页没有出现省略号')

        numbers = page_numbers(html)
        self.assertEqual(numbers, sorted(set(numbers)), '页码不是严格递增的')
        self.assertIn(1, numbers, '首页不在分页条里')
        self.assertIn(self.expected_pages, numbers, '末页不在分页条里')
        self.assertIn(6, numbers, '当前页不在分页条里')
        self.assertLess(
            len(numbers), self.expected_pages,
            '分页条仍然把每一页都渲染出来了',
        )

    def test_item_list_marks_current_page_active(self):
        response = self.client.get(reverse('item_list'), {'page': 4})
        html = response.content.decode()
        active = ACTIVE_RE.search(html)
        self.assertIsNotNone(active, '分页条没有高亮当前页')
        self.assertEqual(int(active.group(1)), 4)

    def test_item_list_keeps_filter_query_on_page_links(self):
        response = self.client.get(reverse('item_list'), {'sort': 'price_asc', 'page': 2})
        self.assertEqual(response.status_code, 200)
        hrefs = PAGE_LINK_RE.findall(pagination_block(response.content.decode()))
        self.assertTrue(hrefs, '分页条没有链接')
        for _number, query, _label in hrefs:
            self.assertIn('sort=price_asc', query, '翻页链接丢了排序条件')

    def test_demand_list_elides_page_range(self):
        response = self.client.get(reverse('demand_list'), {'page': 6})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertTrue(GAP_RE.search(html), '求购分页条没有出现省略号')
        numbers = page_numbers(html)
        self.assertIn(1, numbers)
        self.assertIn(self.expected_pages, numbers)
        self.assertLess(len(numbers), self.expected_pages)

    def test_lost_found_list_elides_page_range(self):
        response = self.client.get(reverse('lost_found_list'), {'page': 6})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertTrue(GAP_RE.search(html), '失物招领分页条没有出现省略号')
        numbers = page_numbers(html)
        self.assertIn(1, numbers)
        self.assertIn(self.expected_pages, numbers)
        self.assertLess(len(numbers), self.expected_pages)

    def test_ellipsis_is_never_rendered_as_a_link(self):
        """Django 的省略号是字符串常量，{% if num %} 会把它当真值。

        第一版实现直接把 get_elided_page_range 的结果交给模板，结果省略号被
        渲染成 <a href="?page=…">…</a>：看起来只是个省略号，点下去却是一个
        必然 404 的死链接。所以省略号必须走 <span>，不能走 <a>。
        """
        for view_name in ('item_list', 'demand_list', 'lost_found_list'):
            html = self.client.get(reverse(view_name), {'page': 6}).content.decode()
            self.assertNotIn('page=…', html, view_name + ' 把省略号渲染成了链接')
            self.assertTrue(GAP_RE.search(html), view_name + ' 没有渲染省略号')

    def test_pagination_css_wraps_so_long_bars_cannot_overflow(self):
        css = io.open(CSS_PATH, encoding='utf-8').read()
        rule = re.search(r'^\.pagination\s*\{([^}]*)\}', css, re.MULTILINE)
        self.assertIsNotNone(rule, 'style.css 里找不到 .pagination 规则')
        self.assertIn('flex-wrap', rule.group(1), '.pagination 不允许换行，长分页条仍会溢出')
