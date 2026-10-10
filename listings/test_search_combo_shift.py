# -*- coding: utf-8 -*-
"""Cover the cross-period comparison of filter combinations.

The single-facet comparison is already covered elsewhere. What these tests pin
down is the step it cannot take, and every boundary that step needs: a
combination is one cell of a two-dimensional cross, so it is strictly less well
evidenced than either single facet, and it has to be measured in a way that
actually answers the operator's question rather than an easier one.

Five things must hold. The share is taken inside the category, because a share
taken against all searches would move with total volume and would compare
unrelated categories against each other. Two single facets rising together
must not be reported as one combination rising, since the two signals can come
from two unrelated groups of students and adding them is arithmetic on a number
nobody measured. A thin cell is held back instead of being given a direction,
and the floor is never lowered to make the table look fuller. A pair that
exists in only one period is reported as new or gone without a share delta,
because there is no earlier share to subtract from. And nothing is written: the
module reads the search log, adds no table and changes no threshold.
"""

from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Category, SearchQuery
from .search_combo_shift import (
    MIN_COMBINATION_SEARCHES,
    build_search_combo_shift,
)


def _local_noon_today():
    """参考时刻锹在本地当天正午，而不是运行刻。

    跨周期面板按本地午夜切窗口，而测试里的时间戳往前推了几天。
    如果参考时刻就取运行刻，那么每当本地时间走过午夜、UTC
    还停在前一天的那几个小时，“今天”的搜索就会落到窗口上界之外。
    正午既跑完了当天该有的搜索，也远远跑不出本地这一天，
    于是无论什么时候跑这个测试，结果都一样。
    """
    from django.utils import timezone
    today = timezone.localdate()
    noon = timezone.datetime.combine(today, timezone.datetime.min.time()) + timedelta(hours=12)
    return timezone.make_aware(noon)


