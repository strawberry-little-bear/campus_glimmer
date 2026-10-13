# -*- coding: utf-8 -*-
"""通知聚合的质量：合并掉的是打扰，还是信息。

这一轮量的是通知系统里唯一一条**写入时合并**的路径。``create_notification`` 收到
``dedupe_key`` 之后，在窗口内找到未读的同类行就叠上去：``occurrence_count += 1``、
覆盖 ``message`` 与 ``target_url``、``title`` 不动。读这两个字段的地方只有一个——
``analytics.py`` 的 ``compression_rate``，而它是一个比例，比例天生看不出样本：
「合并率 60%」既可以是两分钟内的连击，也可以是拖了四天的堆积。

测试要钉住四件事：

1. **对照不能被类型构成冒充。** 被聚合过的行阅读率更低，不等于聚合有害——两组还差着
   类型的构成。模块只报差距，不给因果，测试确认它不下因果结论。
2. **未读行是右删失样本，不能当 0 秒算。** 当 0 会凭空拉低均值。这是本模块最容易出的
   一个错，测试必须有一条专门盯着它。
3. **跨度和次数是两个维度。** 9 次摊在 10 分钟内和 9 次摊在 5 天里是两件事，跨度分桶
   必须独立于次数分档。
4. **空平台报 None 不报 0。** 0 会被读成「发了没人读」，而事实是「没有样本」。

本模块只报告：不改 ``create_notification``、不改 ``dedupe_window_seconds`` 默认值、
不加迁移。测试里有一条确认这条边界——聚合的合并行为必须原样保留。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import Notification
from .notification_aggregation_quality import (
    MIN_BUCKET_SAMPLE,
    OCCURRENCE_BANDS,
    SPAN_BUCKETS,
    _band_index,
    _span_index,
    _span_seconds,
    build_notification_aggregation_quality,
)


def local_noon():
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def make_keyed_notification(recipient, **kwargs):
    """建一条带聚合键的通知，再把自动时间列写回夹具指定的值。

    ``created_at`` 是 ``auto_now_add``，创建时传值会被忽略，所以建完之后再用 update
    写回。否则所有通知都落在「现在」，跨度与阅读情况全都读不出来。
    """
    notification = Notification.objects.create(
        recipient=recipient,
        kind=kwargs.pop('kind', 'demand_match'),
        title=kwargs.pop('title', '一条聚合通知'),
        message=kwargs.pop('message', '内容'),
        dedupe_key=kwargs.pop('dedupe_key', 'key-1'),
        occurrence_count=kwargs.pop('occurrence_count', 1),
    )
    fields = {}
    for name in ('created_at', 'read_at', 'last_occurred_at'):
        if name in kwargs:
            fields[name] = kwargs.pop(name)
    for name in ('is_read', 'target_url'):
        if name in kwargs:
            fields[name] = kwargs.pop(name)
    if fields:
        Notification.objects.filter(pk=notification.pk).update(**fields)
        notification.refresh_from_db()
    return notification


class SetupTests(TestCase):
    """夹具、空态和统计窗口。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('aggregation-setup-user')

    def test_an_empty_period_reports_no_data_rather_than_zero(self):
        report = build_notification_aggregation_quality(days=30, now=self.now)
        self.assertFalse(report['has_data'])
        # 空平台报 None 不报 0：0 会被读成「发了没人读」，而事实是「没有样本」。
        self.assertIsNone(report['current']['aggregation_rate'])
        self.assertIsNone(report['comparison']['groups'][0]['read_rate'])
        self.assertIsNone(report['comparison']['groups'][1]['read_rate'])
        self.assertIsNone(report['structure']['missing_target_rate'])
        self.assertIn('还没有带聚合键的通知', report['summary'])

    def test_a_notification_without_a_dedupe_key_is_excluded(self):
        """没有聚合键的通知走的是一条不查重的路径，混进来会稀释对照组。

        ``create_notification`` 末尾直接 create，不查重。把它算进「1 次」这一档，
        「从没被聚合过」就不再是「键设了但窗口内没有第二次」，而是「从来没参与过这个机制」。
        """
        make_keyed_notification(self.user, dedupe_key='')
        report = build_notification_aggregation_quality(days=30, now=self.now)
        self.assertFalse(report['has_data'])

    def test_a_notification_created_before_the_window_is_excluded(self):
        make_keyed_notification(
            self.user, created_at=self.now - timedelta(days=45),
        )
        report = build_notification_aggregation_quality(days=30, now=self.now)
        self.assertFalse(report['has_data'])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        report = build_notification_aggregation_quality(days=999, now=self.now)
        self.assertEqual(report['period_days'], 30)

    def test_band_and_span_boundaries(self):
        """分档边界按文档给的上界走，不按感觉。

        8 次落「4–8 次」、9 次落「9 次及以上」；10 分钟整落「10 分钟内」而不是下一档。
        """
        self.assertEqual(_band_index(1), 0)
        self.assertEqual(_band_index(3), 1)
        self.assertEqual(_band_index(4), 2)
        self.assertEqual(_band_index(8), 2)
        self.assertEqual(_band_index(9), 3)
        self.assertEqual(_band_index(40), 3)
        self.assertEqual(_span_index(599), 0)
        self.assertEqual(_span_index(600), 1)
        self.assertEqual(_span_index(3599), 1)
        self.assertEqual(_span_index(3600), 2)
        self.assertEqual(_span_index(86400), 3)
        self.assertEqual(_span_index(90000), 3)


