# -*- coding: utf-8 -*-
"""延后的通知，延了之后回来了吗——「现在不方便」第一次被运营读到。

这一轮量的不是解析函数，是三件更根本的事。

第一件是**延后把「处理慢」拆成了两半**。一条 3 月 1 日创建、当天被延后 3 天、3 月 4 日到期
当天被读的通知，``notification_response`` 的处理时长读出来是 3 天，而用户实际的动作是「到期
当天就处理了」。这 3 天不是平台慢，是用户自己说「现在不方便」。测试必须钉住这条边界：扣掉
延期的处理时长从 ``snoozed_until`` 起算，不含延期的那一段从 ``created_at`` 起算，两个中位数
之差就是用户主动要求的等待。

第二件是**延后和未读是两种不同的沉默**。未读是提醒没被看见；延后是提醒被看见了、被明确地推到
以后。测试要确认「还在延后中」不计入阅读率分母——一条还没到期的通知说不出它会不会被读，把它
算进分母会让刚被延后的通知显得拉低阅读率。

第三件是**批量标记已读不清 ``snoozed_until``**。``mark_all_notifications_read`` 和
``mark_selected_notifications_read`` 走的是
``Notification.objects.filter(...).update(is_read=True, read_at=now)``，它不清 ``snoozed_until``。
一条还在延后中的通知被一键已读，落进的是「延后期间就被读了」——而用户只是按了「全部已读」，
并没有提前回来处理。反过来，到期之后被一键已读的那条，落进「到点之后才被读」。所以批量标记
会让两档都变浑。测试如实钉住这两个方向——它是写入路径的事实，不是本模块要修的东西。

本模块只报告：不加迁移、不加字段、不改写入路径、不自动取消延后、不自动标记已读、不按用户排名、
不列任何一条通知的原文。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Notification
from .notification_snooze_return import (
    MIN_BUCKET_SAMPLE,
    NOISE_DAYS,
    NOISE_POINTS,
    _bucket_label,
    _duration_label,
    _outcome_of,
    _return_lag_days,
    build_notification_snooze_return,
)


def local_noon():
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def make_user(username):
    return User.objects.create_user(
        username=username, password='safe-password-123',
    )


def make_notification(recipient, **kwargs):
    """建一条通知，再把自动时间列和延后时刻写回夹具指定的值。

    ``created_at`` 是 ``auto_now_add``，创建时传值会被忽略，所以建完之后再用 update 写回。
    否则所有通知都落在「现在」，到期时刻和处理时长全都读不出来。
    """
    notification = Notification.objects.create(
        recipient=recipient,
        kind=kwargs.pop('kind', 'message_received'),
        title=kwargs.pop('title', '一条通知'),
        message=kwargs.pop('message', '内容'),
    )
    fields = {}
    for name in ('created_at', 'read_at', 'snoozed_until'):
        if name in kwargs:
            fields[name] = kwargs.pop(name)
    for name in ('is_read',):
        if name in kwargs:
            fields[name] = kwargs.pop(name)
    if fields:
        Notification.objects.filter(pk=notification.pk).update(**fields)
        notification.refresh_from_db()
    return notification


class SnoozeReturnSetupTests(TestCase):
    """夹具、空态和统计窗口。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('snooze-return-user')

    def test_an_empty_period_reports_no_data_rather_than_zero(self):
        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertFalse(report['has_data'])
        self.assertEqual(report['totals']['sent_total'], 0)
        self.assertIsNone(report['totals']['snoozed_total'])
        self.assertEqual(report['return_lag']['median_days'], None)
        self.assertIn('还没有通知数据', report['summary'])

    def test_a_period_without_any_snooze_says_so(self):
        make_notification(self.user, created_at=self.now - timedelta(days=3))

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertTrue(report['has_data'])
        self.assertEqual(report['totals']['snoozed_total'], 0)
        self.assertIn('没有一条被延后过', report['summary'])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        report = build_notification_snooze_return(days=999, now=self.now)
        self.assertEqual(report['period_days'], 30)

    def test_a_notification_created_before_the_window_is_excluded(self):
        make_notification(
            self.user, created_at=self.now - timedelta(days=45),
            snoozed_until=self.now - timedelta(days=40),
        )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertFalse(report['has_data'])


