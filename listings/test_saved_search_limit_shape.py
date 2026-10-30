# -*- coding: utf-8 -*-
"""关注搜索单次提醒条数配置与被截断规模的测试。

这个模块读的两个量都带坑。``max_matches_per_notice`` 存的是配置值，而实际生效的值要过
``_clamp_limit`` 的 ``or 3`` 和 ``max(1, min(limit, 9))``——两者在越界数据上不是一回事，
所以测试必须分开造、分开断言。缓冲命中的 ``matched_at`` 是 ``auto_now_add``，本模块不按
它分档，但缓冲区只取最新的 20 条，造的命中超过 20 条时必须自己控制顺序。
"""

from django.contrib.auth.models import User
from django.test import TestCase

from .models import Category, CampusLocation, Item, SavedSearch, SavedSearchMatch
from .saved_search_limit_shape import (
    DEFAULT_LISTED_MATCHES, MAX_LISTED_MATCHES, MAX_SCANNED_MATCHES,
    MIN_BUFFERED_MATCHES, MIN_CADENCE_CELL, MIN_LIMIT_SEARCHES, NOISE_POINTS,
    TRUNCATED_LIST_LIMIT, build_saved_search_limit_shape,
)

# 参照点统一走 testing_clock：本地时间落到 23:59:00-24:00:00 时，各模块自己写的
# 23:59:00 会早于夹具的 auto_now_add 时间戳，把周期内记录整批排除。
from .testing_clock import local_tonight


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def make_item(seller, category, location, title='条数测试商品'):
    return Item.objects.create(
        seller=seller,
        title=title,
        description='用于测试条数统计的商品',
        category=category,
        location=location,
        condition='九成新',
        price='20.00',
    )


def make_search(owner, *, name='条数测试关注', limit=DEFAULT_LISTED_MATCHES,
                frequency='weekly'):
    """造一条关注，条数直接写进字段。

    ``max_matches_per_notice`` 有默认值但没有 ``auto_now``，创建时直接传即可，不需要像
    ``matched_at`` 那样造完再 update 回去。
    """
    return SavedSearch.objects.create(
        user=owner,
        name=name,
        query='测试',
        notify_frequency=frequency,
        max_matches_per_notice=limit,
    )


def make_buffered_match(saved_search, item):
    """造一条还没发出去的缓冲命中。"""
    return SavedSearchMatch.objects.create(saved_search=saved_search, item=item)


def make_buffered(saved_search, count, *, category, location, seller):
    """给一条关注造 count 条缓冲命中，返回这些命中。"""
    matches = []
    for index in range(count):
        matches.append(make_buffered_match(
            saved_search,
            make_item(seller, category, location, title=f'商品 {index}'),
        ))
    return matches