class SpanTests(TestCase):
    """跨度：第一条到最后一条隔了多久，与次数是两个维度。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('aggregation-span-user')

    def test_span_measures_first_to_last_not_created_to_read(self):
        """跨度读的是 ``last_occurred_at - created_at``，与阅读时间无关。

        一条被合并 9 次、四天后才被读的通知，跨度是四天里事件摊了多久，不是用户多久才读。
        这两个数混在一起，就看不出「事件摊得长」和「用户读得慢」哪一个是合并造成的。
        """
        created = self.now - timedelta(days=4)
        make_keyed_notification(
            self.user, occurrence_count=9,
            created_at=created, last_occurred_at=self.now - timedelta(days=1),
            is_read=True, read_at=self.now,
        )
        # 模块只接受 .values() 出来的 dict 行——它跑在只读查询的结果上，不碰模型实例。
        row = Notification.objects.values(
            'occurrence_count', 'created_at', 'last_occurred_at',
        ).get()
        self.assertEqual(_span_seconds(row), 3 * 86400)

    def test_a_single_occurrence_row_has_no_span(self):
        """1 次的行没有跨度——它没有被合并过。"""
        make_keyed_notification(
            self.user, occurrence_count=1,
            created_at=self.now - timedelta(days=2),
            last_occurred_at=self.now - timedelta(days=1),
        )
        row = Notification.objects.values(
            'occurrence_count', 'created_at', 'last_occurred_at',
        ).get()
        self.assertIsNone(_span_seconds(row))

    def test_a_merged_row_without_last_occurred_at_is_skipped(self):
        """合并发生在该字段上线之前的老数据，如实漏过去，不当 0 算。"""
        make_keyed_notification(
            self.user, occurrence_count=4,
            created_at=self.now - timedelta(days=2), last_occurred_at=None,
        )
        report = build_notification_aggregation_quality(days=30, now=self.now)
        total = sum(row['row_count'] for row in report['span_rows'])
        self.assertEqual(total, 0)

    def test_span_distribution_splits_minutes_hours_and_days(self):
        """同样被合并 3 次，跨度可以完全不同——分桶必须报得出这个差别。"""
        # 跨度从 created_at 起算：夹具直接给「第一条到最后一条隔了多久」，不再绕到
        # 「现在」上——否则跨度会被窗口起点一起决定，测的就不是分桶了。
        for index, span in enumerate((
            timedelta(minutes=5), timedelta(minutes=30),
            timedelta(hours=4), timedelta(days=2),
        )):
            created = self.now - timedelta(days=3)
            make_keyed_notification(
                self.user, occurrence_count=3, dedupe_key=f'key-{index}',
                created_at=created, last_occurred_at=created + span,
            )
        report = build_notification_aggregation_quality(days=30, now=self.now)
        counts = [row['row_count'] for row in report['span_rows']]
        self.assertEqual(counts, [1, 1, 1, 1])
        self.assertEqual(len(report['span_rows']), len(SPAN_BUCKETS))


class ComparisonTests(TestCase):
    """核心对照：聚合过的行 vs 从没被聚合过的行。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('aggregation-compare-user')

    def test_unread_rows_are_right_censored_and_not_counted_as_zero_seconds(self):
        """未读行的处理时长不存在，不是零。

        这是本模块最容易出的一个错：把未读行当 0 秒算进均值，会凭空把「已读样本的平均
        处理时长」拉低，让运营以为用户读得更快了。右删失样本单独数。
        """
        make_keyed_notification(
            self.user, occurrence_count=3, dedupe_key='read-one',
            created_at=self.now - timedelta(hours=3),
            is_read=True, read_at=self.now - timedelta(hours=2),
        )
        # 一条合并了 4 次、至今未读的行：它的时长不存在，绝不能当 0。
        make_keyed_notification(
            self.user, occurrence_count=4, dedupe_key='unread-one',
            created_at=self.now - timedelta(hours=3),
        )
        report = build_notification_aggregation_quality(days=30, now=self.now)
        merged = report['comparison']['groups'][1]
        self.assertEqual(merged['row_count'], 2)
        self.assertEqual(merged['read_count'], 1)
        self.assertEqual(merged['unread_count'], 1)
        # 只有一个已读样本，均值就是它自己：1 小时。
        self.assertEqual(merged['avg_seconds'], 3600)

    def test_read_rate_compares_rows_not_events(self):
        """阅读率的分母是行数，不是事件数。

        一条压着 9 件事的未读通知，用户一次点击就能消掉它，但它压着的事一件都没被处理。
        用行数衡量触达、用事件数衡量信息量，两者分开报，不混在一个分母里。
        """
        make_keyed_notification(
            self.user, occurrence_count=1, dedupe_key='single',
            created_at=self.now - timedelta(days=1),
            is_read=True, read_at=self.now,
        )
        make_keyed_notification(
            self.user, occurrence_count=9, dedupe_key='merged',
            created_at=self.now - timedelta(days=1),
        )
        report = build_notification_aggregation_quality(days=30, now=self.now)
        single, merged = report['comparison']['groups']
        self.assertEqual(single['row_count'], 1)
        self.assertEqual(merged['row_count'], 1)
        # 1/1 与 0/1：行数口径。若按事件数，第二组会变成 0/9，同一个数被稀释。
        self.assertEqual(single['read_rate'], 100.0)
        self.assertEqual(merged['read_rate'], 0.0)
        self.assertEqual(report['current']['event_count'], 10)

    def test_the_delta_is_reported_without_a_causal_claim(self):
        """只报差距，不下「聚合有害」的结论。

        两组之间差着类型的构成：被聚合的类型（求购匹配、关注搜索汇总）本身就可能比单次
        通知更容易被跳过。因果要控制构成才能说，而一张对照表做不到。
        """
        make_keyed_notification(
            self.user, occurrence_count=1, dedupe_key='a',
            created_at=self.now - timedelta(days=1),
            is_read=True, read_at=self.now,
        )
        make_keyed_notification(
            self.user, occurrence_count=5, dedupe_key='b',
            created_at=self.now - timedelta(days=1),
        )
        report = build_notification_aggregation_quality(days=30, now=self.now)
        self.assertEqual(report['comparison']['read_rate_delta'], -100.0)
        self.assertNotIn('导致', report['summary'])
        self.assertNotIn('有害', report['summary'])

    def test_a_small_sample_comparison_reports_numbers_without_a_verdict(self):
        """任一侧少于门槛，只报数不下判——小样本上的差距是噪声。"""
        make_keyed_notification(
            self.user, occurrence_count=1, dedupe_key='small',
            created_at=self.now - timedelta(days=1),
            is_read=True, read_at=self.now,
        )
        report = build_notification_aggregation_quality(days=30, now=self.now)
        single, merged = report['comparison']['groups']
        self.assertTrue(single['is_small_sample'])
        self.assertTrue(merged['is_empty'])
        self.assertFalse(report['comparison']['comparable'])
        self.assertIsNone(report['comparison']['read_rate_delta'])

    def test_both_sides_over_the_threshold_become_comparable(self):
        """两侧都过门槛，差距才被读成一个信号。"""
        for index in range(MIN_BUCKET_SAMPLE):
            make_keyed_notification(
                self.user, occurrence_count=1, dedupe_key=f's-{index}',
                created_at=self.now - timedelta(days=1),
                is_read=True, read_at=self.now,
            )
            make_keyed_notification(
                self.user, occurrence_count=6, dedupe_key=f'm-{index}',
                created_at=self.now - timedelta(days=1),
            )
        report = build_notification_aggregation_quality(days=30, now=self.now)
        self.assertTrue(report['comparison']['comparable'])
        self.assertEqual(report['comparison']['read_rate_delta'], -100.0)