class OutcomeTests(TestCase):
    """五种结局：一条被延后过的通知，现在落在哪里。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('snooze-outcome-user')

    def test_a_snooze_still_in_the_future_is_still_snoozed(self):
        row = make_notification(
            self.user, created_at=self.now - timedelta(days=2),
            snoozed_until=self.now + timedelta(days=1),
        )

        self.assertEqual(_outcome_of(
            {'is_read': False, 'read_at': None, 'snoozed_until': row.snoozed_until},
            self.now,
        ), 'still_snoozed')

    def test_reading_before_the_deadline_is_reading_while_snoozed(self):
        """不等了，提前回来处理。到期时刻还没到，谈不上「回来」。"""
        snoozed_until = self.now - timedelta(days=1)
        row = make_notification(
            self.user, created_at=self.now - timedelta(days=5),
            snoozed_until=snoozed_until, read_at=self.now - timedelta(days=2),
            is_read=True,
        )

        self.assertEqual(_outcome_of(
            {'is_read': True, 'read_at': row.read_at, 'snoozed_until': snoozed_until},
            self.now,
        ), 'read_while_snoozed')

    def test_reading_after_the_deadline_is_reading_after_return(self):
        snoozed_until = self.now - timedelta(days=3)
        row = make_notification(
            self.user, created_at=self.now - timedelta(days=10),
            snoozed_until=snoozed_until, read_at=self.now - timedelta(days=1),
            is_read=True,
        )

        self.assertEqual(_outcome_of(
            {'is_read': True, 'read_at': row.read_at, 'snoozed_until': snoozed_until},
            self.now,
        ), 'read_after_return')

    def test_an_expired_snooze_that_was_never_read_is_the_hardest_number(self):
        """到点之后仍未读。它不问「等了多久」，只问「到期了，还没读」。"""
        make_notification(
            self.user, created_at=self.now - timedelta(days=20),
            snoozed_until=self.now - timedelta(days=2),
        )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertEqual(report['totals']['unread_after_return_count'], 1)
        self.assertEqual(report['totals']['outcome_rows'][3]['label'], '到点之后仍未读')

    def test_an_already_read_notification_without_a_timestamp_is_its_own_row(self):
        """``read_at`` 上线之前标记的老数据，如实报出来、不猜、不并进任何一档。"""
        make_notification(
            self.user, created_at=self.now - timedelta(days=20),
            snoozed_until=self.now - timedelta(days=10), is_read=True,
        )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertEqual(report['totals']['read_without_timestamp_count'], 1)
        self.assertEqual(report['totals']['outcome_counts']['read_after_return'], 0)

    def test_a_bulk_mark_read_leaves_the_snooze_and_lands_in_while_snoozed(self):
        """批量标记已读不清 ``snoozed_until``，一条还在延后中的通知会落进「延后期间就被读了」。

        这是写入路径的事实：``mark_all_notifications_read`` 和
        ``mark_selected_notifications_read`` 走的是
        ``Notification.objects.filter(...).update(is_read=True, read_at=now)``，
        **它不清 ``snoozed_until``**。所以一条还没到期的通知被一键已读，``read_at`` 有了值而
        到期时刻还在未来，它落进的是「延后期间就被读了」这一档——而用户根本没有提前回来处理，
        他只是按了「全部已读」。这个档位因此混着两种来源：真的提前回来，和一键清空。

        测试如实钉住它——本模块不改写入路径，也不把这一档读成「用户会提前回来」。
        """
        make_notification(
            self.user, created_at=self.now - timedelta(days=5),
            snoozed_until=self.now + timedelta(days=2),
        )

        Notification.objects.filter(recipient=self.user, is_read=False).update(
            is_read=True, read_at=self.now,
        )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertEqual(report['totals']['read_while_snoozed_count'], 1)
        self.assertEqual(report['totals']['read_after_return_count'], 0)

    def test_a_bulk_mark_read_after_the_deadline_lands_in_after_return(self):
        """同一条写入路径，到期之后被一键已读，就落进「到点之后才被读」。

        和上一条钉住的是同一件事的两面：批量标记不清 ``snoozed_until``，落进哪一档只取决于
        到期时刻过没过。所以「延后期间就被读了」这一档里既有真的提前回来的人，也有一键清空的
        人——把两种来源分开需要一条写入路径，本轮不加。
        """
        make_notification(
            self.user, created_at=self.now - timedelta(days=10),
            snoozed_until=self.now - timedelta(days=3),
        )

        Notification.objects.filter(recipient=self.user, is_read=False).update(
            is_read=True, read_at=self.now,
        )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertEqual(report['totals']['read_after_return_count'], 1)
        self.assertEqual(report['totals']['read_while_snoozed_count'], 0)


class DecompositionTests(TestCase):
    """把延后期从处理时长里拆出来：三个数摆在一起，谁也不加谁。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('snooze-decompose-user')

    def test_the_requested_wait_is_split_out_of_the_handling_time(self):
        """3 月 1 日创建、当天延后 3 天、3 月 4 日到期当天被读。

        含延期的处理时长是 3 天（``notification_response`` 的口径），扣掉延期只剩 0 天——
        用户实际的动作是「到期当天就处理了」。这 3 天是用户自己说「现在不方便」。
        """
        make_notification(
            self.user, created_at=self.now - timedelta(days=3),
            snoozed_until=self.now, read_at=self.now, is_read=True,
        )

        report = build_notification_snooze_return(days=30, now=self.now)
        decomposed = report['decomposed']
        self.assertEqual(decomposed['with_snooze_median_days'], 3.0)
        self.assertEqual(decomposed['without_snooze_median_days'], 0.0)
        self.assertEqual(decomposed['gap_days'], 3.0)

    def test_the_requested_wait_is_a_lower_bound_because_snooze_overwrites(self):
        """一条被延后三次的通知，库里只留得下第三次的时刻，所以这一差是下界。"""
        make_notification(
            self.user, created_at=self.now - timedelta(days=10),
            snoozed_until=self.now - timedelta(days=2),
            read_at=self.now - timedelta(days=1), is_read=True,
        )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertEqual(report['decomposed']['requested_median_days'], 8.0)
        # 用户可能先延了 2 小时、再延 24 小时、最后延到 8 天后——8 天只是最后一次。

    def test_never_snoozed_notifications_do_not_join_the_decomposition(self):
        """从没被延后过的通知没有「回来」可言，不进扣掉延期那一组。"""
        make_notification(
            self.user, created_at=self.now - timedelta(days=5),
            read_at=self.now - timedelta(days=1), is_read=True,
        )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertEqual(report['decomposed']['read_total'], 0)
        self.assertIsNone(report['decomposed']['without_snooze_median_days'])