class SavedSearchLimitShapeTests(TestCase):
    def setUp(self):
        self.owner = make_user('limit-owner')
        self.seller = make_user('limit-seller')
        self.category = Category.objects.create(name='条数测试分类')
        self.location = CampusLocation.objects.create(name='条数测试地点')
        self.now = local_tonight()

    # ---------------------------------------------------------------- 九档分布

    def test_nine_limit_rows_are_all_listed_even_when_empty(self):
        """九档全列出来，包括一条关注都没有的档——漏掉空档会让人以为这一档也是齐的。"""
        result = build_saved_search_limit_shape(days=30, now=self.now)

        self.assertEqual(len(result['limit_rows']), MAX_LISTED_MATCHES)
        self.assertEqual(
            [row.limit for row in result['limit_rows']],
            list(range(1, MAX_LISTED_MATCHES + 1)),
        )

    def test_default_limit_is_flagged(self):
        """默认值 3 单独标出来，因为它同时是「主动配的」和「从没配过的」两种关注的会合点。"""
        make_search(self.owner, name='停在默认值', limit=DEFAULT_LISTED_MATCHES)
        make_search(self.owner, name='配成 5', limit=5)

        result = build_saved_search_limit_shape(days=30, now=self.now)

        default_row = next(r for r in result['limit_rows'] if r.is_default)
        self.assertEqual(default_row.limit, DEFAULT_LISTED_MATCHES)
        self.assertEqual(default_row.count, 1)
        five_row = next(r for r in result['limit_rows'] if r.limit == 5)
        self.assertEqual(five_row.count, 1)
        self.assertFalse(five_row.is_default)

    def test_share_denominator_is_all_searches_not_the_row_itself(self):
        """占比的分母是全部关注。拿一档除一档永远得到 100%，那是恒为真的废话。"""
        make_search(self.owner, name='停在 3', limit=3)
        make_search(self.owner, name='停在 3 二', limit=3)
        make_search(self.owner, name='停在 1', limit=1)

        result = build_saved_search_limit_shape(days=30, now=self.now)

        three_row = next(r for r in result['limit_rows'] if r.limit == 3)
        self.assertEqual(three_row.share, 66.7)

    def test_small_sample_flag_below_threshold(self):
        """不足门槛的关注数只报数字，不给方向。"""
        make_search(self.owner, name='只有一条', limit=3)

        result = build_saved_search_limit_shape(days=30, now=self.now)

        self.assertEqual(result['search_count'], 1)
        # 九档每一档都只有 0 或 1 条关注，全部不足门槛。
        self.assertTrue(all(row.count < MIN_LIMIT_SEARCHES
                        for row in result['limit_rows']))

    def test_no_searches_reports_no_data(self):
        """一条关注都没有时 has_data 为假，看板走空状态而不是报 0%。"""
        result = build_saved_search_limit_shape(days=30, now=self.now)

        self.assertFalse(result['has_data'])
        self.assertIn('没有任何关注搜索', result['summary'])

    # -------------------------------------------------- 配置值与实际生效值的缝隙

    def test_zero_is_not_counted_as_the_default_limit(self):
        """库里存的 0 实际按 3 条生效，但配置不是 3，两档分开数、不合并。"""
        make_search(self.owner, name='配成 0', limit=0)
        make_search(self.owner, name='配成 3', limit=3)

        result = build_saved_search_limit_shape(days=30, now=self.now)

        out_of_range = result['out_of_range']
        self.assertEqual(out_of_range['stored_count'], 1)
        self.assertEqual(out_of_range['effective_limit'], DEFAULT_LISTED_MATCHES)
        default_row = next(r for r in result['limit_rows'] if r.is_default)
        # 0 不并进默认档：并进去会让这一档同时装着两种人。
        self.assertEqual(default_row.count, 1)
        self.assertIn('实际按 3 条生效', result['summary'])

    def test_zero_actually_takes_effect_as_three(self):
        """库里存 0 的关注，实际生效的是 3 条——这一条守住和发送侧同一个式子。"""
        search = make_search(self.owner, name='配成 0', limit=0)
        make_buffered(search, 4, category=self.category,
                      location=self.location, seller=self.seller)

        result = build_saved_search_limit_shape(days=30, now=self.now)

        # 配成 0 实际按 3 条生效，4 条缓冲命中里被挡掉的是第 4 条，不是全部。
        self.assertEqual(result['truncation']['hidden_count'], 1)

    def test_out_of_range_high_value_clamps_to_nine(self):
        """越大的老数据实际按 9 条封顶，配置值本身单开一档。"""
        make_search(self.owner, name='配成 20', limit=20)

        result = build_saved_search_limit_shape(days=30, now=self.now)

        self.assertEqual(result['out_of_range']['stored_count'], 1)

    # ---------------------------------------------------------------- 被截断的规模

    def test_truncation_counts_only_buffered_hits(self):
        """被挡掉的条数只算此刻压在缓冲区里的命中，已经发出去的不保留痕迹。"""
        search = make_search(self.owner, name='配成 2', limit=2)
        make_buffered(search, 5, category=self.category,
                      location=self.location, seller=self.seller)

        result = build_saved_search_limit_shape(days=30, now=self.now)

        truncation = result['truncation']
        self.assertEqual(truncation['buffered_count'], 5)
        self.assertEqual(truncation['hidden_count'], 3)
        self.assertEqual(truncation['truncated_search_count'], 1)

    def test_truncated_share_denominator_is_all_searches(self):
        """「正在被截断的关注占比」的分母是全部关注，不是被截断的那一批。"""
        make_search(self.owner, name='被截断', limit=1)
        make_buffered(make_search(self.owner, name='被截断二', limit=1), 3,
                      category=self.category, location=self.location,
                      seller=self.seller)
        make_search(self.owner, name='没被截断', limit=9)

        result = build_saved_search_limit_shape(days=30, now=self.now)

        truncation = result['truncation']
        self.assertEqual(truncation['truncated_search_count'], 1)
        self.assertEqual(truncation['truncated_share'], 33.3)

    def test_mean_hidden_only_averages_truncated_searches(self):
        """平均被截掉几条只对被截断过的关注求平均——没被截断的关注压根没被截断。"""
        make_buffered(make_search(self.owner, name='截 3 条', limit=1), 4,
                      category=self.category, location=self.location,
                      seller=self.seller)
        # 这一条命中数正好等于条数，没有截断，不该把平均值拉低。
        make_buffered(make_search(self.owner, name='一条没截', limit=3), 3,
                      category=self.category, location=self.location,
                      seller=self.seller)

        result = build_saved_search_limit_shape(days=30, now=self.now)

        self.assertEqual(result['truncation']['mean_hidden_label'], '3.0 条')

    def test_no_buffered_matches_reports_dash_for_mean(self):
        """缓冲区空着时报「—」，不报 0——0 会被读成「截断了但很少」。"""
        make_search(self.owner, name='没有命中', limit=3)

        result = build_saved_search_limit_shape(days=30, now=self.now)

        self.assertEqual(result['truncation']['mean_hidden_label'], '—')
        self.assertEqual(result['truncation']['buffered_count'], 0)

    def test_empty_denominator_is_none_not_zero(self):
        """空分母报 None，模板显示「—」；分子 0、分母存在时报 0.0，两者不是一回事。"""
        make_search(self.owner, name='只有一条', limit=9)

        result = build_saved_search_limit_shape(days=30, now=self.now)

        # 分母存在、分子为 0：这一条就是 0.0，不是 None。
        self.assertEqual(result['truncation']['truncated_share'], 0.0)
        # 分母不存在：缓冲区是空的，hidden_share 是 None。
        self.assertIsNone(result['truncation']['hidden_share'])

    def test_buffer_only_scans_the_newest_twenty(self):
        """缓冲区只取发送侧扫过的那 20 条——第 21 条发送时压根看不到。"""
        search = make_search(self.owner, name='配成 1', limit=1)
        make_buffered(search, MAX_SCANNED_MATCHES + 5, category=self.category,
                      location=self.location, seller=self.seller)

        result = build_saved_search_limit_shape(days=30, now=self.now)

        self.assertEqual(result['truncation']['buffered_count'], MAX_SCANNED_MATCHES)
        self.assertEqual(result['truncation']['hidden_count'],
                         MAX_SCANNED_MATCHES - 1)

    def test_truncated_rows_omit_usernames_and_item_titles(self):
        """清单只列关注名称和数字，不列用户名、不列商品名。"""
        search = make_search(self.owner, name='我的私密关注', limit=1)
        make_buffered(search, 4, category=self.category,
                      location=self.location, seller=self.seller)

        result = build_saved_search_limit_shape(days=30, now=self.now)

        row = result['truncated_rows'][0]
        self.assertEqual(row.saved_search_name, '我的私密关注')
        self.assertEqual(row.hidden_count, 3)
        self.assertEqual(sorted(row._fields), sorted([
            'saved_search_id', 'saved_search_name', 'cadence_label',
            'configured', 'buffered_count', 'hidden_count',
        ]))

    def test_truncated_row_limit_is_reported(self):
        """清单有上限，上限之下的总量一起报出来，否则「完整的在导出里」没有依据。"""
        for index in range(TRUNCATED_LIST_LIMIT + 3):
            search = make_search(self.owner, name=f'关注 {index}', limit=1)
            make_buffered(search, 2, category=self.category,
                          location=self.location, seller=self.seller)

        result = build_saved_search_limit_shape(days=30, now=self.now)

        self.assertEqual(len(result['truncated_rows']), TRUNCATED_LIST_LIMIT)
        self.assertEqual(result['truncated_row_total'], TRUNCATED_LIST_LIMIT + 3)

    def test_truncated_rows_sorted_by_hidden_count_desc(self):
        """按被挡掉的条数降序，规模相同时按 pk，保证同一份数据每次给出同一张表。"""
        make_buffered(make_search(self.owner, name='挡 1 条', limit=2), 3,
                      category=self.category, location=self.location,
                      seller=self.seller)
        make_buffered(make_search(self.owner, name='挡 4 条', limit=1), 5,
                      category=self.category, location=self.location,
                      seller=self.seller)

        result = build_saved_search_limit_shape(days=30, now=self.now)

        self.assertEqual(
            [row.saved_search_name for row in result['truncated_rows']],
            ['挡 4 条', '挡 1 条'],
        )

    # ---------------------------------------------------------------- 条数 × 节奏

    def test_cadence_rows_cover_all_three_frequencies(self):
        """三种节奏全列出来，包括一条关注都没有的节奏。"""
        make_search(self.owner, name='即时', limit=3, frequency='instant')

        result = build_saved_search_limit_shape(days=30, now=self.now)

        self.assertEqual(
            [row['cadence'] for row in result['cadence_rows']],
            ['instant', 'daily', 'weekly'],
        )
        self.assertEqual(result['cadence_rows'][1]['search_count'], 0)

    def test_cadence_cross_is_shape_only_not_advice(self):
        """交叉只报形状：中位条数和截断占比，不给「该配多少条」的建议。"""
        for index in range(3):
            make_buffered(make_search(self.owner, name=f'周度 {index}', limit=1), 4,
                          category=self.category, location=self.location,
                          seller=self.seller)
        make_search(self.owner, name='即时', limit=9, frequency='instant')

        result = build_saved_search_limit_shape(days=30, now=self.now)

        weekly = next(r for r in result['cadence_rows'] if r['cadence'] == 'weekly')
        self.assertEqual(weekly['median_limit'], 1)
        self.assertEqual(weekly['truncated_share'], 100.0)
        # 每个格子都带自己的样本门槛，不足就标出来。
        self.assertTrue(weekly['is_small_sample'])
        self.assertEqual(result['min_cadence_cell'], MIN_CADENCE_CELL)

    def test_median_limit_uses_effective_value(self):
        """中位条数按实际生效的值算，不按库里存的配置值——存 0 的关注生效的是 3。"""
        make_search(self.owner, name='存 0', limit=0)
        make_search(self.owner, name='存 3', limit=3)
        make_search(self.owner, name='存 5', limit=5)

        result = build_saved_search_limit_shape(days=30, now=self.now)

        weekly = next(r for r in result['cadence_rows'] if r['cadence'] == 'weekly')
        self.assertEqual(weekly['median_limit'], 3)

    # ---------------------------------------------------------------- 门槛与周期

    def test_period_falls_back_to_thirty_for_unknown_days(self):
        """周期不在四个候选里时回落 30 天，不抛异常。"""
        result = build_saved_search_limit_shape(days=45, now=self.now)

        self.assertEqual(result['period_days'], 30)

    def test_thresholds_are_visible_on_the_panel(self):
        """门槛必须是面板上看得见的数，不是藏在代码里的魔法值。"""
        result = build_saved_search_limit_shape(days=30, now=self.now)

        self.assertEqual(result['min_limit_searches'], MIN_LIMIT_SEARCHES)
        self.assertEqual(result['min_buffered_matches'], MIN_BUFFERED_MATCHES)
        self.assertEqual(result['noise_points'], NOISE_POINTS)
        self.assertEqual(result['default_limit'], DEFAULT_LISTED_MATCHES)

    def test_small_buffered_sample_is_flagged(self):
        """缓冲命中不足门槛时，截断规模只报数字、不给方向。"""
        search = make_search(self.owner, name='只有两条命中', limit=1)
        make_buffered(search, 2, category=self.category,
                      location=self.location, seller=self.seller)

        result = build_saved_search_limit_shape(days=30, now=self.now)

        self.assertTrue(result['truncation']['is_small_sample'])
        self.assertEqual(result['min_buffered_matches'], MIN_BUFFERED_MATCHES)

    def test_module_does_not_write_anything(self):
        """只报告不改写：跑完之后关注和命中的字段一个都没动。"""
        search = make_search(self.owner, name='不动它', limit=2)
        make_buffered(search, 5, category=self.category,
                      location=self.location, seller=self.seller)
        before = SavedSearch.objects.get(pk=search.pk).max_matches_per_notice

        build_saved_search_limit_shape(days=30, now=self.now)

        after = SavedSearch.objects.get(pk=search.pk)
        self.assertEqual(after.max_matches_per_notice, before)
        self.assertIsNone(after.quiet_until)
        self.assertEqual(
            SavedSearchMatch.objects.filter(notified_at__isnull=True).count(), 5,
        )