class OccurrenceBandTests(TestCase):
    """按聚合次数分档。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('aggregation-band-user')

    def test_bands_partition_rows_without_gaps_or_overlaps(self):
        """每一行恰好落进一档，不重不漏。"""
        for index, count in enumerate((1, 1, 2, 3, 4, 8, 9, 25)):
            make_keyed_notification(
                self.user, occurrence_count=count, dedupe_key=f'band-{index}',
                created_at=self.now - timedelta(days=1),
            )
        report = build_notification_aggregation_quality(days=30, now=self.now)
        bands = report['occurrence_bands']
        self.assertEqual(len(bands), len(OCCURRENCE_BANDS))
        self.assertEqual([band['row_count'] for band in bands], [2, 2, 2, 2])
        self.assertEqual(
            sum(band['row_count'] for band in bands),
            report['current']['keyed_row_count'],
        )

    def test_an_empty_band_reports_none_not_zero(self):
        """空档的阅读率报 None：这一档没有样本，报 0% 会被读成「发了没人读」。"""
        make_keyed_notification(
            self.user, occurrence_count=1, dedupe_key='only-one',
            created_at=self.now - timedelta(days=1),
            is_read=True, read_at=self.now,
        )
        report = build_notification_aggregation_quality(days=30, now=self.now)
        for band in report['occurrence_bands']:
            if band['is_empty']:
                self.assertIsNone(band['read_rate'])
            else:
                self.assertEqual(band['read_rate'], 100.0)

    def test_compressed_events_count_the_difference_between_events_and_rows(self):
        """合并掉的事件数 = 事件数 - 行数。"""
        make_keyed_notification(
            self.user, occurrence_count=1, dedupe_key='one',
            created_at=self.now - timedelta(days=1),
        )
        make_keyed_notification(
            self.user, occurrence_count=4, dedupe_key='four',
            created_at=self.now - timedelta(days=1),
        )
        report = build_notification_aggregation_quality(days=30, now=self.now)
        self.assertEqual(report['current']['event_count'], 5)
        self.assertEqual(report['current']['keyed_row_count'], 2)
        self.assertEqual(report['current']['compressed_event_count'], 3)
        self.assertEqual(report['current']['aggregation_rate'], 60.0)


class KindRowsTests(TestCase):
    """按类型拆聚合：哪一类在反复响。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('aggregation-kind-user')

    def test_aggregation_rate_is_reported_per_kind(self):
        for index in range(3):
            make_keyed_notification(
                self.user, kind='demand_match', dedupe_key=f'dm-{index}',
                occurrence_count=2, created_at=self.now - timedelta(days=1),
            )
        make_keyed_notification(
            self.user, kind='saved_search_digest', dedupe_key='ss-1',
            occurrence_count=1, created_at=self.now - timedelta(days=1),
        )
        report = build_notification_aggregation_quality(days=30, now=self.now)
        rows = {row['kind']: row for row in report['kind_rows']}
        self.assertEqual(rows['demand_match']['row_count'], 3)
        self.assertEqual(rows['demand_match']['event_count'], 6)
        self.assertEqual(rows['demand_match']['aggregation_rate'], 50.0)
        self.assertEqual(rows['demand_match']['max_occurrence'], 2)
        self.assertEqual(rows['demand_match']['avg_occurrence'], 2.0)
        self.assertEqual(rows['saved_search_digest']['aggregation_rate'], 0.0)

    def test_a_small_sample_kind_is_flagged_not_ranked(self):
        """少于门槛的类型标出来，但不据此下「这一类在反复响」的结论。"""
        make_keyed_notification(
            self.user, kind='demand_match', dedupe_key='tiny',
            occurrence_count=9, created_at=self.now - timedelta(days=1),
        )
        report = build_notification_aggregation_quality(days=30, now=self.now)
        row = report['kind_rows'][0]
        self.assertTrue(row['is_small_sample'])
        self.assertFalse(any(row['kind'] == 'demand_match' for row in report['kind_rows'] if not row['is_small_sample']))

    def test_kinds_are_sorted_by_event_count_then_kind(self):
        for index in range(2):
            make_keyed_notification(
                self.user, kind='lifecycle_reminder', dedupe_key=f'lc-{index}',
                occurrence_count=5, created_at=self.now - timedelta(days=1),
            )
        make_keyed_notification(
            self.user, kind='demand_match', dedupe_key='dm-1',
            occurrence_count=3, created_at=self.now - timedelta(days=1),
        )
        report = build_notification_aggregation_quality(days=30, now=self.now)
        self.assertEqual(
            [row['kind'] for row in report['kind_rows']],
            ['lifecycle_reminder', 'demand_match'],
        )