class ReturnLagTests(TestCase):
    """回来之后多久被读：分档、中位数与 P90，至今没读的是右删失样本。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('snooze-lag-user')

    def test_the_lag_is_measured_from_the_deadline_not_from_delivery(self):
        make_notification(
            self.user, created_at=self.now - timedelta(days=10),
            snoozed_until=self.now - timedelta(days=3),
            read_at=self.now - timedelta(days=1), is_read=True,
        )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertEqual(report['return_lag']['median_days'], 2.0)
        self.assertEqual(report['return_lag']['bucket_rows'][1]['count'], 1)

    def test_an_unread_return_is_right_censored_not_zero_days(self):
        """到期未读是右删失样本：它隔多久会被读还没发生，把它记成 0 天会让回来显得立刻被处理。"""
        make_notification(
            self.user, created_at=self.now - timedelta(days=10),
            snoozed_until=self.now - timedelta(days=2),
        )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertEqual(report['return_lag']['unread_after_return_count'], 1)
        self.assertIsNone(report['return_lag']['median_days'])
        self.assertEqual(report['return_lag']['bucket_rows'][0]['count'], 0)

    def test_bucket_labels_put_the_same_day_in_its_own_bucket(self):
        self.assertEqual(_bucket_label(0), '当天')
        self.assertEqual(_bucket_label(0.4), '当天')
        self.assertEqual(_bucket_label(1), '1-3 天')
        self.assertEqual(_bucket_label(7), '4-7 天')
        self.assertEqual(_bucket_label(400), '30 天以上')

    def test_duration_labels_use_the_three_options_the_writer_offers(self):
        """写入路径只给 2 小时、24 小时、3 天三档，这里按这三档归，不按连续值切。"""
        self.assertEqual(_duration_label(timedelta(hours=2)), '2 小时档')
        self.assertEqual(_duration_label(timedelta(hours=24)), '24 小时档')
        self.assertEqual(_duration_label(timedelta(days=3)), '3 天档')
        self.assertEqual(_duration_label(None), None)

    def test_the_p90_is_a_rank_that_really_happened(self):
        """最近秩 P90 取某次真实发生过的等待，不取插值——插值会落在第九次和第十次之间。"""
        # created_at 必须落在统计窗口内：窗口从「今天 - 29 天」的零点开始，
        # now - 30 天在窗口之前，整批都会被筛掉。这是夹具的边界，不是模块的边界。
        # 另外 index=9 会让 snoozed_until 和 read_at 同一天，那条是「延后期间就被读了」，
        # 不是「回来之后」——所以真实发生过的等待是 9 条，第 9 大的那条就是 9 天。
        for index in range(9):
            make_notification(
                self.user, created_at=self.now - timedelta(days=20),
                snoozed_until=self.now - timedelta(days=10 - index),
                read_at=self.now - timedelta(days=1), is_read=True,
            )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertEqual(report['return_lag']['read_after_return_count'], 9)
        self.assertEqual(report['return_lag']['p90_days'], 9.0)


class ContrastTests(TestCase):
    """延后过 vs 从没延后过的阅读率对照。只做对照，不给因果。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('snooze-contrast-user')

    def test_a_still_snoozed_notification_is_not_in_the_denominator(self):
        """一条还没到期的通知说不出它会不会被读，把它算进分母会让刚延后的显得拉低阅读率。"""
        make_notification(
            self.user, created_at=self.now - timedelta(days=2),
            snoozed_until=self.now + timedelta(days=1),
        )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertEqual(report['contrast']['snoozed_total'], 0)

    def test_a_gap_inside_the_noise_band_gets_no_direction(self):
        for _index in range(MIN_BUCKET_SAMPLE):
            make_notification(
                self.user, created_at=self.now - timedelta(days=20),
                snoozed_until=self.now - timedelta(days=5),
                read_at=self.now - timedelta(days=1), is_read=True,
            )
            make_notification(
                self.user, created_at=self.now - timedelta(days=20),
                read_at=self.now - timedelta(days=1), is_read=True,
            )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertTrue(report['contrast']['is_measurable'])
        self.assertFalse(report['contrast']['gap_is_meaningful'])
        self.assertIn('读不出方向', report['summary'])

    def test_a_gap_outside_the_noise_band_gets_a_direction_about_people(self):
        """差值出了噪声区间才给方向，而且方向说的是人，不是通知。"""
        for _index in range(MIN_BUCKET_SAMPLE):
            make_notification(
                self.user, created_at=self.now - timedelta(days=20),
                snoozed_until=self.now - timedelta(days=5),
                read_at=self.now - timedelta(days=1), is_read=True,
            )
            make_notification(
                self.user, created_at=self.now - timedelta(days=20),
            )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertTrue(report['contrast']['gap_is_meaningful'])
        self.assertIn('这是人的差别，不是通知的差别', report['summary'])

    def test_a_thin_sample_gets_no_direction(self):
        for _index in range(MIN_BUCKET_SAMPLE - 1):
            make_notification(
                self.user, created_at=self.now - timedelta(days=20),
                snoozed_until=self.now - timedelta(days=5),
                read_at=self.now - timedelta(days=1), is_read=True,
            )
            make_notification(
                self.user, created_at=self.now - timedelta(days=20),
            )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertFalse(report['contrast']['is_measurable'])
        self.assertFalse(report['contrast']['gap_is_meaningful'])
        self.assertNotIn('这是人的差别', report['summary'])


