# -*- coding: utf-8 -*-
"""关注搜索临时静默的测试。

这个模块读的三个量都带一个共同的坑：``quiet_until`` 和缓冲命中的 ``matched_at``
都是创建时写进去的真实时间，测试造的记录如果直接用库里的当前时刻做参照点，就会被
``quiet_until__gt=now`` 和 ``__lte=now`` 整批排除。所以这里统一用 ``local_tonight()``
做参照点，见下面的注释。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import Category, CampusLocation, Item, SavedSearch, SavedSearchMatch
from .saved_search_quiet_window import (
    BUFFER_AGE_BUCKETS, EXPIRED_LIST_LIMIT, MIN_BUFFERED_MATCHES,
    MIN_QUIET_SEARCHES, build_saved_search_quiet_window,
)


def local_tonight():
    """今天 23:59，一个确保晚于测试运行时刻的参照点。

    ``quiet_until`` 和缓冲命中的 ``matched_at`` 都是创建时写进去的真实当前时间。
    参照点必须晚于它们，否则「正在静默」会被 ``quiet_until__gt=now`` 整批排除、
    「早已过期」会被 ``__lte=now`` 整批排除——用「今天中午」在下午跑测试就会踩到
    这个坑，而它长得像一个计算错误。
    """
    return timezone.localtime().replace(hour=23, minute=59, second=0, microsecond=0)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def make_item(seller, category, location, title='静默测试商品'):
    return Item.objects.create(
        seller=seller,
        title=title,
        description='用于测试静默统计的商品',
        category=category,
        location=location,
        condition='九成新',
        price='20.00',
    )


def make_search(owner, *, name='静默测试关注', frequency='daily', quiet_until=None):
    return SavedSearch.objects.create(
        user=owner,
        name=name,
        query='测试',
        notify_frequency=frequency,
        quiet_until=quiet_until,
    )


def make_buffered_match(saved_search, item, *, matched_at):
    """造一条还没发出去的缓冲命中，把 ``matched_at`` 写回指定时刻。

    ``matched_at`` 是 ``auto_now_add``，创建时只能拿到当前时刻，所以造完必须用
    ``.update()`` 写回。这一句是整个测试文件的关键：不写回，所有按年龄分档的断言
    都会落在「当天」那一档。
    """
    row = SavedSearchMatch.objects.create(saved_search=saved_search, item=item)
    SavedSearchMatch.objects.filter(pk=row.pk).update(matched_at=matched_at)
    row.refresh_from_db()
    return row


class SavedSearchQuietWindowTests(TestCase):
    def setUp(self):
        self.owner = make_user('quiet-owner')
        self.seller = make_user('quiet-seller')
        self.category = Category.objects.create(name='静默测试分类')
        self.location = CampusLocation.objects.create(name='静默测试地点')
        self.now = local_tonight()

    # ---------------------------------------------------------------- 三档状态

    def test_three_quiet_states_are_counted_separately(self):
        """正在静默、早已过期、从没静默过是三件事，不能合成一个「静默过」。"""
        make_search(self.owner, name='正在静默', quiet_until=self.now + timedelta(days=3))
        make_search(self.owner, name='早已过期', quiet_until=self.now - timedelta(days=5))
        make_search(self.owner, name='从没静默')

        result = build_saved_search_quiet_window(days=30, now=self.now)

        self.assertEqual(result['quiet_now_count'], 1)
        self.assertEqual(result['quiet_expired_count'], 1)
        self.assertEqual(result['never_quiet_count'], 1)
        labels = {row['state']: row['label'] for row in result['state_rows']}
        self.assertEqual(labels['quiet_now'], '正在静默')
        self.assertEqual(labels['quiet_expired'], '静默已过期')
        self.assertEqual(labels['never_quiet'], '从没静默过')

    def test_no_quiet_searches_reports_no_data(self):
        """一条静默都没有时 has_data 为假，看板走空状态而不是报 0%。"""
        make_search(self.owner, name='只有一条普通关注')

        result = build_saved_search_quiet_window(days=30, now=self.now)

        self.assertFalse(result['has_data'])
        self.assertIn('没有处在静默中的关注', result['summary'])

    def test_share_denominator_is_all_searches_not_the_state_itself(self):
        """占比的分母是全部关注。拿一档除一档永远得到 100%，那是恒为真的废话。"""
        for index in range(3):
            make_search(self.owner, name=f'静默 {index}', quiet_until=self.now + timedelta(days=2))
        make_search(self.owner, name='普通关注')

        result = build_saved_search_quiet_window(days=30, now=self.now)

        quiet_row = next(r for r in result['state_rows'] if r['state'] == 'quiet_now')
        self.assertEqual(quiet_row['share'], 75.0)

    def test_small_sample_flag_below_threshold(self):
        """不足门槛的关注数只报数字，不给方向。"""
        make_search(self.owner, name='孤零零一条', quiet_until=self.now + timedelta(days=1))

        result = build_saved_search_quiet_window(days=30, now=self.now)

        quiet_row = next(r for r in result['state_rows'] if r['state'] == 'quiet_now')
        self.assertTrue(quiet_row['is_small_sample'])
        self.assertLess(quiet_row['search_count'], MIN_QUIET_SEARCHES)

    # ---------------------------------------------------------------- 缓冲区

    def test_buffered_matches_are_counted_per_state(self):
        """正在静默攒命中是暂停键的本意，早已过期还压着没发是另一件事，分两列报。"""
        quiet_search = make_search(self.owner, name='静默中', quiet_until=self.now + timedelta(days=5))
        expired_search = make_search(self.owner, name='已过期', quiet_until=self.now - timedelta(days=2))
        for index in range(2):
            make_buffered_match(
                quiet_search, make_item(self.seller, self.category, self.location, f'静默商品 {index}'),
                matched_at=self.now - timedelta(days=1),
            )
        make_buffered_match(
            expired_search, make_item(self.seller, self.category, self.location, '过期商品'),
            matched_at=self.now - timedelta(days=20),
        )

        result = build_saved_search_quiet_window(days=30, now=self.now)

        self.assertEqual(result['buffered_now_count'], 2)
        self.assertEqual(result['buffered_expired_count'], 1)

    def test_buffer_age_buckets_are_mutually_exclusive(self):
        """缓冲区分档必须互斥且完备：每条命中只能落进一档。"""
        search = make_search(self.owner, quiet_until=self.now + timedelta(days=9))
        for index, days_ago in enumerate((0, 1, 3, 10, 30)):
            make_buffered_match(
                search, make_item(self.seller, self.category, self.location, f'商品 {index}'),
                matched_at=self.now - timedelta(days=days_ago),
            )

        result = build_saved_search_quiet_window(days=30, now=self.now)

        rows = result['buffer_now']['rows']
        self.assertEqual(len(rows), len(BUFFER_AGE_BUCKETS))
        counted = sum(row['count'] for row in rows)
        self.assertEqual(counted, 5)
        self.assertEqual([row['count'] for row in rows], [1, 1, 1, 1, 1])

    def test_buffer_age_oldest_is_reported_as_a_label_not_a_number(self):
        """最早一条缓冲命中按天说话，不足一天说「当天」。"""
        search = make_search(self.owner, quiet_until=self.now + timedelta(days=9))
        make_buffered_match(
            search, make_item(self.seller, self.category, self.location, '老商品'),
            matched_at=self.now - timedelta(days=6, hours=2),
        )

        result = build_saved_search_quiet_window(days=30, now=self.now)

        self.assertEqual(result['buffer_now']['oldest_label'], '6 天')

    def test_delivered_matches_do_not_count_as_buffered(self):
        """已经发出去的命中不进缓冲区——缓冲区只压还没发的。"""
        search = make_search(self.owner, quiet_until=self.now + timedelta(days=5))
        row = make_buffered_match(
            search, make_item(self.seller, self.category, self.location),
            matched_at=self.now - timedelta(days=1),
        )
        SavedSearchMatch.objects.filter(pk=row.pk).update(notified_at=self.now)

        result = build_saved_search_quiet_window(days=30, now=self.now)

        self.assertEqual(result['buffered_now_count'], 0)

    def test_stale_buffer_rate_is_none_when_nothing_is_buffered(self):
        """空分母报 None 不报 0：0 会被读成「占比很小」，而事实是没有样本。"""
        make_search(self.owner, quiet_until=self.now + timedelta(days=5))

        result = build_saved_search_quiet_window(days=30, now=self.now)

        self.assertIsNone(result['buffer_now']['stale_rate'])
        self.assertTrue(result['buffer_now']['is_small_sample'])

    def test_unavailable_items_are_counted_as_stale(self):
        """送达时商品已不可交易这一栏系统性偏高，但必须真的数出来。"""
        search = make_search(self.owner, quiet_until=self.now + timedelta(days=9))
        sold_item = make_item(self.seller, self.category, self.location, '已卖掉的商品')
        sold_item.status = 'sold'
        sold_item.save()
        make_buffered_match(search, sold_item, matched_at=self.now - timedelta(days=1))

        result = build_saved_search_quiet_window(days=30, now=self.now)

        self.assertEqual(result['buffer_now']['stale_count'], 1)

    # ---------------------------------------------------------------- 静默之后

    def test_expired_search_with_no_later_hits_is_counted_as_silent(self):
        """静默过期之后一条新命中都没有的关注单独数出来，不读成「静默赶走了用户」。"""
        make_search(self.owner, name='暂停后没回来', quiet_until=self.now - timedelta(days=10))

        result = build_saved_search_quiet_window(days=30, now=self.now)

        delivered = result['delivered']
        self.assertEqual(delivered['total'], 1)
        self.assertEqual(delivered['silent_count'], 1)
        self.assertEqual(delivered['alive_count'], 0)

    def test_expired_search_with_later_hits_is_counted_as_alive(self):
        """静默过期之后还有新命中，说明这条关注还活着。"""
        search = make_search(self.owner, quiet_until=self.now - timedelta(days=10))
        make_buffered_match(
            search, make_item(self.seller, self.category, self.location, '后来的商品'),
            matched_at=self.now - timedelta(days=2),
        )

        result = build_saved_search_quiet_window(days=30, now=self.now)

        delivered = result['delivered']
        self.assertEqual(delivered['alive_count'], 1)
        self.assertEqual(delivered['silent_count'], 0)

    def test_delivered_rows_never_leak_usernames_or_item_titles(self):
        """清单只带关注名称——它是用户给条件起的名字；不带用户名、不带商品名。"""
        search = make_search(self.owner, name='我的考研资料关注', quiet_until=self.now - timedelta(days=10))
        make_buffered_match(
            search, make_item(self.seller, self.category, self.location, '某本绝版书'),
            matched_at=self.now - timedelta(days=2),
        )

        result = build_saved_search_quiet_window(days=30, now=self.now)

        row = result['delivered']['rows'][0]
        self.assertEqual(row['saved_search_name'], '我的考研资料关注')
        for forbidden in (self.owner.username, self.seller.username, '某本绝版书'):
            self.assertNotIn(forbidden, repr(row))

    def test_expired_for_days_uses_quiet_until_not_now_minus_created(self):
        """「暂停了多久没回来」的起点是静默结束，不是关注创建。"""
        search = make_search(self.owner, quiet_until=self.now - timedelta(days=4))

        result = build_saved_search_quiet_window(days=30, now=self.now)

        row = result['delivered']['rows'][0]
        self.assertAlmostEqual(row['expired_for_days'], 4.0, delta=0.2)

    def test_delivered_rate_is_none_without_expired_searches(self):
        """没有静默过期的关注时报 None，不报 0%。"""
        make_search(self.owner, quiet_until=self.now + timedelta(days=3))

        result = build_saved_search_quiet_window(days=30, now=self.now)

        self.assertIsNone(result['delivered']['alive_rate'])

    def test_never_quiet_searches_are_not_counted_as_expired(self):
        """从没静默过的关注不进「静默已过期」那一档。"""
        make_search(self.owner, name='普通关注')

        result = build_saved_search_quiet_window(days=30, now=self.now)

        self.assertEqual(result['delivered']['total'], 0)
        self.assertEqual(result['quiet_expired_count'], 0)

    # ---------------------------------------------------------------- 汇总与门槛

    def test_summary_mentions_both_quiet_and_expired(self):
        """汇总一句话要把两档都说到，少一档读者就会以为另一档不存在。"""
        make_search(self.owner, name='静默中', quiet_until=self.now + timedelta(days=5))
        make_search(self.owner, name='已过期', quiet_until=self.now - timedelta(days=3))

        result = build_saved_search_quiet_window(days=30, now=self.now)

        self.assertIn('正在静默', result['summary'])
        self.assertIn('静默已过期', result['summary'])

    def test_invalid_days_falls_back_to_default(self):
        """周期参数不在允许集合里时回落到 30 天，不抛异常。"""
        result = build_saved_search_quiet_window(days=999, now=self.now)

        self.assertEqual(result['period_days'], 30)

    def test_buffered_matches_below_threshold_do_not_give_direction(self):
        """缓冲命中不足门槛时只报数字，年龄结构不给方向。"""
        search = make_search(self.owner, quiet_until=self.now + timedelta(days=5))
        for index in range(3):
            make_buffered_match(
                search, make_item(self.seller, self.category, self.location, f'商品 {index}'),
                matched_at=self.now - timedelta(days=1),
            )

        result = build_saved_search_quiet_window(days=30, now=self.now)

        self.assertEqual(result['buffer_now']['measured_count'], 3)
        self.assertTrue(result['buffer_now']['is_small_sample'])
        self.assertLess(MIN_BUFFERED_MATCHES, 100)

    def test_all_three_cadences_can_be_quiet(self):
        """静默是全部节奏共有的能力，即时关注也能填 quiet_until。"""
        for index, frequency in enumerate(('instant', 'daily', 'weekly')):
            make_search(
                self.owner, name=f'{frequency} 关注', frequency=frequency,
                quiet_until=self.now + timedelta(days=2),
            )

        result = build_saved_search_quiet_window(days=30, now=self.now)

        self.assertEqual(result['quiet_now_count'], 3)

    def test_expired_list_is_truncated_with_total_reported(self):
        """清单有上限，上限之下的总量一起报，否则模板里「完整的在导出里」没有依据。"""
        for index in range(EXPIRED_LIST_LIMIT + 5):
            make_search(
                self.owner, name=f'过期关注 {index}',
                quiet_until=self.now - timedelta(days=1),
            )

        result = build_saved_search_quiet_window(days=30, now=self.now)

        self.assertEqual(result['delivered']['row_total'], EXPIRED_LIST_LIMIT + 5)
        self.assertEqual(len(result['delivered']['rows']), EXPIRED_LIST_LIMIT)
        self.assertEqual(result['delivered']['row_limit'], EXPIRED_LIST_LIMIT)