class StructureTests(TestCase):
    """合并行的结构事实：落点缺失。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('aggregation-structure-user')

    def test_missing_target_url_is_counted_on_merged_rows_only(self):
        """没有落点的通知，用户读完了也无处可去。

        只统计被合并过的行：一条从没被合并过的通知同样可能没有 target_url，但那不是
        合并造成的结构事实。
        """
        make_keyed_notification(
            self.user, occurrence_count=3, dedupe_key='with-target',
            created_at=self.now - timedelta(days=1), target_url='/listings/item/1/',
        )
        make_keyed_notification(
            self.user, occurrence_count=4, dedupe_key='no-target',
            created_at=self.now - timedelta(days=1), target_url='',
        )
        make_keyed_notification(
            self.user, occurrence_count=1, dedupe_key='single-no-target',
            created_at=self.now - timedelta(days=1), target_url='',
        )
        report = build_notification_aggregation_quality(days=30, now=self.now)
        structure = report['structure']
        self.assertEqual(structure['merged_row_count'], 2)
        self.assertEqual(structure['missing_target_count'], 1)
        self.assertEqual(structure['missing_target_rate'], 50.0)

    def test_no_merged_rows_reports_none(self):
        make_keyed_notification(
            self.user, occurrence_count=1, dedupe_key='single',
            created_at=self.now - timedelta(days=1),
        )
        report = build_notification_aggregation_quality(days=30, now=self.now)
        self.assertTrue(report['structure']['is_empty'])
        self.assertIsNone(report['structure']['missing_target_rate'])


class AggregationBoundaryTests(TestCase):
    """本模块只报告，不改写入路径。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('aggregation-boundary-user')

    def test_the_write_path_aggregation_is_left_untouched(self):
        """``create_notification`` 的合并行为必须原样保留。

        本模块只读不改。若有人为了「让数据更好看」动了去重窗口或合并条件，这条测试会响。
        """
        from .notifications import create_notification

        first = create_notification(
            self.user, kind='demand_match', title='第一次', message='内容一',
            dedupe_key='boundary-key', dedupe_window_seconds=600,
        )
        self.assertEqual(first.occurrence_count, 1)

        second = create_notification(
            self.user, kind='demand_match', title='第二次', message='内容二',
            dedupe_key='boundary-key', dedupe_window_seconds=600,
        )
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.occurrence_count, 2)
        # 合并覆盖 message 与 target_url，title 停留在第一次——本模块只报这个结构，
        # 不改它。
        self.assertEqual(second.message, '内容二')
        self.assertEqual(second.title, '第一次')