class CurrentStateTests(TestCase):
    """此刻：不按统计周期过滤。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('snooze-current-user')

    def test_an_overdue_snooze_from_before_the_window_is_not_hidden(self):
        """一个周期开始之前就到期未读的通知，恰恰是最不该被藏起来的一件。"""
        make_notification(
            self.user, created_at=self.now - timedelta(days=200),
            snoozed_until=self.now - timedelta(days=190),
        )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertEqual(report['current']['overdue_count'], 1)
        self.assertEqual(report['current']['overdue_max_days'], 190.0)
        self.assertEqual(report['current']['overdue_kind_rows'][0]['count'], 1)

    def test_a_still_snoozed_notification_is_counted_separately_from_overdue(self):
        make_notification(
            self.user, created_at=self.now - timedelta(days=2),
            snoozed_until=self.now + timedelta(days=1),
        )
        make_notification(
            self.user, created_at=self.now - timedelta(days=2),
            snoozed_until=self.now - timedelta(days=1),
        )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertEqual(report['current']['still_snoozed_count'], 1)
        self.assertEqual(report['current']['overdue_count'], 1)


class KindRowTests(TestCase):
    """按通知类型拆：延后规模、到点未读、扣掉延期的中位时长。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('snooze-kind-user')

    def test_types_are_ranked_by_overdue_count_not_by_database_order(self):
        for _index in range(3):
            make_notification(
                self.user, kind='order_status',
                created_at=self.now - timedelta(days=20),
                snoozed_until=self.now - timedelta(days=2),
            )
        make_notification(
            self.user, kind='message_received',
            created_at=self.now - timedelta(days=20),
            snoozed_until=self.now - timedelta(days=2),
        )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertEqual(report['kind_rows'][0]['label'], '订单状态更新')
        self.assertEqual(report['kind_rows'][0]['unread_after_return'], 3)
        self.assertEqual(report['kind_rows'][0]['is_small_sample'], True)

    def test_a_type_that_was_never_snoozed_gets_no_row(self):
        make_notification(
            self.user, kind='order_created',
            created_at=self.now - timedelta(days=2),
        )

        report = build_notification_snooze_return(days=30, now=self.now)
        self.assertEqual(report['kind_rows'], [])