class ComboShiftSetupTests(TestCase):
    """Window construction and the scope the comparison is built on."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='combo-user', password='safe-password-123',
        )
        self.now = _local_noon_today()
        self.books = Category.objects.create(name='组合图书')
        self.lamps = Category.objects.create(name='组合台灯')

    def search(self, *, days_ago, category, min_price=None, max_price=None,
               term='查询', result_count=3):
        query = SearchQuery.objects.create(
            user=self.user, query=term, category=category,
            min_price=min_price, max_price=max_price, result_count=result_count,
        )
        SearchQuery.objects.filter(pk=query.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        return query

    def test_an_empty_history_reports_no_data_rather_than_zeroes(self):
        combo = build_search_combo_shift(days=7, now=self.now)
        self.assertFalse(combo['has_data'])
        self.assertEqual(combo['rows'], [])
        self.assertEqual(combo['current_searches'], 0)
        self.assertEqual(combo['previous_searches'], 0)

    def test_windows_cover_the_same_number_of_calendar_days(self):
        self.search(days_ago=1, category=self.books, min_price=Decimal('10.00'))
        combo = build_search_combo_shift(days=7, now=self.now)
        current_span = combo['current_period_end'].date() - combo['current_period_start'].date()
        previous_span = combo['current_period_start'].date() - combo['previous_period_start'].date()
        # 与单维筛选同一套窗口口径：两个等长窗口首尾相接，本周期末尾是未完成的当天。
        self.assertEqual(current_span.days, 6)
        self.assertEqual(previous_span.days, 7)
        self.assertEqual(combo['previous_period_start'].time(), time.min)

    def test_a_search_without_a_category_carries_no_combination(self):
        # 没选分类的搜索不提供任何「分类 × 价格带」证据：它落在哪个分类都
        # 不知道，硬塞进某一行只会把那一行的占比算错。
        for offset in range(3):
            SearchQuery.objects.create(
                user=self.user, query='无线', min_price=Decimal('10.00'),
                max_price=Decimal('40.00'), result_count=2,
            )
        combo = build_search_combo_shift(days=7, now=self.now)
        self.assertEqual(combo['rows'], [])
        self.assertEqual(combo['current_searches'], 0)

    def test_the_query_filter_narrows_both_windows(self):
        self.search(days_ago=1, category=self.books, min_price=Decimal('10.00'))
        self.search(days_ago=1, category=self.lamps, term='台灯', min_price=Decimal('10.00'))
        combo = build_search_combo_shift(days=7, now=self.now, query='台灯')
        # 页面已经按搜索词收窄时，组合表不能把范围悄悄放大回全站。
        self.assertEqual(combo['query_filter'], '台灯')
        self.assertEqual(combo['current_searches'], 1)
        self.assertEqual(combo['rows'][0]['category_label'], '组合台灯')


class ComboShareTests(TestCase):
    """What the share inside a category means, and what it must not mean."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='combo-share-user', password='safe-password-123',
        )
        self.now = _local_noon_today()
        self.books = Category.objects.create(name='占比图书')
        self.lamps = Category.objects.create(name='占比台灯')

    def search(self, *, days_ago, category, min_price=None, max_price=None):
        query = SearchQuery.objects.create(
            user=self.user, query='查询', category=category,
            min_price=min_price, max_price=max_price, result_count=3,
        )
        SearchQuery.objects.filter(pk=query.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        return query

    def test_the_share_is_taken_inside_the_category_not_against_all_searches(self):
        # 上一周期图书 8 次里 4 次落在 50-200 元带；本周期图书仍是 8 次，其中
        # 6 次落在该带，同时台灯分类从 0 次涨到 12 次。若按全站搜索做分母，
        # 图书的组合占比会被台灯的增长稀释，那和“图书的价位口味变了”无关。
        for _ in range(4):
            self.search(days_ago=8, category=self.books, min_price=Decimal('60.00'), max_price=Decimal('90.00'))
        for _ in range(4):
            self.search(days_ago=8, category=self.books, min_price=Decimal('300.00'), max_price=Decimal('400.00'))
        for _ in range(6):
            self.search(days_ago=1, category=self.books, min_price=Decimal('60.00'), max_price=Decimal('90.00'))
        for _ in range(2):
            self.search(days_ago=1, category=self.books, min_price=Decimal('300.00'), max_price=Decimal('400.00'))
        for _ in range(12):
            self.search(days_ago=1, category=self.lamps, min_price=Decimal('10.00'), max_price=Decimal('20.00'))

        combo = build_search_combo_shift(days=7, now=self.now)
        books = self.category_for(combo, self.books.id)
        row = self.band_for(books, '50_to_200')

        self.assertEqual(books['current_total'], 8)
        self.assertEqual(row['current_count'], 6)
        self.assertEqual(row['current_share'], 75.0)
        self.assertEqual(row['previous_share'], 50.0)
        self.assertEqual(row['share_delta'], 25.0)
        self.assertEqual(row['direction'], 'rising')

    def test_two_facets_rising_together_is_not_reported_as_a_combination_rising(self):
        # 关键边界：图书占比在单维表里上升，50-200 元带占比也上升，但组合层面
        # 完全没有动。把两个单维信号相加当成组合信号，是对一个没人测过的
        # 数字做算术。
        for _ in range(4):
            self.search(days_ago=8, category=self.books, min_price=Decimal('60.00'), max_price=Decimal('90.00'))
            self.search(days_ago=8, category=self.books, min_price=Decimal('300.00'), max_price=Decimal('400.00'))
            self.search(days_ago=8, category=self.lamps, min_price=Decimal('10.00'), max_price=Decimal('20.00'))
        for _ in range(4):
            self.search(days_ago=1, category=self.books, min_price=Decimal('60.00'), max_price=Decimal('90.00'))
            self.search(days_ago=1, category=self.books, min_price=Decimal('300.00'), max_price=Decimal('400.00'))
            self.search(days_ago=1, category=self.lamps, min_price=Decimal('300.00'), max_price=Decimal('400.00'))

        combo = build_search_combo_shift(days=7, now=self.now)
        books = self.category_for(combo, self.books.id)
        row = self.band_for(books, '50_to_200')

        self.assertEqual(row['current_share'], 50.0)
        self.assertEqual(row['previous_share'], 50.0)
        self.assertEqual(row['share_delta'], 0.0)
        self.assertEqual(row['direction'], 'flat')

    def test_a_platform_wide_rise_does_not_move_the_combination_share(self):
        # 全站搜索量翻倍、每个分类内部结构不变时，没有任何一个组合该被判为
        # 上升——这正是占比口径要防的情况。
        for _ in range(6):
            self.search(days_ago=8, category=self.books, min_price=Decimal('60.00'), max_price=Decimal('90.00'))
        for _ in range(2):
            self.search(days_ago=8, category=self.books, min_price=Decimal('300.00'), max_price=Decimal('400.00'))
        for _ in range(12):
            self.search(days_ago=1, category=self.books, min_price=Decimal('60.00'), max_price=Decimal('90.00'))
        for _ in range(4):
            self.search(days_ago=1, category=self.books, min_price=Decimal('300.00'), max_price=Decimal('400.00'))

        combo = build_search_combo_shift(days=7, now=self.now)
        row = self.band_for(self.category_for(combo, self.books.id), '50_to_200')
        self.assertEqual(row['current_share'], 75.0)
        self.assertEqual(row['previous_share'], 75.0)
        self.assertEqual(row['direction'], 'flat')

    def test_a_band_boundary_uses_the_bounds_the_student_set(self):
        # 与单维价格带同一套判据：闭带要求两个界都设，只设上限
        # 0 不落入免费带；两个价格都没填的搜索不属于任何价格带。
        self.search(days_ago=1, category=self.books, min_price=Decimal('0'), max_price=Decimal('0'))
        self.search(days_ago=1, category=self.books, max_price=Decimal('0'))
        self.search(days_ago=1, category=self.books)
        combo = build_search_combo_shift(days=7, now=self.now)
        books = self.category_for(combo, self.books.id)
        self.assertEqual(self.band_for(books, 'free')['current_count'], 1)
        self.assertEqual(sum(row['current_count'] for row in books['rows']), 1)

    def category_for(self, combo, category_id):
        for row in combo['rows']:
            if row['category_id'] == category_id:
                return row
        raise AssertionError(f'{category_id} not in combo rows')

    def band_for(self, category, key):
        for row in category['rows']:
            if row['key'] == key:
                return row
        raise AssertionError(f'{key} not in {category["category_label"]} rows')


class ComboSampleTests(TestCase):
    """Sparse cells are held back rather than guessed at."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='combo-sample-user', password='safe-password-123',
        )
        self.now = _local_noon_today()
        self.books = Category.objects.create(name='样本图书')

    def search(self, *, days_ago, min_price=None, max_price=None):
        query = SearchQuery.objects.create(
            user=self.user, query='查询', category=self.books,
            min_price=min_price, max_price=max_price, result_count=3,
        )
        SearchQuery.objects.filter(pk=query.pk).update(
            created_at=self.now - timedelta(days=days_ago),
        )
        return query

    def test_a_combination_below_the_floor_is_held_back(self):
        # 分类内只有 1 次搜索落在该价格带，占比只能是 0% 或 100%，此时判方向
        # 和抛硬币没有区别。门槛与单维筛选完全一致，不因为组合表空格子多
        # 就往下调。
        for _ in range(5):
            self.search(days_ago=1, min_price=Decimal('300.00'))
        self.search(days_ago=1, min_price=Decimal('60.00'), max_price=Decimal('90.00'))
        for _ in range(2):
            self.search(days_ago=8, min_price=Decimal('60.00'), max_price=Decimal('90.00'))
        for _ in range(6):
            self.search(days_ago=8, min_price=Decimal('300.00'), max_price=Decimal('400.00'))

        combo = build_search_combo_shift(days=7, now=self.now)
        row = self.band_for(combo['rows'][0], '50_to_200')

        self.assertEqual(row['current_count'], 1)
        self.assertEqual(row['current_share'], 16.7)
        self.assertEqual(row['direction'], 'insufficient')
        self.assertEqual(row['direction_label'], '样本不足')
        self.assertNotIn('rising', combo['direction_counts'])

    def test_the_floor_matches_the_single_facet_floor(self):
        # 组合比单维更容易稀疏，门槛却一分不降：单维那里算太薄的证据，不能
        # 在组合表里变成“足够薄所以够了”。
        self.assertEqual(MIN_COMBINATION_SEARCHES, 4)

    def test_a_pair_present_in_only_one_period_gets_no_share_delta(self):
        for _ in range(5):
            self.search(days_ago=1, min_price=Decimal('300.00'), max_price=Decimal('400.00'))
        combo = build_search_combo_shift(days=7, now=self.now)
        row = self.band_for(combo['rows'][0], 'over_200')

        self.assertEqual(row['direction'], 'new')
        self.assertEqual(row['direction_label'], '本期新出现')
        self.assertIsNone(row['share_delta'])
        self.assertIsNone(row['previous_share'])

    def test_a_pair_that_disappears_is_reported_as_gone(self):
        for _ in range(5):
            self.search(days_ago=8, min_price=Decimal('300.00'), max_price=Decimal('400.00'))
        for _ in range(5):
            self.search(days_ago=1, min_price=Decimal('60.00'), max_price=Decimal('90.00'))
        combo = build_search_combo_shift(days=7, now=self.now)
        row = self.band_for(combo['rows'][0], 'over_200')

        self.assertEqual(row['direction'], 'gone')
        self.assertEqual(row['direction_label'], '本期已消失')
        # 本周期这个分类还有 5 次搜索，占比就是真实的 0%，而不是「算不出来」；
        # 同时占比差也因此能算出来，与单维表对新出现、已消失行的口径一致。
        self.assertEqual(row['current_share'], 0.0)
        self.assertEqual(row['previous_share'], 100.0)
        self.assertEqual(row['share_delta'], -100.0)

    def test_categories_are_capped_by_the_limit(self):
        # 校园平台的分类数量可能不少，表要能被读完，因此只保留搜索量最高的
        # 几个分类，剩下的计入总数。
        for index in range(3):
            category = Category.objects.create(name=f'样本分类{index}')
            for offset in range(index + 5):
                query = SearchQuery.objects.create(
                    user=self.user, query='查询', category=category,
                    min_price=Decimal('60.00'), max_price=Decimal('90.00'), result_count=2,
                )
                SearchQuery.objects.filter(pk=query.pk).update(
                    created_at=self.now - timedelta(days=1),
                )
        combo = build_search_combo_shift(days=7, now=self.now, limit=2)
        self.assertEqual(len(combo['rows']), 2)
        self.assertEqual(combo['row_total'], 3)

    def band_for(self, category, key):
        for row in category['rows']:
            if row['key'] == key:
                return row
        raise AssertionError(f'{key} not in {category["category_label"]} rows')


class ComboPanelTests(TestCase):
    """The panel, the CSV export and the boundary they must state out loud."""

    def setUp(self):
        self.now = _local_noon_today()
        self.staff = User.objects.create_superuser(
            username='combo-staff', email='combo-staff@example.com',
            password='safe-password-123',
        )

    def test_the_panel_appears_on_the_insights_page(self):
        books = Category.objects.create(name='面板图书')
        for _ in range(5):
            query = SearchQuery.objects.create(
                user=self.staff, query='查询', category=books,
                min_price=Decimal('60.00'), result_count=3,
            )
            SearchQuery.objects.filter(pk=query.pk).update(
                created_at=self.now - timedelta(days=1),
            )
        self.client.force_login(self.staff)

        response = self.client.get(reverse('search_insights'), {'days': 7})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '分类与价格带组合')
        self.assertContains(response, '面板图书')
        self.assertContains(response, '50-200 元')

    def test_the_page_states_that_the_floor_is_not_lowered(self):
        books = Category.objects.create(name='门槛图书')
        for _ in range(5):
            query = SearchQuery.objects.create(
                user=self.staff, query='查询', category=books,
                min_price=Decimal('60.00'), result_count=3,
            )
            SearchQuery.objects.filter(pk=query.pk).update(
                created_at=self.now - timedelta(days=1),
            )
        self.client.force_login(self.staff)

        response = self.client.get(reverse('search_insights'), {'days': 7})

        self.assertContains(response, '样本不足')

    def test_the_combination_is_included_in_csv_export(self):
        books = Category.objects.create(name='导出图书')
        for _ in range(5):
            query = SearchQuery.objects.create(
                user=self.staff, query='查询', category=books,
                min_price=Decimal('60.00'), result_count=3,
            )
            SearchQuery.objects.filter(pk=query.pk).update(
                created_at=self.now - timedelta(days=1),
            )
        self.client.force_login(self.staff)

        export = self.client.get(reverse('search_insights_export'), {'days': 7})
        content = export.content.decode('utf-8-sig')

        self.assertEqual(export.status_code, 200)
        self.assertIn('分类与价格带组合', content)
        self.assertIn('分类内占比', content)
        self.assertIn('导出图书', content)

    def test_a_non_staff_user_cannot_reach_the_panel(self):
        self.client.force_login(
            User.objects.create_user(username='combo-visitor', password='safe-password-123'),
        )
        response = self.client.get(reverse('search_insights'), {'days': 7})
        self.assertEqual(response.status_code, 403)