class DashboardWiringTests(TestCase):
    """面板与导出：聚合的质量要出现在运营眼前。"""

    def setUp(self):
        self.now = local_noon()
        self.staff = User.objects.create_superuser(
            username='aggregation-staff', email='staff@example.com',
            password='safe-password-123',
        )

    def test_the_dashboard_panel_renders_the_comparison(self):
        self.client.force_login(self.staff)
        user = make_user('aggregation-wiring-user')
        make_keyed_notification(
            user, occurrence_count=1, dedupe_key='w-1',
            created_at=self.now - timedelta(days=1),
            is_read=True, read_at=self.now,
        )
        make_keyed_notification(
            user, occurrence_count=5, dedupe_key='w-2',
            created_at=self.now - timedelta(days=1),
            last_occurred_at=self.now - timedelta(hours=2),
        )

        response = self.client.get('/listings/operations/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('notification_aggregation_quality', response.context)
        content = response.content.decode('utf-8')
        self.assertIn('通知聚合的质量：合并掉的是打扰，还是信息', content)
        self.assertIn('从没被聚合过', content)
        self.assertIn('被聚合过', content)

    def test_the_csv_export_carries_the_same_section(self):
        self.client.force_login(self.staff)
        user = make_user('aggregation-export-user')
        make_keyed_notification(
            user, occurrence_count=4, dedupe_key='e-1',
            created_at=self.now - timedelta(days=1),
            last_occurred_at=self.now - timedelta(hours=1),
        )

        response = self.client.get('/listings/operations/export/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8-sig')
        self.assertIn('通知聚合的质量：合并掉的是打扰，还是信息', content)
        self.assertIn('聚合过的行 vs 从没被聚合过的行', content)