class PanelAndExportTests(TestCase):
    """看板要真的把这套读法画出来、导出去。"""

    def setUp(self):
        self.now = local_noon()
        self.staff = make_user('snooze-panel-staff')
        self.staff.is_staff = True
        self.staff.save()
        self.user = make_user('snooze-panel-user')
        self.client.force_login(self.staff)

    def make(self, **kwargs):
        return make_notification(self.user, **kwargs)

    def fetch(self):
        html = self.client.get(
            reverse('operations_dashboard'), {'days': 30},
        ).content.decode('utf-8')
        text = self.client.get(
            reverse('operations_dashboard_export'), {'days': 30},
        ).content.decode('utf-8-sig')
        return html, text

    def test_the_panel_names_the_outcomes_and_their_caveat(self):
        self.make(
            created_at=self.now - timedelta(days=10),
            snoozed_until=self.now - timedelta(days=3),
            read_at=self.now - timedelta(days=1), is_read=True,
        )
        self.make(
            created_at=self.now - timedelta(days=20),
            snoozed_until=self.now - timedelta(days=2),
        )

        html, _ = self.fetch()
        self.assertIn('notification-snooze-return-panel', html)
        self.assertIn('延后的通知，延了之后回来了吗', html)
        self.assertIn('还在延后中', html)
        self.assertIn('到点之后仍未读', html)
        self.assertIn('回来之后多久被读', html)
        # 三个时长数字必须写在面板上，不能只写在模块里。
        self.assertIn('含延期', html)
        self.assertIn('扣掉延期', html)

    def test_the_export_names_the_section_and_its_caveats(self):
        self.make(
            created_at=self.now - timedelta(days=10),
            snoozed_until=self.now - timedelta(days=3),
            read_at=self.now - timedelta(days=1), is_read=True,
        )

        _, text = self.fetch()
        self.assertIn('延后的通知，延了之后回来了吗', text)
        self.assertIn('还在延后中', text)
        self.assertIn('回来之后多久被读', text)
        self.assertIn('不按统计周期过滤', text)
        self.assertIn('不加迁移', text)

    def test_the_dashboard_context_carries_the_report(self):
        self.make(created_at=self.now - timedelta(days=10))

        html, _ = self.fetch()
        self.assertIn('notification-snooze-return-panel', html)
        self.assertIn('用户主动要求的等待', html)

    def test_an_empty_period_says_so_instead_of_drawing_zeroes(self):
        html, text = self.fetch()
        self.assertIn('notification-snooze-return-panel', html)
        self.assertIn('还没有通知数据', html)
        self.assertNotIn('回来之后多久被读', html)
        self.assertNotIn('含延期的处理时长', text)
